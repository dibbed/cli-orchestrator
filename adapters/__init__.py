"""CLI adapter registry.

The harness instantiates adapters *by name* from :data:`ADAPTERS`, so which CLI
plays Orchestrator and which plays Worker is decided entirely in ``config.yaml``.
Add a new CLI by dropping a ``CliAdapter`` subclass in this package and
registering it below.
"""

from __future__ import annotations

from .antigravity import AntigravityAdapter
from .base import AdapterError, CliAdapter, CliNotFoundError
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .gemini import GeminiAdapter

#: name -> adapter class
ADAPTERS: dict[str, type[CliAdapter]] = {
    cls.name: cls
    for cls in (
        ClaudeCodeAdapter,
        CodexAdapter,
        AntigravityAdapter,
        GeminiAdapter,
    )
}

__all__ = [
    "ADAPTERS",
    "AdapterError",
    "CliAdapter",
    "CliNotFoundError",
    "create_adapter",
]


def create_adapter(name: str, **kwargs) -> CliAdapter:
    """Instantiate the adapter registered under ``name``.

    Args:
        name: Adapter key (``claude_code`` / ``codex`` / ``antigravity`` / ``gemini``).
        **kwargs: Forwarded to the adapter constructor (``ratelimit_patterns``,
            ``auth_error_patterns``, ``executable``, ``extra_args``).

    Raises:
        AdapterError: if ``name`` is not a registered adapter.
    """
    try:
        cls = ADAPTERS[name]
    except KeyError:
        known = ", ".join(sorted(ADAPTERS))
        raise AdapterError(
            f"Unknown adapter '{name}'. Registered adapters: {known}."
        ) from None
    return cls(**kwargs)
