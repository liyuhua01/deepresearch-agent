# Deep Research Agent V4 演进蓝图

## 1. V4 的目标

V4 不是为了给项目贴上 LangGraph 标签，也不是重写 V3。V4 要解决的核心问题是：

> 系统如何根据当前证据覆盖、来源质量、来源冲突、信息增益和剩余预算，动态决定继续搜索、重新规划还是停止，并且让每次决定可恢复、可观察、可评测。

最终希望把 V3 的固定流程：

```text
Plan → Parallel Research → Summarize → Report
```

升级为有状态反馈循环：

```text
Analyze Query
      ↓
Plan
      ↓
Parallel Research
      ↓
Extract Evidence
      ↓
Check Coverage
  ┌───┴──────────────────────┐
  │                          │
充分 / 预算耗尽             不充分且仍有预算
  │                          │
  ↓                          ↓
Write Report          Replan Missing Work
  ↑                          │
  └──────── Research ◀───────┘
      ↓
Verify Citations
      ↓
END
```

## 2. 设计原则

1. **V3 是可比较 baseline。** 每一阶段都必须能与 V3 使用同一题库对比。
2. **先等价迁移，再增加行为。** 不同时替换编排器、模型封装、Prompt、搜索和前端协议。
3. **LangGraph 只负责机制。** 状态、路由、循环、checkpoint 和恢复由框架承载；Coverage、来源质量、冲突和预算策略由本项目设计。
4. **所有循环必须有硬边界。** 任何模型判断都不能绕过最大轮数、搜索数、Token、费用和时间预算。
5. **每个决定都要留下原因。** 需要能够回答“为什么补搜”和“为什么停止”。
6. **先做确定性指标，再增加 LLM Judge。** 避免用一个不可解释的浮点分数伪装算法。
7. **复用现有资产。** 保留 Vue、FastAPI、SSE、搜索、来源追踪、报告、评测和 Docker 部署。

## 3. 目标状态模型

V4 需要把当前 `SummaryState` 扩展成可持久化、可路由的 `ResearchState`。示意如下：

```python
class ResearchState(TypedDict):
    job_id: str
    query: str
    query_analysis: dict
    tasks: list[ResearchTask]
    evidence: list[Evidence]
    coverage_by_task: dict[str, CoverageResult]
    completed_task_ids: list[str]
    failed_task_ids: list[str]
    attempt_by_task: dict[str, int]
    research_round: int
    search_count: int
    token_usage: int | None
    estimated_cost: float | None
    deadline_at: str | None
    route_decisions: list[RouteDecision]
    report: str | None
    citation_audit: dict
    warnings: list[str]
```

所有进入 checkpoint 的字段必须可序列化。HTTP 客户端、LLM 客户端、锁、线程和文件句柄等运行时对象不得放入状态。

## 4. 新增领域模型

### 4.1 ResearchTask

在现有 `TodoItem` 基础上增加：

- `task_id`
- `parent_task_id`
- `question`
- `importance`
- `status`
- `attempt_count`
- `missing_aspects`
- `generated_queries`
- `stop_reason`

### 4.2 Evidence

`SourceRecord` 表示来源，`Evidence` 表示来源中支持研究判断的具体内容，两者不能混为一谈。

建议字段：

- `evidence_id`
- `task_id`
- `source_id`
- `claim`
- `supporting_excerpt`
- `stance`: `support`、`contradict` 或 `context`
- `relevance_score`
- `source_quality`
- `published_at`
- `content_fingerprint`
- `extraction_method`

### 4.3 CoverageResult

建议同时保留原始指标和最终决策，不能只保存一个总分：

- `direct_evidence_count`
- `independent_domain_count`
- `authoritative_source_count`
- `answered_aspects`
- `missing_aspects`
- `conflict_count`
- `new_evidence_count`
- `coverage_score`
- `confidence`
- `sufficient`
- `decision_reasons`

### 4.4 RouteDecision

每次条件路由记录：

- 决策前的关键状态；
- 选择的 route；
- 确定性规则命中情况；
- LLM 建议（如启用）；
- 最终原因；
- 当时剩余预算。

这是前端研究轨迹、故障分析和面试讲解的共同数据源。

## 5. 功能依赖关系

```text
V3 冻结与基线评测
        ↓
状态与事件协议设计
        ↓
LangGraph 等价迁移
        ↓
Evidence 模型与抽取
        ↓
Coverage 指标
        ↓
有界路由与停止条件
        ↓
Replan 与补搜循环
        ↓
Checkpoint / Resume
        ↓
研究轨迹前端
        ↓
A/B、边界、异常与 Failure Analysis
        ↓
根据实验结果调参和收敛
```

