"""Search dispatch helpers leveraging HelloAgents SearchTool."""

from __future__ import annotations

import ipaddress
import logging
import socket
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from typing import Any, Tuple
from urllib.parse import urljoin, urlsplit

import httpx
from hello_agents.tools import SearchTool

from evaluation.citations import normalize_url
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
MAX_PAGE_BYTES = 262_144
MAX_PAGE_REDIRECTS = 3
_GLOBAL_SEARCH_TOOL = SearchTool(backend="hybrid")
_OFFICIAL_SITE_HINTS = (
    (
        {"python", "asyncio", "threadpoolexecutor", "concurrent.futures"},
        "docs.python.org",
    ),
    ({"kubernetes", "k8s"}, "kubernetes.io"),
    ({"javascript", "typescript", "web api"}, "developer.mozilla.org"),
    ({"openai", "chatgpt"}, "openai.com"),
)


def _ddgs_multi_engine_search(query: str, *, max_results: int) -> dict[str, Any]:
    """Search multiple public engines through the maintained DDGS client."""
    if DDGS is None:
        raise RuntimeError("ddgs 未安装，无法启用多引擎主搜索")

    try:
        with DDGS(timeout=15) as client:
            search_results = client.text(
                query,
                max_results=max_results,
                backend="auto",
                region="wt-wt",
            )
    except Exception as exc:
        raise RuntimeError(f"多引擎主搜索失败: {exc}") from exc

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
        raise RuntimeError("多引擎主搜索未返回有效结果")

    return {
        "results": results,
        "backend": "ddgs-auto",
        "answer": None,
        "notices": [],
    }


