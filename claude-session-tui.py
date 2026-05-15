#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "textual>=0.85",
# ]
# ///
"""TUI for browsing live Claude Code sessions that have an attached tmux pane.

Reads ~/.claude/sessions/*.json, drops dead pids and any pid not mapped to a
tmux pane on the current tmux server, and renders a sortable/groupable list.
Enter deep-links to the pane (osascript activate + tmux select-window/pane),
mirroring ~/.claude/hooks/focus-terminal.sh.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Static

SESSIONS_DIR = Path.home() / ".claude" / "sessions"
PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Cache of session title lookups: sessionId -> (jsonl_mtime, title)
_TITLE_CACHE: dict[str, tuple[float, str]] = {}

TERM_BUNDLE_IDS = {
    "ghostty": "com.mitchellh.ghostty",
    "WezTerm": "com.github.wez.wezterm",
    "Apple_Terminal": "com.apple.Terminal",
    "iTerm.app": "com.googlecode.iterm2",
    "vscode": "com.microsoft.VSCode",
}


@dataclass
class Session:
    pid: int
    session_id: str
    cwd: str
    status: str  # "busy" | "idle" | "" (unknown)
    started_at_ms: int
    updated_at_ms: int
    tmux_target: str  # e.g. "main:2.0"
    title: str = ""

    @property
    def age_seconds(self) -> float:
        ref = self.updated_at_ms or self.started_at_ms
        return max(0.0, time.time() - ref / 1000.0)

    @property
    def cwd_short(self) -> str:
        home = str(Path.home())
        if self.cwd.startswith(home):
            return "~" + self.cwd[len(home):]
        return self.cwd


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
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


def session_title(session_id: str) -> str:
    """Return the most recent ai-title for a session, cached by jsonl mtime."""
    if not session_id:
        return ""
    matches = list(PROJECTS_DIR.glob(f"*/{session_id}.jsonl")) if PROJECTS_DIR.is_dir() else []
    if not matches:
        return _TITLE_CACHE.get(session_id, (0.0, ""))[1]
    jsonl = matches[0]
    try:
        mtime = jsonl.stat().st_mtime
    except OSError:
        return _TITLE_CACHE.get(session_id, (0.0, ""))[1]
    cached = _TITLE_CACHE.get(session_id)
    if cached and cached[0] == mtime:
        return cached[1]
    title = ""
    try:
        with jsonl.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"ai-title"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "ai-title":
                    candidate = d.get("aiTitle")
                    if isinstance(candidate, str) and candidate:
                        title = candidate
    except OSError:
        pass
    _TITLE_CACHE[session_id] = (mtime, title)
    return title


def load_sessions() -> list[Session]:
    if not SESSIONS_DIR.is_dir():
        return []
    pane_map = tmux_panes()
    if not pane_map:
        return []
    sessions: list[Session] = []
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
        session_id = str(data.get("sessionId", ""))
        sessions.append(Session(
            pid=pid,
            session_id=session_id,
            cwd=str(data.get("cwd", "")),
            status=str(data.get("status", "")),
            started_at_ms=int(data.get("startedAt", 0)),
            updated_at_ms=int(data.get("updatedAt", 0)),
            tmux_target=target,
            title=session_title(session_id),
        ))
    return sessions


STATUS_STYLE = {
    "busy": "dark_orange",   # running
    "idle": "green",         # waiting for user
}


def status_cell(status: str) -> str:
    label = status or "—"
    style = STATUS_STYLE.get(status, "dim")
    return f"[{style}]{label}[/]"


def format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{int(seconds / 86400)}d"


def deep_link(target: str) -> None:
    """Mirror ~/.claude/hooks/focus-terminal.sh — activate terminal, select pane."""
    bundle = TERM_BUNDLE_IDS.get(os.environ.get("TERM_PROGRAM", ""))
    if bundle:
        subprocess.run(
            ["osascript", "-e", f'tell application id "{bundle}" to activate'],
            capture_output=True, timeout=2,
        )
    session, _, rest = target.partition(":")
    window, _, pane = rest.partition(".")
    # switch-client first so cross-session jumps actually move the client.
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


SORT_MODES = ("age", "dir")


class SessionsApp(App):
    CSS = """
    Screen { layout: vertical; }
    #header { height: 1; padding: 0 1; background: $boost; }
    DataTable { height: 1fr; }
    DataTable > .datatable--cursor { background: $accent; color: $text; }
    """

    BINDINGS = [
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("enter", "open", "Open", priority=True),
        Binding("s", "cycle_sort", "Sort"),
        Binding("g", "toggle_group", "Group"),
        Binding("a", "toggle_auto", "Auto-refresh"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    sort_mode: reactive[str] = reactive("age")
    group_by_dir: reactive[bool] = reactive(False)
    auto_refresh: reactive[bool] = reactive(True)

    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[Session] = []
        self.row_targets: list[str] = []  # parallel to table rows; "" = group header
        self._timer = None

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        yield DataTable(id="sessions", zebra_stripes=False, cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Status", "Age", "Directory", "Session", "Pane", "Name")
        self.refresh_sessions()
        self._timer = self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        if self.auto_refresh:
            self.refresh_sessions()

    def refresh_sessions(self) -> None:
        # Preserve cursor target across refresh.
        table = self.query_one(DataTable)
        prev_target = ""
        if 0 <= table.cursor_row < len(self.row_targets):
            prev_target = self.row_targets[table.cursor_row]

        self.sessions = load_sessions()
        self._render()

        # Restore cursor.
        if prev_target and prev_target in self.row_targets:
            new_row = self.row_targets.index(prev_target)
            table.move_cursor(row=new_row)

    def _sorted_sessions(self) -> list[Session]:
        if self.sort_mode == "age":
            return sorted(self.sessions, key=lambda s: s.age_seconds)
        return sorted(self.sessions, key=lambda s: (s.cwd_short.lower(), s.age_seconds))

    def _render(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        self.row_targets = []

        sessions = self._sorted_sessions()

        if self.group_by_dir:
            groups: dict[str, list[Session]] = {}
            for s in sessions:
                groups.setdefault(s.cwd_short, []).append(s)
            for cwd_short in sorted(groups.keys(), key=str.lower):
                table.add_row("", "", f"[bold cyan]{cwd_short}[/]", "", "", "")
                self.row_targets.append("")
                for s in sorted(groups[cwd_short], key=lambda x: x.age_seconds):
                    self._add_session_row(table, s, indent=True)
        else:
            for s in sessions:
                self._add_session_row(table, s, indent=False)

        self._update_header()

    def _add_session_row(self, table: DataTable, s: Session, *, indent: bool) -> None:
        cwd_cell = ("  " if indent else "") + (
            Path(s.cwd).name or s.cwd_short if not indent else Path(s.cwd).name or s.cwd_short
        )
        # Show full short path when not grouping; basename when grouping (since header has cwd).
        if not indent:
            cwd_cell = s.cwd_short
        table.add_row(
            status_cell(s.status),
            format_age(s.age_seconds),
            cwd_cell,
            s.session_id[:8],
            s.tmux_target,
            s.title or "[dim]—[/]",
        )
        self.row_targets.append(s.tmux_target)

    def _update_header(self) -> None:
        auto = "on" if self.auto_refresh else "off"
        group = "on" if self.group_by_dir else "off"
        count = len(self.sessions)
        self.query_one("#header", Static).update(
            f"Claude Sessions  •  {count} live  •  "
            f"sort: [b]{self.sort_mode}[/]  group: [b]{group}[/]  "
            f"auto-refresh: [b]{auto}[/]  "
            f"[dim](j/k move · enter open · s sort · g group · a auto · r refresh · q quit)[/]"
        )

    # Actions ---------------------------------------------------------------

    def action_cursor_down(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return
        new_row = (table.cursor_row + 1) % table.row_count
        # Skip group-header rows.
        for _ in range(table.row_count):
            if self.row_targets[new_row] != "":
                break
            new_row = (new_row + 1) % table.row_count
        table.move_cursor(row=new_row)

    def action_cursor_up(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return
        new_row = (table.cursor_row - 1) % table.row_count
        for _ in range(table.row_count):
            if self.row_targets[new_row] != "":
                break
            new_row = (new_row - 1) % table.row_count
        table.move_cursor(row=new_row)

    def action_open(self) -> None:
        table = self.query_one(DataTable)
        if not (0 <= table.cursor_row < len(self.row_targets)):
            return
        target = self.row_targets[table.cursor_row]
        if not target:
            return
        self.exit(result=target)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # DataTable swallows Enter; route its RowSelected to our open action.
        self.action_open()

    def action_cycle_sort(self) -> None:
        idx = SORT_MODES.index(self.sort_mode)
        self.sort_mode = SORT_MODES[(idx + 1) % len(SORT_MODES)]
        self._render()

    def action_toggle_group(self) -> None:
        self.group_by_dir = not self.group_by_dir
        self._render()

    def action_toggle_auto(self) -> None:
        self.auto_refresh = not self.auto_refresh
        self._update_header()

    def action_refresh_now(self) -> None:
        self.refresh_sessions()


def main() -> None:
    app = SessionsApp()
    target = app.run()
    if isinstance(target, str) and target:
        deep_link(target)


if __name__ == "__main__":
    main()
