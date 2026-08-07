"""Fixed-question benchmark runner and reproducible aggregate reporting."""

from __future__ import annotations

import asyncio
import csv
import json
import math
import os
import subprocess
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from time import perf_counter
from typing import Any, Iterable
from uuid import uuid4

import httpx

from evaluation.accessibility import URLAccessibilityChecker
from evaluation.citations import audit_report

BENCHMARK_SCHEMA_VERSION = "1.0"
TERMINAL_STATUSES = {"completed", "failed", "cancelled", "timeout", "incomplete"}


@dataclass(frozen=True)
class BenchmarkQuestion:
    """One versioned benchmark question."""

    id: str
    category: str
    topic: str
    time_limit_seconds: int
    minimum_unique_citations: int


def load_questions(path: Path) -> tuple[str, list[BenchmarkQuestion]]:
    """Load and strictly validate the machine-readable question authority."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    schema_version = str(payload.get("schema_version") or "")
    if schema_version != BENCHMARK_SCHEMA_VERSION:
        raise ValueError(f"unsupported question schema: {schema_version!r}")
    raw_questions = payload.get("questions")
    if not isinstance(raw_questions, list) or len(raw_questions) != 8:
        raise ValueError("fixed benchmark must contain exactly 8 questions")

    questions: list[BenchmarkQuestion] = []
    seen: set[str] = set()
    for item in raw_questions:
        question = BenchmarkQuestion(
            id=str(item["id"]).strip(),
            category=str(item["category"]).strip(),
            topic=str(item["topic"]).strip(),
            time_limit_seconds=int(item["time_limit_seconds"]),
            minimum_unique_citations=int(item["minimum_unique_citations"]),
        )
        if not question.id or question.id in seen:
            raise ValueError(f"duplicate or empty question id: {question.id!r}")
        if not question.topic or question.time_limit_seconds <= 0:
            raise ValueError(f"invalid question: {question.id}")
        if question.minimum_unique_citations < 0:
            raise ValueError(f"invalid citation threshold: {question.id}")
        seen.add(question.id)
        questions.append(question)
    return schema_version, questions


class BenchmarkRunner:
    """Run fixed questions serially through the deployed SSE API."""

    def __init__(
        self,
        *,
        base_url: str,
        output_dir: Path,
        client: httpx.Client | None = None,
        password: str | None = None,
        search_api: str = "duckduckgo",
        enable_source_provenance: bool = True,
        check_accessibility: bool = True,
        git_commit: str | None = None,
        model: str | None = None,
        repetitions: int = 1,
        resume: bool = False,
        rerun_failed: bool = False,
        max_web_research_loops: int = 3,
    ) -> None:
        """Configure one serial benchmark execution session."""
        self.base_url = base_url.rstrip("/")
        self.output_dir = Path(output_dir)
        self.runs_dir = self.output_dir / "runs"
        self.reports_dir = self.output_dir / "reports"
        self.search_api = search_api
        self.enable_source_provenance = enable_source_provenance
        self.check_accessibility = check_accessibility
        self.git_commit = git_commit or resolve_git_commit()
        self.model = model
        self.repetitions = max(1, repetitions)
        self.resume = resume
        self.rerun_failed = rerun_failed
        self.max_web_research_loops = max(1, max_web_research_loops)
        self._owns_client = client is None
        self.client = client or httpx.Client(
            auth=httpx.BasicAuth("", password) if password else None,
            timeout=httpx.Timeout(connect=20, read=None, write=20, pool=20),
            headers={
                "Accept": "text/event-stream",
                "X-Evaluation-Metrics": "1",
            },
        )

    def close(self) -> None:
        """Close the internally created HTTP client."""
        if self._owns_client:
            self.client.close()

    def preflight_capacity(self, required_runs: int) -> dict[str, Any]:
        """Verify readiness and budget before starting a paid benchmark batch."""
        if required_runs <= 0:
            raise ValueError("required_runs must be positive")
        try:
            response = self.client.get(f"{self.base_url}/readyz", timeout=20)
        except httpx.HTTPError as exc:
            raise RuntimeError(
                f"benchmark preflight could not reach /readyz: {type(exc).__name__}"
            ) from exc
        if response.status_code != 200:
            raise RuntimeError(
                f"benchmark preflight failed: /readyz returned HTTP "
                f"{response.status_code}"
            )
        try:
            payload = response.json()
            budget = payload["budget"]
            remaining = int(budget["remaining"])
            raw_rate_limit = budget.get("rate_limit_requests")
            rate_limit = int(raw_rate_limit) if raw_rate_limit is not None else None
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                "benchmark preflight failed: /readyz returned malformed budget data"
            ) from exc
        if payload.get("status") != "ready":
            raise RuntimeError("benchmark preflight failed: deployment is not ready")
        if remaining < required_runs:
            raise RuntimeError(
                "benchmark preflight failed: "
                f"requires {required_runs} runs but only {remaining} budget remains"
            )
        if rate_limit is not None and rate_limit < required_runs:
            window = budget.get("rate_limit_window_seconds", "configured")
            raise RuntimeError(
                "benchmark preflight failed: "
                f"requires {required_runs} uninterrupted runs but the per-client "
                f"limit is {rate_limit} per {window} seconds"
            )
        return payload

    def pending_run_count(self, questions: Iterable[BenchmarkQuestion]) -> int:
        """Count requests still needed, respecting resume and rerun policy."""
        pending = 0
        for question in questions:
            for repetition in range(1, self.repetitions + 1):
                run_path = self.runs_dir / f"{question.id}-run-{repetition:02d}.json"
                if self.resume and _is_terminal_run(run_path):
                    existing = json.loads(run_path.read_text(encoding="utf-8"))
                    if not self.rerun_failed or existing.get("status") == "completed":
                        continue
                pending += 1
        return pending

    def run(self, questions: Iterable[BenchmarkQuestion]) -> dict[str, Any]:
        """Run all requested questions and always rebuild aggregate outputs."""
        selected = list(questions)
        self._prepare_directories()
        manifest = self._load_or_create_manifest(selected)

        for question in selected:
            for repetition in range(1, self.repetitions + 1):
                run_key = f"{question.id}-run-{repetition:02d}"
                run_path = self.runs_dir / f"{run_key}.json"
                if self.resume and _is_terminal_run(run_path):
                    existing = json.loads(run_path.read_text(encoding="utf-8"))
                    if not self.rerun_failed or existing.get("status") == "completed":
                        continue
                run_id = manifest["run_ids"].setdefault(
                    run_key,
                    _new_run_id(manifest["benchmark_id"], question.id, repetition),
                )
                _atomic_json_write(self.output_dir / "manifest.json", manifest)
                result = self._run_one(question, repetition, run_id, run_key)
                _atomic_json_write(run_path, result)

        runs = self._load_runs()
        observed_models = sorted(
            {str(item["model"]) for item in runs if item.get("model")}
        )
        observed_commits = sorted(
            {str(item["git_commit"]) for item in runs if item.get("git_commit")}
        )
        if not manifest.get("model") and len(observed_models) == 1:
            manifest["model"] = observed_models[0]
        if len(observed_commits) == 1:
            manifest["git_commit"] = observed_commits[0]
        summary = build_summary(runs, manifest)
        _atomic_json_write(self.output_dir / "summary.json", summary)
        write_summary_csv(self.output_dir / "summary.csv", runs)
        manifest["finished_at"] = _utc_now()
        manifest["run_count"] = len(runs)
        _atomic_json_write(self.output_dir / "manifest.json", manifest)
        return summary

    def _run_one(
        self,
        question: BenchmarkQuestion,
        repetition: int,
        run_id: str,
        run_key: str,
    ) -> dict[str, Any]:
        started_at = _utc_now()
        started = perf_counter()
        events: list[dict[str, Any]] = []
        report = ""
        server_metrics: dict[str, Any] | None = None
        provenance_audit: dict[str, Any] | None = None
        status = "incomplete"
        failure_stage: str | None = None
        failure_type: str | None = None
        warnings: list[str] = []

        try:
            with self.client.stream(
                "POST",
                f"{self.base_url}/research/stream",
                headers={"X-Evaluation-Metrics": "1"},
                json={
                    "topic": question.topic,
                    "search_api": self.search_api,
                    "enable_source_provenance": self.enable_source_provenance,
                    "max_web_research_loops": self.max_web_research_loops,
                    "job_id": run_id,
                },
                timeout=httpx.Timeout(
                    connect=20,
                    read=question.time_limit_seconds,
                    write=20,
                    pool=20,
                ),
            ) as response:
                response.raise_for_status()
                for event in parse_sse_lines(response.iter_lines()):
                    event_type = str(event.get("type") or "")
                    if event_type in {
                        "final_report",
                        "done",
                        "error",
                        "cancelled",
                        "metrics",
                    }:
                        events.append(event)
                    if event_type == "final_report":
                        report = str(event.get("report") or "")
                        if isinstance(event.get("provenance_audit"), dict):
                            provenance_audit = dict(event["provenance_audit"])
                    elif event_type == "metrics" and isinstance(
                        event.get("metrics"), dict
                    ):
                        server_metrics = dict(event["metrics"])
                    elif event_type == "error":
                        status = "failed"
                        failure_stage = "streaming"
                        failure_type = "ServerErrorEvent"
                    elif event_type == "cancelled":
                        status = "cancelled"
                        failure_stage = "streaming"
                    elif event_type == "done" and report:
                        status = "completed"
        except httpx.TimeoutException as exc:
            status = "timeout"
            failure_stage = "client_timeout"
            failure_type = type(exc).__name__
            warnings.append("research_stream_timed_out")
            self._request_cancel(run_id, warnings)
        except httpx.HTTPStatusError as exc:
            status = "failed"
            failure_stage = "http"
            failure_type = f"HTTP_{exc.response.status_code}"
            warnings.append(f"http_status:{exc.response.status_code}")
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            status = "failed"
            failure_stage = "streaming"
            failure_type = type(exc).__name__
            warnings.append(f"stream_error:{type(exc).__name__}")

        if server_metrics:
            status = str(server_metrics.get("status") or status)
            failure_stage = server_metrics.get("failure_stage") or failure_stage
            failure_type = server_metrics.get("failure_type") or failure_type
        else:
            warnings.append("server_metrics_event_missing")

        duration_ms = max(0, round((perf_counter() - started) * 1000))
        citation = audit_report(report)
        accessibility = self._audit_accessibility(citation, warnings)
        report_rel = f"reports/{run_key}.md"
        _atomic_text_write(self.output_dir / report_rel, report)

        result: dict[str, Any] = {
            "benchmark_schema_version": BENCHMARK_SCHEMA_VERSION,
            "run_id": run_id,
            "question_id": question.id,
            "category": question.category,
            "topic": question.topic,
            "repetition": repetition,
            "git_commit": (server_metrics or {}).get("git_commit") or self.git_commit,
            "model": (server_metrics or {}).get("model") or self.model,
            "search_api": self.search_api,
            "started_at": started_at,
            "finished_at": _utc_now(),
            "status": status,
            "benchmark_success": (
                status == "completed"
                and bool(report.strip())
                and citation.citation_count_unique >= question.minimum_unique_citations
                and duration_ms <= question.time_limit_seconds * 1000
            ),
            "failure_stage": failure_stage,
            "failure_type": failure_type,
            "total_duration_ms": (server_metrics or {}).get("total_duration_ms")
            or duration_ms,
            "client_duration_ms": duration_ms,
            "time_limit_seconds": question.time_limit_seconds,
            "minimum_unique_citations": question.minimum_unique_citations,
            "citation_count_raw": citation.citation_count_raw,
            "citation_count_unique": citation.citation_count_unique,
            "citation_accessible_count": accessibility["accessible_count"],
            "citation_accessibility_rate": accessibility["accessibility_rate"],
            "citation_accessibility_results": accessibility["results"],
            "domain_count": citation.domain_count,
            "claim_units": citation.claim_units,
            "claim_units_with_citations": citation.claim_units_with_citations,
            "claim_citation_coverage": citation.claim_citation_coverage,
            "report_unique_urls": (provenance_audit or {}).get(
                "report_unique_url_count"
            ),
            "report_cited_catalog_sources": (provenance_audit or {}).get(
                "cited_catalog_source_count"
            ),
            "report_catalog_url_match_rate": (provenance_audit or {}).get(
                "report_catalog_url_match_rate"
            ),
            "report_relevant_cited_sources": (provenance_audit or {}).get(
                "report_relevant_cited_source_count"
            ),
            "report_cited_source_relevance_rate": (provenance_audit or {}).get(
                "report_cited_source_relevance_rate"
            ),
            "report_relevant_source_integrity_rate": (provenance_audit or {}).get(
                "report_relevant_source_integrity_rate"
            ),
            "report_path": report_rel,
            "report_chars": len(report),
            "sse_terminal_events": events,
            "metrics_complete": bool(server_metrics)
            and bool(server_metrics.get("metrics_complete", False)),
            "warnings": _dedupe(
                [*warnings, *list((server_metrics or {}).get("warnings") or [])]
            ),
        }
        _copy_server_metrics(result, server_metrics)
        return result

    def _request_cancel(self, run_id: str, warnings: list[str]) -> None:
        try:
            response = self.client.post(
                f"{self.base_url}/research/{run_id}/cancel",
                timeout=20,
            )
            if response.status_code not in {200, 404}:
                warnings.append(f"cancel_http_status:{response.status_code}")
        except httpx.HTTPError as exc:
            warnings.append(f"cancel_failed:{type(exc).__name__}")

    def _audit_accessibility(
        self, citation: Any, warnings: list[str]
    ) -> dict[str, Any]:
        if not self.check_accessibility or not citation.citations:
            return {"accessible_count": None, "accessibility_rate": None, "results": []}
        try:
            checker = URLAccessibilityChecker()
            checked = asyncio.run(
                checker.check_many(item.normalized_url for item in citation.citations)
            )
            accessible = sum(item.status == "accessible" for item in checked)
            return {
                "accessible_count": accessible,
                "accessibility_rate": accessible / len(checked) if checked else None,
                "results": [asdict(item) for item in checked],
            }
        except Exception as exc:
            warnings.append(f"citation_accessibility_failed:{type(exc).__name__}")
            return {"accessible_count": None, "accessibility_rate": None, "results": []}

    def _prepare_directories(self) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runs_dir.mkdir(exist_ok=True, mode=0o700)
        self.reports_dir.mkdir(exist_ok=True, mode=0o700)
        for directory in (self.output_dir, self.runs_dir, self.reports_dir):
            os.chmod(directory, 0o700)

    def _load_or_create_manifest(
        self, questions: list[BenchmarkQuestion]
    ) -> dict[str, Any]:
        path = self.output_dir / "manifest.json"
        if self.resume and path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("question_ids") != [item.id for item in questions]:
                raise ValueError("resume question set does not match manifest")
            if int(payload.get("repetitions", 0)) != self.repetitions:
                raise ValueError("resume repetitions do not match manifest")
            return payload
        return {
            "benchmark_schema_version": BENCHMARK_SCHEMA_VERSION,
            "benchmark_id": datetime.now(timezone.utc).strftime("bench-%Y%m%dT%H%M%SZ"),
            "started_at": _utc_now(),
            "finished_at": None,
            "base_url": self.base_url,
            "git_commit": self.git_commit,
            "model": self.model,
            "search_api": self.search_api,
            "max_web_research_loops": self.max_web_research_loops,
            "execution_mode": "serial",
            "search_fallback_enabled": True,
            "source_provenance_enabled": self.enable_source_provenance,
            "accessibility_check_enabled": self.check_accessibility,
            "question_ids": [item.id for item in questions],
            "repetitions": self.repetitions,
            "run_ids": {},
        }

    def _load_runs(self) -> list[dict[str, Any]]:
        return [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(self.runs_dir.glob("*.json"))
            if _is_terminal_run(path)
        ]


def parse_sse_lines(lines: Iterable[str]) -> Iterable[dict[str, Any]]:
    """Parse SSE data frames, including multi-line data payloads."""
    data_lines: list[str] = []
    for raw_line in lines:
        line = raw_line.rstrip("\r")
        if line == "":
            if data_lines:
                yield json.loads("\n".join(data_lines))
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
    if data_lines:
        yield json.loads("\n".join(data_lines))


def build_summary(
    runs: list[dict[str, Any]], manifest: dict[str, Any]
) -> dict[str, Any]:
    """Aggregate runs using explicit denominators and reproducible percentiles."""
    completed = [item for item in runs if item.get("status") == "completed"]
    successful = [item for item in runs if item.get("benchmark_success") is True]
    durations = [_number(item.get("total_duration_ms")) for item in completed]
    durations = [item for item in durations if item is not None]
    repetitions_by_question = Counter(
        str(item.get("question_id")) for item in completed if item.get("question_id")
    )
    latency_repeatable = bool(
        len(durations) >= 24
        and len(repetitions_by_question) == 8
        and min(repetitions_by_question.values(), default=0) >= 3
    )
    coverages = [_number(item.get("claim_citation_coverage")) for item in completed]
    coverages = [item for item in coverages if item is not None]
    access_rates = [
        _number(item.get("citation_accessibility_rate")) for item in completed
    ]
    access_rates = [item for item in access_rates if item is not None]
    accessible_total = _nullable_sum_field(completed, "citation_accessible_count")
    citation_unique_total = _sum_field(completed, "citation_count_unique")
    claim_units_total = _sum_field(completed, "claim_units")
    cited_claim_units_total = _sum_field(completed, "claim_units_with_citations")
    report_unique_urls_total = _nullable_sum_field(completed, "report_unique_urls")
    report_cited_catalog_total = _nullable_sum_field(
        completed, "report_cited_catalog_sources"
    )
    report_relevant_cited_total = _nullable_sum_field(
        completed, "report_relevant_cited_sources"
    )

    search_attempts = _sum_field(runs, "search_attempts")
    search_failures = _sum_field(runs, "search_failures")
    fallback_triggers = _sum_field(runs, "fallback_triggers")
    fallback_successes = _sum_field(runs, "fallback_successes")
    failure_stages: dict[str, int] = {}
    for item in runs:
        if item.get("status") == "completed":
            continue
        stage = str(item.get("failure_stage") or "unknown")
        failure_stages[stage] = failure_stages.get(stage, 0) + 1

    return {
        "benchmark_schema_version": BENCHMARK_SCHEMA_VERSION,
        "benchmark_id": manifest.get("benchmark_id"),
        "generated_at": max(
            (str(item.get("finished_at") or "") for item in runs),
            default=str(manifest.get("started_at") or ""),
        ),
        "git_commit": manifest.get("git_commit"),
        "model": manifest.get("model"),
        "search_api": manifest.get("search_api"),
        "run_count": len(runs),
        "completed_count": len(completed),
        "failed_count": len(runs) - len(completed),
        "benchmark_success_count": len(successful),
        "task_completion_rate": _rate(len(completed), len(runs)),
        "benchmark_success_rate": _rate(len(successful), len(runs)),
        "duration_ms": {
            "mean": _mean(durations),
            "p50": _percentile(durations, 0.50),
            "p95": _percentile(durations, 0.95),
        },
        "latency_sample_count": len(durations),
        "latency_repetitions_by_question": dict(
            sorted(repetitions_by_question.items())
        ),
        "latency_percentiles_status": (
            "repeatable_baseline" if latency_repeatable else "provisional"
        ),
        "llm_calls_mean": _mean_field(completed, "llm_calls"),
        "llm_calls_total": _nullable_sum_field(completed, "llm_calls"),
        "tokens_mean": _mean_field(completed, "total_tokens"),
        "tokens_total": _nullable_sum_field(completed, "total_tokens"),
        "estimated_cost_mean": _mean_field(completed, "estimated_cost"),
        "estimated_cost_total": _nullable_sum_field(completed, "estimated_cost"),
        "citation_unique_mean": _mean_field(completed, "citation_count_unique"),
        "citation_unique_total": citation_unique_total,
        "citation_accessible_total": accessible_total,
        "claim_citation_coverage_mean": _mean(coverages),
        "claim_citation_coverage_weighted": _rate(
            cited_claim_units_total, claim_units_total
        ),
        "claim_units_total": claim_units_total,
        "claim_units_with_citations_total": cited_claim_units_total,
        "citation_accessibility_rate_mean": _mean(access_rates),
        "citation_accessibility_rate_weighted": _rate(
            accessible_total, citation_unique_total
        )
        if accessible_total is not None
        else None,
        "report_catalog_url_match_rate_mean": _mean_field(
            completed, "report_catalog_url_match_rate"
        ),
        "report_catalog_url_match_rate_weighted": _rate(
            report_cited_catalog_total, report_unique_urls_total
        )
        if report_cited_catalog_total is not None
        and report_unique_urls_total is not None
        else None,
        "report_cited_source_relevance_rate_mean": _mean_field(
            completed, "report_cited_source_relevance_rate"
        ),
        "report_cited_source_relevance_rate_weighted": _rate(
            report_relevant_cited_total, report_cited_catalog_total
        )
        if report_relevant_cited_total is not None
        and report_cited_catalog_total is not None
        else None,
        "report_relevant_source_integrity_rate_mean": _mean_field(
            completed, "report_relevant_source_integrity_rate"
        ),
        "report_relevant_source_integrity_rate_weighted": _rate(
            report_relevant_cited_total, report_unique_urls_total
        )
        if report_relevant_cited_total is not None
        and report_unique_urls_total is not None
        else None,
        "search_failure_rate": _rate(search_failures, search_attempts),
        "fallback_recovery_rate": _rate(fallback_successes, fallback_triggers),
        "failure_stage_counts": dict(sorted(failure_stages.items())),
        "metrics_complete_count": sum(
            bool(item.get("metrics_complete")) for item in runs
        ),
        "runs": [
            {
                "run_id": item.get("run_id"),
                "question_id": item.get("question_id"),
                "status": item.get("status"),
                "benchmark_success": item.get("benchmark_success"),
                "total_duration_ms": item.get("total_duration_ms"),
                "citation_count_unique": item.get("citation_count_unique"),
                "claim_citation_coverage": item.get("claim_citation_coverage"),
                "report_catalog_url_match_rate": item.get(
                    "report_catalog_url_match_rate"
                ),
                "report_cited_source_relevance_rate": item.get(
                    "report_cited_source_relevance_rate"
                ),
                "report_relevant_source_integrity_rate": item.get(
                    "report_relevant_source_integrity_rate"
                ),
                "failure_stage": item.get("failure_stage"),
            }
            for item in runs
        ],
    }


CSV_FIELDS = (
    "run_id",
    "question_id",
    "category",
    "repetition",
    "status",
    "benchmark_success",
    "failure_stage",
    "failure_type",
    "total_duration_ms",
    "planned_subtasks",
    "completed_subtasks",
    "failed_subtasks",
    "llm_calls",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "usage_source",
    "estimated_cost",
    "cost_currency",
    "search_attempts",
    "search_failures",
    "fallback_triggers",
    "fallback_successes",
    "citation_count_raw",
    "citation_count_unique",
    "citation_accessible_count",
    "citation_accessibility_rate",
    "domain_count",
    "claim_units",
    "claim_units_with_citations",
    "claim_citation_coverage",
    "report_unique_urls",
    "report_cited_catalog_sources",
    "report_catalog_url_match_rate",
    "report_relevant_cited_sources",
    "report_cited_source_relevance_rate",
    "report_relevant_source_integrity_rate",
    "metrics_complete",
    "report_path",
)


def write_summary_csv(path: Path, runs: list[dict[str, Any]]) -> None:
    """Atomically write one flat, recomputable row per run."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            writer = csv.DictWriter(
                handle, fieldnames=CSV_FIELDS, extrasaction="ignore"
            )
            writer.writeheader()
            for item in runs:
                writer.writerow({key: item.get(key) for key in CSV_FIELDS})
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except BaseException:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)
        raise


