"""HTTP transport layer shared by `Esoul` (sync) and `AsyncEsoul` (async).

Responsibilities, in order of importance:

  1. Inject the `Authorization: Bearer <token>` header on every request.
  2. Generate / pass through the `Idempotency-Key` for writes.
  3. PROACTIVELY refresh sandbox JWTs ~60 s before their `exp` claim,
     transparently to caller code. PAT tokens never refresh.
  4. Retry on network errors and 5xx responses with exponential backoff +
     jitter. Reuse the SAME idempotency key across retry attempts so a
     successful-but-no-response 200 deduplicates cleanly on the second try.
  5. Parse error responses into typed exceptions via
     `exceptions.exception_for_response`.

The module exposes two transport classes — `SyncTransport` and
`AsyncTransport` — sharing as much logic as possible through module-level
helpers. The sync / async asymmetry is mostly about httpx's two client
types and async refresh locking.

--- Why proactive refresh, not reactive ---

Reactive refresh (catch 401, refresh, retry) is simpler but doubles
latency on every expired-token call AND complicates the retry loop
(the retried call uses a different token, breaking the
idempotency-key-reuse contract on the server side: with the new auth
header the server sees a different request-fingerprint and could reject
the retry as a `idempotency_conflict` if the body changed). Proactive
refresh sidesteps this entirely: the request that would have failed
401 instead carries a fresh token from the start.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
import uuid
from typing import Any, Mapping, Optional, Union

import httpx

from ._auth import Credentials, peek_jwt_expiry, write_sandbox_token_file
from ._version import __version__
from .exceptions import (
    APIError,
    AuthError,
    TransportError,
    exception_for_response,
)

logger = logging.getLogger("esoul")


# ─── Tunables ────────────────────────────────────────────────────────────

#: How early to refresh a sandbox JWT before its `exp`. The plan's JWT TTL
#: is 5 minutes (300 s); refreshing at 60 s remaining gives the SDK ample
#: time to retry if the refresh itself flaps.
REFRESH_LEAD_SECONDS = 60

#: Default per-request timeout. httpx applies this as the connect + read
#: + write + pool timeout uniformly. Override per-client via `timeout=`.
DEFAULT_TIMEOUT_SECONDS = 30.0

#: Default retry count for transient failures.
DEFAULT_MAX_RETRIES = 3

#: Backoff base (seconds). Attempt N waits `BACKOFF_BASE * 2^(N-1)` plus
#: jitter in `[0, BACKOFF_BASE/2)`. Capped at `BACKOFF_MAX_SECONDS`.
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_MAX_SECONDS = 30.0

#: User-Agent string. Bumped automatically with each SDK release.
USER_AGENT = f"esoul-python/{__version__}"


# ─── Shared helpers ──────────────────────────────────────────────────────


def _new_idempotency_key() -> str:
    """Mint a fresh UUID-v4 idempotency key.

    UUIDs have 122 bits of randomness — vanishingly unlikely to collide
    across all callers / all keys ever minted. The server's cache is
    further namespaced by `(sessionId, key)` so cross-session collision
    is impossible.
    """
    return str(uuid.uuid4())


def _build_headers(
    token: str,
    *,
    idempotency_key: Optional[str],
    extra: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Compose the standard request headers.

    `idempotency_key` is omitted entirely when None (the case for reads
    like `/api/v1/state` and `/api/v1/describe`). Servers that don't
    require the header silently ignore it; servers that do require it
    (writes) reject without it as `missing_idempotency_key` — so the
    presence/absence is intentional, not best-effort.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    if extra:
        headers.update(extra)
    return headers


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse the `Retry-After` HTTP header.

    Per RFC 7231, the value is either an integer number of seconds OR an
    HTTP-date. We support the integer form (the common case for our
    server); HTTP-date is rare enough to ignore for v1.
    """
    if not value:
        return None
    try:
        seconds = float(value.strip())
        return max(seconds, 0.0)
    except ValueError:
        return None


def _backoff_delay(attempt: int) -> float:
    """Exponential backoff + jitter for retry attempt N (1-indexed).

    Formula: `BACKOFF_BASE * 2^(N-1) + random.uniform(0, BACKOFF_BASE/2)`
    capped at `BACKOFF_MAX_SECONDS`. Jitter prevents thundering herd
    when many SDK instances retry the same outage simultaneously.
    """
    base = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
    jitter = random.uniform(0, BACKOFF_BASE_SECONDS / 2)
    return min(base + jitter, BACKOFF_MAX_SECONDS)


