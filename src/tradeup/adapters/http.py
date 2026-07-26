"""HTTP transport with the policies every market client needs.

Requirements this satisfies, all of them non-negotiable for a system that spends
money on the strength of what it reads:

* explicit timeouts and bounded retries with exponential backoff and jitter
* rate-limit awareness (``Retry-After`` is obeyed, not guessed at)
* a user agent that identifies us honestly
* a response timestamp and a SHA-256 of the raw payload, archived as evidence
* secret redaction on every error path
* typed errors, never bare exceptions escaping into the pipeline
* a fixture transport so no mandatory test touches the network

Randomness and sleeping are injected. Jitter that called ``random`` directly would
make retry behaviour untestable and the demo non-reproducible.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

import httpx

__all__ = [
    "FixtureTransport",
    "HttpResponse",
    "HttpxTransport",
    "RestClient",
    "RetryPolicy",
    "TransportError",
    "redact",
]


class TransportError(Exception):
    """Base class for transport failures. Message is always redacted."""


class TransportTimeout(TransportError):
    """The request exceeded its timeout."""


class TransportUnavailable(TransportError):
    """Connection failed, or the server returned 5xx after retries."""


class TransportRateLimited(TransportError):
    """The venue rate-limited us."""

    def __init__(self, message: str, retry_after_seconds: int | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class TransportAuthError(TransportError):
    """Credentials absent, rejected or expired."""


class TransportSchemaError(TransportError):
    """The payload did not match what we know how to read. Always fail closed."""


def redact(text: str, secrets: Mapping[str, str] | None = None) -> str:
    """Replace known secret values with a placeholder.

    Applied to every message that can reach a log, an exception, or an evidence
    file. A leaked API key in a traceback is a credential compromise.
    """
    if not secrets:
        return text
    result = text
    for label, value in secrets.items():
        if value and value in result:
            result = result.replace(value, f"<redacted:{label}>")
    return result


@dataclass(frozen=True, slots=True)
class HttpResponse:
    """A response plus the provenance we archive alongside parsed data."""

    status_code: int
    url: str
    headers: Mapping[str, str]
    content: bytes
    received_at: datetime
    elapsed_seconds: float

    @property
    def payload_sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    @property
    def retry_after_seconds(self) -> int | None:
        raw = self.headers.get("retry-after") or self.headers.get("Retry-After")
        if raw is None:
            return None
        try:
            return max(int(raw), 0)
        except ValueError:
            return None

    def json(self) -> Any:
        """Parse JSON with ``parse_float=Decimal``.

        Every numeric field in a market payload is a price or a float value. Letting
        the standard decoder turn them into binary floats would defeat the entire
        money discipline at the point of entry.
        """
        try:
            return json.loads(self.content.decode("utf-8"), parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportSchemaError(
                f"response from {self.url} is not valid JSON: {exc}"
            ) from exc


class Transport(Protocol):
    """Minimal async transport surface."""

    async def send(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 15.0,
    ) -> HttpResponse: ...


class HttpxTransport:
    """Real network transport over a shared ``httpx.AsyncClient``."""

    def __init__(self, client: httpx.AsyncClient, *, clock: Callable[[], datetime]) -> None:
        self._client = client
        self._clock = clock

    async def send(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 15.0,
    ) -> HttpResponse:
        started = self._clock()
        try:
            response = await self._client.request(
                method,
                url,
                params=dict(params) if params else None,
                headers=dict(headers) if headers else None,
                timeout=timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise TransportTimeout(f"{method} {url} timed out after {timeout_seconds}s") from exc
        except httpx.HTTPError as exc:
            raise TransportUnavailable(f"{method} {url} failed: {type(exc).__name__}") from exc
        received = self._clock()
        return HttpResponse(
            status_code=response.status_code,
            url=str(response.url),
            headers={k.lower(): v for k, v in response.headers.items()},
            content=response.content,
            received_at=received,
            elapsed_seconds=(received - started).total_seconds(),
        )


class FixtureTransport:
    """Deterministic transport backed by recorded responses.

    Keys are ``"{METHOD} {url}"``. An unregistered request raises rather than
    returning an empty result, so a test cannot accidentally pass by exercising a
    code path that never made the call it claimed to.
    """

    def __init__(
        self,
        responses: Mapping[str, HttpResponse],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._responses = dict(responses)
        self._clock = clock
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def key(method: str, url: str) -> str:
        return f"{method.upper()} {url}"

    def register(self, method: str, url: str, response: HttpResponse) -> None:
        self._responses[self.key(method, url)] = response

    async def send(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 15.0,
    ) -> HttpResponse:
        self.calls.append((method.upper(), url))
        try:
            return self._responses[self.key(method, url)]
        except KeyError:
            raise TransportUnavailable(
                f"no fixture registered for {self.key(method, url)}; "
                f"registered: {sorted(self._responses)}"
            ) from None


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Bounded retry with exponential backoff and jitter."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0
    jitter_ratio: float = 0.25

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("delays cannot be negative")
        if not (0 <= self.jitter_ratio <= 1):
            raise ValueError("jitter_ratio must be within [0, 1]")

    def delay_for(self, attempt: int, random_value: float) -> float:
        """Delay before ``attempt`` (1-based). ``random_value`` is in [0, 1)."""
        raw = min(self.base_delay_seconds * float(2 ** (attempt - 1)), self.max_delay_seconds)
        jitter = raw * self.jitter_ratio * (2.0 * random_value - 1.0)
        return float(max(raw + jitter, 0.0))


@dataclass
class RestClient:
    """Transport wrapper applying retry, rate-limit and redaction policy."""

    transport: Transport
    user_agent: str
    timeout_seconds: float = 15.0
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    secrets: Mapping[str, str] = field(default_factory=dict)
    sleeper: Callable[[float], Awaitable[None]] = asyncio.sleep
    random_fn: Callable[[], float] = random.random

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        """GET with retries. Raises a typed, redacted :class:`TransportError`."""
        merged: dict[str, str] = {"user-agent": self.user_agent, "accept": "application/json"}
        if headers:
            merged.update(headers)

        last_error: TransportError | None = None
        for attempt in range(1, self.retry.max_attempts + 1):
            try:
                response = await self.transport.send(
                    "GET",
                    url,
                    params=params,
                    headers=merged,
                    timeout_seconds=self.timeout_seconds,
                )
            except (TransportTimeout, TransportUnavailable) as exc:
                last_error = type(exc)(redact(str(exc), self.secrets))
            else:
                if response.is_success:
                    return response
                if response.status_code in (401, 403):
                    raise TransportAuthError(
                        redact(f"{url} returned {response.status_code}", self.secrets)
                    )
                if response.status_code == 429:
                    last_error = TransportRateLimited(
                        redact(f"{url} rate limited", self.secrets),
                        response.retry_after_seconds,
                    )
                elif 500 <= response.status_code < 600:
                    last_error = TransportUnavailable(
                        redact(f"{url} returned {response.status_code}", self.secrets)
                    )
                else:
                    raise TransportUnavailable(
                        redact(f"{url} returned {response.status_code}", self.secrets)
                    )

            if attempt < self.retry.max_attempts:
                # Obey the venue's own guidance when it gives us any.
                if isinstance(last_error, TransportRateLimited) and last_error.retry_after_seconds:
                    await self.sleeper(float(last_error.retry_after_seconds))
                else:
                    await self.sleeper(self.retry.delay_for(attempt, self.random_fn()))

        assert last_error is not None  # loop always sets it before exhausting attempts
        raise last_error
