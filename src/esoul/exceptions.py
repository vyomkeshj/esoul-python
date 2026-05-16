"""Typed exception hierarchy for the esoul SDK.

Every error the SDK surfaces is an `EsoulError` subclass. The hierarchy
splits into three regions:

  EsoulError                          — root; never raised directly
  ├── MissingCredentialsError         — local; raised by `_auth.py` when no
  │                                     credential source resolves
  ├── TransportError                  — network failure AFTER retries
  │                                     exhausted (DNS, connect timeout,
  │                                     read timeout, etc.)
  └── APIError                        — server replied with a non-2xx; the
      │                                 SDK's `_transport.py` maps the
      │                                 response's `error.code` to a subclass
      ├── AuthError                   — 401s (missing_auth, invalid_session,
      │                                 invalid_signature, session_revoked,
      │                                 session_expired, invalid_token_format,
      │                                 unauthenticated, not_owner)
      ├── WorkspaceAccessDenied       — 403 workspace_access_denied
      │                                 + workspace_forbidden
      ├── NotFoundError               — 404 app_not_found, file_not_found,
      │                                 event_not_registered, not_found,
      │                                 folder_not_found
      ├── InvalidRequest              — 400 invalid_request,
      │                                 missing_idempotency_key, is_folder
      ├── IdempotencyConflict         — 409 idempotency_conflict
      ├── RateLimitError              — 429
      ├── DriveNotConnected           — 424 drive_not_connected
      │                                 + drive_scope_missing
      └── DriveError                  — 502 drive_http, drive_failure

Catching `APIError` matches every server-reported failure. Catching
`EsoulError` also catches local credential + transport errors. Catching a
specific subclass (`WorkspaceAccessDenied`, `RateLimitError`, etc.) is the
recommended pattern when you have an action to take on that specific case.

The base `APIError` carries `code`, `status`, `message`, and `details` (the
full server `error.details` dict). Subclasses may surface specific fields
as attributes for ergonomics — e.g. `RateLimitError.retry_after_seconds`.

--- A note on stability ---

The mapping from server `error.code` → exception class is part of the
SDK's public API. Adding NEW codes (additive) is non-breaking; renaming
or removing existing codes IS breaking and bumps the SDK's major version.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional


class EsoulError(Exception):
    """Root of the SDK's exception hierarchy. Never raised directly."""


class MissingCredentialsError(EsoulError):
    """Raised at client construction time when no credential source is
    configured.

    Resolution order (see `_auth.resolve_credentials`):
      1. explicit `token=` kwarg
      2. `ESOUL_TOKEN` env var
      3. `/var/run/esoul/token` (sandbox)
      4. `~/.config/esoul/credentials` (PAT file)

    Once raised, the SDK refuses to make any requests — there is no
    fallback to anonymous access. Fix by exporting `ESOUL_TOKEN` or
    writing a credentials file.
    """


class TransportError(EsoulError):
    """A network-level failure that survived the SDK's retry policy.

    `original` is the underlying httpx exception so callers can drill into
    the specific cause (connect timeout vs. DNS vs. TLS handshake).
    """

    def __init__(self, message: str, *, original: Optional[BaseException] = None):
        super().__init__(message)
        self.original = original