def _should_retry_status(status: int) -> bool:
    """Whether to retry on a given HTTP status code.

    5xx: yes, transient server failure. 429: yes, with Retry-After honoured.
    4xx (other): no, body-shape errors won't change between attempts.
    """
    return status >= 500 or status == 429


def _should_retry_exception(exc: BaseException) -> bool:
    """Whether to retry on a given network-level exception.

    httpx surfaces network failures as a small family: ConnectError /
    ReadTimeout / WriteTimeout / RemoteProtocolError. All are transient
    and worth retrying. `httpx.HTTPStatusError` doesn't surface from our
    code path (we read responses ourselves without `raise_for_status`).
    """
    return isinstance(
        exc,
        (
            httpx.ConnectError,
            httpx.ConnectTimeout,
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.PoolTimeout,
            httpx.RemoteProtocolError,
        ),
    )


def _raise_from_response(response: httpx.Response) -> None:
    """If `response` is non-2xx, parse its body and raise the typed exception.

    Successful responses (2xx) return None — caller continues.

    The server's contract is `{error: {code, message, details?}}` JSON
    for non-2xx. We're tolerant of malformed bodies (proxy 502s,
    HTML 500s, etc.) — fall back to a generic APIError carrying the
    status + raw text.
    """
    if 200 <= response.status_code < 300:
        return
    code = "unknown_error"
    message = f"HTTP {response.status_code}"
    details: Optional[Mapping[str, Any]] = None
    try:
        body = response.json()
    except Exception:  # noqa: BLE001 — tolerant of any JSON failure mode
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        err = body["error"]
        code = str(err.get("code", code))
        message = str(err.get("message", message))
        if isinstance(err.get("details"), dict):
            details = err["details"]
    retry_after = _parse_retry_after(response.headers.get("retry-after"))
    raise exception_for_response(
        code=code,
        status=response.status_code,
        message=message,
        details=details,
        retry_after_seconds=retry_after,
    )


def _refresh_path() -> str:
    """The path the refresh endpoint listens on. Centralised for tests."""
    return "/api/v1/refresh-token"


def _credentials_need_refresh(credentials: Credentials, *, now: float) -> bool:
    """Decide whether to fire a preemptive refresh.

    Only sandbox JWTs are refreshable. Refresh fires when within
    `REFRESH_LEAD_SECONDS` of `exp`, OR if `exp` is unknown (defensive).
    """
    if not credentials.refreshable:
        return False
    if credentials.expires_at_unix is None:
        # Couldn't decode exp — treat as needing refresh on first request.
        # The server will reject if we're wrong; better than skipping
        # entirely and risking an expired-token retry storm.
        return True
    return credentials.expires_at_unix - now <= REFRESH_LEAD_SECONDS


def _apply_refresh_result(
    credentials: Credentials, new_token: str, expires_at_iso: str,
) -> None:
    """Mutate `credentials` with a freshly-refreshed token.

    Also rewrites the sandbox token file when the credentials were loaded
    from one — so a subsequent process boot picks up the new token.
    File-write failures are logged but don't raise (the in-memory
    credential is still good).
    """
    credentials.token = new_token
    credentials.expires_at_unix = peek_jwt_expiry(new_token)
    # Trust the server's reported expiry when we couldn't parse the JWT.
    if credentials.expires_at_unix is None and expires_at_iso:
        try:
            from datetime import datetime

            # The server returns ISO 8601 UTC ("2026-05-15T12:34:56.789Z").
            # Python 3.11+ fromisoformat handles 'Z'; for 3.9-3.10 we
            # strip it.
            iso = expires_at_iso
            if iso.endswith("Z"):
                iso = iso[:-1] + "+00:00"
            credentials.expires_at_unix = datetime.fromisoformat(iso).timestamp()
        except (ValueError, TypeError):
            credentials.expires_at_unix = None
    if credentials.sandbox_token_path is not None:
        try:
            write_sandbox_token_file(credentials.sandbox_token_path, new_token)
        except OSError as err:
            logger.warning(
                "[esoul] failed to rewrite sandbox token file at %s: %s",
                credentials.sandbox_token_path,
                err,
            )


# ─── Sync transport ──────────────────────────────────────────────────────


