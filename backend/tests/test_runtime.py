"""Tests for deployment readiness and demo safeguards."""

from __future__ import annotations

from dataclasses import replace

import pytest

from config import Configuration, SearchAPI
from runtime import ResearchGate, ResearchLimitExceeded, RuntimeSettings, research_configuration_errors


def _settings(**overrides: object) -> RuntimeSettings:
    base = RuntimeSettings.from_env()
    return replace(base, **overrides)


def test_custom_provider_reports_missing_required_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    config = Configuration(
        llm_provider="custom",
        llm_api_key=None,
        llm_base_url=None,
        llm_model_id=None,
        search_api=SearchAPI.TAVILY,
    )

    errors = research_configuration_errors(config)

    assert "缺少 LLM_API_KEY" in errors
    assert "custom/auto 模型需要 LLM_BASE_URL" in errors
    assert "custom/auto 模型需要 LLM_MODEL_ID" in errors
    assert "SEARCH_API=tavily 时必须设置 TAVILY_API_KEY" in errors


def test_gate_enforces_per_client_rate_limit() -> None:
    gate = ResearchGate(
        _settings(
            rate_limit_requests=1,
            rate_limit_window_seconds=3600,
            daily_research_budget=10,
        )
    )
    gate.consume("client-a")

    with pytest.raises(ResearchLimitExceeded, match="请求过于频繁"):
        gate.consume("client-a")


def test_gate_enforces_daily_budget_across_clients() -> None:
    gate = ResearchGate(
        _settings(
            rate_limit_requests=10,
            rate_limit_window_seconds=3600,
            daily_research_budget=1,
        )
    )
    gate.consume("client-a")

    with pytest.raises(ResearchLimitExceeded, match="今日演示额度"):
        gate.consume("client-b")


def test_run_telemetry_feature_flag_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_RUN_TELEMETRY", "false")

    settings = RuntimeSettings.from_env()

    assert settings.enable_run_telemetry is False


def test_inline_citation_audit_is_disabled_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ENABLE_INLINE_CITATION_AUDIT", raising=False)

    settings = RuntimeSettings.from_env()

    assert settings.enable_inline_citation_audit is False
