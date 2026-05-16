"""Example: add a few rows to the test spreadsheet via the SDK.

Pulls credentials from ../../.env.local (sibling of the kinetic repo
root). Reads the spreadsheet's current state to discover the sheetId +
column ids dynamically (rather than hard-coding), then dispatches one
`spreadsheet_add_rows` event with 5 rows.

Run from anywhere:

    cd python-packages/esoul && python examples/add_spreadsheet_rows.py

The browser, if showing the same workspace, should see the new rows
appear within ~100 ms via the existing realtime fan-out.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

import esoul


def load_env_local() -> None:
    """Read kinetic/.env.local — same pattern as conftest.py."""
    path = Path(__file__).resolve().parents[3] / ".env.local"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r'^([A-Z_]+)=(?:"([^"]*)"|(.*))$', line.strip())
        if m and m.group(1) not in os.environ:
            os.environ[m.group(1)] = m.group(2) or m.group(3) or ""


def main() -> None:
    load_env_local()
    token = os.environ["ESOUL_TEST_TOKEN"]
    base_url = os.environ.get("ESOUL_TEST_BASE_URL", "http://localhost:3000")
    app_id = os.environ.get(
        "ESOUL_TEST_SPREADSHEET_APP_ID", "OZM7Iv9n1dtUHYKL5Wxp7",
    )

    with esoul.Esoul(token=token, base_url=base_url) as client:
        # Discover the current shape (which sheet is active, what column
        # ids exist) so the writes always target the right cells even if
        # the spreadsheet gets renamed or has columns added later.
        snapshot = client.dispatch.read_state(app_id=app_id)
        sheets = snapshot.state.get("sheets", [])
        selected_sheet_id = snapshot.state.get("selectedSheetId")
        sheet = next(
            (s for s in sheets if s.get("id") == selected_sheet_id),
            sheets[0] if sheets else None,
        )
        if not sheet:
            raise RuntimeError("No sheet found in this spreadsheet.")

        columns = sheet.get("columns", [])
        if len(columns) < 3:
            raise RuntimeError(
                f"Expected at least 3 columns, found {len(columns)}. "
                "Add columns first via the UI."
            )
        col_a, col_b, col_c = columns[0]["id"], columns[1]["id"], columns[2]["id"]

        items = [
            ("Oat milk", "2", "Whole Foods"),
            ("Apples", "6", "Trader Joe's"),
            ("Coffee beans", "1 bag", "Local roaster"),
            ("Spinach", "1 bunch", "Whole Foods"),
            ("Cherry tomatoes", "1 box", "Trader Joe's"),
        ]

        # Build rows with client-side ids. The server's reducer requires
        # `rowId` to be present (it's how reads + future updates address
        # the row).
        rows = [
            {
                "rowId": str(uuid.uuid4()),
                "cells": {col_a: name, col_b: qty, col_c: where},
            }
            for (name, qty, where) in items
        ]

        result = client.dispatch.event(
            app_id=app_id,
            event_name="spreadsheet_add_rows",
            event_data={"sheetId": sheet["id"], "rows": rows},
        )

        print(f"Added {len(items)} rows.")
        print(f"  event_id={result.event_id}")
        print(f"  sequence_num={result.sequence_num}")
        print(f"  dispatched_at={result.dispatched_at}")

        # Read back to confirm the rows landed.
        after = client.dispatch.read_state(app_id=app_id)
        after_sheet = next(
            (s for s in after.state.get("sheets", []) if s["id"] == sheet["id"]),
            None,
        )
        n_rows = len(after_sheet["rows"]) if after_sheet else 0
        print(f"  spreadsheet row count: {n_rows} (was 0 at start)")


if __name__ == "__main__":
    main()
