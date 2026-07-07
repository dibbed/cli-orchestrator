"""cli-orchestrator harness (Windows-native).

Runs the Orchestrator <-> Worker loop:

    task -> Orchestrator (optimize + route) -> [delegate] -> Worker (execute)
         -> inject result -> Orchestrator -> ... -> [finalize] -> done

Responsibilities:
* load config.yaml + .env, instantiate the two adapters by name,
* alternate headless CLI calls, carrying session context across turns,
* parse each agent's strict JSON contract (last fenced ```json block, with one
  "JSON only" retry, then a graceful stop),
* STOP THE WHOLE LOOP AND NOTIFY the instant either agent hits a rate/usage
  limit -- and likewise on per-call timeout, missing CLI, or auth failure,
* log everything (structured file log + colored per-turn console line + each
  agent's full raw input/output).

Exit codes:
    0  finalized OK          4  CLI not found
    1  unexpected error      5  auth error
    2  rate/usage limit      6  unparseable agent output
    3  per-call timeout      7  max_iterations reached
    130 interrupted (Ctrl+C)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import yaml

import notify
from adapters import CliNotFoundError, create_adapter
from adapters.base import CliAdapter

BASE_DIR = Path(__file__).resolve().parent

# ----- exit codes ---------------------------------------------------------- #
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_RATE_LIMIT = 2
EXIT_TIMEOUT = 3
EXIT_CLI_NOT_FOUND = 4
EXIT_AUTH = 5
EXIT_PARSE = 6
EXIT_MAX_ITERS = 7
EXIT_INTERRUPT = 130

logger = logging.getLogger("orchestrator.harness")


# =========================================================================== #
# Errors that stop the loop (each carries a notification + exit code)
# =========================================================================== #
class HarnessStop(Exception):
    """A condition that cleanly stops the loop and notifies the user."""

    def __init__(self, exit_code: int, title: str, message: str, log_event: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.title = title
        self.message = message
        self.log_event = log_event


class ContractParseError(Exception):
    """Raised when an agent reply cannot be reduced to the required JSON contract."""


class CliTimeoutError(Exception):
    """Raised when a single CLI call exceeds ``loop.timeout_seconds``."""


# =========================================================================== #
# Result of a single CLI call
# =========================================================================== #
@dataclass
class CallResult:
    exit_code: int
    stdout: str
    stderr: str
    latency: float
    command: list[str] = field(default_factory=list)


# =========================================================================== #
# Colored console logging
# =========================================================================== #
_COLORS = {
    "reset": "\033[0m",
    "grey": "\033[90m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "bold": "\033[1m",
}
_LEVEL_COLOR = {
    logging.DEBUG: "grey",
    logging.INFO: "reset",
    logging.WARNING: "yellow",
    logging.ERROR: "red",
    logging.CRITICAL: "red",
}


def _enable_windows_ansi() -> bool:
    """Enable ANSI/VT processing on the Windows console. Returns True on success."""
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # ENABLE_PROCESSED_OUTPUT(1)|ENABLE_WRAP_AT_EOL_OUTPUT(2)|VT_PROCESSING(4)
        return bool(kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7))
    except Exception:  # noqa: BLE001
        return False


class _ConsoleFormatter(logging.Formatter):
    """Colors console lines by level, or by an explicit ``color`` extra."""

    def __init__(self, use_color: bool) -> None:
        super().__init__("%(message)s")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        if not self.use_color:
            return msg
        color = getattr(record, "color", None) or _LEVEL_COLOR.get(record.levelno, "reset")
        return f"{_COLORS.get(color, '')}{msg}{_COLORS['reset']}"


def setup_logging(run_dir: Path, use_color: bool) -> None:
    """Attach a plain file handler and a colored console handler to the root logger."""
    root = logging.getLogger("orchestrator")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()

    file_handler = logging.FileHandler(run_dir / "harness.log", encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    )
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(_ConsoleFormatter(use_color))
    root.addHandler(console)


def progress(message: str, color: str = "reset") -> None:
    """Emit a colored per-turn progress line (console) that is also logged to file."""
    logger.info(message, extra={"color": color})


# =========================================================================== #
# Config
# =========================================================================== #
def load_config(path: Path) -> dict:
    """Load and lightly validate config.yaml."""
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    for role in ("orchestrator", "worker"):
        if role not in cfg or "cli" not in cfg[role]:
            raise ValueError(f"config: '{role}.cli' is required.")
    cfg.setdefault("loop", {})
    cfg.setdefault("run", {})
    cfg.setdefault("adapters", {})
    cfg.setdefault("notifications", {})
    return cfg


def build_adapter(cfg: dict, role: str) -> CliAdapter:
    """Instantiate the adapter for ``role`` ('orchestrator' | 'worker')."""
    role_cfg = cfg[role]
    name = role_cfg["cli"]
    adapter_cfg = (cfg.get("adapters") or {}).get(name, {}) or {}
    return create_adapter(
        name,
        ratelimit_patterns=adapter_cfg.get("ratelimit_patterns", []),
        auth_error_patterns=adapter_cfg.get("auth_error_patterns", []),
        executable=role_cfg.get("executable"),
        extra_args=role_cfg.get("extra_args"),
    )


def resolve_cwd(cfg_value: str | None) -> Path:
    """Resolve a config cwd (relative to the repo root) to an absolute Path."""
    if not cfg_value:
        return BASE_DIR
    p = Path(cfg_value)
    return p if p.is_absolute() else (BASE_DIR / p).resolve()


# =========================================================================== #
# JSON contract extraction
# =========================================================================== #
_FENCED_JSON_RE = re.compile(r"```json\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _iter_balanced_objects(text: str) -> list[str]:
    """Return every top-level ``{...}`` span in ``text`` (string-aware)."""
    spans: list[str] = []
    depth = 0
    start: int | None = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    spans.append(text[start : i + 1])
                    start = None
    return spans


def extract_contract(text: str) -> dict:
    """Extract the agent's JSON contract object from a noisy reply.

    Strategy (spec step 7): prefer the **last** fenced ```json block; fall back
    to the last balanced ``{...}`` object. First candidate that ``json.loads``
    into a dict wins.

    Raises:
        ContractParseError: if no candidate parses to a JSON object.
    """
    candidates: list[str] = []
    candidates.extend(reversed(_FENCED_JSON_RE.findall(text)))
    candidates.extend(reversed(_iter_balanced_objects(text)))
    for cand in candidates:
        cand = cand.strip()
        if not cand:
            continue
        try:
            obj = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ContractParseError("No valid JSON contract object found in reply.")


def validate_orchestrator(obj: dict) -> dict:
    """Validate the Orchestrator contract shape."""
    action = obj.get("action")
    if action not in ("delegate", "finalize"):
        raise ContractParseError(f"Orchestrator: bad action {action!r}.")
    if action == "delegate" and not (obj.get("worker_prompt") or "").strip():
        raise ContractParseError("Orchestrator: delegate requires a non-empty worker_prompt.")
    if action == "finalize" and not (obj.get("final_answer") or "").strip():
        raise ContractParseError("Orchestrator: finalize requires a non-empty final_answer.")
    return obj


def validate_worker(obj: dict) -> dict:
    """Validate the Worker contract shape."""
    if obj.get("status") not in ("ok", "needs_clarification", "error"):
        raise ContractParseError(f"Worker: bad status {obj.get('status')!r}.")
    if "result" not in obj:
        raise ContractParseError("Worker: missing 'result'.")
    return obj


# =========================================================================== #
# Subprocess execution
# =========================================================================== #
_PYTHON_ALIASES = {"python", "python3", "py"}


def call_cli(
    adapter: CliAdapter,
    *,
    prompt: str,
    model: str | None,
    session_id: str | None,
    cwd: Path,
    role: str,
    turn: int,
    attempt: int,
    timeout: int,
    run_dir: Path,
) -> CallResult:
    """Run one headless CLI call and capture its output.

    Forces UTF-8 in the child (so Persian/Unicode never corrupts on Windows),
    feeds empty stdin to headless CLIs, enforces the per-call timeout by killing
    the child, and saves the full input/stdout/stderr to ``run_dir``.

    Raises:
        CliNotFoundError: if the executable is missing.
        CliTimeoutError: if the call exceeds ``timeout``.
    """
    command = adapter.build_command(prompt, model, session_id, str(cwd))
    # Normalize a "python" override to the exact interpreter running the harness
    # (used by the mock dry-run configs).
    if command and command[0].lower() in _PYTHON_ALIASES:
        command[0] = sys.executable

    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["HARNESS_ROLE"] = role
    env["HARNESS_CLI"] = adapter.name
    env["HARNESS_TURN"] = str(turn)

    # Persist the exact input for debugging.
    stem = f"turn{turn:02d}-{role}-attempt{attempt}"
    (run_dir / f"{stem}.input.txt").write_text(prompt, encoding="utf-8")

    printable = command[:2] + (["<prompt>"] if len(command) > 2 else [])
    logger.debug("exec %s (cwd=%s) prompt=%r", printable, cwd, _truncate(prompt, 200))

    stdin = subprocess.DEVNULL if adapter.wants_devnull_stdin else None
    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=stdin,
            cwd=str(cwd),
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise CliNotFoundError(
            f"Could not launch {command[0]!r} for adapter '{adapter.name}': {exc}"
        ) from exc

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        _dump_raw(run_dir, stem, stdout, stderr)
        raise CliTimeoutError(f"{adapter.name} call exceeded {timeout}s")
    except KeyboardInterrupt:
        proc.kill()
        proc.communicate()
        raise

    latency = time.monotonic() - start
    _dump_raw(run_dir, stem, stdout, stderr)
    return CallResult(proc.returncode, stdout or "", stderr or "", latency, command)


def _dump_raw(run_dir: Path, stem: str, stdout: str, stderr: str) -> None:
    (run_dir / f"{stem}.stdout.txt").write_text(stdout or "", encoding="utf-8")
    (run_dir / f"{stem}.stderr.txt").write_text(stderr or "", encoding="utf-8")


def _truncate(text: str, limit: int) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."


# =========================================================================== #
# One agent turn (call + limit checks + parse, with a single JSON-only retry)
# =========================================================================== #
def run_agent_turn(
    adapter: CliAdapter,
    *,
    agent_label: str,
    prompt: str,
    model: str | None,
    session_id: str | None,
    keep_session: bool,
    cwd: Path,
    role: str,
    turn: int,
    timeout: int,
    run_dir: Path,
    validator,
) -> tuple[dict, str | None, CallResult]:
    """Run an agent, detect stop conditions, and parse its contract.

    Returns ``(parsed_contract, new_session_id, last_call_result)``.
    Raises :class:`HarnessStop` on any stop condition.
    """
    last_result: CallResult | None = None
    for attempt in (1, 2):
        ask = prompt
        if attempt == 2:
            ask = (
                prompt
                + "\n\nREMINDER: Respond with ONLY the required JSON contract as a "
                "single fenced ```json block. No prose after the block."
            )
        try:
            result = call_cli(
                adapter,
                prompt=ask,
                model=model,
                session_id=session_id,
                cwd=cwd,
                role=role,
                turn=turn,
                attempt=attempt,
                timeout=timeout,
                run_dir=run_dir,
            )
        except CliNotFoundError as exc:
            raise HarnessStop(
                EXIT_CLI_NOT_FOUND,
                "cli-orchestrator: CLI NOT FOUND",
                f"{agent_label} ({adapter.name}) could not be launched: {exc}",
                "CLI_NOT_FOUND",
            ) from exc
        except CliTimeoutError as exc:
            raise HarnessStop(
                EXIT_TIMEOUT,
                "cli-orchestrator: TIMEOUT",
                f"{agent_label} ({adapter.name}) timed out after {timeout}s on turn {turn}.",
                "TIMEOUT",
            ) from exc

        last_result = result
        snippet = _truncate((result.stderr or result.stdout or "").strip(), 300)

        # --- stop-and-notify checks (order matters) --------------------- #
        if adapter.is_rate_limited(result.exit_code, result.stdout, result.stderr):
            raise HarnessStop(
                EXIT_RATE_LIMIT,
                "cli-orchestrator: RATE LIMIT hit",
                f"{agent_label} ({adapter.name}) hit a rate/usage limit on turn "
                f"{turn} (exit={result.exit_code}). Snippet: {snippet}",
                "RATE_LIMIT",
            )
        if adapter.is_auth_error(result.exit_code, result.stdout, result.stderr):
            raise HarnessStop(
                EXIT_AUTH,
                "cli-orchestrator: AUTH ERROR",
                f"{agent_label} ({adapter.name}) is not authenticated (turn {turn}). "
                f"Snippet: {snippet}",
                "AUTH_ERROR",
            )

        # capture a resumable session id (best-effort; only when keeping session)
        new_sid = session_id
        if keep_session:
            parsed_sid = adapter.parse_session_id(result.stdout, result.stderr)
            if parsed_sid:
                new_sid = parsed_sid

        # --- parse the contract ---------------------------------------- #
        text = adapter.extract_text(result.stdout, result.stderr)
        try:
            contract = validator(extract_contract(text))
            return contract, new_sid, result
        except ContractParseError as exc:
            if attempt == 1:
                logger.warning(
                    "%s produced unparseable output (%s); retrying with a JSON-only reminder.",
                    agent_label,
                    exc,
                )
                continue
            logger.error("%s failed to produce valid JSON twice. Raw text follows:", agent_label)
            logger.error("%s", _truncate(text, 800))
            raise HarnessStop(
                EXIT_PARSE,
                "cli-orchestrator: stopped (unparseable output)",
                f"{agent_label} ({adapter.name}) did not return the JSON contract "
                f"after a retry on turn {turn}. See logs.",
                "PARSE_FAILURE",
            ) from exc

    # Unreachable, but keeps type-checkers happy.
    raise HarnessStop(
        EXIT_ERROR, "cli-orchestrator: internal error", "agent loop fell through", "INTERNAL"
    )


