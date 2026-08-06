"""Search dispatch helpers leveraging HelloAgents SearchTool."""

from __future__ import annotations

import logging
from typing import Any, Optional, Tuple

from hello_agents.tools import SearchTool

from evaluation.provenance import rank_search_results
from evaluation.telemetry import RunRecorder

try:
    from ddgs import DDGS
except Exception:  # pragma: no cover - dependency is validated at runtime
    DDGS = None  # type: ignore[assignment]

from config import Configuration
from utils import (
    deduplicate_and_format_sources,
    format_sources,
    get_config_value,
)

logger = logging.getLogger(__name__)

MAX_TOKENS_PER_SOURCE = 2000
_GLOBAL_SEARCH_TOOL = SearchTool(backend="hybrid")
_OFFICIAL_SITE_HINTS = (
    ({"python", "asyncio", "threadpoolexecutor", "concurrent.futures"}, "docs.python.org"),
    ({"kubernetes", "k8s"}, "kubernetes.io"),
    ({"javascript", "typescript", "web api"}, "developer.mozilla.org"),
    ({"openai", "chatgpt"}, "openai.com"),
)


def _ddgs_auto_fallback(query: str, *, max_results: int) -> dict[str, Any]:
    """Search multiple public engines when the direct DDG backend is blocked."""

    if DDGS is None:
        raise RuntimeError("ddgs 未安装，无法启用多引擎搜索兜底")

    try:
        with DDGS(timeout=15) as client:
            search_results = client.text(
                query,
                max_results=max_results,
                backend="auto",
                region="wt-wt",
            )
    except Exception as exc:
        raise RuntimeError(f"多引擎搜索兜底失败: {exc}") from exc

    results: list[dict[str, str]] = []
    for entry in search_results:
        url = entry.get("href") or entry.get("url")
        title = entry.get("title") or url
        if not url or not title:
            continue
        content = entry.get("body") or entry.get("content") or ""
        results.append(
            {
                "title": str(title),
                "url": str(url),
                "content": str(content),
                "raw_content": str(content),
            }
        )

    if not results:
        raise RuntimeError("多引擎搜索兜底未返回有效结果")

    return {
        "results": results,
        "backend": "ddgs-auto",
        "answer": None,
        "notices": ["DuckDuckGo 直连无结果，已自动切换到多引擎搜索。"],
    }


def dispatch_search(
    query: str,
    config: Configuration,
    loop_count: int,
    recorder: RunRecorder | None = None,
) -> Tuple[dict[str, Any] | None, list[str], Optional[str], str]:
    """Execute configured search backend and normalise response payload."""

    search_api = get_config_value(config.search_api)
    primary_outcome_recorded = False
    if recorder:
        recorder.record_search_attempt()

    try:
        raw_response = _GLOBAL_SEARCH_TOOL.run(
            {
                "input": query,
                "backend": search_api,
                "mode": "structured",
                "fetch_full_page": config.fetch_full_page,
                "max_results": 5,
                "max_tokens_per_source": MAX_TOKENS_PER_SOURCE,
                "loop_count": loop_count,
            }
        )
    except Exception as exc:  # pragma: no cover - provider errors vary by network
        logger.exception("Search backend %s failed: %s", search_api, exc)
        if recorder:
            recorder.record_search_failure()
        primary_outcome_recorded = True
        if search_api != "duckduckgo":
            raise
        raw_response = _run_recorded_fallback(query, recorder=recorder)

    if (
        search_api == "duckduckgo"
        and isinstance(raw_response, dict)
        and not raw_response.get("results")
    ):
        if recorder:
            recorder.record_search_failure(empty_result=True)
        primary_outcome_recorded = True
        raw_response = _run_recorded_fallback(query, recorder=recorder)

    if isinstance(raw_response, str):
        notices = [raw_response]
        logger.warning("Search backend %s returned text notice: %s", search_api, raw_response)
        payload: dict[str, Any] = {
            "results": [],
            "backend": search_api,
            "answer": None,
            "notices": notices,
        }
    else:
        payload = raw_response

    if config.enable_source_provenance and payload.get("results"):
        payload = _enhance_provenance_search(
            query,
            payload,
            config=config,
            loop_count=loop_count,
            recorder=recorder,
        )

    notices = list(payload.get("notices") or [])

    backend_label = str(payload.get("backend") or search_api)
    answer_text = payload.get("answer")
    results = payload.get("results", [])

    if not primary_outcome_recorded and recorder:
        if results:
            recorder.record_search_success()
        else:
            recorder.record_search_failure(empty_result=True)

    if notices:
        for notice in notices:
            logger.info("Search notice (%s): %s", backend_label, notice)

    logger.info(
        "Search backend=%s resolved_backend=%s answer=%s results=%s",
        search_api,
        backend_label,
        bool(answer_text),
        len(results),
    )

    return payload, notices, answer_text, backend_label


