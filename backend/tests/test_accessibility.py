"""Network-mocked tests for safe URL accessibility checks."""

from __future__ import annotations

import asyncio
import socket

import httpx

from evaluation.accessibility import URLAccessibilityChecker


async def _allow_public_test_host(_host: str) -> None:
    return None


def _transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/redirect":
            return httpx.Response(302, headers={"Location": "/ok"})
        if path == "/head-not-allowed" and request.method == "HEAD":
            return httpx.Response(405)
        if path in {"/ok", "/head-not-allowed"}:
            return httpx.Response(200, content=b"readable")
        if path == "/forbidden":
            return httpx.Response(403)
        if path == "/missing":
            return httpx.Response(404)
        if path == "/timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(503)

    return httpx.MockTransport(handler)


def test_accessible_redirect_forbidden_missing_timeout_and_head_fallback() -> None:
    checker = URLAccessibilityChecker(
        transport=_transport(),
        resolver=_allow_public_test_host,
        retries=1,
    )
    results = asyncio.run(
        checker.check_many(
            [
                "https://example.com/ok",
                "https://example.com/redirect",
                "https://example.com/head-not-allowed",
                "https://example.com/forbidden",
                "https://example.com/missing",
                "https://example.com/timeout",
            ]
        )
    )

    assert [result.status for result in results] == [
        "accessible",
        "accessible",
        "accessible",
        "forbidden",
        "not_found",
        "timeout",
    ]
    assert results[1].final_url == "https://example.com/ok"


def test_dns_error_is_classified_without_an_http_request() -> None:
    async def fail_dns(_host: str) -> None:
        raise socket.gaierror("not found")

    checker = URLAccessibilityChecker(transport=_transport(), resolver=fail_dns)
    result = asyncio.run(checker.check_many(["https://missing.example/page"]))[0]

    assert result.status == "dns_error"


def test_private_and_local_addresses_are_rejected_before_request() -> None:
    checker = URLAccessibilityChecker(transport=_transport())
    results = asyncio.run(
        checker.check_many(
            [
                "http://127.0.0.1/private",
                "http://localhost/private",
                "http://10.0.0.1/private",
                "http://[::1]/private",
            ]
        )
    )

    assert [result.status for result in results] == ["invalid_url"] * 4
