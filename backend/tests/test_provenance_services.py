"""Service-level tests for provenance prompt and report integration."""

from __future__ import annotations

from config import Configuration
from evaluation.provenance import ClaimMapping, SourceRecord
from models import SummaryState, TodoItem
from services.reporter import ReportingService
from services.summarizer import SummarizationService


class FakeAgent:
    def __init__(self, response: str) -> None:
        self.response = response
        self.last_prompt = ""

    def run(self, prompt: str) -> str:
        self.last_prompt = prompt
        return self.response

    def clear_history(self) -> None:
        return None


class SequencedAgent(FakeAgent):
    def __init__(self, responses: list[str]) -> None:
        super().__init__("")
        self.responses = iter(responses)
        self.prompts: list[str] = []

    def run(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return next(self.responses)


def _source() -> SourceRecord:
    return SourceRecord(
        source_id="T1-S1",
        task_id=1,
        title="Python 官方文档",
        url="https://docs.python.org/3/library/asyncio.html",
        normalized_url="https://docs.python.org/3/library/asyncio.html",
        domain="docs.python.org",
    )


def test_summarizer_prompt_requires_only_catalogued_source_ids() -> None:
    service = SummarizationService(
        lambda: FakeAgent("unused"),
        Configuration(enable_notes=False, enable_source_provenance=True),
    )
    task = TodoItem(id=1, title="并发", intent="比较", query="asyncio")
    task.source_records = [_source()]

    prompt = service._build_prompt(SummaryState(research_topic="并发"), task, "证据")

    assert "本任务只允许使用这些来源编号：T1-S1" in prompt
    assert "[来源编号](对应URL)" in prompt
    assert "不得编造来源编号或 URL" in prompt


def test_summarizer_retries_when_first_turn_contains_only_dsml_tool_call() -> None:
    tool_only = (
        "<｜｜DSML｜｜TOOL_CALL_OVERALL>"
        '[{"name":"note","arguments":{"content":"internal"}}]'
        "</｜｜DSML｜｜TOOL_CALL_OVERALL>"
    )
    final_summary = (
        "## 任务总结\n- asyncio 适合高并发网络等待 "
        "[T1-S1](https://docs.python.org/3/library/asyncio.html)。"
    )
    agent = SequencedAgent([tool_only, final_summary])
    service = SummarizationService(
        lambda: agent,
        Configuration(enable_notes=False, enable_source_provenance=True),
    )
    task = TodoItem(id=1, title="并发", intent="比较", query="asyncio")
    task.source_records = [_source()]

    summary = service.summarize_task(SummaryState(research_topic="并发"), task, "证据")

    assert summary == final_summary
    assert len(agent.prompts) == 2
    assert "禁止再次调用任何工具" in agent.prompts[1]


def test_summarizer_discards_partial_tool_payload_before_summary_heading() -> None:
    response = (
        '我先同步笔记。\n, "content": "内部笔记"}]\n\n'
        "## 任务总结\n"
        "- asyncio 适合高并发网络等待，事件循环可避免为每个连接创建线程 "
        "[T1-S1](https://docs.python.org/3/library/asyncio.html)。"
    )
    agent = SequencedAgent([response])
    service = SummarizationService(
        lambda: agent,
        Configuration(enable_notes=False, enable_source_provenance=True),
    )
    task = TodoItem(id=1, title="并发", intent="比较", query="asyncio")
    task.source_records = [_source()]

    summary = service.summarize_task(SummaryState(research_topic="并发"), task, "证据")

    assert summary.startswith("## 任务总结")
    assert '"content"' not in summary
    assert len(agent.prompts) == 1


def test_summarizer_retries_for_nonempty_tool_payload_without_summary_heading() -> None:
    payload = '我先同步笔记。\n, "content": "只有内部笔记"}]'
    recovered = (
        "## 任务总结\n"
        "- asyncio 适合高并发网络等待，事件循环可避免为每个连接创建线程 "
        "[T1-S1](https://docs.python.org/3/library/asyncio.html)。"
    )
    agent = SequencedAgent([payload, recovered])
    service = SummarizationService(
        lambda: agent,
        Configuration(enable_notes=False, enable_source_provenance=True),
    )
    task = TodoItem(id=1, title="并发", intent="比较", query="asyncio")
    task.source_records = [_source()]

    summary = service.summarize_task(SummaryState(research_topic="并发"), task, "证据")

    assert summary == recovered
    assert len(agent.prompts) == 2


def test_reporter_expands_tokens_and_records_final_provenance_audit() -> None:
    fake_agent = FakeAgent("asyncio 适合网络等待 [T1-S1]。")
    service = ReportingService(
        fake_agent,
        Configuration(enable_notes=False, enable_source_provenance=True),
    )
    task = TodoItem(
        id=1,
        title="并发",
        intent="比较",
        query="asyncio",
        status="completed",
        summary="asyncio 适合网络等待 [T1-S1]。",
    )
    task.source_records = [_source()]
    task.claim_mappings = [
        ClaimMapping("T1-C1", 1, "asyncio 适合网络等待", ("T1-S1",), ())
    ]
    state = SummaryState(research_topic="并发", todo_items=[task])

    report = service.generate_report(state)

    assert report == (
        "asyncio 适合网络等待 [1](https://docs.python.org/3/library/asyncio.html)。"
    )
    assert "已提取的结论—来源映射" in fake_agent.last_prompt
    assert "无需每句话都添加" in fake_agent.last_prompt
    assert "背景阅读的资料可以只列在参考来源章节" in fake_agent.last_prompt
    assert state.provenance_audit["catalog_source_count"] == 1
    assert state.provenance_audit["cited_catalog_source_rate"] == 1.0
    assert state.provenance_audit["report_catalog_url_match_rate"] == 1.0


def test_reporter_applies_better_low_duplication_revision() -> None:
    repeated = (
        "## 核心洞见\n"
        "- 事实一 [T1-S1]。\n"
        "- 事实二 [T1-S1]。\n"
        "- 事实三 [T1-S1]。\n"
        "- 事实四 [T1-S1]。\n"
        "- 事实五 [T1-S1]。\n"
        "- 事实六 [T1-S1]。"
    )
    revised = (
        "## 核心洞见\n"
        "- 事实一由官方文档支持 [T1-S1]。\n"
        "- 事实二仍由同一官方文档支持 [T1-S1]。"
    )
    agent = SequencedAgent([repeated, revised])
    service = ReportingService(
        agent,
        Configuration(enable_notes=False, enable_source_provenance=True),
    )
    task = TodoItem(
        id=1,
        title="并发",
        intent="比较",
        query="asyncio",
        status="completed",
        summary="任务总结",
    )
    task.source_records = [_source()]
    task.claim_mappings = [
        ClaimMapping("T1-C1", 1, "事实一由官方文档支持", ("T1-S1",), ())
    ]
    state = SummaryState(research_topic="并发", todo_items=[task])

    report = service.generate_report(state)

    assert report.count("https://docs.python.org/3/library/asyncio.html") == 2
    assert len(agent.prompts) == 2
    assert "每个 URL 在全文最多出现 3 次" in agent.prompts[1]
    assert state.provenance_audit["report_quality_retry_attempted"] is True
    assert state.provenance_audit["report_quality_retry_applied"] is True
    assert state.provenance_audit["report_duplicate_citation_rate"] == 0.5


def test_reporter_does_not_retry_diverse_sources_repeated_in_reference_list() -> None:
    class DiverseAudit:
        report_citation_count_raw = 15
        report_duplicate_citation_rate = 2 / 3
        report_max_source_citation_share = 0.2

    assert ReportingService._needs_quality_retry(DiverseAudit()) is False


def test_reporter_rejects_revision_that_drops_source_diversity() -> None:
    second_source = SourceRecord(
        source_id="T1-S2",
        task_id=1,
        title="线程官方文档",
        url="https://docs.python.org/3/library/threading.html",
        normalized_url="https://docs.python.org/3/library/threading.html",
        domain="docs.python.org",
    )
    repeated = (
        "## 核心洞见\n"
        "- 事实一 [T1-S1]。\n"
        "- 事实二 [T1-S1]。\n"
        "- 事实三 [T1-S1]。\n"
        "- 事实四 [T1-S1]。\n"
        "- 事实五 [T1-S1]。\n"
        "- 事实六 [T1-S2]。"
    )
    lower_diversity = (
        "## 核心洞见\n"
        "- 事实一由单一文档支持 [T1-S1]。\n"
        "- 事实二仍由单一文档支持 [T1-S1]。"
    )
    agent = SequencedAgent([repeated, lower_diversity])
    service = ReportingService(
        agent,
        Configuration(enable_notes=False, enable_source_provenance=True),
    )
    task = TodoItem(
        id=1,
        title="并发",
        intent="比较",
        query="asyncio",
        status="completed",
        summary="任务总结",
    )
    task.source_records = [_source(), second_source]
    state = SummaryState(research_topic="并发", todo_items=[task])

    report = service.generate_report(state)

    assert "[2](https://docs.python.org/3/library/threading.html)" in report
    assert state.provenance_audit["report_quality_retry_attempted"] is True
    assert state.provenance_audit["report_quality_retry_applied"] is False
    assert state.provenance_audit["report_unique_url_count"] == 2


def test_disabled_provenance_preserves_report_text_and_prompt() -> None:
    fake_agent = FakeAgent("保留原始编号 [T1-S1]。")
    service = ReportingService(
        fake_agent,
        Configuration(enable_notes=False, enable_source_provenance=False),
    )
    task = TodoItem(
        id=1,
        title="并发",
        intent="比较",
        query="asyncio",
        status="completed",
        summary="原始总结",
    )
    task.source_records = [_source()]
    state = SummaryState(research_topic="并发", todo_items=[task])

    report = service.generate_report(state)

    assert report == "保留原始编号 [T1-S1]。"
    assert "已提取的结论—来源映射" not in fake_agent.last_prompt
    assert state.provenance_audit == {}
