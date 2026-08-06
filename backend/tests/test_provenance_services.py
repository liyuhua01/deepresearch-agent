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
        "asyncio 适合网络等待 "
        "[Python 官方文档](https://docs.python.org/3/library/asyncio.html)。"
    )
    assert "已提取的结论—来源映射" in fake_agent.last_prompt
    assert state.provenance_audit["catalog_source_count"] == 1
    assert state.provenance_audit["cited_catalog_source_rate"] == 1.0


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
