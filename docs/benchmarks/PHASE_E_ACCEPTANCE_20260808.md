# Phase E 质量与可重复性验收（进行中）

## 验收结论

当前状态：**工程门禁通过，完整 Agent 可重复性基线与双人语义复核待完成。**

本文只记录能够由代码、测试、部署响应或评测产物复算的结果。搜索级回归、历史单轮 Agent 结果和本轮完整 Agent 结果分开陈述，不互相替代。

## 版本与环境

- 分支：`codex/phase-a-telemetry`
- Phase E 评测能力提交：`ff90e0d`
- 临时预发布容量声明提交：`f5c0c87`
- 独立双人复核协议提交：`b5dfa43`
- 预发布地址：`https://deepresearch-agent-phase-a-staging.onrender.com`
- 固定题集：`backend/benchmarks/questions.json`，8 题
- 重复次数：每题 3 次，共 24 个完整 Agent 样本
- 执行模式：串行，避免并发争抢免费部署资源并保持样本条件一致

## 已通过的工程门禁

| 门禁 | 当前证据 | 状态 |
|---|---|---:|
| 后端非回归 | `uv run pytest -q`：105 项通过 | 通过 |
| 相关静态检查 | Phase E 变更文件 `ruff E/F/I` 检查通过 | 通过 |
| 前端构建 | `vue-tsc --noEmit && vite build` 成功 | 通过 |
| 容器构建 | `docker build` 成功 | 通过 |
| 容器健康 | `/healthz` 返回 200 | 通过 |
| 访问保护 | 未认证访问首页返回 401，正确密码返回 200 | 通过 |
| 预发布部署 | `/readyz` 返回新容量字段 | 通过 |
| 容量预检 | 24 次需求面对 20 次剩余额度时，在首个研究请求前拒绝 | 通过 |
| 双人复核完整性 | 独立文件合并会拒绝结论/URL 改写、缺失标签和复核者复用 | 通过 |

## 已有但不能冒充完整 Agent 基线的结果

DDGS（DuckDuckGo Search 的维护版多引擎客户端）主搜索对固定 8 题各运行 3 次：24/24 搜索调用返回结构化结果，应用层主搜索失败 0 次，P50 为 2.545 秒，P95 为 4.864 秒。该结果只覆盖 `dispatch_search()`，没有调用 LLM（大语言模型）或生成报告，因此不能用于声明 Agent 任务完成率或最终报告质量。详细证据见 `docs/benchmarks/DDGS_PRIMARY_24_20260807.md`。

历史 Phase D 单轮 8 题结果为 8/8 完成、加权 URL 可访问率约 85.5%，但对应提交为 `346f78b`，且当时主搜索失败率约 97%、全部由降级搜索恢复。该批次只能作为改造前参考，不能写成 Phase E 结果，也不能把单次样本的 P50/P95 写成稳定延迟。

## Phase E 正式指标与证据来源

| 指标 | 验收口径 | 权威产物 | 当前状态 |
|---|---|---|---:|
| 任务完成率 | `completed_count / run_count` | `summary.json` | 待 24 次运行 |
| 任务成功率 | 完成、报告非空、引用数达题目阈值且未超时 | `summary.json` | 待 24 次运行 |
| P50 / P95 | 8 题每题至少 3 个完成样本，总完成样本至少 24 | `summary.json` 中 `latency_percentiles_status=repeatable_baseline` | 待 24 次运行 |
| URL 可追溯率 | 报告 URL 能匹配本次研究来源目录的加权比例 | `report_catalog_url_match_rate_weighted` | 待 24 次运行 |
| 引用来源相关率 | 已引用且被来源追踪流程判定与题目相关的加权比例 | `report_cited_source_relevance_rate_weighted` | 待 24 次运行 |
| URL 可访问率 | 批量检查中可访问唯一 URL 数 / 已检查唯一 URL 数 | `citation_accessibility_rate_weighted` | 待 24 次运行 |
| 搜索失败率 | 搜索失败次数 / 搜索尝试次数 | `search_failure_rate` | 待 24 次运行 |
| 降级恢复率 | 降级成功次数 / 降级触发次数 | `fallback_recovery_rate` | 待 24 次运行 |
| 严格语义支持率 | 双人一致判定为完全支持 / 可判定样本 | `semantic-support-summary.json` | 待双人复核 |
| 宽松语义支持率 | 双人一致判定为完全或部分支持 / 可判定样本 | `semantic-support-summary.json` | 待双人复核 |
| 95% 置信区间 | 严格、宽松支持率的 Wilson 区间 | `semantic-support-summary.json` | 待双人复核 |

## 当前容量阻塞

2026-08-08 检查预发布 `/readyz` 的实际运行配置仍为：

```json
{
  "remaining": 20,
  "rate_limit_requests": 5,
  "rate_limit_window_seconds": 3600
}
```

`render.yaml` 已声明将 `DAILY_RESEARCH_BUDGET` 和 `RATE_LIMIT_REQUESTS` 临时提高为 30，但 Render 不会仅因普通代码自动部署就同步 Blueprint（基础设施配置清单）中的环境变量。必须在 Render 控制台同步 Blueprint，或手动将两个变量改为 30 并重新部署。完成 24 次评测后应恢复为 20 和 5。

## 容量恢复后的固定执行命令

不要在命令历史或文档中写入预发布密码，先通过当前终端安全设置 `BENCHMARK_ACCESS_PASSWORD`。然后执行：

```bash
cd backend
uv run python scripts/run_benchmark.py \
  --base-url https://deepresearch-agent-phase-a-staging.onrender.com \
  --output-dir benchmarks/results/phase-e-24-20260808 \
  --repetitions 3
```

24 次完成后，第一重复轮的 Q01–Q08 同时构成固定 8 题单轮验收；整个目录构成可重复性基线。不得为追求更好结果删除失败样本。中断后只能使用同一输出目录和 `--resume` 继续。

## 双人独立语义复核

从正式 24 次结果中按题目轮转抽取 40 条带引用事实结论：

```bash
uv run python scripts/run_support_audit.py export-benchmark \
  --results-dir benchmarks/results/phase-e-24-20260808 \
  --output semantic-support-authority.json \
  --sample-size 40
```

两名复核者分别复制权威模板，只编辑自己的副本，不查看对方标签。完成后运行：

```bash
uv run python scripts/run_support_audit.py merge \
  --review reviewer-a.json \
  --review reviewer-b.json \
  --output semantic-support-merged.json

uv run python scripts/run_support_audit.py score \
  --annotations semantic-support-merged.json \
  --output semantic-support-summary.json
```

如果两名复核者标签相同，则进入支持率分母；标签冲突则计入 disagreement（分歧）并留待第三方裁决，不自动选择更有利标签。不可访问与上下文不足样本单独计数，不混入可判定支持率分母。

## 简历表述门禁

只有在正式产物生成后，才能用真实数字替换下列占位符：

> 构建 Deep Research Agent 统一评测链路，覆盖固定 8 类复杂研究任务与 24 次重复实验；任务完成率达到 X%，P50/P95 端到端耗时为 X/Y 秒，报告 URL 可追溯率、引用来源相关率和可访问率分别达到 X%/X%/X%；通过双人独立复核统计结论—来源严格/宽松语义支持率及 95% 置信区间。

在 `latency_percentiles_status` 不是 `repeatable_baseline`、双人复核未完成或指标产物无法复算时，禁止使用这段量化表述。
