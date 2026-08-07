"""Runtime configuration and lightweight safeguards for the public demo."""

from __future__ import annotations

import os
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from threading import Lock
from time import time

from config import Configuration, SearchAPI


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数，当前值为 {raw!r}") from exc
    if value <= 0:
        raise RuntimeError(f"{name} 必须大于 0，当前值为 {value}")
    return value


def _boolean(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} 必须是布尔值，当前值为 {raw!r}")


@dataclass(frozen=True)
class RuntimeSettings:
    """Settings that control deployment, access and demo cost boundaries."""

    access_password: str | None
    cors_origins: tuple[str, ...]
    rate_limit_requests: int
    rate_limit_window_seconds: int
    daily_research_budget: int
    frontend_dist_dir: Path
    enable_run_telemetry: bool
    emit_metrics_event: bool
    enable_inline_citation_audit: bool
    persist_run_metrics: bool
    run_metrics_dir: Path
    model_pricing_file: Path | None

    @classmethod
    def from_env(cls) -> "RuntimeSettings":
        project_root = Path(__file__).resolve().parents[2]
        frontend_dir = Path(
            os.getenv("FRONTEND_DIST_DIR", str(project_root / "frontend" / "dist"))
        ).expanduser()
        metrics_dir = Path(
            os.getenv(
                "RUN_METRICS_DIR",
                str(project_root / "backend" / "data" / "run_metrics"),
            )
        ).expanduser()
        pricing_file_value = os.getenv("MODEL_PRICING_FILE", "").strip()
        origins = tuple(
            origin.strip()
            for origin in os.getenv(
                "CORS_ORIGINS",
                "http://localhost:5173,http://localhost:5174",
            ).split(",")
            if origin.strip()
        )
        return cls(
            access_password=os.getenv("APP_ACCESS_PASSWORD") or None,
            cors_origins=origins,
            rate_limit_requests=_positive_int("RATE_LIMIT_REQUESTS", 5),
            rate_limit_window_seconds=_positive_int("RATE_LIMIT_WINDOW_SECONDS", 3600),
            daily_research_budget=_positive_int("DAILY_RESEARCH_BUDGET", 20),
            frontend_dist_dir=frontend_dir,
            enable_run_telemetry=_boolean("ENABLE_RUN_TELEMETRY", True),
            emit_metrics_event=_boolean("EMIT_METRICS_EVENT", False),
            enable_inline_citation_audit=_boolean(
                "ENABLE_INLINE_CITATION_AUDIT", False
            ),
            persist_run_metrics=_boolean("PERSIST_RUN_METRICS", True),
            run_metrics_dir=metrics_dir,
            model_pricing_file=(
                Path(pricing_file_value).expanduser() if pricing_file_value else None
            ),
        )


class ResearchLimitExceeded(RuntimeError):
    """Raised when a client or the whole demo exceeds a configured limit."""

    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class ResearchGate:
    """Single-process request limiter and daily research budget guard."""

    def __init__(self, settings: RuntimeSettings) -> None:
        self._settings = settings
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._budget_date = date.today()
        self._daily_count = 0
        self._lock = Lock()

    def consume(self, client_id: str) -> None:
        now = time()
        with self._lock:
            today = date.today()
            if today != self._budget_date:
                self._budget_date = today
                self._daily_count = 0

            if self._daily_count >= self._settings.daily_research_budget:
                raise ResearchLimitExceeded(
                    "今日演示额度已用完，请明天再试或联系项目维护者。",
                    retry_after=3600,
                )

            history = self._requests[client_id]
            threshold = now - self._settings.rate_limit_window_seconds
            while history and history[0] <= threshold:
                history.popleft()

            if len(history) >= self._settings.rate_limit_requests:
                retry_after = max(1, int(history[0] + self._settings.rate_limit_window_seconds - now))
                raise ResearchLimitExceeded(
                    "请求过于频繁，请稍后再试。",
                    retry_after=retry_after,
                )

            history.append(now)
            self._daily_count += 1

    def snapshot(self) -> dict[str, int | str]:
        with self._lock:
            return {
                "date": self._budget_date.isoformat(),
                "used": self._daily_count,
                "limit": self._settings.daily_research_budget,
                "remaining": max(0, self._settings.daily_research_budget - self._daily_count),
            }


def research_configuration_errors(config: Configuration) -> list[str]:
    """Return actionable configuration errors without exposing secret values."""

    errors: list[str] = []
    provider = (config.llm_provider or "").strip().lower()

    if provider in {"custom", "auto", "openai", "deepseek", "qwen", "modelscope", "kimi", "zhipu"}:
        if not config.llm_api_key:
            errors.append("缺少 LLM_API_KEY")
        if provider in {"custom", "auto"} and not config.llm_base_url:
            errors.append("custom/auto 模型需要 LLM_BASE_URL")
        if provider in {"custom", "auto"} and not config.llm_model_id:
            errors.append("custom/auto 模型需要 LLM_MODEL_ID")

    if config.search_api == SearchAPI.TAVILY and not os.getenv("TAVILY_API_KEY"):
        errors.append("SEARCH_API=tavily 时必须设置 TAVILY_API_KEY")
    if config.search_api == SearchAPI.PERPLEXITY and not os.getenv("PERPLEXITY_API_KEY"):
        errors.append("SEARCH_API=perplexity 时必须设置 PERPLEXITY_API_KEY")
    if config.search_api == SearchAPI.SEARXNG and not os.getenv("SEARXNG_URL"):
        errors.append("SEARCH_API=searxng 时必须设置 SEARXNG_URL")

    return errors
