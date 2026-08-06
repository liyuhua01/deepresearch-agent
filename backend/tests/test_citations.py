"""Tests for deterministic citation extraction and claim coverage."""

from __future__ import annotations

from pathlib import Path

from evaluation.citations import (
    audit_report,
    claim_citation_coverage,
    extract_citations,
    normalize_url,
    split_claim_units,
)
from evaluation.domains import classify_domain

FIXTURE = Path(__file__).parent / "fixtures" / "citation_report.md"


def test_url_normalization_removes_only_known_tracking_components() -> None:
    assert (
        normalize_url("HTTPS://Example.COM:443/path?q=kept&utm_source=drop#fragment")
        == "https://example.com/path?q=kept"
    )
    assert normalize_url("https://user:password@example.com/private") is None
    assert normalize_url("ftp://example.com/file") is None


def test_markdown_and_bare_urls_are_extracted_and_deduplicated() -> None:
    report = FIXTURE.read_text(encoding="utf-8")
    citations = extract_citations(report)

    assert [item.normalized_url for item in citations] == [
        "https://docs.python.org/3/library/asyncio.html",
        "https://example.com/threading?ref=guide",
        "https://www.reuters.com/technology/example",
    ]
    assert citations[0].source_type == "official_documentation"
    assert citations[2].source_type == "news_media"


def test_report_audit_distinguishes_raw_unique_domain_and_coverage_counts() -> None:
    report = FIXTURE.read_text(encoding="utf-8")
    audit = audit_report(report)

    assert audit.citation_count_raw == 4
    assert audit.citation_count_unique == 3
    assert audit.domain_count == 3
    assert audit.claim_units == 3
    assert audit.claim_units_with_citations == 2
    assert audit.claim_citation_coverage == 2 / 3


def test_code_fences_and_structural_headings_are_not_claim_units() -> None:
    report = FIXTURE.read_text(encoding="utf-8")
    units = split_claim_units(report)

    assert all("should-not-be-counted" not in unit for unit in units)
    assert (
        claim_citation_coverage("# 标题\n\n## 参考资料").claim_citation_coverage is None
    )


def test_domain_classification_is_explainable() -> None:
    assert classify_domain("www.gov.cn") == "government"
    assert classify_domain("arxiv.org") == "academic"
    assert classify_domain("docs.python.org") == "official_documentation"
    assert classify_domain("reuters.com") == "news_media"
    assert classify_domain("stackoverflow.com") == "community"
    assert classify_domain("deepseek.com") == "company_official"
    assert classify_domain("unclassified.example") == "unknown"
