# 固定演示题与评测记录

这组题目用于每次部署前 regression test（回归测试：用相同输入确认改动没有让结果退化），也用于向面试官展示你对 Agent 质量的量化意识。

## 指标口径

- **总耗时**：点击开始到最终报告出现；失败则记录到错误出现。
- **子任务数**：Agent 规划并实际执行的任务数量。
- **引用数**：最终报告中去重后的外部 URL 数量。
- **可访问率**：引用 URL 经网络检查后能够正常读取的比例，只衡量链接状态。
- **URL 可追溯率**：报告中的 URL 能回溯到本次检索来源目录的比例。
- **来源主题相关率**：可追溯来源中，通过自动主题相关性初筛的比例；不等同于逐句语义支持率。
- **结论引用覆盖率**：事实结论附近是否有链接的诊断指标，不设置高覆盖率硬门槛。
- **成功**：在预期时间内生成非空报告，并至少包含 3 个可核验引用。
- **失败阶段**：配置、规划、搜索、网页抓取、总结、最终报告或前端显示。

## 版本化研究题库

题库 `deep-research-20-v1` 共 20 题。原有 Q01–Q08 保留 `core`（核心）标签，以便和历史结果直接比较；Q09–Q20 使用 `extended`（扩展）标签，补充来源冲突、证据不足、安全、时效性和跨语言研究。

### 核心题 Q01–Q08

1. 比较 RAG 与长上下文模型在企业知识问答中的适用边界，给出 2024 年以来的公开证据。
2. 梳理欧盟 AI Act 对通用人工智能模型提供者的主要义务与生效时间线。
3. 比较 PostgreSQL `pgvector`、Milvus 和 Qdrant 在中小型语义检索系统中的工程取舍。
4. 调研 2025 年以来浏览器端小语言模型推理的主要技术路线、性能约束和代表项目。
5. 分析 AI Agent 任务中 SSE、WebSocket 与轮询三种进度传输方案的优缺点。
6. 研究深度研究 Agent 中“引用存在但不支持结论”的原因，并总结可自动检测的方法。
7. 对比 Tavily、Perplexity Search API 与自建 SearXNG 用于研究智能体时的成本、稳定性和合规风险。
8. 设计一个面向公开演示的 LLM 应用成本防护方案，覆盖访问控制、限流、预算、日志与密钥管理。

前 4 题考察事实检索与时间敏感信息，后 4 题考察工程分析。正式录屏建议选第 5 或第 8 题，运行时间更可控，也和项目改造直接相关。

### 扩展题 Q09–Q20

9. 核查不同机构对 AI 编程 Agent 基准测试成绩的报道差异，区分原始评测、厂商声明和二手解读，并说明哪些比较不能直接成立。
10. 核验“百万级上下文窗口已经使 RAG 过时”这一说法，分别寻找支持与反对证据，并标注证据适用条件。
11. 评估公开证据是否足以支持“浏览器端小模型可以全面替代企业云端推理”，证据不足时必须明确指出未知项和验证方案。
12. 依据浏览器标准和官方文档，核验 SSE 与 WebSocket 在自动重连、双向通信、代理兼容和连接限制方面的常见说法。
13. 分析研究型 Agent 从不可信网页读取内容时可能遭遇的提示注入攻击，给出输入隔离、工具权限、来源标记和输出审查方案。
14. 调查是否存在名为“OpenResearch Protocol 3.0”的正式行业标准；如果找不到可靠证据，不得补造定义，并说明检索范围和不确定性。
15. 比较厂商博客与独立技术资料对长上下文和 RAG 成本的估算，识别计费口径、缓存假设和工作负载差异造成的冲突。
16. 截至评测执行日期，对比主要模型提供商公开文档中的上下文窗口、输入输出价格和缓存计费，并记录资料更新时间。
17. 截至评测执行日期，核对欧盟 AI Act 中已经适用和即将适用的关键义务，优先引用欧盟官方来源并区分通过、生效与适用日期。
18. 截至评测执行日期，比较三个主流开源 Agent 框架最近一年的发布活跃度、维护状态和可观测性能力，说明选择依据。
19. 对照欧盟官方英文与至少一种非英文版本资料，核验通用人工智能模型义务的关键术语是否存在翻译差异及其影响。
20. 结合日文官方文件与英文资料，梳理日本面向企业的 AI 治理指导原则，并区分法律义务、政府指南和行业建议。

### 筛选与分批运行

执行器支持按题号、类别和标签筛选。各筛选维度之间为 AND（同时满足），同一维度重复参数为 OR（满足任意一个）。例如只运行核心工程题：

```bash
uv run python scripts/run_benchmark.py \
  --base-url <staging-url> \
  --tag core \
  --category engineering_analysis \
  --output-dir benchmarks/results/core-engineering
```

在 Render 每客户端每小时 5 次限制下，可把完整 20 题评测写入同一目录，每个时间窗口运行一批：

