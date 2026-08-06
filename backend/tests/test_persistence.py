"""Tests for atomic, fail-open run-metrics persistence."""

from __future__ import annotations

import json
import stat
from datetime import datetime, timezone

import pytest

from evaluation.persistence import RunMetricsStore
from evaluation.telemetry import RunRecorder


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
def test_each_terminal_state_writes_one_private_json(tmp_path, terminal: str) -> None:
    store = RunMetricsStore(tmp_path)
    recorder = RunRecorder(
        run_id=f"run_{terminal}",
        topic="持久化测试",
        persist_callback=store.persist,
        wall_clock=lambda: datetime(2026, 8, 6, tzinfo=timezone.utc),
    )

    if terminal == "completed":
        recorder.mark_completed()
    elif terminal == "failed":
        recorder.mark_failed(RuntimeError("provider unavailable"), stage="search")
    else:
        recorder.mark_cancelled(stage="summarization")

    artifact = tmp_path / f"run_{terminal}.json"
    payload = json.loads(artifact.read_text(encoding="utf-8"))
    assert payload["status"] == terminal
    assert payload["run_id"] == f"run_{terminal}"
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert list(tmp_path.glob("*.tmp")) == []


def test_invalid_run_id_cannot_escape_metrics_directory(tmp_path) -> None:
    store = RunMetricsStore(tmp_path)

    with pytest.raises(ValueError, match="unsafe"):
        store.persist({"run_id": "../escape", "status": "completed"})

    assert list(tmp_path.iterdir()) == []


def test_persistence_failure_never_changes_terminal_business_result() -> None:
    def failing_persist(_metrics):
        raise OSError("disk full")

    recorder = RunRecorder(
        run_id="run_disk_failure",
        topic="故障测试",
        persist_callback=failing_persist,
    )

    recorder.mark_completed()

    metrics = recorder.snapshot()
    assert metrics["status"] == "completed"
    assert "metrics_persistence_failed:OSError" in metrics["warnings"]
    assert metrics["metrics_complete"] is False
