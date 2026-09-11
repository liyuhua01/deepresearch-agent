"""Search fallback behavior for public demo deployments."""

from __future__ import annotations

import socket

import pytest

from config import Configuration, SearchAPI
from evaluation.telemetry import RunRecorder
from services import search


def test_duckduckgo_uses_ddgs_as_primary_without_false_failure(monkeypatch) -> None:
    recorder = RunRecorder(run_id="search_primary", topic="test")
    primary_payload = {
        "results": [
            {
                "title": "Primary result",
                "url": "https://example.com/source",
                "content": "Evidence",
                "raw_content": "Evidence",
            }
        ],
        "backend": "ddgs-auto",
        "answer": None,
        "notices": [],
    }
    monkeypatch.setattr(
        search,
        "_ddgs_multi_engine_search",
        lambda _query, *, max_results: primary_payload,
    )

    payload, notices, answer, backend = search.dispatch_search(
        "latest agent frameworks",
        Configuration(
            search_api=SearchAPI.DUCKDUCKGO,
            fetch_full_page=False,
        ),
        loop_count=0,
        recorder=recorder,
    )

    assert payload == primary_payload
    assert notices == []
    assert answer is None
    assert backend == "ddgs-auto"
    metrics = recorder.snapshot()
    assert metrics["search_attempts"] == 1
    assert metrics["search_successes"] == 1
    assert metrics["search_failures"] == 0
    assert metrics["fallback_triggers"] == 0
    assert metrics["fallback_successes"] == 0


def test_duckduckgo_enriches_full_page_evidence_without_mutating_primary(
    monkeypatch,
) -> None:
    primary_payload = {
        "results": [
            {
                "title": "Primary result",
                "url": "https://example.com/source",
                "content": "Search snippet",
                "raw_content": "Search snippet",
            }
        ],
        "backend": "ddgs-auto",
        "answer": None,
        "notices": [],
    }
    monkeypatch.setattr(
        search,
        "_ddgs_multi_engine_search",
        lambda _query, *, max_results: primary_payload,
    )
    monkeypatch.setattr(
        search,
        "_fetch_public_page_text",
        lambda _url, *, max_chars: "Bounded full-page evidence",
    )

    payload, notices, _, _ = search.dispatch_search(
        "latest agent frameworks",
        Configuration(
            search_api=SearchAPI.DUCKDUCKGO,
            fetch_full_page=True,
        ),
        loop_count=0,
    )

    assert payload is not None
    assert payload["results"][0]["raw_content"] == "Bounded full-page evidence"
    assert primary_payload["results"][0]["raw_content"] == "Search snippet"
    assert primary_payload["notices"] == []
    assert notices == ["DDGS 主搜索已取得 1/1 个受限全文证据。"]


def test_duckduckgo_skips_full_page_fetch_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(
        search,
        "_ddgs_multi_engine_search",
        lambda _query, *, max_results: {
            "results": [
                {
                    "title": "Primary result",
                    "url": "https://example.com/source",
                    "content": "Search snippet",
                    "raw_content": "Search snippet",
                }
            ],
            "backend": "ddgs-auto",
            "answer": None,
            "notices": [],
        },
    )
    monkeypatch.setattr(
        search,
        "_enrich_ddgs_full_pages",
        lambda *_args, **_kwargs: pytest.fail("full-page fetch should be disabled"),
    )

    payload, _, _, _ = search.dispatch_search(
        "latest agent frameworks",
        Configuration(
            search_api=SearchAPI.DUCKDUCKGO,
            fetch_full_page=False,
        ),
        loop_count=0,
    )

    assert payload is not None
    assert payload["results"][0]["raw_content"] == "Search snippet"


def test_reject_non_public_evidence_hosts(monkeypatch) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.4", 0))],
    )

    with pytest.raises(ValueError, match="non-public"):
        search._reject_non_public_host("internal.example")


def test_empty_ddgs_and_failed_legacy_fallback_records_failure(monkeypatch) -> None:
    recorder = RunRecorder(run_id="search_fallback_failed", topic="test")
    monkeypatch.setattr(
        search,
        "_ddgs_multi_engine_search",
        lambda _query, *, max_results: {
            "results": [],
            "backend": "ddgs-auto",
            "answer": None,
            "notices": [],
        },
    )
    monkeypatch.setattr(
        search._GLOBAL_SEARCH_TOOL,
        "run",
        lambda _parameters: (_ for _ in ()).throw(RuntimeError("legacy failed")),
    )

    with pytest.raises(RuntimeError, match="legacy failed"):
        search.dispatch_search(
            "framework landscape",
            Configuration(search_api=SearchAPI.DUCKDUCKGO),
            loop_count=0,
            recorder=recorder,
        )

    metrics = recorder.snapshot()
    assert metrics["search_attempts"] == 1
    assert metrics["search_failures"] == 1
    assert metrics["search_empty_results"] == 1
    assert metrics["fallback_triggers"] == 1
    assert metrics["fallback_successes"] == 0
    assert metrics["fallback_triggers"] - metrics["fallback_successes"] == 1


