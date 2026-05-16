"""End-to-end tests for /api/v1/dispatch-event.

Covers:
  - happy path: dispatch a no-op event (agent_builder viewport update)
  - event_not_registered: unknown eventName → 404
  - idempotency replay: same key + same body → cached response, no
    second event written
  - idempotency conflict: same key + different body → 409

The write tests use `agent_builder_set_viewport` against the test
agent_builder app — pure UI state with no functional side effect.
"""

from __future__ import annotations

import uuid
from typing import Optional

import pytest
import esoul


def _viewport_event(zoom: float = 1.0) -> dict:
    """The minimal payload `agent_builder_set_viewport` accepts.

    Stored verbatim in the agent_builder app's state; no downstream side
    effects, no validation beyond shape.
    """
    return {"viewport": {"x": 0, "y": 0, "zoom": zoom}}


def test_dispatch_event_returns_event_id_and_seq(
    integration_token: str,
    integration_base_url: str,
    integration_agent_builder_app_id: Optional[str],
) -> None:
    """Happy path: a valid event returns the new event's id + sequenceNum."""
    if not integration_agent_builder_app_id:
        pytest.skip("ESOUL_TEST_AGENT_BUILDER_APP_ID not set")
    with esoul.Esoul(
        token=integration_token, base_url=integration_base_url,
    ) as client:
        result = client.dispatch.event(
            app_id=integration_agent_builder_app_id,
            event_name="agent_builder_set_viewport",
            event_data=_viewport_event(zoom=1.0),
        )
    assert result.event_id, "event_id is empty"
    assert isinstance(result.sequence_num, int)
    assert result.sequence_num >= 0
    assert result.dispatched_at, "dispatched_at is empty"


def test_dispatch_event_unregistered_event_name_raises_not_found(
    integration_token: str,
    integration_base_url: str,
    integration_agent_builder_app_id: Optional[str],
) -> None:
    """Unknown eventName against a real app → NotFoundError(event_not_registered)."""
    if not integration_agent_builder_app_id:
        pytest.skip("ESOUL_TEST_AGENT_BUILDER_APP_ID not set")
    with esoul.Esoul(
        token=integration_token, base_url=integration_base_url,
    ) as client:
        with pytest.raises(esoul.NotFoundError) as exc_info:
            client.dispatch.event(
                app_id=integration_agent_builder_app_id,
                event_name="this_event_does_not_exist",
                event_data={"foo": "bar"},
            )
    assert exc_info.value.code == "event_not_registered"
    assert exc_info.value.status == 404


def test_dispatch_idempotency_replay_returns_cached_response(
    integration_token: str,
    integration_base_url: str,
    integration_agent_builder_app_id: Optional[str],
) -> None:
    """Same key + same body → second call returns cached response.

    The server caches the response under `(sessionId, key)` so a retry
    returns the SAME eventId. Verifies no duplicate event was written.
    """
    if not integration_agent_builder_app_id:
        pytest.skip("ESOUL_TEST_AGENT_BUILDER_APP_ID not set")
    key = str(uuid.uuid4())
    payload = _viewport_event(zoom=2.0)
    with esoul.Esoul(
        token=integration_token, base_url=integration_base_url,
    ) as client:
        first = client.dispatch.event(
            app_id=integration_agent_builder_app_id,
            event_name="agent_builder_set_viewport",
            event_data=payload,
            idempotency_key=key,
        )
        second = client.dispatch.event(
            app_id=integration_agent_builder_app_id,
            event_name="agent_builder_set_viewport",
            event_data=payload,
            idempotency_key=key,
        )
    assert first.event_id == second.event_id, (
        "Idempotency replay should return the cached eventId, not a new one"
    )
    assert first.sequence_num == second.sequence_num
    assert first.dispatched_at == second.dispatched_at


def test_dispatch_idempotency_conflict_raises_409(
    integration_token: str,
    integration_base_url: str,
    integration_agent_builder_app_id: Optional[str],
) -> None:
    """Same key + DIFFERENT body → 409 IdempotencyConflict, no second event."""
    if not integration_agent_builder_app_id:
        pytest.skip("ESOUL_TEST_AGENT_BUILDER_APP_ID not set")
    key = str(uuid.uuid4())
    with esoul.Esoul(
        token=integration_token, base_url=integration_base_url,
    ) as client:
        # First write: succeeds + populates cache under `key`.
        client.dispatch.event(
            app_id=integration_agent_builder_app_id,
            event_name="agent_builder_set_viewport",
            event_data=_viewport_event(zoom=3.0),
            idempotency_key=key,
        )
        # Second write with same key but DIFFERENT body → conflict.
        with pytest.raises(esoul.IdempotencyConflict) as exc_info:
            client.dispatch.event(
                app_id=integration_agent_builder_app_id,
                event_name="agent_builder_set_viewport",
                event_data=_viewport_event(zoom=4.0),
                idempotency_key=key,
            )
    assert exc_info.value.status == 409
    assert exc_info.value.code == "idempotency_conflict"
    # The server returns both body hashes for debugging.
    assert "cachedBodyHash" in exc_info.value.details
    assert "incomingBodyHash" in exc_info.value.details
    assert (
        exc_info.value.details["cachedBodyHash"]
        != exc_info.value.details["incomingBodyHash"]
    )


def test_dispatch_missing_idempotency_key_when_explicitly_empty() -> None:
    """SDK-side check: passing `idempotency_key=""` raises ValueError before
    any network call. This guards against silent auto-generation on a
    caller typo."""
    # Use an obviously-bad token — we never hit the network because the
    # ValueError fires on the empty string check.
    client = esoul.Esoul(token="esoul_pat_x.y", base_url="http://localhost:1")
    try:
        with pytest.raises(ValueError, match="non-empty"):
            client.dispatch.event(
                app_id="any",
                event_name="any",
                event_data={},
                idempotency_key="",
            )
    finally:
        client.close()