其中：

- 没有稳定 V3 基线，就不能证明 V4 的提升。
- 没有 Evidence，就无法可靠计算 Coverage 和冲突。
- 没有 Coverage，就没有合理的 Replan 触发条件。
- 没有有界停止条件，Replan 可能产生无限循环和不可控成本。
- 没有幂等节点和可序列化状态，Checkpoint 只能“保存”，不能可靠“恢复”。
- 没有 RouteDecision，前端和 Failure Analysis 都无法解释真实决策过程。

## 6. 分阶段实施计划

### 阶段 0：冻结 V3

**目标：** 建立不可漂移、可复现的对照组。

工作内容：

1. 完成 V3 工作区整理和提交。
2. 跑后端测试、前端构建、Docker 构建。
3. 使用固定配置运行基线题集。
4. 保存 commit、Tag、题库 hash、模型、搜索后端和指标。
5. 创建并推送 `v3.0.0` tag。
6. 从 V3 创建 V4 开发分支。

验收标准：任意时间都可以检出 V3，并使用记录的配置重新执行同一组评测。

### 阶段 1：定义 V4 状态和兼容协议

**目标：** 在写 Graph 前先明确数据边界。

工作内容：

- 定义 `ResearchState`、`ResearchTask`、`Evidence`、`CoverageResult` 和 `RouteDecision`。
- 定义节点输入输出，禁止节点隐式修改不可追踪的全局状态。
- 明确 reducer：哪些字段覆盖、哪些字段追加、哪些字段按 task_id 合并。
- 保持现有 API 与 SSE 事件兼容。
- 给新增内部事件预留版本字段，例如 `schema_version`。

验收标准：状态可 JSON 序列化；现有前端无需修改仍能消费旧事件。

### 阶段 2：LangGraph 等价迁移

**目标：** 先改变编排实现，不改变研究行为。

初始节点：

```text
plan → parallel_research → write_report → verify_citations
```

工作内容：

- 将现有 `PlanningService` 包装为 plan node。
- 将搜索和总结包装为 research node。
- 将 `ReportingService` 包装为 report node。
- 将已有 citation/provenance audit 包装为 verify node。
- 把 Graph stream 转换为现有 SSE 事件。
- 保留 HelloAgents 的模型封装，暂不全面迁移到 LangChain Agent。

验收标准：

- V3 与 Graph-only 版本完成率和输出协议无明显回归。
- 同样输入产生相同层次的任务、来源和报告。
- 前端、取消和评测 runner 继续工作。

这一阶段不是最终亮点，只是后续条件路由和恢复的地基。

### 阶段 3：Evidence Store

**目标：** 从“保存网页文本”升级为“保存可判断、可引用的证据”。

工作内容：

- 在搜索后增加 `extract_evidence` 节点。
- 将来源元数据与具体证据片段分离。
- 对相同 URL、相同内容指纹和近似 claim 去重。
- 保存支持、反驳和背景三种 stance。
- Evidence 必须回链到 task 和 source。
- 报告继续兼容现有来源编号体系。

验收标准：抽样证据能追溯到原始来源；不存在引用未知来源；重复证据率可计算。

### 阶段 4：Coverage Checker V1（确定性规则）

**目标：** 建立可解释的充分性判断，不急于使用 LLM 打分。

首版指标建议：

- 是否存在直接证据；
- 独立域名数量；
- 权威来源数量；
- 关键 aspect 覆盖数量；
- 空结果和失败情况；
- 支持/反驳冲突数量；
- 相对上一轮新增的有效证据数量。

示例规则：

```text
sufficient =
  关键 aspect 全部有直接证据
  AND 独立来源达到最低要求
  AND 高重要度任务至少有一个较高质量来源
  AND 不存在未处理的重大冲突
```

不同问题类型应允许不同规则；事实查询不应强制使用与复杂比较题相同的来源数量。

验收标准：每次判断输出原始指标、布尔结果和人类可读原因。

### 阶段 5：预算和停止条件

**目标：** 在引入循环之前确保循环必定终止。

硬边界至少包括：

- 最大研究轮数；
- 最大搜索调用数；
- 每个子任务最大尝试次数；
- 最大总运行时间；
- Token 上限；
- 费用上限（价格可用时）；
- 用户取消。

质量停止条件包括：

- Coverage 达标；
- 连续若干轮无新增有效证据；
- 补搜结果重复；
- 搜索后端持续失败；
- 剩余缺口无法通过公开网页合理解决。

