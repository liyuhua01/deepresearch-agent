"""Human-auditable semantic support datasets and aggregate metrics."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Literal

from evaluation.citations import extract_citations, is_evidence_claim, split_claim_units

SupportLabel = Literal[
    "fully_supported",
    "partially_supported",
    "unsupported",
    "inaccessible",
    "insufficient_context",
]
VALID_LABELS: set[str] = {
    "fully_supported",
    "partially_supported",
    "unsupported",
    "inaccessible",
    "insufficient_context",
}
DECIDABLE_LABELS = {"fully_supported", "partially_supported", "unsupported"}


@dataclass(frozen=True)
class SupportReviewItem:
    """One evidence-bearing report claim and its adjacent citations."""

    item_id: str
    run_id: str
    question_id: str | None
    claim: str
    citation_urls: tuple[str, ...]
    annotations: tuple[dict[str, str], ...] = ()


def build_review_items(
    report_markdown: str,
    *,
    run_id: str,
    question_id: str | None = None,
) -> list[SupportReviewItem]:
    """Extract stable, cited claim units for independent human review."""
    items: list[SupportReviewItem] = []
    for unit in split_claim_units(report_markdown):
        if not is_evidence_claim(unit):
            continue
        citations = tuple(item.normalized_url for item in extract_citations(unit))
        if not citations:
            continue
        digest = hashlib.sha256(
            f"{run_id}\n{unit}\n{'|'.join(citations)}".encode()
        ).hexdigest()[:16]
        items.append(
            SupportReviewItem(
                item_id=f"support-{digest}",
                run_id=run_id,
                question_id=question_id,
                claim=unit,
                citation_urls=citations,
            )
        )
    return items


def load_review_items(path: Path) -> list[SupportReviewItem]:
    """Load and strictly validate an annotated support-review dataset."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "1.0":
        raise ValueError("unsupported semantic-support schema")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise ValueError("semantic-support items must be a list")

    items: list[SupportReviewItem] = []
    seen_ids: set[str] = set()
    for raw in raw_items:
        item_id = str(raw.get("item_id") or "").strip()
        if not item_id or item_id in seen_ids:
            raise ValueError(f"duplicate or empty support item id: {item_id!r}")
        seen_ids.add(item_id)
        annotations = tuple(raw.get("annotations") or ())
        reviewers: set[str] = set()
        for annotation in annotations:
            reviewer = str(annotation.get("reviewer") or "").strip()
            label = str(annotation.get("label") or "").strip()
            if not reviewer or reviewer in reviewers:
                raise ValueError(f"invalid reviewer for {item_id}")
            if label not in VALID_LABELS:
                raise ValueError(f"invalid support label for {item_id}: {label!r}")
            reviewers.add(reviewer)
        items.append(
            SupportReviewItem(
                item_id=item_id,
                run_id=str(raw.get("run_id") or ""),
                question_id=raw.get("question_id"),
                claim=str(raw.get("claim") or ""),
                citation_urls=tuple(raw.get("citation_urls") or ()),
                annotations=annotations,
            )
        )
    return items


def score_review_items(
    items: Iterable[SupportReviewItem],
    *,
    minimum_reviewers: int = 2,
) -> dict[str, Any]:
    """Compute consensus semantic-support rates with explicit denominators."""
    all_items = list(items)
    consensus_labels: list[str] = []
    reviewed = 0
    disagreements = 0
    for item in all_items:
        labels = [str(annotation["label"]) for annotation in item.annotations]
        if len(labels) < minimum_reviewers:
            continue
        reviewed += 1
        counts = Counter(labels)
        label, count = counts.most_common(1)[0]
        if list(counts.values()).count(count) > 1:
            disagreements += 1
            continue
        consensus_labels.append(label)

    counts = Counter(consensus_labels)
    decidable = sum(counts[label] for label in DECIDABLE_LABELS)
    fully = counts["fully_supported"]
    partial = counts["partially_supported"]
    strict_rate = fully / decidable if decidable else None
    lenient_rate = (fully + partial) / decidable if decidable else None
    return {
        "schema_version": "1.0",
        "item_count": len(all_items),
        "reviewed_item_count": reviewed,
        "consensus_item_count": len(consensus_labels),
        "disagreement_count": disagreements,
        "minimum_reviewers": minimum_reviewers,
        "label_counts": dict(sorted(counts.items())),
        "decidable_item_count": decidable,
        "strict_semantic_support_rate": strict_rate,
        "strict_semantic_support_rate_95ci": _wilson_interval(fully, decidable),
        "lenient_semantic_support_rate": lenient_rate,
        "lenient_semantic_support_rate_95ci": _wilson_interval(
            fully + partial, decidable
        ),
        "review_completion_rate": reviewed / len(all_items) if all_items else None,
        "consensus_rate": len(consensus_labels) / reviewed if reviewed else None,
    }


