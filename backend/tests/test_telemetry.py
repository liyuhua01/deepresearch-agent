"""Tests for fail-open, thread-safe research telemetry."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from evaluation.telemetry import RunRecorder


class FakeClock:
    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _recorder(clock: FakeClock | None = None) -> RunRecorder:
    return RunRecorder(
        run_id="run_test_1234",
        topic="测试主题",
        clock=clock or FakeClock(),
        wall_clock=lambda: datetime(2026, 8, 6, tzinfo=timezone.utc),
    )


def test_stage_timing_and_successful_terminal_snapshot() -> None:
    clock = FakeClock()
    recorder = _recorder(clock)

    with recorder.stage("planning"):
        assert recorder.current_stage() == "planning"
        clock.advance(0.125)

    recorder.record_tasks_planned(2)
    recorder.record_task_status(1, "completed")
    recorder.record_task_status(2, "skipped")
    clock.advance(0.375)
    recorder.mark_completed()

    metrics = recorder.snapshot()
    assert metrics["status"] == "completed"
    assert metrics["total_duration_ms"] == 500
    assert metrics["stage_durations_ms"] == {"planning": 125}
    assert metrics["stage_invocations"] == {"planning": 1}
    assert metrics["planned_subtasks"] == 2
    assert metrics["completed_subtasks"] == 1
    assert metrics["skipped_subtasks"] == 1


def test_stage_preserves_exception_and_failed_run_records_stage() -> None:
    recorder = _recorder()

    with pytest.raises(ValueError, match="planner failed"):
        with recorder.stage("planning"):
            raise ValueError("planner failed")

    recorder.mark_failed(ValueError("planner failed"))
    metrics = recorder.snapshot()
    assert metrics["status"] == "failed"
    assert metrics["failure_stage"] == "planning"
    assert metrics["failure_type"] == "ValueError"
    assert metrics["stage_failures"] == {"planning": 1}


def test_task_updates_are_idempotent_and_thread_safe() -> None:
    recorder = _recorder()
    recorder.record_tasks_planned(100)

    def complete(task_id: int) -> None:
        recorder.record_task_status(task_id, "in_progress")
        recorder.record_task_status(task_id, "completed")
        recorder.record_search_attempt()
        recorder.record_search_success()

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(complete, range(100)))

    metrics = recorder.snapshot()
    assert metrics["completed_subtasks"] == 100
    assert metrics["search_attempts"] == 100
    assert metrics["search_successes"] == 100


def test_search_fallback_counters_distinguish_empty_primary_result() -> None:
    recorder = _recorder()
    recorder.record_search_attempt()
    recorder.record_search_failure(empty_result=True)
    recorder.record_fallback_trigger()
    recorder.record_fallback_result(success=True)

    metrics = recorder.snapshot()
    assert metrics["search_attempts"] == 1
    assert metrics["search_failures"] == 1
    assert metrics["search_empty_results"] == 1
    assert metrics["fallback_triggers"] == 1
    assert metrics["fallback_successes"] == 1


def test_disabled_recorder_is_a_noop() -> None:
    recorder = RunRecorder(run_id="disabled_run", topic="测试", enabled=False)
    with recorder.stage("planning"):
        recorder.record_tasks_planned(5)
        recorder.record_search_attempt()
    recorder.mark_completed()

    metrics = recorder.snapshot()
    assert metrics["status"] == "disabled"
    assert metrics["planned_subtasks"] == 0
    assert metrics["search_attempts"] == 0


def test_cancelled_run_has_a_terminal_record() -> None:
    recorder = _recorder()
    recorder.mark_cancelled(stage="summarization")

    metrics = recorder.snapshot()
    assert metrics["status"] == "cancelled"
    assert metrics["failure_stage"] == "summarization"
    assert metrics["failure_type"] is None
