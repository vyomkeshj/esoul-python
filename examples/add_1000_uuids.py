"""Stress demo: create a new tab with one column + 1000 UUIDs.

Demonstrates the bulk-dispatch path: ONE `spreadsheet_add_rows` event
carries all 1000 rows in `eventData.rows[]`. One DB write, one reducer
run, one realtime broadcast. The cost should be sub-linear in row count
compared to dispatching 1000 individual events.

Run:

    cd python-packages/esoul && python examples/add_1000_uuids.py
"""

from __future__ import annotations

import os
import re
import secrets
import time
import uuid
from pathlib import Path

import esoul


def load_env_local() -> None:
    path = Path(__file__).resolve().parents[3] / ".env.local"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r'^([A-Z_]+)=(?:"([^"]*)"|(.*))$', line.strip())
        if m and m.group(1) not in os.environ:
            os.environ[m.group(1)] = m.group(2) or m.group(3) or ""


def nanoid_like(length: int = 21) -> str:
    """Client-side id generator matching the codebase's nanoid usage.

    Spreadsheet event dataCreators use `nanoid()` for sheet/column ids;
    we pre-generate the same shape here so our event payload mirrors
    what the dataCreator would produce server-side.
    """
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    return "".join(secrets.choice(alphabet) for _ in range(length))


def main() -> None:
    load_env_local()
    token = os.environ["ESOUL_TEST_TOKEN"]
    base_url = os.environ.get("ESOUL_TEST_BASE_URL", "http://localhost:3000")
    app_id = os.environ.get(
        "ESOUL_TEST_SPREADSHEET_APP_ID", "OZM7Iv9n1dtUHYKL5Wxp7",
    )

    print(f"Target: {base_url}  app_id={app_id}")
    print()

    with esoul.Esoul(token=token, base_url=base_url) as client:
        # Step 1: create a new sheet. We pre-generate sheetId + columnId
        # client-side so we can address them in the subsequent add_rows
        # dispatch. The spreadsheet's add_sheet reducer reads both from
        # eventData (the original dataCreator generates them, but the
        # processor takes whatever's in eventData) — so client-generated
        # ids are honoured.
        new_sheet_id = nanoid_like()
        new_col_id = nanoid_like()
        created_at = int(time.time() * 1000)

        print("[01] Create sheet '1000 UUIDs'")
        t0 = time.perf_counter()
        r1 = client.dispatch.event(
            app_id=app_id,
            event_name="spreadsheet_add_sheet",
            event_data={
                "name": "1000 UUIDs",
                "sheetId": new_sheet_id,
                "columnId": new_col_id,
                "createdAt": created_at,
            },
        )
        t1 = time.perf_counter()
        print(
            f"  ↳ {(t1 - t0) * 1000:6.0f} ms  seq={r1.sequence_num}"
            f"  sheetId={new_sheet_id[:8]}…"
        )

        # Step 2: build 1000 rows in memory and dispatch in ONE event.
        # Each row gets a fresh rowId (server requires this) and a single
        # cell mapping `columnId → <uuid string>`.
        print()
        print("[02] Build 1000-row payload locally")
        t0 = time.perf_counter()
        rows = [
            {"rowId": str(uuid.uuid4()), "cells": {new_col_id: str(uuid.uuid4())}}
            for _ in range(1000)
        ]
        local_ms = (time.perf_counter() - t0) * 1000
        print(f"  ↳ {local_ms:6.0f} ms  (in-memory only)")

        # Step 3: dispatch the bulk event.
        print()
        print("[03] Dispatch spreadsheet_add_rows (1000 rows, one event)")
        t0 = time.perf_counter()
        r2 = client.dispatch.event(
            app_id=app_id,
            event_name="spreadsheet_add_rows",
            event_data={"sheetId": new_sheet_id, "rows": rows},
        )
        dispatch_ms = (time.perf_counter() - t0) * 1000
        print(
            f"  ↳ {dispatch_ms:6.0f} ms  seq={r2.sequence_num}"
            f"  event={r2.event_id[:8]}…"
        )
        print(
            f"      = {dispatch_ms / 1000:.0f} µs per row "
            f"(end-to-end: HTTP + DB + state-cache + realtime publish)"
        )

        print()
        print(f"Total: 1 new sheet + 1000 rows in {(dispatch_ms + (t1 - t0) * 0):.0f} ms"
              f" of dispatch time.")
        print("Switch to the '1000 UUIDs' tab in your browser to see them.")


if __name__ == "__main__":
    main()