def merge_independent_reviews(
    review_sets: Iterable[Iterable[SupportReviewItem]],
    *,
    require_complete: bool = True,
) -> list[SupportReviewItem]:
    """Merge independently annotated copies after strict identity checks."""
    datasets = [list(items) for items in review_sets]
    if len(datasets) < 2:
        raise ValueError("at least two independent review files are required")
    if not datasets[0]:
        raise ValueError("semantic-support review set must not be empty")

    authority = {item.item_id: item for item in datasets[0]}
    if len(authority) != len(datasets[0]):
        raise ValueError("duplicate support item ids in review authority")

    reviewer_files: dict[str, int] = {}
    merged_annotations: dict[str, dict[str, dict[str, str]]] = {
        item_id: {} for item_id in authority
    }
    for file_index, dataset in enumerate(datasets, start=1):
        indexed = {item.item_id: item for item in dataset}
        if len(indexed) != len(dataset) or set(indexed) != set(authority):
            raise ValueError(
                f"review file {file_index} does not match the authority item ids"
            )

        file_reviewers: set[str] = set()
        for item_id, expected in authority.items():
            observed = indexed[item_id]
            if (
                observed.run_id != expected.run_id
                or observed.question_id != expected.question_id
                or observed.claim != expected.claim
                or observed.citation_urls != expected.citation_urls
            ):
                raise ValueError(
                    f"review file {file_index} changed immutable item {item_id}"
                )
            if require_complete and len(observed.annotations) != 1:
                raise ValueError(
                    f"review file {file_index} must contain exactly one annotation "
                    f"for {item_id}"
                )
            if len(observed.annotations) > 1:
                raise ValueError(
                    f"review file {file_index} contains multiple annotations for "
                    f"{item_id}"
                )
            for annotation in observed.annotations:
                reviewer = str(annotation["reviewer"])
                file_reviewers.add(reviewer)
                if reviewer in merged_annotations[item_id]:
                    raise ValueError(
                        f"reviewer {reviewer!r} appears more than once for {item_id}"
                    )
                merged_annotations[item_id][reviewer] = dict(annotation)

        if not file_reviewers:
            if require_complete:
                raise ValueError(f"review file {file_index} has no reviewer")
            continue
        if len(file_reviewers) != 1:
            raise ValueError(
                f"review file {file_index} must belong to exactly one reviewer"
            )
        reviewer = next(iter(file_reviewers))
        if reviewer in reviewer_files:
            raise ValueError(
                f"reviewer {reviewer!r} is reused in review files "
                f"{reviewer_files[reviewer]} and {file_index}"
            )
        reviewer_files[reviewer] = file_index

    if len(reviewer_files) < 2:
        raise ValueError("at least two distinct reviewers are required")

    return [
        SupportReviewItem(
            item_id=item.item_id,
            run_id=item.run_id,
            question_id=item.question_id,
            claim=item.claim,
            citation_urls=item.citation_urls,
            annotations=tuple(
                merged_annotations[item.item_id][reviewer]
                for reviewer in sorted(merged_annotations[item.item_id])
            ),
        )
        for item in datasets[0]
    ]


def sample_review_items(
    items: Iterable[SupportReviewItem],
    *,
    sample_size: int,
) -> list[SupportReviewItem]:
    """Select a deterministic round-robin sample across benchmark questions."""
    all_items = list(items)
    if sample_size <= 0:
        raise ValueError("sample_size must be positive")
    if sample_size >= len(all_items):
        return all_items

    groups: dict[str, list[SupportReviewItem]] = {}
    for item in all_items:
        key = item.question_id or "unassigned"
        groups.setdefault(key, []).append(item)
    for group in groups.values():
        group.sort(key=lambda item: item.item_id)

    sampled: list[SupportReviewItem] = []
    ordered_keys = sorted(groups)
    while len(sampled) < sample_size:
        progressed = False
        for key in ordered_keys:
            group = groups[key]
            if group and len(sampled) < sample_size:
                sampled.append(group.pop(0))
                progressed = True
        if not progressed:
            break
    return sampled


def _wilson_interval(successes: int, total: int) -> list[float] | None:
    """Return a 95% Wilson score interval for one binomial rate."""
    if total <= 0:
        return None
    z = 1.959963984540054
    rate = successes / total
    denominator = 1 + (z * z / total)
    centre = (rate + (z * z / (2 * total))) / denominator
    margin = (
        z
        * math.sqrt((rate * (1 - rate) / total) + (z * z / (4 * total * total)))
        / denominator
    )
    return [max(0.0, centre - margin), min(1.0, centre + margin)]


def write_review_template(path: Path, items: Iterable[SupportReviewItem]) -> None:
    """Write a stable JSON template without inventing semantic labels."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "label_guide": {
            "fully_supported": "引用内容直接支持结论的全部关键事实",
            "partially_supported": "引用仅支持结论的一部分或需要弱推断",
            "unsupported": "引用可访问但不支持该结论",
            "inaccessible": "引用无法取得，不能判断",
            "insufficient_context": "现有摘录不足以完成判断",
        },
        "items": [asdict(item) for item in items],
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