def resolve_git_commit() -> str | None:
    """Resolve the current commit without failing outside a Git checkout."""
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _copy_server_metrics(
    target: dict[str, Any], metrics: dict[str, Any] | None
) -> None:
    fields = (
        "schema_version",
        "stage_durations_ms",
        "planned_subtasks",
        "completed_subtasks",
        "failed_subtasks",
        "llm_calls",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "usage_source",
        "estimated_cost",
        "cost_currency",
        "search_attempts",
        "search_failures",
        "fallback_triggers",
        "fallback_successes",
        "report_unique_urls",
        "report_cited_catalog_sources",
        "report_catalog_url_match_rate",
        "report_relevant_cited_sources",
        "report_cited_source_relevance_rate",
        "report_relevant_source_integrity_rate",
    )
    for field in fields:
        if metrics is not None and field in metrics:
            target[field] = metrics[field]
        elif field not in target:
            target[field] = None


def _is_terminal_run(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload.get("status") in TERMINAL_STATUSES
    except (OSError, json.JSONDecodeError):
        return False


def _new_run_id(benchmark_id: str, question_id: str, repetition: int) -> str:
    stamp = benchmark_id.replace("bench-", "").replace("T", "").replace("Z", "")
    return f"bm_{stamp}_{question_id}_{repetition:02d}_{uuid4().hex[:6]}"


def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
    _atomic_text_write(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def _atomic_text_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            os.chmod(temporary_path, 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    except BaseException:
        if temporary_path:
            temporary_path.unlink(missing_ok=True)
        raise


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _sum_field(items: list[dict[str, Any]], field: str) -> float:
    return sum(
        value for item in items if (value := _number(item.get(field))) is not None
    )


def _nullable_sum_field(items: list[dict[str, Any]], field: str) -> float | None:
    values = [
        value for item in items if (value := _number(item.get(field))) is not None
    ]
    return sum(values) if values else None


def _mean_field(items: list[dict[str, Any]], field: str) -> float | None:
    values = [
        value for item in items if (value := _number(item.get(field))) is not None
    ]
    return _mean(values)


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


def _rate(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 6)


def _dedupe(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(value)[:200] for value in values if value))
