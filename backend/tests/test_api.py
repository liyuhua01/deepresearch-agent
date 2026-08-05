"""Smoke tests for public HTTP behavior without calling external providers."""

from __future__ import annotations

from fastapi.testclient import TestClient

from main import _register_job, _remove_job, app


def test_health_endpoint_is_publicly_available() -> None:
    with TestClient(app) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


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
