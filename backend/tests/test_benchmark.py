"""Integration tests for the fixed benchmark execution contract."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx
import pytest

from evaluation.benchmark import (
    BenchmarkQuestion,
    BenchmarkRunner,
    _validate_campaign_runs,
    build_summary,
    load_questions,
    parse_sse_lines,
    select_question_batch,
    select_questions,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
QUESTIONS_PATH = PROJECT_ROOT / "backend" / "benchmarks" / "questions.json"


def _event(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


def _metrics(run_id: str, *, status: str = "completed") -> dict:
    return {
        "type": "metrics",
        "job_id": run_id,
        "metrics": {
            "schema_version": "1.7",
            "run_id": run_id,
            "status": status,
            "failure_stage": "search" if status == "failed" else None,
            "failure_type": "RuntimeError" if status == "failed" else None,
            "total_duration_ms": 1200,
            "stage_durations_ms": {"planning": 100, "search": 500},
            "planned_subtasks": 2,
            "completed_subtasks": 2 if status == "completed" else 1,
            "failed_subtasks": 0 if status == "completed" else 1,
            "llm_calls": 4,
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "usage_source": "provider",
            "estimated_cost": 0.01,
            "cost_currency": "USD",
            "search_attempts": 2,
            "search_failures": 0 if status == "completed" else 1,
            "fallback_triggers": 1,
            "fallback_successes": 1,
            "report_unique_urls": 3,
            "report_cited_catalog_sources": 3,
            "report_catalog_url_match_rate": 1.0,
            "report_relevant_cited_sources": 2,
            "report_cited_source_relevance_rate": 2 / 3,
            "report_relevant_source_integrity_rate": 2 / 3,
            "metrics_complete": True,
            "warnings": [],
        },
    }


def test_question_file_has_twenty_versioned_questions_and_matches_document() -> None:
    schema, questions = load_questions(QUESTIONS_PATH)
    document = (
        (PROJECT_ROOT / "docs" / "DEMO_BENCHMARK.md")
        .read_text(encoding="utf-8")
        .replace("`", "")
    )

    assert schema == "1.1"
    assert [item.id for item in questions] == [
        f"Q{index:02d}" for index in range(1, 21)
    ]
    assert sum("core" in item.tags for item in questions) == 8
    assert sum("extended" in item.tags for item in questions) == 12
    assert all(item.topic in document for item in questions)


def test_question_selection_supports_ids_categories_and_tags() -> None:
    _, questions = load_questions(QUESTIONS_PATH)

    selected = select_questions(
        questions,
        question_ids=["Q05", "Q08", "Q13"],
        categories=["engineering_analysis", "robustness_safety"],
        tags=["safety"],
    )

    assert [item.id for item in selected] == ["Q08", "Q13"]
    with pytest.raises(ValueError, match="unknown benchmark question ids: Q99"):
        select_questions(questions, question_ids=["Q99"])


def test_question_batch_is_stable_and_rejects_out_of_range() -> None:
    _, questions = load_questions(QUESTIONS_PATH)

    assert [
        item.id
        for item in select_question_batch(questions, batch_size=5, batch_index=3)
    ] == ["Q11", "Q12", "Q13", "Q14", "Q15"]
    with pytest.raises(ValueError, match="available batch count 4"):
        select_question_batch(questions, batch_size=5, batch_index=5)


def test_loader_keeps_legacy_question_files_compatible(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy-questions.json"
    legacy_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "questions": [
                    {
                        "id": "LEGACY-1",
                        "category": "legacy",
                        "topic": "A valid legacy benchmark question",
                        "time_limit_seconds": 60,
                        "minimum_unique_citations": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    schema, questions = load_questions(legacy_path)

    assert schema == "1.0"
    assert len(questions) == 1
    assert questions[0].tags == ("legacy",)


def test_parse_sse_supports_comments_and_multiline_data() -> None:
    lines = [
        ": keepalive",
        'data: {"type":',
        'data: "done"}',
        "",
    ]

    assert list(parse_sse_lines(lines)) == [{"type": "done"}]


def test_fake_sse_runs_core_eight_and_continues_after_failure(tmp_path: Path) -> None:
    _, questions = load_questions(QUESTIONS_PATH)
    questions = select_questions(questions, tags=["core"])
    stream_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        assert request.headers["x-evaluation-metrics"] == "1"
        payload = json.loads(request.content)
        assert payload["max_web_research_loops"] == 3
        run_id = payload["job_id"]
        stream_calls.append(run_id)
        question_number = len(stream_calls)
        if question_number == 3:
            body = b"".join(
                [
                    _event({"type": "error", "detail": "search unavailable"}),
                    _event(_metrics(run_id, status="failed")),
                ]
            )
        else:
            report = (
                "# Report\n\n"
                "Fact one [A](https://example.com/a). "
                "Fact two [B](https://example.org/b). "
                "Fact three [C](https://example.net/c)."
            )
            body = b"".join(
                [
                    _event({"type": "final_report", "report": report}),
                    _event({"type": "done"}),
                    _event(_metrics(run_id)),
                ]
            )
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    runner = BenchmarkRunner(
        base_url="https://benchmark.test",
        output_dir=tmp_path,
        client=client,
        check_accessibility=False,
    )
    summary = runner.run(questions)

    assert len(stream_calls) == 8
    assert summary["run_count"] == 8
    assert summary["expected_run_count"] == 8
    assert summary["campaign_terminal_rate"] == 1.0
    assert summary["campaign_status"] == "complete"
    assert summary["completed_count"] == 7
    assert summary["benchmark_success_count"] == 7
    assert summary["failure_stage_counts"] == {"search": 1}
    assert summary["duration_ms"] == {"mean": 1200.0, "p50": 1200.0, "p95": 1200.0}
    assert summary["latency_sample_count"] == 7
    assert summary["latency_percentiles_status"] == "provisional"
    assert summary["search_failure_rate"] == 1 / 16
    assert summary["fallback_recovery_rate"] == 1.0
    assert summary["metrics_complete_count"] == 8
    assert summary["llm_calls_total"] == 28
    assert summary["tokens_total"] == 1050
    assert summary["citation_unique_total"] == 21
    assert summary["citation_accessibility_rate_weighted"] is None
    assert summary["claim_citation_coverage_weighted"] == 1.0
    assert summary["report_catalog_url_match_rate_weighted"] == 1.0
    assert summary["report_cited_source_relevance_rate_weighted"] == pytest.approx(
        2 / 3
    )
    assert summary["report_relevant_source_integrity_rate_weighted"] == pytest.approx(
        2 / 3
    )
    assert len(list((tmp_path / "runs").glob("*.json"))) == 8
    assert len(list((tmp_path / "reports").glob("*.md"))) == 8
    assert len((tmp_path / "summary.csv").read_text(encoding="utf-8").splitlines()) == 9
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "runs").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "summary.json").stat().st_mode) == 0o600


def test_latency_percentiles_require_three_runs_for_each_question() -> None:
    runs = [
        {
            "run_id": f"Q{question:02d}-{repetition}",
            "question_id": f"Q{question:02d}",
            "status": "completed",
            "benchmark_success": True,
            "total_duration_ms": 1000 + question,
        }
        for question in range(1, 9)
        for repetition in range(1, 4)
    ]

    summary = build_summary(
        runs,
        {
            "benchmark_id": "stable",
            "question_ids": [f"Q{question:02d}" for question in range(1, 9)],
            "repetitions": 3,
            "expected_run_count": 24,
        },
    )

    assert summary["latency_sample_count"] == 24
    assert summary["latency_percentiles_status"] == "repeatable_baseline"
    assert summary["latency_repetitions_by_question"]["Q01"] == 3


def test_preflight_rejects_insufficient_remote_budget(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/readyz"
        return httpx.Response(
            200,
            json={
                "status": "ready",
                "errors": [],
                "budget": {"used": 0, "limit": 20, "remaining": 20},
            },
        )

    runner = BenchmarkRunner(
        base_url="https://benchmark.test",
        output_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError, match="requires 24 runs.*20 budget remains"):
        runner.preflight_capacity(24)


def test_preflight_returns_ready_payload_when_capacity_is_sufficient(
    tmp_path: Path,
) -> None:
    expected = {
        "status": "ready",
        "errors": [],
        "budget": {"used": 2, "limit": 30, "remaining": 28},
    }
    runner = BenchmarkRunner(
        base_url="https://benchmark.test",
        output_dir=tmp_path,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=expected)
            )
        ),
    )

    assert runner.preflight_capacity(24) == expected


def test_preflight_allows_render_cold_start_window(tmp_path: Path) -> None:
    class RecordingClient:
        timeout: float | None = None

        def get(self, url: str, *, timeout: float) -> httpx.Response:
            del url
            self.timeout = timeout
            return httpx.Response(
                200,
                json={
                    "status": "ready",
                    "errors": [],
                    "budget": {"used": 0, "limit": 20, "remaining": 20},
                },
            )

    client = RecordingClient()
    runner = BenchmarkRunner(
        base_url="https://benchmark.test",
        output_dir=tmp_path,
        client=client,  # type: ignore[arg-type]
    )

    runner.preflight_capacity(5)

    assert client.timeout == 60


def test_preflight_rejects_per_client_limit(tmp_path: Path) -> None:
    payload = {
        "status": "ready",
        "errors": [],
        "budget": {
            "used": 0,
            "limit": 30,
            "remaining": 30,
            "rate_limit_requests": 5,
            "rate_limit_window_seconds": 3600,
        },
    }
    runner = BenchmarkRunner(
        base_url="https://benchmark.test",
        output_dir=tmp_path,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=payload)
            )
        ),
    )

    with pytest.raises(RuntimeError, match="per-client limit is 5 per 3600 seconds"):
        runner.preflight_capacity(24)


def test_pending_count_only_includes_failed_resume_artifacts(tmp_path: Path) -> None:
    _, questions = load_questions(QUESTIONS_PATH)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    (runs_dir / "Q01-run-01.json").write_text(
        json.dumps({"status": "completed"}), encoding="utf-8"
    )
    (runs_dir / "Q02-run-01.json").write_text(
        json.dumps({"status": "failed"}), encoding="utf-8"
    )
    runner = BenchmarkRunner(
        base_url="https://benchmark.test",
        output_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(lambda request: None)),
        resume=True,
        rerun_failed=True,
    )

    assert runner.pending_run_count(questions[:2]) == 1


class _TimeoutStream(httpx.SyncByteStream):
    def __iter__(self):
        yield _event({"type": "status", "message": "working"})
        raise httpx.ReadTimeout("deadline exceeded")


def test_timeout_requests_cancel_and_records_terminal_artifact(tmp_path: Path) -> None:
    _, questions = load_questions(QUESTIONS_PATH)
    cancelled: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/cancel"):
            cancelled.append(request.url.path.split("/")[-2])
            return httpx.Response(200, json={"status": "cancellation_requested"})
        return httpx.Response(
            200,
            stream=_TimeoutStream(),
            headers={"content-type": "text/event-stream"},
        )

    runner = BenchmarkRunner(
        base_url="https://benchmark.test",
        output_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        check_accessibility=False,
    )
    summary = runner.run(questions[:1])
    run = json.loads(next((tmp_path / "runs").glob("*.json")).read_text())

    assert len(cancelled) == 1
    assert run["status"] == "timeout"
    assert run["failure_stage"] == "client_timeout"
    assert summary["failure_stage_counts"] == {"client_timeout": 1}


def test_resume_skips_terminal_runs_and_rebuilds_identical_summary(
    tmp_path: Path,
) -> None:
    _, questions = load_questions(QUESTIONS_PATH)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = json.loads(request.content)
        report = (
            "Claim [A](https://example.com/a), [B](https://example.org/b), "
            "[C](https://example.net/c)."
        )
        return httpx.Response(
            200,
            content=b"".join(
                [
                    _event({"type": "final_report", "report": report}),
                    _event({"type": "done"}),
                    _event(_metrics(payload["job_id"])),
                ]
            ),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    first = BenchmarkRunner(
        base_url="https://benchmark.test",
        output_dir=tmp_path,
        client=client,
        check_accessibility=False,
    )
    first_summary = first.run(questions[:2])
    first_bytes = (tmp_path / "summary.json").read_bytes()

    resumed = BenchmarkRunner(
        base_url="https://benchmark.test",
        output_dir=tmp_path,
        client=client,
        check_accessibility=False,
        resume=True,
    )
    resumed_summary = resumed.run(questions[:2])

    assert calls == 2
    assert resumed_summary == first_summary
    assert (tmp_path / "summary.json").read_bytes() == first_bytes


def test_batches_accumulate_into_one_campaign_directory(tmp_path: Path) -> None:
    _, all_questions = load_questions(QUESTIONS_PATH)
    campaign = all_questions[:4]
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        calls.append(payload["topic"])
        report = (
            "Claim [A](https://example.com/a), [B](https://example.org/b), "
            "[C](https://example.net/c)."
        )
        return httpx.Response(
            200,
            content=b"".join(
                [
                    _event({"type": "final_report", "report": report}),
                    _event({"type": "done"}),
                    _event(_metrics(payload["job_id"])),
                ]
            ),
        )

    client = httpx.Client(transport=httpx.MockTransport(handler))
    first = BenchmarkRunner(
        base_url="https://benchmark.test",
        output_dir=tmp_path,
        client=client,
        check_accessibility=False,
    )
    first_summary = first.run(campaign[:2], campaign_questions=campaign)
    first_manifest = json.loads((tmp_path / "manifest.json").read_text())

    assert first_summary["run_count"] == 2
    assert first_summary["expected_run_count"] == 4
    assert first_summary["campaign_terminal_rate"] == 0.5
    assert first_summary["campaign_status"] == "partial"
    assert first_manifest["finished_at"] is None

    second = BenchmarkRunner(
        base_url="https://benchmark.test",
        output_dir=tmp_path,
        client=client,
        check_accessibility=False,
        resume=True,
    )
    final_summary = second.run(campaign[2:], campaign_questions=campaign)
    final_manifest = json.loads((tmp_path / "manifest.json").read_text())

    assert len(calls) == 4
    assert final_summary["run_count"] == 4
    assert final_summary["campaign_terminal_rate"] == 1.0
    assert final_summary["campaign_status"] == "complete"
    assert final_manifest["finished_at"] is not None
    assert final_manifest["question_ids"] == [item.id for item in campaign]


def test_resume_rejects_changed_question_definitions(tmp_path: Path) -> None:
    _, questions = load_questions(QUESTIONS_PATH)
    campaign = questions[:1]
    runner = BenchmarkRunner(
        base_url="https://benchmark.test",
        output_dir=tmp_path,
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    content=b"".join(
                        [
                            _event(
                                {
                                    "type": "final_report",
                                    "report": (
                                        "Claim [A](https://example.com/a), "
                                        "[B](https://example.org/b), "
                                        "[C](https://example.net/c)."
                                    ),
                                }
                            ),
                            _event({"type": "done"}),
                            _event(_metrics("ignored-run-id")),
                        ]
                    ),
                )
            )
        ),
        check_accessibility=False,
    )
    runner.run(campaign)
    changed = [
        BenchmarkQuestion(
            id=campaign[0].id,
            category=campaign[0].category,
            topic="changed after first batch",
            time_limit_seconds=campaign[0].time_limit_seconds,
            minimum_unique_citations=campaign[0].minimum_unique_citations,
            tags=campaign[0].tags,
        )
    ]

    resumed = BenchmarkRunner(
        base_url="https://benchmark.test",
        output_dir=tmp_path,
        client=httpx.Client(transport=httpx.MockTransport(lambda request: None)),
        check_accessibility=False,
        resume=True,
    )
    with pytest.raises(ValueError, match="definitions do not match"):
        resumed.run(changed)


def test_campaign_rejects_foreign_terminal_artifact() -> None:
    with pytest.raises(ValueError, match="unknown question run: Q99"):
        _validate_campaign_runs(
            [{"question_id": "Q99", "repetition": 1}],
            {"question_ids": ["Q01"], "repetitions": 1},
        )


def test_resume_can_rerun_only_failed_artifacts(tmp_path: Path) -> None:
    _, questions = load_questions(QUESTIONS_PATH)
    calls: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        topic = payload["topic"]
        calls[topic] = calls.get(topic, 0) + 1
        should_fail = topic == questions[1].topic and calls[topic] == 1
        if should_fail:
            events = [
                _event({"type": "error", "detail": "temporary failure"}),
                _event(_metrics(payload["job_id"], status="failed")),
            ]
        else:
            report = (
                "Claim [A](https://example.com/a), [B](https://example.org/b), "
                "[C](https://example.net/c)."
            )
            events = [
                _event({"type": "final_report", "report": report}),
                _event({"type": "done"}),
                _event(_metrics(payload["job_id"])),
            ]
        return httpx.Response(200, content=b"".join(events))

    client = httpx.Client(transport=httpx.MockTransport(handler))
    BenchmarkRunner(
        base_url="https://benchmark.test",
        output_dir=tmp_path,
        client=client,
        check_accessibility=False,
    ).run(questions[:2])

    summary = BenchmarkRunner(
        base_url="https://benchmark.test",
        output_dir=tmp_path,
        client=client,
        check_accessibility=False,
        resume=True,
        rerun_failed=True,
    ).run(questions[:2])

    assert calls[questions[0].topic] == 1
    assert calls[questions[1].topic] == 2
    assert summary["completed_count"] == 2


def test_summary_uses_linear_interpolated_p50_and_p95() -> None:
    runs = [
        {
            "run_id": f"r{index}",
            "status": "completed",
            "benchmark_success": True,
            "total_duration_ms": duration,
        }
        for index, duration in enumerate([100, 200, 300, 400])
    ]

    summary = build_summary(runs, {"benchmark_id": "b1"})

    assert summary["duration_ms"]["p50"] == 250.0
    assert summary["duration_ms"]["p95"] == 385.0
