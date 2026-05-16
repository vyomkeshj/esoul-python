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

Run inside the sandbox (env vars are pre-populated):

    python lion_question_test.py

Run locally:

    ESOUL_TOKEN=esoul_pat_... \\
    ESOUL_BASE_URL=http://localhost:3000 \\
    python lion_question_test.py
"""

from __future__ import annotations

import os
import sys
import time

import esoul

WORKSPACE_ID = os.environ.get(
    "WORKSPACE_ID", "6ae852ff-4eb3-411c-89d3-8dbe089066f4"
)
AGENT = os.environ.get("AGENT", "test-agent")


def main() -> int:
    client = esoul.Esoul()  # ESOUL_TOKEN / ESOUL_BASE_URL from env

    print(f"Invoking '{AGENT}' on workspace {WORKSPACE_ID}…")
    handle = client.agents.invoke(
        WORKSPACE_ID,
        AGENT,
        input="Begin.",
    )
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
