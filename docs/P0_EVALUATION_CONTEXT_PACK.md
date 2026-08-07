# Deep Research Agent P0 评测链路 Context Pack

> 状态：Phase A–D 已完成；Phase D 已通过伪 SSE 集成验收和真实预发布 8 题批跑；段落级数字引用与报告来源指标已完成单题预发布验收
>
> 适用范围：运行埋点、自动引用检查、固定评测执行器
>
> 第一原则：在现有研究能力上增加旁路评测能力，不改变或削弱已完成的研究流程。

## 1. 目标与非目标

### 1.1 本阶段目标

建立一条统一、可追踪、可导出的评测数据链路：

```text
研究请求（run_id）
  -> 运行记录器
  -> Agent 阶段计时
  -> LLM / 搜索调用统计
  -> 原有最终报告
  -> 报告完成后的引用审计
  -> 单次 JSON 结果
  -> 批量 CSV / JSON 汇总
```

完成后能够回答：任务是否成功、失败发生在哪个阶段、耗时多少、调用了多少次模型、消耗了多少 Token、搜索是否失败或降级、报告引用是否可访问、主要结论是否有引用，以及固定 8 题的整体完成率和分位耗时。

### 1.2 非目标

本阶段不做以下改变：

- 不替换 HelloAgents，不重写 Planning、Summarization 或 Reporting Agent。
- 不改变现有 Prompt（提示词：约束模型规划、总结和报告输出的文本指令）。
- 不改变现有 `/research`、`/research/stream`、`/research/{job_id}/cancel` 请求格式。
- 不改变前端现有任务规划、来源、阶段总结、工具调用、取消和最终报告交互。
- 不把 PostgreSQL、Redis、消息队列或多实例部署作为三个 P0 的前置条件。
- 不在 P0 阶段宣称“引用支持率”；自动程序先实现引用可访问率和结论引用覆盖率，语义支持率留待独立评审。

## 2. 当前系统基线

### 2.1 已完成能力

- Vue 3 + TypeScript 前端展示研究进度、子任务、来源、总结和最终报告。
- FastAPI 通过 SSE（服务器发送事件：服务端持续向浏览器推送进度）提供流式研究结果。
- 研究主题由规划、搜索、总结、报告四个主要阶段组成。
- 多个研究子任务使用线程并发执行。
- 支持 DuckDuckGo、Tavily、Perplexity、SearXNG；DuckDuckGo 失败或空结果时自动降级到 DDGS 多引擎搜索。
- 支持 Ollama、LM Studio 和 OpenAI-compatible API（兼容 OpenAI 请求格式的模型服务）。
- 已有 `job_id`、协作式取消、明确错误事件、Basic Auth、按 IP 限流、每日预算、健康检查和配置就绪检查。
- Vue 静态资源和 FastAPI 后端使用一个 Docker 镜像，在 Render 单 Web Service 中部署。

### 2.2 2026-08-06 非回归基线

```text
后端：8 passed
前端：vue-tsc --noEmit 通过
前端：vite production build 通过
当前 Git 提交：67e459f Add resilient search fallback
```

每个实施阶段开始前和合并前都必须重新运行：

```bash
cd backend && uv run pytest -q
cd ../frontend && npm run build
```

### 2.3 必须保持的外部契约

| 契约 | 当前行为 | P0 要求 |
|---|---|---|
| `POST /research/stream` | 接收 `topic`、可选 `search_api`、`job_id` | 请求字段保持不变 |
| SSE 事件 | 现有事件持续推送，最终以 `done` 结束 | 不删除、不重命名、不改变现有字段含义 |
| 取消接口 | 按 `job_id` 设置取消信号 | 路径、状态码和返回结构保持兼容 |
| 错误行为 | 配置错误返回 503；运行错误通过 SSE `error` 反馈 | 新埋点不得吞掉或替换原错误 |
| 搜索降级 | DuckDuckGo 失败或空结果切换 DDGS | 只记录，不改变触发条件 |
| 最终报告 | `final_report` 事件携带 Markdown 报告 | 引用审计不得修改报告正文 |
| 健康检查 | `/healthz` 和 `/readyz` 对外公开 | 新模块失败不得让 `/healthz` 失败 |

