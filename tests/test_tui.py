"""The interactive `tiket tasks` view, driven with Textual's pilot."""

import asyncio

import pytest

from tiket import clipboard, tui
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
            await pilot.press(*keys)
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
