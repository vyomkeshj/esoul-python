"""Agent invocation resource — Stage 12 (SDK-driven agent runs).

Primary surface:

    client.agents.invoke(
        workspace_id,            # WS in session.workspace_ids
        agent,                   # nodeId (uuid) OR instanceName (case-insensitive)
        input="...",
        images=["fileId-or-path", ...],
    )

The handle returned polls /agent-invocations/{id} for status. Long-running
runs are durable across reconnect; the SDK is allowed to crash mid-wait
and resume the same handle later via `client.agents.get(invocation_id)`.

`invoke_pin(pinned_agent_id, ...)` is a thin convenience layer over
`invoke` — looks up the caller's own pinned agent and routes to its
source workspace + node.

Both sync (`AgentsResource`) and async (`AsyncAgentsResource`) variants
are exposed; identical surface except for `await` + asyncio.sleep.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, Union

from .._transport import AsyncTransport, SyncTransport
from ..exceptions import APIError, EsoulError

# ─── Type aliases ────────────────────────────────────────────────────────

InputType = Union[str, List[Dict[str, Any]]]
"""String OR HandoffEntry[] (multimodal — each entry: {text, images?}).
The server normalises both shapes."""

InvocationStatusLiteral = Literal[
    "running",
    "awaiting_user_answer",
    "awaiting_approval",
    "ok",
    "error",
    "canceled",
    "timeout",
]

TERMINAL_STATUSES = frozenset({"ok", "error", "canceled", "timeout"})


# ─── Dataclasses ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class InvocationStatus:
    """Polling snapshot of an invocation's state."""

    invocation_id: str
    status: InvocationStatusLiteral
    started_at: str
    finished_at: Optional[str]
    result_text: Optional[str]
    result_router_handoff: Optional[List[Dict[str, Any]]]
    error_message: Optional[str]
    pending_user_question: Optional[Dict[str, Any]]
    pending_approval: Optional[Dict[str, Any]]
    agent_workspace_id: str
    agent_node_id: str
    agent_instance_name: Optional[str]
    pinned_agent_id: Optional[str]

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass(frozen=True)
class InvocationResult:
    """Terminal-state result returned by `InvocationHandle.wait()`."""

    invocation_id: str
    status: Literal["ok", "error", "canceled", "timeout"]
    text: str
    router_handoff: Optional[List[Dict[str, Any]]]
    error_message: Optional[str]
    started_at: str
    finished_at: str


# ─── Decoders ────────────────────────────────────────────────────────────


def _decode_status(body: Dict[str, Any]) -> InvocationStatus:
    return InvocationStatus(
        invocation_id=body["invocationId"],
        status=body["status"],
        started_at=body["startedAt"],
        finished_at=body.get("finishedAt"),
        result_text=body.get("resultText"),
        result_router_handoff=body.get("resultRouterHandoff"),
        error_message=body.get("errorMessage"),
        pending_user_question=body.get("pendingUserQuestion"),
        pending_approval=body.get("pendingApproval"),
        agent_workspace_id=body["agentWorkspaceId"],
        agent_node_id=body["agentNodeId"],
        agent_instance_name=body.get("agentInstanceName"),
        pinned_agent_id=body.get("pinnedAgentId"),
    )


def _to_result(status: InvocationStatus) -> InvocationResult:
    assert status.is_terminal
    return InvocationResult(
        invocation_id=status.invocation_id,
        status=status.status,  # type: ignore[arg-type]
        text=status.result_text or "",
        router_handoff=status.result_router_handoff,
        error_message=status.error_message,
        started_at=status.started_at,
        finished_at=status.finished_at or "",
    )


def _backoff(interval: float) -> float:
    """Exponential backoff capped at 10 s. Matches the server's
    `agents.invoke` design — short polls early, longer polls under load.
    Multiplier 1.5: 2.0 → 3.0 → 4.5 → 6.75 → 10.0."""
    return min(interval * 1.5, 10.0)


# ─── Sync handle + resource ──────────────────────────────────────────────


class InvocationTimeout(EsoulError):
    """Raised by `wait()` when the LOCAL timeout elapses before the
    invocation reaches a terminal state. The run continues server-side;
    poll/wait again on the same `invocation_id`."""

    def __init__(self, *, invocation_id: str, elapsed: float) -> None:
        super().__init__(
            f"Local wait timeout ({elapsed:.1f}s) elapsed while polling "
            f"invocation {invocation_id}. The run continues server-side; "
            f"call client.agents.get(invocation_id) to resume polling."
        )
        self.invocation_id = invocation_id
        self.elapsed = elapsed


class InvocationError(APIError):
    """Raised by `wait()` when the invocation reaches terminal status='error'."""

    def __init__(self, *, invocation_id: str, message: Optional[str]) -> None:
        super().__init__(
            f"Invocation {invocation_id} terminated with error: {message or '(no message)'}",
            code="invocation_error",
            status_code=500,
            details={"invocationId": invocation_id, "message": message},
        )


