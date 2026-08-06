"""Tests for stable source IDs and claim-to-source provenance."""

from __future__ import annotations

from evaluation.provenance import (
    ClaimMapping,
    SourceRecord,
    audit_provenance,
    build_source_records,
    expand_source_tokens,
    extract_claim_mappings,
)


def _sources() -> list[SourceRecord]:
    return [
        SourceRecord(
            source_id="T2-S1",
            task_id=2,
            title="Python asyncio 文档",
            url="https://docs.python.org/3/library/asyncio.html",
            normalized_url="https://docs.python.org/3/library/asyncio.html",
            domain="docs.python.org",
        ),
        SourceRecord(
            source_id="T2-S2",
            task_id=2,
            title="线程文档",
            url="https://docs.python.org/3/library/threading.html",
            normalized_url="https://docs.python.org/3/library/threading.html",
            domain="docs.python.org",
        ),
    ]


def test_source_ids_are_deterministic_and_urls_are_deduplicated() -> None:
    records = build_source_records(
        {
            "results": [
                {
                    "title": "First",
                    "url": "https://example.com/a?utm_source=test",
                },
                {"title": "Duplicate", "url": "https://example.com/a"},
                {"title": "Second", "url": "https://example.org/b"},
                {"title": "Invalid", "url": "not-a-url"},
            ]
        },
        task_id=3,
    )

    assert [record.source_id for record in records] == ["T3-S1", "T3-S2"]
    assert [record.normalized_url for record in records] == [
        "https://example.com/a",
        "https://example.org/b",
    ]


def test_claim_mappings_keep_known_and_unknown_ids_separate() -> None:
    summary = (
        "- asyncio 使用事件循环处理大量网络等待任务，适合高并发 I/O "
        "[T2-S1](https://docs.python.org/3/library/asyncio.html)。\n"
        "- 这个判断引用了不存在的来源编号，需要被检测出来 [T2-S9]。\n"
        "- 这个足够长的事实性判断没有引用，也必须作为未映射结论保留下来。"
    )

    claims = extract_claim_mappings(summary, task_id=2, sources=_sources())

    assert [claim.claim_id for claim in claims] == ["T2-C1", "T2-C2", "T2-C3"]
    assert claims[0].source_ids == ("T2-S1",)
    assert claims[1].unknown_source_ids == ("T2-S9",)
    assert claims[2].source_ids == ()


def test_bare_source_tokens_expand_to_clickable_links_without_touching_unknowns() -> (
    None
):
    report = "事件循环适合网络等待 [T2-S1]；未知结论 [T2-S9]。"

    expanded = expand_source_tokens(report, _sources())

    assert (
        "[Python asyncio 文档](https://docs.python.org/3/library/asyncio.html)"
        in expanded
    )
    assert "[T2-S9]" in expanded


def test_final_audit_distinguishes_catalog_sources_and_uncatalogued_urls() -> None:
    claims = [
        ClaimMapping("T2-C1", 2, "有来源的结论", ("T2-S1",), ()),
        ClaimMapping("T2-C2", 2, "无来源的结论", (), ()),
    ]
    report = (
        "有来源的结论 [官方文档](https://docs.python.org/3/library/asyncio.html)。\n\n"
        "额外链接 [外部页面](https://example.com/uncatalogued)。"
    )

    audit = audit_provenance(report, sources=_sources(), claims=claims)

    assert audit.catalog_source_count == 2
    assert audit.cited_catalog_source_count == 1
    assert audit.cited_catalog_source_rate == 0.5
    assert audit.uncatalogued_url_count == 1
    assert audit.mapped_claim_count == 1
    assert audit.unmapped_claim_count == 1
