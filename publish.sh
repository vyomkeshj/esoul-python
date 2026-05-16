#!/usr/bin/env bash
#
# Publish the esoul Python SDK as an independent GitHub repo + push to PyPI.
#
# Idempotent: skips steps that are already done. Pauses for confirmation
# before every destructive / network-facing action (git push, twine upload).
#
# Preconditions:
#   1. https://github.com/vyomkeshj/esoul-python EXISTS but is EMPTY
#      (no README, no LICENSE, no .gitignore initialised on GitHub —
#      otherwise the first push fails with non-fast-forward).
#   2. python3 -m build and twine are installed and on PATH.
#   3. For the PyPI step: TWINE_USERNAME=__token__ and TWINE_PASSWORD=pypi-...
#      env vars are set (or ~/.pypirc is configured). The script will
#      refuse to upload without one of the two.
#
# Usage:
#   cd python-packages/esoul && ./publish.sh
#   ./publish.sh --skip-pypi      # GitHub push only
#   ./publish.sh --skip-github    # PyPI upload only (assumes already pushed)
#   ./publish.sh --yes            # skip all confirmation prompts (CI)

set -euo pipefail

# ─── Config ──────────────────────────────────────────────────────────────

GITHUB_REMOTE_URL="https://github.com/vyomkeshj/esoul-python.git"
TAG_NAME=""   # filled from _version.py below
COMMIT_MSG="Initial commit — esoul 0.1.0

Python SDK for ExternalSoul. Workspace-event dispatch + Drive proxy +
agent invocation (agents.invoke / invoke_pin) + workspace HIL queue
(questions.ask). Sync + async parity."

# ─── Arg parsing ─────────────────────────────────────────────────────────

SKIP_GITHUB=0
SKIP_PYPI=0
AUTO_YES=0
for arg in "$@"; do
  case "$arg" in
    --skip-github) SKIP_GITHUB=1 ;;
    --skip-pypi)   SKIP_PYPI=1 ;;
    --yes|-y)      AUTO_YES=1 ;;
    -h|--help)
      grep -E '^# ' "$0" | sed 's/^# //'
      exit 0
      ;;
    *)
      echo "unknown flag: $arg" >&2
      exit 2
      ;;
  esac
done

# ─── Helpers ─────────────────────────────────────────────────────────────

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
ok()    { printf '\033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '\033[33m!\033[0m %s\n' "$*"; }
fail()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }
step()  { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }

confirm() {
  local prompt="$1"
  if [ "$AUTO_YES" -eq 1 ]; then
    echo "[--yes] $prompt"
    return 0
  fi
  printf '%s [y/N] ' "$prompt"
  read -r answer
  case "$answer" in
    y|Y|yes|YES) return 0 ;;
    *)           fail "Aborted by user." ;;
  esac
}

# ─── Locate package root ─────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
if [ ! -f pyproject.toml ] || [ ! -d src/esoul ]; then
  fail "Expected to be running from inside python-packages/esoul (no pyproject.toml or src/esoul here)."
fi
bold "Package root: $SCRIPT_DIR"

# Extract version from _version.py — single source of truth.
PKG_VERSION="$(grep -oP '__version__\s*=\s*"\K[^"]+' src/esoul/_version.py)"
[ -n "$PKG_VERSION" ] || fail "Could not read __version__ from src/esoul/_version.py."
TAG_NAME="v$PKG_VERSION"
ok "Version: $PKG_VERSION (tag: $TAG_NAME)"

# ─── Sanity checks ───────────────────────────────────────────────────────

step "Pre-flight checks"

# Stale URLs in pyproject — these get baked into the wheel METADATA.
if grep -qE "externalsoul/esoul-python" pyproject.toml; then
  fail "pyproject.toml still references 'externalsoul/esoul-python'. Update [project.urls] before publishing."
fi
ok "pyproject.toml URLs look correct"

# Block if we'd inadvertently use the kinetic monorepo's .git.
if [ -d .git ]; then
  # Verify it's OUR repo, not a stray.
  if [ "$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel 2>/dev/null)" = "$SCRIPT_DIR" ]; then
    ok "Local .git exists (this dir is already a standalone repo)"
  else
    fail ".git here doesn't resolve to $SCRIPT_DIR — possibly a worktree of a different repo."
  fi
else
  # No .git here yet — does git walk up to a parent (kinetic)?
  if PARENT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)" && [ "$PARENT_ROOT" != "$SCRIPT_DIR" ]; then
    warn "No .git here, but git walks up to: $PARENT_ROOT"
    warn "This is fine — 'git init' below will create a NEW repo here, isolated from the parent."
  else
    ok "No git ancestry — clean slate"
  fi
fi

# ─── GitHub push ─────────────────────────────────────────────────────────