## 3. 统一数据链路设计

### 3.1 标识符

第一阶段定义 `run_id = job_id`。客户端仍使用现有 `job_id` 发起和取消任务，评测记录内部统一称为 `run_id`，避免同时维护两个生命周期相同的标识符。

批量评测执行器生成不可重复的 ID，例如：

```text
Q05-20260806T153012-7f31c2d8
```

### 3.2 RunRecorder

新增 `backend/src/evaluation/telemetry.py`，提供线程安全的 `RunRecorder`。它只收集数据，不决定 Agent 的业务流程。

核心职责：

- 记录开始、结束、成功、失败和取消状态。
- 记录配置、规划、搜索、抓取、总结、报告等阶段耗时。
- 记录规划、完成、失败、跳过的子任务数量。
- 累加 LLM 调用次数、Token 和费用。
- 累加搜索尝试、失败、降级触发和降级恢复。
- 输出只包含可公开评测字段的快照，不输出 API Key、完整 Prompt 或抓取正文。

并发安全要求：

- 计数器和集合写入使用 `threading.Lock`。
- 当前阶段与 `task_id` 使用 `threading.local()` 隔离。
- 端到端耗时使用单调时钟 `time.perf_counter()`。
- 并发子任务的阶段耗时可以分别记录，但不得相加后冒充端到端耗时。
- 只记录第一次导致整项研究失败的 `failure_stage`；子任务局部失败另存明细。

### 3.3 阶段模型

固定阶段名，避免后续 CSV 出现多个同义字段：

```text
configuration
planning
search
fetch
summarization
reporting
streaming
citation_audit
```

阶段上下文管理器的语义：

```python
with recorder.stage("planning"):
    todo_items = planner.plan_todo_list(state)
```

- 正常退出：累计耗时并标记成功。
- 异常退出：记录耗时、错误类型和失败阶段，然后原样重新抛出异常。
- 取消：记录 `cancelled`，继续沿用现有 `ResearchCancelledError`。

### 3.4 LLM 统计

新增 `backend/src/evaluation/instrumented_llm.py`，包装当前 `HelloAgentsLLM`：

- 保持 `invoke()` 和 `stream_invoke()` 的调用签名及文本输出不变。
- 非流式调用从 `response.usage` 读取真实 Token。
- 流式调用优先使用 `stream_options={"include_usage": true}` 获取最终 usage。
- 提供商不支持 usage 时，允许 Tokenizer Estimate（分词器估算：根据模型分词规则近似计算 Token）作为降级，但必须保存 `usage_source=estimated`。
- 无可靠分词器时保存 `usage_source=unavailable`，不得填入伪造的 0。
- 单次 LLM 调用统计失败不能导致研究任务失败；只把指标标记为不完整。

模型费用从独立价格配置读取。价格缺失时 `estimated_cost` 必须为 `null`，不能猜测单价。

### 3.5 搜索统计

在 `dispatch_search()` 的现有控制分支旁增加记录调用，不改变返回结构和降级条件：

- 原始后端与实际后端。
- 查询哈希、开始时间、耗时、结果数量。
- 主搜索成功、异常或空结果。
- 是否触发 DDGS 降级。
- 降级是否返回有效结果。
- 错误类型；不保存完整异常栈到公开结果。

核心计算口径：

```text
搜索失败率 = 主搜索失败或空结果次数 / 主搜索尝试次数
降级恢复成功率 = 降级后取得有效结果次数 / 降级触发次数
```

### 3.6 终态指标事件

为 SSE 增加新的可选 `metrics` 事件，但必须放在现有 `done` 之前：

```text
final_report -> metrics -> done
```

旧前端会忽略未知事件，因此保持向后兼容；新前端未来可以选择显示指标。失败和取消路径也应尽力发送 `metrics`，确保评测不会只保存成功样本。

若指标序列化失败：记录服务端日志并继续发送原有 `done` 或 `error`，不得破坏原研究结果。

### 3.7 本地 JSON 持久化

任务首次进入 `completed`、`failed` 或 `cancelled` 终态时，将当前快照写入 `RUN_METRICS_DIR/<run_id>.json`：

