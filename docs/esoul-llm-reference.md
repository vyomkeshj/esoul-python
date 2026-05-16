# `esoul` Python SDK — Complete API Reference

> Programmatic workspace mutation on the **ExternalSoul / Kinetic** platform.
> Mutate spreadsheets, notes, slideshows, todos, kanban boards, calendars, contacts, email, agent-builder networks, sandboxes and more — from a Python script, a Colab notebook, a CI job, or a Vercel cron.
>
> This document is the canonical reference. An agent reading this should be able to write correct SDK code on the first try. Every event the platform accepts is enumerated below with its exact `event_name`, `event_data` shape, and a working Python example.

---

## 🤖 If you are an agent in an ExternalSoul sandbox — READ THIS FIRST

The SDK is already installed and **authentication is automatic**. Do this:

```python
import esoul

# No token argument needed — the sandbox provisions a short-lived JWT at
# /var/run/esoul/token that esoul.Esoul() auto-detects. The sandbox is
# scoped to exactly ONE workspace; describe() tells you which.
client = esoul.Esoul()

info = client.dispatch.describe()
workspace_id = info.session.workspace_ids[0]      # your sandbox's workspace

# Discover what's in the workspace BEFORE mutating it:
result = client.workspaces.apps.list(
    workspace_id=workspace_id,
    include_state=True,                           # embed each app's live state
)
for app in result.apps:
    print(app.node_id, app.application_type, app.instance_name)
    print("  state:", app.state)                  # full materialised view

# Mutate any app you found by passing its node_id to dispatch.event(...):
client.dispatch.event(
    app_id=result.apps[0].node_id,
    event_name="…",
    event_data={…},
)
```

That's it. **You do not need a PAT, an API key, or any environment variable.** The orientation flow is always:
1. `client = esoul.Esoul()`
2. `info = client.dispatch.describe()` → tells you `info.session.workspace_ids`
3. `client.workspaces.apps.list(workspace_id=..., include_state=True)` → enumerates every app + state
4. `client.dispatch.event(...)` or `client.workspaces.apps.create / rename / delete` to mutate

The rest of this document is the catalogue of every event you can dispatch.

---

## Table of contents

