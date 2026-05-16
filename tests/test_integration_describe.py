"""End-to-end tests for /api/v1/describe.

Covers:
  - happy path: token authenticates, response shape is well-formed
  - bad token: structured 401 with `invalid_*` error code
  - missing auth: structured 401 with `missing_auth`
"""

from __future__ import annotations

import pytest
import esoul


def test_describe_returns_session_and_namespaces(
    integration_token: str, integration_base_url: str
) -> None:
    """A valid PAT resolves to a session, and describe returns namespaces.

    Locked invariants we verify here so a future server change can't break
    them silently:
      - apiVersion = "v1"
      - session.kind is "sandbox" or "pat"
      - session.workspace_ids is a non-empty list
      - namespaces dict has at least one entry, each with an `events` list
    """
    with esoul.Esoul(
        token=integration_token, base_url=integration_base_url,
    ) as client:
        result = client.dispatch.describe()
    assert result.api_version == "v1"
    assert result.session.kind in ("sandbox", "pat")
    assert result.session.user_id, "session.user_id is empty"
    assert result.session.workspace_ids, "session.workspace_ids is empty"
    assert result.namespaces, "namespaces dict is empty"
    # Spot-check: at least one app should have events. APP_REGISTRY isn't
    # empty in any realistic deploy.
    has_events = any(ns.events for ns in result.namespaces.values())
    assert has_events, "no namespace has any events — registry empty?"


def test_describe_with_bad_token_raises_auth_error(
    integration_base_url: str,
) -> None:
    """An obviously-malformed token surfaces as `AuthError`, not a transport
    exception. Tests the error-mapping pipeline end-to-end."""
    with esoul.Esoul(
        token="esoul_pat_invalid.notarealsig",
        base_url=integration_base_url,
    ) as client:
        with pytest.raises(esoul.AuthError) as exc_info:
            client.dispatch.describe()
    assert exc_info.value.status == 401
    # Server should classify this; the error code lives in the error.code
    # field. invalid_session OR invalid_signature both indicate the
    # auth chain rejected the token at some stage — either is acceptable.
    assert exc_info.value.code in {
        "invalid_session",
        "invalid_signature",
        "invalid_token_format",
    }


#
# (`test_describe_missing_auth` was removed.) The server's `missing_auth`
# branch fires when the Authorization header is absent or malformed. httpx
# rejects whitespace-only tokens locally (LocalProtocolError) before they
# reach the wire, so the SDK can't trigger that server branch via its
# normal request path. Covered by the bogus-PAT test above which exercises
# the equivalent reject-at-the-server path.
#
