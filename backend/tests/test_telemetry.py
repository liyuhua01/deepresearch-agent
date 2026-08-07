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


def test_llm_usage_aggregation_is_thread_safe() -> None:
    recorder = _recorder()

    def record_usage(_index: int) -> None:
        recorder.record_llm_call()
        recorder.record_llm_usage(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            source="provider",
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(record_usage, range(100)))

    metrics = recorder.snapshot()
    assert metrics["llm_calls"] == 100
    assert metrics["llm_usage_recorded_calls"] == 100
    assert metrics["prompt_tokens"] == 1000
    assert metrics["completion_tokens"] == 500
    assert metrics["total_tokens"] == 1500
    assert metrics["usage_source"] == "provider"


def test_source_provenance_metrics_join_the_terminal_snapshot() -> None:
    recorder = _recorder()
    recorder.record_source_catalog(
        5,
        needing_relevance_review=2,
        authoritative_sources=2,
    )
    recorder.record_claim_provenance(
        mapped=3,
        unmapped=2,
        unknown_source_ids=1,
        mismatched_source_ids=2,
        unlinked_source_ids=1,
    )
    recorder.record_provenance_audit(
        {
            "cited_catalog_source_count": 4,
            "cited_catalog_source_rate": 0.8,
            "report_unique_url_count": 5,
            "report_catalog_url_match_rate": 0.8,
            "report_relevant_cited_source_count": 3,
            "report_cited_source_relevance_rate": 0.75,
            "report_relevant_source_integrity_rate": 0.6,
            "uncatalogued_url_count": 1,
            "report_unknown_source_id_count": 1,
            "report_duplicate_citation_count": 4,
            "report_duplicate_citation_rate": 0.4,
            "report_max_source_citation_share": 0.5,
            "report_quality_retry_attempted": True,
            "report_quality_retry_applied": True,
            "final_claim_units": 10,
            "final_claim_units_with_citations": 8,
            "final_claim_citation_coverage": 0.8,
        }
    )
    recorder.mark_completed()

    metrics = recorder.snapshot()
    assert metrics["catalog_sources"] == 5
    assert metrics["catalog_sources_needing_relevance_review"] == 2
    assert metrics["catalog_authoritative_sources"] == 2
    assert metrics["context_source_count"] == 5
    assert metrics["context_relevant_source_count"] == 3
    assert metrics["context_source_relevance_rate"] == 0.6
    assert metrics["mapped_claims"] == 3
    assert metrics["unmapped_claims"] == 2
    assert metrics["unknown_source_ids"] == 1
    assert metrics["mismatched_source_ids"] == 2
    assert metrics["unlinked_source_ids"] == 1
    assert metrics["report_cited_catalog_sources"] == 4
    assert metrics["report_cited_catalog_source_rate"] == 0.8
    assert metrics["report_unique_urls"] == 5
    assert metrics["report_catalog_url_match_rate"] == 0.8
    assert metrics["report_relevant_cited_sources"] == 3
    assert metrics["report_cited_source_relevance_rate"] == 0.75
    assert metrics["report_relevant_source_integrity_rate"] == 0.6
    assert metrics["report_uncatalogued_urls"] == 1
    assert metrics["report_unknown_source_ids"] == 1
    assert metrics["report_duplicate_citations"] == 4
    assert metrics["report_duplicate_citation_rate"] == 0.4
    assert metrics["report_max_source_citation_share"] == 0.5
    assert metrics["report_quality_retry_attempted"] is True
    assert metrics["report_quality_retry_applied"] is True
    assert metrics["final_claim_units"] == 10
    assert metrics["final_claim_units_with_citations"] == 8
    assert metrics["final_claim_citation_coverage"] == 0.8
