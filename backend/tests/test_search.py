"""Search fallback behavior for public demo deployments."""

from __future__ import annotations

from config import Configuration, SearchAPI
from evaluation.telemetry import RunRecorder
from services import search


def test_duckduckgo_failure_uses_multi_engine_fallback(monkeypatch) -> None:
    recorder = RunRecorder(run_id="search_failure", topic="test")

    def fail_direct_search(_parameters):
        raise RuntimeError("DuckDuckGo 搜索失败: No results found.")

    fallback_payload = {
        "results": [
            {
                "title": "Fallback result",
                "url": "https://example.com/source",
                "content": "Evidence",
                "raw_content": "Evidence",
            }
        ],
        "backend": "ddgs-auto",
        "answer": None,
        "notices": ["已自动切换到多引擎搜索。"],
    }

    monkeypatch.setattr(search._GLOBAL_SEARCH_TOOL, "run", fail_direct_search)
    monkeypatch.setattr(
        search,
        "_ddgs_auto_fallback",
        lambda _query, *, max_results: fallback_payload,
    )

    payload, notices, answer, backend = search.dispatch_search(
        "latest agent frameworks",
        Configuration(search_api=SearchAPI.DUCKDUCKGO),
        loop_count=0,
        recorder=recorder,
    )

    assert payload == fallback_payload
    assert notices == ["已自动切换到多引擎搜索。"]
    assert answer is None
    assert backend == "ddgs-auto"
    metrics = recorder.snapshot()
    assert metrics["search_attempts"] == 1
    assert metrics["search_failures"] == 1
    assert metrics["fallback_triggers"] == 1
    assert metrics["fallback_successes"] == 1


def test_empty_duckduckgo_payload_uses_multi_engine_fallback(monkeypatch) -> None:
    recorder = RunRecorder(run_id="search_empty", topic="test")
    monkeypatch.setattr(
        search._GLOBAL_SEARCH_TOOL,
        "run",
        lambda _parameters: {
            "results": [],
            "backend": "duckduckgo",
            "answer": None,
            "notices": [],
        },
    )
    monkeypatch.setattr(
        search,
        "_ddgs_auto_fallback",
        lambda _query, *, max_results: {
            "results": [{"title": "Recovered", "url": "https://example.com"}],
            "backend": "ddgs-auto",
            "answer": None,
            "notices": [],
        },
    )

    payload, _, _, backend = search.dispatch_search(
        "framework landscape",
        Configuration(search_api=SearchAPI.DUCKDUCKGO),
        loop_count=0,
        recorder=recorder,
    )

    assert payload is not None
    assert payload["results"]
    assert backend == "ddgs-auto"
    metrics = recorder.snapshot()
    assert metrics["search_attempts"] == 1
    assert metrics["search_failures"] == 1
    assert metrics["search_empty_results"] == 1
    assert metrics["fallback_triggers"] == 1
    assert metrics["fallback_successes"] == 1
