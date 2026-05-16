"""End-to-end tests for /api/v1/workspaces/{wsId}/apps/* — the
workspace-management surface (create / rename / delete).

Each test does its own full lifecycle (create → mutate → delete) so the
suite leaves no residue in the test workspace. Failures during mid-cycle
trigger cleanup via the `created_apps` fixture's finalizer.
"""

from __future__ import annotations

from typing import List, Optional

import pytest

import esoul
from esoul.exceptions import NotFoundError


@pytest.fixture
def created_apps(integration_token, integration_base_url, integration_workspace_id):
    """Track every CreatedApp this test made and delete it at teardown.

    The fixture yields a list the test appends to. After yield, we DELETE
    each app — best-effort, so failing one doesn't mask the others. Catches
    leakage when a test asserts mid-lifecycle and forgets to clean up.
    """
    if not integration_workspace_id:
        pytest.skip("ESOUL_TEST_WORKSPACE_ID not set")
    tracker: List[str] = []
    yield tracker
    # Teardown
    if not tracker:
        return
    with esoul.Esoul(token=integration_token, base_url=integration_base_url) as client:
        for node_id in tracker:
            try:
                client.workspaces.apps.delete(
                    workspace_id=integration_workspace_id,
                    node_id=node_id,
                )
            except Exception:
                pass


def test_create_returns_node_id_and_metadata(
    integration_token: str,
    integration_base_url: str,
    integration_workspace_id: Optional[str],
    created_apps: List[str],
) -> None:
    """create() returns a populated CreatedApp."""
    assert integration_workspace_id is not None  # fixture guard
    with esoul.Esoul(
        token=integration_token, base_url=integration_base_url,
    ) as client:
        result = client.workspaces.apps.create(
            workspace_id=integration_workspace_id,
            application_type="note_editor",
            instance_name="SDK test — create",
        )
        created_apps.append(result.node_id)

        assert result.node_id, "node_id should be populated"
        assert result.application_type == "note_editor"
        assert result.instance_name == "SDK test — create"
        assert result.workspace_id == integration_workspace_id
        assert result.created_at, "created_at should be populated"


def test_create_unknown_application_type_404(
    integration_token: str,
    integration_base_url: str,
    integration_workspace_id: Optional[str],
) -> None:
    """An unknown applicationType is rejected with 404 unknown_application_type."""
    assert integration_workspace_id is not None
    with esoul.Esoul(
        token=integration_token, base_url=integration_base_url,
    ) as client:
        with pytest.raises(NotFoundError) as excinfo:
            client.workspaces.apps.create(
                workspace_id=integration_workspace_id,
                application_type="not_a_real_app_type",
                instance_name="should never persist",
            )
        # Surface the server error code so we know we got the right 404
        # (not, e.g., the workspace_id 404 path).
        assert excinfo.value.code == "unknown_application_type"


def test_rename_changes_instance_name(
    integration_token: str,
    integration_base_url: str,
    integration_workspace_id: Optional[str],
    created_apps: List[str],
) -> None:
    """rename() updates instanceName + returns oldName/newName pair."""
    assert integration_workspace_id is not None
    with esoul.Esoul(
        token=integration_token, base_url=integration_base_url,
    ) as client:
        created = client.workspaces.apps.create(
            workspace_id=integration_workspace_id,
            application_type="todo_app",
            instance_name="SDK test — pre-rename",
        )
        created_apps.append(created.node_id)

        renamed = client.workspaces.apps.rename(
            workspace_id=integration_workspace_id,
            node_id=created.node_id,
            new_name="SDK test — post-rename",
        )

        assert renamed.node_id == created.node_id
        assert renamed.application_type == "todo_app"
        assert renamed.old_name == "SDK test — pre-rename"
        assert renamed.new_name == "SDK test — post-rename"
        assert renamed.workspace_id == integration_workspace_id


