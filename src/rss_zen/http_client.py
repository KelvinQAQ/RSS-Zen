"""HTTP boundary for conditional feed retrieval."""

from __future__ import annotations

import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from random import Random
from urllib.parse import urljoin

import httpx

from rss_zen.errors import AppError
from rss_zen.network import FeedUrlPolicy

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class FeedHttpClient:
    """Fetch feeds with validated redirects, bounded payloads, and retries."""

    _transient_statuses = {429, 500, 502, 503, 504}

    def __init__(
        self,
        client: httpx.Client,
        *,
        max_attempts: int = 3,
        max_response_bytes: int = 10_000_000,
        max_redirects: int = 5,
        policy: FeedUrlPolicy | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random: Random | None = None,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least one")
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be at least one")
        if max_redirects < 0:
            raise ValueError("max_redirects must not be negative")
        self._client = client
        self._max_attempts = max_attempts
        self._max_response_bytes = max_response_bytes
        self._max_redirects = max_redirects
        self._policy = policy
        self._sleep = sleep
        self._random = random or Random()

    def get_feed(self, url: str, headers: Mapping[str, str]) -> httpx.Response:
        """Get one validated feed, retrying only bounded transient failures."""
        request_headers = dict(headers)
        # Some publishers block default python-httpx user agents; use a browser
        # user agent unless the caller explicitly provides one.
        request_headers.setdefault("User-Agent", _DEFAULT_USER_AGENT)
        initial_url = self._validate_url(url)
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._get_once(initial_url, request_headers)
            except httpx.TimeoutException as error:
                if attempt == self._max_attempts:
                    raise AppError(
                        "feed_timeout", "feed request timed out", retryable=True, cause=error
                    ) from error
                self._sleep(_backoff_seconds(attempt, self._random))
                continue
            except httpx.RequestError as error:
                if attempt == self._max_attempts:
                    raise AppError(
                        "feed_network_error", "feed request failed", retryable=True, cause=error
                    ) from error
                self._sleep(_backoff_seconds(attempt, self._random))
                continue

            if response.status_code in self._transient_statuses:
                response.close()
                if attempt == self._max_attempts:
                    raise AppError(
                        f"feed_http_{response.status_code}",
                        f"feed server returned HTTP {response.status_code}",
                        retryable=True,
                    )
                self._sleep(_retry_after_seconds(response, attempt, self._random))
                continue
            if response.status_code not in {200, 304}:
                response.close()
                raise AppError(
                    f"feed_http_{response.status_code}",
                    f"feed server returned HTTP {response.status_code}",
                )
            return response
        raise RuntimeError("unreachable retry loop")

    def _get_once(self, url: str, headers: Mapping[str, str]) -> httpx.Response:
        current_url = url
        for redirect_count in range(self._max_redirects + 1):
            request = self._client.build_request("GET", current_url, headers=headers)
            response = self._client.send(request, stream=True, follow_redirects=False)
            if response.status_code == 304:
                return self._read_bounded_response(response)
            if response.is_redirect:
                location = response.headers.get("Location")
                response.close()
                if location is None:
                    raise AppError(
                        "feed_redirect_invalid", "feed redirect did not include a Location header"
                    )
                if redirect_count == self._max_redirects:
                    raise AppError("feed_redirect_limit", "feed exceeded the redirect limit")
                current_url = self._validate_url(urljoin(current_url, location))
                continue
            return self._read_bounded_response(response)
        raise RuntimeError("unreachable redirect loop")

    def _read_bounded_response(self, response: httpx.Response) -> httpx.Response:
        content_length = response.headers.get("Content-Length")
        if content_length is not None:
            try:
                if int(content_length) > self._max_response_bytes:
                    raise AppError(
                        "feed_response_too_large",
                        "feed response exceeded the configured size limit",
                    )
            except ValueError:
                pass
        content = bytearray()
        try:
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > self._max_response_bytes:
                    raise AppError(
                        "feed_response_too_large",
                        "feed response exceeded the configured size limit",
                    )
            # ``iter_bytes`` already yields decoded content; drop Content-Encoding so the
            # reconstructed response does not attempt a second (and failing) decode.
            headers = response.headers
            if headers.get("Content-Encoding"):
                headers = headers.copy()
                headers.pop("Content-Encoding", None)
            return httpx.Response(
                response.status_code,
                headers=headers,
                content=bytes(content),
                request=response.request,
                extensions=response.extensions,
            )
        finally:
            response.close()

    def _validate_url(self, url: str) -> str:
        return self._policy.validate(url) if self._policy is not None else url

    def get_feed_curl(
        self, url: str, headers: Mapping[str, str], *, timeout: int = 60
    ) -> httpx.Response:
        """Fetch a feed through the system curl binary.

        Some feed frontends fingerprint the TLS client hello and serve placeholders
        to Python's TLS stack (httpx/OpenSSL) while accepting system curl. Shelling
        out keeps conditional requests, size bounds, and transient retries intact.
        """
        request_headers = dict(headers)
        request_headers.setdefault("User-Agent", _DEFAULT_USER_AGENT)
        initial_url = self._validate_url(url)
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = self._curl_once(initial_url, request_headers, timeout)
            except subprocess.TimeoutExpired as error:
                if attempt == self._max_attempts:
                    raise AppError(
                        "feed_timeout", "feed request timed out", retryable=True, cause=error
                    ) from error
                self._sleep(_backoff_seconds(attempt, self._random))
                continue

            if response.status_code in self._transient_statuses:
                if attempt == self._max_attempts:
                    raise AppError(
                        f"feed_http_{response.status_code}",
                        f"feed server returned HTTP {response.status_code}",
                        retryable=True,
                    )
                self._sleep(_curl_retry_after(response, attempt, self._random))
                continue
            if response.status_code not in {200, 304}:
                raise AppError(
                    f"feed_http_{response.status_code}",
                    f"feed server returned HTTP {response.status_code}",
                )
            return response
        raise RuntimeError("unreachable retry loop")

    def _curl_once(
        self, url: str, headers: Mapping[str, str], timeout: int
    ) -> httpx.Response:
        with (
            tempfile.NamedTemporaryFile() as body_file,
            tempfile.NamedTemporaryFile() as header_file,
        ):
            command = [
                "curl",
                "-sS",
                "-L",
                "--http1.1",
                "--max-time",
                str(timeout),
                "-o",
                body_file.name,
                "-D",
                header_file.name,
                "-w",
                "%{http_code}",
            ]
            for key, value in headers.items():
                command += ["-H", f"{key}: {value}"]
            command.append(url)
            completed = subprocess.run(
                command,
                capture_output=True,
                check=False,
                timeout=timeout + 15,
            )
            body = body_file.read()
            header_bytes = header_file.read()
        try:
            status_code = int(completed.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            status_code = 0
        if len(body) > self._max_response_bytes:
            raise AppError(
                "feed_response_too_large", "feed response exceeded the configured size limit"
            )
        return httpx.Response(
            status_code,
            headers=_parse_curl_headers(header_bytes),
            content=body,
        )

def _backoff_seconds(attempt: int, random: Random) -> float:
    base = min(2 ** (attempt - 1), 30)
    return float(base + random.uniform(0.0, min(base * 0.1, 3.0)))


def _parse_curl_headers(raw: bytes) -> httpx.Headers:
    """Parse the final response header block written by ``curl -D``."""
    blocks = raw.split(b"\n\n")
    headers = httpx.Headers()
    for line in (blocks[-1] if blocks else b"").splitlines():
        if b":" not in line:
            continue
        name, _, value = line.partition(b":")
        name = name.decode("latin-1").strip()
        if not name:
            continue
        headers[name] = value.decode("latin-1").strip()
    return headers


def _curl_retry_after(response: httpx.Response, attempt: int, random: Random) -> float:
    value = response.headers.get("Retry-After")
    if value is not None:
        try:
            return min(max(float(value), 0.0), 300.0)
        except ValueError:
            pass
    return _backoff_seconds(attempt, random)


def _retry_after_seconds(response: httpx.Response, attempt: int, random: Random) -> float:
    value = response.headers.get("Retry-After")
    if value is not None:
        try:
            return min(max(float(value), 0.0), 300.0)
        except ValueError:
            pass
    return _backoff_seconds(attempt, random)
