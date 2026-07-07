"""Adapter for Google's **Gemini** CLI (`gemini`).

NOT INSTALLED on the build machine, so these flags come from the reference
template and are **UNVERIFIED**.  Before using Gemini as Orchestrator or Worker,
run ``gemini --help`` and correct anything below (see README).

Reference template:

* ``gemini -p "<PROMPT>"``   headless prompt
* ``-m <MODEL>``             model
* ``--yolo``                 auto-approve all actions (equivalent of the other
                             CLIs' "skip permissions")

Notes / things to confirm against your installed version:
* Gemini CLI may expose ``-o json`` / ``--output-format json``; if you enable
  it, override :meth:`extract_text` to unwrap the reply.  By default we treat
  stdout as plain text.
* Headless session/resume support is not assumed here -> :meth:`parse_session_id`
  returns ``None`` and the harness keeps context by re-injecting prior results.
"""

from __future__ import annotations

from .base import CliAdapter


class GeminiAdapter(CliAdapter):
    """Gemini CLI adapter (UNVERIFIED -- confirm flags with ``gemini --help``)."""

    name = "gemini"
    default_executable = "gemini"

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
            cmd += ["-m", model]
        cmd += ["--yolo"]  # auto-approve; VERIFY the exact flag for your version
        cmd += self.extra_args
        # NOTE: `session_id` is intentionally unused -- headless resume is not
        # confirmed for the Gemini CLI. Enable it here once verified.
        return cmd
