# cli-orchestrator

Two AI coding CLIs run in a loop, driven by a Windows-native Python harness:

- **Orchestrator (AI #1)** — lightweight. Turns your task into a crisp, optimized
  prompt and *routes*: delegate another step, or finalize.
- **Worker (AI #2)** — heavyweight. Runs headless inside an **isolated directory**
  (`worker_dir/`) that carries its own rules/skills, executes the actual task,
  and returns a structured result.

The harness alternates calls, carries session context across turns, parses each
agent's strict JSON contract, and **stops the whole loop and notifies you the
instant either AI hits a rate/usage limit** (also on timeout, missing CLI, or
auth failure).

**Design principle: context isolation.** The Orchestrator stays lean (its system
prompt is injected per call and it runs in a directory with *no* rules file). The
Worker carries the heavy, task-specific rules in `worker_dir/`. Each model stays
fast and cheap, and you can point the Worker at a lighter/cheaper model.

---

## Repository layout

```
cli-orchestrator/
├─ config.yaml                  # single source of truth (the swappable brain)
├─ config.mock.yaml             # ready-to-run mock dry-run (no quota spent)
├─ config.mock.ratelimit.yaml   # ready-to-run rate-limit stop-and-notify demo
├─ .env.example                 # secrets template (Telegram) -> copy to .env
├─ requirements.txt
├─ README.md
├─ harness.py                   # the orchestration loop
├─ notify.py                    # toast + sound + Telegram
├─ mock_cli.py                  # fake CLI for testing the loop without quota
├─ orchestrator.system.md       # Orchestrator system prompt (injected per call)
├─ task.txt                     # default task (used when none passed on CLI)
├─ adapters/
│  ├─ __init__.py               # name -> adapter registry
│  ├─ base.py                   # abstract CliAdapter + shared detection
│  ├─ claude_code.py            # `claude`      (VERIFIED)
│  ├─ codex.py                  # `codex`       (VERIFIED)
│  ├─ antigravity.py            # `agy`         (VERIFIED)
│  └─ gemini.py                 # `gemini`      (UNVERIFIED — not installed here)
├─ worker_dir/
│  ├─ CLAUDE.md                 # Worker rules for Claude Code
│  ├─ AGENTS.md                 # Worker rules for Codex / Antigravity
│  ├─ GEMINI.md                 # Worker rules for Gemini CLI
│  └─ .skills/analyze/SKILL.md  # example "structured analysis" skill
└─ logs/                        # created at runtime (one subfolder per run)
```

---

## Requirements

- **Windows 10/11** (primary target). Degrades on macOS/Linux where possible.
- **Python 3.11+** (developed/verified on 3.12.8).
- At least one supported CLI installed and logged in for a real run
  (`claude`, `codex`, `agy`, or `gemini`). **Not needed for the mock demos.**

## Install

```powershell
cd cli-orchestrator
python -m pip install -r requirements.txt
copy .env.example .env      # optional; only needed for Telegram notifications
```

Only `PyYAML` and `python-dotenv` are strictly required. `win11toast`/`plyer`
are optional toast backends (the harness falls back to PowerShell BurntToast,
then console-only). Telegram uses the standard library — no extra dependency.

---

## Quick start (mock — spend **zero** quota)

The mock lets you exercise the entire loop without calling any real API.

**1) Full delegate → delegate → finalize cycle:**

```powershell
python harness.py --config config.mock.yaml --task "Summarize the risk in the auth log"
```

Expected (exit code **0**):

```
[turn 1] ORCHESTRATOR (claude_code) -> delegate   (...s)  $... / ... tok
[turn 1] WORKER       (claude_code) -> status=ok  (...s)
[turn 2] ORCHESTRATOR (claude_code) -> delegate   (...s)
[turn 2] WORKER       (claude_code) -> status=ok  (...s)
[turn 3] ORCHESTRATOR (claude_code) -> finalize   (...s)
[turn 3] FINALIZED in 3 turn(s). Saved -> logs\run-...\final_answer.txt
```

**2) Rate-limit stop-and-notify path:**

```powershell
python harness.py --config config.mock.ratelimit.yaml --task "anything"
```

Expected (exit code **2**): the Orchestrator delegates, the Worker trips a
simulated `429`, and the harness logs a `RATE_LIMIT` event, fires the
**sound + toast** (and Telegram if enabled), and exits:

```
[turn 1] ORCHESTRATOR (claude_code) -> delegate ...
RATE_LIMIT | WORKER (claude_code) hit a rate/usage limit on turn 1 (exit=1). Snippet: ERROR: 429 ...
Notifications dispatched: {'sound': True, 'toast': True}
```

> **How the mock is wired:** both `config.mock*.yaml` set an `executable`
> override (`["python", "mock_cli.py"]`) on each role so the adapters launch the
> fake CLI instead of a real one. The harness sets `HARNESS_ROLE` / `HARNESS_CLI`
> / `HARNESS_TURN` on every child call (real CLIs ignore these; the mock uses
> them to behave deterministically). `--simulate-ratelimit` on the Worker's
> override makes it emit the `429`.
>
> To point your **own** `config.yaml` at the mock instead, add
> `executable: ["python", "mock_cli.py"]` under `orchestrator:` and `worker:`,
> set both `cwd: "."`, and (optionally) add `--simulate-ratelimit` to the
> Worker's override.

