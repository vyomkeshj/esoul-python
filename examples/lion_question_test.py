"""
Sandbox-runnable smoke test for the `test-agent` lion-question flow.

The agent's prompt:
   "Generate image of a lion, ask the user if it's okay, if they say
    it's okay, save it to workspace else generate the next one based
    on their input."

This script invokes the agent and waits — the agent generates an image
via `generate_image`, attaches it to an `ask_user` call, and pauses
until you answer via the workspace header drawer in your browser. The
script polls the invocation status the entire time so you can watch
the lifecycle on stdout.

Defaults target the LOCAL dev server at http://localhost:3000 since the
v1 agent-invocation routes haven't been deployed to externalsoul.com yet.
Override via env vars when running against a different host:

    # Local dev (no env overrides needed):
    python lion_question_test.py

    # Custom host / different PAT:
    ESOUL_TOKEN=esoul_pat_... \\
    ESOUL_BASE_URL=https://externalsoul.com \\
    python lion_question_test.py
"""

from __future__ import annotations

import os
import sys
import time

import esoul

AGENT = os.environ.get("AGENT", "test-agent")

# Hardcoded PAT + base URL — explicit values WIN over env so the script
# behaves predictably regardless of where it runs.
#
# IMPORTANT: this script is designed to run on the SAME machine as the
# kinetic dev server (so `http://localhost:3000` resolves to it). If you
# `python lion_question_test.py` from inside an E2B sandbox, "localhost"
# resolves to the sandbox itself (a remote microVM) and the connection
# will fail. From a sandbox, you need a publicly-reachable URL — either
# the deployed Vercel domain (after `git push` deploys it) or an ngrok
# tunnel pointing at your local dev server.
TOKEN = (
    "esoul_pat_fe6c2b0b-2dff-4a71-b200-8771168e3661.dpyw624WPZZVMrlDk7Da6ZXBK15zDAZCu7eYelfclsc"
)
BASE_URL = "http://localhost:3000"


def main() -> int:
    client = esoul.Esoul(token=TOKEN, base_url=BASE_URL)
    print(f"Targeting {BASE_URL}")
    # Friendly preflight: if BASE_URL points at localhost but we're not
    # running on a host that can reach it, bail with a clear message
    # instead of letting httpx raise a cryptic ConnectError.
    if "localhost" in BASE_URL or "127.0.0.1" in BASE_URL:
        import socket

        host, _, port = BASE_URL.replace("http://", "").replace("https://", "").partition(":")
        port_int = int(port.rstrip("/") or "80")
        try:
            with socket.create_connection((host, port_int), timeout=2):
                pass
        except OSError as err:
            print()
            print(f"Cannot reach {BASE_URL} from this host: {err}")
            print("If you're running this from an E2B sandbox, 'localhost'")
            print("resolves to the sandbox itself. Run this script from your")
            print("dev machine instead, or change BASE_URL to a publicly-")
            print("reachable URL (deployed Vercel domain or ngrok tunnel).")
            return 1

    # workspace_id is auto-resolved from the session — the PAT is scoped
    # to specific workspaces; when it's a single workspace, the SDK uses
    # it without the caller having to look it up. Override via
    # `client.agents.invoke(..., workspace_id="...")` when needed.
    print(f"Invoking '{AGENT}'…")
    handle = client.agents.invoke(AGENT, input="Begin.")
    print(f"  invocationId = {handle.invocation_id}")

    # Callbacks fire once per NEW pending state (deduped by questionId).
    def on_question(pending: dict) -> None:
        qid = pending.get("questionId")
        text = pending.get("question", "")
        print()
        print("┌── Agent is asking ──────────────────────────────")
        print(f"│ questionId: {qid}")
        print(f"│ {text[:200]}")
        print("└─ Open the workspace's header bell to see the image + answer.")
        print()

    def on_approval(pending: dict) -> None:
        print(f"  approval requested: {pending.get('summary', '')}")

    try:
        result = handle.wait(
            timeout=10 * 60,  # 10 minutes — agent may iterate a few rounds
            poll_interval=2.0,
            on_question=on_question,
            on_approval=on_approval,
        )
    except esoul.InvocationTimeout:
        print()
        print("Local wait timed out. The run continues server-side; check the")
        print(f"workspace UI or call client.agents.get('{handle.invocation_id}').")
        return 2
    except esoul.InvocationError as err:
        print()
        print(f"Run errored: {err}")
        return 1

    print()
    print("┌── Run finished ─────────────────────────────────")
    print(f"│ status:     {result.status}")
    print(f"│ started_at: {result.started_at}")
    print(f"│ finished:   {result.finished_at}")
    print("│")
    if result.text:
        print("│ Output:")
        for line in result.text.splitlines():
            print(f"│   {line}")
    if result.router_handoff:
        print("│ Structured handoff:")
        for i, entry in enumerate(result.router_handoff):
            print(f"│   [{i}] {entry.get('text', '')[:140]}")
            for img in entry.get("images") or []:
                print(f"│         + image fileId={img.get('fileId')}")
    print("└─────────────────────────────────────────────────")
    return 0


if __name__ == "__main__":
    sys.exit(main())