- 使用同目录临时文件、`fsync` 和原子替换，读取方不会看到半份 JSON。
- 文件权限设为 `0600`，文件名只接受安全的 `run_id` 字符。
- 写盘失败采用 fail-open，只增加 warning，不改变报告、异常或取消行为。
- 不保存 API Key、Authorization Header、完整 Prompt、抓取正文和最终报告。
- Render Free 本地文件会随重启或重新部署丢失；该层用于本地/预发布证据，长期历史需要外部数据库或对象存储。

### 3.8 来源溯源与结论映射

实施状态（2026-08-07）：已完成首轮真实预发布 A/B 及 B2–B6 同题修复复验。开启组首轮因 DSML 工具包装得到 0/2 映射；B3 修复了工具包装和编号—URL 校验。B5 捕获到搜索后端忽略官方域名限定、返回滑雪场页面的真实失败；B6 增加域名校验、受限多引擎恢复和离题来源剔除后，得到 3/3 Python 官方文档、3/3 URL HTTP 200、5/5 过程结论映射，且未知、错配、裸编号和待复核来源均为 0。Phase D 已在 8 题上验证完成率和引用指标。随后新增确定性数字引用转换和来源有效性指标；来源追踪默认仍保持关闭，待预发布复验后再决定是否默认开启。完整证据见 `docs/benchmarks/PROVENANCE_AB_20260807.md` 和 `docs/benchmarks/PHASE_D_8Q_20260807.md`。

段落级引用补充验收（2026-08-07）：预发布 commit `4632610` 完成 Q01，报告实际引用的 5/5 来源通过主题相关性初筛，5/5 URL 可追溯、5/5 可访问；结论邻近覆盖率为 52.94%，仅作诊断，不影响通过。完整证据见 `docs/benchmarks/RELAXED_CITATION_SMOKE_20260807.md`。

性能修复复验（2026-08-07）：commit `7dd5fa1` 在同一 Q01 上将不必要的报告质量重写从“触发并采用”降为“不触发”，LLM 调用由 6 次降至 5 次，reporting 阶段由 90.138 秒降至 26.657 秒，总耗时由 181.552 秒降至 108.595 秒；报告 URL 可追溯率、报告引用来源相关率和 URL 可访问率均保持 100%。该结果为单次同题方向性证据，正式性能结论仍需多次重复。

新增 `evaluation/provenance.py`，在报告生成之前建立可追踪关系，而不是只在成品报告中猜测引用对应关系：

```text
任务 1 搜索结果 -> T1-S1、T1-S2
任务 1 总结结论 -> T1-C1 -> [T1-S1]
最终报告 -> 内联来源/结论编号 -> 展开为 [1](真实 URL)、[2](真实 URL)
最终复核 -> URL 可追溯率、引用来源主题相关率、目录外 URL、未映射结论
```

- `source_id` 使用任务内稳定编号 `T{task_id}-S{index}`，不受并发完成顺序影响。
- 总结阶段只允许引用本任务目录中的来源编号；按主题段落或要点组提供来源，同一段多句话可以共用段尾引用，不要求逐句添加。没有证据的结论必须标记为待验证；工具调用包装会先被移除，若模型没有输出用户总结，则在同一上下文追加一次禁止工具调用的恢复请求。
- 只有来源编号和规范化 URL 准确配对才算有效映射；未知编号、错误 URL 配对和缺少 URL 的裸编号分别计数。
- 报告阶段接收结构化结论—来源映射，并把 `T1-S1` 或可映射的 `T1-C1` 确定性展开为 `[1](URL)` 形式的可点击数字引用；同一 URL 全文复用同一编号。无法映射的结论编号显示为“待验证”，不会伪造来源。
- 原有 `sources`、`task_status`、`final_report` 事件类型保持不变，只在开关启用时增加可选字段。
- 过程指标写入同一个终态 JSON，包括来源目录数、相关性待复核来源数、映射/未映射结论数、未知/错配/未链接编号、报告 URL 可追溯率、引用来源主题相关率、来源有效率、目录外 URL、重复引用率和最大单一来源占比。
- 来源追踪开启时，在原主检索之外增加一次最多 5 条的“官方文档/一手资料”补充检索；合并候选按主题词重合、来源类型、内容可用性和域名多样性排序，最多保留 8 条。补充检索失败采用 fail-open，继续使用主检索结果。
- 报告引用次数不少于 6、重复引用率超过 50%，并且最大单一来源占比同时超过 35% 时，才允许一次引用质量改写。这样不会因为同一来源分别出现在正文和参考来源区就额外调用模型。只有不存在目录外 URL、唯一来源没有明显减少、来源有效率不下降，并且重复率或集中度至少改善 10 个百分点时才采用改写结果。结论引用覆盖率不再参与硬性采用门槛。
- `ENABLE_SOURCE_PROVENANCE=false` 是初始安全默认值；完成预发布对照验收后再开启。
- A/B 验收可通过请求字段 `enable_source_provenance` 对单次运行显式覆盖；字段省略时仍严格遵循部署默认值，避免为了两组实验反复修改 Render 环境并重启服务。