1. [Install](#install)
2. [Quickstart](#quickstart)
3. [Authentication](#authentication)
4. [Core concepts](#core-concepts)
5. [Client surface](#client-surface)
6. [Errors](#errors)
7. [Workspace management](#workspace-management)
8. [Workspace-level events](#workspace-level-events)
9. [Per-app event catalogue](#per-app-event-catalogue)
   - [Spreadsheet](#spreadsheet)
   - [Notes](#notes)
   - [Slideshow](#slideshow)
   - [Todo](#todo)
   - [Kanban board](#kanban-board)
   - [Calendar](#calendar)
   - [Contacts](#contacts)
   - [Email](#email)
   - [Image viewer](#image-viewer)
   - [PDF viewer](#pdf-viewer)
   - [Text editor](#text-editor)
   - [TLDraw canvas](#tldraw-canvas)
   - [E2B sandbox](#e2b-sandbox)
   - [Agent builder](#agent-builder)
   - [Search engine](#search-engine)
10. [Drive](#drive)
11. [Cookbook](#cookbook)
12. [Performance + idempotency](#performance--idempotency)

---

## Install

```bash
pip install esoul
```

Python 3.9+. The SDK ships with type hints (`py.typed`) and works in both sync and async contexts.

---

## Quickstart

```python
import esoul

# Auto-detects credentials in this order:
#   1. ESOUL_TOKEN env var
#   2. /var/run/esoul/token  (set inside a sandbox)
#   3. ~/.config/esoul/credentials
client = esoul.Esoul()

# Or pass them explicitly:
client = esoul.Esoul(
    token="esoul_pat_…",
    base_url="https://externalsoul.com",
)

# Describe what this session can do:
info = client.dispatch.describe()
print(info.session.kind, info.session.workspace_ids)
for ns, ns_info in info.namespaces.items():
    print(ns, len(ns_info.events), "events")

# Dispatch one event onto a spreadsheet app:
result = client.dispatch.event(
    app_id="OZM7Iv9n1dtUHYKL5Wxp7",      # the spreadsheet's nodeId
    event_name="spreadsheet_add_row",
    event_data={
        "sheetId": "abc123",
        "cells": {"col-name": "Acme Co.", "col-email": "hello@acme.com"},
    },
)
print(result.event_id, result.sequence_num)
```

Every mutation goes through `dispatch.event`. There is no special-case API for "add a row" or "set a cell" — you just dispatch the right `event_name` with the right `event_data` shape. This entire document is the catalogue of those names + shapes.

---

## Authentication

### Personal Access Tokens (PATs)

The most common way to use the SDK from a script or notebook. Generate one in the workspace UI:

> User menu (avatar, top-right) → **Access Token** → Generate.

The token is scoped to the workspace it was minted in. Format:

```
esoul_pat_<sessionId>.<base64hmac>
```

Pass it explicitly:

```python
client = esoul.Esoul(token="esoul_pat_…")
```

Or set `ESOUL_TOKEN` in your environment. **Never commit a token to git.**

### Sandbox JWTs

Inside an ExternalSoul-managed sandbox, the SDK auto-detects a short-lived JWT at `/var/run/esoul/token` (mode `0400`, owned by the sandbox user). The token refreshes every ~4 minutes; the SDK handles refresh transparently.

### What a session can do

A `describe()` call returns the session shape:

```python
info = client.dispatch.describe()
info.session.kind            # "pat" or "sandbox"
info.session.user_id         # the principal
info.session.workspace_ids   # ["ws_abc", "ws_def"] — the set this token can mutate
info.session.expires_at      # ISO 8601 or None
```

Workspace isolation is **structural**, not a permission check: a PAT minted for workspace A cannot mutate workspace B even if you know workspace B's app ids. The dispatch endpoint returns `404 app_not_found` (not 403) when you reference an app outside `session.workspace_ids`.

---

## Core concepts

### Events are the truth

Every state change is a typed event on a per-workspace timeline. The SDK never writes to Prisma directly. When you dispatch `spreadsheet_add_row`, the server:

1. Validates the event against the app's schema (Zod).
2. Stamps an actor (`{kind: "sandbox", userId, sessionId, workspaceId, sandboxId?}`).
3. `appendEvent` writes a `WorkspaceEvent` row and updates the per-branch `headEventSeq`.
4. The event's reducer runs against the current `WorkspaceApplication.state` and writes the new state column.
5. Realtime broadcasts go out to any browser viewing the workspace.

This is the same path a human user typing in the UI, or an agent calling a tool, takes. The audit trail is not a side effect — the audit trail **is** the system.

### `appId` addresses an app instance

`appId` is the app's **graph nodeId** (`WorkspaceApplication.nodeId`), not the row's primary key. It's a short string (typically 21 chars, generated by `nanoid()` at app creation). You discover ids via `dispatch.describe()`, `dispatch.read_state()`, or by reading the URL of an app in the browser.

```python
# Get the live state of an app (no replay, ~5 ms):
state = client.dispatch.read_state(app_id="OZM7Iv9n1dtUHYKL5Wxp7")
print(state.state)        # the app's current data
print(state.version)      # the branch's headEventSeq at read time
```

### Client-side id pre-generation

Many events create new entities (rows, slides, tabs, etc.). To let scripts chain operations without round-trips, **most create-events accept a client-generated id** in `event_data`. The SDK examples below show the pattern; the rule of thumb is:

- For rows: `rowId` is optional but recommended (`str(uuid.uuid4())`).
- For sheets / slides / tabs / columns: id field is optional; if you want to use the new id immediately, generate it client-side and pass it.

If you don't pass an id, the server's `dataCreator` generates one — but you won't see it in the response until a future read.

### Idempotency

Every `dispatch.event` and `dispatch.batch` call accepts an optional `idempotency_key`. By default the SDK auto-generates a UUID v4 per call and reuses it on internal retries (so transient 5xx never produces duplicate rows). You can pass an explicit string when your script has its own retry strategy:

```python
client.dispatch.event(
    app_id=app_id,
    event_name="spreadsheet_add_rows",
    event_data={"sheetId": sheet_id, "rows": rows},
    idempotency_key="nightly-import-2026-05-15-batch-7",
)
```

Server-side, `(sessionId, idempotency_key)` maps to the cached response for 24 hours. Same key + same body → cached response. Same key + different body → 409 `idempotency_conflict`.

### `event_data` field names use camelCase

The platform's TypeScript schemas are camelCase; the SDK passes `event_data` straight through. So your Python dict keys must be camelCase:

```python
# Correct:
event_data={"sheetId": sheet_id, "rowId": row_id, "cells": {...}}

# Wrong — server will reject with schema_validation_error:
event_data={"sheet_id": sheet_id, "row_id": row_id, "cells": {...}}
```

---

## Client surface

### `esoul.Esoul`

```python
client = esoul.Esoul(
    token: Optional[str] = None,        # falls back to env / sandbox file / config
    base_url: str = "https://externalsoul.com",
    timeout: float = 30.0,              # HTTP timeout in seconds
    retries: int = 3,                   # transient 5xx retries with backoff
)
```

Resources:

- `client.dispatch.event(...)` — dispatch one event
- `client.dispatch.batch(events, ...)` — dispatch up to 100 events
- `client.dispatch.read_state(app_id=...)` — read live app state
- `client.dispatch.describe()` — session + registered apps/events
- `client.drive.list(...)`, `client.drive.read(...)`, `client.drive.upload(...)` — Drive proxy

### `esoul.AsyncEsoul`

Same surface, async. Use inside `asyncio` programs:

```python
async with esoul.AsyncEsoul(token=…) as client:
    info = await client.dispatch.describe()
    result = await client.dispatch.event(app_id=…, event_name=…, event_data=…)
```

### Return shapes

```python
@dataclass
class DispatchResult:
    event_id: str           # the WorkspaceEvent row id
    sequence_num: int       # per-branch monotonic counter
    dispatched_at: str      # ISO 8601 UTC

@dataclass
class BatchResult:
    results: list[DispatchResult]

@dataclass
class ReadStateResult:
    state: dict             # the materialised view
    version: int            # branch headEventSeq
    updated_at: str
```

### `dispatch.event`

```python
client.dispatch.event(
    *,
    app_id: str,
    event_name: str,
    event_data: dict,
    idempotency_key: Optional[str] = None,
) -> DispatchResult
```

Three-step server-side authz:
1. Token validity + session not revoked.
2. `app.workspaceId` ∈ `session.workspace_ids`.
3. Event registered for `app.applicationType`.

After the row is committed, realtime publish + trigger fan-out fire in `waitUntil` so the response returns within ~700 ms steady-state.

### `dispatch.batch`

```python
client.dispatch.batch(
    events: Sequence[BatchEvent],   # up to 100
    *,
    idempotency_key: Optional[str] = None,
) -> BatchResult

@dataclass
class BatchEvent:
    app_id: str
    event_name: str
    event_data: dict
```

Validation is upfront-all-or-nothing: if any event fails authz / registration / shape, **nothing is written**. After validation, events are dispatched in order; the response is the per-event `DispatchResult` array in input order.

Use a batch when you have multiple mutations that should arrive "together" in one HTTP round-trip. For pure throughput, a single bulk event (`spreadsheet_add_rows` with 1000 rows in one event) beats a batch of 1000 single-row events.

---

## Errors

```python
import esoul

try:
    client.dispatch.event(...)
except esoul.exceptions.AuthError:
    # 401 — token invalid / expired / revoked
    ...
except esoul.exceptions.WorkspaceAccessDenied:
    # The session can't reach the workspace this app lives in
    ...
except esoul.exceptions.NotFoundError as e:
    # 404 — app_not_found, event_not_registered, etc.
    print(e.code, e.detail)
except esoul.exceptions.InvalidRequest as e:
    # 400 — schema_validation_error, malformed body
    print(e.code, e.detail)
except esoul.exceptions.IdempotencyConflict as e:
    # 409 — key reused with different body
    print(e.detail)
except esoul.exceptions.RateLimitError as e:
    # 429 — back off and retry
    ...
except esoul.exceptions.DriveNotConnected:
    # The workspace has no Google Drive connection
    ...
except esoul.exceptions.APIError as e:
    # catch-all for any server-side error
    print(e.status_code, e.code, e.detail)
```

All exceptions inherit from `esoul.exceptions.EsoulError`. The `code` field is the server's machine-readable error code (e.g. `"event_not_registered"`); `detail` is a dict with extra context (offending field, expected type, etc.).

---

## Workspace management

Workspace lifecycle (list / create / rename / delete apps) lives on the `client.workspaces.apps.*` resource. Every mutation writes a real timeline event (`workspace/add_app`, `workspace/rename_app`, `workspace/remove_app`) AND fans out via Inngest Realtime to connected browsers, so the change appears in any open tab within ~100-300 ms — same liveness contract as a human clicking the UI button.

### Discover apps in a workspace

The orientation primitive — call this FIRST when a script/agent enters a workspace:

```python
result = client.workspaces.apps.list(
    workspace_id="ws_abc",
    include_state=True,                       # embed each app's live state
    types=["spreadsheet", "note_editor"],     # optional applicationType filter
)
print(result.workspace_id)
for app in result.apps:
    print(app.node_id, app.application_type, app.instance_name)
    print("  created_at:", app.created_at)
    if app.application_type == "spreadsheet":
        print("  sheets:", len(app.state["sheets"]))
```

Returns `ListAppsResult(workspace_id, apps: List[AppInstance])` where each `AppInstance` carries `node_id`, `application_type`, `instance_name`, `created_at`, `updated_at`, and (when `include_state=True`) `state` — the full materialised view.

`include_state=True` is the right default when an agent is orienting itself ("what's in this workspace and what does it look like?"). For long-lived scripts that already know the app shape and only need ids, leave it `False` for a smaller response.

### Create an app

```python
app = client.workspaces.apps.create(
    workspace_id="ws_abc",
    application_type="spreadsheet",
    instance_name="Results",
)
print(app.node_id)             # the new app's nodeId — use it in dispatch.event
print(app.application_type)
print(app.instance_name)
print(app.created_at)
```

`application_type` must be one of the registered apps. The complete list:

| `application_type` | Doc anchor |
|---|---|
| `"spreadsheet"` | [Spreadsheet](#spreadsheet) |
| `"note_editor"` | [Notes](#notes) |
| `"slideshow"` | [Slideshow](#slideshow) |
| `"todo_app"` | [Todo](#todo) |
| `"kanban_board"` | [Kanban board](#kanban-board) |
| `"calendar"` | [Calendar](#calendar) |
| `"contacts_viewer"` | [Contacts](#contacts) |
| `"email_viewer"` | [Email](#email) |
| `"image_viewer"` | [Image viewer](#image-viewer) |
| `"pdf_viewer"` | [PDF viewer](#pdf-viewer) |
| `"text_editor"` | [Text editor](#text-editor) |
| `"tldraw_canvas"` | [TLDraw canvas](#tldraw-canvas) |
| `"e2b_sandbox"` | [E2B sandbox](#e2b-sandbox) |
| `"agent_builder"` | [Agent builder](#agent-builder) |
| `"search_engine"` | [Search engine](#search-engine) |

`initial_state` is optional — when omitted the platform uses each app's schema default. Pass it when you want to skip a follow-up dispatch round-trip to set up sheets / columns / tabs / etc.

### Rename an app

```python
renamed = client.workspaces.apps.rename(
    workspace_id="ws_abc",
    node_id=app.node_id,
    new_name="Q2 results",
)
print(renamed.old_name, "→", renamed.new_name)
```

Updates `instanceName` on the `WorkspaceApplication` row + mirrors the new name into the workspace graph node's `data.label` so sidebars / canvases pick it up live.

### Delete an app

```python
removed = client.workspaces.apps.delete(
    workspace_id="ws_abc",
    node_id=app.node_id,
)
```

Writes a `workspace/remove_app` event, cascade-deletes `ApplicationPort` rows + the `WorkspaceApplication` row, and strips the node + connected edges from the workspace graph. **The timeline rows referencing the removed app stay intact** (the audit invariant) — scrubbing past the delete in the UI cleanly replays prior events against the now-empty reducer.

### End-to-end pattern: stand up a workspace from scratch

```python
import esoul

with esoul.Esoul() as client:
    # Create the apps you need:
    leads = client.workspaces.apps.create(
        workspace_id="ws_abc",
        application_type="spreadsheet",
        instance_name="Leads",
    )
    brief = client.workspaces.apps.create(
        workspace_id="ws_abc",
        application_type="note_editor",
        instance_name="Project brief",
    )
    today = client.workspaces.apps.create(
        workspace_id="ws_abc",
        application_type="todo_app",
        instance_name="Today",
    )

    # Immediately mutate them via dispatch.event — the new node_ids are
    # already addressable (read-after-write is consistent: appendEvent
    # writes both the timeline row AND the state cache synchronously).
    client.dispatch.event(
        app_id=brief.node_id,
        event_name="note_update_content",
        event_data={"tabName": "Default", "content": "# Brief\n\n..."},
    )
```

### Idempotency

All three operations accept the same `idempotency_key` kwarg as `dispatch.event` (auto-generated UUID v4 if omitted). The SDK retries 5xx/network errors with the same key so duplicates are server-deduplicated. For a script-driven setup you can run multiple times safely, pass deterministic keys:

```python
client.workspaces.apps.create(
    workspace_id="ws_abc",
    application_type="spreadsheet",
    instance_name="Leads",
    idempotency_key="setup-2026-05-15-create-leads",
)
```

### Realtime: how the change reaches the browser

For each operation the server:
1. Writes the event row + mutates state inside one Prisma transaction.
2. Publishes on the workspace channel (`agentBuilderWorkspaceChannel.workspace_events`) — any browser viewing this workspace receives it.
3. Publishes on the user channel (`userChannel.workspace_event` with `kind: "add"` / `"remove"` / `"rename"`) — tabs that have the workspace loaded but aren't currently viewing it ALSO update.
4. Returns the HTTP response (steps 2 and 3 run in `waitUntil` so the caller is unblocked).

Both broadcasts are non-fatal — a transient broker stall doesn't roll back the DB transaction. If realtime drops, the next browser refresh / workspace navigation lands canonical state from the DB.

---

## Workspace-level events

These events shape the workspace itself. They are emitted server-side (by the agent runtime, the human UI, the move-app tool, the file-upload pipeline) and replayed by every reducer that touches workspace state. **The SDK doesn't dispatch them today** — see [Workspace management (gap)](#workspace-management-gap) — but they're documented here so you understand the events you'll see on the timeline, in trigger payloads, and in `read_state` reads.

### `workspace/add_app`
Adds a new app instance to the workspace.

```ts
event_data: {
  applicationType: string,    // "spreadsheet" | "notes" | … (see catalogue below)
  instanceName: string,       // human-readable label
  nodeId?: string,            // generated if omitted
  state?: object,             // initial state (defaults from the app's schema)
  ports?: object[],           // input/output ports for graph wiring
}
```

### `workspace/remove_app`
Deletes an app and its events / state from the workspace.

```ts
event_data: { instanceId: string }   // == nodeId
```

### `workspace/rename_app`
Changes an app's display name.

```ts
event_data: { instanceId: string, newName: string }
```

### `workspace/pin_app` / `workspace/unpin_app`
Adds / removes from the workspace sidebar's pinned list.

```ts
event_data: { instanceId: string }
```

### `workspace/focus_app`
Maximises an app in the workspace UI.

```ts
event_data: { nodeId: string }
```

### `workspace/move_app_in` / `workspace/move_app_out`
Paired events that move an app between workspaces (the pinned-agent `move_app_to_workspace` primitive).

```ts
move_app_in.event_data: {
  nodeId: string,
  applicationType: string,
  instanceName: string,
  state?: object,
  ports?: object[],
}
move_app_out.event_data: {
  nodeId: string,
  instanceName: string,
  applicationType: string,
  destWorkspaceId: string,
  destNodeId: string,
  eventCountMoved: number,
}
```

### `workspace/file_added` / `workspace/file_deleted` / `workspace/file_renamed`
Workspace file lifecycle. The SDK's `client.drive.upload(...)` fires `file_added` server-side; deletion / rename of `WorkspaceFile`s currently lives on the UI side.

```ts
file_added.event_data:    { file: WorkspaceFile }   // {id, name, mimeType, blobPath, …}
file_deleted.event_data:  { fileId: string }
file_renamed.event_data:  { fileId: string, newName: string }
```

---

## Per-app event catalogue

Every event below can be dispatched via `client.dispatch.event(app_id=…, event_name=…, event_data=…)`. The `app_id` must be the nodeId of an existing app whose `applicationType` matches the section heading.

The full list of `applicationType` strings is also returned in `dispatch.describe().namespaces`.

---

### Spreadsheet

`applicationType: "spreadsheet"`

A spreadsheet is a list of **sheets**, each with **columns** (typed) and **rows** (stable ids, cell values keyed by column id).

#### Discovery

```python
state = client.dispatch.read_state(app_id=spreadsheet_app_id).state
for sheet in state["sheets"]:
    print(sheet["id"], sheet["name"], len(sheet["columns"]), "cols",
                                       len(sheet["rows"]), "rows")
    for col in sheet["columns"]:
        print("  ", col["id"], col["name"], col["type"])
```

#### Sheet lifecycle

##### `spreadsheet_add_sheet`
```python
import secrets
def nanoid(n=21):
    alpha = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    return "".join(secrets.choice(alpha) for _ in range(n))

sheet_id = nanoid()
column_id = nanoid()
client.dispatch.event(
    app_id=spreadsheet_app_id,
    event_name="spreadsheet_add_sheet",
    event_data={
        "name": "Results",
        "sheetId": sheet_id,        # optional but recommended
        "columnId": column_id,      # optional id for the default column
    },
)
```

##### `spreadsheet_rename_sheet`
```python
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_rename_sheet",
    event_data={"sheetId": sheet_id, "newName": "Q2 results"},
)
```

##### `spreadsheet_delete_sheet`
```python
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_delete_sheet",
    event_data={"sheetId": sheet_id},
)
```
Never deletes the last remaining sheet.

##### `spreadsheet_select_sheet`
```python
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_select_sheet",
    event_data={"sheetId": sheet_id},
)
```

##### `spreadsheet_reorder_sheets`
```python
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_reorder_sheets",
    event_data={"orderedIds": [sheet_a_id, sheet_c_id, sheet_b_id]},
)
```
Must be an exact permutation of existing sheet ids.

#### Columns

##### `spreadsheet_add_column`
```python
col_id = nanoid()
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_add_column",
    event_data={
        "sheetId": sheet_id,
        "columnId": col_id,
        "name": "Status",
        "type": "status",                          # optional, see ColumnType below
        "options": ["new", "in-progress", "done"], # optional, for status type
        "afterColumnId": existing_col_id,          # optional, controls insertion order
    },
)
```

ColumnType values: `"text"` (default), `"number"`, `"email"`, `"url"`, `"date"`, `"status"`, `"checkbox"`, `"contact_ref"`, `"email_thread_ref"`, `"workspace_ref"`.

##### `spreadsheet_rename_column`
```python
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_rename_column",
    event_data={"sheetId": sheet_id, "columnId": col_id, "newName": "Stage"},
)
```

##### `spreadsheet_set_column_type`
```python
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_set_column_type",
    event_data={
        "sheetId": sheet_id, "columnId": col_id,
        "type": "status", "options": ["draft", "review", "approved"],
    },
)
```

##### `spreadsheet_resize_column`
```python
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_resize_column",
    event_data={"sheetId": sheet_id, "columnId": col_id, "width": 220},
)
```
Width clamped to 50–800. Collapsible (500 ms window per column).

##### `spreadsheet_delete_column`
```python
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_delete_column",
    event_data={"sheetId": sheet_id, "columnId": col_id},
)
```
Cells in the deleted column are removed from every row; never deletes the last column.

##### `spreadsheet_reorder_columns`
```python
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_reorder_columns",
    event_data={"sheetId": sheet_id, "orderedIds": [col_b, col_a, col_c]},
)
```

#### Rows

##### `spreadsheet_add_row`
```python
import uuid
row_id = str(uuid.uuid4())
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_add_row",
    event_data={
        "sheetId": sheet_id,
        "rowId": row_id,
        "cells": {col_name_id: "Acme Co.", col_email_id: "hello@acme.com"},
    },
)
```

##### `spreadsheet_add_rows`  (bulk — the hot path)
One event, one DB write, one realtime broadcast. Use this for ≥10 rows.

```python
rows = [
    {"rowId": str(uuid.uuid4()),
     "cells": {col_name: f"Vendor {i}", col_status: "pending"}}
    for i in range(1000)
]
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_add_rows",
    event_data={"sheetId": sheet_id, "rows": rows},
)
# 1000 rows lands in ~1.7 s end-to-end (HTTP + DB + state cache + publish).
```

##### `spreadsheet_update_row`
```python
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_update_row",
    event_data={
        "sheetId": sheet_id, "rowId": row_id,
        "cells": {col_status: "complete", col_notes: "Verified."},
    },
)
```
Shallow-merges `cells` into the existing row — only the keys you provide are touched.

##### `spreadsheet_delete_row` / `spreadsheet_delete_rows`
```python
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_delete_row",
    event_data={"sheetId": sheet_id, "rowId": row_id},
)
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_delete_rows",
    event_data={"sheetId": sheet_id, "rowIds": [row_a, row_b, row_c]},
)
```

##### `spreadsheet_reorder_rows`
```python
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_reorder_rows",
    event_data={"sheetId": sheet_id, "orderedIds": [...all row ids in new order]},
)
```

#### Cells

##### `spreadsheet_set_cell`
```python
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_set_cell",
    event_data={
        "sheetId": sheet_id, "rowId": row_id, "columnId": col_id,
        "value": "running",
    },
)
```
Collapsible (2 s window per row × column). Pass `"value": ""` to clear.

##### `spreadsheet_set_cell_format`
```python
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_set_cell_format",
    event_data={
        "sheetId": sheet_id, "rowId": row_id, "columnId": col_id,
        "format": {
            "bold": True, "italic": False,
            "fontSize": 14, "fontColor": "#ffffff",
            "backgroundColor": "#1e3a8a", "textAlign": "right",
        },
    },
)
```
Pass `"format": None` to clear formatting on that cell.

##### `spreadsheet_set_range_format`
```python
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_set_range_format",
    event_data={
        "sheetId": sheet_id,
        "rowIds": [row_a, row_b],
        "columnIds": [col_a, col_b],
        "format": {"backgroundColor": "#fef3c7"},
    },
)
```

##### `spreadsheet_clear_range`
```python
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_clear_range",
    event_data={"sheetId": sheet_id,
                "rowIds": [row_a, row_b], "columnIds": [col_a, col_b]},
)
```

#### File integration

##### `spreadsheet_load_file`
```python
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_load_file",
    event_data={
        "workspaceFileId": file_id,
        "fileName": "leads.csv",
        "sheetName": "Imported leads",
        "columns": [{"id": col_a, "name": "Name", "type": "text"}, …],
        "rows": [{"rowId": str(uuid.uuid4()),
                  "cells": {col_a: "Acme"}}, …],
    },
)
```

##### `spreadsheet_import_csv`
```python
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_import_csv",
    event_data={
        "sheetId": sheet_id,                # optional; creates new sheet if omitted
        "sheetName": "From CSV",
        "csvText": "name,email\nAcme,hi@acme.com\n",
    },
)
```

##### `spreadsheet_save_completed`
Marks the spreadsheet as saved at a specific version (used by the autosave loop).

```python
client.dispatch.event(
    app_id=spreadsheet_app_id, event_name="spreadsheet_save_completed",
    event_data={"version": state.version, "savedAt": int(time.time() * 1000)},
)
```

---

### Notes

`applicationType: "note_editor"`

Notes is a multi-tab markdown editor. Each tab has a name, content, and optional per-tab background / font colours for light + dark themes.

#### `note_create_tab`
```python
client.dispatch.event(
    app_id=notes_app_id, event_name="note_create_tab",
    event_data={"tabName": "Brief"},
)
```

#### `note_rename_tab`
```python
client.dispatch.event(
    app_id=notes_app_id, event_name="note_rename_tab",
    event_data={"tabName": "Brief", "newName": "Project brief"},
)
```

#### `note_delete_tab`
```python
client.dispatch.event(
    app_id=notes_app_id, event_name="note_delete_tab",
    event_data={"tabName": "Brief"},
)
```
Never deletes the last remaining tab.

#### `note_update_content`
```python
client.dispatch.event(
    app_id=notes_app_id, event_name="note_update_content",
    event_data={
        "tabName": "Project brief",
        "content": "# Overview\n\nLorem ipsum.\n",
    },
)
```
Collapsible (2 s window per app × tab). If the tab name doesn't exist, the reducer creates it.

#### Colours

```python
client.dispatch.event(
    app_id=notes_app_id, event_name="note_set_tab_color",
    event_data={"tabName": "Project brief", "color": "#1e1b4b"},
)
# Also: note_set_tab_font_color, note_set_tab_light_color,
#       note_set_tab_light_font_color — each takes {tabName, color}.
```

#### `note_select_tab`
```python
client.dispatch.event(
    app_id=notes_app_id, event_name="note_select_tab",
    event_data={"tabName": "Project brief"},
)
```

#### `note_reorder_tabs`
```python
client.dispatch.event(
    app_id=notes_app_id, event_name="note_reorder_tabs",
    event_data={"orderedIds": [tab_a_id, tab_c_id, tab_b_id]},
)
```

#### `note_set_preview_mode`
```python
client.dispatch.event(
    app_id=notes_app_id, event_name="note_set_preview_mode",
    event_data={"previewMode": "live"},   # "edit" | "live" | "preview"
)
```

---

### Slideshow

`applicationType: "slideshow"`

Each slide is an agent-authored TSX component, rendered in-browser via `@babel/standalone`. Edits flow through `slide_apply_edits` (preferred — atomic find/replace pairs) or `slide_replace_source` (escape hatch — full rewrite).

#### `slide_create`
```python
client.dispatch.event(
    app_id=slideshow_app_id, event_name="slide_create",
    event_data={
        "slideName": "Title",
        "tsxSource": "export default function Slide() {\n  return <h1>Hello</h1>;\n}",
    },
)
```

#### `slide_apply_edits`  (preferred)
Atomic find/replace pairs. The reducer applies each in order; if any `find` doesn't match exactly, the whole edit fails and `errorMessage` is set on the slide.

```python
client.dispatch.event(
    app_id=slideshow_app_id, event_name="slide_apply_edits",
    event_data={
        "slide": "Title",               # accepts id or name
        "edits": [
            {"find": "<h1>Hello</h1>", "replace": "<h1>Welcome</h1>"},
            {"find": "function Slide()", "replace": "function TitleSlide()"},
        ],
    },
)
```
Collapsible (1.5 s window per app × slide).

#### `slide_replace_source`
```python
client.dispatch.event(
    app_id=slideshow_app_id, event_name="slide_replace_source",
    event_data={
        "slide": slide_id_or_name,
        "tsxSource": "export default function Slide() {\n  return <div>…</div>;\n}",
    },
)
```

#### `slide_rename` / `slide_delete` / `slide_select`
```python
client.dispatch.event(app_id=slideshow_app_id, event_name="slide_rename",
    event_data={"slide": slide_id, "newName": "Intro"})

client.dispatch.event(app_id=slideshow_app_id, event_name="slide_delete",
    event_data={"slide": slide_id})

client.dispatch.event(app_id=slideshow_app_id, event_name="slide_select",
    event_data={"slide": slide_id})
```

#### `slide_reorder`
```python
client.dispatch.event(
    app_id=slideshow_app_id, event_name="slide_reorder",
    event_data={"orderedSlides": [s1, s3, s2]},   # accepts ids or names
)
```

#### `slideshow_set_presentation_mode`
```python
client.dispatch.event(
    app_id=slideshow_app_id, event_name="slideshow_set_presentation_mode",
    event_data={"enabled": True},
)
```

---

### Todo

`applicationType: "todo_app"`

Todo is a multi-list checklist. Items live in lists; both have stable ids.

#### `create_list`
```python
client.dispatch.event(
    app_id=todo_app_id, event_name="create_list",
    event_data={"listName": "Today", "listId": nanoid()},
)
```

#### `delete_list`
```python
client.dispatch.event(
    app_id=todo_app_id, event_name="delete_list",
    event_data={"listName": "Today"},
)
```

#### `todo_add_item`
```python
client.dispatch.event(
    app_id=todo_app_id, event_name="todo_add_item",
    event_data={
        "listName": "Today",
        "text": "Buy bread",
        "itemId": str(uuid.uuid4()),    # optional
    },
)
```

#### `todo_delete_item` / `todo_rename_item` / `todo_toggle_item_completed`
```python
client.dispatch.event(app_id=todo_app_id, event_name="todo_delete_item",
    event_data={"listName": "Today", "itemId": item_id})

client.dispatch.event(app_id=todo_app_id, event_name="todo_rename_item",
    event_data={"listName": "Today", "itemId": item_id,
                "itemText": "Buy bread", "newText": "Buy sourdough bread"})

client.dispatch.event(app_id=todo_app_id, event_name="todo_toggle_item_completed",
    event_data={"listName": "Today", "itemId": item_id,
                "text": "Buy sourdough bread", "isCompleted": True})
```

#### `todo_move_item`
```python
client.dispatch.event(
    app_id=todo_app_id, event_name="todo_move_item",
    event_data={
        "listName": "Today", "itemId": item_id, "text": "Buy bread",
        "newListName": "Tomorrow",
    },
)
```

#### `todo_reorder_item`
```python
client.dispatch.event(
    app_id=todo_app_id, event_name="todo_reorder_item",
    event_data={"listName": "Today",
                "orderedIds": [item_b, item_a, item_c]},
)
```

---

### Kanban board

`applicationType: "kanban_board"`

Two-tier hierarchy: **lists** (e.g. "Sprint 1") contain **sub-lists / cards** ("Backlog", "Doing", "Done"), and cards contain **items**.

#### Lists

```python
client.dispatch.event(app_id=kb_app_id, event_name="kanban_create_list",
    event_data={"listName": "Sprint 1", "listId": nanoid()})

client.dispatch.event(app_id=kb_app_id, event_name="kanban_rename_list",
    event_data={"oldName": "Sprint 1", "newName": "Sprint 1 — May"})

client.dispatch.event(app_id=kb_app_id, event_name="kanban_delete_list",
    event_data={"listName": "Sprint 1 — May"})
```

#### Cards (sub-lists)

```python
client.dispatch.event(app_id=kb_app_id, event_name="kanban_create_sub_list",
    event_data={"parentListName": "Sprint 1", "subListName": "Backlog",
                "subListId": nanoid()})

client.dispatch.event(app_id=kb_app_id, event_name="kanban_rename_sub_list",
    event_data={"parentListName": "Sprint 1", "subListId": card_id,
                "newName": "Todo"})

client.dispatch.event(app_id=kb_app_id, event_name="kanban_delete_sub_list",
    event_data={"parentListName": "Sprint 1", "subListId": card_id})

client.dispatch.event(app_id=kb_app_id, event_name="kanban_set_sub_list_color",
    event_data={"parentListName": "Sprint 1", "subListId": card_id,
                "color": "#fde68a"})

client.dispatch.event(app_id=kb_app_id, event_name="kanban_update_sub_list_config",
    event_data={"parentListName": "Sprint 1", "subListId": card_id,
                "maxItems": 5})

client.dispatch.event(app_id=kb_app_id, event_name="kanban_reorder_sub_list",
    event_data={"parentListName": "Sprint 1",
                "orderedIds": [card_b, card_a, card_c]})
```

#### Items

```python
client.dispatch.event(app_id=kb_app_id, event_name="kanban_add_item",
    event_data={"parentListName": "Sprint 1", "subListId": card_id,
                "text": "Land idempotency", "itemId": str(uuid.uuid4())})

client.dispatch.event(app_id=kb_app_id, event_name="kanban_delete_item",
    event_data={"parentListName": "Sprint 1", "subListId": card_id,
                "itemId": item_id})

client.dispatch.event(app_id=kb_app_id, event_name="kanban_move_item",
    event_data={"fromSubListId": doing_card, "toSubListId": done_card,
                "itemId": item_id})
```

---

### Calendar

`applicationType: "calendar"`

Workspace calendar with optional Google sync (`calendar_external_*` events are emitted by the sync engine; user scripts typically only dispatch the local-event mutations).

#### `calendar_create_event`
```python
client.dispatch.event(
    app_id=cal_app_id, event_name="calendar_create_event",
    event_data={
        "title": "Sprint review",
        "startTime": 1715842800000,           # ms epoch
        "endTime": 1715846400000,
        "description": "Demos + retrospective",
        "eventId": str(uuid.uuid4()),         # optional
    },
)
```

#### `calendar_update_event`
```python
client.dispatch.event(
    app_id=cal_app_id, event_name="calendar_update_event",
    event_data={
        "eventId": event_id,
        "title": "Sprint review (rescheduled)",
        "startTime": 1716447600000,
    },
)
```

#### `calendar_delete_event`
```python
client.dispatch.event(
    app_id=cal_app_id, event_name="calendar_delete_event",
    event_data={"eventId": event_id},
)
```

#### View / selection (UI-only)
```python
client.dispatch.event(app_id=cal_app_id, event_name="calendar_select_date",
    event_data={"date": 1715760000000})

client.dispatch.event(app_id=cal_app_id, event_name="calendar_change_view",
    event_data={"viewMode": "week"})        # "day" | "week" | "month"
```

#### Pending request flow (used by Calendly-style booking)
```python
client.dispatch.event(app_id=cal_app_id, event_name="calendar_request_event",
    event_data={"requestId": req_id, "title": "Coffee chat",
                "description": "30 min, Tuesday afternoon"})

client.dispatch.event(app_id=cal_app_id, event_name="calendar_decide_pending",
    event_data={"requestId": req_id, "approved": True})

client.dispatch.event(app_id=cal_app_id, event_name="calendar_dismiss_execution",
    event_data={"executionId": exec_id})
```

#### Triggers (automation)
```python
client.dispatch.event(app_id=cal_app_id, event_name="calendar_create_trigger",
    event_data={"name": "Daily review", "eventPattern": {...},
                "actions": ["send_email"]})

client.dispatch.event(app_id=cal_app_id, event_name="calendar_toggle_trigger",
    event_data={"triggerId": tr_id, "enabled": False})

client.dispatch.event(app_id=cal_app_id, event_name="calendar_delete_trigger",
    event_data={"triggerId": tr_id})
```

#### External (Google) sync — server-emitted
`calendar_external_events_synced`, `calendar_external_sync_error`, `calendar_external_link`, `calendar_external_unlink`. Listed here for completeness; user scripts don't dispatch these.

---

### Contacts

`applicationType: "contacts_viewer"`

Workspace contact directory. Contacts live in `contacts`; named groups live in `lists` and reference contacts by id.

#### `contact_added_by_agent`
```python
client.dispatch.event(
    app_id=contacts_app_id, event_name="contact_added_by_agent",
    event_data={
        "name": "Jane Park",
        "email": "jane@acme.com",
        "phone": "+1-415-555-0143",
        "contactId": str(uuid.uuid4()),
    },
)
```

#### `contact_updated` / `contact_removed`
```python
client.dispatch.event(app_id=contacts_app_id, event_name="contact_updated",
    event_data={"contactId": cid, "phone": "+1-415-555-0177"})

client.dispatch.event(app_id=contacts_app_id, event_name="contact_removed",
    event_data={"contactId": cid})
```

#### Lists
```python
client.dispatch.event(app_id=contacts_app_id, event_name="contact_list_created",
    event_data={"listName": "Q2 outreach", "listId": nanoid()})

client.dispatch.event(app_id=contacts_app_id, event_name="contact_added_to_list",
    event_data={"contactId": cid, "listId": list_id})

client.dispatch.event(app_id=contacts_app_id, event_name="contact_removed_from_list",
    event_data={"contactId": cid, "listId": list_id})

client.dispatch.event(app_id=contacts_app_id, event_name="contact_list_deleted",
    event_data={"listId": list_id})
```

#### View state
```python
client.dispatch.event(app_id=contacts_app_id, event_name="contacts_view_change",
    event_data={"viewMode": "detail", "selectedContactId": cid})
```

---

### Email

`applicationType: "email_viewer"`

Gmail inbox surface. Composition / sending go through the SDK; archive / star / trash / read are paired forward+inverse events for clean scrubbing.

#### View state
```python
client.dispatch.event(app_id=email_app_id, event_name="email_view_change",
    event_data={"viewMode": "thread", "selectedThreadId": thread_id})

client.dispatch.event(app_id=email_app_id, event_name="email_body_fetched",
    event_data={"messageId": mid, "body": "<html>…</html>",
                "fetchedAt": int(time.time() * 1000)})
```

#### Composition
```python
client.dispatch.event(app_id=email_app_id, event_name="email_compose_started",
    event_data={"draftId": draft_id,
                "to": "hello@acme.com",
                "subject": "Follow-up",
                "body": "<p>Hi …</p>"})

client.dispatch.event(app_id=email_app_id, event_name="email_compose_updated",
    event_data={"draftId": draft_id, "body": "<p>Updated body</p>"})

client.dispatch.event(app_id=email_app_id, event_name="email_compose_discarded",
    event_data={"draftId": draft_id})
```

#### Sending
```python
client.dispatch.event(app_id=email_app_id, event_name="email_send_started",
    event_data={"draftId": draft_id, "to": "hello@acme.com",
                "subject": "Follow-up",
                "startedAt": int(time.time() * 1000)})

client.dispatch.event(app_id=email_app_id, event_name="email_send_succeeded",
    event_data={"draftId": draft_id, "messageId": "<gmail-mid>",
                "sentAt": int(time.time() * 1000)})

client.dispatch.event(app_id=email_app_id, event_name="email_send_failed",
    event_data={"draftId": draft_id, "error": "SMTP 550",
                "failedAt": int(time.time() * 1000)})
```

> **`email_send_succeeded` is `external-irreversible`** — the message has already been delivered. Scrubbing past this event leaves the wire-state as it was; only forks create divergent timelines.

#### Thread actions (paired forward/inverse)

```python
# Each pair is external-reversible: scrubbing past the forward event
# can be reconciled by dispatching the inverse.

client.dispatch.event(app_id=email_app_id, event_name="email_archive_thread",
    event_data={"threadId": tid, "archivedAt": int(time.time() * 1000)})
client.dispatch.event(app_id=email_app_id, event_name="email_unarchive_thread",
    event_data={"threadId": tid, "unarchivedAt": int(time.time() * 1000)})

client.dispatch.event(app_id=email_app_id, event_name="email_star_thread",   …)
client.dispatch.event(app_id=email_app_id, event_name="email_unstar_thread", …)

client.dispatch.event(app_id=email_app_id, event_name="email_trash_thread",  …)
client.dispatch.event(app_id=email_app_id, event_name="email_untrash_thread",…)

client.dispatch.event(app_id=email_app_id, event_name="email_mark_thread_read",   …)
client.dispatch.event(app_id=email_app_id, event_name="email_mark_thread_unread", …)
```

#### Sync (server-emitted)
`email_messages_synced`, `email_body_fetched`, `email_sync_error`, `email_label_action_failed`, `email-app/new-email-received`. Listed for completeness.

---

### Image viewer

`applicationType: "image_viewer"`

Lightweight viewer for workspace image files. Most mutations come via `workspace/file_added` (server-side); the only event scripts typically dispatch is selection.

```python
client.dispatch.event(app_id=img_app_id, event_name="image_set_selected",
    event_data={"fileId": file_id})
```

---

### PDF viewer

`applicationType: "pdf_viewer"`

#### Load + page navigation
```python
client.dispatch.event(app_id=pdf_app_id, event_name="pdf_load_document",
    event_data={"fileId": file_id, "fileName": "report.pdf",
                "url": "https://blob.…/report.pdf",
                "loadedAt": int(time.time() * 1000)})

client.dispatch.event(app_id=pdf_app_id, event_name="pdf_set_page",
    event_data={"page": 3})

client.dispatch.event(app_id=pdf_app_id, event_name="pdf_set_total_pages",
    event_data={"totalPages": 42})

client.dispatch.event(app_id=pdf_app_id, event_name="pdf_set_zoom",
    event_data={"zoomLevel": 1.5})           # clamped 0.5–3.0

client.dispatch.event(app_id=pdf_app_id, event_name="pdf_set_sidebar",
    event_data={"open": True})
```

#### Highlights
```python
client.dispatch.event(app_id=pdf_app_id, event_name="pdf_add_highlight",
    event_data={"page": 3,
                "bounds": {"x": 100, "y": 200, "width": 220, "height": 40},
                "color": "#fde68a",
                "highlightId": str(uuid.uuid4())})

client.dispatch.event(app_id=pdf_app_id, event_name="pdf_remove_highlight",
    event_data={"highlightId": h_id})

client.dispatch.event(app_id=pdf_app_id, event_name="pdf_set_active_highlight",
    event_data={"highlightId": h_id})
```

#### Reference open (internal link)
```python
client.dispatch.event(app_id=pdf_app_id, event_name="pdf_open_reference",
    event_data={"page": 17, "view": "FitH"})
```

---

### Text editor

`applicationType: "text_editor"`

Single-document editor (e.g. for code snippets, prompts, raw markdown).

```python
client.dispatch.event(app_id=ed_app_id, event_name="editor_state_update",
    event_data={"content": "Hello, world.\n"})
```
Collapsible (2 s window per app).

---

### TLDraw canvas

`applicationType: "tldraw_canvas"`

Multi-tab whiteboard / sketchpad. Each tab is an independent `TLStoreSnapshot` JSON blob.

#### Tabs
```python
client.dispatch.event(app_id=draw_app_id, event_name="tldraw_create_tab",
    event_data={"tabName": "Architecture", "tabId": nanoid()})

client.dispatch.event(app_id=draw_app_id, event_name="tldraw_rename_tab",
    event_data={"tabId": tab_id, "newName": "System diagram"})

client.dispatch.event(app_id=draw_app_id, event_name="tldraw_delete_tab",
    event_data={"tabId": tab_id})

client.dispatch.event(app_id=draw_app_id, event_name="tldraw_select_tab",
    event_data={"tabId": tab_id})

client.dispatch.event(app_id=draw_app_id, event_name="tldraw_reorder_tabs",
    event_data={"orderedIds": [tab_b, tab_a, tab_c]})
```

#### Content
```python
client.dispatch.event(app_id=draw_app_id, event_name="tldraw_update_snapshot",
    event_data={"tabId": tab_id, "snapshot": "{...serialized TLStore JSON...}"})

client.dispatch.event(app_id=draw_app_id, event_name="tldraw_clear_tab",
    event_data={"tabId": tab_id})
```
`tldraw_update_snapshot` is collapsible (1.5 s window per tab).

---

### E2B sandbox

`applicationType: "e2b_sandbox"`

A Jupyter-style Python notebook backed by an E2B sandbox. Each tab is a cell; the kernel state persists across runs.

#### Tabs
```python
client.dispatch.event(app_id=sb_app_id, event_name="sandbox_create_tab",
    event_data={"tabName": "preprocess.py", "tabId": nanoid()})

client.dispatch.event(app_id=sb_app_id, event_name="sandbox_rename_tab",
    event_data={"tabId": tab_id, "newName": "preprocess_v2.py"})

client.dispatch.event(app_id=sb_app_id, event_name="sandbox_delete_tab",
    event_data={"tabId": tab_id})

client.dispatch.event(app_id=sb_app_id, event_name="sandbox_switch_tab",
    event_data={"tabId": tab_id})

client.dispatch.event(app_id=sb_app_id, event_name="sandbox_reorder_tabs",
    event_data={"orderedIds": [tab_b, tab_a]})
```

#### Code

```python
client.dispatch.event(app_id=sb_app_id, event_name="sandbox_update_code",
    event_data={"tabId": tab_id,
                "code": "import pandas as pd\nprint(pd.__version__)\n"})

client.dispatch.event(app_id=sb_app_id, event_name="sandbox_apply_patch_to_tab",
    event_data={"tabId": tab_id,
                "patch": "@@ -1,2 +1,2 @@\n-old line\n+new line\n"})

client.dispatch.event(app_id=sb_app_id, event_name="sandbox_run_code",
    event_data={"tabId": tab_id, "runId": str(uuid.uuid4())})
```

> **`sandbox_run_code` is a state-mutation event only** — it flips the running flag and picks which tab to display output for. The actual code execution still happens via the dedicated `/api/sandbox` route (driven server-side, not the SDK). Most SDK scripts won't need to dispatch this.

#### Output + terminal
```python
client.dispatch.event(app_id=sb_app_id, event_name="sandbox_save_output",
    event_data={"tabId": tab_id, "output": "0.1.0\n", "exitCode": 0,
                "timestamp": int(time.time() * 1000)})

client.dispatch.event(app_id=sb_app_id, event_name="sandbox_append_terminal_lines",
    event_data={"tabId": tab_id,
                "lines": ["[INFO] Started", "[INFO] Done"]})

client.dispatch.event(app_id=sb_app_id, event_name="sandbox_clear_terminal_log",
    event_data={"tabId": tab_id})

client.dispatch.event(app_id=sb_app_id, event_name="sandbox_delete_terminal_log_entry",
    event_data={"tabId": tab_id, "lineIndex": 7})
```

#### UI state
```python
client.dispatch.event(app_id=sb_app_id, event_name="sandbox_toggle_view",
    event_data={"splitView": True})

client.dispatch.event(app_id=sb_app_id, event_name="sandbox_set_sidebar_collapsed",
    event_data={"collapsed": False})

client.dispatch.event(app_id=sb_app_id, event_name="sandbox_set_sidebar_cwd",
    event_data={"cwd": "/home/user/data"})

client.dispatch.event(app_id=sb_app_id, event_name="sandbox_set_terminal_collapsed",
    event_data={"collapsed": False})

client.dispatch.event(app_id=sb_app_id, event_name="sandbox_set_terminal_height",
    event_data={"height": 220})
```

#### Sandbox lifecycle (mostly server-emitted)
```python
client.dispatch.event(app_id=sb_app_id, event_name="sandbox_set_id",
    event_data={"sandboxId": "e2b_…",
                "linkedAt": int(time.time() * 1000)})

client.dispatch.event(app_id=sb_app_id, event_name="sandbox_files_seeded",
    event_data={"files": [{"path": "/home/user/data.csv", "content": "…"}],
                "seededAt": int(time.time() * 1000)})

client.dispatch.event(app_id=sb_app_id, event_name="sandbox_file_copied_in",
    event_data={"sourcePath": "/workspace/data.csv",
                "destPath": "/home/user/data.csv",
                "copiedAt": int(time.time() * 1000)})

client.dispatch.event(app_id=sb_app_id, event_name="sandbox_set_drive_mounted",
    event_data={"isMounted": True,
                "mountedAt": int(time.time() * 1000)})

client.dispatch.event(app_id=sb_app_id, event_name="sandbox_delete_image",
    event_data={"imageId": img_id})
```

---

### Agent builder

`applicationType: "agent_builder"`

The visual graph editor + runtime for multi-agent networks. The full surface is large; you'll mostly only mutate the **graph** (nodes + edges) and observe **run state**.

#### Graph mutations
```python
client.dispatch.event(app_id=ab_app_id, event_name="agent_builder_add_node",
    event_data={
        "nodeId": nanoid(),
        "type": "agent",                 # "agent" | "tool" | "workspace_tool"
                                         # | "trigger" | "search"
                                         # | "image_edit_agent" | "open_app"
        "position": {"x": 100, "y": 200},
        "data": {
            "name": "Outreach",
            "promptSource": {"kind": "inline", "prompt": "You are an…"},
            "model": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        },
    })

client.dispatch.event(app_id=ab_app_id, event_name="agent_builder_update_node",
    event_data={"nodeId": node_id, "data": {"name": "Outreach v2"}})

client.dispatch.event(app_id=ab_app_id, event_name="agent_builder_move_node",
    event_data={"nodeId": node_id, "position": {"x": 200, "y": 200}})

client.dispatch.event(app_id=ab_app_id, event_name="agent_builder_remove_node",
    event_data={"nodeId": node_id})

client.dispatch.event(app_id=ab_app_id, event_name="agent_builder_add_edge",
    event_data={"edgeId": nanoid(),
                "source": planner_id, "target": outreach_id,
                "sourceHandle": "out", "targetHandle": "in"})

client.dispatch.event(app_id=ab_app_id, event_name="agent_builder_update_edge",
    event_data={"edgeId": edge_id, "routingPrompt": "Route to Outreach when…"})

client.dispatch.event(app_id=ab_app_id, event_name="agent_builder_remove_edge",
    event_data={"edgeId": edge_id})

client.dispatch.event(app_id=ab_app_id, event_name="agent_builder_set_graph",
    event_data={"nodes": [...], "edges": [...]})       # atomic replace
```

#### Network config
```python
client.dispatch.event(app_id=ab_app_id,
    event_name="agent_builder_update_network_config",
    event_data={
        "name": "Lead-discovery network",
        "description": "Find ~100 suppliers, qualify, outreach.",
        "defaultModel": {"provider": "anthropic", "model": "claude-sonnet-4-6"},
        "maxIter": 12,
        "routerContextApps": ["leads_spreadsheet", "brief_notes"],
    })
```

#### Run lifecycle (mostly server-emitted)

```python
# Status transitions (the runtime emits these; rarely user-dispatched):
client.dispatch.event(app_id=ab_app_id, event_name="agent_builder_update_run_state",
    event_data={"status": "running", "runId": run_id,
                "activeNodeId": agent_node_id})

# When a run completes:
client.dispatch.event(app_id=ab_app_id, event_name="agent_finish_running",
    event_data={"response": "Done. Found 87 candidates.",
                "timestamp": int(time.time() * 1000)})
```

Status values: `"idle" | "running" | "pausing" | "paused" | "completed" | "error" | "waiting" | "awaiting_approval" | "awaiting_user_answer"`.

#### Terminal / log

```python
client.dispatch.event(app_id=ab_app_id, event_name="agent_builder_append_run_log",
    event_data={"text": "Agent decided to skip duplicate row.",
                "kind": "info",      # "info" | "agent" | "error" | "system" | "separator"
                "timestamp": int(time.time() * 1000)})

client.dispatch.event(app_id=ab_app_id, event_name="agent_builder_clear_run_log",
    event_data={})

client.dispatch.event(app_id=ab_app_id, event_name="agent_builder_delete_run_log_entry",
    event_data={"entryId": entry_id})

client.dispatch.event(app_id=ab_app_id, event_name="agent_builder_set_terminal_collapsed",
    event_data={"collapsed": True})

client.dispatch.event(app_id=ab_app_id, event_name="agent_builder_set_terminal_height",
    event_data={"height": 180})

client.dispatch.event(app_id=ab_app_id, event_name="agent_builder_close_last_separator",
    event_data={})
```

#### Approval primitive (server-emitted; documented for completeness)

```python
# The agent runtime fires this when an agent calls request_approval_*.
event_data={
    "summary": "Send 47 outreach emails?",
    "payload": {"recipientIds": [...], "subjectTemplate": "…"},
    "requestingAgentNodeId": agent_id,
    "runId": run_id,
    "requestedAt": int(time.time() * 1000),
}

# To resolve a pending approval from a script, dispatch:
client.dispatch.event(app_id=ab_app_id, event_name="agent_approval_resolved",
    event_data={"decision": "approved", "note": "Looks good.",
                "resolvedAt": int(time.time() * 1000)})
```

#### Ask-user primitive (server-emitted)

```python
# Resolving:
client.dispatch.event(app_id=ab_app_id, event_name="agent_user_question_answered",
    event_data={"questionId": q_id, "answer": "Yes, proceed with $200 budget.",
                "answeredAt": int(time.time() * 1000)})
```

#### Other

```python
client.dispatch.event(app_id=ab_app_id,
    event_name="agent_builder_reorder_context_apps",
    event_data={"orderedInstanceNames": ["leads_spreadsheet", "brief_notes"]})

client.dispatch.event(app_id=ab_app_id,
    event_name="agent_builder_set_viewport",
    event_data={"viewport": {"x": -100, "y": 0, "zoom": 0.8}})

client.dispatch.event(app_id=ab_app_id,
    event_name="agent_inner_messages_saved",
    event_data={"agentNodeId": agent_id,
                "messages": [{"role": "assistant", "content": "…"}, …]})
```

---

### Search engine

`applicationType: "search_engine"`

A Firecrawl-backed web search surface. Typically driven by the agent runtime, but scripts can dispatch.

```python
client.dispatch.event(app_id=se_app_id, event_name="start_search",
    event_data={"query": "best dishwasher suppliers in Germany",
                "searchId": str(uuid.uuid4())})

client.dispatch.event(app_id=se_app_id, event_name="search_state_update",
    event_data={"query": "best dishwasher suppliers in Germany",
                "results": [{"title": "Acme GmbH", "url": "https://…",
                             "snippet": "…"}],
                "completedAt": int(time.time() * 1000)})
```

---

## Drive

The `drive` resource proxies Google Drive operations through the platform's OAuth identity. The session must belong to a workspace with a connected Drive (`DriveNotConnected` raised otherwise).

### `client.drive.list`
```python
result = client.drive.list(
    folder_id="folder_abc",
    recursive=False,
    page_token=None,
)
for f in result.files:
    print(f.id, f.name, f.mime_type)
if result.next_page_token:
    next_result = client.drive.list(folder_id="folder_abc",
                                    page_token=result.next_page_token)
```

### `client.drive.read`
```python
content_bytes = client.drive.read(file_id="file_xyz")
```
Google-native docs are exported as PDF. For Docs/Sheets/Slides, you get the binary export — handle accordingly.

### `client.drive.upload`
```python
new_file = client.drive.upload(
    folder_id="folder_abc",
    name="result.png",
    content=png_bytes,            # bytes
    mime_type="image/png",        # optional; auto-detected from name
)
print(new_file.id, new_file.name)
```
Also creates a `WorkspaceFile` row and fires `workspace/file_added` so the file appears in the workspace sidebar.

---

## Cookbook

### Recipe 0: create a new spreadsheet app, then write to it

```python
import esoul, uuid

with esoul.Esoul() as client:
    app = client.workspaces.apps.create(
        workspace_id="ws_abc",
        application_type="spreadsheet",
        instance_name="Q2 results",
    )

    # Grab the default sheet + column the schema seeded.
    state = client.dispatch.read_state(app_id=app.node_id).state
    sheet = state["sheets"][0]
    default_col = sheet["columns"][0]["id"]

    client.dispatch.event(
        app_id=app.node_id, event_name="spreadsheet_add_row",
        event_data={
            "sheetId": sheet["id"],
            "rowId": str(uuid.uuid4()),
            "cells": {default_col: "Hello from a new spreadsheet"},
        },
    )
```

### Recipe 1: bulk-import a CSV into a new sheet

```python
import csv, secrets, uuid

def nanoid(n=21):
    a = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    return "".join(secrets.choice(a) for _ in range(n))

# 1. Create the sheet client-side so we know its id immediately.
sheet_id = nanoid()
client.dispatch.event(app_id=ssh_app, event_name="spreadsheet_add_sheet",
    event_data={"name": "Imported leads", "sheetId": sheet_id})

# 2. Read sheet back to learn the default column id.
state = client.dispatch.read_state(app_id=ssh_app).state
sheet = next(s for s in state["sheets"] if s["id"] == sheet_id)
default_col = sheet["columns"][0]["id"]

# 3. Add a second column for emails.
email_col = nanoid()
client.dispatch.event(app_id=ssh_app, event_name="spreadsheet_add_column",
    event_data={"sheetId": sheet_id, "columnId": email_col,
                "name": "Email", "type": "email"})

# 4. Bulk-insert all rows in one event.
rows = []
with open("leads.csv") as f:
    for row in csv.DictReader(f):
        rows.append({
            "rowId": str(uuid.uuid4()),
            "cells": {default_col: row["name"], email_col: row["email"]},
        })

# 5. Dispatch up to 5000 rows in one shot, or chunk.
CHUNK = 1000
for i in range(0, len(rows), CHUNK):
    client.dispatch.event(
        app_id=ssh_app, event_name="spreadsheet_add_rows",
        event_data={"sheetId": sheet_id, "rows": rows[i:i + CHUNK]},
        idempotency_key=f"csv-import-{run_id}-chunk-{i // CHUNK}",
    )
```

### Recipe 2: programmatically build a slideshow from a markdown deck

```python
slides = [
    ("Title",  "export default () => <h1>Q2 review</h1>;"),
    ("Wins",   "export default () => <ul><li>Shipped X</li><li>…</li></ul>;"),
    ("Asks",   "export default () => <h2>Asks</h2>;"),
]
for name, src in slides:
    client.dispatch.event(
        app_id=deck_app, event_name="slide_create",
        event_data={"slideName": name, "tsxSource": src},
    )
```

### Recipe 3: update a status column in a long-running pipeline

```python
state = client.dispatch.read_state(app_id=ssh_app).state
sheet = state["sheets"][0]
status_col = next(c for c in sheet["columns"] if c["name"] == "Status")["id"]

for row in sheet["rows"]:
    if row["cells"].get(status_col) == "pending":
        client.dispatch.event(
            app_id=ssh_app, event_name="spreadsheet_set_cell",
            event_data={"sheetId": sheet["id"], "rowId": row["id"],
                        "columnId": status_col, "value": "running"},
        )
        # … do work on this row …
        client.dispatch.event(
            app_id=ssh_app, event_name="spreadsheet_set_cell",
            event_data={"sheetId": sheet["id"], "rowId": row["id"],
                        "columnId": status_col, "value": "complete"},
        )
```

### Recipe 4: append to an agent's run log from a separate script

```python
client.dispatch.event(
    app_id=ab_app, event_name="agent_builder_append_run_log",
    event_data={"text": "External cron tick.",
                "kind": "system",
                "timestamp": int(time.time() * 1000)},
)
```

### Recipe 5: batch dispatch — atomic-ish multi-app coordination

```python
from esoul.resources.dispatch import BatchEvent

client.dispatch.batch([
    BatchEvent(app_id=ssh_app, event_name="spreadsheet_add_row",
               event_data={"sheetId": sheet_id, "rowId": rid,
                           "cells": {col: "New lead"}}),
    BatchEvent(app_id=notes_app, event_name="note_update_content",
               event_data={"tabName": "Activity",
                           "content": notes_text + "\n- Added new lead.\n"}),
    BatchEvent(app_id=todo_app, event_name="todo_add_item",
               event_data={"listName": "Today",
                           "text": "Email new lead", "itemId": str(uuid.uuid4())}),
])
```
Up to 100 events. Validation is all-or-nothing; commit is sequential.

### Recipe 6: subscribe to a workspace by polling `read_state` + `version`

```python
import time

last_version = -1
while True:
    rs = client.dispatch.read_state(app_id=app_id)
    if rs.version != last_version:
        print("changed:", rs.state)
        last_version = rs.version
    time.sleep(2)
```
For live updates, the SDK currently exposes only read polling. A realtime subscription transport is planned.

---

## Performance + idempotency

### Latency (steady state)

| Operation | Typical |
|---|---|
| `dispatch.describe` | 50–150 ms |
| `read_state` | 100–200 ms |
| `dispatch.event` (small payload, warm) | ~700 ms |
| `dispatch.event` (1000-row `spreadsheet_add_rows`) | ~1.7 s |
| `dispatch.batch` (100 small events) | ~1–2 s |

After response: realtime publish + trigger fan-out fire in `waitUntil` so the caller is unblocked even when downstream is briefly slow.

### Retries

The SDK retries on transient 5xx + network errors with exponential backoff + jitter (default 3 attempts). Idempotency-Key is reused across retries so the server dedups duplicate-but-successful calls — your script will never see a duplicate row from a retried dispatch.

### Idempotency key strategy

| Scenario | Recommended key |
|---|---|
| One-off interactive call | None (auto-generated UUID) |
| Idempotent CI step | `f"ci-{job_id}-{step_id}"` |
| Per-record import from CSV | `f"csv-import-{run_id}-row-{index}"` |
| Cron with a fixed daily input | `f"daily-import-{date}"` |
| User-driven retry | Reuse the same key across attempts |

A key collides if you send the same key + a different body → 409 conflict (caught early; protects against typos). Cache TTL is 24 h.

### Throughput tips

1. **Use bulk events.** `spreadsheet_add_rows` with 1000 rows ≈ 1.7 s. 1000 `spreadsheet_add_row` calls ≈ 1000 × 700 ms = 12 min.
2. **Don't open multiple `Esoul()` instances in the same process.** The transport reuses a connection pool — opening many instances thrashes TLS handshakes.
3. **Async for fan-out.** When you have N independent calls (e.g. status updates across rows that can't be merged), use `AsyncEsoul` + `asyncio.gather` instead of sequential sync calls.
4. **Avoid `read_state` in tight loops.** It's ~100 ms — bad inside a per-row hot loop. Read once, mutate, read once at the end.

### Anti-patterns

- ❌ Dispatching `spreadsheet_add_row` in a `for` loop for >50 rows. Use `spreadsheet_add_rows`.
- ❌ Generating ids server-side then immediately calling `read_state` to find them. Generate ids client-side (UUID v4 or `nanoid()`).
- ❌ Polling `describe()` in a hot loop. The shape changes only when apps are added/removed — cache it.
- ❌ Building a `BatchEvent` of 100 unrelated events just to save HTTP overhead. Use it for events that need to land "together" semantically; for raw throughput, single bulk events beat batches.
- ❌ Forgetting that `event_data` keys are camelCase. `sheet_id` will silently never match the schema.

---

## Versioning + stability

The platform's wire format is versioned by URL path (`/api/v1/…`). Within v1:

- **Adding a new event** to an app is a non-breaking change. Old SDK callers ignore it.
- **Adding an optional field** to `event_data` is non-breaking.
- **Adding a required field** is breaking — the server makes it optional for 30 days, then enforces.
- **Removing or renaming** an event is breaking and goes through deprecate-warn-remove (30 days minimum). The CHANGELOG documents every schema change.

The SDK's PyPI package follows semver. Code generated from event schemas lands in minor releases automatically; major releases are reserved for breaking API changes.

---

## Where the docs live

This document is hand-curated for LLM consumption. The canonical machine-readable equivalent is `client.dispatch.describe()` — which enumerates registered apps + event names exactly as the server understands them at the moment of the call. When the docs and `describe()` disagree, **`describe()` is authoritative**.

For platform internals (event-store architecture, reducer semantics, the agent-builder runtime), see `.cursor/rules/docs/` in the kinetic repo.
