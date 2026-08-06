"""Pure citation extraction, normalization, deduplication and coverage metrics."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from evaluation.domains import classify_domain

_MARKDOWN_LINK = re.compile(
    r"(?<!!)\[[^\]]*\]\((https?://[^\s)]+)(?:\s+[\"'][^)]*[\"'])?\)",
    re.IGNORECASE,
)
_BARE_URL = re.compile(r"https?://[^\s<>\]\[{}\"']+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?，。；：！？、）)"
_TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref_src",
}
_INLINE_CITATION = re.compile(
    r"\[[^\]]+\]\(https?://[^)]+\)|https?://[^\s<>]+", re.IGNORECASE
)
_MARKDOWN_DECORATION = re.compile(r"[`*_>#|~]")
_STRUCTURAL_ONLY = re.compile(
    r"^(参考资料|参考文献|来源|结论|总结|目录|注意事项|下一步|references?|sources?)[:：]?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Citation:
    """One unique normalized citation and its first raw representation."""

    raw_url: str
    normalized_url: str
    domain: str
    source_type: str


@dataclass(frozen=True)
class ClaimCoverage:
    """Citation-nearness statistics for evidence-bearing claim units."""

    claim_units: int
    claim_units_with_citations: int
    claim_citation_coverage: float | None


@dataclass(frozen=True)
class CitationAudit:
    """Complete deterministic citation metrics for one Markdown report."""

    citations: tuple[Citation, ...]
    citation_count_raw: int
    citation_count_unique: int
    domain_count: int
    domain_type_counts: dict[str, int]
    claim_units: int
    claim_units_with_citations: int
    claim_citation_coverage: float | None


def normalize_url(raw_url: str) -> str | None:
    """Normalize a public HTTP URL for deterministic citation deduplication."""
    candidate = raw_url.strip().rstrip(_TRAILING_PUNCTUATION)
    try:
        parsed = urlsplit(candidate)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        host = parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
        if ":" in host:
            host = f"[{host}]"
        port = parsed.port
        if port is not None and not (
            (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
        ):
            host = f"{host}:{port}"
        query_items = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.lower().startswith("utm_")
            and key.lower() not in _TRACKING_PARAMETERS
        ]
        return urlunsplit(
            (scheme, host, parsed.path or "/", urlencode(query_items, doseq=True), "")
        )
    except (UnicodeError, ValueError):
        return None


def extract_citations(markdown: str) -> list[Citation]:
    """Extract Markdown, footnote and bare URLs, then deduplicate in source order."""
    candidates = _extract_candidates(markdown)

    citations: list[Citation] = []
    seen: set[str] = set()
    for _, raw_url in candidates:
        normalized = normalize_url(raw_url)
        if normalized is None or normalized in seen:
            continue
        seen.add(normalized)
        domain = urlsplit(normalized).hostname or ""
        citations.append(
            Citation(
                raw_url=raw_url.rstrip(_TRAILING_PUNCTUATION),
                normalized_url=normalized,
                domain=domain,
                source_type=classify_domain(domain, normalized),
            )
        )
    return citations


def audit_report(markdown: str) -> CitationAudit:
    """Produce deterministic extraction, domain and nearby-coverage metrics."""
    candidates = _extract_candidates(markdown)
    raw_count = sum(normalize_url(raw_url) is not None for _, raw_url in candidates)
    citations = extract_citations(markdown)
    coverage = claim_citation_coverage(markdown)
    return CitationAudit(
        citations=tuple(citations),
        citation_count_raw=raw_count,
        citation_count_unique=len(citations),
        domain_count=len({citation.domain for citation in citations}),
        domain_type_counts=dict(Counter(item.source_type for item in citations)),
        claim_units=coverage.claim_units,
        claim_units_with_citations=coverage.claim_units_with_citations,
        claim_citation_coverage=coverage.claim_citation_coverage,
    )


def citation_occurrence_counts(markdown: str) -> dict[str, int]:
    """Count every valid citation occurrence by normalized URL."""
    counts: Counter[str] = Counter()
    for _, raw_url in _extract_candidates(markdown):
        normalized = normalize_url(raw_url)
        if normalized is not None:
            counts[normalized] += 1
    return dict(counts)


def _extract_candidates(markdown: str) -> list[tuple[int, str]]:
    searchable = _mask_code_fences(markdown)
    candidates: list[tuple[int, str]] = []
    markdown_spans: list[tuple[int, int]] = []
    for match in _MARKDOWN_LINK.finditer(searchable):
        candidates.append((match.start(1), match.group(1)))
        markdown_spans.append(match.span(1))
    for match in _BARE_URL.finditer(searchable):
        if any(start <= match.start() < end for start, end in markdown_spans):
            continue
        candidates.append((match.start(), match.group(0)))

    return sorted(candidates, key=lambda item: item[0])


def _mask_code_fences(markdown: str) -> str:
    """Replace fenced code with whitespace while preserving source offsets."""
    masked: list[str] = []
    in_fence = False
    for line in markdown.splitlines(keepends=True):
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            masked.append("".join("\n" if char == "\n" else " " for char in line))
        elif in_fence:
            masked.append("".join("\n" if char == "\n" else " " for char in line))
        else:
            masked.append(line)
    return "".join(masked)


def split_claim_units(markdown: str) -> list[str]:
    """Split report prose and list items into auditable claim-sized units."""
    units: list[str] = []
    paragraph: list[str] = []
    in_code_fence = False

    def flush_paragraph() -> None:
        if paragraph:
            units.append(" ".join(paragraph).strip())
            paragraph.clear()

    for raw_line in markdown.splitlines():
        line = raw_line.strip()
        if line.startswith("```") or line.startswith("~~~"):
            flush_paragraph()
            in_code_fence = not in_code_fence
            continue
        if in_code_fence:
            continue
        if not line:
            flush_paragraph()
            continue
        if line.startswith("#") or re.fullmatch(r"[-:| ]+", line):
            flush_paragraph()
            continue
        list_match = re.match(r"^(?:[-+*]|\d+[.)])\s+(.+)$", line)
        if list_match:
            flush_paragraph()
            units.append(list_match.group(1).strip())
            continue
        paragraph.append(line)
    flush_paragraph()
    return units


def claim_citation_coverage(markdown: str) -> ClaimCoverage:
    """Calculate nearby-citation coverage without claiming semantic support."""
    evidence_units = [
        unit for unit in split_claim_units(markdown) if is_evidence_claim(unit)
    ]
    cited = sum(bool(_INLINE_CITATION.search(unit)) for unit in evidence_units)
    total = len(evidence_units)
    return ClaimCoverage(
        claim_units=total,
        claim_units_with_citations=cited,
        claim_citation_coverage=(cited / total if total else None),
    )


def is_evidence_claim(unit: str) -> bool:
    """Return whether a unit is substantial enough to require external evidence."""
    without_links = _INLINE_CITATION.sub("", unit)
    plain = _MARKDOWN_DECORATION.sub("", without_links).strip(" -:：")
    if not plain or _STRUCTURAL_ONLY.fullmatch(plain):
        return False
    return len(plain) >= 20 and bool(re.search(r"[A-Za-z0-9\u3400-\u9fff]", plain))
