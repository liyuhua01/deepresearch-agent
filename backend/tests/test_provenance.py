"""Tests for stable source IDs and claim-to-source provenance."""

from __future__ import annotations

from evaluation.provenance import (
    ClaimMapping,
    SourceRecord,
    audit_provenance,
    build_source_records,
    collapse_adjacent_duplicate_citations,
    expand_source_tokens,
    extract_claim_mappings,
    rank_search_results,
    render_numbered_citations,
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
        "- 这个来源编号配上了错误 URL，需要作为不匹配处理 "
        "[T2-S2](https://example.com/wrong)。\n"
        "- 这个来源编号没有附带 URL，需要作为未链接处理 [T2-S2]。\n"
        "- 这个足够长的事实性判断没有引用，也必须作为未映射结论保留下来。"
    )

    claims = extract_claim_mappings(summary, task_id=2, sources=_sources())

    assert [claim.claim_id for claim in claims] == [
        "T2-C1",
        "T2-C2",
        "T2-C3",
        "T2-C4",
        "T2-C5",
    ]
    assert claims[0].source_ids == ("T2-S1",)
    assert claims[1].unknown_source_ids == ("T2-S9",)
    assert claims[2].mismatched_source_ids == ("T2-S2",)
    assert claims[3].unlinked_source_ids == ("T2-S2",)
    assert claims[4].source_ids == ()


def test_source_coverage_prose_is_not_counted_as_an_evidence_claim() -> None:
    summary = (
        "## 任务总结\n\n"
        "> 以下综合 T2-S1～T2-S2 两份来源的证据。\n\n"
        "- asyncio 使用事件循环处理网络等待，适合高并发 I/O "
        "[T2-S1](https://docs.python.org/3/library/asyncio.html)。\n\n"
        "*来源覆盖说明：本总结引用了 T2-S1、T2-S2。*"
        "\n\n- **已忽略**：T2-S2（与主题无关）。"
    )

    claims = extract_claim_mappings(summary, task_id=2, sources=_sources())

    assert len(claims) == 1
    assert claims[0].source_ids == ("T2-S1",)
    assert claims[0].unlinked_source_ids == ()


def test_source_relevance_is_flagged_without_dropping_results() -> None:
    records = build_source_records(
        {
            "results": [
                {
                    "title": "asyncio event loop documentation",
                    "url": "https://docs.python.org/3/library/asyncio.html",
                },
                {
                    "title": "uv package manager quickstart",
                    "url": "https://example.com/uv",
                },
            ]
        },
        task_id=1,
        relevance_text="asyncio and ThreadPoolExecutor for network I/O",
    )

    assert len(records) == 2
    assert records[0].relevance_status == "likely_relevant"
    assert "asyncio" in records[0].relevance_terms
    assert records[1].relevance_status == "needs_review"


def test_relevance_screen_uses_distinctive_chinese_terms_not_generic_api_words() -> (
    None
):
    records = build_source_records(
        {
            "results": [
                {
                    "title": "欧盟人工智能法案的生效时间与提供者义务",
                    "url": "https://example.eu/ai-act-obligations",
                },
                {
                    "title": "Windows Search API reference",
                    "url": "https://learn.microsoft.com/windows/search/api",
                },
            ]
        },
        task_id=4,
        relevance_text="梳理欧盟 AI Act 提供者主要义务和生效时间线",
    )

    assert records[0].relevance_status == "likely_relevant"
    assert records[1].relevance_status == "needs_review"


def test_search_ranking_prefers_authoritative_relevant_and_diverse_sources() -> None:
    ranked = rank_search_results(
        {
            "results": [
                {
                    "title": "asyncio community post",
                    "url": "https://example.com/one",
                    "content": "evidence",
                },
                {
                    "title": "asyncio duplicate domain",
                    "url": "https://example.com/two",
                    "content": "evidence",
                },
                {
                    "title": "asyncio third same domain",
                    "url": "https://example.com/three",
                    "content": "evidence",
                },
                {
                    "title": "asyncio — official documentation",
                    "url": "https://docs.python.org/3/library/asyncio.html",
                    "content": "official evidence",
                },
            ]
        },
        relevance_text="Python asyncio concurrency",
        max_results=8,
    )

    assert ranked["results"][0]["url"] == (
        "https://docs.python.org/3/library/asyncio.html"
    )
    assert len(ranked["results"]) == 3


