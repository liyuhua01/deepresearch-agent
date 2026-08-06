"""Deterministic source IDs and claim-to-source provenance tracking."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

from evaluation.citations import (
    audit_report,
    citation_occurrence_counts,
    is_evidence_claim,
    normalize_url,
    split_claim_units,
)
from evaluation.domains import classify_domain

_SOURCE_TOKEN = re.compile(r"\[(T\d+-S\d+)\](?!\()")
_SOURCE_ID_ANYWHERE = re.compile(r"T\d+-S\d+")
_SOURCE_LINK = re.compile(r"\[(T\d+-S\d+)\]\((https?://[^)\s]+)\)")
_PROVENANCE_META = re.compile(
    r"来源覆盖说明|来源概览|本总结引用了|以下综合\s*T\d+-S\d+|"
    r"当前证据库|核心可用|辅助旁证|背景参考|已忽略",
    re.IGNORECASE,
)
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z])(?=[A-Z])")
_TERM = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}|[\u3400-\u9fff]{2,}")
_GENERIC_TERMS = {
    "python",
    "using",
    "guide",
    "tutorial",
    "documentation",
    "docs",
    "比较",
    "建议",
    "场景",
}
_SOURCE_TYPE_WEIGHTS = {
    "government": 6,
    "official_documentation": 6,
    "academic": 5,
    "company_official": 3,
    "news_media": 2,
    "community": 1,
    "unknown": 0,
}


@dataclass(frozen=True)
class SourceRecord:
    """One normalized search source with a stable task-scoped identifier."""

    source_id: str
    task_id: int
    title: str
    url: str
    normalized_url: str
    domain: str
    relevance_status: str = "unassessed"
    relevance_terms: tuple[str, ...] = ()
    source_type: str = "unknown"
    quality_score: int = 0


@dataclass(frozen=True)
class ClaimMapping:
    """One summary claim and the source IDs emitted beside it."""

    claim_id: str
    task_id: int
    text: str
    source_ids: tuple[str, ...]
    unknown_source_ids: tuple[str, ...]
    mismatched_source_ids: tuple[str, ...] = ()
    unlinked_source_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProvenanceAudit:
    """Cross-check final-report links against process-time source records."""

    catalog_source_count: int
    catalog_sources_needing_relevance_review: int
    catalog_authoritative_source_count: int
    cited_catalog_source_count: int
    cited_catalog_source_rate: float | None
    report_unique_url_count: int
    report_citation_count_raw: int
    report_domain_count: int
    uncatalogued_url_count: int
    mapped_claim_count: int
    unmapped_claim_count: int
    unknown_source_id_count: int
    mismatched_source_id_count: int
    unlinked_source_id_count: int
    report_unknown_source_id_count: int
    report_duplicate_citation_count: int
    report_duplicate_citation_rate: float | None
    report_max_source_citation_share: float | None
    final_claim_units: int
    final_claim_units_with_citations: int
    final_claim_citation_coverage: float | None


def build_source_records(
    search_result: dict[str, Any] | None,
    *,
    task_id: int,
    relevance_text: str = "",
) -> list[SourceRecord]:
    """Assign deterministic IDs to unique valid URLs in search-result order."""
    results = (search_result or {}).get("results") or []
    records: list[SourceRecord] = []
    seen: set[str] = set()
    for item in results:
        raw_url = str(item.get("url") or "").strip()
        normalized = normalize_url(raw_url)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        title = str(item.get("title") or raw_url).strip() or raw_url
        relevance_status, relevance_terms = _assess_source_relevance(
            relevance_text,
            f"{title} {normalized}",
        )
        domain = urlsplit(normalized).hostname or ""
        source_type = classify_domain(domain, normalized)
        records.append(
            SourceRecord(
                source_id=f"T{task_id}-S{len(records) + 1}",
                task_id=task_id,
                title=title,
                url=raw_url,
                normalized_url=normalized,
                domain=domain,
                relevance_status=relevance_status,
                relevance_terms=relevance_terms,
                source_type=source_type,
                quality_score=_source_quality_score(
                    source_type,
                    relevance_status,
                    bool(str(item.get("content") or item.get("raw_content") or "")),
                ),
            )
        )
    return records


def format_source_catalog(records: list[SourceRecord]) -> str:
    """Render source IDs in a prompt- and user-readable Markdown catalog."""
    return "\n".join(
        f"- [{record.source_id}] [{_escape_markdown_label(record.title)}]"
        f"({record.normalized_url})"
        f" [{_source_type_label(record.source_type)}]"
        f"{' [相关性待复核]' if record.relevance_status == 'needs_review' else ''}"
        for record in records
    )


def add_catalog_to_context(context: str, records: list[SourceRecord]) -> str:
    """Prepend the stable source catalog without altering the evidence body."""
    if not records:
        return context
    return (
        f"来源编号目录：\n{format_source_catalog(records)}\n\n检索证据正文：\n{context}"
    )


def extract_claim_mappings(
    summary_markdown: str,
    *,
    task_id: int,
    sources: list[SourceRecord],
) -> list[ClaimMapping]:
    """Extract evidence claims and validate every referenced source ID."""
    by_id = {source.source_id: source for source in sources}
    mappings: list[ClaimMapping] = []
    for unit in split_claim_units(summary_markdown):
        if _PROVENANCE_META.search(unit) or not is_evidence_claim(unit):
            continue
        mentioned = list(dict.fromkeys(_SOURCE_ID_ANYWHERE.findall(unit)))
        linked_urls: dict[str, list[str]] = {}
        for source_id, raw_url in _SOURCE_LINK.findall(unit):
            linked_urls.setdefault(source_id, []).append(raw_url)

        known: list[str] = []
        mismatched: list[str] = []
        unlinked: list[str] = []
        for source_id in mentioned:
            source = by_id.get(source_id)
            if source is None:
                continue
            urls = linked_urls.get(source_id, [])
            if not urls:
                unlinked.append(source_id)
            elif any(normalize_url(url) == source.normalized_url for url in urls):
                known.append(source_id)
            else:
                mismatched.append(source_id)
        unknown = tuple(
            source_id for source_id in mentioned if source_id not in by_id
        )
        mappings.append(
            ClaimMapping(
                claim_id=f"T{task_id}-C{len(mappings) + 1}",
                task_id=task_id,
                text=unit,
                source_ids=tuple(known),
                unknown_source_ids=unknown,
                mismatched_source_ids=tuple(mismatched),
                unlinked_source_ids=tuple(unlinked),
            )
        )
    return mappings


def expand_source_tokens(markdown: str, sources: list[SourceRecord]) -> str:
    """Turn bare ``[T1-S1]`` tokens into clickable catalog-backed links."""
    by_id = {source.source_id: source for source in sources}

    def replace(match: re.Match[str]) -> str:
        source = by_id.get(match.group(1))
        if source is None:
            return match.group(0)
        return f"[{_escape_markdown_label(source.title)}]({source.normalized_url})"

    return _SOURCE_TOKEN.sub(replace, markdown)


def audit_provenance(
    report_markdown: str,
    *,
    sources: list[SourceRecord],
    claims: list[ClaimMapping],
) -> ProvenanceAudit:
    """Compare final URLs with the source catalog and process-time claim mappings."""
    citation_audit = audit_report(report_markdown)
    occurrence_counts = citation_occurrence_counts(report_markdown)
    report_urls = {citation.normalized_url for citation in citation_audit.citations}
    catalog_urls = {source.normalized_url for source in sources}
    cited_catalog = report_urls & catalog_urls
    mapped_claims = sum(bool(claim.source_ids) for claim in claims)
    duplicate_count = max(
        0,
        citation_audit.citation_count_raw - citation_audit.citation_count_unique,
    )
    report_source_ids = set(_SOURCE_ID_ANYWHERE.findall(report_markdown))
    catalog_source_ids = {source.source_id for source in sources}
    return ProvenanceAudit(
        catalog_source_count=len(catalog_urls),
        catalog_sources_needing_relevance_review=sum(
            source.relevance_status == "needs_review" for source in sources
        ),
        catalog_authoritative_source_count=sum(
            source.source_type
            in {"government", "official_documentation", "academic"}
            for source in sources
        ),
        cited_catalog_source_count=len(cited_catalog),
        cited_catalog_source_rate=(
            len(cited_catalog) / len(catalog_urls) if catalog_urls else None
        ),
        report_unique_url_count=len(report_urls),
        report_citation_count_raw=citation_audit.citation_count_raw,
        report_domain_count=citation_audit.domain_count,
        uncatalogued_url_count=len(report_urls - catalog_urls),
        mapped_claim_count=mapped_claims,
        unmapped_claim_count=len(claims) - mapped_claims,
        unknown_source_id_count=sum(len(claim.unknown_source_ids) for claim in claims),
        mismatched_source_id_count=sum(
            len(claim.mismatched_source_ids) for claim in claims
        ),
        unlinked_source_id_count=sum(
            len(claim.unlinked_source_ids) for claim in claims
        ),
        report_unknown_source_id_count=len(report_source_ids - catalog_source_ids),
        report_duplicate_citation_count=duplicate_count,
        report_duplicate_citation_rate=(
            duplicate_count / citation_audit.citation_count_raw
            if citation_audit.citation_count_raw
            else None
        ),
        report_max_source_citation_share=(
            max(occurrence_counts.values()) / citation_audit.citation_count_raw
            if citation_audit.citation_count_raw and occurrence_counts
            else None
        ),
        final_claim_units=citation_audit.claim_units,
        final_claim_units_with_citations=citation_audit.claim_units_with_citations,
        final_claim_citation_coverage=citation_audit.claim_citation_coverage,
    )


def serialize_records(
    records: list[SourceRecord] | list[ClaimMapping],
) -> list[dict[str, Any]]:
    """Convert frozen provenance records to JSON-safe dictionaries."""
    return [asdict(record) for record in records]


def _escape_markdown_label(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("\n", " ")
    )


def _assess_source_relevance(
    relevance_text: str,
    source_text: str,
) -> tuple[str, tuple[str, ...]]:
    """Flag weak lexical matches for review without deleting search evidence."""
    if not relevance_text.strip():
        return "unassessed", ()
    query_terms = _relevance_terms(relevance_text)
    source_terms = _relevance_terms(source_text)
    overlap = tuple(sorted(query_terms & source_terms))
    return ("likely_relevant" if overlap else "needs_review", overlap)


def rank_search_results(
    search_result: dict[str, Any],
    *,
    relevance_text: str,
    max_results: int = 8,
) -> dict[str, Any]:
    """Rank, deduplicate and diversify search results without mutating input."""
    ranked: list[tuple[int, int, str, dict[str, Any]]] = []
    seen: set[str] = set()
    for index, item in enumerate(search_result.get("results") or []):
        normalized = normalize_url(str(item.get("url") or ""))
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        title = str(item.get("title") or normalized)
        relevance_status, _ = _assess_source_relevance(
            relevance_text,
            f"{title} {normalized}",
        )
        domain = urlsplit(normalized).hostname or ""
        source_type = classify_domain(domain, normalized)
        has_content = bool(str(item.get("content") or item.get("raw_content") or ""))
        score = _source_quality_score(source_type, relevance_status, has_content)
        ranked.append((score, index, domain, dict(item)))

    ranked.sort(key=lambda entry: (-entry[0], entry[1]))
    selected: list[dict[str, Any]] = []
    domain_counts: dict[str, int] = {}
    for _, _, domain, item in ranked:
        source_type = classify_domain(domain, str(item.get("url") or ""))
        domain_limit = 3 if source_type in {
            "government",
            "official_documentation",
            "academic",
        } else 2
        if domain_counts.get(domain, 0) >= domain_limit:
            continue
        selected.append(item)
        domain_counts[domain] = domain_counts.get(domain, 0) + 1
        if len(selected) >= max(1, max_results):
            break

    payload = dict(search_result)
    payload["results"] = selected
    return payload


def _source_quality_score(
    source_type: str,
    relevance_status: str,
    has_content: bool,
) -> int:
    return (
        _SOURCE_TYPE_WEIGHTS.get(source_type, 0)
        + (5 if relevance_status == "likely_relevant" else -4)
        + (1 if has_content else 0)
    )


def _source_type_label(source_type: str) -> str:
    return {
        "government": "政府/公共机构",
        "official_documentation": "官方文档",
        "academic": "学术来源",
        "company_official": "企业官方",
        "news_media": "新闻媒体",
        "community": "社区来源",
        "unknown": "一般网页",
    }.get(source_type, "一般网页")


def _relevance_terms(text: str) -> set[str]:
    expanded = _CAMEL_BOUNDARY.sub(" ", text)
    expanded = re.sub(r"[._+/-]+", " ", expanded)
    lowered = expanded.lower()
    terms = {
        match.group(0).lower()
        for match in _TERM.finditer(lowered)
    }
    if "threadpoolexecutor" in text.lower():
        terms.update({"thread", "pool", "executor", "concurrent", "futures"})
    if "asyncio" in terms:
        terms.update({"async", "event", "loop", "coroutine"})
    return {term for term in terms if term and term not in _GENERIC_TERMS}
