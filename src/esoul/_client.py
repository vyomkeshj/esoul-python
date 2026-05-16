"""The user-facing sync `Esoul` client.

`Esoul()` is the entry point. It owns:
  - A `Credentials` object (resolved from explicit kwarg → env → file).
  - A `SyncTransport` wrapping httpx with retries + idempotency + refresh.
  - Resource accessors (`.dispatch`, `.drive`, more added by codegen).

Resources are lazy properties so that constructing a client with bad
credentials still works for `client.describe()` style introspection
(which itself will raise, but at the request site, not at client init).
"""

from __future__ import annotations

from typing import Optional

import httpx

from ._auth import Credentials, resolve_credentials
from ._transport import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    SyncTransport,
)
from .resources.dispatch import DispatchResource
from .resources.drive import DriveResource
from .resources.workspaces import WorkspacesResource
from .resources.agents import AgentsResource
from .resources.questions import QuestionsResource

#: Default server base URL. Override per-client via `base_url=` kwarg, or
#: via the `ESOUL_BASE_URL` env var (set by the sandbox-boot script so
#: in-sandbox SDK auto-targets the right host).
DEFAULT_BASE_URL = "https://externalsoul.com"


def _resolve_base_url(explicit: Optional[str]) -> str:
    import os

    if explicit:
        return explicit
    return os.environ.get("ESOUL_BASE_URL", DEFAULT_BASE_URL)


class Esoul:
    """Sync client for the ExternalSoul platform.

    Construction is cheap — it resolves credentials and opens an httpx
    connection pool but doesn't make any network calls until you invoke
    a method.

    Args:
        token: Explicit bearer token. If omitted, the SDK auto-detects
            from `ESOUL_TOKEN` env, then `/var/run/esoul/token`, then
            `~/.config/esoul/credentials`.
        base_url: Server origin (e.g. `"https://externalsoul.com"`).
            Defaults to `ESOUL_BASE_URL` env or the package default.
        timeout: Per-request timeout in seconds. httpx applies this to
            connect + read + write phases uniformly.
        max_retries: Number of attempts (including the first) the SDK
            makes on retryable failures. The same idempotency key is
            reused across attempts so the server dedups on success.
        http_client: Pre-configured `httpx.Client` to share. Useful when
            the calling application already has a pool tuned for its
            own needs.

    Example:
        >>> client = esoul.Esoul()
        >>> client.dispatch.describe()       # introspection
        >>> client.dispatch.event(app_id=..., event_name=..., event_data=...)
    """

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: Optional[httpx.Client] = None,
    ):
        self._credentials: Credentials = resolve_credentials(token)
        self._transport = SyncTransport(
            self._credentials,
            base_url=_resolve_base_url(base_url),
            timeout=timeout,
            max_retries=max_retries,
            http_client=http_client,
        )
        # Resources hold a transport reference but no state — cheap.
        self._dispatch_resource: Optional[DispatchResource] = None
        self._drive_resource: Optional[DriveResource] = None
        self._workspaces_resource: Optional[WorkspacesResource] = None
        self._agents_resource: Optional[AgentsResource] = None
        self._questions_resource: Optional[QuestionsResource] = None

    # ─── Public resource accessors ─────────────────────────────────────

    @property
    def dispatch(self) -> DispatchResource:
        """Low-level event dispatch + state read + describe."""
        if self._dispatch_resource is None:
            self._dispatch_resource = DispatchResource(self._transport)
        return self._dispatch_resource

    @property
    def drive(self) -> DriveResource:
        """Google Drive operations on the workspace's connected Drive."""
        if self._drive_resource is None:
            self._drive_resource = DriveResource(self._transport)
        return self._drive_resource

    @property
    def workspaces(self) -> WorkspacesResource:
        """Workspace-management — create / rename / delete apps.

        Example:
            >>> app = client.workspaces.apps.create(
            ...     workspace_id="ws_abc",
            ...     application_type="spreadsheet",
            ...     instance_name="results",
            ... )
            >>> client.workspaces.apps.rename(
            ...     workspace_id="ws_abc",
            ...     node_id=app.node_id,
            ...     new_name="Q2 results",
            ... )
            >>> client.workspaces.apps.delete(
            ...     workspace_id="ws_abc",
            ...     node_id=app.node_id,
            ... )
        """
        if self._workspaces_resource is None:
            self._workspaces_resource = WorkspacesResource(self._transport)
        return self._workspaces_resource

    @property
    def agents(self) -> AgentsResource:
        """Invoke agent_builder runs from the SDK.

        Example:
            >>> h = client.agents.invoke(
            ...     "ws_abc",
            ...     "image_extractor",          # nodeId or instanceName
            ...     input="Extract fields from photo.jpg",
            ...     images=["file_xyz"],
            ... )
            >>> result = h.wait(timeout=300)
            >>> print(result.text)
        """
        if self._agents_resource is None:
            self._agents_resource = AgentsResource(self._transport)
        return self._agents_resource

    @property
    def questions(self) -> QuestionsResource:
        """Ask the human user a question — workspace HIL queue.

        Example:
            >>> ans = client.questions.ask(
            ...     "ws_abc", "Approve chunk 47?",
            ...     default_on_timeout="approved",
            ...     timeout=300,
            ... )
            >>> print(ans.answer)
        """
        if self._questions_resource is None:
            self._questions_resource = QuestionsResource(self._transport)
        return self._questions_resource

    # ─── Convenience accessors ─────────────────────────────────────────

    @property
    def credentials(self) -> Credentials:
        """Access the resolved credentials.

        Useful for diagnostics — e.g. `client.credentials.source` tells
        you which precedence rung the token came from. The `token` field
        is sensitive; treat as you would any secret.
        """
        return self._credentials

    # ─── Lifecycle ─────────────────────────────────────────────────────

    def close(self) -> None:
        """Close the underlying HTTP connection pool.

        Safe to call multiple times. Useful at process shutdown to flush
        in-flight TLS sessions. The `with` form below is the idiomatic
        pattern.
        """
        self._transport.close()

    def __enter__(self) -> "Esoul":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = ["Esoul"]