# =========================================================================== #
# Prompt assembly
# =========================================================================== #
def worker_display(entry: dict) -> str:
    """Render a stored worker result for injection into the Orchestrator prompt."""
    parts = [str(entry.get("result", "")).strip()]
    status = entry.get("status")
    if status and status != "ok":
        parts.append(f"[worker status: {status}]")
    if entry.get("message"):
        parts.append(f"[worker message: {entry['message']}]")
    return "\n".join(p for p in parts if p)


def build_orchestrator_input(
    system_md: str, task: str, history: list[dict], resuming: bool
) -> str:
    """Assemble the Orchestrator prompt for this turn.

    When resuming a live session, send only the latest worker result (spec step
    5). Otherwise send the full self-contained context so ANY CLI -- even one
    without session support -- has everything it needs.
    """
    if resuming and history:
        return (
            f"Previous worker result:\n{worker_display(history[-1])}\n\n"
            "Decide the next action (delegate or finalize) and respond with the "
            "Orchestrator JSON contract."
        )
    parts = [system_md, f"\n\n# TASK\n{task}\n"]
    if history:
        parts.append("\n# PRIOR WORKER RESULTS (most recent last)\n")
        for i, entry in enumerate(history, 1):
            parts.append(f"\n## Delegation {i}\n{worker_display(entry)}\n")
    parts.append(
        "\n# YOUR TURN\nReturn exactly one Orchestrator JSON contract block "
        "(action = delegate or finalize)."
    )
    return "".join(parts)