def test_rename_missing_app_404(
    integration_token: str,
    integration_base_url: str,
    integration_workspace_id: Optional[str],
) -> None:
    """Renaming an app that doesn't exist returns 404 app_not_found."""
    assert integration_workspace_id is not None
    with esoul.Esoul(
        token=integration_token, base_url=integration_base_url,
    ) as client:
        with pytest.raises(NotFoundError) as excinfo:
            client.workspaces.apps.rename(
                workspace_id=integration_workspace_id,
                node_id="definitely-not-a-real-node-id",
                new_name="ghost rename",
            )
        assert excinfo.value.code == "app_not_found"


def test_delete_removes_app(
    integration_token: str,
    integration_base_url: str,
    integration_workspace_id: Optional[str],
) -> None:
    """delete() removes the app and a subsequent read_state 404s."""
    assert integration_workspace_id is not None
    with esoul.Esoul(
        token=integration_token, base_url=integration_base_url,
    ) as client:
        created = client.workspaces.apps.create(
            workspace_id=integration_workspace_id,
            application_type="kanban_board",
            instance_name="SDK test — to be deleted",
        )

        removed = client.workspaces.apps.delete(
            workspace_id=integration_workspace_id,
            node_id=created.node_id,
        )
        assert removed.node_id == created.node_id
        assert removed.application_type == "kanban_board"
        assert removed.workspace_id == integration_workspace_id

        # Reading the app's state after delete should fail.
        with pytest.raises(NotFoundError):
            client.dispatch.read_state(app_id=created.node_id)


def test_delete_missing_app_404(
    integration_token: str,
    integration_base_url: str,
    integration_workspace_id: Optional[str],
) -> None:
    """Deleting an app that doesn't exist returns 404 app_not_found."""
    assert integration_workspace_id is not None
    with esoul.Esoul(
        token=integration_token, base_url=integration_base_url,
    ) as client:
        with pytest.raises(NotFoundError) as excinfo:
            client.workspaces.apps.delete(
                workspace_id=integration_workspace_id,
                node_id="not-a-real-node-id",
            )
        assert excinfo.value.code == "app_not_found"


def test_create_then_dispatch_to_new_app(
    integration_token: str,
    integration_base_url: str,
    integration_workspace_id: Optional[str],
    created_apps: List[str],
) -> None:
    """The new app is immediately addressable by dispatch.event."""
    assert integration_workspace_id is not None
    with esoul.Esoul(
        token=integration_token, base_url=integration_base_url,
    ) as client:
        created = client.workspaces.apps.create(
            workspace_id=integration_workspace_id,
            application_type="note_editor",
            instance_name="SDK test — write-after-create",
        )
        created_apps.append(created.node_id)

        result = client.dispatch.event(
            app_id=created.node_id,
            event_name="note_update_content",
            event_data={
                "tabName": "Default",
                "content": "Hello from a freshly-created app.",
            },
        )
        assert result.event_id
        # Read back to confirm the dispatch actually mutated state.
        state = client.dispatch.read_state(app_id=created.node_id).state
        notes = state.get("notes", [])
        assert any(
            "Hello from a freshly-created app." in n.get("content", "")
            for n in notes
        ), f"new note content not found in state: {state}"


def test_full_lifecycle_create_rename_delete(
    integration_token: str,
    integration_base_url: str,
    integration_workspace_id: Optional[str],
) -> None:
    """End-to-end happy path: create → rename → delete in one test."""
    assert integration_workspace_id is not None
    with esoul.Esoul(
        token=integration_token, base_url=integration_base_url,
    ) as client:
        # Create.
        c = client.workspaces.apps.create(
            workspace_id=integration_workspace_id,
            application_type="text_editor",
            instance_name="SDK lifecycle — created",
        )
        assert c.node_id

        # Rename.
        r = client.workspaces.apps.rename(
            workspace_id=integration_workspace_id,
            node_id=c.node_id,
            new_name="SDK lifecycle — renamed",
        )
        assert r.new_name == "SDK lifecycle — renamed"
        assert r.old_name == "SDK lifecycle — created"

        # Delete.
        d = client.workspaces.apps.delete(
            workspace_id=integration_workspace_id,
            node_id=c.node_id,
        )
        # instance_name on delete reflects the LATEST name, not the
        # original — the server reads it from WorkspaceApplication at
        # delete time, after the rename has landed.
        assert d.instance_name == "SDK lifecycle — renamed"