def test_search_ranking_drops_irrelevant_noise_when_relevant_results_exist() -> None:
    ranked = rank_search_results(
        {
            "results": [
                {
                    "title": "asyncio event loop",
                    "url": "https://docs.python.org/3/library/asyncio.html",
                },
                {
                    "title": "Greek Peak lift tickets",
                    "url": "https://www.greekpeak.net/lift-tickets/",
                },
            ]
        },
        relevance_text="Python asyncio network concurrency",
    )

    assert [item["url"] for item in ranked["results"]] == [
        "https://docs.python.org/3/library/asyncio.html"
    ]


def test_search_ranking_can_reject_an_entire_off_topic_pool() -> None:
    ranked = rank_search_results(
        {
            "results": [
                {
                    "title": "Greek Peak lift tickets",
                    "url": "https://www.greekpeak.net/lift-tickets/",
                }
            ]
        },
        relevance_text="Python asyncio network concurrency",
        require_relevance=True,
    )

    assert ranked["results"] == []


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


def test_source_and_claim_tokens_render_as_stable_clickable_numbers() -> None:
    claims = [
        ClaimMapping("T2-C1", 2, "有两个来源的结论", ("T2-S1", "T2-S2"), ()),
        ClaimMapping("T2-C2", 2, "没有来源的结论", (), ()),
    ]
    report = (
        "直接来源 [T2-S1]；同一来源 "
        "[标题](https://docs.python.org/3/library/asyncio.html)；"
        "结论映射 [T2-C1]；未映射结论 [T2-C2]。"
    )

    rendered = render_numbered_citations(
        report,
        sources=_sources(),
        claims=claims,
    )

    assert rendered.count("[1](https://docs.python.org/3/library/asyncio.html)") == 3
    assert "[2](https://docs.python.org/3/library/threading.html)" in rendered
    assert "未映射结论 [待验证]" in rendered
    assert "T2-C" not in rendered


def test_linked_internal_ids_cannot_substitute_an_uncatalogued_url() -> None:
    claims = [ClaimMapping("T2-C1", 2, "结论", ("T2-S1",), ())]
    report = (
        "来源 [T2-S1](https://evil.example/source)，"
        "结论 [T2-C1](https://evil.example/claim)。"
    )

    rendered = render_numbered_citations(
        report,
        sources=_sources(),
        claims=claims,
    )

    assert "evil.example" not in rendered
    assert rendered.count("[1](https://docs.python.org/3/library/asyncio.html)") == 2


def test_only_adjacent_same_url_citations_are_collapsed() -> None:
    report = (
        "来源："
        "[编号](https://docs.python.org/3/library/asyncio.html) "
        "[标题](https://docs.python.org/3/library/asyncio.html)。\n"
        "另一结论仍可再次引用 "
        "[标题](https://docs.python.org/3/library/asyncio.html)。"
    )

    collapsed = collapse_adjacent_duplicate_citations(report)

    assert collapsed.count("https://docs.python.org/3/library/asyncio.html") == 2
    assert "[编号]" in collapsed
    assert "[标题]" in collapsed


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
    assert audit.report_catalog_url_match_rate == 0.5
    assert audit.uncatalogued_url_count == 1
    assert audit.mapped_claim_count == 1
    assert audit.unmapped_claim_count == 1
    assert audit.report_duplicate_citation_count == 0
    assert audit.report_duplicate_citation_rate == 0.0


def test_final_audit_separates_catalog_existence_from_topic_relevance() -> None:
    sources = [
        SourceRecord(
            **{
                **source.__dict__,
                "relevance_status": (
                    "likely_relevant" if source.source_id == "T2-S1" else "needs_review"
                ),
            }
        )
        for source in _sources()
    ]
    report = (
        "相关来源 [1](https://docs.python.org/3/library/asyncio.html)，"
        "待复核来源 [2](https://docs.python.org/3/library/threading.html)，"
        "目录外来源 [3](https://example.com/outside)。"
    )

    audit = audit_provenance(report, sources=sources, claims=[])

    assert audit.report_catalog_url_match_rate == 2 / 3
    assert audit.report_relevant_cited_source_count == 1
    assert audit.report_cited_source_relevance_rate == 0.5
    assert audit.report_relevant_source_integrity_rate == 1 / 3


def test_final_audit_reports_duplicate_and_concentrated_citations() -> None:
    report = (
        "事实一 [文档](https://docs.python.org/3/library/asyncio.html)。\n"
        "事实二 [文档](https://docs.python.org/3/library/asyncio.html)。\n"
        "事实三 [线程](https://docs.python.org/3/library/threading.html)。"
    )

    audit = audit_provenance(report, sources=_sources(), claims=[])

    assert audit.report_citation_count_raw == 3
    assert audit.report_duplicate_citation_count == 1
    assert audit.report_duplicate_citation_rate == 1 / 3
    assert audit.report_max_source_citation_share == 2 / 3
