#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "textual>=0.85",
# ]
# ///
"""Browse every device the UniFi UDM knows about across all VLANs, with live online/offline status."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Input, Static


# ─── data model ───────────────────────────────────────────────────────────────
@dataclass
class Row:
    online: bool
    ip: str
    name: str
    mac: str
    vlan: str


# ─── data sourcing (UDM: ip-neigh for live presence, mongo `ace` for names) ─────
SSH_HOST = "unifi"

# One round-trip: dump the kernel neighbor table (live presence across all br* VLANs),
# the controller's known-client list (names + fixed/last IP), and the UniFi infrastructure
# (APs/switches/gateway, which live in `device`, not `user`). JSON.stringify keeps the
# mongo legacy shell output one-object-per-line so we can parse it line by line.
_USER_EVAL = (
    "db.user.find({},{mac:1,name:1,hostname:1,fixed_ip:1,use_fixedip:1,last_ip:1,_id:0})"
    ".forEach(function(d){print(JSON.stringify(d))})"
)
_DEVICE_EVAL = (
    "db.device.find({},{mac:1,name:1,ip:1,model:1,type:1,_id:0})"
    ".forEach(function(d){print(JSON.stringify(d))})"
)
REMOTE_CMD = (
    "echo ===NEIGH===; ip neigh show; "
    f'echo ===USERS===; mongo --quiet --port 27117 ace --eval "{_USER_EVAL}"; '
    f'echo ===DEVICES===; mongo --quiet --port 27117 ace --eval "{_DEVICE_EVAL}"'
)

# The UDM reports its WAN address in `device.ip`; it's really the LAN gateway.
GATEWAY_LAN_IP = "192.168.1.1"

# Third octet of 192.168.X.0/24 -> VLAN name (matches the UDM's network config).
VLAN_BY_OCTET = {
    1: "Main",
    10: "Trusted",
    20: "IoT",
    30: "Cameras",
    40: "Guest",
    50: "Isolated",
}
VLAN_ORDER = {"Main": 0, "Trusted": 1, "IoT": 2, "Cameras": 3, "Guest": 4, "Isolated": 5}

# Prefer a confirmed-present neighbor entry over a stale one when a MAC has several.
_STATE_PRIORITY = {"REACHABLE": 0, "DELAY": 1, "PROBE": 2, "PERMANENT": 3, "STALE": 4}


def _vlan_for(ip: str) -> str:
    parts = ip.split(".")
    if len(parts) == 4 and parts[0] == "192" and parts[1] == "168":
        try:
            octet = int(parts[2])
        except ValueError:
            return "?"
        return VLAN_BY_OCTET.get(octet, f"VLAN{octet}")
    return "?"


def _ip_sort_key(ip: str) -> tuple:
    try:
        return tuple(int(p) for p in ip.split("."))
    except ValueError:
        return (999, 999, 999, 999)  # missing/unknown IPs sort last


def _parse_neigh(lines: list[str]) -> dict[str, list[tuple[str, str]]]:
    """mac -> list of (ip, state) for LAN-bridge neighbors that actually resolved (have an lladdr)."""
    by_mac: dict[str, list[tuple[str, str]]] = {}
    for line in lines:
        toks = line.split()
        if "lladdr" not in toks or "dev" not in toks:
            continue
        ip = toks[0]
        dev = toks[toks.index("dev") + 1]
        if not dev.startswith("br"):  # skip WAN (eth4) and other non-LAN interfaces
            continue
        mac = toks[toks.index("lladdr") + 1].lower()
        state = toks[-1]
        by_mac.setdefault(mac, []).append((ip, state))
    return by_mac


def load_rows() -> list[Row]:
    """Pull live presence + known clients from the UDM and merge them into one device list."""
    out = subprocess.run(
        ["ssh", SSH_HOST, REMOTE_CMD],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ssh {SSH_HOST} failed: {out.stderr.strip() or 'unknown error'}")

    sections: dict[str, list[str]] = {"neigh": [], "users": [], "devices": []}
    marker = {"===NEIGH===": "neigh", "===USERS===": "users", "===DEVICES===": "devices"}
    section = None
    for line in out.stdout.splitlines():
        if line in marker:
            section = marker[line]
            continue
        if section:
            sections[section].append(line)

    neigh_by_mac = _parse_neigh(sections["neigh"])

    def best_neigh_ip(mac: str) -> str:
        entries = neigh_by_mac[mac]
        return min(entries, key=lambda e: _STATE_PRIORITY.get(e[1], 9))[0]

    def resolve(mac: str, fallback_ip: str) -> tuple[bool, str]:
        """Online + display IP: prefer the live neighbor IP, else the controller's record."""
        if mac in neigh_by_mac:
            neigh_ips = {ip for ip, _ in neigh_by_mac[mac]}
            return True, fallback_ip if fallback_ip in neigh_ips else best_neigh_ip(mac)
        return False, fallback_ip or "-"

    def parse_json_lines(lines: list[str]):
        for line in lines:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

    rows: dict[str, Row] = {}

    # Known clients from the controller DB (authoritative for client names).
    for d in parse_json_lines(sections["users"]):
        mac = str(d.get("mac", "")).lower()
        if not mac:
            continue
        name = d.get("name") or d.get("hostname") or "-"
        mongo_ip = (d.get("fixed_ip") if d.get("use_fixedip") else d.get("last_ip")) or ""
        online, ip = resolve(mac, mongo_ip)
        rows[mac] = Row(online=online, ip=ip, name=name, mac=mac, vlan=_vlan_for(ip))

    # UniFi infrastructure (APs/switches/gateway) — overrides client rows on name.
    for d in parse_json_lines(sections["devices"]):
        mac = str(d.get("mac", "")).lower()
        if not mac:
            continue
        name = d.get("name") or d.get("model") or "-"
        if d.get("type") == "udm":  # gateway: LAN-side, always reachable
            online, ip = True, GATEWAY_LAN_IP
        else:
            dev_ip = d.get("ip") or ""
            online, ip = resolve(mac, dev_ip if dev_ip.startswith("192.168") else "")
        rows[mac] = Row(online=online, ip=ip, name=name, mac=mac, vlan=_vlan_for(ip))

    # Present devices neither collection has a record of (show them anyway).
    for mac in neigh_by_mac:
        if mac in rows:
            continue
        ip = best_neigh_ip(mac)
        rows[mac] = Row(online=True, ip=ip, name="-", mac=mac, vlan=_vlan_for(ip))

    return sorted(
        rows.values(),
        key=lambda r: (not r.online, VLAN_ORDER.get(r.vlan, 9), _ip_sort_key(r.ip)),
    )


