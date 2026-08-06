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


def test_provenance_search_adds_and_prioritizes_official_sources(monkeypatch) -> None:
    recorder = RunRecorder(run_id="quality_search", topic="asyncio")
    responses = iter(
        [
            {
                "results": [
                    {
                        "title": "Asyncio community comparison",
                        "url": "https://example.com/asyncio-comparison",
                        "content": "community evidence",
                    },
                    {
                        "title": "Unrelated package manager",
                        "url": "https://example.org/uv-guide",
                        "content": "unrelated",
                    },
                ],
                "backend": "duckduckgo",
                "answer": None,
                "notices": [],
            },
            {
                "results": [
                    {
                        "title": "asyncio — Asynchronous I/O",
                        "url": "https://docs.python.org/3/library/asyncio.html",
                        "content": "official evidence",
                    }
                ],
                "backend": "duckduckgo",
                "answer": None,
                "notices": [],
            },
        ]
    )
    monkeypatch.setattr(search._GLOBAL_SEARCH_TOOL, "run", lambda _params: next(responses))

    payload, notices, _, _ = search.dispatch_search(
        "Python asyncio high concurrency",
        Configuration(
            search_api=SearchAPI.DUCKDUCKGO,
            fetch_full_page=False,
            enable_source_provenance=True,
        ),
        loop_count=0,
        recorder=recorder,
    )

    assert payload is not None
    assert payload["results"][0]["url"] == (
        "https://docs.python.org/3/library/asyncio.html"
    )
    assert len(payload["results"]) == 3
    assert any("来源质量排序" in notice for notice in notices)
    metrics = recorder.snapshot()
    assert metrics["search_attempts"] == 2
    assert metrics["search_successes"] == 2


def test_provenance_supplement_failure_keeps_primary_results(monkeypatch) -> None:
    calls = 0

    def run_search(_params):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("supplement unavailable")
        return {
            "results": [
                {
                    "title": "Primary asyncio result",
                    "url": "https://example.com/asyncio",
                    "content": "evidence",
                }
            ],
            "backend": "duckduckgo",
            "answer": None,
            "notices": [],
        }

    monkeypatch.setattr(search._GLOBAL_SEARCH_TOOL, "run", run_search)

    payload, notices, _, _ = search.dispatch_search(
        "Python asyncio",
        Configuration(
            search_api=SearchAPI.DUCKDUCKGO,
            fetch_full_page=False,
            enable_source_provenance=True,
        ),
        loop_count=0,
    )

    assert payload is not None
    assert [item["url"] for item in payload["results"]] == [
        "https://example.com/asyncio"
    ]
    assert any("补充检索失败" in notice for notice in notices)


def test_authoritative_query_uses_known_official_technical_domain() -> None:
    query = search._build_authoritative_query(
        "Python asyncio 与 ThreadPoolExecutor 错误处理"
    )

    assert query.startswith("site:docs.python.org ")
