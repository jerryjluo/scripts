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
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Static

SESSIONS_DIR = Path.home() / ".claude" / "sessions"
PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Per-session jsonl-derived info, cached by file mtime.
# sessionId -> (jsonl_mtime, title, context_tokens, recent_events)
_META_CACHE: dict[str, tuple[float, str, int, list["ActivityEvent"]]] = {}

# Default model context window in tokens. Override with CLAUDE_CONTEXT_LIMIT env.
CONTEXT_LIMIT = int(os.environ.get("CLAUDE_CONTEXT_LIMIT", "1000000"))

# Number of recent activity events to display under each session row.
DETAIL_ROWS = 3

TERM_BUNDLE_IDS = {
    "ghostty": "com.mitchellh.ghostty",
    "WezTerm": "com.github.wez.wezterm",
    "Apple_Terminal": "com.apple.Terminal",
    "iTerm.app": "com.googlecode.iterm2",
    "vscode": "com.microsoft.VSCode",
}


@dataclass
class ActivityEvent:
    ts_iso: str
    kind: str   # 'user' | 'text' | 'thinking' | 'tool'
    label: str  # display text, e.g. "Bash: npm test"


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
    ctx_tokens: int = 0
    recent_events: list[ActivityEvent] = field(default_factory=list)

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


def _truncate(s: str, n: int = 80) -> str:
    s = s.strip().splitlines()[0] if s else ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _format_tool(block: dict) -> str:
    name = str(block.get("name") or "tool")
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    preview = ""
    if name == "Bash":
        preview = _truncate(str(inp.get("command", "")), 80)
    elif name in ("Read", "Edit", "Write", "NotebookEdit"):
        fp = str(inp.get("file_path", ""))
        preview = Path(fp).name if fp else ""
    elif name in ("Grep", "Glob"):
        preview = str(inp.get("pattern", ""))
    elif name == "TodoWrite":
        todos = inp.get("todos") if isinstance(inp.get("todos"), list) else []
        in_prog = next(
            (str(t.get("content", "")) for t in todos
             if isinstance(t, dict) and t.get("status") == "in_progress"),
            "",
        )
        preview = f"{len(todos)} todos" + (f" · {_truncate(in_prog, 60)}" if in_prog else "")
    elif name == "Task":
        preview = str(inp.get("subagent_type") or inp.get("description") or "")
    elif name == "WebFetch":
        preview = str(inp.get("url", ""))
    else:
        for v in (inp or {}).values():
            if isinstance(v, str) and v:
                preview = _truncate(v, 60)
                break
    return f"{name}: {preview}" if preview else name


def _classify_event(d: dict) -> ActivityEvent | None:
    """Return a display event for user messages, assistant text, and tool calls.

    Thinking blocks are intentionally skipped: they get logged at the start of a
    streamed turn and may not be followed by the final text/tool blocks in the
    jsonl, which makes a trailing "thinking…" entry misleading. The session's
    busy/idle status already conveys "currently reasoning."
    """
    t = d.get("type")
    ts = str(d.get("timestamp", ""))
    msg = d.get("message") if isinstance(d.get("message"), dict) else None
    if t == "user" and msg is not None:
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return ActivityEvent(ts, "user", _truncate(content, 100))
        return None
    if t == "assistant" and msg is not None:
        content = msg.get("content")
        if not isinstance(content, list):
            return None
        tool = next((c for c in content if isinstance(c, dict) and c.get("type") == "tool_use"), None)
        if tool:
            return ActivityEvent(ts, "tool", _format_tool(tool))
        text = next((c for c in content if isinstance(c, dict) and c.get("type") == "text"), None)
        if text:
            txt = _truncate(str(text.get("text", "")), 100)
            if txt:
                return ActivityEvent(ts, "text", txt)
        return None
    return None


