"""Credential resolution + token-shape introspection.

The SDK auto-detects credentials from four sources, in priority order:

  1. Explicit `token=` kwarg passed to `Esoul()` / `AsyncEsoul()`.
  2. `ESOUL_TOKEN` environment variable.
  3. `/var/run/esoul/token` — the file every E2B sandbox gets at boot.
     Mode 0600, sandbox-user-owned. The SDK can also REWRITE this file
     during refresh (so a subsequent process boot sees the latest token).
  4. `~/.config/esoul/credentials` — INI file with `[default]` section
     containing `token = esoul_pat_...`. The directory + file should be
     mode 0700 / 0600; we don't enforce this (user's local choice).

If none resolve, `MissingCredentialsError` is raised at client construction.
The SDK never falls back to anonymous access.

Token shapes (the format-detection is mirrored in `src/lib/access-sessions/sign.ts`):

  Sandbox JWT:  `<headerB64u>.<bodyB64u>.<sigB64u>` (3 dot-separated parts;
                header pins HS256). Decodable body gives us `{sessionId, exp}`
                — we trust `exp` for refresh scheduling ONLY (the server
                still verifies the signature).

  PAT:          `esoul_pat_<sessionId>.<sigB64u>` (2 dot-separated parts
                after the prefix). No expiry field — PATs live until
                manually revoked OR until the server-side `expiresAt`
                column (set at mint time) passes.

The distinction matters for refresh scheduling: only sandbox JWTs are
refreshable. PATs that hit `session_expired` need to be re-minted in the
UI.
"""

from __future__ import annotations

import base64
import configparser
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from .exceptions import MissingCredentialsError


# ─── Paths ───────────────────────────────────────────────────────────────


def _sandbox_token_path() -> Path:
    """The file every E2B sandbox gets at boot.

    Override via env `ESOUL_SANDBOX_TOKEN_PATH` (useful for local
    integration tests that don't run inside a real sandbox).
    """
    return Path(os.environ.get("ESOUL_SANDBOX_TOKEN_PATH", "/var/run/esoul/token"))


def _user_config_path() -> Path:
    """The off-platform credentials file at `~/.config/esoul/credentials`.

    Honours XDG_CONFIG_HOME for users with non-standard layouts.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "esoul" / "credentials"


# ─── Token introspection ─────────────────────────────────────────────────

_PAT_PREFIX = "esoul_pat_"


def detect_token_kind(token: str) -> Literal["sandbox", "pat"]:
    """Determine whether a token is a sandbox JWT or a PAT.

    Structural check only — does NOT verify signatures (which would
    require the per-session secret, only known server-side). The server
    rejects mismatched / forged tokens regardless of what we infer here.

    Falls back to "sandbox" for ambiguous shapes (e.g. an empty token) —
    the server will fail it cleanly with a 401 invalid_token_format.
    """
    if token.startswith(_PAT_PREFIX):
        return "pat"
    return "sandbox"


def peek_jwt_expiry(jwt: str) -> Optional[float]:
    """Extract the `exp` claim from a sandbox JWT body without verifying.

    Returns Unix seconds (float) or None if the JWT structure is malformed
    or the body lacks an `exp` field.

    Trusting this value for refresh-scheduling is safe: even if we get it
    wrong, the server still verifies. The worst case from a corrupt `exp`
    is a refresh that fires too early (cheap) or too late (the server
    returns 401 invalid_signature and the SDK reports it as AuthError).
    """
    parts = jwt.split(".")
    if len(parts) != 3:
        return None
    try:
        body_b64 = parts[1]
        # Pad to multiple of 4 for stdlib base64.
        body_b64 += "=" * (-len(body_b64) % 4)
        # Translate base64url → standard base64 alphabet.
        body_b64 = body_b64.replace("-", "+").replace("_", "/")
        body_bytes = base64.b64decode(body_b64)
        body = json.loads(body_bytes.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    exp = body.get("exp")
    if isinstance(exp, (int, float)):
        return float(exp)
    return None


# ─── Credentials container ───────────────────────────────────────────────


CredentialSource = Literal["explicit", "env", "sandbox_file", "config_file"]


@dataclass
class Credentials:
    """Resolved credentials + provenance.

    Carries enough state for the transport layer to:
      - inject the Authorization header
      - decide whether refresh applies (sandbox JWTs yes, PATs no)
      - find the disk file to overwrite on refresh
      - schedule refresh ahead of `exp`

    Treat instances as MUTABLE — the transport layer rewrites `token` and
    `expires_at_unix` after every successful refresh. `kind`, `source`,
    and `sandbox_token_path` are stable for the lifetime of the
    credentials.
    """

    token: str
    kind: Literal["sandbox", "pat"]
    source: CredentialSource
    sandbox_token_path: Optional[Path]
    expires_at_unix: Optional[float]

    @property
    def refreshable(self) -> bool:
        """True iff this credential can be refreshed in place (sandbox JWTs).

        PATs are NEVER refreshable — they're long-lived by design; expiry
        means the user must mint a new one in the settings UI.
        """
        return self.kind == "sandbox"


# ─── File I/O ────────────────────────────────────────────────────────────


def _read_sandbox_token_file(path: Path) -> Optional[str]:
    """Read the sandbox token file. Returns None if the file doesn't exist
    or is unreadable for any reason (permissions, corruption, race).

    Errors here are silent because this is the THIRD source consulted — we
    only get this far if explicit + env both failed, and a missing file is
    the expected case off-platform.
    """
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except (FileNotFoundError, PermissionError, OSError):
        return None


def _read_user_config_file(path: Path) -> Optional[str]:
    """Read the `[default]` profile's `token` from `~/.config/esoul/credentials`.

    Returns None if the file is missing, unreadable, or doesn't contain a
    `[default]` section with a `token` key.

    Future enhancement (deferred): named profiles via env `ESOUL_PROFILE`
    selecting `[my-laptop]` etc. — Stripe/AWS pattern. Out of scope for v1.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, OSError):
        return None
    parser = configparser.ConfigParser()
    try:
        parser.read_string(text)
    except configparser.Error:
        return None
    if not parser.has_section("default"):
        return None
    token = parser.get("default", "token", fallback="").strip()
    return token or None


