"""Non-regression tests for telemetry attached to the existing agent flow."""

from __future__ import annotations

from threading import Event, Lock

import agent as agent_module
from agent import DeepResearchAgent
from config import Configuration
from evaluation.telemetry import RunRecorder
from models import TodoItem


class FakePlanner:
    def plan_todo_list(self, _state):
        return [
            TodoItem(
                id=1,
                title="证据检索",
                intent="找到证据",
                query="test query",
            )
        ]

    def create_fallback_task(self, _state):  # pragma: no cover - defensive API parity
        raise AssertionError("fallback should not be used")


class FakeSummarizer:
    def summarize_task(self, _state, _task, _context):
        return "任务总结"

    def stream_task_summary(self, _state, _task, _context):
        chunks = iter(["任务", "总结"])
        return chunks, lambda: "任务总结"


class FakeReporter:
    def generate_report(self, _state):
        return "# 最终报告"


class FakeToolTracker:
    def drain(self, _state, step=None):
        return []

    def set_event_sink(self, _sink):
        return None


def _agent(recorder: RunRecorder) -> DeepResearchAgent:
    instance = object.__new__(DeepResearchAgent)
    instance.config = Configuration(enable_notes=False)
    instance.cancel_event = Event()
    instance.recorder = recorder
    instance.note_tool = None
    instance.planner = FakePlanner()
    instance.summarizer = FakeSummarizer()
    instance.reporting = FakeReporter()
    instance._tool_tracker = FakeToolTracker()
    instance._tool_event_sink_enabled = False
    instance._state_lock = Lock()
    instance._last_search_notices = []
    return instance


def _fake_search(_query, _config, _loop_count, recorder=None):
    if recorder:
        recorder.record_search_attempt()
        recorder.record_search_success()
    return (
        {
            "results": [
                {
                    "title": "Source",
                    "url": "https://example.com/source",
                    "content": "Evidence",
                }
            ]
        },
        [],
        None,
        "fake",
    )


def test_non_streaming_agent_records_stages_without_changing_result(
    monkeypatch,
) -> None:
    recorder = RunRecorder(run_id="agent_sync", topic="测试主题")
    instance = _agent(recorder)
    monkeypatch.setattr(agent_module, "dispatch_search", _fake_search)
    monkeypatch.setattr(
        agent_module,
        "prepare_research_context",
        lambda *_args: ("- [Source](https://example.com/source)", "Evidence"),
    )

    result = instance.run("测试主题")

    assert result.report_markdown == "# 最终报告"
    metrics = recorder.snapshot()
    assert metrics["planned_subtasks"] == 1
    assert metrics["completed_subtasks"] == 1
    assert metrics["search_attempts"] == 1
    assert set(metrics["stage_invocations"]) == {
        "planning",
        "search",
        "summarization",
        "reporting",
    }


def test_streaming_agent_keeps_existing_event_contract(monkeypatch) -> None:
    recorder = RunRecorder(run_id="agent_stream", topic="测试主题")
    instance = _agent(recorder)
    monkeypatch.setattr(agent_module, "dispatch_search", _fake_search)
    monkeypatch.setattr(
        agent_module,
        "prepare_research_context",
        lambda *_args: ("- [Source](https://example.com/source)", "Evidence"),
    )

    events = list(instance.run_stream("测试主题"))
    event_types = [event["type"] for event in events]

    assert event_types[0] == "status"
    assert "todo_list" in event_types
    assert "sources" in event_types
    assert "task_summary_chunk" in event_types
    assert "final_report" in event_types
    assert event_types[-1] == "done"
    assert "metrics" not in event_types
    assert recorder.snapshot()["completed_subtasks"] == 1
