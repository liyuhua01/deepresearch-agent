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

## 固定研究题目

1. 比较 RAG 与长上下文模型在企业知识问答中的适用边界，给出 2024 年以来的公开证据。
2. 梳理欧盟 AI Act 对通用人工智能模型提供者的主要义务与生效时间线。
3. 比较 PostgreSQL `pgvector`、Milvus 和 Qdrant 在中小型语义检索系统中的工程取舍。
4. 调研 2025 年以来浏览器端小语言模型推理的主要技术路线、性能约束和代表项目。
5. 分析 AI Agent 任务中 SSE、WebSocket 与轮询三种进度传输方案的优缺点。
6. 研究深度研究 Agent 中“引用存在但不支持结论”的原因，并总结可自动检测的方法。
7. 对比 Tavily、Perplexity Search API 与自建 SearXNG 用于研究智能体时的成本、稳定性和合规风险。
8. 设计一个面向公开演示的 LLM 应用成本防护方案，覆盖访问控制、限流、预算、日志与密钥管理。

前 4 题考察事实检索与时间敏感信息，后 4 题考察工程分析。正式录屏建议选第 5 或第 8 题，运行时间更可控，也和项目改造直接相关。

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

用于面试演示的候选配置，应在 8 题中至少 7 题成功；单题不超过 5 分钟；成功报告平均至少 5 个去重引用；抽查引用可访问率达到 80%。未达到时不要只换一个更贵的模型，应先按失败阶段区分搜索质量、网页抓取、上下文长度、输出解析和前端流式处理问题。

延迟分位数只有在每题至少重复 3 次、总计至少 24 个完成样本时才标记为 `repeatable_baseline`（可重复基线）；不足时自动标记为 `provisional`，不得写成稳定生产指标。

正式批量评测默认先请求 `/readyz` 做 capacity preflight（容量预检：在产生 LLM 费用前确认服务就绪且限额足够）。8 题 × 3 次至少需要 24 次剩余日额度，单 IP 限额也应至少为 24；不足时在首个研究请求前终止，避免产生不完整基线。断点续跑只按未完成样本计算所需容量。

引用语义支持率采用独立人工复核，不用“结论附近存在 URL”代替。先导出复核集：

```bash
cd backend
uv run python scripts/run_support_audit.py export-benchmark \
  --results-dir benchmarks/results/<benchmark-id> \
  --output benchmarks/results/<benchmark-id>/semantic-support-review.json \
  --sample-size 40
```

每条结论至少由两名复核者标注 `fully_supported`、`partially_supported`、`unsupported`、`inaccessible` 或 `insufficient_context`，再执行：

```bash
uv run python scripts/run_support_audit.py score \
  --annotations benchmarks/results/<benchmark-id>/semantic-support-review.json \
  --output benchmarks/results/<benchmark-id>/semantic-support-summary.json
```

输出同时包含严格口径、宽松口径及各自的 95% Wilson confidence interval（Wilson 置信区间：用于表达样本比例的不确定范围）。未完成双人标注的项目不进入支持率分母。
