"""Workspace-management resource — create / rename / delete apps in a workspace.

Wraps three endpoints:
  - POST   /api/v1/workspaces/{workspaceId}/apps
  - POST   /api/v1/workspaces/{workspaceId}/apps/{nodeId}/rename
  - DELETE /api/v1/workspaces/{workspaceId}/apps/{nodeId}

Until this resource existed, the SDK could only mutate *existing* apps via
`client.dispatch.event(...)` — the dispatch route requires `app_id` to
resolve to a `WorkspaceApplication` row. Workspace lifecycle (add /
remove / rename) had to happen in the browser UI. This resource closes
that gap: scripts can stand up an entire workspace from scratch.

All three operations live on the server-side timeline (the underlying
events are `workspace/add_app`, `workspace/remove_app`,
`workspace/rename_app` — identical to what the human UI dispatches), and
all three fan out via Inngest Realtime to connected browsers so the
change appears in any open tab within ~100-300 ms.

Usage:
    client = esoul.Esoul()
    app = client.workspaces.apps.create(
        workspace_id="ws_abc",
        application_type="spreadsheet",
        instance_name="results",
    )
    print(app.node_id, app.application_type)

    client.workspaces.apps.rename(
        workspace_id="ws_abc",
        node_id=app.node_id,
        new_name="Q2 results",
    )

    client.workspaces.apps.delete(
        workspace_id="ws_abc",
        node_id=app.node_id,
    )
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .._transport import AsyncTransport, SyncTransport
from .dispatch import _resolve_idempotency_key


# ─── Response dataclasses ────────────────────────────────────────────────


@dataclass
class CreatedApp:
    """Result of `workspaces.apps.create`.

    `node_id` is the new app's nodeId — pass it to subsequent
    `dispatch.event(app_id=...)` calls, or store it for later
    `workspaces.apps.rename` / `delete` calls.
    """

    node_id: str
    application_type: str
    instance_name: str
    workspace_id: str
    created_at: str


@dataclass
class RenamedApp:
    """Result of `workspaces.apps.rename`."""

    node_id: str
    application_type: str
    old_name: str
    new_name: str
    workspace_id: str
    renamed_at: str


@dataclass
class RemovedApp:
    """Result of `workspaces.apps.delete`."""

    node_id: str
    application_type: str
    instance_name: str
    workspace_id: str
    removed_at: str


@dataclass
class AppInstance:
    """One app in a workspace, as returned by `workspaces.apps.list`.

    `state` is populated only when `list(..., include_state=True)` —
    that's the full materialised view of the app (e.g. for a
    spreadsheet, the sheets / columns / rows; for a notes app, the
    tabs). Without `include_state`, `state` is `None` and you save a
    chunk of payload.
    """

    node_id: str
    application_type: str
    instance_name: str
    created_at: str
    updated_at: str
    state: Optional[Dict[str, Any]] = None


@dataclass
class ListAppsResult:
    workspace_id: str
    apps: List[AppInstance] = field(default_factory=list)


# ─── Sync resource ───────────────────────────────────────────────────────


class _AppsSyncResource:
    """Nested resource exposed as `client.workspaces.apps.*`.

    Lives one level below `WorkspacesResource` so the surface reads
    naturally: `client.workspaces.apps.create(...)`. Mirrors the
    `client.dispatch.event(...)` convention of keyword-only args so
    call sites are self-documenting.
    """

    def __init__(self, transport: SyncTransport) -> None:
        self._transport = transport

    def list(
        self,
        *,
        workspace_id: str,
        include_state: bool = False,
        types: Optional[Sequence[str]] = None,
    ) -> ListAppsResult:
        """Enumerate every app instance in the workspace.

        This is the orientation primitive an agent (or a script
        standing up a workspace) calls first to discover what's in the
        workspace. `dispatch.describe()` returns the *registry* of app
        TYPES + events — `apps.list()` returns the actual instances.

        Args:
            workspace_id: The workspace to list. Must be in the
                session's `workspace_ids`.
            include_state: When True, each app's `state` (live
                materialised view) is embedded in the response. Bumps
                payload size — only ask when you need it.
            types: Filter to specific applicationType values
                (e.g. `["spreadsheet", "note_editor"]`). Unknown types
                are rejected as 400 invalid_request.

        Example:
            >>> result = client.workspaces.apps.list(
            ...     workspace_id="ws_abc", include_state=True,
            ... )
            >>> for app in result.apps:
            ...     print(app.node_id, app.application_type,
            ...           app.instance_name)
            ...     if app.application_type == "spreadsheet":
            ...         print("  sheets:", len(app.state["sheets"]))
        """
        params: Dict[str, str] = {}
        if include_state:
            params["include_state"] = "true"
        if types:
            params["types"] = ",".join(types)
        response = self._transport.request(
            "GET",
            f"/api/v1/workspaces/{workspace_id}/apps",
            params=params if params else None,
        )
        data = response.json()
        return ListAppsResult(
            workspace_id=data["workspaceId"],
            apps=[
                AppInstance(
                    node_id=a["nodeId"],
                    application_type=a["applicationType"],
                    instance_name=a["instanceName"],
                    created_at=a["createdAt"],
                    updated_at=a["updatedAt"],
                    state=a.get("state"),
                )
                for a in data.get("apps", [])
            ],
        )

    def create(
        self,
        *,
        workspace_id: str,
        application_type: str,
        instance_name: str,
        initial_state: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> CreatedApp:
        """Create a new app in the workspace.

        `application_type` must be a value from the platform's
        `APP_REGISTRY` — e.g. `"spreadsheet"`, `"note_editor"`,
        `"slideshow"`, `"todo_app"`, `"kanban_board"`, `"calendar"`,
        `"contacts_viewer"`, `"email_viewer"`, `"image_viewer"`,
        `"pdf_viewer"`, `"text_editor"`, `"tldraw_canvas"`,
        `"e2b_sandbox"`, `"agent_builder"`, `"search_engine"`.

        `initial_state` is optional — when omitted the platform uses
        each app's schema-provided default. Useful when you want to
        pre-seed sheets / columns / tabs without a second dispatch
        round-trip.
        """
        body: Dict[str, Any] = {
            "applicationType": application_type,
            "instanceName": instance_name,
        }
        if initial_state is not None:
            body["initialState"] = initial_state
        response = self._transport.request(
            "POST",
            f"/api/v1/workspaces/{workspace_id}/apps",
            json_body=body,
            idempotency_key=_resolve_idempotency_key(idempotency_key),
        )
        data = response.json()
        return CreatedApp(
            node_id=data["nodeId"],
            application_type=data["applicationType"],
            instance_name=data["instanceName"],
            workspace_id=data["workspaceId"],
            created_at=data["createdAt"],
        )

    def rename(
        self,
        *,
        workspace_id: str,
        node_id: str,
        new_name: str,
        idempotency_key: Optional[str] = None,
    ) -> RenamedApp:
        """Change an app's `instanceName`.

        Writes one `workspace/rename_app` event + updates the live
        column + mirrors the rename into the workspace-graph node so
        the UI sidebar / canvas pick up the new label immediately.
        """
        response = self._transport.request(
            "POST",
            f"/api/v1/workspaces/{workspace_id}/apps/{node_id}/rename",
            json_body={"newName": new_name},
            idempotency_key=_resolve_idempotency_key(idempotency_key),
        )
        data = response.json()
        return RenamedApp(
            node_id=data["nodeId"],
            application_type=data["applicationType"],
            old_name=data["oldName"],
            new_name=data["newName"],
            workspace_id=data["workspaceId"],
            renamed_at=data["renamedAt"],
        )

    def delete(
        self,
        *,
        workspace_id: str,
        node_id: str,
        idempotency_key: Optional[str] = None,
    ) -> RemovedApp:
        """Remove an app from the workspace.

        Writes one `workspace/remove_app` event, deletes the
        `WorkspaceApplication` row + cascade `ApplicationPort` rows,
        and strips the node + connected edges from the workspace
        graph. Timeline rows referencing the removed app remain
        intact (the audit invariant) — scrubbing past the delete
        replays prior events against the (now empty) reducer cleanly.
        """
        response = self._transport.request(
            "DELETE",
            f"/api/v1/workspaces/{workspace_id}/apps/{node_id}",
            idempotency_key=_resolve_idempotency_key(idempotency_key),
        )
        data = response.json()
        return RemovedApp(
            node_id=data["nodeId"],
            application_type=data["applicationType"],
            instance_name=data["instanceName"],
            workspace_id=data["workspaceId"],
            removed_at=data["removedAt"],
        )


class WorkspacesResource:
    """`client.workspaces.*` — workspace-management surface.

    Today this exposes a single nested resource — `.apps` — that
    covers app lifecycle (create / rename / delete). Future additions
    (pin / unpin / focus / file-management) layer in additively
    without changing the existing surface.
    """

    def __init__(self, transport: SyncTransport) -> None:
        self._transport = transport
        self._apps: Optional[_AppsSyncResource] = None

    @property
    def apps(self) -> _AppsSyncResource:
        if self._apps is None:
            self._apps = _AppsSyncResource(self._transport)
        return self._apps


# ─── Async resource ──────────────────────────────────────────────────────


class _AppsAsyncResource:
    def __init__(self, transport: AsyncTransport) -> None:
        self._transport = transport

    async def list(
        self,
        *,
        workspace_id: str,
        include_state: bool = False,
        types: Optional[Sequence[str]] = None,
    ) -> ListAppsResult:
        """Async mirror of `_AppsSyncResource.list`."""
        params: Dict[str, str] = {}
        if include_state:
            params["include_state"] = "true"
        if types:
            params["types"] = ",".join(types)
        response = await self._transport.request(
            "GET",
            f"/api/v1/workspaces/{workspace_id}/apps",
            params=params if params else None,
        )
        data = response.json()
        return ListAppsResult(
            workspace_id=data["workspaceId"],
            apps=[
                AppInstance(
                    node_id=a["nodeId"],
                    application_type=a["applicationType"],
                    instance_name=a["instanceName"],
                    created_at=a["createdAt"],
                    updated_at=a["updatedAt"],
                    state=a.get("state"),
                )
                for a in data.get("apps", [])
            ],
        )

    async def create(
        self,
        *,
        workspace_id: str,
        application_type: str,
        instance_name: str,
        initial_state: Optional[Dict[str, Any]] = None,
        idempotency_key: Optional[str] = None,
    ) -> CreatedApp:
        body: Dict[str, Any] = {
            "applicationType": application_type,
            "instanceName": instance_name,
        }
        if initial_state is not None:
            body["initialState"] = initial_state
        response = await self._transport.request(
            "POST",
            f"/api/v1/workspaces/{workspace_id}/apps",
            json_body=body,
            idempotency_key=_resolve_idempotency_key(idempotency_key),
        )
        data = response.json()
        return CreatedApp(
            node_id=data["nodeId"],
            application_type=data["applicationType"],
            instance_name=data["instanceName"],
            workspace_id=data["workspaceId"],
            created_at=data["createdAt"],
        )

    async def rename(
        self,
        *,
        workspace_id: str,
        node_id: str,
        new_name: str,
        idempotency_key: Optional[str] = None,
    ) -> RenamedApp:
        response = await self._transport.request(
            "POST",
            f"/api/v1/workspaces/{workspace_id}/apps/{node_id}/rename",
            json_body={"newName": new_name},
            idempotency_key=_resolve_idempotency_key(idempotency_key),
        )
        data = response.json()
        return RenamedApp(
            node_id=data["nodeId"],
            application_type=data["applicationType"],
            old_name=data["oldName"],
            new_name=data["newName"],
            workspace_id=data["workspaceId"],
            renamed_at=data["renamedAt"],
        )

    async def delete(
        self,
        *,
        workspace_id: str,
        node_id: str,
        idempotency_key: Optional[str] = None,
    ) -> RemovedApp:
        response = await self._transport.request(
            "DELETE",
            f"/api/v1/workspaces/{workspace_id}/apps/{node_id}",
            idempotency_key=_resolve_idempotency_key(idempotency_key),
        )
        data = response.json()
        return RemovedApp(
            node_id=data["nodeId"],
            application_type=data["applicationType"],
            instance_name=data["instanceName"],
            workspace_id=data["workspaceId"],
            removed_at=data["removedAt"],
        )


class AsyncWorkspacesResource:
    def __init__(self, transport: AsyncTransport) -> None:
        self._transport = transport
        self._apps: Optional[_AppsAsyncResource] = None

    @property
    def apps(self) -> _AppsAsyncResource:
        if self._apps is None:
            self._apps = _AppsAsyncResource(self._transport)
        return self._apps


__all__ = [
    "WorkspacesResource",
    "AsyncWorkspacesResource",
    "CreatedApp",
    "RenamedApp",
    "RemovedApp",
    "AppInstance",
    "ListAppsResult",
]