# =========================================================================== #
# Task input
# =========================================================================== #
def read_task(args_task: str | None, cfg: dict) -> str:
    """Resolve the task from the CLI arg, else the configured task file."""
    if args_task:
        return args_task.strip()
    task_file = (cfg.get("run") or {}).get("task_file", "task.txt")
    path = resolve_cwd(task_file)
    if path.exists():
        text = path.read_text(encoding="utf-8").strip()
        if text:
            return text
    raise HarnessStop(
        EXIT_ERROR,
        "cli-orchestrator: no task",
        f"No task provided. Pass one on the command line or create {path}.",
        "NO_TASK",
    )


# =========================================================================== #
# Main loop
# =========================================================================== #
def run(cfg: dict, task: str, run_dir: Path) -> int:
    """Execute the Orchestrator<->Worker loop. Returns a process exit code."""
    orch = build_adapter(cfg, "orchestrator")
    worker = build_adapter(cfg, "worker")

    orch_cfg = cfg["orchestrator"]
    worker_cfg = cfg["worker"]
    loop_cfg = cfg["loop"]
    max_iters = int(loop_cfg.get("max_iterations", 12))
    timeout = int(loop_cfg.get("timeout_seconds", 600))

    orch_cwd = resolve_cwd(orch_cfg.get("cwd", "."))
    worker_cwd = resolve_cwd(worker_cfg.get("cwd", "./worker_dir"))
    orch_keep = bool(orch_cfg.get("keep_session", True))
    worker_keep = bool(worker_cfg.get("keep_session", True))
    orch_model = orch_cfg.get("model")
    worker_default_model = worker_cfg.get("model")

    system_md = (BASE_DIR / "orchestrator.system.md").read_text(encoding="utf-8")

    progress(
        f"Orchestrator = {orch.name}  |  Worker = {worker.name}  |  "
        f"max_iterations = {max_iters}  |  timeout = {timeout}s",
        "bold",
    )
    progress(f"TASK: {_truncate(task, 200)}", "cyan")

    history: list[dict] = []
    orch_session: str | None = None
    worker_session: str | None = None

    for turn in range(1, max_iters + 1):
        # ---- Orchestrator ------------------------------------------------ #
        resuming = orch_keep and orch_session is not None
        orch_input = build_orchestrator_input(system_md, task, history, resuming)
        contract, orch_session, res = run_agent_turn(
            orch,
            agent_label="ORCHESTRATOR",
            prompt=orch_input,
            model=orch_model,
            session_id=orch_session,
            keep_session=orch_keep,
            cwd=orch_cwd,
            role="orchestrator",
            turn=turn,
            timeout=timeout,
            run_dir=run_dir,
            validator=validate_orchestrator,
        )
        action = contract["action"]
        progress(
            f"[turn {turn}] ORCHESTRATOR ({orch.name}) -> {action}  "
            f"({res.latency:.2f}s){_usage_str(orch, res)}",
            "cyan",
        )
        logger.debug("orchestrator reasoning: %s", contract.get("reasoning", ""))

        if action == "finalize":
            final = str(contract.get("final_answer", "")).strip()
            out_path = run_dir / "final_answer.txt"
            out_path.write_text(final, encoding="utf-8")
            progress(f"[turn {turn}] FINALIZED in {turn} turn(s). Saved -> {out_path}", "green")
            print("\n" + "=" * 70 + "\nFINAL ANSWER\n" + "=" * 70)
            print(final)
            print("=" * 70)
            return EXIT_OK

        # ---- Worker ------------------------------------------------------ #
        worker_prompt = str(contract["worker_prompt"])
        worker_model = contract.get("worker_model") or worker_default_model
        w_resuming = worker_keep and worker_session is not None
        w_contract, worker_session, w_res = run_agent_turn(
            worker,
            agent_label="WORKER",
            prompt=worker_prompt,
            model=worker_model,
            session_id=worker_session if w_resuming else None,
            keep_session=worker_keep,
            cwd=worker_cwd,
            role="worker",
            turn=turn,
            timeout=timeout,
            run_dir=run_dir,
            validator=validate_worker,
        )
        status = w_contract.get("status")
        artifacts = w_contract.get("artifacts") or []
        history.append(
            {
                "status": status,
                "result": w_contract.get("result", ""),
                "message": w_contract.get("message", ""),
                "artifacts": artifacts,
            }
        )
        art_str = f"  artifacts={artifacts}" if artifacts else ""
        progress(
            f"[turn {turn}] WORKER ({worker.name}) -> status={status}  "
            f"({w_res.latency:.2f}s){_usage_str(worker, w_res)}{art_str}",
            "magenta",
        )

    # Ran out of turns without a finalize.
    raise HarnessStop(
        EXIT_MAX_ITERS,
        "cli-orchestrator: stopped (max iterations)",
        f"Reached max_iterations ({max_iters}) without a finalize. See {run_dir}.",
        "MAX_ITERATIONS",
    )