## 4. 自动引用检查设计

### 4.1 运行位置

引用审计是报告完成后的旁路任务：

- 不修改 Agent 的输入、上下文或最终报告。
- 不影响 `final_report` 的生成成功判定。
- `total_duration_ms` 在报告生成完成时停止。
- 引用审计耗时单独保存为 `citation_audit_duration_ms`。
- 在线交互默认不等待完整 URL 网络检查；固定评测执行器在收到报告后执行完整审计。

该边界保证引用站点缓慢、403 或超时不会让原有研究功能失败。

### 4.2 提取与去重

新增：

```text
backend/src/evaluation/citations.py
backend/src/evaluation/domains.py
```

需要识别 Markdown 链接、裸 URL 和脚注 URL。规范化时：

- 域名转小写。
- 移除 `#fragment`。
- 移除 `utm_*` 等明确追踪参数。
- 移除默认端口。
- 保留可能改变页面内容的查询参数。
- 同时保留 `raw_url` 和 `normalized_url`，用后者去重。

### 4.3 可访问性检测

固定评测执行器使用异步 HTTP 客户端，最大并发 8：

- 先尝试 `HEAD`，不支持时回退到受限大小的流式 `GET`。
- 跟随最多 5 次重定向。
- 单链接超时 10 秒，最多重试 1 次。
- 不下载完整大文件。
- 状态分类为 `accessible`、`authentication`、`forbidden`、`not_found`、`timeout`、`dns_error`、`ssl_error`、`server_error`、`invalid_url`。

只有无需登录且能读取有效内容的链接计入“可访问”。

### 4.4 域名分类

第一阶段只做可解释的来源类型分类：政府/监管机构、学术论文、官方技术文档、新闻媒体、企业官网、社区内容、未知。分类描述来源类型，不等同于内容质量评分。

### 4.5 结论引用覆盖率

把 Markdown 按正文段落和列表项拆成 Claim Unit（结论单元：可以单独判断是否需要外部证据的一段正文或一个要点），排除标题、目录和纯结构性语句，然后计算：

```text
结论引用覆盖率 = 含至少一个引用的结论单元 / 需要证据的结论单元
```

这一指标只代表“结论附近是否有引用”，不代表引用在语义上真正支持结论。它保留为排查报告格式问题的诊断指标，不设置高覆盖率硬门槛，也不单独决定任务是否成功。

### 4.6 来源存在性与主题相关性

来源质量按三个可解释维度分别记录，避免把不同问题混成一个分数：

- `citation_accessibility_rate`：报告 URL 当前是否能通过受限网络检查正常读取，用来判断页面是否真实可访问。
- `report_catalog_url_match_rate`：报告 URL 中有多少能回溯到本次运行的来源目录，用来发现模型自行生成的目录外链接。
- `report_cited_source_relevance_rate`：已回溯来源中有多少通过查询词、标题和 URL 的自动主题相关性筛查。
- `report_relevant_source_integrity_rate`：报告全部唯一 URL 中，同时满足“来自本次来源目录”和“自动判定与主题相关”的比例。

这些指标用于定位搜索召回、来源绑定和链接可用性问题，不设置统一高阈值。自动相关性筛查是可复算的初筛，不等同于“该来源在语义上支持某一句结论”；需要对外声称支持率时仍应做人工抽样或独立评审。

