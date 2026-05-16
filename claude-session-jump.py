#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Jump between live Claude Code sessions in tmux, ordered by age.

Usage: claude-session-jump.py [next|prev|newest]

  next    Cycle to the next-older session (wraps). If not on a Claude
          session, jumps to the newest.
  prev    Cycle to the next-newer session (wraps). If not on a Claude
          session, jumps to the oldest.
  newest  Always jump to the lowest-age session, regardless of current pane.

Intended for Raycast — create one script command per mode.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

SESSIONS_DIR = Path.home() / ".claude" / "sessions"

TERM_BUNDLE_IDS = {
    "ghostty": "com.mitchellh.ghostty",
    "WezTerm": "com.github.wez.wezterm",
    "Apple_Terminal": "com.apple.Terminal",
    "iTerm.app": "com.googlecode.iterm2",
    "vscode": "com.microsoft.VSCode",
}


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def pid_tty(pid: int) -> str | None:
    try:
        out = subprocess.run(
            ["ps", "-o", "tty=", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    tty = out.stdout.strip()
    if not tty or tty == "??":
        return None
    return tty


def tmux_panes() -> dict[str, str]:
    """Map pane_tty (without /dev/) -> 'session:window.pane'."""
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_tty}|#{session_name}:#{window_index}.#{pane_index}"],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return {}
    panes: dict[str, str] = {}
    for line in out.stdout.splitlines():
        tty, _, target = line.partition("|")
        if tty.startswith("/dev/"):
            tty = tty[len("/dev/"):]
        if tty and target:
            panes[tty] = target
    return panes


def current_pane_target() -> str:
    """Return 'session:window.pane' for the active client/pane, or ''."""
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p",
             "#{session_name}:#{window_index}.#{pane_index}"],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return ""
    return out.stdout.strip()


def load_session_targets() -> list[tuple[float, str]]:
    """Return [(age_seconds, tmux_target), ...] sorted by age ascending."""
    if not SESSIONS_DIR.is_dir():
        return []
    pane_map = tmux_panes()
    if not pane_map:
        return []
    results: list[tuple[float, str]] = []
    now = time.time()
    for path in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        pid = data.get("pid")
        if not isinstance(pid, int) or not pid_alive(pid):
            continue
        if data.get("kind") != "interactive":
            continue
        tty = pid_tty(pid)
        if not tty:
            continue
        target = pane_map.get(tty)
        if not target:
            continue
        updated = int(data.get("updatedAt", 0)) or int(data.get("startedAt", 0))
        age = max(0.0, now - updated / 1000.0) if updated else float("inf")
        results.append((age, target))
    results.sort(key=lambda x: x[0])
    return results


def deep_link(target: str) -> None:
    """Activate the terminal app, then select the tmux pane."""
    bundle = TERM_BUNDLE_IDS.get(os.environ.get("TERM_PROGRAM", ""))
    if bundle:
        subprocess.run(
            ["osascript", "-e", f'tell application id "{bundle}" to activate'],
            capture_output=True, timeout=2,
        )
    session, _, rest = target.partition(":")
    window, _, pane = rest.partition(".")
    subprocess.run(
        ["tmux", "switch-client", "-t", session],
        capture_output=True, timeout=2,
    )
    subprocess.run(
        ["tmux", "select-window", "-t", f"{session}:{window}"],
        capture_output=True, timeout=2,
    )
    subprocess.run(
        ["tmux", "select-pane", "-t", f"{session}:{window}.{pane}"],
        capture_output=True, timeout=2,
    )


def main() -> int:
    mode = (sys.argv[1] if len(sys.argv) > 1 else "next").lower()
    if mode not in ("next", "prev", "newest"):
        print(f"usage: {Path(sys.argv[0]).name} [next|prev|newest]", file=sys.stderr)
        return 2

    sessions = load_session_targets()
    if not sessions:
        print("no live claude sessions", file=sys.stderr)
        return 1

    targets = [t for _, t in sessions]
    current = current_pane_target()

    if mode == "newest":
        target = targets[0]
    elif current in targets:
        idx = targets.index(current)
        step = 1 if mode == "next" else -1
        target = targets[(idx + step) % len(targets)]
    else:
        target = targets[0] if mode == "next" else targets[-1]

    if target == current:
        # Already on the target — nothing to do.
        return 0

    deep_link(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