停止时必须记录 `stop_reason`，并在证据不足的报告中明确不确定性。

验收标准：所有循环测试都能在可预测边界内结束；不能由 LLM 绕过硬预算。

### 阶段 6：Replan 与补搜循环

**目标：** 让 Agent 根据缺口采取有针对性的下一步，而不是重复原搜索词。

Replan 输入：

- 原问题和当前任务；
- 已有 Evidence 摘要；
- `missing_aspects`；
- 冲突描述；
- 已执行查询；
- 剩余预算。

Replan 输出：

- 新增或修订的 research task；
- 新查询及查询意图；
- 期望寻找的来源类型；
- 本轮成功条件。

典型路由：

```text
缺少权威来源 → 定向搜索官方/政府/论文来源
存在冲突     → 搜索独立第三方或原始材料
关键方面遗漏 → 创建补充子任务
结果重复     → 改写查询或停止
证据充分     → 进入报告阶段
预算耗尽     → 带限制说明进入报告阶段
```

验收标准：补搜查询与已识别缺口一致；同一查询不会无意义重复；所有路由有 `RouteDecision`。

### 阶段 7：Checkpoint / Resume

**目标：** 使长任务失败后不必从头支付搜索和模型成本。

实施顺序：

1. 本地使用 SQLite checkpointer 验证语义。
2. 使用现有 `job_id` 映射 Graph `thread_id`。
3. 新增 resume API，并定义 completed、failed、cancelled、resumable 状态。
4. 保证搜索和证据写入节点幂等；使用 task/attempt/evidence fingerprint 去重。
5. 注入节点失败，验证已完成节点不会重复运行。
6. 确认部署文件系统边界后，再决定是否使用 PostgreSQL。

验收标准：在 plan、research、coverage 和 replan 等不同位置注入失败后，能够恢复并避免重复计费和重复证据。

### 阶段 8：可靠性增强

**目标：** 统一处理第三方服务的不稳定性。

工作内容：

- 任务级最大并发和 semaphore；
- 分类重试：timeout、429、5xx 可重试，明显参数错误不重试；
- 指数退避和 jitter；
- 单任务失败不必导致整次研究失败；
- 搜索、抓取、LLM 各自设置超时；
- 明确 partial success 和 incomplete 报告语义。

验收标准：部分 worker 失败、限流和超时时，系统能在预算内降级完成或给出准确终态。

### 阶段 9：前端研究轨迹

**目标：** 展示本次任务真实经过的路径，而不是暴露复杂框架内部名称。

默认视图显示：

- 当前阶段；
- 子任务进度；
- 当前轮次；
- 来源和 Evidence 数量；
- 是否发生补搜；
- 补搜原因；
- 冲突状态；
- 为什么停止。

可折叠的面试/调试视图显示：

- 节点执行时间线；
- Coverage 原始指标；
- 路由选择和 `decision_reasons`；
- 剩余搜索、时间、Token 和费用预算；
- retry、fallback、failure 和 resume 记录。

前端不必直接渲染 LangGraph 的静态拓扑图；最有价值的是本次运行的实际轨迹。

验收标准：观看一次演示即可说清系统何时补搜、补搜了什么、为何停止。

### 阶段 10：评测、A/B 与 Failure Analysis

**目标：** 用数据证明哪些改造有效，以及它们的代价和失败边界。

#### 10.1 实验矩阵

至少保留以下可独立开关的版本：

| 实验组 | 说明 |
|---|---|
| V3 baseline | 当前固定流程 |
| V4 graph-only | 仅更换编排，不增加循环 |
| + Evidence | 增加结构化证据 |
| + Coverage | 增加充分性检查，但暂不补搜 |
| + Replan | Coverage 驱动有界补搜 |
| + Resume | 增加持久化恢复 |

同一题、同一模型、同一搜索后端、相近时间窗口下比较，避免把模型或搜索变化误认为架构提升。

#### 10.2 指标

质量指标：

- 任务完成率；
- 关键 aspect 覆盖率；
- Claim citation coverage；
- Citation semantic support；
- Unsupported claim rate；
- 权威来源覆盖率；
- 冲突识别率；
- 人工整体质量评分。

效率指标：

- 总延迟和 P50/P95；
- 搜索次数；
- 页面抓取数；
- LLM 调用和 Token；
- 单任务成本；
- 每次新增 Evidence 的边际成本；
- 补搜后有效 Evidence 增量。

可靠性指标：

- 超时率和失败率；
- retry 成功率；
- partial success rate；
- resume 成功率；
- 恢复时重复调用数；
- 无法解释的路由决策数。

#### 10.3 正常、边界和异常测试