## 5. 固定评测执行器设计

### 5.1 题库

把现有 `docs/DEMO_BENCHMARK.md` 的 8 道题复制为机器可读题库：

```text
backend/benchmarks/questions.json
```

每题包含 `id`、`category`、`topic`、`time_limit_seconds` 和 `minimum_unique_citations`。文档继续作为人类阅读版本，JSON 是执行权威源；后续用校验测试防止两者长期漂移。

### 5.2 执行路径

新增 `backend/scripts/run_benchmark.py`，默认通过真实 `/research/stream` API 执行，而不是绕过 Web 层直接调用 Agent，以覆盖 HTTP、FastAPI、SSE、取消和错误反馈。

执行器必须：

- 生成唯一 `run_id`。
- 解析并保存 SSE 原始终态事件。
- 在超时后调用现有取消接口。
- 单题失败后继续下一题。
- 支持 `--resume`，跳过已经有完整结果的运行。
- 默认串行发题，避免多个顶层研究互相竞争资源。
- 对最终报告执行引用审计。
- 输出逐次 JSON 和汇总 JSON/CSV。

### 5.3 产物结构

```text
backend/benchmarks/results/<timestamp>/
  manifest.json
  runs/Q01-run-01.json
  reports/Q01-run-01.md
  summary.json
  summary.csv
```

`manifest.json` 必须记录 Git commit、模型、搜索后端、检索轮数、是否并发、是否启用搜索降级和评测开始时间。不得记录密钥。

原始运行结果默认不提交 Git；经过人工检查、脱敏的汇总报告可以提交到 `docs/benchmarks/`。

### 5.4 Render 配额处理

当前默认配置是每 IP 每小时 5 次、实例每日 20 次研究，而固定题库包含 8 题。因此完整评测不能在未知公开访问者共享的生产配额中直接连续运行。

推荐路径：

1. 使用与 Render 镜像相同的 Docker 镜像，在本地或独立预发布 Render Service 上运行完整 8 题。
2. 预发布服务使用独立模型预算和访问密码，临时把频率上限调到覆盖评测规模。
3. 完成后恢复或删除预发布服务，不修改公开演示服务的保护参数。
4. 公网生产服务只做 1～2 道烟雾验证，确认真实 HTTPS、SSE 和模型配置可用。

不建议为评测在生产接口里加入隐藏的“免限流后门”。

## 6. 数据契约

单次 JSON 至少包含：

```text
schema_version
run_id
question_id
topic
git_commit
model
search_api
started_at
finished_at
status
failure_stage
failure_type
total_duration_ms
stage_durations_ms
planned_subtasks
completed_subtasks
failed_subtasks
llm_calls
prompt_tokens
completion_tokens
total_tokens
usage_source
estimated_cost
cost_currency
search_attempts
search_failures
fallback_triggers
fallback_successes
citation_count_raw
citation_count_unique
citation_accessible_count
citation_accessibility_rate
domain_count
claim_units
claim_units_with_citations
claim_citation_coverage
report_unique_urls
report_cited_catalog_sources
report_catalog_url_match_rate
report_relevant_cited_sources
report_cited_source_relevance_rate
report_relevant_source_integrity_rate
report_path
metrics_complete
warnings
```

所有比率保存 0～1 的原始小数，展示层再转换为百分比。分母为 0 时保存 `null`，不能用 0 混淆“没有发生”和“发生但全部失败”。

## 7. 功能开关与安全降级

新增环境变量，默认值以保护原行为为原则：

| 变量 | 默认值 | 作用 |
|---|---:|---|
| `ENABLE_RUN_TELEMETRY` | `true` | 收集轻量运行指标 |
| `PERSIST_RUN_METRICS` | `true` | 终态时原子写入单次 JSON |
| `RUN_METRICS_DIR` | `backend/data/run_metrics` | 本地指标目录 |
| `EMIT_METRICS_EVENT` | `false` | 是否向 SSE 客户端发送指标事件 |
| `ENABLE_INLINE_CITATION_AUDIT` | `false` | 在线请求是否执行网络引用检查 |
| `TOKEN_USAGE_FALLBACK` | `unavailable` | usage 缺失时不默认做不可靠估算 |
| `LLM_STREAM_USAGE` | `auto` | 仅为已知兼容端点请求流式 usage |
| `ENABLE_SOURCE_PROVENANCE` | `false` | 启用稳定来源编号、结论映射、来源质量排序和引用压缩 |
| `MODEL_PRICING_FILE` | 空 | 未配置时不计算费用 |

