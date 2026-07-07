"""Abstract CLI adapter interface + shared behaviour.

Every supported coding CLI (Claude Code, Codex, Antigravity, Gemini) is wrapped
in a small :class:`CliAdapter` subclass.  An adapter knows four things and
nothing else:

* how to build a *headless* argv list for a single prompt
  (:meth:`build_command`),
* how to pull the assistant's textual reply out of that CLI's raw stdout
  (:meth:`extract_text`),
* how to recover a resumable session id, if the CLI exposes one
  (:meth:`parse_session_id`),
* how to recognise that CLI's rate/usage-limit and auth errors
  (:meth:`is_rate_limited` / :meth:`is_auth_error`, driven by editable regex
  lists from ``config.yaml``).

Because the interface is uniform, the harness can flip which CLI is the
Orchestrator and which is the Worker purely from ``config.yaml`` -- no Python
changes required.
"""

from __future__ import annotations

import re
import shutil
from abc import ABC, abstractmethod
from typing import Iterable


class AdapterError(RuntimeError):
    """Base class for adapter-level failures."""


class CliNotFoundError(AdapterError):
    """Raised when a CLI executable cannot be located on PATH."""


class CliAdapter(ABC):
    """Uniform, swappable wrapper around one coding CLI.

    Subclasses set :attr:`name` / :attr:`default_executable` and implement
    :meth:`build_command`.  The rest has sensible, override-able defaults.
    """

    #: Adapter key used in ``config.yaml`` (``orchestrator.cli`` / ``worker.cli``).
    name: str = "base"
    #: Bare executable name looked up on PATH (e.g. ``"claude"``, ``"codex"``).
    default_executable: str = ""
    #: Exit codes that *by themselves* mean "rate/usage limited" for this CLI.
    rate_limit_exit_codes: frozenset[int] = frozenset()
    #: Headless CLIs must never block waiting on stdin -> feed them empty stdin
    #: (``subprocess.DEVNULL``; the Windows-native equivalent of ``< NUL``).
    wants_devnull_stdin: bool = True

    def __init__(
        self,
        *,
        ratelimit_patterns: Iterable[str] | None = None,
        auth_error_patterns: Iterable[str] | None = None,
        executable: Iterable[str] | None = None,
        extra_args: Iterable[str] | None = None,
    ) -> None:
        """Configure an adapter instance for one role.

        Args:
            ratelimit_patterns: Case-insensitive regexes that mark a
                rate/usage-limit error (from ``config.yaml``).
            auth_error_patterns: Case-insensitive regexes that mark an
                authentication/login error.
            executable: Optional argv prefix that *overrides* the default
                executable -- e.g. ``["python", "mock_cli.py"]`` to point the
                adapter at ``mock_cli.py`` for a dry run.  When omitted, the
                default executable is resolved on PATH.
            extra_args: Extra flags appended to every command (lets you tune a
                CLI's behaviour without editing Python).
        """
        self.ratelimit_patterns: list[str] = list(ratelimit_patterns or [])
        self.auth_error_patterns: list[str] = list(auth_error_patterns or [])
        self.extra_args: list[str] = [str(a) for a in (extra_args or [])]
        self._executable_override: list[str] | None = (
            [str(x) for x in executable] if executable else None
        )
        self._rl_re: list[re.Pattern[str]] = [
            re.compile(p, re.IGNORECASE) for p in self.ratelimit_patterns
        ]
        self._auth_re: list[re.Pattern[str]] = [
            re.compile(p, re.IGNORECASE) for p in self.auth_error_patterns
        ]

    # ------------------------------------------------------------------ #
    # Executable resolution
    # ------------------------------------------------------------------ #
    def executable_argv(self) -> list[str]:
        """Return the argv prefix that launches this CLI.

        Uses the config ``executable`` override verbatim if present, otherwise
        resolves :attr:`default_executable` on PATH via :func:`shutil.which`
        (which honours ``PATHEXT`` and so returns ``codex.cmd`` etc.).

        Raises:
            CliNotFoundError: if the executable cannot be found.
        """
        if self._executable_override:
            return list(self._executable_override)
        resolved = shutil.which(self.default_executable)
        if not resolved:
            raise CliNotFoundError(
                f"CLI executable '{self.default_executable}' for adapter "
                f"'{self.name}' was not found on PATH. Install it, or set an "
                f"'executable' override under the role in config.yaml."
            )
        return [resolved]

    # ------------------------------------------------------------------ #
    # Required / override-able interface
    # ------------------------------------------------------------------ #
    @abstractmethod
    def build_command(
        self,
        prompt: str,
        model: str | None,
        session_id: str | None,
        cwd: str | None,
    ) -> list[str]:
        """Build the full argv list for a single headless call.

        Never build a shell string -- always return an argv list so the harness
        can run it with ``shell=False``.  ``cwd`` is informational; the harness
        sets the child's working directory itself.
        """

    def parse_session_id(self, stdout: str, stderr: str) -> str | None:
        """Return a resumable session id from CLI output, or ``None``.

        Default: ``None`` (no session continuity).  Subclasses override when
        the CLI exposes a session/conversation id.
        """
        return None

    def extract_text(self, stdout: str, stderr: str) -> str:
        """Return the assistant's textual reply from raw CLI output.

        Default: stdout as-is (correct for CLIs that print plain text).  CLIs
        that wrap the reply in JSON (Claude Code) override this.
        """
        return (stdout or "").strip()

    def usage_summary(self, stdout: str, stderr: str) -> dict[str, object]:
        """Return ``{tokens, cost_usd, ...}`` if the CLI reports it, else ``{}``."""
        return {}

    # ------------------------------------------------------------------ #
    # Shared error detection (config-driven regex lists)
    # ------------------------------------------------------------------ #
    def is_rate_limited(self, exit_code: int, stdout: str, stderr: str) -> bool:
        """True if this call hit a rate/usage limit.

        Matches a known rate-limit exit code, OR any configured regex against
        ``stdout + stderr`` (case-insensitive).
        """
        if exit_code in self.rate_limit_exit_codes:
            return True
        blob = f"{stdout or ''}\n{stderr or ''}"
        return any(rx.search(blob) for rx in self._rl_re)

    def is_auth_error(self, exit_code: int, stdout: str, stderr: str) -> bool:
        """True if this call failed because the CLI is not authenticated."""
        blob = f"{stdout or ''}\n{stderr or ''}"
        return any(rx.search(blob) for rx in self._auth_re)

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        exe = self._executable_override or [self.default_executable]
        return f"<{type(self).__name__} name={self.name!r} exe={exe!r}>"
