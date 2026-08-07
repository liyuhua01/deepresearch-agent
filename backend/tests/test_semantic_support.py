"""Semantic support review extraction and scoring tests."""

from __future__ import annotations

import json

import pytest

from evaluation.semantic_support import (
    SupportReviewItem,
    build_review_items,
    load_review_items,
    merge_independent_reviews,
    sample_review_items,
    score_review_items,
    write_review_template,
)


def test_build_review_items_only_exports_cited_evidence_claims() -> None:
    report = """
# 结论

这是一个足够长、需要证据支持的事实性结论。[来源](https://example.com/a)

这是另一个足够长、但当前没有任何引用支持的事实性结论。
"""
    items = build_review_items(report, run_id="run-1", question_id="Q01")

    assert len(items) == 1
    assert items[0].citation_urls == ("https://example.com/a",)
    assert items[0].item_id.startswith("support-")


def test_score_uses_consensus_and_explicit_decidable_denominator() -> None:
    items = [
        SupportReviewItem(
            item_id="a",
            run_id="r",
            question_id="Q01",
            claim="claim a",
            citation_urls=("https://example.com/a",),
            annotations=(
                {"reviewer": "one", "label": "fully_supported"},
                {"reviewer": "two", "label": "fully_supported"},
            ),
        ),
        SupportReviewItem(
            item_id="b",
            run_id="r",
            question_id="Q01",
            claim="claim b",
            citation_urls=("https://example.com/b",),
            annotations=(
                {"reviewer": "one", "label": "partially_supported"},
                {"reviewer": "two", "label": "partially_supported"},
            ),
        ),
        SupportReviewItem(
            item_id="c",
            run_id="r",
            question_id="Q01",
            claim="claim c",
            citation_urls=("https://example.com/c",),
            annotations=(
                {"reviewer": "one", "label": "inaccessible"},
                {"reviewer": "two", "label": "inaccessible"},
            ),
        ),
    ]

    summary = score_review_items(items)

    assert summary["decidable_item_count"] == 2
    assert summary["strict_semantic_support_rate"] == 0.5
    assert summary["strict_semantic_support_rate_95ci"][0] < 0.5
    assert summary["strict_semantic_support_rate_95ci"][1] > 0.5
    assert summary["lenient_semantic_support_rate"] == 1.0
    assert summary["consensus_rate"] == 1.0


def test_review_file_rejects_invalid_labels(tmp_path) -> None:
    path = tmp_path / "reviews.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "items": [
                    {
                        "item_id": "one",
                        "run_id": "r",
                        "claim": "claim",
                        "citation_urls": ["https://example.com"],
                        "annotations": [{"reviewer": "a", "label": "probably_true"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="invalid support label"):
        load_review_items(path)


def test_template_round_trip(tmp_path) -> None:
    path = tmp_path / "template.json"
    item = SupportReviewItem(
        item_id="one",
        run_id="r",
        question_id=None,
        claim="claim",
        citation_urls=("https://example.com/",),
    )
    write_review_template(path, [item])

    loaded = load_review_items(path)

    assert loaded == [item]


def test_sampling_is_deterministic_and_spans_questions() -> None:
    items = [
        SupportReviewItem(
            item_id=f"{question}-{index}",
            run_id="r",
            question_id=question,
            claim="claim",
            citation_urls=("https://example.com/",),
        )
        for question in ("Q01", "Q02", "Q03")
        for index in range(5)
    ]

    first = sample_review_items(items, sample_size=6)
    second = sample_review_items(reversed(items), sample_size=6)

    assert first == second
    assert {item.question_id for item in first} == {"Q01", "Q02", "Q03"}


def _review_copy(
    reviewer: str,
    label: str,
    *,
    claim: str = "claim",
) -> list[SupportReviewItem]:
    return [
        SupportReviewItem(
            item_id="one",
            run_id="run-1",
            question_id="Q01",
            claim=claim,
            citation_urls=("https://example.com/",),
            annotations=({"reviewer": reviewer, "label": label},),
        )
    ]


def test_merge_independent_reviews_preserves_distinct_labels() -> None:
    merged = merge_independent_reviews(
        [
            _review_copy("reviewer-a", "fully_supported"),
            _review_copy("reviewer-b", "partially_supported"),
        ]
    )

    assert merged[0].annotations == (
        {"reviewer": "reviewer-a", "label": "fully_supported"},
        {"reviewer": "reviewer-b", "label": "partially_supported"},
    )


def test_merge_rejects_changed_claims() -> None:
    with pytest.raises(ValueError, match="changed immutable item"):
        merge_independent_reviews(
            [
                _review_copy("reviewer-a", "fully_supported"),
                _review_copy(
                    "reviewer-b",
                    "fully_supported",
                    claim="silently changed claim",
                ),
            ]
        )


def test_merge_rejects_reused_reviewer_identity() -> None:
    with pytest.raises(ValueError, match="appears more than once"):
        merge_independent_reviews(
            [
                _review_copy("same-reviewer", "fully_supported"),
                _review_copy("same-reviewer", "unsupported"),
            ]
        )
