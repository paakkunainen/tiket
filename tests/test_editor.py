"""The edit forms for a generated ticket / update, driven with Textual's pilot."""

import asyncio

from tiket.backends import Choice, Details, Draft, Profile, Update
from tiket.editor import DraftEditor, UpdateEditor

DRAFT = Draft(
    "Old title", "Old body", "Task", "",
    Details(parent=Choice("BI-890", "BI-890 AI", 3), story_points=2, labels=["ai"], sprint=Choice("5", "S5 (active)")),
)
PROFILE = Profile(parents=[Choice("BI-890", "BI-890 AI", 3), Choice("BI-12", "BI-12 Data", 1)])


def drive(app, *keys, before=None):
    async def go():
        async with app.run_test(size=(100, 40)) as pilot:
            if before:
                before(app)
            await pilot.press(*keys)
            await pilot.pause()
            app.still_running = app.is_running

    asyncio.run(go())
    return app


def set_value(selector, value):
    def apply(app):
        widget = app.query_one(selector)
        if hasattr(widget, "load_text"):
            widget.load_text(value)
        else:
            widget.value = value

    return apply


def test_save_returns_the_edited_draft():
    def edit(app):
        for selector, value in [
            ("#title", "New title"), ("#type", "Bug"), ("#description", "## Steps\n- one"),
            ("#d-parent", "none"), ("#d-labels", "ai, data"), ("#d-story-points", "3"),
        ]:
            set_value(selector, value)(app)

    app = drive(DraftEditor(DRAFT, ["Task", "Bug"], PROFILE, show_ac=False), "ctrl+s", before=edit)
    d = app.return_value
    assert (d.title, d.issue_type, d.description) == ("New title", "Bug", "## Steps\n- one")
    assert d.details.parent is None and d.details.labels == ["ai", "data"] and d.details.story_points == 3
    assert d.details.sprint == DRAFT.details.sprint  # untouched fields keep their value and id


def test_typing_into_the_title():
    app = drive(DraftEditor(DRAFT, ["Task"], PROFILE, False), "end", "!", "ctrl+s")
    assert app.return_value.title == "Old title!"  # the title has focus when the form opens


def test_parent_key_can_be_typed():
    app = drive(DraftEditor(DRAFT, ["Task"], PROFILE, False), "ctrl+s", before=set_value("#d-parent", "BI-77"))
    assert app.return_value.details.parent == Choice("BI-77", "BI-77")


def test_acceptance_criteria_field_only_when_used():
    seen = []
    app = drive(DraftEditor(DRAFT, ["Task"], PROFILE, show_ac=False), "ctrl+s",
                before=lambda app: seen.append(len(app.query("#ac"))))
    assert seen == [0] and app.return_value.acceptance_criteria == ""
    app = drive(DraftEditor(DRAFT, ["Task"], PROFILE, show_ac=True), "ctrl+s", before=set_value("#ac", "- works"))
    assert app.return_value.acceptance_criteria == "- works"


def test_unknown_current_type_is_still_selectable():
    app = drive(DraftEditor(DRAFT, ["Bug"], PROFILE, False), "ctrl+s")
    assert app.return_value.issue_type == "Task"


def test_escape_without_changes_cancels():
    app = drive(DraftEditor(DRAFT, ["Task"], PROFILE, False), "escape")
    assert app.return_value is None and not app.still_running


def test_escape_with_changes_asks_once():
    app = drive(DraftEditor(DRAFT, ["Task"], PROFILE, False), "x", "escape")
    assert app.still_running  # first escape only warns
    app = drive(DraftEditor(DRAFT, ["Task"], PROFILE, False), "x", "escape", "escape")
    assert app.return_value is None and not app.still_running


def test_update_editor():
    def edit(app):
        set_value("#text", "Edited text")(app)
        set_value("#target", "description")(app)

    app = drive(UpdateEditor(Update("comment", "Old", "why"), "BI-5"), "ctrl+s", before=edit)
    assert app.return_value == Update("description", "Edited text", "why")


def test_typing_in_a_detail_replaces_its_value():
    # title → description → parent
    app = drive(DraftEditor(DRAFT, ["Task"], PROFILE, False), "tab", "tab", *"BI-12", "ctrl+s")
    assert app.return_value.details.parent == Choice("BI-12", "BI-12")
    app = drive(DraftEditor(DRAFT, ["Task"], PROFILE, False), "tab", "tab", "end", *" x", "ctrl+s")
    assert app.return_value.details.parent == Choice("BI-890", "BI-890 AI x")