def _usage_str(adapter: CliAdapter, res: CallResult) -> str:
    """Format tokens/cost for the progress line, if the CLI reported them."""
    usage = adapter.usage_summary(res.stdout, res.stderr)
    bits = []
    if "cost_usd" in usage:
        bits.append(f"${usage['cost_usd']:.4f}")
    if "tokens" in usage:
        bits.append(f"{usage['tokens']} tok")
    return "  " + " / ".join(bits) if bits else ""


# =========================================================================== #
# Entry point
# =========================================================================== #
def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Two-agent CLI orchestrator (Orchestrator <-> Worker loop)."
    )
    parser.add_argument("task", nargs="*", help="the task (overrides run.task_file)")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--task", dest="task_opt", default=None, help="the task, as an option")
    parser.add_argument("--no-color", action="store_true", help="disable colored console output")
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)

    # .env for secrets (Telegram, API keys).
    try:
        from dotenv import load_dotenv

        load_dotenv(BASE_DIR / ".env")
    except Exception:  # noqa: BLE001
        pass

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    run_dir = BASE_DIR / "logs" / f"run-{timestamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    use_color = not args.no_color and _enable_windows_ansi()
    setup_logging(run_dir, use_color)

    config_path = resolve_cwd(args.config)
    logger.info("cli-orchestrator run %s", timestamp)
    logger.info("config=%s  logs=%s", config_path, run_dir)

    cfg: dict = {}
    try:
        cfg = load_config(config_path)
        task_arg = args.task_opt or (" ".join(args.task).strip() or None)
        task = read_task(task_arg, cfg)
        return run(cfg, task, run_dir)
    except HarnessStop as stop:
        logger.error("%s | %s", stop.log_event, stop.message)
        notify.notify_all(cfg.get("notifications", {}), stop.title, stop.message)
        return stop.exit_code
    except KeyboardInterrupt:
        logger.warning("Interrupted by user (Ctrl+C). Child processes terminated.")
        return EXIT_INTERRUPT
    except Exception as exc:  # noqa: BLE001
        logger.exception("Unexpected error: %s", exc)
        try:
            notify.notify_all(
                cfg.get("notifications", {}),
                "cli-orchestrator: crashed",
                f"Unexpected error: {exc}",
            )
        except Exception:  # noqa: BLE001
            pass
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