def test_empty_ddgs_primary_uses_legacy_adapter_fallback(monkeypatch) -> None:
    recorder = RunRecorder(run_id="search_empty", topic="test")
    monkeypatch.setattr(
        search,
        "_ddgs_multi_engine_search",
        lambda _query, *, max_results: {
            "results": [],
            "backend": "ddgs-auto",
            "answer": None,
            "notices": [],
        },
    )
    monkeypatch.setattr(
        search._GLOBAL_SEARCH_TOOL,
        "run",
        lambda _parameters: {
            "results": [{"title": "Recovered", "url": "https://example.com"}],
            "backend": "duckduckgo-legacy",
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
    assert backend == "duckduckgo-legacy"
    metrics = recorder.snapshot()
    assert metrics["search_attempts"] == 1
    assert metrics["search_failures"] == 1
    assert metrics["search_empty_results"] == 1
    assert metrics["fallback_triggers"] == 1
    assert metrics["fallback_successes"] == 1


def test_provenance_search_adds_and_prioritizes_official_sources(monkeypatch) -> None:
    recorder = RunRecorder(run_id="quality_search", topic="asyncio")
    ddgs_results = iter(
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
            "backend": "ddgs-auto",
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
    monkeypatch.setattr(
        search,
        "_ddgs_multi_engine_search",
        lambda _query, *, max_results: next(ddgs_results),
    )

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
    assert len(payload["results"]) == 2
    assert any("来源质量排序" in notice for notice in notices)
    metrics = recorder.snapshot()
    assert metrics["search_attempts"] == 2
    assert metrics["search_successes"] == 2


def test_provenance_supplement_failure_keeps_primary_results(monkeypatch) -> None:
    call_count = 0

    def ddgs(_query, *, max_results):
        nonlocal call_count
        call_count += 1
        if call_count > 1:
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

    monkeypatch.setattr(
        search,
        "_ddgs_multi_engine_search",
        ddgs,
    )

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


def test_authoritative_query_targets_mdn_for_sse_and_websocket() -> None:
    query = search._build_authoritative_query("SSE 与 WebSocket 自动重连")

    assert query.startswith("site:developer.mozilla.org ")


def test_ai_act_subquery_targets_eu_and_seeds_canonical_sources() -> None:
    query = "AI Act 高风险系统适用时间线"

    assert search._build_authoritative_query(query).startswith("site:europa.eu ")
    seeds = search._authoritative_seed_results(query)
    assert [item["url"] for item in seeds] == [
        "https://eur-lex.europa.eu/eli/reg/2024/1689/oj",
        "https://digital-strategy.ec.europa.eu/en/policies/regulatory-framework-ai",
    ]


def test_normal_search_filters_unrelated_portal_noise(monkeypatch) -> None:
    monkeypatch.setattr(
        search,
        "_ddgs_multi_engine_search",
        lambda _query, *, max_results: {
            "results": [
                {
                    "title": "Using server-sent events",
                    "url": "https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events",
                    "content": "SSE EventSource reconnect behavior",
                },
                {
                    "title": "Video homepage",
                    "url": "https://youtube.com/",
                    "content": "videos",
                },
            ],
            "backend": "ddgs-auto",
            "answer": None,
            "notices": [],
        },
    )

    payload, _, _, _ = search.dispatch_search(
        "SSE EventSource reconnect behavior",
        Configuration(search_api=SearchAPI.DUCKDUCKGO, fetch_full_page=False),
        loop_count=0,
    )

    assert payload is not None
    assert [item["title"] for item in payload["results"]] == [
        "Using server-sent events"
    ]


def test_provenance_retries_when_provider_ignores_official_domain(monkeypatch) -> None:
    ddgs_responses = iter(
        [
            {
                "results": [
                    {"title": "asyncio article", "url": "https://example.com/asyncio"}
                ],
                "backend": "duckduckgo",
                "notices": [],
            },
            {
                "results": [
                    {
                        "title": "asyncio — Asynchronous I/O",
                        "url": "https://docs.python.org/3/library/asyncio.html",
                        "content": "official evidence",
                    }
                ]
            },
        ]
    )
    monkeypatch.setattr(
        search,
        "_ddgs_multi_engine_search",
        lambda _query, max_results: next(ddgs_responses),
    )
    monkeypatch.setattr(
        search._GLOBAL_SEARCH_TOOL,
        "run",
        lambda _params: {
            "results": [{"title": "Greek Peak", "url": "https://www.greekpeak.net/"}],
            "backend": "duckduckgo",
            "notices": [],
        },
    )

    payload, notices, _, _ = search.dispatch_search(
        "Python asyncio concurrency",
        Configuration(
            search_api=SearchAPI.DUCKDUCKGO,
            fetch_full_page=False,
            enable_source_provenance=True,
        ),
        loop_count=0,
    )

    assert payload is not None
    assert [item["url"] for item in payload["results"]] == [
        "https://docs.python.org/3/library/asyncio.html",
        "https://example.com/asyncio",
    ]
    assert not any("多引擎检索仍未返回" in notice for notice in notices)
