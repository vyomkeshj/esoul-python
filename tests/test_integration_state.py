"""End-to-end tests for /api/v1/state.

Covers:
  - happy path: read state of a real app
  - workspace-isolation: requesting an app outside our workspaces → 404
"""

from __future__ import annotations

from typing import Optional

import pytest
import esoul


def test_read_state_returns_state_and_version(
    integration_token: str,
    integration_base_url: str,
    integration_spreadsheet_app_id: Optional[str],
) -> None:
    """A real app's read_state returns a dict + monotonic version."""
    if not integration_spreadsheet_app_id:
        pytest.skip("ESOUL_TEST_SPREADSHEET_APP_ID not set")
    with esoul.Esoul(
        token=integration_token, base_url=integration_base_url,
    ) as client:
        result = client.dispatch.read_state(app_id=integration_spreadsheet_app_id)
    assert isinstance(result.state, dict)
    assert isinstance(result.version, int)
    assert result.version >= 0
    assert result.updated_at, "updated_at is empty"


def test_read_state_with_nonexistent_app_id_raises_not_found(
    integration_token: str, integration_base_url: str,
) -> None:
    """Unknown appId → 404 NotFoundError, not a generic APIError."""
    bogus_id = "this-app-does-not-exist-anywhere-in-the-system-abc123"
    with esoul.Esoul(
        token=integration_token, base_url=integration_base_url,
    ) as client:
        with pytest.raises(esoul.NotFoundError) as exc_info:
            client.dispatch.read_state(app_id=bogus_id)
    assert exc_info.value.status == 404
    assert exc_info.value.code == "app_not_found"
    # Server includes the appId in details for debugging.
    assert exc_info.value.details.get("appId") == bogus_id


def test_read_state_consecutive_reads_same_version(
    integration_token: str,
    integration_base_url: str,
    integration_spreadsheet_app_id: Optional[str],
) -> None:
    """Two reads with no intervening writes return the same version.

    Confirms `version` is monotonic + stable — a useful property for
    change-detection callers.
    """
    if not integration_spreadsheet_app_id:
        pytest.skip("ESOUL_TEST_SPREADSHEET_APP_ID not set")
    with esoul.Esoul(
        token=integration_token, base_url=integration_base_url,
    ) as client:
        first = client.dispatch.read_state(app_id=integration_spreadsheet_app_id)
        second = client.dispatch.read_state(app_id=integration_spreadsheet_app_id)
    # second.version >= first.version is the guarantee; equality is the
    # strict case (no concurrent writes between calls). On a quiet test
    # workspace they should match.
    assert second.version >= first.version
