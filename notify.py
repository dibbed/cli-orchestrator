"""Notification channels: Windows toast + sound + optional Telegram.

Used by the harness to alert you the instant either agent hits a rate/usage
limit (or times out, or a CLI is missing / unauthenticated). Every channel is
best-effort and independently guarded -- a failure in one never breaks the
others or the harness.

Run standalone to test your setup:

    python notify.py --test
    python notify.py --title "Hi" --message "hello" --telegram
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import threading
import urllib.parse
import urllib.request

logger = logging.getLogger("orchestrator.notify")

IS_WINDOWS = os.name == "nt"


# --------------------------------------------------------------------------- #
# Small util: never let a flaky notifier hang the harness.
# --------------------------------------------------------------------------- #
def _run_with_timeout(fn, timeout: float, label: str) -> bool:
    """Run ``fn()`` in a daemon thread; return True if it finished cleanly."""
    outcome: dict[str, object] = {}

    def _worker() -> None:
        try:
            fn()
            outcome["ok"] = True
        except Exception as exc:  # noqa: BLE001 - best-effort channel
            outcome["error"] = exc

    t = threading.Thread(target=_worker, name=f"notify-{label}", daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        logger.warning("Notification channel %r timed out after %.1fs", label, timeout)
        return False
    if "error" in outcome:
        logger.warning("Notification channel %r failed: %s", label, outcome["error"])
        return False
    return bool(outcome.get("ok"))


# --------------------------------------------------------------------------- #
# Toast
# --------------------------------------------------------------------------- #
def show_toast(title: str, message: str, *, timeout: float = 12.0) -> bool:
    """Show a Windows toast. Tries win11toast -> plyer -> PowerShell BurntToast.

    Returns True if any backend succeeded.
    """

    def _win11toast() -> None:
        import contextlib
        import io

        from win11toast import toast  # type: ignore

        # win11toast prints its activation-result dict to stdout; swallow it so
        # the harness console stays clean. (The main thread is blocked in
        # join() while this runs, so no harness output is lost.)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            toast(title, message, duration="short")

    if _run_with_timeout(_win11toast, timeout, "win11toast"):
        logger.debug("toast shown via win11toast")
        return True

    def _plyer() -> None:
        from plyer import notification  # type: ignore

        notification.notify(title=title, message=message, timeout=5)

    if _run_with_timeout(_plyer, timeout, "plyer"):
        logger.debug("toast shown via plyer")
        return True

    if IS_WINDOWS and _burnt_toast(title, message, timeout):
        logger.debug("toast shown via BurntToast")
        return True

    logger.warning("All toast backends failed (title=%r).", title)
    return False


def _burnt_toast(title: str, message: str, timeout: float) -> bool:
    """PowerShell BurntToast fallback (only if the module is installed)."""
    import subprocess

    # Pass text as argv to PowerShell to avoid any string interpolation issues.
    ps = (
        "if (Get-Module -ListAvailable -Name BurntToast) {"
        " Import-Module BurntToast;"
        " New-BurntToastNotification -Text $args[0], $args[1];"
        " exit 0 } else { exit 3 }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps, title, message],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return proc.returncode == 0
    except Exception as exc:  # noqa: BLE001
        logger.debug("BurntToast fallback failed: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# Sound
# --------------------------------------------------------------------------- #
def play_sound() -> bool:
    """Play a short attention beep pattern (Windows: winsound)."""
    if not IS_WINDOWS:
        # Best-effort bell on other platforms.
        try:
            print("\a", end="", flush=True)
            return True
        except Exception:  # noqa: BLE001
            return False
    try:
        import winsound

        for freq in (880, 660, 880):
            winsound.Beep(freq, 150)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("winsound.Beep failed (%s); trying MessageBeep.", exc)
        try:
            import winsound

            winsound.MessageBeep(winsound.MB_ICONHAND)
            return True
        except Exception as exc2:  # noqa: BLE001
            logger.warning("Sound notification failed: %s", exc2)
            return False


# --------------------------------------------------------------------------- #
# Telegram (stdlib only -- no extra dependency)
# --------------------------------------------------------------------------- #
def send_telegram(
    message: str,
    *,
    token: str | None = None,
    chat_id: str | None = None,
    timeout: float = 10.0,
) -> bool:
    """Send a Telegram message via the Bot API.

    Reads ``TELEGRAM_BOT_TOKEN`` / ``TELEGRAM_CHAT_ID`` from the environment when
    not passed explicitly. Returns True on HTTP 200 with ``ok: true``.
    """
    token = token or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        logger.warning(
            "Telegram enabled but TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env; skipping."
        )
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": message, "disable_web_page_preview": "true"}
    ).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed api host
            body = resp.read().decode("utf-8", "replace")
            ok = resp.status == 200 and '"ok":true' in body.replace(" ", "")
            if not ok:
                logger.warning("Telegram API returned non-ok: %s %s", resp.status, body[:200])
            return ok
    except Exception as exc:  # noqa: BLE001
        logger.warning("Telegram send failed: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #
def notify_all(
    notifications_cfg: dict,
    title: str,
    message: str,
) -> dict[str, bool]:
    """Fire every channel enabled in the ``notifications`` config block.

    Args:
        notifications_cfg: the ``notifications`` mapping from config.yaml.
        title: short notification title.
        message: notification body.

    Returns:
        ``{channel: success}`` for each channel attempted.
    """
    cfg = notifications_cfg or {}
    results: dict[str, bool] = {}

    if cfg.get("sound", True):
        results["sound"] = play_sound()
    if cfg.get("toast", True):
        results["toast"] = show_toast(title, message)

    tg = cfg.get("telegram") or {}
    if tg.get("enabled", False):
        results["telegram"] = send_telegram(f"{title}\n\n{message}")

    logger.info("Notifications dispatched: %s", results)
    return results


# --------------------------------------------------------------------------- #
# CLI entry point (manual testing)
# --------------------------------------------------------------------------- #
def _main() -> int:
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Test the notification channels.")
    parser.add_argument("--title", default="cli-orchestrator")
    parser.add_argument("--message", default="Test notification from notify.py")
    parser.add_argument("--toast", action="store_true", help="show a toast")
    parser.add_argument("--sound", action="store_true", help="play the beep")
    parser.add_argument("--telegram", action="store_true", help="send a Telegram message")
    parser.add_argument("--test", action="store_true", help="test toast + sound together")
    args = parser.parse_args()

    # Load .env so a standalone Telegram test can find the secrets.
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except Exception:  # noqa: BLE001
        pass

    if args.test or args.toast:
        print("toast ->", show_toast(args.title, args.message))
    if args.test or args.sound:
        print("sound ->", play_sound())
    if args.telegram:
        print("telegram ->", send_telegram(f"{args.title}\n\n{args.message}"))
    if not any([args.test, args.toast, args.sound, args.telegram]):
        parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