class SyncTransport:
    """Sync HTTP transport. Wraps an httpx.Client.

    Thread-safe: httpx.Client is itself thread-safe, and the refresh path
    is guarded by a threading.Lock to prevent thundering-herd refresh
    when many threads share one client.
    """

    def __init__(
        self,
        credentials: Credentials,
        *,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: Optional[httpx.Client] = None,
    ):
        self._credentials = credentials
        self._base_url = base_url.rstrip("/")
        self._max_retries = max(1, max_retries)
        self._owns_client = http_client is None
        # httpx.Client carries its own connection pool. Sharing one across
        # all calls amortises TLS handshake — sub-10ms LAN per call.
        self._client = http_client or httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            http2=False,  # http/2 multiplexing pairs poorly with single-call retries
        )
        self._refresh_lock = threading.Lock()
        # Cached session workspace ids — populated on first call to
        # `resolve_workspace_id(None)` via /describe. Avoids one extra
        # HTTP round-trip per resource call.
        self._workspace_ids_cache: Optional[list[str]] = None

    @property
    def credentials(self) -> Credentials:
        return self._credentials

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> SyncTransport:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ─── Workspace resolution ────────────────────────────────────────────

    def resolve_workspace_id(self, explicit: Optional[str]) -> str:
        """Return `explicit` when set; otherwise auto-resolve from session.

        Lazily fetches `/api/v1/describe` once per transport lifetime to
        learn the session's `workspaceIds`. When the session has exactly
        ONE workspace, it becomes the implicit default. Multiple → raise
        with the list (caller must disambiguate). Zero → raise (broken
        session).
        """
        if explicit:
            return explicit
        ids = self._fetch_workspace_ids()
        if len(ids) == 1:
            return ids[0]
        if len(ids) == 0:
            from .exceptions import EsoulError
            raise EsoulError(
                "Session has no accessible workspaces. Re-create the PAT "
                "with explicit workspace scope, or pass workspace_id=... ."
            )
        from .exceptions import EsoulError
        raise EsoulError(
            f"Session has access to {len(ids)} workspaces; pass "
            f"workspace_id=... explicitly. Available: {ids}"
        )

    def _fetch_workspace_ids(self) -> list[str]:
        if self._workspace_ids_cache is not None:
            return self._workspace_ids_cache
        resp = self.request("GET", "/api/v1/describe")
        body = resp.json()
        ids = list(body.get("session", {}).get("workspaceIds", []) or [])
        self._workspace_ids_cache = ids
        return ids

    # ─── Public surface ──────────────────────────────────────────────────

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Any] = None,
        params: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> httpx.Response:
        """Send a request, handling refresh + retries + error mapping.

        Idempotency: pass an explicit `idempotency_key` string for writes.
        Resource methods auto-generate one per call (UUID v4) before
        calling here, so transport never sees `None` for writes. Reads
        pass `None` and the header is omitted.

        `params` is forwarded to httpx as a properly URL-encoded query
        string — preferred over manual string interpolation into `path`
        because httpx handles escaping correctly.

        Returns the raw httpx.Response on 2xx for callers that need to
        read non-JSON bodies (Drive read returns binary). Callers that
        want JSON should call `.json()` themselves.

        Raises:
            APIError + subclasses on non-2xx server responses.
            TransportError on retry-exhausted network failures.
            KeyboardInterrupt / SystemExit / asyncio.CancelledError
                propagate — we never swallow process-control signals.
        """
        self._maybe_refresh()

        last_exc: Optional[BaseException] = None
        for attempt in range(1, self._max_retries + 1):
            try:
                headers = _build_headers(
                    self._credentials.token,
                    idempotency_key=idempotency_key,
                    extra=extra_headers,
                )
                response = self._client.request(
                    method, path, json=json_body, params=params, headers=headers,
                )
            except Exception as exc:  # noqa: BLE001 — KeyboardInterrupt + CancelledError are BaseException, propagate
                last_exc = exc
                if not _should_retry_exception(exc) or attempt == self._max_retries:
                    raise TransportError(
                        f"Network request to {path} failed: {exc}",
                        original=exc,
                    ) from exc
                time.sleep(_backoff_delay(attempt))
                continue

            # Successful response OR a 4xx we won't retry — let
            # `_raise_from_response` decide. For retryable statuses
            # (5xx / 429), look at Retry-After and sleep before retrying.
            if 200 <= response.status_code < 300:
                return response
            if _should_retry_status(response.status_code) and attempt < self._max_retries:
                retry_after = _parse_retry_after(
                    response.headers.get("retry-after"),
                )
                delay = retry_after if retry_after is not None else _backoff_delay(attempt)
                time.sleep(min(delay, BACKOFF_MAX_SECONDS))
                continue
            _raise_from_response(response)
        # Unreachable — the loop either returns, raises, or continues.
        # The explicit raise here makes mypy happy.
        if last_exc is not None:
            raise TransportError(
                f"Retries exhausted for {path}",
                original=last_exc,
            ) from last_exc
        raise TransportError(f"Retries exhausted for {path}")  # pragma: no cover

    # ─── Refresh ─────────────────────────────────────────────────────────

    def _maybe_refresh(self) -> None:
        """Refresh the sandbox JWT if within `REFRESH_LEAD_SECONDS` of expiry.

        Concurrency: serialised via `_refresh_lock`. Many threads calling
        `request` near-simultaneously each see "needs refresh" before the
        first finishes; the lock + re-check inside the lock collapses
        that to one network round-trip.
        """
        now = time.time()
        if not _credentials_need_refresh(self._credentials, now=now):
            return
        with self._refresh_lock:
            # Re-check inside the lock: another thread may have refreshed
            # while we waited.
            if not _credentials_need_refresh(self._credentials, now=time.time()):
                return
            try:
                self._do_refresh()
            except APIError as err:
                # If the OLD token is already past `exp`, surface the
                # auth failure now — the upcoming request would fail
                # anyway. If still within window, log and proceed; the
                # cached refresh attempt failed but the old token will
                # work for at least a few more seconds.
                if (
                    self._credentials.expires_at_unix is not None
                    and self._credentials.expires_at_unix - time.time() <= 0
                ):
                    raise AuthError(
                        "Refresh failed and the previous token has expired. "
                        "The sandbox / session must be reprovisioned.",
                        code=err.code,
                        status=err.status,
                        details=err.details,
                    ) from err
                logger.warning(
                    "[esoul] preemptive refresh failed (%s); using old token: %s",
                    err.code, err.message,
                )

    def _do_refresh(self) -> None:
        """Single refresh round-trip. Updates `self._credentials` on success.

        Does NOT route through the retry loop — refresh is a special call,
        we want failures to be visible immediately rather than retried
        silently. The caller (`_maybe_refresh`) decides what to do with
        the failure.
        """
        headers = {
            "Authorization": f"Bearer {self._credentials.token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        response = self._client.post(_refresh_path(), headers=headers)
        if response.status_code != 200:
            _raise_from_response(response)
        body = response.json()
        new_token = body.get("token")
        expires_at_iso = body.get("expiresAt", "")
        if not isinstance(new_token, str) or not new_token:
            raise APIError(
                "Refresh response missing `token` field",
                code="malformed_refresh_response",
                status=response.status_code,
                details={"body": body},
            )
        _apply_refresh_result(self._credentials, new_token, expires_at_iso)
        logger.debug("[esoul] sandbox JWT refreshed; new exp=%s", expires_at_iso)


# ─── Async transport ─────────────────────────────────────────────────────


class AsyncTransport:
    """Async HTTP transport. Mirrors `SyncTransport`'s API with awaitables.

    Concurrency: refresh path serialised via `asyncio.Lock`. Many tasks
    awaiting `request` near-simultaneously each see "needs refresh"; the
    lock + re-check collapses to one network round-trip.
    """

    def __init__(
        self,
        credentials: Credentials,
        *,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        self._credentials = credentials
        self._base_url = base_url.rstrip("/")
        self._max_retries = max(1, max_retries)
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            http2=False,
        )
        # Lazy-init: asyncio.Lock() pre-Python-3.10 binds to the current
        # event loop at construction. Since `AsyncEsoul()` is constructed
        # synchronously (often outside a running loop), we defer creating
        # the lock until the first request actually awaits it.
        self._refresh_lock: Optional[asyncio.Lock] = None
        self._workspace_ids_cache: Optional[list[str]] = None

    async def resolve_workspace_id(self, explicit: Optional[str]) -> str:
        if explicit:
            return explicit
        ids = await self._fetch_workspace_ids()
        if len(ids) == 1:
            return ids[0]
        if len(ids) == 0:
            from .exceptions import EsoulError
            raise EsoulError(
                "Session has no accessible workspaces. Re-create the PAT "
                "with explicit workspace scope, or pass workspace_id=... ."
            )
        from .exceptions import EsoulError
        raise EsoulError(
            f"Session has access to {len(ids)} workspaces; pass "
            f"workspace_id=... explicitly. Available: {ids}"
        )

    async def _fetch_workspace_ids(self) -> list[str]:
        if self._workspace_ids_cache is not None:
            return self._workspace_ids_cache
        resp = await self.request("GET", "/api/v1/describe")
        body = resp.json()
        ids = list(body.get("session", {}).get("workspaceIds", []) or [])
        self._workspace_ids_cache = ids
        return ids

    def _get_refresh_lock(self) -> asyncio.Lock:
        """Construct the asyncio.Lock on demand, inside a running loop.

        Safe under concurrent callers: in Python <3.10, the first task
        to reach here gets there before any other (single-threaded
        event loop), so the lock is created exactly once.
        """
        if self._refresh_lock is None:
            self._refresh_lock = asyncio.Lock()
        return self._refresh_lock

    @property
    def credentials(self) -> Credentials:
        return self._credentials

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> AsyncTransport:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Optional[Any] = None,
        params: Optional[Mapping[str, Any]] = None,
        idempotency_key: Optional[str] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> httpx.Response:
        await self._maybe_refresh()

        last_exc: Optional[BaseException] = None
        for attempt in range(1, self._max_retries + 1):
            try:
                headers = _build_headers(
                    self._credentials.token,
                    idempotency_key=idempotency_key,
                    extra=extra_headers,
                )
                response = await self._client.request(
                    method, path, json=json_body, params=params, headers=headers,
                )
            except Exception as exc:  # noqa: BLE001 — CancelledError + KeyboardInterrupt propagate
                last_exc = exc
                if not _should_retry_exception(exc) or attempt == self._max_retries:
                    raise TransportError(
                        f"Network request to {path} failed: {exc}",
                        original=exc,
                    ) from exc
                await asyncio.sleep(_backoff_delay(attempt))
                continue

            if 200 <= response.status_code < 300:
                return response
            if _should_retry_status(response.status_code) and attempt < self._max_retries:
                retry_after = _parse_retry_after(
                    response.headers.get("retry-after"),
                )
                delay = retry_after if retry_after is not None else _backoff_delay(attempt)
                await asyncio.sleep(min(delay, BACKOFF_MAX_SECONDS))
                continue
            _raise_from_response(response)
        if last_exc is not None:
            raise TransportError(
                f"Retries exhausted for {path}",
                original=last_exc,
            ) from last_exc
        raise TransportError(f"Retries exhausted for {path}")  # pragma: no cover

    async def _maybe_refresh(self) -> None:
        now = time.time()
        if not _credentials_need_refresh(self._credentials, now=now):
            return
        async with self._get_refresh_lock():
            if not _credentials_need_refresh(self._credentials, now=time.time()):
                return
            try:
                await self._do_refresh()
            except APIError as err:
                if (
                    self._credentials.expires_at_unix is not None
                    and self._credentials.expires_at_unix - time.time() <= 0
                ):
                    raise AuthError(
                        "Refresh failed and the previous token has expired. "
                        "The sandbox / session must be reprovisioned.",
                        code=err.code,
                        status=err.status,
                        details=err.details,
                    ) from err
                logger.warning(
                    "[esoul] preemptive refresh failed (%s); using old token: %s",
                    err.code, err.message,
                )

    async def _do_refresh(self) -> None:
        headers = {
            "Authorization": f"Bearer {self._credentials.token}",
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
        }
        response = await self._client.post(_refresh_path(), headers=headers)
        if response.status_code != 200:
            _raise_from_response(response)
        body = response.json()
        new_token = body.get("token")
        expires_at_iso = body.get("expiresAt", "")
        if not isinstance(new_token, str) or not new_token:
            raise APIError(
                "Refresh response missing `token` field",
                code="malformed_refresh_response",
                status=response.status_code,
                details={"body": body},
            )
        _apply_refresh_result(self._credentials, new_token, expires_at_iso)
        logger.debug("[esoul] sandbox JWT refreshed; new exp=%s", expires_at_iso)


# Type alias used by resource classes to accept either transport.
TransportT = Union[SyncTransport, AsyncTransport]

__all__ = [
    "SyncTransport",
    "AsyncTransport",
    "TransportT",
    "REFRESH_LEAD_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DEFAULT_MAX_RETRIES",
    "USER_AGENT",
]