---

## Real run

```powershell
# task from task.txt:
python harness.py

# or inline:
python harness.py --task "Refactor utils.py and summarize the changes"

# or a different config:
python harness.py --config config.yaml "Do the thing"
```

Per-run output lands in `logs/run-<timestamp>/`:
`harness.log` (structured), `final_answer.txt`, and every call's full
`*.input.txt` / `*.stdout.txt` / `*.stderr.txt` for debugging.

---

## Switching Orchestrator ↔ Worker (Setup A / Setup B)

Edit **only** `config.yaml`. `cli:` must be one of
`claude_code | codex | antigravity | gemini`.

**Setup A (default): Orchestrator = Antigravity, Worker = Codex**

```yaml
orchestrator:
  cli: antigravity
  model: null
  keep_session: true
  cwd: "."
worker:
  cli: codex
  model: null
  keep_session: true
  cwd: "./worker_dir"
```

**Setup B: Orchestrator = Codex, Worker = Antigravity** — just swap them:

```yaml
orchestrator:
  cli: codex
  model: null
  keep_session: true
  cwd: "."
worker:
  cli: antigravity
  model: null
  keep_session: true
  cwd: "./worker_dir"
```

**Claude Code** is a drop-in for either role: `cli: claude_code` (optionally
`model: "sonnet"`/`"opus"`/`"fable"` or a full model id).

`model: null` means "use the CLI's own default model" (see assumptions).

---

## Notifications

Configured under `notifications:` in the config:

- **toast** — `win11toast` → `plyer` → PowerShell **BurntToast** → console-only.
- **sound** — `winsound` beep pattern.
- **telegram** — set `enabled: true`, then put `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_CHAT_ID` in `.env`. Uses only the standard library.

Test channels standalone:

```powershell
python notify.py --test                 # toast + sound
python notify.py --telegram --message "hi"   # requires .env
```

Fired on every **stop-and-notify** condition: rate/usage limit, per-call
timeout, CLI-not-found, and auth error (each with distinct text).

---

## The inter-agent protocol

Both agents must end every reply with **exactly one** fenced ` ```json ` block
(the harness grabs the **last** one; falls back to the last bare `{...}`; retries
the agent **once** with a "JSON only" reminder; then stops gracefully).

**Orchestrator contract**

```json
{
  "action": "delegate | finalize",
  "worker_prompt": "optimized prompt for the Worker (required when delegate)",
  "worker_model": "optional model override for this turn, or null",
  "final_answer": "the completed result (required when finalize)",
  "reasoning": "one or two lines on the decision"
}
```

**Worker contract**

```json
{
  "status": "ok | needs_clarification | error",
  "result": "the actual work product",
  "artifacts": ["optional file paths written"],
  "message": "short note (e.g. what was missing)"
}
```

Context handling: with a live, resumable session (`keep_session: true` **and** a
captured session id) the harness sends only the latest worker result (spec's
`Previous worker result:` template). Otherwise it re-sends the full,
self-contained context so any CLI works even without session support.

---

## Exit codes

| Code | Meaning                        |
|-----:|--------------------------------|
| 0    | finalized OK                   |
| 1    | unexpected error               |
| 2    | rate/usage limit (stop+notify) |
| 3    | per-call timeout (stop+notify) |
| 4    | CLI not found (stop+notify)    |
| 5    | auth error (stop+notify)       |
| 6    | unparseable agent output       |
| 7    | max_iterations reached         |
| 130  | interrupted (Ctrl+C)           |

Detect "stopped due to limit" from a wrapper script by checking for exit code 2.

---

## CLI flag verification (done with `--help` on this machine, 2026-07)

Per the spec, the reference command templates were treated as starting points
and verified against the installed CLIs. Summary of what was confirmed and
**corrected**:

### `claude` — Claude Code (`claude.exe`) — VERIFIED
Command used:
```
claude -p "<PROMPT>" --model <MODEL> --output-format json --dangerously-skip-permissions [--resume <SID>]
```
- `-p/--print`, `--output-format json`, `--model`, `-r/--resume`,
  `--dangerously-skip-permissions` all confirmed present.
- Session id is read from the JSON envelope's `session_id`; `result` holds the
  reply text; `total_cost_usd`/`usage` feed the progress line. No corrections.

### `codex` — Codex CLI (`codex.cmd`) — VERIFIED
Command used:
```
codex exec "<PROMPT>" -m <MODEL> --dangerously-bypass-approvals-and-sandbox --skip-git-repo-check --color never [-C <cwd>]
resume: codex exec resume <SID> "<PROMPT>" ...
```
- Confirmed the auto-approve/sandbox-bypass flag is
  **`--dangerously-bypass-approvals-and-sandbox`** (the template said "confirm
  exact names"). `--full-auto` is a milder alternative.
- **Added `--skip-git-repo-check`** — Codex otherwise refuses to run inside
  `worker_dir/`, which is not a git repo.
- Chose plain stdout + last-```json extraction over `--json` (JSONL): the
  streaming-JSON mode escapes the fenced block and defeats text extraction.
