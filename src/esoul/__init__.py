"""esoul — Python SDK for the ExternalSoul platform.

See README.md for usage. The top-level surface:

  esoul.Esoul          — sync client
  esoul.AsyncEsoul     — async client
  esoul.Credentials    — credential dataclass (typically accessed via client.credentials)
  esoul.<Exception>    — typed errors; see exceptions module docstring for the full hierarchy

Per-app typed resources (spreadsheet, slideshow, notes, …) ship via the
codegen pipeline and attach to the client as new attributes; they're
non-breaking additions.
"""

from __future__ import annotations

from ._async_client import AsyncEsoul
from ._auth import Credentials
from ._client import Esoul
from ._version import __version__
from .exceptions import (
    APIError,
    AuthError,
    DriveError,
    DriveNotConnected,
    EsoulError,
    IdempotencyConflict,
    InvalidRequest,
    MissingCredentialsError,
    NotFoundError,
    RateLimitError,
    TransportError,
    WorkspaceAccessDenied,
)
from .resources.agents import (
    InvocationError,
    InvocationHandle,
    InvocationResult,
    InvocationStatus,
    InvocationTimeout,
)
from .resources.questions import (
    Answer,
    Question,
    QuestionAlreadyResolved,
    QuestionTimeout,
)

__all__ = [
    # version
    "__version__",
    # clients
    "Esoul",
    "AsyncEsoul",
    "Credentials",
    # exceptions (re-exported at top-level for ergonomic `except esoul.AuthError`)
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
    # Stage 12 — agent invocation + workspace HIL queue
    "InvocationStatus",
    "InvocationResult",
    "InvocationHandle",
    "InvocationTimeout",
    "InvocationError",
    "Question",
    "Answer",
    "QuestionTimeout",
    "QuestionAlreadyResolved",
]