class _HTMLTextExtractor(HTMLParser):
    """Extract visible text from bounded HTML without another dependency."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._ignored_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)

    def handle_data(self, data: str) -> None:
        if not self._ignored_depth and (text := " ".join(data.split())):
            self.parts.append(text)


def _enrich_ddgs_full_pages(
    payload: dict[str, Any],
    *,
    max_tokens_per_source: int,
) -> dict[str, Any]:
    """Fetch bounded public-page text while preserving every DDGS result."""
    results = [dict(item) for item in payload.get("results") or []]
    if not results:
        return payload
    max_chars = max(1, max_tokens_per_source) * 4
    with ThreadPoolExecutor(max_workers=min(4, len(results))) as executor:
        fetched_pages = list(
            executor.map(
                lambda item: _fetch_public_page_text(
                    str(item.get("url") or ""), max_chars=max_chars
                ),
                results,
            )
        )
    fetched_count = 0
    for item, fetched in zip(results, fetched_pages, strict=True):
        if fetched:
            item["raw_content"] = fetched
            fetched_count += 1
        else:
            item.setdefault("raw_content", str(item.get("content") or ""))
    enriched = dict(payload)
    enriched["results"] = results
    enriched["notices"] = list(payload.get("notices") or [])
    enriched["notices"].append(
        f"DDGS 主搜索已取得 {fetched_count}/{len(results)} 个受限全文证据。"
    )
    return enriched


def _fetch_public_page_text(url: str, *, max_chars: int) -> str | None:
    """Fetch one text page with SSRF, redirect, timeout and size bounds."""
    current = normalize_url(url)
    if current is None:
        return None
    try:
        with httpx.Client(
            timeout=httpx.Timeout(10.0),
            follow_redirects=False,
            headers={
                "User-Agent": "DeepResearchEvidenceFetcher/1.0",
                "Accept": "text/html,text/plain,application/xhtml+xml",
                "Range": f"bytes=0-{MAX_PAGE_BYTES - 1}",
            },
        ) as client:
            for redirect_index in range(MAX_PAGE_REDIRECTS + 1):
                _reject_non_public_host(urlsplit(current).hostname or "")
                with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location or redirect_index == MAX_PAGE_REDIRECTS:
                            return None
                        current = normalize_url(urljoin(current, location))
                        if current is None:
                            return None
                        continue
                    response.raise_for_status()
                    content_type = response.headers.get("content-type", "").lower()
                    if not any(
                        allowed in content_type
                        for allowed in (
                            "text/html",
                            "text/plain",
                            "application/xhtml+xml",
                        )
                    ):
                        return None
                    received = bytearray()
                    for chunk in response.iter_bytes():
                        remaining = MAX_PAGE_BYTES - len(received)
                        if remaining <= 0:
                            break
                        received.extend(chunk[:remaining])
                    text = bytes(received).decode(
                        response.charset_encoding or "utf-8",
                        errors="replace",
                    )
                    if "html" in content_type:
                        parser = _HTMLTextExtractor()
                        parser.feed(text)
                        text = "\n".join(parser.parts)
                    normalized_text = "\n".join(
                        line
                        for line in (part.strip() for part in text.splitlines())
                        if line
                    )
                    return normalized_text[:max_chars] or None
    except (httpx.HTTPError, OSError, ValueError, UnicodeError):
        return None
    return None


def _reject_non_public_host(host: str) -> None:
    """Reject localhost and any hostname resolving to non-public addresses."""
    if not host or host.lower() == "localhost":
        raise ValueError("non-public evidence host")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        records = socket.getaddrinfo(host, None)
        addresses = list({ipaddress.ip_address(record[4][0]) for record in records})
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("non-public evidence host")


def _helloagents_duckduckgo_fallback(
    query: str,
    *,
    config: Configuration,
    loop_count: int,
) -> dict[str, Any]:
    """Use the legacy HelloAgents DuckDuckGo adapter as a bounded fallback."""
    response = _GLOBAL_SEARCH_TOOL.run(
        {
            "input": query,
            "backend": "duckduckgo",
            "mode": "structured",
            "fetch_full_page": config.fetch_full_page,
            "max_results": 5,
            "max_tokens_per_source": MAX_TOKENS_PER_SOURCE,
            "loop_count": loop_count,
        }
    )
    if not isinstance(response, dict) or not response.get("results"):
        raise RuntimeError("HelloAgents DuckDuckGo 备用路径未返回有效结果")
    payload = dict(response)
    payload.setdefault("backend", "duckduckgo-legacy")
    payload.setdefault("notices", []).append(
        "DDGS 主搜索不可用，已使用 HelloAgents DuckDuckGo 备用路径。"
    )
    return payload


def dispatch_search(
    query: str,
    config: Configuration,
    loop_count: int,
    recorder: RunRecorder | None = None,
) -> Tuple[dict[str, Any] | None, list[str], str | None, str]:
    """Execute configured search backend and normalise response payload."""
    search_api = get_config_value(config.search_api)
    primary_outcome_recorded = False
    if recorder:
        recorder.record_search_attempt()

    try:
        if search_api == "duckduckgo":
            # The deployed HelloAgents DDG adapter failed on 32/33 recorded
            # attempts, while DDGS recovered every triggered fallback. Promote
            # the proven path to primary so normal operation is not reported as
            # a failure/recovery cycle.
            raw_response = _ddgs_multi_engine_search(query, max_results=5)
            if config.fetch_full_page:
                raw_response = _enrich_ddgs_full_pages(
                    raw_response,
                    max_tokens_per_source=MAX_TOKENS_PER_SOURCE,
                )
        else:
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
        if recorder:
            recorder.record_fallback_trigger()
        try:
            raw_response = _helloagents_duckduckgo_fallback(
                query,
                config=config,
                loop_count=loop_count,
            )
        except Exception:
            if recorder:
                recorder.record_fallback_result(success=False)
            raise
        if recorder:
            recorder.record_fallback_result(success=True)

    if (
        search_api == "duckduckgo"
        and isinstance(raw_response, dict)
        and not raw_response.get("results")
    ):
        if recorder:
            recorder.record_search_failure(empty_result=True)
        primary_outcome_recorded = True
        if recorder:
            recorder.record_fallback_trigger()
        try:
            raw_response = _helloagents_duckduckgo_fallback(
                query,
                config=config,
                loop_count=loop_count,
            )
        except Exception:
            if recorder:
                recorder.record_fallback_result(success=False)
            raise
        if recorder:
            recorder.record_fallback_result(success=True)

    if isinstance(raw_response, str):
        notices = [raw_response]
        logger.warning(
            "Search backend %s returned text notice: %s", search_api, raw_response
        )
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
            fallback = _ddgs_multi_engine_search(supplemental_query, max_results=5)
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


def prepare_research_context(
    search_result: dict[str, Any] | None,
    answer_text: str | None,
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