# ─── columns ──────────────────────────────────────────────────────────────────
COLUMNS: list[tuple[str, callable]] = [
    ("St", lambda r: "● up" if r.online else "○ down"),
    ("IP", lambda r: r.ip),
    ("Name", lambda r: r.name),
    ("MAC", lambda r: r.mac),
    ("VLAN", lambda r: r.vlan),
]


# ─── boilerplate (template) — only BINDINGS + action_* + header text customized ─


def yank(text: str) -> None:
    subprocess.run(["pbcopy"], input=text.encode(), check=False)


class ListTUI(App):
    CSS = """
    Screen { layout: vertical; }
    #header { height: 1; padding: 0 1; background: $boost; }
    #search { display: none; height: 3; border: tall $accent; }
    #search.visible { display: block; }
    DataTable { height: 1fr; }
    DataTable > .datatable--cursor { background: $accent 60%; text-style: bold; }
    DataTable > .datatable--cursor-row { background: $accent 30%; }
    """

    BINDINGS = [
        Binding("j,down", "cursor_down", "Down", show=False),
        Binding("k,up", "cursor_up", "Up", show=False),
        Binding("slash", "start_search", "Search"),
        Binding("escape", "clear_search", "Clear", show=False),
        Binding("q", "quit", "Quit"),
        Binding("y", "yank_row", "Yank IP"),
        Binding("r", "refresh_rows", "Refresh"),
        Binding("enter", "open_row", "Copy row", priority=True),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.all_rows: list[Row] = []
        self.visible_rows: list[Row] = []
        self.search_query: str = ""
        self.search_mode: bool = False

    def compose(self) -> ComposeResult:
        yield Static(id="header")
        yield Input(placeholder="search…", id="search")
        yield DataTable(id="table", zebra_stripes=False, cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.cursor_foreground_priority = "renderable"
        table.add_columns(*[c[0] for c in COLUMNS])
        self._reload(notify_done=False)
        # Refresh presence periodically; statuses change as devices come and go.
        self.set_interval(20.0, self._tick)
        table.focus()

    def _reload(self, notify_done: bool = True) -> None:
        try:
            self.all_rows = load_rows()
        except Exception as exc:  # surface ssh/parse failures instead of crashing
            self.all_rows = []
            self.notify(f"Load failed: {exc}", severity="error", timeout=8)
            self._render()
            return
        self._render()
        if notify_done:
            up = sum(1 for r in self.all_rows if r.online)
            self.notify(f"Refreshed · {up} up / {len(self.all_rows)} total")

    def _tick(self) -> None:
        # Preserve the cursor across refreshes by tracking the focused device's MAC.
        focused = self.current_row()
        keep_mac = focused.mac if focused else None
        self._reload(notify_done=False)
        if keep_mac is not None:
            for idx, r in enumerate(self.visible_rows):
                if r.mac == keep_mac:
                    self.query_one(DataTable).move_cursor(row=idx)
                    break

    # ─── rendering ───────────────────────────────────────────────────────────

    def _matches(self, row: Row, query: str) -> bool:
        if not query:
            return True
        q = query.lower()
        return any(q in extractor(row).lower() for _, extractor in COLUMNS)

    def _render(self) -> None:
        table = self.query_one(DataTable)
        table.clear()
        self.visible_rows = [r for r in self.all_rows if self._matches(r, self.search_query)]
        for r in self.visible_rows:
            table.add_row(*[extractor(r) for _, extractor in COLUMNS])
        self._update_header()

    def _update_header(self) -> None:
        n = len(self.visible_rows)
        total = len(self.all_rows)
        up = sum(1 for r in self.all_rows if r.online)
        filt = f"  filter: [b]{self.search_query}[/]" if self.search_query else ""
        self.query_one("#header", Static).update(
            f"[b]Network devices[/]  •  [green]{up} up[/] / {total} total  •  showing {n}{filt}  "
            f"[dim](j/k move · / search · y yank IP · enter copy row · r refresh · q quit)[/]"
        )

    def current_row(self) -> Row | None:
        table = self.query_one(DataTable)
        if 0 <= table.cursor_row < len(self.visible_rows):
            return self.visible_rows[table.cursor_row]
        return None

    # ─── navigation (wraps) ──────────────────────────────────────────────────

    def action_cursor_down(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return
        table.move_cursor(row=(table.cursor_row + 1) % table.row_count)

    def action_cursor_up(self) -> None:
        table = self.query_one(DataTable)
        if table.row_count == 0:
            return
        table.move_cursor(row=(table.cursor_row - 1) % table.row_count)

    # ─── search mode ─────────────────────────────────────────────────────────

    def action_start_search(self) -> None:
        self.search_mode = True
        inp = self.query_one("#search", Input)
        inp.add_class("visible")
        inp.value = self.search_query
        inp.focus()

    def action_clear_search(self) -> None:
        self.search_query = ""
        self.search_mode = False
        inp = self.query_one("#search", Input)
        inp.value = ""
        inp.remove_class("visible")
        self.query_one(DataTable).focus()
        self._render()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self.search_query = event.value
            self._render()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            self.search_mode = False
            event.input.remove_class("visible")
            self.query_one(DataTable).focus()

    # ─── row actions ─────────────────────────────────────────────────────────

    def action_yank_row(self) -> None:
        row = self.current_row()
        if row is None:
            return
        yank(row.ip)
        self.notify(f"Copied IP: {row.ip}")

    def action_refresh_rows(self) -> None:
        self._reload()

    def action_open_row(self) -> None:
        # priority=True binding: in search mode Enter just exits search, keeping the filter.
        if self.search_mode:
            self.search_mode = False
            inp = self.query_one("#search", Input)
            inp.remove_class("visible")
            self.query_one(DataTable).focus()
            return
        row = self.current_row()
        if row is None:
            return
        detail = f"{row.ip}\t{row.name}\t{row.mac}\t{row.vlan}"
        yank(detail)
        self.notify(f"Copied row: {row.name} ({row.ip})")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if not self.search_mode:
            self.action_open_row()


def main() -> None:
    ListTUI().run()


if __name__ == "__main__":
    main()
