"""Interactive, vim-style browser for `tiket tasks`."""

import webbrowser
from collections.abc import Callable
from dataclasses import dataclass

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, OptionList
from textual.widgets.option_list import Option

from .backends import MissingFieldsError, Status
from .clipboard import copy_to_clipboard


@dataclass
class TaskRow:
    key: str
    title: str
    project: str
    issue_type: str
    status: str
    updated: str
    url: str
    done: bool
    site: str = ""

    def matches(self, needle: str) -> bool:
        haystack = f"{self.key} {self.title} {self.project} {self.issue_type} {self.status}"
        return needle.lower() in haystack.lower()


COLUMNS = ("key", "title", "project", "type", "status", "updated")
MIN_TITLE = 20


class TaskTable(DataTable):
    # Bound here rather than on the App so typing in the filter box isn't hijacked
    BINDINGS = [
        Binding("j", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("g", "top", "top", show=False),
        Binding("G", "bottom", "bottom", show=False),
        Binding("ctrl+d", "half_page(1)", "½ page down", show=False),
        Binding("ctrl+u", "half_page(-1)", "½ page up", show=False),
        Binding("y", "yank", "yank key+title"),
        Binding("Y", "yank_url", "yank url"),
        Binding("c", "yank_context", "copy full ticket"),
        Binding("s", "status", "status"),
        Binding("o", "open", "open"),
        Binding("r", "refresh", "refresh"),
        Binding("slash", "filter", "filter"),
        Binding("q", "app.quit", "quit"),
        Binding("escape", "clear_filter", "clear filter", show=False),
    ]

    def action_top(self) -> None:
        self.move_cursor(row=0)

    def action_bottom(self) -> None:
        self.move_cursor(row=self.row_count - 1)

    def action_half_page(self, direction: int) -> None:
        step = max(1, self.size.height // 2)
        self.move_cursor(row=max(0, min(self.row_count - 1, self.cursor_row + direction * step)))

    def action_yank(self) -> None:
        if row := self.app.current():
            self.app.yank(f"{row.key} {row.title}")

    def action_yank_url(self) -> None:
        if row := self.app.current():
            self.app.yank(row.url)

    def action_yank_context(self) -> None:
        if row := self.app.current():
            self.app.yank_context(row)

    def action_status(self) -> None:
        if row := self.app.current():
            self.app.pick_status(row)

    def action_refresh(self) -> None:
        self.app.refresh_rows()

    def action_open(self) -> None:
        if row := self.app.current():
            webbrowser.open(row.url)

    def action_filter(self) -> None:
        self.app.show_filter()

    def action_clear_filter(self) -> None:
        self.app.set_filter("")


class FilterInput(Input):
    BINDINGS = [Binding("escape", "close", "close", show=False)]

    def action_close(self) -> None:
        self.app.hide_filter(clear=True)


class StatusPicker(ModalScreen[Status | None]):
    """The statuses the ticket can move to; enter picks one, esc leaves it as it is."""

    BINDINGS = [
        Binding("j", "move(1)", "down", show=False),
        Binding("k", "move(-1)", "up", show=False),
        Binding("escape", "dismiss(None)", "cancel"),
        Binding("q", "dismiss(None)", "cancel", show=False),
    ]
    DEFAULT_CSS = """
    StatusPicker { align: center middle; }
    StatusPicker OptionList { width: auto; min-width: 36; max-width: 80%; height: auto; max-height: 80%; padding: 0 1; }
    """

    def __init__(self, row: TaskRow, statuses: list[Status]):
        super().__init__()
        self.row = row
        self.statuses = statuses

    def compose(self) -> ComposeResult:
        options = OptionList(*(Option(s.label) for s in self.statuses))
        options.border_title = f"{self.row.key}: {self.row.status} →"
        options.border_subtitle = "enter move · esc cancel"
        yield options

    def action_move(self, step: int) -> None:
        options = self.query_one(OptionList)
        options.action_cursor_down() if step > 0 else options.action_cursor_up()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self.statuses[event.option_index])


class TaskBrowser(App):
    TITLE = "tiket tasks"
    CSS = """
    TaskTable { height: 1fr; }
    FilterInput { dock: bottom; display: none; margin-bottom: 1; }
    FilterInput.visible { display: block; }
    """

    def __init__(
        self,
        rows: list[TaskRow],
        note: str = "",
        fetch: Callable[[TaskRow], tuple[str, str]] | None = None,
        statuses: Callable[[TaskRow], list[Status]] | None = None,
        move: Callable[[TaskRow, Status], None] | None = None,
        reload: Callable[[], tuple[list[TaskRow], str]] | None = None,
    ):
        super().__init__()
        self.fetch = fetch  # row -> (markdown, short summary) for the "copy full ticket" key
        self.statuses = statuses  # row -> where the ticket can move to, for the "status" key
        self.move = move  # (row, status) -> moves the ticket
        self.reload = reload  # () -> (rows, note), fetched again for the "refresh" key
        self.all_rows = rows
        self.shown: list[TaskRow] = rows
        self.filter_text = ""
        self.note = note

    def compose(self) -> ComposeResult:
        yield Header()
        yield TaskTable(cursor_type="row", zebra_stripes=True)
        yield FilterInput(placeholder="filter: key, title, project, type or status (enter = keep, esc = clear)")
        yield Footer()

    def on_mount(self) -> None:
        self.table = self.query_one(TaskTable)
        self.filter_input = self.query_one(FilterInput)
        self.rebuild()
        self.table.focus()

    def _title_width(self, fixed: dict[str, int]) -> int:
        # DataTable pads each cell by 1 on both sides, plus room for the scrollbar
        return self.size.width - sum(fixed.values()) - 2 * (len(fixed) + 1) - 2

    def on_resize(self) -> None:
        self.rebuild()

    # --- data -------------------------------------------------------------

    def current(self) -> TaskRow | None:
        if not self.shown:
            return None
        return self.shown[self.table.cursor_row]

    def rebuild(self) -> None:
        if not hasattr(self, "table"):
            return
        keep = self.current().url if self.shown else None
        self.shown = [r for r in self.all_rows if r.matches(self.filter_text)]

        def width(values: list[str], name: str, cap: int) -> int:
            return min(cap, max([len(v) for v in values] + [len(name)]))

        fixed = {
            "key": width([r.key for r in self.shown], "key", 14),
            "project": width([r.project for r in self.shown], "project", 12),
            "type": width([r.issue_type for r in self.shown], "type", 10),
            "status": width([r.status for r in self.shown], "status", 14),
            "updated": 10,
        }
        # Narrow terminal: drop the least useful columns before squeezing the title
        for droppable in ("updated", "type"):
            if self._title_width(fixed) >= MIN_TITLE:
                break
            del fixed[droppable]
        title_width = max(MIN_TITLE, self._title_width(fixed))

        cells = {
            "key": lambda r: Text(r.key, style="bold"),
            "title": lambda r: _clip(r.title, title_width),
            "project": lambda r: _clip(r.project, fixed["project"]),
            "type": lambda r: _clip(r.issue_type, fixed["type"]),
            "status": lambda r: Text(_clip(r.status, fixed["status"]), style="dim" if r.done else ""),
            "updated": lambda r: Text(r.updated.rjust(10), style="dim"),
        }
        names = [n for n in COLUMNS if n == "title" or n in fixed]
        self.table.clear(columns=True)
        for name in names:
            self.table.add_column(name, width=fixed.get(name, title_width), key=name)
        for r in self.shown:
            self.table.add_row(*(cells[n](r) for n in names), key=r.url)
        if keep and any(r.url == keep for r in self.shown):
            self.table.move_cursor(row=next(i for i, r in enumerate(self.shown) if r.url == keep))

        shown = f"{len(self.shown)}/{len(self.all_rows)}" if self.filter_text else str(len(self.all_rows))
        filt = f" · filter: {self.filter_text!r}" if self.filter_text else ""
        self.sub_title = f"{shown} tickets{filt}{self.note}"

    # --- actions used by the widgets --------------------------------------

    def yank(self, text: str, message: str | None = None) -> None:
        if not copy_to_clipboard(text):
            self.copy_to_clipboard(text)  # OSC 52 fallback, works in most modern terminals
        self.notify(message or text, title="Copied", timeout=2)

    @work(thread=True, exclusive=True)
    def yank_context(self, row: TaskRow) -> None:
        if not self.fetch:
            return
        self.call_from_thread(self.notify, f"Fetching {row.key}…", timeout=2)
        try:
            text, summary = self.fetch(row)
        except Exception as e:
            self.call_from_thread(self.notify, str(e), title=row.key, severity="error")
            return
        self.call_from_thread(self.yank, text, summary)

    @work(thread=True, exclusive=True, group="refresh")
    def refresh_rows(self) -> None:
        if not self.reload:
            return
        self.call_from_thread(self.notify, "Refreshing…", timeout=2)
        try:
            rows, note = self.reload()
        except Exception as e:
            self.call_from_thread(self.notify, str(e), title="Refresh failed", severity="error", timeout=8)
            return
        self.call_from_thread(self.refreshed, rows, note)

    def refreshed(self, rows: list[TaskRow], note: str) -> None:
        self.all_rows, self.note = rows, note
        self.rebuild()  # keeps the filter, and the cursor on the same ticket if it is still there
        self.notify(f"{len(rows)} tickets", title="Refreshed", timeout=2)

    @work(thread=True, exclusive=True, group="status")
    def pick_status(self, row: TaskRow) -> None:
        if not (self.statuses and self.move):
            return
        self.call_from_thread(self.notify, f"Reading the workflow of {row.key}…", timeout=2)
        try:
            options = self.statuses(row)
        except Exception as e:
            self.call_from_thread(self.notify, str(e), title=row.key, severity="error")
            return
        if not options:
            self.call_from_thread(
                self.notify, f"{row.status}: can't be moved anywhere from here", title=row.key, severity="warning"
            )
            return

        def picked(status: Status | None) -> None:
            if status:
                self.move_to(row, status)

        self.call_from_thread(self.push_screen, StatusPicker(row, options), picked)

    @work(thread=True, group="status")
    def move_to(self, row: TaskRow, status: Status) -> None:
        try:
            self.move(row, status)
        except MissingFieldsError as e:
            needs = ", ".join(label for _, label in e.fields)
            self.call_from_thread(
                self.notify,
                f"{status.name} asks for {needs}. Press o to do it in the browser.",
                title=row.key,
                severity="error",
                timeout=8,
            )
            return
        except Exception as e:
            self.call_from_thread(self.notify, str(e), title=row.key, severity="error", timeout=8)
            return
        self.call_from_thread(self.moved, row, status)

    def moved(self, row: TaskRow, status: Status) -> None:
        was, row.status, row.done, row.updated = row.status, status.name, status.done, "today"
        self.rebuild()
        self.notify(f"{was} → {status.name}", title=row.key, timeout=3)

    def set_filter(self, text: str) -> None:
        self.filter_text = text
        self.rebuild()

    def show_filter(self) -> None:
        self.filter_input.add_class("visible")
        self.filter_input.value = self.filter_text
        self.filter_input.focus()

    def hide_filter(self, clear: bool = False) -> None:
        if clear:
            self.set_filter("")
        self.filter_input.remove_class("visible")
        self.table.focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        self.set_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.hide_filter()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        webbrowser.open(event.row_key.value)


def _clip(s: str, width: int) -> str:
    return s if len(s) <= width else s[: width - 1] + "…"
