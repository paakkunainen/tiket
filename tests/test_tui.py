"""The interactive `tiket tasks` view, driven with Textual's pilot."""

import asyncio

import pytest

from tiket import clipboard, tui
from tiket.backends import MissingFieldsError, Status
from tiket.tui import TaskBrowser, TaskRow


def rows(n=3):
    return [
        TaskRow(f"BI-{i}", f"Title {i}", "bf" if i % 2 else "esko", "Task", "Open", "today", f"https://t/BI-{i}", False, "cloud")
        for i in range(1, n + 1)
    ]


@pytest.fixture
def clip(monkeypatch):
    copied = []
    monkeypatch.setattr(tui, "copy_to_clipboard", lambda text: copied.append(text) or True)
    return copied


@pytest.fixture
def opened(monkeypatch):
    urls = []
    monkeypatch.setattr(tui.webbrowser, "open", urls.append)
    return urls


def drive(app: TaskBrowser, *keys: str, size=(120, 30)):
    """Press keys and return the app for inspection."""

    async def go():
        async with app.run_test(size=size) as pilot:
            for key in keys:
                if key == "wait":  # let a background fetch finish before the next key
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                else:
                    await pilot.press(key)
            await app.workers.wait_for_complete()
            await pilot.pause()
            app.final_columns = [str(c.label) for c in app.table.columns.values()]
            app.final_row = app.current()
            app.final_subtitle = app.sub_title

    asyncio.run(go())
    return app


def test_yank_copies_key_and_title(clip):
    drive(TaskBrowser(rows()), "y")
    assert clip == ["BI-1 Title 1"]


def test_vim_movement(clip):
    app = drive(TaskBrowser(rows(5)), "j", "j", "k", "y", "G", "y", "g", "y")
    assert clip == ["BI-2 Title 2", "BI-5 Title 5", "BI-1 Title 1"]
    assert app.final_row.key == "BI-1"


def test_half_page_moves_and_stops_at_ends(clip):
    app = drive(TaskBrowser(rows(40)), "ctrl+d")
    assert app.final_row.key != "BI-1"
    app = drive(TaskBrowser(rows(40)), "ctrl+d", "ctrl+d", "ctrl+d", "ctrl+d", "ctrl+d")
    assert app.final_row.key == "BI-40"
    app = drive(TaskBrowser(rows(40)), "ctrl+u")
    assert app.final_row.key == "BI-1"


def test_yank_url_and_open(clip, opened):
    drive(TaskBrowser(rows()), "j", "Y", "o", "enter")
    assert clip == ["https://t/BI-2"]
    assert opened == ["https://t/BI-2", "https://t/BI-2"]


def test_filter_narrows_rows_and_escape_clears(clip):
    app = drive(TaskBrowser(rows(5)), "slash", "T", "i", "t", "l", "e", "space", "3", "enter", "y")
    assert clip == ["BI-3 Title 3"]
    assert [r.key for r in app.shown] == ["BI-3"]
    assert "1/5 tickets · filter: 'Title 3'" in app.final_subtitle

    app = drive(TaskBrowser(rows(5)), "slash", "e", "s", "k", "o", "enter", "escape")
    assert len(app.shown) == 5 and app.filter_text == ""


def test_filter_escape_inside_input_clears(clip):
    app = drive(TaskBrowser(rows(5)), "slash", "B", "I", "-", "2", "escape", "y")
    assert app.filter_text == "" and len(app.shown) == 5
    assert clip == ["BI-2 Title 2"]  # focus is back on the table, still on the ticket that was shown


def test_filter_keys_type_into_input_not_table(clip):
    # j / y / q typed into the filter must not move, copy or quit
    app = drive(TaskBrowser(rows(3)), "slash", "j", "y", "q")
    assert app.filter_text == "jyq" and clip == []


def test_cursor_stays_on_same_ticket_when_filtering(clip):
    app = drive(TaskBrowser(rows(5)), "G", "slash", "B", "I", "enter")
    assert app.final_row.key == "BI-5"


def test_no_rows(clip):
    app = drive(TaskBrowser([]), "y", "Y", "c")
    assert clip == [] and app.final_row is None


def test_copy_full_ticket(clip):
    fetched = []

    def fetch(row):
        fetched.append((row.site, row.key))
        return f"# {row.key}\n", f"{row.key} (2 comments)"

    drive(TaskBrowser(rows(), fetch=fetch), "j", "c")
    assert fetched == [("cloud", "BI-2")]
    assert clip == ["# BI-2\n"]


def test_copy_full_ticket_failure_copies_nothing(clip):
    def fetch(row):
        raise RuntimeError("401")

    drive(TaskBrowser(rows(), fetch=fetch), "c")
    assert clip == []


STATUSES = [Status("In Progress", "11", via="Start"), Status("Done", "31", done=True)]


def status_app(n=3, statuses=STATUSES, error=None):
    moves = []

    def move(row, status):
        if error:
            raise error
        moves.append((row.site, row.key, status.value))

    app = TaskBrowser(rows(n), statuses=lambda row: statuses, move=move)
    app.moves = moves
    return app


def test_status_key_moves_the_selected_ticket(clip):
    app = drive(status_app(), "j", "s", "wait", "j", "enter", "wait", "y")
    assert app.moves == [("cloud", "BI-2", "31")]
    row = app.all_rows[1]
    assert (row.status, row.done, row.updated) == ("Done", True, "today")
    assert app.final_row.key == "BI-2"  # the cursor stays on the ticket
    assert clip == ["BI-2 Title 2"]  # and the table has the keys again


