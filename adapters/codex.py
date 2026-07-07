"""Adapter for OpenAI's **Codex** CLI (`codex`).

Flags verified with ``codex --help`` and ``codex exec --help`` (Windows, 2026-07):

* ``codex exec "<PROMPT>"``                    run non-interactively (prompt is positional;
                                              ``-`` / omitted reads the prompt from stdin)
* ``-m, --model <MODEL>``                      model to use
* ``--dangerously-bypass-approvals-and-sandbox`` skip *all* approval prompts and sandboxing
                                              (the headless auto-approve switch)
* ``--skip-git-repo-check``                    required so Codex will run inside ``worker_dir``,
                                              which is **not** a git repo
* ``-C, --cd <DIR>``                           agent working root (we also set the child cwd)
* ``--color never``                            keep stdout free of ANSI escapes
* ``codex exec resume <SESSION_ID> "<PROMPT>"`` resume a prior session

On Windows ``codex`` resolves (via PATHEXT) to ``codex.cmd``; Python's
``subprocess`` launches ``.cmd`` shims directly with ``shell=False`` (verified).

Session id capture is **best-effort**: Codex's exec output format for the
session/rollout id is not a documented contract, so :meth:`parse_session_id`
scans conservatively and returns ``None`` when unsure -- in which case the
harness keeps context by re-injecting prior results instead of resuming.
"""

from __future__ import annotations

import re

from .base import CliAdapter

_SESSION_LABEL_RE = re.compile(
    r"(?:session|rollout|thread)[ _-]?id[\"']?\s*[:=]\s*[\"']?([0-9a-fA-F][0-9a-fA-F-]{7,})",
    re.IGNORECASE,
)
_UUID_RE = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)


class CodexAdapter(CliAdapter):
    """Codex CLI adapter (verified against the installed version)."""

    name = "codex"
    default_executable = "codex"  # -> codex.cmd on Windows via PATHEXT

    def build_command(
        self,
        prompt: str,
        model: str | None,
        session_id: str | None,
        cwd: str | None = None,
    ) -> list[str]:
        cmd = self.executable_argv()
        if session_id:
            # Best-effort resume; only reached when a session id was captured.
            cmd += ["exec", "resume", session_id, prompt]
        else:
            cmd += ["exec", prompt]
        if model:
            cmd += ["-m", model]
        cmd += [
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
            "--color",
            "never",
        ]
        if cwd:
            cmd += ["-C", cwd]
        cmd += self.extra_args
        return cmd

    def parse_session_id(self, stdout: str, stderr: str) -> str | None:
        blob = f"{stdout or ''}\n{stderr or ''}"
        m = _SESSION_LABEL_RE.search(blob)
        if m:
            return m.group(1)
        m = _UUID_RE.search(blob)
        return m.group(1) if m else None
