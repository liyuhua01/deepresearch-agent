"""Integration tests for the fixed benchmark execution contract."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import httpx

from evaluation.benchmark import (
    BenchmarkRunner,
    build_summary,
    load_questions,
    parse_sse_lines,
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
            "schema_version": "1.4",
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
            "metrics_complete": True,
            "warnings": [],
        },
    }


def test_question_file_has_fixed_eight_and_matches_human_document() -> None:
    schema, questions = load_questions(QUESTIONS_PATH)
    document = (PROJECT_ROOT / "docs" / "DEMO_BENCHMARK.md").read_text(
        encoding="utf-8"
    ).replace("`", "")

    assert schema == "1.0"
    assert [item.id for item in questions] == [f"Q{index:02d}" for index in range(1, 9)]
    assert all(item.topic in document for item in questions)


def test_parse_sse_supports_comments_and_multiline_data() -> None:
    lines = [
        ": keepalive",
        "data: {\"type\":",
        'data: "done"}',
        "",
    ]

    assert list(parse_sse_lines(lines)) == [{"type": "done"}]


def test_fake_sse_runs_all_eight_and_continues_after_failure(tmp_path: Path) -> None:
    _, questions = load_questions(QUESTIONS_PATH)
    stream_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        assert request.headers["x-evaluation-metrics"] == "1"
        payload = json.loads(request.content)
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
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})

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
    assert summary["completed_count"] == 7
    assert summary["benchmark_success_count"] == 7
    assert summary["failure_stage_counts"] == {"search": 1}
    assert summary["duration_ms"] == {"mean": 1200.0, "p50": 1200.0, "p95": 1200.0}
    assert summary["search_failure_rate"] == 1 / 16
    assert summary["fallback_recovery_rate"] == 1.0
    assert summary["metrics_complete_count"] == 8
    assert summary["llm_calls_total"] == 28
    assert summary["tokens_total"] == 1050
    assert summary["citation_unique_total"] == 21
    assert summary["citation_accessibility_rate_weighted"] is None
    assert summary["claim_citation_coverage_weighted"] == 1.0
    assert len(list((tmp_path / "runs").glob("*.json"))) == 8
    assert len(list((tmp_path / "reports").glob("*.md"))) == 8
    assert len((tmp_path / "summary.csv").read_text(encoding="utf-8").splitlines()) == 9
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "runs").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "summary.json").stat().st_mode) == 0o600


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


def test_resume_skips_terminal_runs_and_rebuilds_identical_summary(tmp_path: Path) -> None:
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
