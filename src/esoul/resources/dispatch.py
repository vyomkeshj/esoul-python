"""Low-level dispatch resource — the SDK's escape hatch.

Maps 1:1 onto the `/api/v1/dispatch-event`, `/api/v1/dispatch-batch`,
`/api/v1/state`, `/api/v1/describe` endpoints. Per-app typed resources
(emitted by the codegen pipeline) ultimately call into these — but user
code can also call them directly for events not yet covered by codegen
or for raw control.

Both sync (`DispatchResource`) and async (`AsyncDispatchResource`)
variants are exposed; the public API is identical except for `await`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .._transport import AsyncTransport, SyncTransport


def _resolve_idempotency_key(explicit: Optional[str]) -> str:
    """Resource-layer idempotency-key resolution.

    Auto-generates a UUID v4 when `explicit` is None — the common case.
    When `explicit` is a non-empty string, use it as-is — the caller has
    its own retry strategy and wants stable dedup keys.

    Empty strings are rejected loudly: silently auto-generating on `""`
    would let a typo silently disable the caller's idempotency contract.
    """
    if explicit is None:
        return str(uuid.uuid4())
    if not explicit:
        raise ValueError(
            "`idempotency_key` must be either None (auto-generate) or a non-empty string",
        )
    return explicit


# ─── Response dataclasses ────────────────────────────────────────────────


@dataclass
class DispatchResult:
    """Result of a single dispatched event.

    `event_id` is the WorkspaceEvent row id (stable, unique forever).
    `sequence_num` is the per-branch monotonic counter — useful as a
    cursor for `events/since` style queries.
    `dispatched_at` is ISO 8601 UTC (the server's `timestamp.toISOString()`).
    """

    event_id: str
    sequence_num: int
    dispatched_at: str


@dataclass
class BatchEvent:
    """Input shape for `dispatch.batch`. Mirrors the request body's per-event entry."""

    app_id: str
    event_name: str
    event_data: Dict[str, Any]


@dataclass
class BatchResult:
    """Result of `dispatch.batch`. Length equals input event count on success."""

    results: List[DispatchResult]


@dataclass
class ReadStateResult:
    """Result of `dispatch.read_state`.

    `state` is the live materialised view of the app's data — exactly what
    the reducer would produce on cold replay of the event log.
    `version` is the workspace branch's `headEventSeq` at read time;
    useful as an opaque token for change detection (two reads with the
    same `version` saw the workspace at the same logical point).
    """

    state: Dict[str, Any]
    version: int
    updated_at: str


@dataclass
class SessionInfo:
    user_id: str
    session_id: str
    kind: str
    sandbox_id: Optional[str]
    workspace_ids: List[str]
    expires_at: Optional[str]


@dataclass
class EventInfo:
    name: str
    type: str
    collapsible: bool


@dataclass
class NamespaceInfo:
    events: List[EventInfo] = field(default_factory=list)


@dataclass
class DescribeResult:
    """Result of `dispatch.describe`.

    Use to discover (1) what workspaces this session can mutate, (2) what
    apps + event names are registered server-side. Per-app schemas (types
    + return shapes + docs) ship via the codegen pipeline in additive
    fields here.
    """

    api_version: str
    session: SessionInfo
    namespaces: Dict[str, NamespaceInfo]


# ─── Wire shape helpers ──────────────────────────────────────────────────


def _decode_dispatch_result(body: Dict[str, Any]) -> DispatchResult:
    return DispatchResult(
        event_id=body["eventId"],
        sequence_num=int(body["sequenceNum"]),
        dispatched_at=body["dispatchedAt"],
    )


def _decode_describe(body: Dict[str, Any]) -> DescribeResult:
    sess = body.get("session", {})
    namespaces_raw = body.get("namespaces", {})
    namespaces: Dict[str, NamespaceInfo] = {}
    for ns_name, ns_body in namespaces_raw.items():
        events = [
            EventInfo(
                name=ev["name"], type=ev.get("type", "Workspace"),
                collapsible=bool(ev.get("collapsible", False)),
            )
            for ev in ns_body.get("events", [])
        ]
        namespaces[ns_name] = NamespaceInfo(events=events)
    return DescribeResult(
        api_version=body.get("apiVersion", "v1"),
        session=SessionInfo(
            user_id=sess.get("userId", ""),
            session_id=sess.get("sessionId", ""),
            kind=sess.get("kind", ""),
            sandbox_id=sess.get("sandboxId"),
            workspace_ids=list(sess.get("workspaceIds", [])),
            expires_at=sess.get("expiresAt"),
        ),
        namespaces=namespaces,
    )


def _serialize_batch_events(events: Sequence[BatchEvent]) -> List[Dict[str, Any]]:
    return [
        {"appId": e.app_id, "eventName": e.event_name, "eventData": e.event_data}
        for e in events
    ]


# ─── Sync resource ───────────────────────────────────────────────────────


class DispatchResource:
    """Low-level event dispatch + state read + describe.

    Typical usage is through codegen'd typed resources (e.g.
    `client.spreadsheet.rows.create(...)`); this resource is the escape
    hatch when codegen hasn't covered the event yet, or when the caller
    wants raw control over the dispatch shape.
    """

    def __init__(self, transport: SyncTransport) -> None:
        self._transport = transport

    def event(
        self,
        *,
        app_id: str,
        event_name: str,
        event_data: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> DispatchResult:
        """Dispatch a single event.

        `idempotency_key`:
          - None (default): SDK auto-generates a UUID v4 per call.
          - explicit string: caller-controlled. Useful when YOUR script
            has its own retry logic and you want OUR cache to dedup
            (e.g. nightly cron with a stable per-run key).
        """
        response = self._transport.request(
            "POST",
            "/api/v1/dispatch-event",
            json_body={
                "appId": app_id,
                "eventName": event_name,
                "eventData": event_data,
            },
            idempotency_key=_resolve_idempotency_key(idempotency_key),
        )
        return _decode_dispatch_result(response.json())

    def batch(
        self,
        events: Sequence[BatchEvent],
        *,
        idempotency_key: Optional[str] = None,
    ) -> BatchResult:
        """Dispatch up to 100 events in one HTTP round-trip.

        Validation is upfront-all-or-nothing: if any event fails authz /
        registration / shape, NOTHING is written. Sequential dispatch
        after validation means a mid-batch DB failure leaves prior
        events committed (see /api/v1/dispatch-batch route header for
        the full atomicity caveat).
        """
        if not events:
            raise ValueError("`events` must contain at least one BatchEvent")
        if len(events) > 100:
            raise ValueError("Batch is limited to 100 events per call")
        response = self._transport.request(
            "POST",
            "/api/v1/dispatch-batch",
            json_body={"events": _serialize_batch_events(events)},
            idempotency_key=_resolve_idempotency_key(idempotency_key),
        )
        body = response.json()
        return BatchResult(
            results=[_decode_dispatch_result(r) for r in body.get("results", [])],
        )

    def read_state(self, *, app_id: str) -> ReadStateResult:
        """Read the live state of an app. No event replay; ~5 ms typical."""
        response = self._transport.request(
            "GET", "/api/v1/state", params={"appId": app_id},
        )
        body = response.json()
        return ReadStateResult(
            state=body.get("state", {}),
            version=int(body.get("version", 0)),
            updated_at=body.get("updatedAt", ""),
        )

    def describe(self) -> DescribeResult:
        """Introspect the platform: session + registered apps/events."""
        response = self._transport.request("GET", "/api/v1/describe")
        return _decode_describe(response.json())


# ─── Async resource ──────────────────────────────────────────────────────


class AsyncDispatchResource:
    """Async mirror of `DispatchResource`. Identical surface, awaitable methods."""

    def __init__(self, transport: AsyncTransport) -> None:
        self._transport = transport

    async def event(
        self,
        *,
        app_id: str,
        event_name: str,
        event_data: Dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> DispatchResult:
        response = await self._transport.request(
            "POST",
            "/api/v1/dispatch-event",
            json_body={
                "appId": app_id,
                "eventName": event_name,
                "eventData": event_data,
            },
            idempotency_key=_resolve_idempotency_key(idempotency_key),
        )
        return _decode_dispatch_result(response.json())

    async def batch(
        self,
        events: Sequence[BatchEvent],
        *,
        idempotency_key: Optional[str] = None,
    ) -> BatchResult:
        if not events:
            raise ValueError("`events` must contain at least one BatchEvent")
        if len(events) > 100:
            raise ValueError("Batch is limited to 100 events per call")
        response = await self._transport.request(
            "POST",
            "/api/v1/dispatch-batch",
            json_body={"events": _serialize_batch_events(events)},
            idempotency_key=_resolve_idempotency_key(idempotency_key),
        )
        body = response.json()
        return BatchResult(
            results=[_decode_dispatch_result(r) for r in body.get("results", [])],
        )

    async def read_state(self, *, app_id: str) -> ReadStateResult:
        response = await self._transport.request(
            "GET", f"/api/v1/state?appId={app_id}",
        )
        body = response.json()
        return ReadStateResult(
            state=body.get("state", {}),
            version=int(body.get("version", 0)),
            updated_at=body.get("updatedAt", ""),
        )

    async def describe(self) -> DescribeResult:
        response = await self._transport.request("GET", "/api/v1/describe")
        return _decode_describe(response.json())


__all__ = [
    "DispatchResource",
    "AsyncDispatchResource",
    "DispatchResult",
    "BatchEvent",
    "BatchResult",
    "ReadStateResult",
    "DescribeResult",
    "SessionInfo",
    "EventInfo",
    "NamespaceInfo",
]