def _enhance_provenance_search(
    query: str,
    payload: dict[str, Any],
    *,
    config: Configuration,
    loop_count: int,
    recorder: RunRecorder | None,
) -> dict[str, Any]:
    """Add one bounded authoritative-source search, then rank the merged pool."""
    search_api = get_config_value(config.search_api)
    supplemental_query = _build_authoritative_query(query)
    expected_official_domain = _official_domain_for_query(query)
    notices = list(payload.get("notices") or [])
    supplemental_results: list[dict[str, Any]] = []
    if recorder:
        recorder.record_search_attempt()
    try:
        supplemental = _GLOBAL_SEARCH_TOOL.run(
            {
                "input": supplemental_query,
                "backend": search_api,
                "mode": "structured",
                "fetch_full_page": config.fetch_full_page,
                "max_results": 5,
                "max_tokens_per_source": MAX_TOKENS_PER_SOURCE,
                "loop_count": loop_count,
            }
        )
        if isinstance(supplemental, dict):
            supplemental_results = list(supplemental.get("results") or [])
        if recorder:
            if supplemental_results:
                recorder.record_search_success()
            else:
                recorder.record_search_failure(empty_result=True)
        if not supplemental_results:
            notices.append("官方/一手资料补充检索未返回结果，已保留主检索来源。")
    except Exception as exc:  # quality enhancement must remain fail-open
        logger.warning("Authoritative source supplement failed: %s", exc)
        if recorder:
            recorder.record_search_failure()
        notices.append("官方/一手资料补充检索失败，已使用主检索来源继续。")

    if expected_official_domain and not _contains_domain(
        supplemental_results,
        expected_official_domain,
    ):
        notices.append(
            f"补充检索未返回 {expected_official_domain} 官方来源，"
            "已触发受限多引擎官方域名检索。"
        )
        if recorder:
            recorder.record_search_attempt()
        try:
            fallback = _ddgs_auto_fallback(supplemental_query, max_results=5)
            fallback_results = list(fallback.get("results") or [])
            verified = [
                item
                for item in fallback_results
                if _url_matches_domain(
                    str(item.get("url") or ""), expected_official_domain
                )
            ]
            if recorder:
                if verified:
                    recorder.record_search_success()
                else:
                    recorder.record_search_failure(empty_result=True)
            if verified:
                supplemental_results = verified
            else:
                notices.append("多引擎检索仍未返回经域名校验的官方来源。")
        except Exception as exc:  # fail-open quality recovery
            logger.warning("Verified official-source fallback failed: %s", exc)
            if recorder:
                recorder.record_search_failure()
            notices.append("多引擎官方域名检索失败，已保留可用的原始来源。")

    merged = dict(payload)
    merged["results"] = list(payload.get("results") or []) + supplemental_results
    merged["notices"] = notices
    ranked = rank_search_results(
        merged,
        relevance_text=query,
        max_results=8,
        require_relevance=expected_official_domain is not None,
    )
    ranked.setdefault("notices", []).append(
        f"来源质量排序已从 {len(merged['results'])} 条候选中选择 "
        f"{len(ranked.get('results') or [])} 条。"
    )
    return ranked


def _build_authoritative_query(query: str) -> str:
    """Add a transparent official-site hint for known technical ecosystems."""
    domain = _official_domain_for_query(query)
    if domain:
        return f"site:{domain} {query} official documentation"
    return f"{query} 官方文档 official documentation primary source"


def _official_domain_for_query(query: str) -> str | None:
    lowered = query.lower()
    for keywords, domain in _OFFICIAL_SITE_HINTS:
        if any(keyword in lowered for keyword in keywords):
            return domain
    return None


def _contains_domain(results: list[dict[str, Any]], expected_domain: str) -> bool:
    return any(
        _url_matches_domain(str(item.get("url") or ""), expected_domain)
        for item in results
    )


def _url_matches_domain(url: str, expected_domain: str) -> bool:
    from urllib.parse import urlsplit

    hostname = (urlsplit(url).hostname or "").lower().rstrip(".")
    expected = expected_domain.lower().rstrip(".")
    return hostname == expected or hostname.endswith(f".{expected}")


def _run_recorded_fallback(
    query: str,
    *,
    recorder: RunRecorder | None,
) -> dict[str, Any]:
    """Run the existing fallback while recording only its outcome."""

    if recorder:
        recorder.record_fallback_trigger()
    try:
        payload = _ddgs_auto_fallback(query, max_results=5)
    except Exception:
        if recorder:
            recorder.record_fallback_result(success=False)
        raise
    if recorder:
        recorder.record_fallback_result(success=bool(payload.get("results")))
    return payload


def prepare_research_context(
    search_result: dict[str, Any] | None,
    answer_text: Optional[str],
    config: Configuration,
) -> tuple[str, str]:
    """Build structured context and source summary for downstream agents."""

    sources_summary = format_sources(search_result)
    context = deduplicate_and_format_sources(
        search_result or {"results": []},
        max_tokens_per_source=MAX_TOKENS_PER_SOURCE,
        fetch_full_page=config.fetch_full_page,
    )

    if answer_text:
        context = f"AI直接答案：\n{answer_text}\n\n{context}"

    return sources_summary, context
