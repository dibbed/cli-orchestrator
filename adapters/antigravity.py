"""Adapter for **Antigravity** CLI (`agy`).

Flags verified with ``agy --help`` (Windows, 2026-07):

* ``-p, --print``                     run a single prompt non-interactively
                                      (``--prompt`` is an alias; ``-p`` MUST be
                                      the LAST argument, prompt value follows it)
* ``--model <MODEL>``                 model for the session
* ``--conversation <ID>``             resume a previous conversation by id
* ``--dangerously-skip-permissions``  auto-approve all tool permissions
* ``--print-timeout <dur>``           print-mode wait (default 5m; tune via extra_args)

Antigravity prints plain text (no JSON envelope), so :meth:`extract_text`
returns stdout unchanged and the harness pulls the fenced ```json contract out
of it.  ``agy`` must be fed empty stdin on Windows (the native equivalent of the
documented ``< NUL``) which the harness supplies via ``subprocess.DEVNULL``.

Conversation-id capture is **best-effort** (the print-mode output format for the
id is not a documented contract); when unknown the harness re-injects context
instead of resuming.
"""

from __future__ import annotations

import re

from .base import CliAdapter

_CONV_LABEL_RE = re.compile(
    r"conversation(?:[ _-]?id)?[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9][A-Za-z0-9_-]{6,})",
    re.IGNORECASE,
)
_UUID_RE = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)


class AntigravityAdapter(CliAdapter):
    """Antigravity CLI adapter (verified against the installed version)."""

    name = "antigravity"
    default_executable = "agy"
    wants_devnull_stdin = True  # `agy` expects empty stdin in headless mode

    def build_command(
        self,
        prompt: str,
        model: str | None,
        session_id: str | None,
        cwd: str | None = None,
    ) -> list[str]:
        cmd = self.executable_argv()
        if session_id:
            cmd += ["--conversation", session_id]
        if model:
            cmd += ["--model", model]
        cmd += ["--dangerously-skip-permissions"]
        cmd += self.extra_args
        # `-p` / the prompt value must come LAST.
        cmd += ["-p", prompt]
        return cmd

    def parse_session_id(self, stdout: str, stderr: str) -> str | None:
        blob = f"{stdout or ''}\n{stderr or ''}"
        m = _CONV_LABEL_RE.search(blob)
        if m:
            return m.group(1)
        m = _UUID_RE.search(blob)
        return m.group(1) if m else None
