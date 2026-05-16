"""Workspace HIL question queue resource — Stage 12.

Program-initiated questions:

    answer = client.questions.ask(
        WS, "Approve chunk 47?", default_on_timeout="approved",
    )
    # answer.answer is the user's response (or default_on_timeout)

Agent-initiated questions ALSO land in the same queue (via the
`ask_user` workspace tool); this resource lets a program LIST + ANSWER
them or just monitor.

Sync + async parity. All write endpoints require an Idempotency-Key
header which we auto-generate per call.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Literal, Optional, Sequence

from .._transport import AsyncTransport, SyncTransport
from ..exceptions import APIError, EsoulError

QuestionStatusLiteral = Literal[
    "pending", "answered", "expired", "cancelled",
]


@dataclass(frozen=True)
class Question:
    question_id: str
    workspace_id: str
    source: Literal["program", "agent"]
    invocation_id: Optional[str]
    asked_by_user_id: str
    question: str
    image_file_ids: List[str]
    status: QuestionStatusLiteral
    answer: Optional[str]
    answered_at: Optional[str]
    answered_by_user_id: Optional[str]
    created_at: str
    expires_at: str


@dataclass(frozen=True)
class Answer:
    question_id: str
    answer: str
    answered_at: str
    answered_by_user_id: str


def _decode_question(body: Dict[str, Any]) -> Question:
    return Question(
        question_id=body["questionId"],
        workspace_id=body["workspaceId"],
        source=body["source"],
        invocation_id=body.get("invocationId"),
        asked_by_user_id=body["askedByUserId"],
        question=body["question"],
        image_file_ids=list(body.get("imageFileIds", [])),
        status=body["status"],
        answer=body.get("answer"),
        answered_at=body.get("answeredAt"),
        answered_by_user_id=body.get("answeredByUserId"),
        created_at=body["createdAt"],
        expires_at=body["expiresAt"],
    )


class QuestionTimeout(EsoulError):
    """Raised by `ask()` / `wait_for_answer()` when the LOCAL wait
    times out. The question still exists server-side; the answer (or
    expiry) may land later."""

    def __init__(self, *, question_id: str, elapsed: float) -> None:
        super().__init__(
            f"Local wait timeout ({elapsed:.1f}s) elapsed while waiting on "
            f"question {question_id}. Call client.questions.get(...) to "
            f"poll again later."
        )
        self.question_id = question_id
        self.elapsed = elapsed


class QuestionAlreadyResolved(APIError):
    """409 when the question was already answered / cancelled / expired
    before this answer / cancel attempt won the race."""


def _backoff(interval: float) -> float:
    return min(interval * 1.5, 10.0)


# ─── Sync ────────────────────────────────────────────────────────────────


class QuestionsResource:
    def __init__(self, transport: SyncTransport) -> None:
        self._transport = transport

    def ask(
        self,
        workspace_id: str,
        question: str,
        *,
        image_file_ids: Optional[Sequence[str]] = None,
        timeout: float = 600.0,
        expires_in_seconds: int = 7 * 24 * 3600,
        default_on_timeout: Optional[str] = None,
        poll_interval: float = 2.0,
        idempotency_key: Optional[str] = None,
    ) -> Answer:
        """Synchronous ask + wait. Glues `ask_async` + `wait_for_answer`.

        Args:
            timeout: LOCAL wait timeout. The question's server-side TTL is
                `expires_in_seconds` (default 7d, max 30d). When `timeout`
                expires before the answer, raises `QuestionTimeout` —
                the question persists; poll again later.
            default_on_timeout: When set, the server's expire-cron
                auto-answers with this text instead of marking the row
                "expired". `ask()` returns the synthetic answer.
        """
        qid = self.ask_async(
            workspace_id, question,
            image_file_ids=image_file_ids,
            expires_in_seconds=expires_in_seconds,
            default_on_timeout=default_on_timeout,
            idempotency_key=idempotency_key,
        )
        return self.wait_for_answer(
            workspace_id, qid, timeout=timeout, poll_interval=poll_interval,
        )

    def ask_async(
        self,
        workspace_id: str,
        question: str,
        *,
        image_file_ids: Optional[Sequence[str]] = None,
        expires_in_seconds: int = 7 * 24 * 3600,
        default_on_timeout: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        """Post a question; return its id without waiting."""
        idem = idempotency_key or str(uuid.uuid4())
        body: Dict[str, Any] = {
            "question": question,
            "expiresInSeconds": expires_in_seconds,
        }
        if image_file_ids:
            body["imageFileIds"] = list(image_file_ids)
        if default_on_timeout is not None:
            body["defaultOnTimeout"] = default_on_timeout
        r = self._transport.request(
            "POST",
            f"/api/v1/workspaces/{workspace_id}/questions",
            json_body=body,
            idempotency_key=idem,
        )
        return r.json()["questionId"]

    def wait_for_answer(
        self,
        workspace_id: str,
        question_id: str,
        *,
        timeout: float = 600.0,
        poll_interval: float = 2.0,
    ) -> Answer:
        deadline = time.monotonic() + timeout
        interval = poll_interval
        while True:
            q = self.get(workspace_id, question_id)
            if q.status == "answered":
                return Answer(
                    question_id=q.question_id,
                    answer=q.answer or "",
                    answered_at=q.answered_at or "",
                    answered_by_user_id=q.answered_by_user_id or "",
                )
            if q.status in ("expired", "cancelled"):
                raise QuestionAlreadyResolved(
                    f"Question {question_id} resolved without an answer (status={q.status}).",
                    code="question_resolved_without_answer",
                    status_code=409,
                    details={"questionId": question_id, "status": q.status},
                )
            now = time.monotonic()
            if now >= deadline:
                raise QuestionTimeout(question_id=question_id, elapsed=timeout)
            time.sleep(min(interval, deadline - now))
            interval = _backoff(interval)

    def list(
        self,
        workspace_id: str,
        *,
        status: Literal["pending", "answered", "expired", "cancelled", "all"] = "pending",
        since: Optional[str] = None,
        limit: int = 50,
    ) -> List[Question]:
        params: Dict[str, Any] = {"status": status, "limit": limit}
        if since:
            params["since"] = since
        r = self._transport.request(
            "GET",
            f"/api/v1/workspaces/{workspace_id}/questions",
            params=params,
        )
        return [_decode_question(item) for item in r.json().get("questions", [])]

    def get(self, workspace_id: str, question_id: str) -> Question:
        r = self._transport.request(
            "GET",
            f"/api/v1/workspaces/{workspace_id}/questions/{question_id}",
        )
        return _decode_question(r.json())

    def answer(
        self,
        workspace_id: str,
        question_id: str,
        answer: str,
        *,
        idempotency_key: Optional[str] = None,
    ) -> Answer:
        idem = idempotency_key or str(uuid.uuid4())
        r = self._transport.request(
            "POST",
            f"/api/v1/workspaces/{workspace_id}/questions/{question_id}/answer",
            json_body={"answer": answer},
            idempotency_key=idem,
        )
        body = r.json()
        return Answer(
            question_id=body["questionId"],
            answer=body["answer"],
            answered_at=body["answeredAt"],
            answered_by_user_id=body["answeredByUserId"],
        )

    def cancel(
        self,
        workspace_id: str,
        question_id: str,
        *,
        idempotency_key: Optional[str] = None,
    ) -> Question:
        idem = idempotency_key or str(uuid.uuid4())
        self._transport.request(
            "POST",
            f"/api/v1/workspaces/{workspace_id}/questions/{question_id}/cancel",
            json_body={},
            idempotency_key=idem,
        )
        return self.get(workspace_id, question_id)


# ─── Async ───────────────────────────────────────────────────────────────


class AsyncQuestionsResource:
    def __init__(self, transport: AsyncTransport) -> None:
        self._transport = transport

    async def ask(
        self,
        workspace_id: str,
        question: str,
        *,
        image_file_ids: Optional[Sequence[str]] = None,
        timeout: float = 600.0,
        expires_in_seconds: int = 7 * 24 * 3600,
        default_on_timeout: Optional[str] = None,
        poll_interval: float = 2.0,
        idempotency_key: Optional[str] = None,
    ) -> Answer:
        qid = await self.ask_async(
            workspace_id, question,
            image_file_ids=image_file_ids,
            expires_in_seconds=expires_in_seconds,
            default_on_timeout=default_on_timeout,
            idempotency_key=idempotency_key,
        )
        return await self.wait_for_answer(
            workspace_id, qid, timeout=timeout, poll_interval=poll_interval,
        )

    async def ask_async(
        self,
        workspace_id: str,
        question: str,
        *,
        image_file_ids: Optional[Sequence[str]] = None,
        expires_in_seconds: int = 7 * 24 * 3600,
        default_on_timeout: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> str:
        idem = idempotency_key or str(uuid.uuid4())
        body: Dict[str, Any] = {
            "question": question,
            "expiresInSeconds": expires_in_seconds,
        }
        if image_file_ids:
            body["imageFileIds"] = list(image_file_ids)
        if default_on_timeout is not None:
            body["defaultOnTimeout"] = default_on_timeout
        r = await self._transport.request(
            "POST",
            f"/api/v1/workspaces/{workspace_id}/questions",
            json_body=body,
            idempotency_key=idem,
        )
        return r.json()["questionId"]

    async def wait_for_answer(
        self,
        workspace_id: str,
        question_id: str,
        *,
        timeout: float = 600.0,
        poll_interval: float = 2.0,
    ) -> Answer:
        deadline = time.monotonic() + timeout
        interval = poll_interval
        while True:
            q = await self.get(workspace_id, question_id)
            if q.status == "answered":
                return Answer(
                    question_id=q.question_id,
                    answer=q.answer or "",
                    answered_at=q.answered_at or "",
                    answered_by_user_id=q.answered_by_user_id or "",
                )
            if q.status in ("expired", "cancelled"):
                raise QuestionAlreadyResolved(
                    f"Question {question_id} resolved without an answer (status={q.status}).",
                    code="question_resolved_without_answer",
                    status_code=409,
                    details={"questionId": question_id, "status": q.status},
                )
            now = time.monotonic()
            if now >= deadline:
                raise QuestionTimeout(question_id=question_id, elapsed=timeout)
            await asyncio.sleep(min(interval, deadline - now))
            interval = _backoff(interval)

    async def list(
        self,
        workspace_id: str,
        *,
        status: Literal["pending", "answered", "expired", "cancelled", "all"] = "pending",
        since: Optional[str] = None,
        limit: int = 50,
    ) -> List[Question]:
        params: Dict[str, Any] = {"status": status, "limit": limit}
        if since:
            params["since"] = since
        r = await self._transport.request(
            "GET",
            f"/api/v1/workspaces/{workspace_id}/questions",
            params=params,
        )
        return [_decode_question(item) for item in r.json().get("questions", [])]

    async def get(self, workspace_id: str, question_id: str) -> Question:
        r = await self._transport.request(
            "GET",
            f"/api/v1/workspaces/{workspace_id}/questions/{question_id}",
        )
        return _decode_question(r.json())

    async def answer(
        self,
        workspace_id: str,
        question_id: str,
        answer: str,
        *,
        idempotency_key: Optional[str] = None,
    ) -> Answer:
        idem = idempotency_key or str(uuid.uuid4())
        r = await self._transport.request(
            "POST",
            f"/api/v1/workspaces/{workspace_id}/questions/{question_id}/answer",
            json_body={"answer": answer},
            idempotency_key=idem,
        )
        body = r.json()
        return Answer(
            question_id=body["questionId"],
            answer=body["answer"],
            answered_at=body["answeredAt"],
            answered_by_user_id=body["answeredByUserId"],
        )

    async def cancel(
        self,
        workspace_id: str,
        question_id: str,
        *,
        idempotency_key: Optional[str] = None,
    ) -> Question:
        idem = idempotency_key or str(uuid.uuid4())
        await self._transport.request(
            "POST",
            f"/api/v1/workspaces/{workspace_id}/questions/{question_id}/cancel",
            json_body={},
            idempotency_key=idem,
        )
        return await self.get(workspace_id, question_id)


__all__ = [
    "QuestionsResource",
    "AsyncQuestionsResource",
    "Question",
    "Answer",
    "QuestionTimeout",
    "QuestionAlreadyResolved",
]