- On Windows `codex` resolves via PATHEXT to `codex.cmd` (the `.ps1` shim is not
  on PATHEXT); Python `subprocess` launches `.cmd` shims directly with
  `shell=False` (verified against `npm.CMD`).
- Session-id capture is **best-effort** (Codex's exec id format isn't a
  documented contract) — verify against your version if you rely on resume.

### `agy` — Antigravity (`agy.exe`) — VERIFIED
Command used:
```
agy [--conversation <SID>] [--model <MODEL>] --dangerously-skip-permissions -p "<PROMPT>"
```
- `-p` is the **last** argument (confirmed requirement); `--print`/`--prompt`
  are aliases.
- The installed version has **no `--headless` / `--approve all`** from the
  template — the correct auto-approve is **`--dangerously-skip-permissions`**.
- Empty stdin is supplied via `subprocess.DEVNULL` (the Windows-native
  equivalent of the template's shell `< NUL`; we never build shell strings).
- `--conversation <ID>` resumes; id capture is **best-effort**. `--print-timeout`
  (default 5m) exists — raise it via `extra_args` if your calls run long.

### `gemini` — Gemini CLI — NOT INSTALLED (UNVERIFIED)
Adapter written from the template (`gemini -p "<PROMPT>" -m <MODEL> --yolo`).
**Run `gemini --help` and confirm** the headless, model, and auto-approve flags
before using it; enable session/resume and JSON output there if your version
supports them.

Rate-limit (and auth) signals for every adapter live in `config.yaml` as
**editable, case-insensitive regex lists**; `is_rate_limited` matches them
against `stdout + stderr` or a known rate-limit exit code.

---

## Assumptions & decisions

1. **Config extensions (`executable`, `extra_args`).** Added two optional
   per-role keys beyond the given schema: `executable` (an argv override used by
   the mock dry-run and for odd install paths) and `extra_args` (extra flags
   appended to every call, so you can tune a CLI without editing Python).
2. **`auth_error_patterns`.** The spec asked for distinct auth-error handling, so
   auth signals are configurable per-adapter regex lists, mirroring the
   rate-limit lists.
3. **`model: null` defaults.** Exact accepted model strings could not be
   confirmed for every CLI (`agy models` needs auth; Codex doesn't list models
   via `--help`), so defaults are `null` = "use the CLI's own default model".
   Real examples are in `config.yaml` comments. Set explicit models when ready.
4. **Session strategy.** Verified session-resume only for Claude Code; it is
   best-effort for Codex/Antigravity and off for Gemini. When an id can't be
   captured, the harness keeps correctness by re-sending full self-contained
   context, so `keep_session: true` is always safe.
5. **Empty stdin everywhere.** All headless calls get `subprocess.DEVNULL` stdin
   so no CLI can block waiting on input (Antigravity specifically needs this).
6. **Windows shim handling.** `shutil.which` (honouring PATHEXT) resolves
   `codex` → `codex.cmd`; `.cmd` launches directly under `shell=False`; a
   `python` override in the mock configs is normalized to the exact
   `sys.executable`.
7. **Directory placement = context isolation.** The Orchestrator runs at the
   repo root (**no** `CLAUDE.md`/`AGENTS.md` there, on purpose) with its system
   prompt injected per call; the Worker runs in `worker_dir/` so the CLI
   auto-loads the local rules.
8. **UTF-8 forced** in every child (`PYTHONIOENCODING`/`PYTHONUTF8`, and
   `encoding="utf-8", errors="replace"`) so Persian/Unicode never corrupts.
9. **Prompt passing.** Prompts are passed as a single argv element (never a
   concatenated shell string). Extremely large prompts could exceed the Windows
   ~32 KB command-line limit; chunk the task if you hit that.
10. **Two mock configs added** (`config.mock.yaml`, `config.mock.ratelimit.yaml`)
    and a `--config` flag, to make the two required demos one command each. The
    default `config.yaml` stays configured for a real run (Setup A).
11. **Colored console** via Windows VT (enabled through `ctypes`); disable with
    `--no-color`. File logs are always plain.

---

## Troubleshooting

- **`CLI NOT FOUND` (exit 4):** the executable isn't on PATH. Install it, or set
  an `executable` override under the role in `config.yaml`.
- **`AUTH ERROR` (exit 5):** log in to the CLI (`claude`, `codex login`, `agy`,
  `gemini`) or set the relevant key in `.env`.
- **Unparseable output (exit 6):** the agent didn't emit the JSON contract even
  after the retry. Inspect `logs/run-*/turn*-*.stdout.txt`. Usually a model/model
  -string issue or a CLI that streamed non-final output.
- **No toast appears:** install `win11toast`/`plyer`, or the PowerShell
  `BurntToast` module (`Install-Module BurntToast`); otherwise you still get the
  console log + sound.
- **Codex won't run:** ensure `--skip-git-repo-check` is present (it is by
  default) and that you're logged in (`codex login`).