def test_status_picker_shows_transition_names_and_vim_keys(clip):
    app = status_app()
    labels = []

    async def go():
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.press("s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            options = app.screen.query_one(tui.OptionList)
            labels.extend(str(options.get_option_at_index(i).prompt) for i in range(options.option_count))
            await pilot.press("j", "j", "k", "k", "enter")
            await app.workers.wait_for_complete()
            await pilot.pause()

    asyncio.run(go())
    assert labels == ["In Progress (Start)", "Done"]
    assert app.moves == [("cloud", "BI-1", "11")]


@pytest.mark.parametrize("cancel", ["escape", "q"])
def test_status_picker_cancel_changes_nothing(clip, cancel):
    app = drive(status_app(), "s", "wait", cancel, "y")
    assert app.moves == [] and app.all_rows[0].status == "Open"
    assert clip == ["BI-1 Title 1"]  # back on the table, not quit


@pytest.mark.parametrize(
    "error", [RuntimeError("403 no permission"), MissingFieldsError([("resolution", "Resolution")], "raw")]
)
def test_status_failure_leaves_the_row_alone(clip, error):
    app = drive(status_app(error=error), "s", "wait", "enter")
    assert app.all_rows[0].status == "Open" and app.all_rows[0].done is False


def test_status_with_nowhere_to_go_or_no_backend(clip):
    app = drive(status_app(statuses=[]), "s", "wait", "y")
    assert app.moves == [] and clip == ["BI-1 Title 1"]  # no picker opened
    drive(TaskBrowser(rows()), "s", "wait", "y")
    drive(TaskBrowser([]), "s")


def test_refresh_reloads_rows_and_keeps_cursor_and_filter(clip):
    def reload():
        fresh = rows(4)[1:]  # BI-1 is gone, BI-4 is new
        fresh[0].status = "Done"
        return fresh, " · ⚠ failed: dc"

    app = drive(TaskBrowser(rows(3), reload=reload), "slash", "T", "i", "enter", "j", "r", "wait", "y")
    assert [r.key for r in app.shown] == ["BI-2", "BI-3", "BI-4"]
    assert app.final_row.key == "BI-2" and app.final_row.status == "Done"
    assert clip == ["BI-2 Title 2"]
    assert app.final_subtitle == "3/3 tickets · filter: 'Ti' · ⚠ failed: dc"


def test_refresh_failure_or_no_loader_keeps_rows(clip):
    def reload():
        raise RuntimeError("offline")

    app = drive(TaskBrowser(rows(3), reload=reload), "r", "wait", "y")
    assert len(app.shown) == 3 and clip == ["BI-1 Title 1"]
    assert len(drive(TaskBrowser(rows(3)), "r").shown) == 3


def test_osc52_fallback_without_clipboard_tool(monkeypatch):
    monkeypatch.setattr(tui, "copy_to_clipboard", lambda text: False)
    sent = []
    monkeypatch.setattr(TaskBrowser, "copy_to_clipboard", lambda self, text: sent.append(text))
    drive(TaskBrowser(rows()), "y")
    assert sent == ["BI-1 Title 1"]


def test_quit(clip):
    app = drive(TaskBrowser(rows()), "q", "y")
    assert clip == []  # nothing after quitting
    assert app.return_code in (0, None)


@pytest.mark.parametrize(
    "width, columns",
    [
        (120, ["key", "title", "project", "type", "status", "updated"]),
        (65, ["key", "title", "project", "type", "status", "updated"]),
        (60, ["key", "title", "project", "type", "status"]),
        (50, ["key", "title", "project", "status"]),
    ],
)
def test_narrow_terminals_drop_columns(clip, width, columns):
    app = drive(TaskBrowser(rows()), size=(width, 20))
    assert app.final_columns == columns


def test_long_titles_are_clipped():
    assert tui._clip("abcdef", 4) == "abc…"
    assert tui._clip("abc", 4) == "abc"


def test_row_matching():
    row = rows(1)[0]
    assert row.matches("title 1") and row.matches("BF") and row.matches("open")
    assert not row.matches("nothing")


def test_clipboard_tools(monkeypatch):
    calls = []
    monkeypatch.setattr(clipboard.shutil, "which", lambda name: name == "xclip")
    monkeypatch.setattr(clipboard.subprocess, "run", lambda cmd, input, check: calls.append((cmd, input)))
    assert clipboard.copy_to_clipboard("hei") is True
    assert calls == [(["xclip", "-selection", "clipboard"], "hei".encode())]

    monkeypatch.setattr(clipboard.shutil, "which", lambda name: None)
    assert clipboard.copy_to_clipboard("x") is False


def test_paste_from_clipboard_tools(monkeypatch):
    calls = []

    class Done:
        stdout = "notes\nline 2".encode()

    monkeypatch.setattr(clipboard.shutil, "which", lambda name: name == "wl-paste")
    monkeypatch.setattr(clipboard.subprocess, "run", lambda cmd, capture_output, check: calls.append(cmd) or Done)
    assert clipboard.paste_from_clipboard() == "notes\nline 2"
    assert calls == [["wl-paste", "--no-newline"]]

    monkeypatch.setattr(clipboard.shutil, "which", lambda name: None)
    assert clipboard.paste_from_clipboard() is None
