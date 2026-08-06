"""Smoke tests for public HTTP behavior without calling external providers."""

from __future__ import annotations

from fastapi.testclient import TestClient

import main as main_module
from config import Configuration
from main import (
    ResearchRequest,
    ResearchResponse,
    _build_config,
    _register_job,
    _remove_job,
    app,
)


def test_health_endpoint_is_publicly_available() -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_disabled_optional_provenance_does_not_change_response_shape() -> None:
    response = ResearchResponse(job_id="job_123456", report_markdown="# report")

    assert response.model_dump(exclude_none=True) == {
        "job_id": "job_123456",
        "report_markdown": "# report",
        "todo_items": [],
    }


def test_request_can_override_provenance_for_controlled_ab_run(monkeypatch) -> None:
    monkeypatch.setenv("ENABLE_SOURCE_PROVENANCE", "false")

    enabled = _build_config(
        ResearchRequest(
            topic="测试研究主题",
            job_id="provenance_ab_on",
            enable_source_provenance=True,
        )
    )
    defaulted = _build_config(
        ResearchRequest(topic="测试研究主题", job_id="provenance_ab_default")
    )

    assert enabled.enable_source_provenance is True
    assert defaulted.enable_source_provenance is False


def test_missing_model_configuration_returns_actionable_error(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "custom")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)

    with TestClient(app) as client:
        response = client.post(
            "/research/stream",
            json={"topic": "测试研究主题", "job_id": "test_job_1234"},
        )

    assert response.status_code == 503
    assert "LLM_API_KEY" in response.json()["detail"]


def test_active_job_can_be_cancelled() -> None:
    job_id = "cancel_test_job"
    cancel_event = _register_job(job_id)
    try:
        with TestClient(app) as client:
            response = client.post(f"/research/{job_id}/cancel")

        assert response.status_code == 200
        assert response.json()["status"] == "cancellation_requested"
        assert cancel_event.is_set()
    finally:
        _remove_job(job_id)


def test_streaming_api_finalizes_telemetry_without_changing_events(monkeypatch) -> None:
    captured = {}

    class FakeAgent:
        def __init__(self, *, recorder, **_kwargs):
            captured["recorder"] = recorder

        def run_stream(self, _topic):
            yield {"type": "status", "message": "working"}
            yield {"type": "final_report", "report": "# report"}
            yield {"type": "done"}

    monkeypatch.setattr(main_module, "DeepResearchAgent", FakeAgent)
    monkeypatch.setattr(
        main_module,
        "_validated_config",
        lambda _payload: Configuration(enable_notes=False),
    )
    monkeypatch.setattr(main_module, "_consume_research_budget", lambda _request: None)

    with TestClient(app) as client:
        response = client.post(
            "/research/stream",
            json={"topic": "测试研究主题", "job_id": "metrics_api_job"},
        )

    assert response.status_code == 200
    assert '"type": "status"' in response.text
    assert '"type": "final_report"' in response.text
    assert '"type": "done"' in response.text
    assert '"type": "metrics"' not in response.text
    assert captured["recorder"].snapshot()["status"] == "completed"


def test_streaming_api_records_failure_without_changing_error_event(
    monkeypatch,
) -> None:
    captured = {}

    class FailingAgent:
        def __init__(self, *, recorder, **_kwargs):
            captured["recorder"] = recorder

        def run_stream(self, _topic):
            yield {"type": "status", "message": "working"}
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(main_module, "DeepResearchAgent", FailingAgent)
    monkeypatch.setattr(
        main_module,
        "_validated_config",
        lambda _payload: Configuration(enable_notes=False),
    )
    monkeypatch.setattr(main_module, "_consume_research_budget", lambda _request: None)

    with TestClient(app) as client:
        response = client.post(
            "/research/stream",
            json={"topic": "测试研究主题", "job_id": "metrics_fail_job"},
        )

    assert response.status_code == 200
    assert '"type": "error"' in response.text
    assert '"type": "metrics"' not in response.text
    metrics = captured["recorder"].snapshot()
    assert metrics["status"] == "failed"
    assert metrics["failure_type"] == "RuntimeError"


def test_streaming_api_records_cancellation_without_changing_event(monkeypatch) -> None:
    captured = {}

    class CancelledAgent:
        def __init__(self, *, recorder, **_kwargs):
            captured["recorder"] = recorder

        def run_stream(self, _topic):
            yield {"type": "status", "message": "working"}
            raise main_module.ResearchCancelledError("研究任务已取消")

    monkeypatch.setattr(main_module, "DeepResearchAgent", CancelledAgent)
    monkeypatch.setattr(
        main_module,
        "_validated_config",
        lambda _payload: Configuration(enable_notes=False),
    )
    monkeypatch.setattr(main_module, "_consume_research_budget", lambda _request: None)

    with TestClient(app) as client:
        response = client.post(
            "/research/stream",
            json={"topic": "测试研究主题", "job_id": "metrics_cancel_job"},
        )

    assert response.status_code == 200
    assert '"type": "cancelled"' in response.text
    assert '"type": "metrics"' not in response.text
    assert captured["recorder"].snapshot()["status"] == "cancelled"
