"""Thread-safe, fail-open telemetry for one research run."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from threading import Lock, local
from time import perf_counter
from typing import Any

Clock = Callable[[], float]
WallClock = Callable[[], datetime]
PersistCallback = Callable[[Mapping[str, Any]], Any]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunRecorder:
    """Collect metrics without becoming a dependency of the research result."""

    schema_version = "1.1"

    def __init__(
        self,
        *,
        run_id: str,
        topic: str,
        enabled: bool = True,
        model: str | None = None,
        search_api: str | None = None,
        git_commit: str | None = None,
        persist_callback: PersistCallback | None = None,
        pricing_catalog: Any | None = None,
        initial_warnings: tuple[str, ...] = (),
        clock: Clock = perf_counter,
        wall_clock: WallClock = _utc_now,
    ) -> None:
        self.run_id = run_id
        self.topic = topic
        self.enabled = enabled
        self.model = model
        self.search_api = search_api
        self.git_commit = git_commit
        self._persist_callback = persist_callback
        self._pricing_catalog = pricing_catalog
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = Lock()
        self._thread_context = local()

        self._started_monotonic = self._safe_clock()
        self._finished_monotonic: float | None = None
        self._started_at = self._safe_wall_clock()
        self._finished_at: datetime | None = None
        self._status = "running" if enabled else "disabled"
        self._failure_stage: str | None = None
        self._failure_type: str | None = None
        self._failure_message: str | None = None
        self._last_error_stage: str | None = None

        self._stage_durations_ms: dict[str, float] = {}
        self._stage_invocations: dict[str, int] = {}
        self._stage_failures: dict[str, int] = {}
        self._planned_subtasks = 0
        self._task_statuses: dict[str, str] = {}

        self._search_attempts = 0
        self._search_successes = 0
        self._search_failures = 0
        self._search_empty_results = 0
        self._fallback_triggers = 0
        self._fallback_successes = 0
        self._llm_calls = 0
        self._llm_failures = 0
        self._llm_usage_sources: list[str] = []
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._total_tokens = 0
        self._warnings: list[str] = list(dict.fromkeys(initial_warnings))

    @contextmanager
    def stage(self, name: str, *, task_id: str | int | None = None) -> Iterator[None]:
        """Measure a stage while preserving every business exception unchanged."""

        if not self.enabled:
            yield
            return

        try:
            started = self._clock()
            previous = getattr(self._thread_context, "stage", None)
            normalized_task_id = str(task_id) if task_id is not None else None
            self._thread_context.stage = (name, normalized_task_id)
        except Exception as exc:  # pragma: no cover - defensive fail-open path
            self.add_warning(f"stage_start_failed:{type(exc).__name__}")
            yield
            return

        try:
            yield
        except BaseException:
            self._record_stage(name, started, failed=True)
            with self._lock:
                self._last_error_stage = name
            raise
        else:
            self._record_stage(name, started, failed=False)
        finally:
            self._thread_context.stage = previous

    def current_stage(self) -> str | None:
        """Return the stage associated with the calling thread."""

        value = getattr(self._thread_context, "stage", None)
        return value[0] if value else None

    def record_tasks_planned(self, count: int) -> None:
        """Record the number of tasks produced by the planner."""

        if not self.enabled:
            return
        try:
            normalized = max(0, int(count))
            with self._lock:
                self._planned_subtasks = normalized
        except Exception as exc:  # pragma: no cover - defensive fail-open path
            self.add_warning(f"task_plan_metric_failed:{type(exc).__name__}")

    def record_task_status(self, task_id: str | int, status: str) -> None:
        """Store the latest status for a task, making repeated events idempotent."""

        if not self.enabled:
            return
        try:
            with self._lock:
                self._task_statuses[str(task_id)] = str(status)
        except Exception as exc:  # pragma: no cover - defensive fail-open path
            self.add_warning(f"task_status_metric_failed:{type(exc).__name__}")

    def record_search_attempt(self) -> None:
        """Count one primary search backend invocation."""

        self._increment("_search_attempts")

    def record_search_success(self) -> None:
        """Count a primary search returning at least one result."""

        self._increment("_search_successes")

    def record_search_failure(self, *, empty_result: bool = False) -> None:
        """Count a primary search exception or empty result."""

        self._increment("_search_failures")
        if empty_result:
            self._increment("_search_empty_results")

    def record_fallback_trigger(self) -> None:
        """Count one search fallback attempt."""

        self._increment("_fallback_triggers")

    def record_fallback_result(self, *, success: bool) -> None:
        """Count a successful fallback; failures are derived from triggers."""

        if success:
            self._increment("_fallback_successes")

    def record_llm_call(self) -> None:
        """Count one attempted LLM request."""

        self._increment("_llm_calls")

    def record_llm_failure(self) -> None:
        """Count one LLM request that raised an exception."""

        self._increment("_llm_failures")

    def record_llm_usage(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int | None = None,
        source: str,
    ) -> None:
        """Record provider-reported or explicitly estimated token usage."""

        if not self.enabled:
            return
        try:
            normalized_source = str(source).strip().lower()
            if normalized_source not in {"provider", "estimated"}:
                raise ValueError("unsupported usage source")
            prompt = max(0, int(prompt_tokens))
            completion = max(0, int(completion_tokens))
            total = max(prompt + completion, int(total_tokens or 0))
            with self._lock:
                self._prompt_tokens += prompt
                self._completion_tokens += completion
                self._total_tokens += total
                self._llm_usage_sources.append(normalized_source)
        except Exception as exc:
            self.add_warning(f"llm_usage_metric_failed:{type(exc).__name__}")

    def mark_completed(self) -> None:
        """Finalize the run as completed."""

        self._finish("completed")

    def mark_failed(self, exc: BaseException, *, stage: str | None = None) -> None:
        """Finalize the run as failed without retaining sensitive payloads."""

        failure_message = str(exc).strip().replace("\n", " ")[:300]
        with self._lock:
            resolved_stage = stage or self._last_error_stage or self.current_stage()
        self._finish(
            "failed",
            failure_stage=resolved_stage,
            failure_type=type(exc).__name__,
            failure_message=failure_message or None,
        )

    def mark_cancelled(self, *, stage: str | None = None) -> None:
        """Finalize the run as cancelled."""

        with self._lock:
            resolved_stage = stage or self.current_stage() or self._last_error_stage
        self._finish("cancelled", failure_stage=resolved_stage)

    def add_warning(self, warning: str) -> None:
        """Add a bounded, de-duplicated instrumentation warning."""

        try:
            normalized = str(warning).strip().replace("\n", " ")[:200]
            if not normalized:
                return
            with self._lock:
                if normalized not in self._warnings:
                    self._warnings.append(normalized)
        except Exception:
            return

    def snapshot(self) -> dict[str, Any]:
        """Return a JSON-serializable point-in-time metrics snapshot."""

        with self._lock:
            finished = self._finished_monotonic
            started = self._started_monotonic
            if started is None:
                total_duration_ms = None
            else:
                end = finished if finished is not None else self._safe_clock()
                total_duration_ms = (
                    max(0, round((end - started) * 1000)) if end is not None else None
                )

            statuses = tuple(self._task_statuses.values())
            stage_durations = {
                key: round(value)
                for key, value in sorted(self._stage_durations_ms.items())
            }
            has_token_usage = bool(self._llm_usage_sources)
            estimated_cost, cost_currency = self._estimated_cost()
            return {
                "schema_version": self.schema_version,
                "run_id": self.run_id,
                "topic": self.topic,
                "model": self.model,
                "search_api": self.search_api,
                "git_commit": self.git_commit,
                "started_at": self._isoformat(self._started_at),
                "finished_at": self._isoformat(self._finished_at),
                "status": self._status,
                "failure_stage": self._failure_stage,
                "failure_type": self._failure_type,
                "failure_message": self._failure_message,
                "total_duration_ms": total_duration_ms,
                "stage_durations_ms": stage_durations,
                "stage_invocations": dict(sorted(self._stage_invocations.items())),
                "stage_failures": dict(sorted(self._stage_failures.items())),
                "planned_subtasks": self._planned_subtasks,
                "completed_subtasks": statuses.count("completed"),
                "failed_subtasks": statuses.count("failed"),
                "skipped_subtasks": statuses.count("skipped"),
                "cancelled_subtasks": statuses.count("cancelled"),
                "search_attempts": self._search_attempts,
                "search_successes": self._search_successes,
                "search_failures": self._search_failures,
                "search_empty_results": self._search_empty_results,
                "fallback_triggers": self._fallback_triggers,
                "fallback_successes": self._fallback_successes,
                "llm_calls": self._llm_calls,
                "llm_failures": self._llm_failures,
                "llm_usage_recorded_calls": len(self._llm_usage_sources),
                "prompt_tokens": self._prompt_tokens if has_token_usage else None,
                "completion_tokens": (
                    self._completion_tokens if has_token_usage else None
                ),
                "total_tokens": self._total_tokens if has_token_usage else None,
                "usage_source": self._usage_source(),
                "estimated_cost": estimated_cost,
                "cost_currency": cost_currency,
                "metrics_complete": not self._warnings,
                "warnings": list(self._warnings),
            }

    def _record_stage(self, name: str, started: float, *, failed: bool) -> None:
        try:
            elapsed_ms = max(0.0, (self._clock() - started) * 1000)
            with self._lock:
                previous_duration = self._stage_durations_ms.get(name, 0.0)
                self._stage_durations_ms[name] = previous_duration + elapsed_ms
                self._stage_invocations[name] = self._stage_invocations.get(name, 0) + 1
                if failed:
                    self._stage_failures[name] = self._stage_failures.get(name, 0) + 1
        except Exception as exc:  # pragma: no cover - defensive fail-open path
            self.add_warning(f"stage_finish_failed:{type(exc).__name__}")

    def _increment(self, field_name: str) -> None:
        if not self.enabled:
            return
        try:
            with self._lock:
                setattr(self, field_name, getattr(self, field_name) + 1)
        except Exception as exc:  # pragma: no cover - defensive fail-open path
            self.add_warning(f"counter_failed:{field_name}:{type(exc).__name__}")

    def _finish(
        self,
        status: str,
        *,
        failure_stage: str | None = None,
        failure_type: str | None = None,
        failure_message: str | None = None,
    ) -> None:
        if not self.enabled:
            return
        try:
            finished_monotonic = self._clock()
            finished_at = self._wall_clock()
            with self._lock:
                if self._status != "running":
                    return
                self._finished_monotonic = finished_monotonic
                self._finished_at = finished_at
                self._status = status
                self._failure_stage = failure_stage
                self._failure_type = failure_type
                self._failure_message = failure_message
            self._persist_terminal_snapshot()
        except Exception as exc:  # pragma: no cover - defensive fail-open path
            self.add_warning(f"run_finish_failed:{type(exc).__name__}")

    def _persist_terminal_snapshot(self) -> None:
        if self._persist_callback is None:
            return
        try:
            self._persist_callback(self.snapshot())
        except Exception as exc:
            self.add_warning(f"metrics_persistence_failed:{type(exc).__name__}")

    def _usage_source(self) -> str:
        sources = set(self._llm_usage_sources)
        if not sources:
            return "unavailable"
        successful_calls = max(0, self._llm_calls - self._llm_failures)
        if len(self._llm_usage_sources) < successful_calls:
            return "mixed"
        if len(sources) == 1:
            return next(iter(sources))
        return "mixed"

    def _estimated_cost(self) -> tuple[float | None, str | None]:
        if self._pricing_catalog is None or not self._llm_usage_sources:
            return None, None
        try:
            return self._pricing_catalog.estimate(
                model=self.model,
                prompt_tokens=self._prompt_tokens,
                completion_tokens=self._completion_tokens,
            )
        except Exception:
            return None, None

    def _safe_clock(self) -> float | None:
        try:
            return self._clock()
        except Exception:
            return None

    def _safe_wall_clock(self) -> datetime | None:
        try:
            return self._wall_clock()
        except Exception:
            return None

    @staticmethod
    def _isoformat(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None
