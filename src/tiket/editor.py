"""Full-screen forms for editing a generated ticket or update before it is written."""

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.suggester import SuggestFromList
from textual.widgets import Footer, Header, Input, Label, Select, TextArea

from . import details as ticket_details
from .backends import Draft, Profile, Update

# Detail fields in the order they are shown; values use the same text format as the $EDITOR file
DETAIL_FIELDS = ["Parent", "Story points", "Priority", "Components", "Labels", "Sprint", "Area"]

CSS = """
#form { height: 1fr; padding: 1 2; }
.row { height: 1; margin-bottom: 1; }
.row Label { width: 14; color: $text-muted; }
.row Input, .row Select { width: 1fr; }
.row Input:focus, .row Select:focus { background: $primary 30%; }
.section { color: $text-muted; }
TextArea { height: auto; min-height: 8; max-height: 24; margin-bottom: 1; }
#hint { color: $text-muted; }
"""


class _Form(App):
    """Shared keys: ctrl+s saves, escape asks once before throwing changes away."""

    CSS = CSS
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+s", "save", "save", priority=True),
        # not priority: an open Select list closes on escape first
        Binding("escape", "cancel", "cancel"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._initial = None
        self._confirm_cancel = False

    def on_mount(self) -> None:
        self._initial = self.collect()

    def collect(self):
        raise NotImplementedError

    def action_save(self) -> None:
        self.exit(self.collect())

    def action_cancel(self) -> None:
        if self.collect() == self._initial or self._confirm_cancel:
            self.exit(None)
            return
        self._confirm_cancel = True
        self.notify("Unsaved changes. Esc again to discard them, ctrl+s to save.", severity="warning")


class DraftEditor(_Form):
    TITLE = "Edit ticket"
    SUB_TITLE = "ctrl+s save · esc cancel · tab next field"

    def __init__(self, draft: Draft, types: list[str], profile: Profile, show_ac: bool) -> None:
        self.draft = draft
        self.types = list(dict.fromkeys([*types, draft.issue_type]))
        self.profile = profile
        self.show_ac = show_ac or bool(draft.acceptance_criteria)
        self.detail_values = dict(
            line.split(": ", 1) if ": " in line else (line.rstrip(":"), "")
            for line in ticket_details.to_editor(draft.details).splitlines()
        )
        super().__init__()

    def _suggestions(self, field: str) -> list[str]:
        p = self.profile
        return {
            "Parent": [c.label for c in p.parents],
            "Priority": [c.label for c in p.priorities],
            "Components": [c.label for c in p.components],
            "Labels": [c.label for c in p.labels],
            "Sprint": [f"{d.sprint.value} {d.sprint.label}"] if (d := self.draft.details).sprint else [],
        }.get(field, []) + [ticket_details.NONE]

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="form"):
            with Horizontal(classes="row"):
                yield Label("Type")
                yield Select([(t, t) for t in self.types], value=self.draft.issue_type, allow_blank=False, compact=True, id="type")
            with Horizontal(classes="row"):
                yield Label("Title")
                yield Input(self.draft.title, compact=True, select_on_focus=False, id="title")
            yield Label("Description (markdown)", classes="section")
            yield TextArea(self.draft.description, id="description")
            if self.show_ac:
                yield Label("Acceptance criteria", classes="section")
                yield TextArea(self.draft.acceptance_criteria, id="ac")
            for field in DETAIL_FIELDS:
                if field not in self.detail_values:
                    continue
                with Horizontal(classes="row"):
                    yield Label(field)
                    yield Input(
                        self.detail_values[field],
                        id=_field_id(field),
                        compact=True,  # selected on focus: typing replaces the value
                        suggester=SuggestFromList(self._suggestions(field), case_sensitive=False),
                    )
            yield Label(
                "Details: typing replaces the value, → or End keeps it for editing, → also accepts a suggestion.\n"
                "`none` clears a detail; any parent key or sprint id can be typed.",
                id="hint",
            )
        yield Footer()

    def on_mount(self) -> None:
        title = self.query_one("#title", Input)
        title.focus()
        title.cursor_position = len(title.value)
        super().on_mount()

    def collect(self) -> Draft:
        meta = {f: self.query_one(f"#{_field_id(f)}", Input).value for f in DETAIL_FIELDS if f in self.detail_values}
        return Draft(
            title=self.query_one("#title", Input).value.strip(),
            description=self.query_one("#description", TextArea).text.strip(),
            issue_type=self.query_one("#type", Select).value,
            acceptance_criteria=self.query_one("#ac", TextArea).text.strip() if self.show_ac else "",
            details=ticket_details.from_editor(meta, self.draft.details),
        )


class UpdateEditor(_Form):
    TITLE = "Edit addition"
    SUB_TITLE = "ctrl+s save · esc cancel · tab next field"

    def __init__(self, update: Update, ref: str) -> None:
        self.addition = update
        self.ref = ref
        super().__init__()

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="form"):
            with Horizontal(classes="row"):
                yield Label("Add as")
                yield Select(
                    [(f"comment on {self.ref}", "comment"), (f"section at the end of {self.ref}'s description", "description")],
                    value=self.addition.target,
                    allow_blank=False,
                    compact=True,
                    id="target",
                )
            yield Label("Text (markdown)", classes="section")
            yield TextArea(self.addition.text, id="text")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#text", TextArea).focus()
        super().on_mount()

    def collect(self) -> Update:
        return Update(
            target=self.query_one("#target", Select).value,
            text=self.query_one("#text", TextArea).text.strip(),
            reason=self.addition.reason,
        )


def _field_id(field: str) -> str:
    return "d-" + field.lower().replace(" ", "-")


def edit_draft(draft: Draft, types: list[str], profile: Profile, show_ac: bool) -> Draft | None:
    """The edited draft, or None if cancelled."""
    return DraftEditor(draft, types, profile, show_ac).run()


def edit_update(update: Update, ref: str) -> Update | None:
    return UpdateEditor(update, ref).run()
