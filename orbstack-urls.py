#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "textual>=0.85",
# ]
# ///
"""Browse OrbStack container URLs; Enter opens in Chrome, y yanks to clipboard."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Input, Static


@dataclass
class Row:
    name: str
    url: str


COLUMNS: list[tuple[str, callable]] = [
    ("Container", lambda r: r.name),
    ("URL", lambda r: r.url),
]


def load_rows() -> list[Row]:
    """Return the list of OrbStack URLs from running docker containers."""
    try:
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    rows: list[Row] = []
    for name in result.stdout.splitlines():
        name = name.strip()
        if not name:
            continue
        rows.append(Row(name=name, url=f"https://{name}.orb.local"))
    return rows


def yank(text: str) -> None:
    subprocess.run(["pbcopy"], input=text.encode(), check=False)


def open_in_chrome(url: str) -> None:
    subprocess.run(["open", "-a", "Google Chrome", url], check=False)


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
        Binding("y", "yank_row", "Yank"),
        Binding("enter", "open_row", "Open", priority=True),
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
        self.all_rows = load_rows()
        self._render()
        table.focus()

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
        filt = f"  filter: [b]{self.search_query}[/]" if self.search_query else ""
        self.query_one("#header", Static).update(
            f"[b]OrbStack URLs[/]  •  {n}/{total} rows{filt}  "
            f"[dim](j/k move · / search · y yank · enter open in Chrome · q quit)[/]"
        )

    def current_row(self) -> Row | None:
        table = self.query_one(DataTable)
        if 0 <= table.cursor_row < len(self.visible_rows):
            return self.visible_rows[table.cursor_row]
        return None

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

    def action_yank_row(self) -> None:
        row = self.current_row()
        if row is None:
            return
        yank(row.url)
        self.notify(f"Copied: {row.url}")

    def action_open_row(self) -> None:
        if self.search_mode:
            self.search_mode = False
            inp = self.query_one("#search", Input)
            inp.remove_class("visible")
            self.query_one(DataTable).focus()
            return
        row = self.current_row()
        if row is None:
            return
        open_in_chrome(row.url)
        self.notify(f"Opened: {row.url}")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if not self.search_mode:
            self.action_open_row()


def main() -> None:
    ListTUI().run()


if __name__ == "__main__":
    main()