class InvocationHandle:
    def __init__(self, transport: SyncTransport, invocation_id: str) -> None:
        self._transport = transport
        self._id = invocation_id

    @property
    def invocation_id(self) -> str:
        return self._id

    def poll(self) -> InvocationStatus:
        r = self._transport.request("GET", f"/api/v1/agent-invocations/{self._id}")
        return _decode_status(r.json())

    def wait(
        self,
        *,
        timeout: float = 600.0,
        poll_interval: float = 2.0,
        on_question: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_approval: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> InvocationResult:
        """Poll until terminal status OR `timeout` seconds elapsed.

        Exponential backoff: poll_interval → 1.5x each → 10s cap. Callbacks
        fire once per NEW pending state (deduped by questionId / summary)
        so a 7-day-paused invocation doesn't re-invoke them every 10s."""
        deadline = time.monotonic() + timeout
        interval = poll_interval
        last_qid: Optional[str] = None
        last_approval_at: Optional[float] = None
        while True:
            status = self.poll()
            if status.is_terminal:
                if status.status == "error":
                    raise InvocationError(
                        invocation_id=self._id, message=status.error_message,
                    )
                return _to_result(status)
            if on_question and status.pending_user_question:
                qid = status.pending_user_question.get("questionId")
                if qid and qid != last_qid:
                    on_question(status.pending_user_question)
                    last_qid = qid
            if on_approval and status.pending_approval:
                requested_at = status.pending_approval.get("requestedAt")
                if requested_at != last_approval_at:
                    on_approval(status.pending_approval)
                    last_approval_at = requested_at  # type: ignore[assignment]
            now = time.monotonic()
            if now >= deadline:
                raise InvocationTimeout(invocation_id=self._id, elapsed=timeout)
            sleep_for = min(interval, deadline - now)
            time.sleep(sleep_for)
            interval = _backoff(interval)

    def cancel(self) -> InvocationStatus:
        idem = str(uuid.uuid4())
        r = self._transport.request(
            "POST",
            f"/api/v1/agent-invocations/{self._id}/cancel",
            json_body={},
            idempotency_key=idem,
        )
        body = r.json()
        # /cancel returns {invocationId, status}; re-fetch full snapshot
        # for the caller. Cheap; the row is already in memory server-side.
        snap = self.poll()
        return snap if snap else _decode_status(body)


class AgentsResource:
    """Invoke agent_builder runs from the SDK."""

    def __init__(self, transport: SyncTransport) -> None:
        self._transport = transport

    def invoke(
        self,
        agent: str,
        *,
        input: InputType,
        workspace_id: Optional[str] = None,
        images: Optional[Sequence[str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> InvocationHandle:
        """Invoke any agent_builder app in the workspace.

        Args:
            agent: nodeId (uuid) OR instanceName (case-insensitive).
                Ambiguous instanceName → APIError(409, agent_ambiguous)
                with `details.matchingNodeIds` for disambiguation.
            input: Plain text or HandoffEntry[] for multimodal.
            workspace_id: A workspace in session.workspace_ids. When
                omitted, defaults to the session's only workspace (or
                raises if the session has multiple).
            images: Optional workspace fileIds / paths to attach as
                multimodal content for the first agent.
            idempotency_key: Auto-generated UUID v4 if omitted. Retries
                with the same key produce the same invocationId (server
                seeds the deterministic uuidv5 derivation off it).
        """
        ws = self._transport.resolve_workspace_id(workspace_id)
        idem = idempotency_key or str(uuid.uuid4())
        body: Dict[str, Any] = {"input": input}
        if images:
            body["images"] = list(images)
        r = self._transport.request(
            "POST",
            f"/api/v1/workspaces/{ws}/agents/{agent}/invoke",
            json_body=body,
            idempotency_key=idem,
        )
        invocation_id = r.json()["invocationId"]
        return InvocationHandle(self._transport, invocation_id)

    def invoke_pin(
        self,
        pinned_agent_id: str,
        *,
        input: InputType,
        images: Optional[Sequence[str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> InvocationHandle:
        """Invoke a pinned agent on the caller's own account.

        Same shape as `invoke()`, but routes via the pin-convenience
        endpoint which resolves the pin's source workspace + nodeId
        server-side. Useful when the SDK script knows it wants "my
        pinned X" without looking up where it lives.
        """
        idem = idempotency_key or str(uuid.uuid4())
        body: Dict[str, Any] = {"input": input}
        if images:
            body["images"] = list(images)
        r = self._transport.request(
            "POST",
            f"/api/v1/pinned-agents/{pinned_agent_id}/invoke",
            json_body=body,
            idempotency_key=idem,
        )
        invocation_id = r.json()["invocationId"]
        return InvocationHandle(self._transport, invocation_id)

    def get(self, invocation_id: str) -> InvocationStatus:
        """Resume polling an existing invocation by id."""
        r = self._transport.request(
            "GET", f"/api/v1/agent-invocations/{invocation_id}",
        )
        return _decode_status(r.json())

    def list(
        self,
        *,
        workspace_id: Optional[str] = None,
        status: Optional[InvocationStatusLiteral] = None,
        limit: int = 50,
        since: Optional[str] = None,
    ) -> List[InvocationStatus]:
        """List invocations the current caller started.

        Filterable by workspace, status, and a since-cursor (ISO-8601).
        Order: most-recent-first."""
        params: Dict[str, Any] = {"limit": limit}
        if workspace_id:
            params["workspace_id"] = workspace_id
        if status:
            params["status"] = status
        if since:
            params["since"] = since
        r = self._transport.request("GET", "/api/v1/agent-invocations", params=params)
        body = r.json()
        return [_decode_status(item) for item in body.get("invocations", [])]


# ─── Async handle + resource ─────────────────────────────────────────────


class AsyncInvocationHandle:
    def __init__(self, transport: AsyncTransport, invocation_id: str) -> None:
        self._transport = transport
        self._id = invocation_id

    @property
    def invocation_id(self) -> str:
        return self._id

    async def poll(self) -> InvocationStatus:
        r = await self._transport.request(
            "GET", f"/api/v1/agent-invocations/{self._id}",
        )
        return _decode_status(r.json())

    async def wait(
        self,
        *,
        timeout: float = 600.0,
        poll_interval: float = 2.0,
        on_question: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_approval: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> InvocationResult:
        deadline = time.monotonic() + timeout
        interval = poll_interval
        last_qid: Optional[str] = None
        last_approval_at: Optional[float] = None
        while True:
            status = await self.poll()
            if status.is_terminal:
                if status.status == "error":
                    raise InvocationError(
                        invocation_id=self._id, message=status.error_message,
                    )
                return _to_result(status)
            if on_question and status.pending_user_question:
                qid = status.pending_user_question.get("questionId")
                if qid and qid != last_qid:
                    on_question(status.pending_user_question)
                    last_qid = qid
            if on_approval and status.pending_approval:
                requested_at = status.pending_approval.get("requestedAt")
                if requested_at != last_approval_at:
                    on_approval(status.pending_approval)
                    last_approval_at = requested_at  # type: ignore[assignment]
            now = time.monotonic()
            if now >= deadline:
                raise InvocationTimeout(invocation_id=self._id, elapsed=timeout)
            sleep_for = min(interval, deadline - now)
            await asyncio.sleep(sleep_for)
            interval = _backoff(interval)

    async def cancel(self) -> InvocationStatus:
        idem = str(uuid.uuid4())
        await self._transport.request(
            "POST",
            f"/api/v1/agent-invocations/{self._id}/cancel",
            json_body={},
            idempotency_key=idem,
        )
        return await self.poll()


class AsyncAgentsResource:
    def __init__(self, transport: AsyncTransport) -> None:
        self._transport = transport

    async def invoke(
        self,
        agent: str,
        *,
        input: InputType,
        workspace_id: Optional[str] = None,
        images: Optional[Sequence[str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> AsyncInvocationHandle:
        ws = await self._transport.resolve_workspace_id(workspace_id)
        idem = idempotency_key or str(uuid.uuid4())
        body: Dict[str, Any] = {"input": input}
        if images:
            body["images"] = list(images)
        r = await self._transport.request(
            "POST",
            f"/api/v1/workspaces/{ws}/agents/{agent}/invoke",
            json_body=body,
            idempotency_key=idem,
        )
        return AsyncInvocationHandle(self._transport, r.json()["invocationId"])

    async def invoke_pin(
        self,
        pinned_agent_id: str,
        *,
        input: InputType,
        images: Optional[Sequence[str]] = None,
        idempotency_key: Optional[str] = None,
    ) -> AsyncInvocationHandle:
        idem = idempotency_key or str(uuid.uuid4())
        body: Dict[str, Any] = {"input": input}
        if images:
            body["images"] = list(images)
        r = await self._transport.request(
            "POST",
            f"/api/v1/pinned-agents/{pinned_agent_id}/invoke",
            json_body=body,
            idempotency_key=idem,
        )
        return AsyncInvocationHandle(self._transport, r.json()["invocationId"])

    async def get(self, invocation_id: str) -> InvocationStatus:
        r = await self._transport.request(
            "GET", f"/api/v1/agent-invocations/{invocation_id}",
        )
        return _decode_status(r.json())

    async def list(
        self,
        *,
        workspace_id: Optional[str] = None,
        status: Optional[InvocationStatusLiteral] = None,
        limit: int = 50,
        since: Optional[str] = None,
    ) -> List[InvocationStatus]:
        params: Dict[str, Any] = {"limit": limit}
        if workspace_id:
            params["workspace_id"] = workspace_id
        if status:
            params["status"] = status
        if since:
            params["since"] = since
        r = await self._transport.request(
            "GET", "/api/v1/agent-invocations", params=params,
        )
        body = r.json()
        return [_decode_status(item) for item in body.get("invocations", [])]


__all__ = [
    "AgentsResource",
    "AsyncAgentsResource",
    "InvocationHandle",
    "AsyncInvocationHandle",
    "InvocationStatus",
    "InvocationResult",
    "InvocationTimeout",
    "InvocationError",
]
