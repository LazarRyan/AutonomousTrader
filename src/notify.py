"""
macOS notification helper.

Best-effort, non-critical UX layer -- a notification is a "nice to know",
never a decision input, so failures here are caught and printed, never
raised. Same discipline as src.db.write_signal's best-effort telemetry
writes, deliberately NOT the loud-crash discipline of src.db.write_audit_log
(where a missing audit row is itself the bug). Nothing in the actual
trading pipeline (risk scoring, safety rails, execution) depends on a
notification actually arriving -- it's purely informational, fired after
the real decision/persistence has already happened.

Three backends, tried in this order:

  1. `terminal-notifier` (https://github.com/julienXX/terminal-notifier),
     if installed (checked each call: PATH first, then Homebrew's known
     install locations directly -- launchd's minimal PATH doesn't include
     Homebrew's bin dir, see TERMINAL_NOTIFIER_FALLBACK_PATHS). PREFERRED, and
     the one confirmed working on this machine -- with a real caveat found
     in practice (2026-07-06): after `brew install terminal-notifier`, it
     exits 0 but posts nothing, shows no permission prompt, and doesn't
     appear in System Settings -> Notifications until the machine is
     RESTARTED. After a reboot it registered, could be enabled, and worked.
     So if it seems broken right after install: reboot before concluding
     anything. It's a real .app bundle with its own identity, args are
     plain argv (no shell/script parsing), so arbitrary title/message text
     just works with no escaping.

  2. A locally-compiled AppleScript applet
     (~/Applications/AutonomousTraderNotifier.app, built by
     `scripts/setup_notifier_applet.sh` via osacompile), as a fallback for
     machines where terminal-notifier isn't installed or genuinely doesn't
     work. Applets can't take argv, so the title/message/subtitle payload
     is passed through a small file (~/Library/Application Support/
     autonomous-trader/notification.txt, one field per line) that the
     applet reads when launched via `open -g -W` (background,
     wait-for-exit). Built as the intended replacement while
     terminal-notifier appeared broken (before the restart discovery
     above); on this machine the applet did NOT visibly post after being
     built, so it's kept as a fallback rather than the preference.

  3. `osascript -e 'display notification ...'`, as the last resort. No
     extra dependency (ships with macOS), but real-world testing found it
     silently does nothing on current macOS even after a restart: exits 0,
     no error, no permission prompt, no entry in System Settings ->
     Notifications -- Apple has tightened what bare `osascript` (no proper
     app identity behind it) is allowed to post over recent releases.

If notifications aren't appearing: install terminal-notifier
(`brew install terminal-notifier`), send a test, and if nothing shows up
REBOOT and test again -- see scripts/launchd/README.md's notification
section.

No-ops (with a printed note) on any non-macOS platform, so callers never
need to guard calls to this module by platform themselves.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
from pathlib import Path

# Built once by scripts/setup_notifier_applet.sh -- see backend 1 in the
# module docstring. Module-level constants (rather than inline expressions)
# so tests can patch them to temp paths.
APPLET_PATH = Path.home() / "Applications" / "AutonomousTraderNotifier.app"
APPLET_PAYLOAD_PATH = (
    Path.home() / "Library" / "Application Support" / "autonomous-trader" / "notification.txt"
)

# Homebrew's two install locations, checked directly when terminal-notifier
# isn't on PATH. Real failure this fixes (2026-07-07, the first successful
# scheduled cycle): launchd's minimal environment (PATH=/usr/bin:/bin:...)
# doesn't include Homebrew's bin dir, so shutil.which() came up empty on the
# unattended run and both notifications (approval-needed + cycle summary)
# silently fell through to the non-working fallbacks -- even though the
# identical manual terminal test worked fine. Same class of bug as the
# hardcoded interpreter path in scripts/launchd/run_cycle.sh, and solved
# the same way. Module-level so tests can pin it to ().
TERMINAL_NOTIFIER_FALLBACK_PATHS = (
    "/opt/homebrew/bin/terminal-notifier",  # Apple Silicon Homebrew
    "/usr/local/bin/terminal-notifier",  # Intel Homebrew
)


def _find_terminal_notifier() -> str | None:
    found = shutil.which("terminal-notifier")
    if found:
        return found
    for candidate in TERMINAL_NOTIFIER_FALLBACK_PATHS:
        if Path(candidate).exists():
            return candidate
    return None


def _collapse_whitespace(text: str) -> str:
    """Newlines/runs of whitespace -> single spaces. The applet payload
    file is one field per line, so a literal newline inside a field would
    shift every following field; macOS banners don't render internal
    newlines meaningfully anyway.
    """
    return " ".join(text.split())


def _escape_applescript_string(text: str) -> str:
    """Escape a string for safe embedding inside an AppleScript
    double-quoted string literal. Only used by the osascript fallback path
    below -- terminal-notifier takes its arguments as plain argv elements
    (no shell/script parsing involved), so it needs none of this.

    Backslashes and double quotes both have to be escaped or they'd either
    break out of the string (a literal `"` ends it early, corrupting the
    script) or be misinterpreted -- and this text is never author-controlled
    here (it's built from live symbols/reasoning strings, e.g. a portfolio
    manager reasoning string that itself happens to quote a headline), so it
    can't be assumed "safe" the way a hardcoded literal would be. Newlines
    are collapsed to single spaces since an AppleScript string literal can't
    contain a literal line break -- passing one through unescaped would
    break the `-e` script, not just look odd in the notification.
    """
    text = text.replace("\\", "\\\\").replace('"', '\\"')
    text = " ".join(text.split())
    return text


def _send_via_applet(title: str, message: str, subtitle: str | None) -> None:
    """Backend 1: write the payload file (line 1 title, line 2 message,
    line 3 subtitle or empty), then launch the applet. `open -g` keeps it
    in the background (no focus steal from an unattended launchd run);
    `-W` waits for the applet to exit, which both surfaces a nonzero exit
    and guarantees the payload file isn't overwritten by a subsequent send
    before this one has been read.
    """
    payload = "\n".join(
        [
            _collapse_whitespace(title),
            _collapse_whitespace(message),
            _collapse_whitespace(subtitle) if subtitle else "",
        ]
    )
    APPLET_PAYLOAD_PATH.parent.mkdir(parents=True, exist_ok=True)
    APPLET_PAYLOAD_PATH.write_text(payload, encoding="utf-8")
    subprocess.run(
        ["open", "-g", "-W", str(APPLET_PATH)], check=True, capture_output=True, timeout=30
    )


def _send_via_terminal_notifier(
    terminal_notifier_path: str, title: str, message: str, subtitle: str | None
) -> None:
    args = [terminal_notifier_path, "-title", title, "-message", message]
    if subtitle:
        args += ["-subtitle", subtitle]
    subprocess.run(args, check=True, capture_output=True, timeout=10)


def _send_via_osascript(title: str, message: str, subtitle: str | None) -> None:
    safe_title = _escape_applescript_string(title)
    safe_message = _escape_applescript_string(message)

    script = f'display notification "{safe_message}" with title "{safe_title}"'
    if subtitle:
        safe_subtitle = _escape_applescript_string(subtitle)
        script += f' subtitle "{safe_subtitle}"'

    subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=10)


def send_macos_notification(
    title: str,
    message: str,
    subtitle: str | None = None,
    max_message_length: int = 500,
) -> None:
    """Fire a native macOS notification via terminal-notifier if it's
    installed, else the compiled applet if it's been built, else osascript
    (see module docstring for the ordering rationale and each backend's
    real-world status). Best-effort: any failure (not on macOS, no backend
    available/working, notification permission not granted, etc.) is
    caught, printed to stdout (so it still shows up in run_cycle.log for
    an unattended launchd run), and swallowed -- this must never raise,
    since a notification glitch can't be allowed to take down or alter the
    outcome of an actual trading cycle.

    message is truncated defensively (macOS notifications themselves also
    truncate long text, but this keeps what's handed to any backend
    bounded rather than relying on that).
    """
    if platform.system() != "Darwin":
        print(f"[notify] skipping macOS notification (not on macOS): {title} -- {message}")
        return

    message = message[:max_message_length]
    terminal_notifier_path = _find_terminal_notifier()

    try:
        if terminal_notifier_path:
            _send_via_terminal_notifier(terminal_notifier_path, title, message, subtitle)
        elif APPLET_PATH.exists():
            _send_via_applet(title, message, subtitle)
        else:
            _send_via_osascript(title, message, subtitle)
    except Exception as exc:  # noqa: BLE001 -- best-effort, see docstrings above
        print(f"[notify] failed to send macOS notification ({title!r}): {exc}")
