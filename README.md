# esoul — Python SDK for ExternalSoul

`esoul` is the official Python SDK for the **ExternalSoul** platform. It lets
scripts mutate workspace state from anywhere Python runs — inside the
platform's E2B sandboxes, on a data scientist's laptop, in a Colab notebook,
in CI, in a scheduled cron job.

```bash
pip install esoul
```

```python
import esoul

client = esoul.Esoul()                       # auto-detects credentials
state = client.describe()                    # what workspaces + apps + events are available
print(state.session.workspace_ids)

# Low-level dispatch
result = client.dispatch_event(
    app_id="app_abc123",
    event_name="spreadsheet_add_row",
    event_data={"cells": {"name": "Alice", "email": "a@example.com"}},
)
print(result.event_id, result.sequence_num)
```

## Why an SDK?

ExternalSoul workspaces are **event-sourced** — every state mutation is a
typed event on a per-workspace timeline, scrubbable and forkable. The UI,
agents, and now scripts all dispatch through the **same** reducers; events
are the source of truth, audit logging is automatic.

The SDK is the platform's universal binding for **scripted workspace
operations**. Common use cases:

- Bulk-import 1000 rows into a spreadsheet (one event, not 1000 LLM tool calls)
- Run an OpenCV pipeline over Drive images and write masks back
- Programmatic kanban / contacts load from a CSV
- Notebook-as-workspace-builder
- Any agent or human writing Python to mutate app state

## Authentication

`esoul.Esoul()` auto-detects credentials in this order:

1. Explicit `token=` kwarg
2. `ESOUL_TOKEN` environment variable
3. `/var/run/esoul/token` (the file mode 0600 token written by every E2B
   sandbox at boot — `Esoul()` inside a sandbox Just Works)
4. `~/.config/esoul/credentials` (TOML file with a `[default]` section
   containing `token = "esoul_pat_..."`)

For off-platform use (laptop, CI), create a **Personal Access Token** in
workspace settings → "Access Tokens", pick the workspaces it can mutate,
copy the token (shown once), and either:

```bash
# Export inline:
export ESOUL_TOKEN="esoul_pat_..."

# Or write to ~/.config/esoul/credentials:
mkdir -p ~/.config/esoul
cat > ~/.config/esoul/credentials <<EOF
[default]
token = "esoul_pat_..."
EOF
chmod 600 ~/.config/esoul/credentials
```

## Workspace isolation

A given credential is scoped to one or more specific workspaces. Cross-
workspace dispatch is **structurally impossible** — not a permission check
in your script, an architectural invariant of the server.

## Idempotency

Every write is automatically idempotent. The SDK generates a UUID per call
and reuses it across retries; the server caches the response under
`(session, key)` for 24h. Same key + same body → identical cached response,
not a duplicate event. You can override the key for application-level retry
control:

```python
client.dispatch_event(
    app_id=...,
    event_name=...,
    event_data=...,
    idempotency_key="my-task-2026-05-15",  # caller-controlled
)
```

## Async client

The async client mirrors the sync API exactly:

```python
import asyncio
import esoul

async def main():
    async with esoul.AsyncEsoul() as client:
        result = await client.dispatch_event(
            app_id=..., event_name=..., event_data=...,
        )

asyncio.run(main())
```

## Typed errors

Failures surface as Python exception classes mapped from the server's
`error.code`. You can catch the kind you care about, and `EsoulError`
catches everything:

```python
import esoul

try:
    client.dispatch_event(...)
except esoul.WorkspaceAccessDenied as e:
    print(f"This token cannot reach workspace {e.details['workspaceId']}")
except esoul.IdempotencyConflict as e:
    print("Same key was used with a different request body")
except esoul.RateLimitError as e:
    time.sleep(e.retry_after_seconds or 1)
except esoul.AuthError:
    print("Token expired or revoked — get a new one")
except esoul.EsoulError as e:
    print(f"{e.code}: {e.message}")
```

## Status

**v0.1 — alpha.** Wire surface is stable; per-app typed resources are
codegen'd and shipping incrementally. The low-level `dispatch_event` /
`dispatch_batch` / `read_state` paths are production-ready.

See [CHANGELOG.md](./CHANGELOG.md) for release notes.

## License

MIT.
