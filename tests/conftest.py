"""pytest configuration + shared fixtures for the esoul SDK test suite.

Tests split into two layers:

  - **Unit tests** (no `_integration_` in the name) — run against mocks via
    respx. Fast, no network, no credentials required. Always run in CI.

  - **Integration tests** (named `test_integration_*` or in the
    `tests/integration/` directory) — hit a real server (localhost or
    deployed). Require `ESOUL_TEST_TOKEN` + `ESOUL_TEST_BASE_URL` env vars.
    Skipped automatically when those env vars are missing.

The .env.local file in the kinetic repo root carries the test token + base
URL; we load it manually here so `pytest` from the SDK directory picks
them up.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import pytest


def _load_env_local() -> None:
    """Read kinetic/.env.local and inject into os.environ.

    We don't depend on python-dotenv to avoid pulling a dev-only dep into
    the SDK's deps. The file format we care about is `KEY=VALUE` or
    `KEY="VALUE"` per line — simple enough to parse inline.

    Only sets variables that aren't already present in the environment,
    so explicit `export ESOUL_TEST_TOKEN=...` from the shell wins.
    """
    candidates = [
        Path(__file__).resolve().parent.parent.parent.parent / ".env.local",
        Path(__file__).resolve().parent.parent.parent.parent / ".env",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_env_local()


def _missing_env(name: str) -> bool:
    return not os.environ.get(name)


@pytest.fixture(scope="session")
def integration_token() -> str:
    """The PAT used for integration tests. Skips when absent."""
    token = os.environ.get("ESOUL_TEST_TOKEN")
    if not token:
        pytest.skip("ESOUL_TEST_TOKEN not set — integration test skipped")
    return token


@pytest.fixture(scope="session")
def integration_base_url() -> str:
    """The server origin integration tests hit. Defaults to localhost."""
    return os.environ.get("ESOUL_TEST_BASE_URL", "http://localhost:3000")


@pytest.fixture(scope="session")
def integration_workspace_id() -> Optional[str]:
    """Optional pinned workspace for tests that need to address apps inside
    one. Loaded from `ESOUL_TEST_WORKSPACE_ID` if set; otherwise discovered
    on demand via `describe()` (the first workspace the token can see)."""
    return os.environ.get("ESOUL_TEST_WORKSPACE_ID")


@pytest.fixture(scope="session")
def integration_spreadsheet_app_id() -> Optional[str]:
    """Optional pinned appId for the spreadsheet-backed read-state tests.

    Loaded from `ESOUL_TEST_SPREADSHEET_APP_ID`. When unset, the test
    discovers an appId at runtime by calling `describe()` + querying its
    own state; tests that need a hard-coded id will skip if neither is
    available.
    """
    return os.environ.get("ESOUL_TEST_SPREADSHEET_APP_ID")


@pytest.fixture(scope="session")
def integration_agent_builder_app_id() -> Optional[str]:
    """Optional pinned appId for agent_builder-backed write tests (used by
    the idempotency-conflict test which dispatches a viewport update — a
    pure no-op UI change). Same env-var fallback rule as the spreadsheet
    fixture."""
    return os.environ.get("ESOUL_TEST_AGENT_BUILDER_APP_ID")
