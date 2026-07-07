"""Adapter for Anthropic's **Claude Code** CLI (`claude`).

Flags verified with ``claude --help`` (v2.x, Windows, 2026-07):

* ``-p, --print``                     headless / non-interactive output
* ``--output-format json``            single JSON envelope on stdout
                                      (contains ``result`` + ``session_id``)
* ``--model <model>``                 alias (``sonnet``/``opus``/``fable``) or full id
* ``-r, --resume <session_id>``       resume a conversation (works with --print)
* ``--dangerously-skip-permissions``  bypass all tool-permission prompts

Session continuity is fully supported: the session id is read straight out of
the JSON envelope and replayed via ``--resume`` on later turns.
"""

from __future__ import annotations

import json

from .base import CliAdapter


class ClaudeCodeAdapter(CliAdapter):
    """Claude Code CLI adapter (verified against the installed version)."""

    name = "claude_code"
    default_executable = "claude"

    def build_command(
        self,
        prompt: str,
        model: str | None,
        session_id: str | None,
        cwd: str | None = None,
    ) -> list[str]:
        cmd = self.executable_argv()
        cmd += ["-p", prompt]
        if model:
            cmd += ["--model", model]
        cmd += ["--output-format", "json", "--dangerously-skip-permissions"]
        if session_id:
            cmd += ["--resume", session_id]
        cmd += self.extra_args
        return cmd

    # -- output parsing ------------------------------------------------- #
    def _envelope(self, stdout: str) -> dict | None:
        """Parse the single JSON object Claude Code prints with -p --output-format json."""
        text = (stdout or "").strip()
        if not text:
            return None
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            # Be forgiving: grab the outermost {...} span and retry once.
            start, end = text.find("{"), text.rfind("}")
            if 0 <= start < end:
                try:
                    obj = json.loads(text[start : end + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    return None
            return None

    def extract_text(self, stdout: str, stderr: str) -> str:
        obj = self._envelope(stdout)
        if obj is not None:
            result = obj.get("result")
            if isinstance(result, str):
                return result.strip()
        return (stdout or "").strip()

    def parse_session_id(self, stdout: str, stderr: str) -> str | None:
        obj = self._envelope(stdout)
        if obj is not None:
            sid = obj.get("session_id")
            if isinstance(sid, str) and sid:
                return sid
        return None

    def usage_summary(self, stdout: str, stderr: str) -> dict[str, object]:
        obj = self._envelope(stdout)
        if obj is None:
            return {}
        summary: dict[str, object] = {}
        if isinstance(obj.get("total_cost_usd"), (int, float)):
            summary["cost_usd"] = obj["total_cost_usd"]
        usage = obj.get("usage")
        if isinstance(usage, dict):
            inp = usage.get("input_tokens")
            out = usage.get("output_tokens")
            if isinstance(inp, int) and isinstance(out, int):
                summary["tokens"] = inp + out
        if isinstance(obj.get("num_turns"), int):
            summary["turns"] = obj["num_turns"]
        return summary
