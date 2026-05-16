"""Live-demo: 10 mutations to the workspace, one per second.

Watch the spreadsheet in your browser while this runs — you should see
each change land within ~100-300 ms of the script logging it. The script
prints per-call latency so you can read the SDK's end-to-end cost (DB
write + state-cache + realtime publish) per dispatch.

Run from anywhere:

    cd python-packages/esoul && python examples/live_demo_10_mutations.py
"""

from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path

import esoul


def load_env_local() -> None:
    """Pull ESOUL_TEST_* from kinetic/.env.local."""
    path = Path(__file__).resolve().parents[3] / ".env.local"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r'^([A-Z_]+)=(?:"([^"]*)"|(.*))$', line.strip())
        if m and m.group(1) not in os.environ:
            os.environ[m.group(1)] = m.group(2) or m.group(3) or ""


# ─── Mutation step helpers ─────────────────────────────────────────────


class Demo:
    """Encapsulates the state the script accumulates as it runs.

    Each mutation builds on the previous one — we remember column ids,
    row ids, and the sheet id as we go so later steps can address what
    earlier steps created.
    """

    def __init__(
        self,
        client: esoul.Esoul,
        spreadsheet_app_id: str,
    ):
        self.client = client
        self.app_id = spreadsheet_app_id
        self.sheet_id: str = ""
        self.col_a: str = ""
        self.col_b: str = ""
        self.col_c: str = ""
        self.status_col_id: str = ""  # populated by mutation #2
        self.notes_col_id: str = ""   # populated by mutation #8
        self.test_1_row_id: str = ""  # populated by mutation #3
        self.latencies_ms: list[float] = []

    def prime(self) -> None:
        """Read the spreadsheet to discover a sheet with the 3-column shape
        the demo expects. Falls through to whichever sheet has ≥3 columns
        (typically Sheet 1 — the auto-created default with A/B/C); after
        the 1000-UUID demo, the currently-selected sheet may have just
        1 column, so we don't blindly use `selectedSheetId`.
        """
        state = self.client.dispatch.read_state(app_id=self.app_id).state
        sheets = state.get("sheets", [])
        sheet = next(
            (s for s in sheets if len(s.get("columns", [])) >= 3),
            None,
        )
        if not sheet:
            raise RuntimeError(
                "No sheet with ≥3 columns. Create one in the UI first.",
            )
        self.sheet_id = sheet["id"]
        cols = sheet["columns"]
        self.col_a, self.col_b, self.col_c = cols[0]["id"], cols[1]["id"], cols[2]["id"]

    def time_dispatch(self, label: str, event_name: str, event_data: dict) -> None:
        """Dispatch + record latency + print one-line status."""
        t0 = time.perf_counter()
        result = self.client.dispatch.event(
            app_id=self.app_id,
            event_name=event_name,
            event_data=event_data,
        )
        latency_ms = (time.perf_counter() - t0) * 1000
        self.latencies_ms.append(latency_ms)
        print(
            f"  ↳ {latency_ms:6.0f} ms  seq={result.sequence_num:<4}  {label}"
        )

    # ── individual mutations ────────────────────────────────────────

    def step_1_rename_sheet(self) -> None:
        self.time_dispatch(
            "rename sheet → 'Live SDK Demo'",
            "spreadsheet_rename_sheet",
            {"sheetId": self.sheet_id, "newName": "Live SDK Demo"},
        )

    def step_2_add_status_column(self) -> None:
        self.status_col_id = str(uuid.uuid4()).replace("-", "")[:21]
        self.time_dispatch(
            "add column 'Status'",
            "spreadsheet_add_column",
            {
                "sheetId": self.sheet_id,
                "columnId": self.status_col_id,
                "name": "Status",
                "type": "text",
            },
        )

    def step_3_add_first_row(self) -> None:
        self.test_1_row_id = str(uuid.uuid4())
        self.time_dispatch(
            "add row 'Test 1: Hello from Python'",
            "spreadsheet_add_row",
            {
                "sheetId": self.sheet_id,
                "rowId": self.test_1_row_id,
                "cells": {
                    self.col_a: "Test 1",
                    self.col_b: "Hello from Python",
                    self.col_c: f"{time.strftime('%H:%M:%S')}",
                },
            },
        )

    def step_4_mark_running(self) -> None:
        self.time_dispatch(
            "set Test 1 status → 'running'",
            "spreadsheet_set_cell",
            {
                "sheetId": self.sheet_id,
                "rowId": self.test_1_row_id,
                "columnId": self.status_col_id,
                "value": "running",
            },
        )

    def step_5_add_second_row(self) -> None:
        rid = str(uuid.uuid4())
        self.time_dispatch(
            "add row 'Test 2: Realtime publish working'",
            "spreadsheet_add_row",
            {
                "sheetId": self.sheet_id,
                "rowId": rid,
                "cells": {
                    self.col_a: "Test 2",
                    self.col_b: "Realtime publish working",
                    self.col_c: f"{time.strftime('%H:%M:%S')}",
                    self.status_col_id: "complete",
                },
            },
        )

    def step_6_mark_complete(self) -> None:
        self.time_dispatch(
            "set Test 1 status → 'complete'",
            "spreadsheet_set_cell",
            {
                "sheetId": self.sheet_id,
                "rowId": self.test_1_row_id,
                "columnId": self.status_col_id,
                "value": "complete",
            },
        )

    def step_7_bulk_add_three(self) -> None:
        rows = [
            {
                "rowId": str(uuid.uuid4()),
                "cells": {
                    self.col_a: f"Test {3 + i}",
                    self.col_b: msg,
                    self.col_c: f"{time.strftime('%H:%M:%S')}",
                    self.status_col_id: "complete",
                },
            }
            for i, msg in enumerate([
                "Bulk inserts via one event",
                "Reducer batched in Immer",
                "One realtime publish, three rows",
            ])
        ]
        self.time_dispatch(
            "bulk add 3 rows (one event, one publish)",
            "spreadsheet_add_rows",
            {"sheetId": self.sheet_id, "rows": rows},
        )

    def step_8_add_notes_column(self) -> None:
        self.notes_col_id = str(uuid.uuid4()).replace("-", "")[:21]
        self.time_dispatch(
            "add column 'Notes'",
            "spreadsheet_add_column",
            {
                "sheetId": self.sheet_id,
                "columnId": self.notes_col_id,
                "name": "Notes",
                "type": "text",
            },
        )

    def step_9_annotate_test_1(self) -> None:
        self.time_dispatch(
            "set Test 1 notes → 'Verified end-to-end!'",
            "spreadsheet_set_cell",
            {
                "sheetId": self.sheet_id,
                "rowId": self.test_1_row_id,
                "columnId": self.notes_col_id,
                "value": "Verified end-to-end!",
            },
        )

    def step_10_final_rename(self) -> None:
        self.time_dispatch(
            "rename sheet → 'Live SDK Demo — done'",
            "spreadsheet_rename_sheet",
            {"sheetId": self.sheet_id, "newName": "Live SDK Demo — done"},
        )

    def summary(self) -> None:
        n = len(self.latencies_ms)
        avg = sum(self.latencies_ms) / n
        mn = min(self.latencies_ms)
        mx = max(self.latencies_ms)
        # 50th percentile (median).
        sorted_ms = sorted(self.latencies_ms)
        p50 = sorted_ms[n // 2]
        print()
        print(f"  {n} mutations dispatched")
        print(f"  latency  min={mn:.0f} ms  median={p50:.0f} ms  max={mx:.0f} ms  avg={avg:.0f} ms")


def main() -> None:
    load_env_local()
    token = os.environ["ESOUL_TEST_TOKEN"]
    base_url = os.environ.get("ESOUL_TEST_BASE_URL", "http://localhost:3000")
    app_id = os.environ.get(
        "ESOUL_TEST_SPREADSHEET_APP_ID", "OZM7Iv9n1dtUHYKL5Wxp7",
    )

    print(f"Target: {base_url}  app_id={app_id}")
    print("Watch the spreadsheet in your browser. 10 mutations, 1 per second.")
    print()

    with esoul.Esoul(token=token, base_url=base_url) as client:
        demo = Demo(client, app_id)
        demo.prime()

        steps = [
            ("01", demo.step_1_rename_sheet),
            ("02", demo.step_2_add_status_column),
            ("03", demo.step_3_add_first_row),
            ("04", demo.step_4_mark_running),
            ("05", demo.step_5_add_second_row),
            ("06", demo.step_6_mark_complete),
            ("07", demo.step_7_bulk_add_three),
            ("08", demo.step_8_add_notes_column),
            ("09", demo.step_9_annotate_test_1),
            ("10", demo.step_10_final_rename),
        ]

        for label, step in steps:
            print(f"[{label}]")
            step()
            # 1 s gap so the user can visually track each change as it
            # arrives. Subtract the dispatch latency we already burnt so
            # the wall-clock per iteration is ~1 s, not ~1 s + latency.
            elapsed_ms = demo.latencies_ms[-1]
            remaining_ms = max(0.0, 1000.0 - elapsed_ms)
            time.sleep(remaining_ms / 1000.0)

        demo.summary()


if __name__ == "__main__":
    main()
