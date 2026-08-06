"""Bounded asynchronous URL accessibility checks with SSRF protection."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import httpx

from evaluation.citations import normalize_url

Resolver = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class URLCheckResult:
    """One normalized URL accessibility result."""

    url: str
    final_url: str | None
    status: str
    http_status: int | None = None
    error_type: str | None = None


class URLAccessibilityChecker:
    """Check URLs concurrently without downloading unbounded response bodies."""

    def __init__(
        self,
        *,
        concurrency: int = 8,
        timeout_seconds: float = 10.0,
        retries: int = 1,
        max_redirects: int = 5,
        max_body_bytes: int = 65_536,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        """Configure concurrency, bounds, transport and hostname resolution."""
        self._semaphore = asyncio.Semaphore(max(1, concurrency))
        self._timeout = httpx.Timeout(timeout_seconds)
        self._retries = max(0, retries)
        self._max_redirects = max(0, max_redirects)
        self._max_body_bytes = max(1, max_body_bytes)
        self._transport = transport
        self._resolver = resolver or _reject_non_public_host

    async def check_many(self, urls: Iterable[str]) -> list[URLCheckResult]:
        """Check URLs concurrently while preserving input order."""
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            transport=self._transport,
            headers={"User-Agent": "DeepResearchCitationAudit/1.0"},
        ) as client:
            return await asyncio.gather(
                *(self._guarded_check(client, url) for url in urls)
            )

    async def _guarded_check(
        self, client: httpx.AsyncClient, raw_url: str
    ) -> URLCheckResult:
        async with self._semaphore:
            return await self._check_with_retries(client, raw_url)

    async def _check_with_retries(
        self, client: httpx.AsyncClient, raw_url: str
    ) -> URLCheckResult:
        normalized = normalize_url(raw_url)
        if normalized is None:
            return URLCheckResult(
                raw_url, None, "invalid_url", error_type="invalid_url"
            )
        for attempt in range(self._retries + 1):
            try:
                return await self._check_redirect_chain(client, normalized)
            except httpx.TimeoutException:
                if attempt == self._retries:
                    return URLCheckResult(
                        normalized, None, "timeout", error_type="timeout"
                    )
            except socket.gaierror:
                return URLCheckResult(
                    normalized, None, "dns_error", error_type="dns_error"
                )
            except ssl.SSLError:
                return URLCheckResult(
                    normalized, None, "ssl_error", error_type="ssl_error"
                )
            except httpx.ConnectError as exc:
                cause = exc.__cause__
                status = (
                    "ssl_error" if isinstance(cause, ssl.SSLError) else "server_error"
                )
                return URLCheckResult(
                    normalized,
                    None,
                    status,
                    error_type=type(exc).__name__,
                )
            except (ValueError, UnicodeError) as exc:
                return URLCheckResult(
                    normalized,
                    None,
                    "invalid_url",
                    error_type=type(exc).__name__,
                )
        return URLCheckResult(normalized, None, "server_error", error_type="unknown")

    async def _check_redirect_chain(
        self, client: httpx.AsyncClient, initial_url: str
    ) -> URLCheckResult:
        current = initial_url
        visited: set[str] = set()
        for redirect_index in range(self._max_redirects + 1):
            if current in visited:
                return URLCheckResult(
                    initial_url, current, "server_error", error_type="redirect_loop"
                )
            visited.add(current)
            host = urlsplit(current).hostname
            if not host:
                raise ValueError("URL has no hostname")
            await self._resolver(host)

            response = await client.head(current)
            if response.status_code in {405, 501}:
                response = await self._limited_get(client, current)
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    return URLCheckResult(
                        initial_url,
                        current,
                        "server_error",
                        response.status_code,
                        "redirect_without_location",
                    )
                redirected = normalize_url(urljoin(current, location))
                if redirected is None:
                    return URLCheckResult(
                        initial_url,
                        current,
                        "invalid_url",
                        response.status_code,
                        "invalid_redirect",
                    )
                if redirect_index == self._max_redirects:
                    return URLCheckResult(
                        initial_url,
                        current,
                        "server_error",
                        response.status_code,
                        "too_many_redirects",
                    )
                current = redirected
                continue
            return _classify_response(initial_url, current, response.status_code)
        return URLCheckResult(
            initial_url, current, "server_error", error_type="redirect"
        )

    async def _limited_get(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        async with client.stream(
            "GET", url, headers={"Range": "bytes=0-65535"}
        ) as response:
            received = 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received >= self._max_body_bytes:
                    break
            return response


def _classify_response(original: str, final: str, status_code: int) -> URLCheckResult:
    if 200 <= status_code < 300:
        status = "accessible"
    elif status_code == 401:
        status = "authentication"
    elif status_code == 403:
        status = "forbidden"
    elif status_code == 404:
        status = "not_found"
    elif status_code == 408:
        status = "timeout"
    else:
        status = "server_error"
    return URLCheckResult(original, final, status, status_code)


async def _reject_non_public_host(host: str) -> None:
    """Resolve a hostname and reject every non-public destination address."""
    if host.lower() == "localhost":
        raise ValueError("local address is not auditable")
    try:
        literal = ipaddress.ip_address(host)
        addresses = [literal]
    except ValueError:
        records = await asyncio.to_thread(socket.getaddrinfo, host, None)
        addresses = list({ipaddress.ip_address(record[4][0]) for record in records})
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("non-public address is not auditable")
