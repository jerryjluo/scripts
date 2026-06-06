#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "textual>=0.85",
# ]
# ///
"""TUI for browsing live Claude Code and Codex sessions.

Reads ~/.claude/sessions/*.json and shows two kinds of live Claude session:
interactive sessions attached to a tmux pane, and background jobs (kind ==
"bg", hosted detached by the `claude daemon`). Interactive rows that don't map
to a current tmux pane are dropped; background rows are always kept, badged
"BG".

A background job's own process has no controlling tty, but it can be *attached*
— viewed in a `claude agents` / `claude attach` pane, whose tmux title is set
to the job's name. We match that title to recover a jump target, so:

  - interactive + attached bg jobs: Enter deep-links to the pane (osascript
    activate + tmux select-window/pane, mirroring focus-terminal.sh).
  - detached bg jobs: no pane, so Enter instead yanks a `claude attach
    <short-id>` command.

y always yanks the full session id; d shows recent activity.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.reactive import reactive
from textual.widgets import DataTable, Footer, Static

CLAUDE_SESSIONS_DIR = Path.home() / ".claude" / "sessions"
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"
CODEX_HOME = Path.home() / ".codex"
CODEX_SESSIONS_DIR = CODEX_HOME / "sessions"

# Per-session jsonl-derived info, cached by file mtime.
# sessionId -> (jsonl_mtime, title, context_tokens, recent_events)
_META_CACHE: dict[str, tuple[float, str, int, list["ActivityEvent"]]] = {}

# Per-Codex rollout info, cached by file mtime/size.
# rollout_path -> (mtime, size, status, ctx_tokens, ctx_limit, recent_events)
_CODEX_META_CACHE: dict[str, tuple[float, int, str, int, int, list["ActivityEvent"]]] = {}

# Default model context window in tokens. Override with CLAUDE_CONTEXT_LIMIT env.
CONTEXT_LIMIT = int(os.environ.get("CLAUDE_CONTEXT_LIMIT", "1000000"))

# Number of recent activity events to display under each session row.
DETAIL_ROWS = 3

# Keep long titles from pushing Ctx/Session off-screen. Override when needed.
NAME_WIDTH = int(os.environ.get("SESSION_TUI_NAME_WIDTH", "56"))

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
    provider: str  # "claude" | "codex"
    pid: int
    session_id: str
    cwd: str
    status: str  # "busy" | "idle" | "" (unknown)
    started_at_ms: int
    updated_at_ms: int
    tmux_target: str  # e.g. "main:2.0"
    title: str = ""
    ctx_tokens: int = 0
    ctx_limit: int = CONTEXT_LIMIT
    recent_events: list[ActivityEvent] = field(default_factory=list)
    bridge_session_id: str = ""  # set when connected to remote control (the bridge)
    name: str = ""               # bg-job launch name (only set for background jobs)
    is_background: bool = False   # kind == "bg": detached, no tmux pane
    attach_command: str = ""      # copied on Enter when there is no pane target

    @property
    def row_key(self) -> str:
        return f"{self.provider}:{self.session_id}"

    @property
    def is_remote(self) -> bool:
        return bool(self.bridge_session_id)

    @property
    def is_attached_bg(self) -> bool:
        # A background job whose name matched a pane title: viewable/jumpable.
        return self.is_background and bool(self.tmux_target)

    @property
    def display_title(self) -> str:
        # Background jobs carry an explicit launch name; prefer it, then fall
        # back to the jsonl-derived ai-title.
        if self.is_background:
            return self.name or self.title
        return self.title

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


def _strip_status_glyph(title: str) -> str:
    """Drop a leading status glyph from a pane title: '⠂ Foo' -> 'Foo'.

    Claude sets pane titles to '<glyph> <session name>', where the glyph is a
    single non-alphanumeric spinner/status character followed by a space.
    """
    title = title.strip()
    head, sep, rest = title.partition(" ")
    if sep and head and not head[0].isalnum() and len(head) <= 2:
        return rest.strip()
    return title


def tmux_panes() -> tuple[dict[str, str], dict[str, str]]:
    """Return (tty_map, title_map), both keyed to 'session:window.pane'.

    tty_map:   pane_tty (without /dev/) -> target. Maps interactive sessions to
               their pane via the session process's controlling tty.
    title_map: pane title (raw and glyph-stripped) -> target. Lets us locate a
               detached background job that's being viewed in a `claude agents`
               / `claude attach` pane: that pane's title is the job's name, so
               we match a bg job's name against it.
    """
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F",
             "#{pane_tty}|#{session_name}:#{window_index}.#{pane_index}|#{pane_title}"],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return {}, {}
    tty_map: dict[str, str] = {}
    title_map: dict[str, str] = {}
    for line in out.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 2:
            continue
        tty, target = parts[0], parts[1]
        title = parts[2] if len(parts) > 2 else ""
        if tty.startswith("/dev/"):
            tty = tty[len("/dev/"):]
        if tty and target:
            tty_map[tty] = target
        if title and target:
            # First pane wins on a title collision (setdefault).
            for key in (title.strip(), _strip_status_glyph(title)):
                if key:
                    title_map.setdefault(key, target)
    return tty_map, title_map


def _truncate(s: str, n: int = 80) -> str:
    lines = s.strip().splitlines() if s else []
    s = lines[0] if lines else ""
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
    matches = (
        list(CLAUDE_PROJECTS_DIR.glob(f"*/{session_id}.jsonl"))
        if CLAUDE_PROJECTS_DIR.is_dir()
        else []
    )
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


def load_claude_sessions(tty_map: dict[str, str], title_map: dict[str, str]) -> list[Session]:
    if not CLAUDE_SESSIONS_DIR.is_dir():
        return []
    sessions: list[Session] = []
    for path in CLAUDE_SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        pid = data.get("pid")
        if not isinstance(pid, int) or not pid_alive(pid):
            continue
        kind = data.get("kind")
        if kind not in ("interactive", "bg"):
            continue
        name = str(data.get("name", ""))
        # Interactive sessions must resolve to a live tmux pane via their tty
        # (the thing Enter deep-links into); drop those that don't. Background
        # jobs are daemon-hosted with no tty, but an attached one is viewed in a
        # `claude agents` pane titled with the job name — match that for a jump
        # target. Unmatched bg jobs are detached (target stays "").
        target = ""
        if kind == "interactive":
            tty = pid_tty(pid)
            if not tty:
                continue
            target = tty_map.get(tty, "")
            if not target:
                continue
        elif name:
            target = title_map.get(name, "")
        session_id = str(data.get("sessionId", ""))
        title, ctx_tokens, events = session_meta(session_id)
        sessions.append(Session(
            provider="claude",
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
            bridge_session_id=str(data.get("bridgeSessionId", "")),
            name=name,
            is_background=(kind == "bg"),
            attach_command=f"claude attach {session_id.split('-', 1)[0]}" if kind == "bg" else "",
        ))
    return sessions


def codex_state_db() -> Path | None:
    override = os.environ.get("CODEX_STATE_DB")
    if override:
        path = Path(override).expanduser()
        return path if path.exists() else None
    if not CODEX_HOME.is_dir():
        return None
    try:
        return max(CODEX_HOME.glob("state_*.sqlite"), key=lambda p: p.stat().st_mtime)
    except (ValueError, OSError):
        return None


def codex_process_pids() -> list[int]:
    try:
        out = subprocess.run(
            ["ps", "-axo", "pid=,comm=,args="],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    pids: list[int] = []
    for line in out.stdout.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        text = " ".join(parts[1:]).lower()
        if pid != os.getpid() and "codex" in text:
            pids.append(pid)
    return pids


def codex_rollouts_open_by_pid(pid: int) -> list[Path]:
    try:
        out = subprocess.run(
            ["lsof", "-Fn", "-p", str(pid)],
            capture_output=True, text=True, timeout=2,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return []
    prefix = str(CODEX_SESSIONS_DIR)
    paths: list[Path] = []
    for line in out.stdout.splitlines():
        if not line.startswith("n"):
            continue
        name = line[1:]
        if name.startswith(prefix) and name.endswith(".jsonl"):
            paths.append(Path(name))
    return paths


def codex_rollout_targets(tty_map: dict[str, str]) -> dict[str, tuple[int, str]]:
    targets: dict[str, tuple[int, str]] = {}
    for pid in codex_process_pids():
        tty = pid_tty(pid)
        if not tty:
            continue
        target = tty_map.get(tty, "")
        if not target:
            continue
        for rollout in codex_rollouts_open_by_pid(pid):
            targets.setdefault(str(rollout), (pid, target))
    return targets


def _format_codex_tool(payload: dict) -> str:
    name = str(payload.get("name") or "tool")
    args = payload.get("arguments")
    parsed: object = None
    if isinstance(args, str):
        try:
            parsed = json.loads(args)
        except json.JSONDecodeError:
            parsed = None
    elif isinstance(args, dict):
        parsed = args

    preview = ""
    if isinstance(parsed, dict):
        if isinstance(parsed.get("cmd"), str):
            preview = _truncate(str(parsed.get("cmd")), 80)
        elif isinstance(parsed.get("command"), str):
            preview = _truncate(str(parsed.get("command")), 80)
        elif isinstance(parsed.get("path"), str):
            preview = Path(str(parsed.get("path"))).name
        elif isinstance(parsed.get("file_path"), str):
            preview = Path(str(parsed.get("file_path"))).name
        else:
            for value in parsed.values():
                if isinstance(value, str) and value:
                    preview = _truncate(value, 60)
                    break
    return f"{name}: {preview}" if preview else name


def _classify_codex_event(d: dict) -> ActivityEvent | None:
    ts = str(d.get("timestamp", ""))
    t = d.get("type")
    payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}

    if t == "event_msg":
        event_type = payload.get("type")
        if event_type == "user_message":
            message = str(payload.get("message") or "")
            return ActivityEvent(ts, "user", _truncate(message, 100)) if message.strip() else None
        if event_type == "agent_message":
            message = str(payload.get("message") or "")
            return ActivityEvent(ts, "text", _truncate(message, 100)) if message.strip() else None
        return None

    if t == "response_item":
        item_type = payload.get("type")
        if item_type == "function_call":
            return ActivityEvent(ts, "tool", _format_codex_tool(payload))
        if item_type == "message":
            content = payload.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "output_text":
                        text = _truncate(str(block.get("text") or ""), 100)
                        if text:
                            return ActivityEvent(ts, "text", text)
        return None

    return None


def codex_meta(rollout_path: Path) -> tuple[str, int, int, list[ActivityEvent]]:
    try:
        stat = rollout_path.stat()
    except OSError:
        cached = _CODEX_META_CACHE.get(str(rollout_path))
        return (cached[2], cached[3], cached[4], cached[5]) if cached else ("", 0, CONTEXT_LIMIT, [])

    cache_key = str(rollout_path)
    cached = _CODEX_META_CACHE.get(cache_key)
    if cached and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
        return cached[2], cached[3], cached[4], cached[5]

    busy = False
    ctx_tokens = 0
    ctx_limit = CONTEXT_LIMIT
    events: deque[ActivityEvent] = deque(maxlen=DETAIL_ROWS * 4)
    last_user: ActivityEvent | None = None
    try:
        with rollout_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if '"type"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if d.get("type") == "event_msg":
                    payload = d.get("payload") if isinstance(d.get("payload"), dict) else {}
                    event_type = payload.get("type")
                    if event_type == "task_started":
                        busy = True
                    elif event_type in ("task_complete", "turn_aborted"):
                        busy = False
                    elif event_type == "token_count":
                        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
                        usage = (
                            info.get("last_token_usage")
                            if isinstance(info.get("last_token_usage"), dict)
                            else {}
                        )
                        total = int(usage.get("total_tokens", 0) or 0)
                        if total > 0:
                            ctx_tokens = total
                        limit = int(info.get("model_context_window", 0) or 0)
                        if limit > 0:
                            ctx_limit = limit
                ev = _classify_codex_event(d)
                if ev:
                    events.append(ev)
                    if ev.kind == "user":
                        last_user = ev
    except OSError:
        pass

    status = "busy" if busy else "idle"
    recent = list(events)[-DETAIL_ROWS:]
    if last_user and last_user not in recent:
        recent = [last_user] + recent[1:] if len(recent) >= DETAIL_ROWS else [last_user] + recent
    _CODEX_META_CACHE[cache_key] = (stat.st_mtime, stat.st_size, status, ctx_tokens, ctx_limit, recent)
    return status, ctx_tokens, ctx_limit, recent


def load_codex_sessions(tty_map: dict[str, str]) -> list[Session]:
    db_path = codex_state_db()
    if not db_path:
        return []
    rollout_targets = codex_rollout_targets(tty_map)
    if not rollout_targets:
        return []

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            select id, rollout_path, cwd, title, preview,
                   created_at_ms, updated_at_ms
            from threads
            where archived = 0
              and id not in (select child_thread_id from thread_spawn_edges)
            order by updated_at_ms desc, id desc
            limit 500
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            conn.close()
        except UnboundLocalError:
            pass

    sessions: list[Session] = []
    for row in rows:
        rollout_path = str(row["rollout_path"] or "")
        if not rollout_path:
            continue
        live = rollout_targets.get(rollout_path)
        if not live:
            continue
        pid, target = live
        path = Path(rollout_path)
        status, ctx_tokens, ctx_limit, events = codex_meta(path)
        title = str(row["title"] or row["preview"] or "")
        sessions.append(Session(
            provider="codex",
            pid=pid,
            session_id=str(row["id"] or ""),
            cwd=str(row["cwd"] or ""),
            status=status,
            started_at_ms=int(row["created_at_ms"] or 0),
            updated_at_ms=int(row["updated_at_ms"] or 0),
            tmux_target=target,
            title=title,
            ctx_tokens=ctx_tokens,
            ctx_limit=ctx_limit,
            recent_events=events,
        ))
    return sessions


def load_sessions() -> list[Session]:
    tty_map, title_map = tmux_panes()  # empty if no tmux server
    return load_claude_sessions(tty_map, title_map) + load_codex_sessions(tty_map)


STATUS_STYLE = {
    "busy": "dark_orange",   # running
    "idle": "green",         # waiting for user
}

AGENT_STYLE = {
    "claude": "dark_orange",
    "codex": "#5dade2",
}


def agent_cell(provider: str) -> str:
    style = AGENT_STYLE.get(provider, "white")
    return f"[{style}]{provider}[/]"


def status_cell(
    status: str, *, remote: bool = False, background: bool = False, attached: bool = False
) -> str:
    label = status or "—"
    style = STATUS_STYLE.get(status, "dim")
    cell = f"[{style}]{label}[/]"
    if background:
        # Badge background jobs; ⇄ marks one attached to a pane (jumpable),
        # plain BG marks a detached one (Enter yanks `claude attach`).
        cell += " [bold cyan]BG⇄[/]" if attached else " [bold cyan]BG[/]"
    if remote:
        # Badge sessions driven by remote control (the bridge).
        cell += " [bold blue]🔗[/]"
    return cell


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
        return f"{int(seconds / 3600)}h"
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
        Binding("y", "yank", "Yank id"),
        Binding("s", "cycle_sort", "Sort"),
        Binding("g", "toggle_group", "Group"),
        Binding("d", "toggle_details", "Details"),
        Binding("a", "toggle_auto", "Auto-refresh"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    sort_mode: reactive[str] = reactive("age")
    group_by_dir: reactive[bool] = reactive(False)
    show_details: reactive[bool] = reactive(False)
    auto_refresh: reactive[bool] = reactive(True)

    def __init__(self) -> None:
        super().__init__()
        self.sessions: list[Session] = []
        self.row_targets: list[str] = []  # parallel to table rows; "" = group header
        self.row_session_ids: list[str] = []  # parallel to table rows; full session id
        self.row_session_keys: list[str] = []  # parallel to table rows; provider:id
        self.row_attach_commands: list[str] = []  # parallel to table rows
        self._timer = None

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        yield DataTable(id="sessions", zebra_stripes=False, cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_foreground_priority = "renderable"
        table.add_column("Agent", width=7)
        table.add_column("Status", width=14)
        table.add_column("Age", width=5)
        table.add_column("Tmux", width=14)
        table.add_column("Directory")
        table.add_column("Name", width=NAME_WIDTH)
        table.add_column("Ctx", width=5)
        table.add_column("Session", width=8)
        self.refresh_sessions()
        self._timer = self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        if self.auto_refresh:
            self.refresh_sessions()

    def refresh_sessions(self) -> None:
        # Preserve the cursor's session across refresh. Key off the session id
        # (stable and unique) rather than the tmux target, which is empty for
        # background rows.
        table = self.query_one(DataTable)
        prev_key = ""
        if 0 <= table.cursor_row < len(self.row_session_keys):
            prev_key = self.row_session_keys[table.cursor_row]

        self.sessions = load_sessions()
        self._render()

        # Restore cursor.
        if prev_key and prev_key in self.row_session_keys:
            table.move_cursor(row=self.row_session_keys.index(prev_key))

    def _sorted_sessions(self) -> list[Session]:
        if self.sort_mode == "age":
            return sorted(self.sessions, key=lambda s: s.age_seconds)
        return sorted(self.sessions, key=lambda s: (s.cwd_short.lower(), s.age_seconds))

    def _render(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        self.row_targets = []
        self.row_session_ids = []
        self.row_session_keys = []
        self.row_attach_commands = []

        sessions = self._sorted_sessions()

        if self.group_by_dir:
            groups: dict[str, list[Session]] = {}
            for s in sessions:
                groups.setdefault(s.cwd_short, []).append(s)
            for cwd_short in sorted(groups.keys(), key=str.lower):
                table.add_row("", "", "", "", f"[bold cyan]{cwd_short}[/]", "", "", "")
                self.row_targets.append("")
                self.row_session_ids.append("")
                self.row_session_keys.append("")
                self.row_attach_commands.append("")
                for s in sorted(groups[cwd_short], key=lambda x: x.age_seconds):
                    self._add_session_row(table, s, indent=True)
        else:
            for s in sessions:
                self._add_session_row(table, s, indent=False)

        self._update_header()

    def _add_session_row(self, table: DataTable, s: Session, *, indent: bool) -> None:
        cwd_cell = ("  " + (Path(s.cwd).name or s.cwd_short)) if indent else s.cwd_short
        # A pane to show (interactive or attached bg) renders its session name;
        # a detached background job has none.
        tmux_cell = s.tmux_target.split(":", 1)[0] if s.tmux_target else "[dim]—[/]"
        table.add_row(
            agent_cell(s.provider),
            status_cell(
                s.status, remote=s.is_remote,
                background=s.is_background, attached=s.is_attached_bg,
            ),
            format_age(s.age_seconds),
            tmux_cell,
            cwd_cell,
            _truncate(s.display_title, NAME_WIDTH) or "[dim]—[/]",
            format_ctx(s.ctx_tokens, s.ctx_limit),
            s.session_id[:8],
        )
        self.row_targets.append(s.tmux_target)
        self.row_session_ids.append(s.session_id)
        self.row_session_keys.append(s.row_key)
        self.row_attach_commands.append(s.attach_command)
        if self.show_details and s.recent_events:
            n = len(s.recent_events)
            for i, ev in enumerate(s.recent_events):
                is_last = i == n - 1
                table.add_row(
                    "",
                    "",
                    f"[dim]{format_age(event_age_seconds(ev.ts_iso))}[/]",
                    "",
                    "",
                    format_event_cell(ev, is_last=is_last),
                    "", "",
                )
                self.row_targets.append("")
                self.row_session_ids.append("")
                self.row_session_keys.append("")
                self.row_attach_commands.append("")

    def _update_header(self) -> None:
        auto = "on" if self.auto_refresh else "off"
        group = "on" if self.group_by_dir else "off"
        details = "on" if self.show_details else "off"
        count = len(self.sessions)
        claude = sum(1 for s in self.sessions if s.provider == "claude")
        codex = sum(1 for s in self.sessions if s.provider == "codex")
        bg = sum(1 for s in self.sessions if s.is_background)
        parts = [f"{count} live", f"{claude} claude", f"{codex} codex"]
        if bg:
            parts.append(f"{bg} bg")
        live_label = " (" + ", ".join(parts[1:]) + ")" if count else ""
        self.query_one("#header", Static).update(
            f"Agent Sessions  •  {parts[0]}{live_label}  •  "
            f"sort: [b]{self.sort_mode}[/]  group: [b]{group}[/]  "
            f"details: [b]{details}[/]  auto-refresh: [b]{auto}[/]  "
            f"[dim](j/k move · enter open · y yank id · s sort · g group · d details · a auto · r refresh · q quit)[/]"
        )

    # Actions ---------------------------------------------------------------

    def action_cursor_down(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return
        new_row = (table.cursor_row + 1) % table.row_count
        # Skip non-session rows (group headers, detail rows). Keyed off the
        # session id, not the tmux target, so background rows stay selectable.
        for _ in range(table.row_count):
            if self.row_session_keys[new_row] != "":
                break
            new_row = (new_row + 1) % table.row_count
        table.move_cursor(row=new_row)

    def action_cursor_up(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return
        new_row = (table.cursor_row - 1) % table.row_count
        for _ in range(table.row_count):
            if self.row_session_keys[new_row] != "":
                break
            new_row = (new_row - 1) % table.row_count
        table.move_cursor(row=new_row)

    def _copy(self, text: str) -> bool:
        try:
            subprocess.run(["pbcopy"], input=text, text=True, timeout=2)
        except (subprocess.SubprocessError, FileNotFoundError):
            self.notify("Failed to copy to clipboard", severity="error")
            return False
        return True

    def action_open(self) -> None:
        table = self.query_one(DataTable)
        if not (0 <= table.cursor_row < len(self.row_targets)):
            return
        target = self.row_targets[table.cursor_row]
        if target:
            self.exit(result=target)
            return
        # Detached Claude background jobs have no pane; copy their attach
        # command instead. Stay silent on group-header / detail rows.
        cmd = self.row_attach_commands[table.cursor_row]
        if not cmd:
            return
        if self._copy(cmd):
            self.notify(f"Copied: {cmd}")

    def action_yank(self) -> None:
        table = self.query_one(DataTable)
        if not (0 <= table.cursor_row < len(self.row_session_ids)):
            return
        session_id = self.row_session_ids[table.cursor_row]
        if not session_id:
            return
        if self._copy(session_id):
            self.notify(f"Copied {session_id}")

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