任一评测模块异常时采用 fail-open（开放式降级：评测失败但原业务继续运行）：

```text
埋点异常 -> metrics_complete=false -> 原研究继续
Token 读取失败 -> usage_source=unavailable -> 原模型输出继续
引用检查失败 -> 保存 warning -> 原报告保持成功
CSV 汇总失败 -> 保留逐次 JSON -> 不重新消耗模型运行
```

## 8. 分阶段实施与验收门

### Phase A：只增加运行记录器

实施状态（2026-08-06）：已完成代码接入并通过独立 Render 预发布验收。记录器不发送 `metrics` SSE 事件；原始研究输出、取消和错误事件保持兼容。

改动范围：数据模型、阶段计时、失败阶段、子任务和搜索计数。不开启 SSE 指标事件，不做引用网络请求。

验收：

- 原 8 项后端测试全部通过。
- 前端生产构建通过。
- 新增并发写入、成功、失败、取消测试。
- 使用假的 LLM / 搜索依赖完成一次确定性运行，原事件序列不变。

### Phase B：增加 LLM usage

实施状态（2026-08-06）：已完成代码、JSON 原子持久化和专项回归，并通过独立 Render 预发布验收。33 项后端测试、前端生产构建和 Docker 构建通过；真实、估算、不可用 usage 以及流式输出兼容均有自动测试。预发布真实研究用时约 167 秒，生成 5 组来源和 5990 字最终报告，SSE 正常以 `final_report -> done` 结束；取消接口返回 200 并以 `cancelled` 结束。两条路径均未新增 `metrics` 事件。公开生产 `main` 未更新。

改动范围：模型包装与 usage 统计。保持 Agent 调用接口和文本结果不变。

验收：

- 非流式和流式输出字符序列与包装前一致。
- 真实 usage、估算 usage、不可用三种状态均有测试。
- 指标异常不能导致研究失败。

### Phase C：增加引用审计库

实施状态（2026-08-06）：已完成本地实现与完整回归。新增纯函数引用提取、URL 规范化与去重、可解释域名分类、结论单元覆盖率，以及带并发/超时/重定向/下载上限和 SSRF 防护的异步可访问性检查。在线开关保持 `false`，未接入原研究主链路；42 项后端测试、前端生产构建和 Phase C Docker 镜像构建通过。对 Phase B 真实报告做离线试算得到原始 URL 16 个、去重 URL 15 个、12 个域名、结论引用覆盖率约 9.1%；该结果只代表引用邻近覆盖，不代表语义支持率。

改动范围：纯函数提取/分类与评测端网络检查。在线默认关闭完整审计。

验收：

- 使用固定 Markdown Fixture（测试样本）验证提取、规范化、去重和覆盖率。
- 所有网络状态使用 Mock（模拟响应）测试，不依赖外网通过 CI。
- 审计失败不会改变最终报告或研究状态。

### Phase D：增加固定评测执行器

改动范围：题库、CLI、JSON/CSV 汇总，不改前端。

验收：

- 使用假的 SSE 服务完成 8 题批跑测试。
- 验证超时取消、失败继续、断点恢复和幂等汇总。
- 在预发布环境完成至少 1 道真实题，再运行完整 8 题。

实施状态（2026-08-07）：已完成。新增机器可读 8 题题库、真实 SSE 串行执行器、超时取消、单题失败继续、`--resume`、`--rerun-failed`、原子产物写入、URL 可访问检查和 JSON/CSV 汇总。伪 SSE 服务验证了 8 题批跑、故意失败后继续、超时取消、断点恢复、只重跑失败题和字节级幂等汇总。真实预发布使用同一 commit/模型/搜索后端完成 8/8，P50/P95 耗时 168.22/287.64 秒，引用加权可访问率 85.51%，结论引用加权覆盖率 43.93%。详见 `docs/benchmarks/PHASE_D_8Q_20260807.md`。

