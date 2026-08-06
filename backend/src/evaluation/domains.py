"""Explainable source-domain classification for citation audit."""

from __future__ import annotations

from urllib.parse import urlsplit

GOVERNMENT_SUFFIXES = (
    ".gov",
    ".gov.cn",
    ".gov.uk",
    ".gc.ca",
    ".europa.eu",
)
ACADEMIC_DOMAINS = {
    "arxiv.org",
    "doi.org",
    "jstor.org",
    "nature.com",
    "pubmed.ncbi.nlm.nih.gov",
    "sciencedirect.com",
    "science.org",
    "springer.com",
}
OFFICIAL_DOC_DOMAINS = {
    "developer.mozilla.org",
    "docs.github.com",
    "docs.python.org",
    "kubernetes.io",
    "learn.microsoft.com",
    "openai.com",
}
NEWS_DOMAINS = {
    "apnews.com",
    "bbc.com",
    "bbc.co.uk",
    "bloomberg.com",
    "reuters.com",
    "theguardian.com",
}
COMMUNITY_DOMAINS = {
    "dev.to",
    "news.ycombinator.com",
    "quora.com",
    "reddit.com",
    "stackoverflow.com",
    "zhihu.com",
}
COMPANY_DOMAINS = {
    "amazon.com",
    "anthropic.com",
    "apple.com",
    "deepseek.com",
    "github.com",
    "google.com",
    "meta.com",
    "microsoft.com",
}


def registrable_match(domain: str, candidate: str) -> bool:
    """Return whether a domain is the candidate or one of its subdomains."""
    return domain == candidate or domain.endswith(f".{candidate}")


def classify_domain(domain: str, url: str = "") -> str:
    """Classify source type using transparent domain and path rules."""
    normalized = domain.lower().rstrip(".")
    if any(normalized.endswith(suffix) for suffix in GOVERNMENT_SUFFIXES):
        return "government"
    if any(registrable_match(normalized, item) for item in ACADEMIC_DOMAINS):
        return "academic"
    if any(registrable_match(normalized, item) for item in OFFICIAL_DOC_DOMAINS):
        return "official_documentation"
    path = urlsplit(url).path.lower() if url else ""
    if "/docs/" in path or path.startswith("/docs/") or normalized.startswith("docs."):
        return "official_documentation"
    if any(registrable_match(normalized, item) for item in NEWS_DOMAINS):
        return "news_media"
    if any(registrable_match(normalized, item) for item in COMMUNITY_DOMAINS):
        return "community"
    if any(registrable_match(normalized, item) for item in COMPANY_DOMAINS):
        return "company_official"
    return "unknown"