class APIError(EsoulError):
    """Base for any error reported by the server with a structured
    `{error: {code, message, details?}}` body.

    Subclasses cover the codes the SDK knows about; this base catches
    every other server-reported failure.

    Attributes:
        code:    the server's `error.code` string (e.g. "invalid_request",
                 "dispatch_failed", "internal_error")
        status:  HTTP status code (200 wouldn't reach here; usually 4xx/5xx)
        message: human-readable explanation from the server
        details: structured per-code data; shape varies. May be None.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: int,
        details: Optional[Mapping[str, Any]] = None,
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.message = message
        self.details: Mapping[str, Any] = dict(details) if details else {}

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return (
            f"{self.__class__.__name__}("
            f"code={self.code!r}, status={self.status}, "
            f"message={self.message!r}, details={dict(self.details)!r})"
        )


# ─── Auth-family ─────────────────────────────────────────────────────────


class AuthError(APIError):
    """The credential failed to authenticate.

    Covers every 401 the server might return:
      - missing_auth         (Authorization header absent / malformed)
      - invalid_token_format (token didn't match jwt or PAT shape)
      - invalid_session      (sessionId references nothing)
      - invalid_signature    (HMAC mismatch — token tampered or wrong secret)
      - session_revoked      (the AccessSession row has revokedAt set)
      - session_expired      (the AccessSession row's expiresAt is past)
      - unauthenticated      (PAT routes — no Kinde session)
      - not_owner            (PAT routes — trying to act on someone else's)

    After this exception, the token is permanently dead — get a new one.
    The SDK does NOT auto-recover. (For sandbox JWTs the refresh path is
    automatic BEFORE this point; if you see this, the refresh window
    already passed.)
    """


# ─── Authorisation-family ────────────────────────────────────────────────


class WorkspaceAccessDenied(APIError):
    """The credential authenticated, but doesn't have access to the
    requested workspace.

    Covers:
      - workspace_access_denied (403 from dispatch routes)
      - workspace_forbidden     (403 from PAT/create when granting access
                                 to a workspace the caller doesn't own)
    """


# ─── 404 family ──────────────────────────────────────────────────────────


class NotFoundError(APIError):
    """The requested entity doesn't exist (or doesn't exist within the
    accessible workspaces).

    Covers:
      - app_not_found
      - file_not_found
      - folder_not_found        (Drive path resolution)
      - event_not_registered    (eventName isn't in the app's registry entry)
      - not_found               (PAT routes — sessionId doesn't resolve)
    """


# ─── 400 family ──────────────────────────────────────────────────────────


class InvalidRequest(APIError):
    """The request itself is malformed.

    Covers:
      - invalid_request           (body shape / missing fields)
      - missing_idempotency_key   (write endpoint without header)
      - is_folder                 (Drive: tried to read a folder as a file)
    """


# ─── 409 ─────────────────────────────────────────────────────────────────


class IdempotencyConflict(APIError):
    """The Idempotency-Key was reused with a DIFFERENT request body.

    This is almost always a client bug — your code generated the same
    key for two semantically different calls. Mint a fresh key for the
    second call.

    `details` includes `cachedBodyHash` and `incomingBodyHash` so you
    can locate the divergence.
    """


# ─── 429 ─────────────────────────────────────────────────────────────────


class RateLimitError(APIError):
    """The server is rate-limiting this caller.

    `retry_after_seconds` is parsed from the `Retry-After` response header
    if present; None if the server didn't send one. Treat it as advisory —
    waiting at least that long before retrying is the cooperative choice.
    """

    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: int,
        details: Optional[Mapping[str, Any]] = None,
        retry_after_seconds: Optional[float] = None,
    ):
        super().__init__(message, code=code, status=status, details=details)
        self.retry_after_seconds = retry_after_seconds


# ─── Drive (424 + 502) ───────────────────────────────────────────────────


class DriveNotConnected(APIError):
    """The workspace doesn't have a connected Google Drive, or the
    connection lacks the required scope.

    Covers:
      - drive_not_connected
      - drive_scope_missing

    The caller's action is to ask the workspace owner to connect Drive
    in workspace settings; programmatic reconnection isn't currently
    exposed.
    """


class DriveError(APIError):
    """A Drive API call failed for a non-auth reason — usually upstream
    Drive returned a non-2xx, but covers any other Drive-side failure.

    `details.upstreamStatus` carries Drive's own status code when known.

    Covers:
      - drive_http
      - drive_failure
    """


# ─── Internal mapping ─────────────────────────────────────────────────────

# Server error.code → exception class. Codes not listed here fall through
# to base `APIError`. Adding a new code is non-breaking; renaming an
# existing one is a major-version bump.
_CODE_TO_CLASS: Mapping[str, type] = {
    # Auth
    "missing_auth": AuthError,
    "invalid_token_format": AuthError,
    "invalid_session": AuthError,
    "invalid_signature": AuthError,
    "session_revoked": AuthError,
    "session_expired": AuthError,
    "unauthenticated": AuthError,
    "not_owner": AuthError,
    # Authz
    "workspace_access_denied": WorkspaceAccessDenied,
    "workspace_forbidden": WorkspaceAccessDenied,
    # Not found
    "app_not_found": NotFoundError,
    "file_not_found": NotFoundError,
    "folder_not_found": NotFoundError,
    "event_not_registered": NotFoundError,
    "unknown_application_type": NotFoundError,
    "not_found": NotFoundError,
    # 400
    "invalid_request": InvalidRequest,
    "missing_idempotency_key": InvalidRequest,
    "is_folder": InvalidRequest,
    # 409
    "idempotency_conflict": IdempotencyConflict,
    # Drive
    "drive_not_connected": DriveNotConnected,
    "drive_scope_missing": DriveNotConnected,
    "drive_http": DriveError,
    "drive_failure": DriveError,
}


def exception_for_response(
    *,
    code: str,
    status: int,
    message: str,
    details: Optional[Mapping[str, Any]],
    retry_after_seconds: Optional[float] = None,
) -> APIError:
    """Map a server error body to the right exception class.

    Used by `_transport.py` after parsing a non-2xx response. Centralised
    here so the mapping table has one canonical home and additive changes
    don't drift across files.

    `retry_after_seconds` is only consulted for 429 responses; other codes
    ignore it.
    """
    # 429 always becomes RateLimitError, regardless of code field. (Some
    # gateways return 429 with their own error body shape; we still want
    # to surface retry_after correctly.)
    if status == 429:
        return RateLimitError(
            message,
            code=code or "rate_limited",
            status=status,
            details=details,
            retry_after_seconds=retry_after_seconds,
        )
    cls = _CODE_TO_CLASS.get(code, APIError)
    return cls(message, code=code, status=status, details=details)


__all__ = [
    "EsoulError",
    "MissingCredentialsError",
    "TransportError",
    "APIError",
    "AuthError",
    "WorkspaceAccessDenied",
    "NotFoundError",
    "InvalidRequest",
    "IdempotencyConflict",
    "RateLimitError",
    "DriveNotConnected",
    "DriveError",
    "exception_for_response",
]
