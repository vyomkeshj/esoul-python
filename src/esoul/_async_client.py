"""The user-facing async `AsyncEsoul` client.

Mirrors the sync `Esoul` API exactly with awaitable methods. Internally
backed by `AsyncTransport` (httpx.AsyncClient) and the per-resource
`Async*` mirrors.

Use as an async context manager to ensure connection-pool cleanup:

    async with esoul.AsyncEsoul() as client:
        result = await client.dispatch.event(...)
"""

from __future__ import annotations

import os
from typing import Optional

import httpx

from ._auth import Credentials, resolve_credentials
from ._client import DEFAULT_BASE_URL
from ._transport import (
    DEFAULT_MAX_RETRIES,
    DEFAULT_TIMEOUT_SECONDS,
    AsyncTransport,
)
from .resources.dispatch import AsyncDispatchResource
from .resources.drive import AsyncDriveResource
from .resources.workspaces import AsyncWorkspacesResource
from .resources.agents import AsyncAgentsResource
from .resources.questions import AsyncQuestionsResource


def _resolve_base_url(explicit: Optional[str]) -> str:
    if explicit:
        return explicit
    return os.environ.get("ESOUL_BASE_URL", DEFAULT_BASE_URL)


class AsyncEsoul:
    """Async client for the ExternalSoul platform.

    API identical to `Esoul` except all I/O methods are coroutines.
    Construction is sync; the network only fires on the first awaited
    request.

    Args: see `Esoul`.

    Example:
        >>> async with esoul.AsyncEsoul() as client:
        ...     state = await client.dispatch.read_state(app_id="...")
    """

    def __init__(
        self,
        *,
        token: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        self._credentials: Credentials = resolve_credentials(token)
        self._transport = AsyncTransport(
            self._credentials,
            base_url=_resolve_base_url(base_url),
            timeout=timeout,
            max_retries=max_retries,
            http_client=http_client,
        )
        self._dispatch_resource: Optional[AsyncDispatchResource] = None
        self._drive_resource: Optional[AsyncDriveResource] = None
        self._workspaces_resource: Optional[AsyncWorkspacesResource] = None
        self._agents_resource: Optional[AsyncAgentsResource] = None
        self._questions_resource: Optional[AsyncQuestionsResource] = None

    @property
    def dispatch(self) -> AsyncDispatchResource:
        if self._dispatch_resource is None:
            self._dispatch_resource = AsyncDispatchResource(self._transport)
        return self._dispatch_resource

    @property
    def drive(self) -> AsyncDriveResource:
        if self._drive_resource is None:
            self._drive_resource = AsyncDriveResource(self._transport)
        return self._drive_resource

    @property
    def workspaces(self) -> AsyncWorkspacesResource:
        """Workspace-management — create / rename / delete apps.

        Example:
            >>> async with esoul.AsyncEsoul() as client:
            ...     app = await client.workspaces.apps.create(
            ...         workspace_id="ws_abc",
            ...         application_type="spreadsheet",
            ...         instance_name="results",
            ...     )
        """
        if self._workspaces_resource is None:
            self._workspaces_resource = AsyncWorkspacesResource(self._transport)
        return self._workspaces_resource

    @property
    def agents(self) -> AsyncAgentsResource:
        """Invoke agent_builder runs from the SDK (async)."""
        if self._agents_resource is None:
            self._agents_resource = AsyncAgentsResource(self._transport)
        return self._agents_resource

    @property
    def questions(self) -> AsyncQuestionsResource:
        """Workspace HIL question queue (async)."""
        if self._questions_resource is None:
            self._questions_resource = AsyncQuestionsResource(self._transport)
        return self._questions_resource

    @property
    def credentials(self) -> Credentials:
        return self._credentials

    async def aclose(self) -> None:
        """Close the underlying httpx.AsyncClient. Idempotent."""
        await self._transport.aclose()

    async def __aenter__(self) -> "AsyncEsoul":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()


__all__ = ["AsyncEsoul"]