def write_sandbox_token_file(path: Path, token: str) -> None:
    """Atomically rewrite the sandbox token file during refresh.

    Used by `_transport.py`'s background refresh path. Writes to a
    temp file in the same directory THEN renames — atomic on POSIX so
    a concurrent reader (a sibling process re-reading the file) always
    sees either the old or the new token, never a half-written file.

    Best-effort: failures are logged by the caller but don't fail the
    refresh (the in-memory `Credentials.token` is updated regardless;
    only a sandbox restart needs the file).
    """
    # Same directory as target so rename is atomic (rename across
    # filesystems is not atomic).
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(token, encoding="utf-8")
    # Match the original 0600 — owner-rw only.
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        # Best-effort: some sandboxes mount /var/run with different
        # semantics; refresh still completes.
        pass
    os.replace(tmp, path)


# ─── Public resolver ─────────────────────────────────────────────────────


def resolve_credentials(token: Optional[str] = None) -> Credentials:
    """Resolve credentials from the four-source precedence chain.

    Raises `MissingCredentialsError` if no source yields a non-empty token.

    The returned `Credentials.source` records which source won, useful for
    diagnostics ("which file did the SDK use?").
    """
    # 1. Explicit kwarg.
    if token:
        return _credentials_for(token, source="explicit", file_path=None)

    # 2. Environment variable.
    env_token = os.environ.get("ESOUL_TOKEN", "").strip()
    if env_token:
        return _credentials_for(env_token, source="env", file_path=None)

    # 3. Sandbox file.
    sandbox_path = _sandbox_token_path()
    sandbox_token = _read_sandbox_token_file(sandbox_path)
    if sandbox_token:
        return _credentials_for(
            sandbox_token, source="sandbox_file", file_path=sandbox_path,
        )

    # 4. User config file.
    config_path = _user_config_path()
    config_token = _read_user_config_file(config_path)
    if config_token:
        return _credentials_for(config_token, source="config_file", file_path=None)

    raise MissingCredentialsError(
        "No credentials found. Set the `ESOUL_TOKEN` env var, write a token "
        f"to {config_path}, or pass `token=...` to Esoul()."
    )


def _credentials_for(
    token: str, *, source: CredentialSource, file_path: Optional[Path],
) -> Credentials:
    """Build a Credentials instance, inferring kind + expiry from the token."""
    kind = detect_token_kind(token)
    expires_at_unix = peek_jwt_expiry(token) if kind == "sandbox" else None
    # `sandbox_token_path` is only set for the sandbox_file source — that's
    # the only case where refresh writes back to disk.
    sandbox_token_path = file_path if source == "sandbox_file" else None
    return Credentials(
        token=token,
        kind=kind,
        source=source,
        sandbox_token_path=sandbox_token_path,
        expires_at_unix=expires_at_unix,
    )


__all__ = [
    "Credentials",
    "CredentialSource",
    "detect_token_kind",
    "peek_jwt_expiry",
    "resolve_credentials",
    "write_sandbox_token_file",
]
