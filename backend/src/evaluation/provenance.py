"""Deterministic source IDs and claim-to-source provenance tracking."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

from evaluation.citations import (
    audit_report,
    is_evidence_claim,
    normalize_url,
    split_claim_units,
)

_SOURCE_TOKEN = re.compile(r"\[(T\d+-S\d+)\](?!\()")
_SOURCE_ID_ANYWHERE = re.compile(r"T\d+-S\d+")


@dataclass(frozen=True)
class SourceRecord:
    """One normalized search source with a stable task-scoped identifier."""

    source_id: str
    task_id: int
    title: str
    url: str
    normalized_url: str
    domain: str


@dataclass(frozen=True)
class ClaimMapping:
    """One summary claim and the source IDs emitted beside it."""

    claim_id: str
    task_id: int
    text: str
    source_ids: tuple[str, ...]
    unknown_source_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProvenanceAudit:
    """Cross-check final-report links against process-time source records."""

    catalog_source_count: int
    cited_catalog_source_count: int
    cited_catalog_source_rate: float | None
    report_unique_url_count: int
    report_citation_count_raw: int
    report_domain_count: int
    uncatalogued_url_count: int
    mapped_claim_count: int
    unmapped_claim_count: int
    unknown_source_id_count: int
    final_claim_units: int
    final_claim_units_with_citations: int
    final_claim_citation_coverage: float | None


def build_source_records(
    search_result: dict[str, Any] | None,
    *,
    task_id: int,
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
        records.append(
            SourceRecord(
                source_id=f"T{task_id}-S{len(records) + 1}",
                task_id=task_id,
                title=title,
                url=raw_url,
                normalized_url=normalized,
                domain=urlsplit(normalized).hostname or "",
            )
        )
    return records


def format_source_catalog(records: list[SourceRecord]) -> str:
    """Render source IDs in a prompt- and user-readable Markdown catalog."""
    return "\n".join(
        f"- [{record.source_id}] [{_escape_markdown_label(record.title)}]"
        f"({record.normalized_url})"
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
    allowed = {source.source_id for source in sources}
    mappings: list[ClaimMapping] = []
    for unit in split_claim_units(summary_markdown):
        if not is_evidence_claim(unit):
            continue
        mentioned = list(dict.fromkeys(_SOURCE_ID_ANYWHERE.findall(unit)))
        known = tuple(source_id for source_id in mentioned if source_id in allowed)
        unknown = tuple(
            source_id for source_id in mentioned if source_id not in allowed
        )
        mappings.append(
            ClaimMapping(
                claim_id=f"T{task_id}-C{len(mappings) + 1}",
                task_id=task_id,
                text=unit,
                source_ids=known,
                unknown_source_ids=unknown,
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
    report_urls = {citation.normalized_url for citation in citation_audit.citations}
    catalog_urls = {source.normalized_url for source in sources}
    cited_catalog = report_urls & catalog_urls
    mapped_claims = sum(bool(claim.source_ids) for claim in claims)
    return ProvenanceAudit(
        catalog_source_count=len(catalog_urls),
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