def session_meta(session_id: str) -> tuple[str, int, list[ActivityEvent]]:
    """Return (latest ai-title, ctx_tokens, last N activity events) for a session.

    ctx_tokens = input_tokens + cache_creation_input_tokens + cache_read_input_tokens
    from the most recent assistant usage block. Cached by jsonl mtime.
    """
    if not session_id:
        return "", 0, []
    matches = list(PROJECTS_DIR.glob(f"*/{session_id}.jsonl")) if PROJECTS_DIR.is_dir() else []
    if not matches:
        cached = _META_CACHE.get(session_id)
        return (cached[1], cached[2], cached[3]) if cached else ("", 0, [])
    jsonl = matches[0]
    try:
        mtime = jsonl.stat().st_mtime
    except OSError:
        cached = _META_CACHE.get(session_id)
        return (cached[1], cached[2], cached[3]) if cached else ("", 0, [])
    cached = _META_CACHE.get(session_id)
    if cached and cached[0] == mtime:
        return cached[1], cached[2], cached[3]
    title = ""
    ctx_tokens = 0
    events: deque[ActivityEvent] = deque(maxlen=DETAIL_ROWS * 4)
    last_user: ActivityEvent | None = None
    try:
        with jsonl.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                # Cheap rejections before parsing.
                if '"type"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                t = d.get("type")
                if t == "ai-title":
                    candidate = d.get("aiTitle")
                    if isinstance(candidate, str) and candidate:
                        title = candidate
                    continue
                if t in ("assistant", "user"):
                    # usage scan
                    msg = d.get("message")
                    if isinstance(msg, dict):
                        usage = msg.get("usage")
                        if isinstance(usage, dict):
                            total = (
                                int(usage.get("input_tokens", 0) or 0)
                                + int(usage.get("cache_creation_input_tokens", 0) or 0)
                                + int(usage.get("cache_read_input_tokens", 0) or 0)
                            )
                            if total > 0:
                                ctx_tokens = total
                    ev = _classify_event(d)
                    if ev:
                        events.append(ev)
                        if ev.kind == "user":
                            last_user = ev
    except OSError:
        pass
    recent = list(events)[-DETAIL_ROWS:]
    # Pin the most recent user message at the top if it's about to scroll off.
    if last_user and last_user not in recent:
        recent = [last_user] + recent[1:] if len(recent) >= DETAIL_ROWS else [last_user] + recent
    _META_CACHE[session_id] = (mtime, title, ctx_tokens, recent)
    return title, ctx_tokens, recent


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
        title, ctx_tokens, events = session_meta(session_id)
        sessions.append(Session(
            pid=pid,
            session_id=session_id,
            cwd=str(data.get("cwd", "")),
            status=str(data.get("status", "")),
            started_at_ms=int(data.get("startedAt", 0)),
            updated_at_ms=int(data.get("updatedAt", 0)),
            tmux_target=target,
            title=title,
            ctx_tokens=ctx_tokens,
            recent_events=events,
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


def format_ctx(tokens: int, limit: int = CONTEXT_LIMIT) -> str:
    if tokens <= 0:
        return "[dim]—[/]"
    if tokens >= 1000:
        label = f"{tokens / 1000:.0f}K"
    else:
        label = str(tokens)
    pct = tokens / limit if limit > 0 else 0
    if pct < 0.2:
        style = "green"
    elif pct < 0.4:
        style = "yellow"
    elif pct < 0.6:
        style = "dark_orange"
    else:
        style = "red"
    return f"[{style}]{label}[/]"


def format_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{int(seconds / 86400)}d"


EVENT_GLYPH = {
    "user": ("→", "cyan"),
    "tool": ("⚙", "magenta"),
    "text": ("✻", "white"),
}


def event_age_seconds(ts_iso: str) -> float:
    if not ts_iso:
        return 0.0
    try:
        dt = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return max(0.0, time.time() - dt.timestamp())


def format_event_cell(ev: ActivityEvent, *, is_last: bool) -> str:
    branch = "╰─" if is_last else "├─"
    glyph, color = EVENT_GLYPH.get(ev.kind, ("·", "white"))
    return f"[dim]{branch}[/] [{color}]{glyph}[/] {ev.label}"


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
    DataTable > .datatable--cursor { background: $accent 60%; text-style: bold; }
    DataTable > .datatable--cursor-row { background: $accent 30%; }
    """

    BINDINGS = [
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("enter", "open", "Open", priority=True),
        Binding("s", "cycle_sort", "Sort"),
        Binding("g", "toggle_group", "Group"),
        Binding("d", "toggle_details", "Details"),
        Binding("a", "toggle_auto", "Auto-refresh"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    sort_mode: reactive[str] = reactive("age")
    group_by_dir: reactive[bool] = reactive(False)
    show_details: reactive[bool] = reactive(True)
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
        table.cursor_foreground_priority = "renderable"
        table.add_columns("Status", "Age", "Directory", "Name", "Ctx", "Session", "Pane")
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
                table.add_row("", "", f"[bold cyan]{cwd_short}[/]", "", "", "", "")
                self.row_targets.append("")
                for s in sorted(groups[cwd_short], key=lambda x: x.age_seconds):
                    self._add_session_row(table, s, indent=True)
        else:
            for s in sessions:
                self._add_session_row(table, s, indent=False)

        self._update_header()

    def _add_session_row(self, table: DataTable, s: Session, *, indent: bool) -> None:
        cwd_cell = ("  " + (Path(s.cwd).name or s.cwd_short)) if indent else s.cwd_short
        table.add_row(
            status_cell(s.status),
            format_age(s.age_seconds),
            cwd_cell,
            s.title or "[dim]—[/]",
            format_ctx(s.ctx_tokens),
            s.session_id[:8],
            s.tmux_target,
        )
        self.row_targets.append(s.tmux_target)
        if self.show_details and s.recent_events:
            n = len(s.recent_events)
            for i, ev in enumerate(s.recent_events):
                is_last = i == n - 1
                table.add_row(
                    "",
                    f"[dim]{format_age(event_age_seconds(ev.ts_iso))}[/]",
                    "",
                    format_event_cell(ev, is_last=is_last),
                    "", "", "",
                )
                self.row_targets.append("")

    def _update_header(self) -> None:
        auto = "on" if self.auto_refresh else "off"
        group = "on" if self.group_by_dir else "off"
        details = "on" if self.show_details else "off"
        count = len(self.sessions)
        self.query_one("#header", Static).update(
            f"Claude Sessions  •  {count} live  •  "
            f"sort: [b]{self.sort_mode}[/]  group: [b]{group}[/]  "
            f"details: [b]{details}[/]  auto-refresh: [b]{auto}[/]  "
            f"[dim](j/k move · enter open · s sort · g group · d details · a auto · r refresh · q quit)[/]"
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

    def action_toggle_details(self) -> None:
        self.show_details = not self.show_details
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
