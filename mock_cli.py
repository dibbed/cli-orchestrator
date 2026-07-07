"""A fake coding CLI for testing the harness WITHOUT spending real API quota.

The harness sets three environment variables on every child call, which real
CLIs harmlessly ignore but this mock uses to behave deterministically:

    HARNESS_ROLE  = "orchestrator" | "worker"   -> which contract to emit
    HARNESS_CLI   = adapter name                 -> which output envelope to mimic
    HARNESS_TURN  = "1", "2", ...                -> drives delegate -> finalize

Behaviour:
* As the **Orchestrator**: emits `delegate` on turns 1-2, then `finalize` on
  turn >= 3 (a full delegate -> delegate -> finalize cycle).
* As the **Worker**: emits a valid `ok` result.
* When impersonating `claude_code`, wraps the reply in the same JSON envelope
  Claude Code prints with `--output-format json` (so session-id parsing and
  text extraction are exercised too); otherwise prints plain text.

Flags:
* ``--simulate-ratelimit`` -> print a 429 / rate-limit error to stderr and exit
  non-zero, so the stop-and-notify path can be verified end to end.

Unknown flags passed by the adapters (``-p``, ``--model``, ``--output-format``,
``--resume`` ...) are tolerated and ignored.
"""

from __future__ import annotations

import json
import os
import sys


def _extract_prompt(argv: list[str]) -> str:
    """Best-effort recovery of the prompt from whatever flags an adapter used."""
    for flag in ("-p", "--prompt", "--print"):
        if flag in argv:
            i = argv.index(flag)
            if i + 1 < len(argv):
                return argv[i + 1]
    # codex form: `exec <PROMPT> ...` (or `exec resume <id> <PROMPT> ...`)
    if "exec" in argv:
        i = argv.index("exec")
        rest = argv[i + 1 :]
        if rest and rest[0] == "resume":
            rest = rest[2:]  # skip "resume" and the session id
        for tok in rest:
            if not tok.startswith("-"):
                return tok
    # Fallback heuristic: the longest non-flag argument.
    candidates = [a for a in argv[1:] if not a.startswith("-")]
    return max(candidates, key=len) if candidates else ""


def _truncate(text: str, limit: int = 160) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def _orchestrator_reply(turn: int, prompt_echo: str) -> str:
    """Build the Orchestrator contract text for this turn."""
    if turn < 3:
        payload = {
            "action": "delegate",
            "worker_prompt": (
                f"[MOCK] Step {turn}: analyze the task and return a structured "
                f"3-point summary. Context: {prompt_echo}. "
                f"Make reasonable assumptions; do not ask questions."
            ),
            "worker_model": None,
            "final_answer": None,
            "reasoning": f"[MOCK turn {turn}] Optimized the task into delegation #{turn}.",
        }
        human = f"(mock orchestrator, turn {turn}) Delegating step {turn} to the worker."
    else:
        payload = {
            "action": "finalize",
            "worker_prompt": None,
            "worker_model": None,
            "final_answer": (
                "[MOCK] Final synthesized answer after 2 delegations. "
                "The worker's structured analyses were sufficient to complete the task."
            ),
            "reasoning": f"[MOCK turn {turn}] Worker results are sufficient; finalizing.",
        }
        human = f"(mock orchestrator, turn {turn}) Task complete - finalizing."
    return human + "\n\n```json\n" + json.dumps(payload, indent=2) + "\n```"


def _worker_reply(turn: int, prompt_echo: str) -> str:
    """Build the Worker contract text."""
    payload = {
        "status": "ok",
        "result": (
            f"[MOCK worker] Structured analysis for step {turn}:\n"
            f"1. Interpreted the request: {prompt_echo}\n"
            f"2. Applied the .skills/analyze rules.\n"
            f"3. Produced a concise, well-formed deliverable.\n"
            f"_Assumption: inputs are well-formed._"
        ),
        "artifacts": [],
        "message": "",
    }
    human = f"(mock worker, turn {turn}) Completed the delegated step."
    return human + "\n\n```json\n" + json.dumps(payload, indent=2) + "\n```"


def _emit(text: str, cli: str, role: str) -> None:
    """Print the reply, mimicking the target CLI's stdout envelope."""
    if cli == "claude_code":
        envelope = {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": text,
            "session_id": f"mock-{role}-session-0001",
            "total_cost_usd": 0.0,
            "num_turns": 1,
            "usage": {"input_tokens": 123, "output_tokens": 45},
        }
        sys.stdout.write(json.dumps(envelope))
    else:
        # codex / antigravity / gemini print plain text.
        sys.stdout.write(text)
    sys.stdout.write("\n")
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    if "--simulate-ratelimit" in argv:
        # Mimic a provider rate/usage limit and fail loudly on stderr.
        sys.stderr.write(
            "ERROR: 429 Too Many Requests - rate limit / usage limit reached "
            "(RESOURCE_EXHAUSTED). Please retry later.\n"
        )
        sys.stderr.flush()
        return 1

    role = os.environ.get("HARNESS_ROLE", "orchestrator").strip().lower()
    cli = os.environ.get("HARNESS_CLI", "claude_code").strip().lower()
    try:
        turn = int(os.environ.get("HARNESS_TURN", "1"))
    except ValueError:
        turn = 1

    prompt_echo = _truncate(_extract_prompt(argv))

    if role == "worker":
        text = _worker_reply(turn, prompt_echo)
    else:
        text = _orchestrator_reply(turn, prompt_echo)

    _emit(text, cli, role)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