```bash
# 第一批
uv run python scripts/run_benchmark.py \
  --base-url <staging-url> \
  --output-dir benchmarks/results/deep-research-20-v1 \
  --batch-size 5 --batch-index 1

# 后续窗口依次把 batch-index 改为 2、3、4，并显式断点恢复
uv run python scripts/run_benchmark.py \
  --base-url <staging-url> \
  --output-dir benchmarks/results/deep-research-20-v1 \
  --batch-size 5 --batch-index 2 --resume
```

`manifest.json` 始终保存完整 20 题的题目摘要哈希；后续批次如修改题目内容、重复次数或筛选范围，执行器会拒绝混入旧结果。`summary.json` 会同时报告当前已运行样本和整个 campaign（评测活动）的完成比例。

## 运行记录

模型、搜索源、代码版本或关键提示词变化后，新开一行，不覆盖旧数据。

| 日期 | 代码版本 | 题号 | 模型 | 搜索源 | 耗时(s) | 子任务 | 引用数 | 可访问率 | 成功 | 失败阶段/备注 |
|---|---|---:|---|---|---:|---:|---:|---:|---|---|
| 2026-08-07 | 346f78b | 1 | deepseek-v4-flash | duckduckgo | 130.68 | 1 | 5 | 100% | 是 | 覆盖率 68.42% |
| 2026-08-07 | 346f78b | 2 | deepseek-v4-flash | duckduckgo | 103.28 | 1 | 5 | 80% | 是 | 覆盖率 61.11% |
| 2026-08-07 | 346f78b | 3 | deepseek-v4-flash | duckduckgo | 273.60 | 5 | 22 | 90.91% | 是 | 覆盖率 0% |
| 2026-08-07 | 346f78b | 4 | deepseek-v4-flash | duckduckgo | 171.64 | 1 | 5 | 80% | 是 | 覆盖率 81.82% |
| 2026-08-07 | 346f78b | 5 | deepseek-v4-flash | duckduckgo | 164.81 | 1 | 4 | 50% | 是 | 覆盖率 31.58% |
| 2026-08-07 | 346f78b | 6 | deepseek-v4-flash | duckduckgo | 295.20 | 5 | 20 | 80% | 是 | 覆盖率 89.29% |
| 2026-08-07 | 346f78b | 7 | deepseek-v4-flash | duckduckgo | 154.60 | 1 | 3 | 100% | 是 | 覆盖率 10.53% |
| 2026-08-07 | 346f78b | 8 | deepseek-v4-flash | duckduckgo | 188.75 | 1 | 5 | 100% | 是 | 覆盖率 8.33% |

## 最小验收线

用于面试演示的核心配置，应在 Q01–Q08 中至少 7 题成功；单题不超过 5 分钟；成功报告平均至少 5 个去重引用；抽查引用可访问率达到 80%。扩展20题结果必须另外报告，不用扩大后的分母改写历史8题成绩。未达到时不要只换一个更贵的模型，应先按失败阶段区分搜索质量、网页抓取、上下文长度、输出解析和前端流式处理问题。

延迟分位数只有在所选题集不少于8题、每题至少完成配置的3次重复时才标记为 `repeatable_baseline`（可重复基线）；不足时自动标记为 `provisional`，不得写成稳定生产指标。

正式批量评测默认先请求 `/readyz` 做 capacity preflight（容量预检：在产生 LLM 费用前确认服务就绪且本批容量足够）。分批模式只检查当前批次尚未完成的请求数；断点恢复会继续读取同一目录内此前批次的终态产物。

引用语义支持率采用独立人工复核，不用“结论附近存在 URL”代替。先导出复核集：

```bash
cd backend
uv run python scripts/run_support_audit.py export-benchmark \
  --results-dir benchmarks/results/<benchmark-id> \
  --output benchmarks/results/<benchmark-id>/semantic-support-review.json \
  --sample-size 40
```

将导出文件复制为两份，每名复核者只能查看并编辑自己的副本，为每条结论添加唯一的 `annotations` 标签：`fully_supported`、`partially_supported`、`unsupported`、`inaccessible` 或 `insufficient_context`。两人完成前不得交换标签。然后严格合并两个独立文件：

```bash
uv run python scripts/run_support_audit.py merge \
  --review reviewer-a.json \
  --review reviewer-b.json \
  --output semantic-support-merged.json
```

合并器会拒绝被修改的结论、URL、题号、重复复核者身份以及缺失标签。最后执行：

```bash
uv run python scripts/run_support_audit.py score \
  --annotations semantic-support-merged.json \
  --output benchmarks/results/<benchmark-id>/semantic-support-summary.json
```

输出同时包含严格口径、宽松口径及各自的 95% Wilson confidence interval（Wilson 置信区间：用于表达样本比例的不确定范围）。未完成双人标注的项目不进入支持率分母。