### Phase E：Render 灰度发布

顺序：

1. 构建与当前一致的 Docker 镜像。
2. 部署预发布 Render Service。
3. 验证 `/healthz`、`/readyz`、登录、研究、取消、错误展示。
4. 运行 1 道端到端题并检查报告与原版本体验一致。
5. 完整跑 8 题和引用审计。
6. 再部署公开演示服务；初次部署保持 `EMIT_METRICS_EVENT=false`、`ENABLE_INLINE_CITATION_AUDIT=false`。
7. 验证无回归后，按需开启只读指标事件。

## 9. 回滚策略

- 新增功能全部受环境变量控制；首先关闭引用审计和指标事件，不需要回滚镜像。
- 运行记录器本身异常时自动 fail-open，不阻止请求。
- Render 保留上一个成功部署；新版本研究主链路异常时回滚到上一个镜像/提交。
- 评测产物与运行服务解耦，删除或重建汇总不会影响研究业务。
- 不在本阶段进行数据库迁移，因此没有数据结构回滚风险。

## 10. 需要新增的测试

至少新增以下测试，现有测试不得删除或放宽断言：

1. 阶段正常结束、异常和取消都记录正确。
2. 多线程写入不会丢失或重复计数。
3. 端到端耗时不等于并发阶段耗时简单相加。
4. 非流式 LLM usage 统计正确。
5. 流式 usage 统计正确且输出片段不变。
6. usage 不可用时不影响模型输出。
7. 主搜索异常触发降级并正确计数。
8. 主搜索空结果触发降级并正确计数。
9. Markdown 链接、裸 URL、脚注提取与去重正确。
10. URL 重定向、403、404、超时和 DNS 错误分类正确。
11. 结论引用覆盖率分母为 0 时返回 `null`。
12. SSE 失败和取消路径仍能保留原事件，并可选输出 metrics。
13. CLI 单题失败后继续下一题。
14. CLI 超时后调用取消接口。
15. CLI `--resume` 不重复产生模型费用。
16. JSON 和 CSV 汇总数值一致。
17. 输出中不包含 API Key、Authorization Header、完整 Prompt 或抓取正文。

## 11. Definition of Done

三个 P0 完成必须同时满足：

- 当前研究功能与前端体验没有可观察回归。
- 原 8 项后端测试和前端生产构建持续通过。
- 新增评测测试全部通过，且没有依赖真实外网的 CI 测试。
- 单次运行可生成 schema 固定的 JSON。
- 8 题批量执行可生成可复算的 JSON 和 CSV。
- 成功、失败、取消均有运行记录。
- Token 明确区分真实、估算和不可用。
- 引用检查明确区分可访问率、覆盖率和支持率。
- 评测模块故障不会阻断最终报告。
- Render 预发布完成研究、取消、错误和健康检查验收后，才允许更新公开演示服务。

## 12. 建议文件变更地图

```text
backend/src/evaluation/__init__.py             新增
backend/src/evaluation/telemetry.py            新增
backend/src/evaluation/instrumented_llm.py     新增
backend/src/evaluation/citations.py            新增
backend/src/evaluation/domains.py              新增
backend/src/agent.py                           小范围接入 RunRecorder
backend/src/main.py                            建立 run_id、可选 metrics 事件
backend/src/services/search.py                 增加旁路搜索统计
backend/src/config.py                          增加功能开关
backend/benchmarks/questions.json              新增机器可读题库
backend/scripts/run_benchmark.py               新增批量执行器
backend/tests/test_telemetry.py                 新增
backend/tests/test_instrumented_llm.py          新增
backend/tests/test_citations.py                 新增
backend/tests/test_benchmark_runner.py          新增
docs/DEMO_BENCHMARK.md                         补充运行命令和真实结果
docs/DEPLOYMENT.md                             补充预发布与功能开关
render.yaml                                    仅增加安全的默认开关
```

控制原则是“小范围接入、核心算法不重写、默认不增加在线网络开销、每个阶段都有独立回滚点”。
