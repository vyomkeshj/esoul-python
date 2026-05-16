# Changelog

All notable changes to this project will be documented in this file. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and
this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] — 2026-05-17

### Changed (breaking)
- `client.agents.invoke(workspace_id, agent, ...)` → `client.agents.invoke(agent, ...)`.
  `workspace_id` is now a keyword-only argument that defaults to the
  session's only workspace (or raises a helpful error if the session
  has multiple). Same change applied to `client.questions.ask`,
  `ask_async`, `wait_for_answer`, `list`, `get`, `answer`, `cancel`.
  Migration: pass workspace_id as a keyword when the session has
  multiple workspaces; otherwise just drop the positional argument.

### Added
- `Transport.resolve_workspace_id(explicit)` — lazy-fetches the
  session's workspaceIds via `/api/v1/describe` once per transport
  lifetime and caches them. Powers the auto-resolution above.

## [0.1.0] — 2026-05-17

### Added
- **`client.agents`** — SDK-driven agent_builder invocation. Primary
  surface `agents.invoke(workspace_id, agent, input=..., images=...)`
  where `agent` is a nodeId (uuid) or instanceName (case-insensitive).
  Convenience layer `agents.invoke_pin(pinned_agent_id, ...)` for
  cross-workspace pins on the caller's own account. Polling handle
  with `wait(timeout, on_question, on_approval)` (exponential backoff
  2s → 10s, callbacks dedup'd by questionId / summary). `agents.get`,
  `agents.list`, `handle.cancel`.
- **`client.questions`** — workspace HIL queue. Programs can ask
  questions via `questions.ask(workspace_id, "...", default_on_timeout=)`
  which blocks until a human answers via the workspace's header drawer.
  Also `ask_async`, `wait_for_answer`, `list`, `get`, `answer`, `cancel`.
- Async parity for both resources: `AsyncAgentsResource`,
  `AsyncInvocationHandle`, `AsyncQuestionsResource`.
- New typed dataclasses: `InvocationStatus`, `InvocationResult`,
  `Question`, `Answer`.
- New typed exceptions: `InvocationTimeout`, `InvocationError`,
  `QuestionTimeout`, `QuestionAlreadyResolved`. Mapped from server
  error codes `agent_not_found`, `agent_ambiguous`, `agent_invalid_type`,
  `invocation_*`, `pinned_agent_*`, `question_*`.
- Initial sync `Esoul` and async `AsyncEsoul` clients.
- Credential auto-detection: explicit kwarg → `ESOUL_TOKEN` env →
  `/var/run/esoul/token` (sandbox) → `~/.config/esoul/credentials` (PAT).
- Low-level dispatch surface: `dispatch_event`, `dispatch_batch`,
  `read_state`, `describe`, `refresh_token`.
- Drive proxy resource: `drive.list_folder`, `drive.read_file`,
  `drive.upload_file`.
- Typed exception hierarchy mapped from server `error.code`:
  `AuthError`, `WorkspaceAccessDenied`, `AppNotFoundError`,
  `EventNotRegistered`, `IdempotencyConflict`, `RateLimitError`,
  `DriveNotConnected`, `APIError`.
- Auto-generated `Idempotency-Key` (UUID v4 per call), reused across
  retries; caller can override via `idempotency_key=` kwarg.
- Retry-on-5xx + network-error policy with exponential backoff + jitter.
  4xx errors don't retry. 429 respects `Retry-After`.
- Background token-refresh for sandbox JWTs (60s before expiry). PAT
  tokens don't refresh.
- `py.typed` marker for PEP 561 / mypy compatibility.

### Not yet shipped
- Per-app typed resources (`spreadsheet`, `notes`, etc.) — landing as
  codegen pipeline ships, one app at a time.
- Batch context manager (`with client.batch():`) for client-side id
  pre-generation in multi-event flows.
- Per-event return shapes (depends on server adding `returnShape` to
  event definitions).