正常场景：事实查询、比较题、多来源综合题。

边界场景：证据刚好达到阈值、预算只剩一次搜索、所有来源来自同一域、官方来源缺失、时间敏感信息、只有单一可靠来源。

异常场景：零搜索结果、403、429、timeout、5xx、无效 JSON、模型空输出、模型输出不存在的来源编号、单个 worker 崩溃、所有 worker 失败、恢复期间重复事件、数据库短暂不可用。

#### 10.4 Failure Analysis

建议在现有 `backend/src/evaluation/` 下新增：

```text
failure_analysis.py   # 自动归类失败阶段和失败类型
experiment.py         # 实验配置、版本标识和 A/B 元数据
comparison.py         # 同题不同版本的成对比较
```

每次运行产物至少保存：

- 版本、commit、配置和题目；
- plan 和全部查询；
- 每轮 Evidence 增量；
- Coverage 原始指标；
- RouteDecision；
- retry、fallback、stop reason；
- 报告和引用审计；
- 时间、Token、费用和终态；
- 自动失败分类；
- 人工复核标签和备注。

建议失败分类：

```text
planning_failure
irrelevant_query
search_empty
source_access_failure
evidence_extraction_failure
insufficient_coverage
missed_conflict
unnecessary_replan
repeated_search
budget_exhausted
unsupported_claim
citation_mismatch
report_generation_failure
checkpoint_failure
resume_duplication
```

Failure Analysis 的输出不是简单错误日志，而应回答：失败发生在哪个阶段、根因是什么、影响了哪个指标、是否可通过规则修复、修复后是否在固定回归集上改善。

验收标准：可以针对每个主要失败类别展示真实案例、复现输入、根因、修复和修复前后数据。

## 7. 推荐配置开关

为了支持渐进发布和 A/B，建议增加以下运行级开关：

```text
WORKFLOW_VERSION=v3|v4
ENABLE_EVIDENCE_EXTRACTION=true|false
ENABLE_COVERAGE_CHECK=true|false
ENABLE_REPLAN=true|false
ENABLE_CHECKPOINT=true|false
MAX_RESEARCH_ROUNDS
MAX_TOTAL_SEARCHES
MAX_TASK_ATTEMPTS
MAX_RUN_SECONDS
MAX_TOTAL_TOKENS
MAX_ESTIMATED_COST
MIN_INDEPENDENT_DOMAINS
MIN_AUTHORITATIVE_SOURCES
MIN_COVERAGE_SCORE
MIN_NEW_EVIDENCE_PER_ROUND
```

硬预算由后端确定性代码执行，不能只写入 Prompt。

## 8. 不在首轮 V4 范围内的内容

以下内容暂不作为主线：

- 全面改用 LangChain Agent；
- MCP 工具服务；
- 多 Agent 角色堆叠；
- 向量数据库；
- Redis 队列和多 worker 部署；
- LangSmith 强依赖；
- 一开始扩充到 100 道题；
- 为了展示而制作复杂的动态拓扑动画。

只有当核心循环、恢复和评测已经稳定，并且新组件解决了被实验确认的问题时，才进入上述工作。

## 9. V4 完成定义

V4 不是“成功安装 LangGraph”。完成至少需要满足：

1. V3 与 V4 均可从固定 commit 复现。
2. V4 具有显式、可序列化的 ResearchState。
3. Evidence 可以追溯到 task、source 和原始片段。
4. Coverage 决策包含原始指标和可读原因。
5. 证据不足时能够定向 Replan，而不是重复搜索。
6. 所有循环受到硬预算限制并记录 stop reason。
7. 节点失败后可以从 checkpoint 恢复，且不会明显重复计费。
8. 前端能够展示实际研究轨迹、补搜原因和停止原因。
9. 使用固定数据集完成 A/B 和消融实验。
10. 形成正常、边界、异常和 Failure Analysis 报告。

## 10. V4 希望形成的面试叙事

> 我先将具备完整搜索、流式交互、来源追踪和评测能力的 V3 固定为 baseline，然后在保持 API、前端和评测集不变的情况下，将核心编排迁移到 LangGraph。迁移本身不作为效果提升，我在其上设计了 Evidence 模型、确定性与语义结合的 Coverage Checker、有硬预算的 Replan 循环，以及可恢复的 checkpoint。最后通过逐组件消融、正常/边界/异常测试和 Failure Analysis 比较质量、延迟、成本与可靠性，明确哪些改造有效、在哪些场景会失败。

这才是 V4 的目标结果：不仅系统更复杂，而是每个设计都有原因、数据、代价和失败案例。

