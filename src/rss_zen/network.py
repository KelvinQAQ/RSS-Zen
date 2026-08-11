"""Outbound HTTPS URL validation for untrusted feed sources."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from urllib.parse import SplitResult, urlsplit, urlunsplit

from rss_zen.errors import AppError

Resolver = Callable[[str, int | None], Iterable[str]]


@dataclass(frozen=True)
class FeedUrlPolicy:
    """Permit only globally-routable HTTPS feed endpoints."""

    resolver: Resolver | None = None

    def validate(self, url: str) -> str:
        """Validate and normalize one URL before an outbound request."""
        parsed = urlsplit(url.strip())
        if parsed.scheme != "https" or not parsed.hostname:
            raise AppError("invalid_feed_url", "feed URL must use HTTPS")
        if parsed.username is not None or parsed.password is not None:
            raise AppError("invalid_feed_url", "feed URL must not include URL credentials")
        if parsed.fragment:
            parsed = SplitResult(parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")

        addresses = self._resolve(parsed.hostname, parsed.port)
        if not addresses:
            raise AppError(
                "feed_host_unresolvable", "feed host did not resolve to an address", retryable=True
            )
        for address in addresses:
            try:
                ip = ipaddress.ip_address(address)
            except ValueError as error:
                raise AppError(
                    "feed_host_unresolvable", "feed host returned an invalid address"
                ) from error
            if not ip.is_global:
                raise AppError("feed_private_address", "feed URL resolves to a non-public address")
        return urlunsplit(
            SplitResult(
                parsed.scheme.lower(), parsed.netloc.lower(), parsed.path or "/", parsed.query, ""
            )
        )

    def _resolve(self, host: str, port: int | None) -> tuple[str, ...]:
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            return (host,)

        if self.resolver is not None:
            return tuple(self.resolver(host, port))
        try:
            results = socket.getaddrinfo(host, port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror as error:
            raise AppError(
                "feed_host_unresolvable", "feed host could not be resolved", retryable=True
            ) from error
        return tuple(str(item[4][0]) for item in results)