if [ "$SKIP_GITHUB" -eq 0 ]; then
  step "GitHub: init / commit / push"

  if [ ! -d .git ]; then
    git init -q
    ok "git init"
  else
    ok "Skipped git init (already a repo)"
  fi

  # Stage everything; .gitignore handles dist/ + caches.
  git add .
  STAGED="$(git diff --cached --name-only | wc -l | tr -d ' ')"
  if [ "$STAGED" -eq 0 ] && [ -z "$(git status --porcelain)" ]; then
    if git rev-parse HEAD >/dev/null 2>&1; then
      ok "Working tree clean (HEAD already commits everything)"
    else
      fail "Nothing to commit and no HEAD — sanity-check the package contents."
    fi
  fi

  # Show what we're about to commit — eyeball check.
  if [ "$STAGED" -gt 0 ]; then
    echo
    echo "About to commit these files:"
    git diff --cached --name-only | sed 's/^/  /' | head -50
    [ "$STAGED" -gt 50 ] && echo "  …and $((STAGED - 50)) more"
    echo
    confirm "Looks right?"
  fi

  if ! git rev-parse HEAD >/dev/null 2>&1; then
    git commit -q -m "$COMMIT_MSG"
    ok "Initial commit created"
  elif [ "$STAGED" -gt 0 ]; then
    git commit -q -m "$COMMIT_MSG"
    ok "Commit created (HEAD already existed; new commit on top)"
  fi

  git branch -M main
  ok "Branch renamed to main"

  # Add or update remote.
  if git remote get-url origin >/dev/null 2>&1; then
    EXISTING_URL="$(git remote get-url origin)"
    if [ "$EXISTING_URL" = "$GITHUB_REMOTE_URL" ]; then
      ok "Remote 'origin' already points at $GITHUB_REMOTE_URL"
    else
      warn "Remote 'origin' points at $EXISTING_URL"
      confirm "Replace with $GITHUB_REMOTE_URL?"
      git remote set-url origin "$GITHUB_REMOTE_URL"
      ok "Remote 'origin' updated"
    fi
  else
    git remote add origin "$GITHUB_REMOTE_URL"
    ok "Remote 'origin' added → $GITHUB_REMOTE_URL"
  fi

  # PUSH.
  echo
  confirm "Push to $GITHUB_REMOTE_URL (branch: main)?"
  if ! git push -u origin main 2>&1; then
    fail "Push failed. If GitHub returned 'repository not found', create it at $GITHUB_REMOTE_URL first (empty, no README). If 'fetch first / non-fast-forward', the remote was initialised with a README — see https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-new-repository#initializing-the-repository."
  fi
  ok "main pushed"

  # Tag.
  if git rev-parse "$TAG_NAME" >/dev/null 2>&1; then
    ok "Tag $TAG_NAME already exists locally — skipping creation"
  else
    git tag -a "$TAG_NAME" -m "esoul $PKG_VERSION"
    ok "Tag $TAG_NAME created"
  fi
  echo
  confirm "Push tag $TAG_NAME?"
  git push origin "$TAG_NAME" 2>&1 | sed 's/^/    /'
  ok "Tag pushed"
fi

# ─── PyPI upload ─────────────────────────────────────────────────────────

if [ "$SKIP_PYPI" -eq 0 ]; then
  step "PyPI: rebuild + upload"

  # Token presence check — refuse to interactive-prompt for credentials
  # when running unattended-ish. Either env vars OR ~/.pypirc must work.
  if [ -z "${TWINE_PASSWORD:-}" ] && [ ! -f "$HOME/.pypirc" ]; then
    fail "Neither TWINE_PASSWORD env var nor ~/.pypirc is set. Set: export TWINE_USERNAME=__token__ TWINE_PASSWORD=pypi-..."
  fi

  command -v python3 >/dev/null || fail "python3 not on PATH"
  command -v twine   >/dev/null || fail "twine not on PATH. Install: pip install --user twine"

  # Always rebuild — the on-disk dist/ may have stale URLs baked in.
  echo "Cleaning prior dist/ and rebuilding…"
  rm -rf dist/ build/
  python3 -m build 2>&1 | tail -3
  ls -lh dist/ | sed 's/^/    /'

  # Validate metadata.
  twine check dist/* 2>&1 | sed 's/^/    /'

  # Verify the new METADATA carries the corrected URLs.
  if python3 -c "
import zipfile, sys
with zipfile.ZipFile('dist/esoul-$PKG_VERSION-py3-none-any.whl') as z:
    md = z.read('esoul-$PKG_VERSION.dist-info/METADATA').decode()
if 'externalsoul/esoul-python' in md:
    print('STALE URL in wheel METADATA'); sys.exit(1)
if 'vyomkeshj/esoul-python' not in md:
    print('Expected URL missing from wheel METADATA'); sys.exit(1)
print('METADATA URLs OK')
"; then
    ok "Wheel METADATA URLs correct"
  else
    fail "Wheel METADATA URL check failed."
  fi

  echo
  bold "Ready to upload esoul $PKG_VERSION to PyPI."
  echo "This is the production PyPI registry. Cannot be undone."
  confirm "Upload to https://pypi.org/?"
  twine upload dist/*

  ok "Uploaded — https://pypi.org/project/esoul/$PKG_VERSION/"
  echo
  echo "Smoke-test (in a throwaway venv):"
  echo "  python3 -m venv /tmp/esoul-live && source /tmp/esoul-live/bin/activate"
  echo "  pip install esoul==$PKG_VERSION"
  echo "  python -c 'import esoul; print(esoul.__version__)'"
  echo
  echo "Then revoke the broad PyPI token and create a project-scoped one:"
  echo "  https://pypi.org/manage/account/token/"
fi

echo
ok "Done."
