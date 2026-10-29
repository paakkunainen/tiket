"""The CLI commands, with fake backends and a fake model."""

import json
import sys

import pytest
from typer.testing import CliRunner

from fakes import FakeBackend, FakeModel, FakeUpdateModel
from tiket import cli
from tiket.backends import Choice, Comment, Details, Draft, Example, Item, Ticket
from tiket.config import ConfigError

runner = CliRunner()


@pytest.fixture
def backend(config_file, monkeypatch) -> FakeBackend:
    """One fake backend for every site; remembers which site/project it was made for."""
    fake = FakeBackend()
    made = []

    def make_backend(site, project=None):
        made.append((site.name, project.alias if project else None))
        fake.site, fake.project = site, project
        return fake

    monkeypatch.setattr(cli, "make_backend", make_backend)
    fake.made = made
    return fake


@pytest.fixture
def model(monkeypatch) -> FakeModel:
    fake = FakeModel()
    monkeypatch.setattr(cli.llm, "generate", fake)
    return fake


def run(*args, input=None):
    return runner.invoke(cli.app, list(args), input=input)


# --- new ----------------------------------------------------------------------

def test_new_dry_run_shows_preview_and_details(backend, model):
    model.drafts = [Draft("Lataus kaatuu", "Kuvaus", "Task", details=Details(parent=Choice("BI-890", "BI-890 AI"), sprint=Choice("5", "S5 (active)")))]
    result = run("new", "bf", "lataus", "kaatuu", "-n")
    assert result.exit_code == 0, result.output
    assert "Lataus kaatuu" in result.output
    assert "Task → cloud / BI" in result.output
    assert "BI-890 AI" in result.output and "S5 (active)" in result.output
    assert backend.created == []

    call = model.calls[0]
    assert call["user_text"] == "lataus kaatuu"
    assert call["types"] == ["Task"]  # default type + types seen in examples
    assert call["profile"] is backend.profile and call["sprint"] is backend.sprint
    assert call["ac_field"] is False
    assert backend.made == [("cloud", "bf")]


def test_new_yes_creates_and_prints_url_and_warnings(backend, model):
    result = run("new", "bf", "something", "-y")
    assert result.exit_code == 0, result.output
    assert "Created: https://tracker.test/BI-100" in result.output
    assert "⚠ Could not set x" in result.output
    (draft, extra), = backend.created
    assert draft.title == "Generated title" and extra == {}


def test_new_confirm_with_y(backend, model):
    result = run("new", "bf", "x", input="y\n")
    assert result.exit_code == 0, result.output
    assert len(backend.created) == 1


def test_new_cancel_with_n(backend, model):
    result = run("new", "bf", "x", input="n\n")
    assert result.exit_code == 1
    assert backend.created == []


def test_new_regenerate_with_feedback(backend, model):
    first, second = Draft("First", "d", "Task"), Draft("Second", "d", "Task")
    model.drafts = [first, second]
    result = run("new", "bf", "x", input="r\nshorter please\ny\n")
    assert result.exit_code == 0, result.output
    assert model.calls[1]["previous"] is first
    assert model.calls[1]["feedback"] == "shorter please"
    assert backend.created[0][0] is second


def test_new_regenerate_without_feedback(backend, model):
    result = run("new", "bf", "x", input="r\n\ny\n")
    assert result.exit_code == 0, result.output
    assert model.calls[1]["feedback"] is None


def test_new_edit_in_editor(backend, model, tmp_path, monkeypatch):
    model.drafts = [Draft("Old", "old body", "Task", "", Details(parent=Choice("BI-890", "BI-890 AI"), labels=["a"]))]
    script = tmp_path / "editor.py"
    script.write_text(
        "import sys\n"
        "path = sys.argv[1]\n"
        "text = open(path).read()\n"
        "assert 'Parent: BI-890 AI' in text and 'old body' in text, text\n"
        "open(path, 'w').write('Type: Bug\\nTitle: New title\\nParent: none\\nLabels: x, y\\n---\\nNew body\\n')\n"
    )
    monkeypatch.setenv("EDITOR", f"{sys.executable} {script}")
    monkeypatch.delenv("VISUAL", raising=False)
    result = run("new", "bf", "x", input="e\ny\n")
    assert result.exit_code == 0, result.output
    draft = backend.created[0][0]
    assert (draft.title, draft.issue_type, draft.description) == ("New title", "Bug", "New body")
    assert draft.details.parent is None and draft.details.labels == ["x", "y"]


def test_new_edit_then_decline(backend, model, tmp_path, monkeypatch):
    monkeypatch.setenv("EDITOR", "true")  # leaves the file as is
    monkeypatch.delenv("VISUAL", raising=False)
    result = run("new", "bf", "x", input="e\nn\n")
    assert result.exit_code == 1
    assert backend.created == []


def test_new_edit_keeps_acceptance_criteria_section(backend, model, monkeypatch):
    backend.separate_acceptance_criteria = True
    model.drafts = [Draft("T", "Body", "Task", "- ac one")]
    monkeypatch.setenv("EDITOR", "true")
    monkeypatch.delenv("VISUAL", raising=False)
    result = run("new", "bf", "x", input="e\ny\n")
    assert result.exit_code == 0, result.output
    draft = backend.created[0][0]
    assert (draft.description, draft.acceptance_criteria) == ("Body", "- ac one")
    assert model.calls[0]["ac_field"] is True


def test_new_prompts_for_required_fields(backend, model):
    backend.required = [("customfield_1", "Team"), ("customfield_2", "Areas")]
    result = run("new", "bf", "x", "-y", input='Data\n["a", "b"]\n')
    assert result.exit_code == 0, result.output
    assert "requires more fields" in result.output
    assert backend.created[0][1] == {"customfield_1": "Data", "customfield_2": ["a", "b"]}


def test_new_required_field_left_empty_cancels(backend, model):
    backend.required = [("customfield_1", "Team")]
    result = run("new", "bf", "x", "-y", input="\n")
    assert result.exit_code == 1
    assert backend.created == []


@pytest.mark.parametrize(
    "flag, creatable, expected",
    [
        ("bug", ["Task", "Bug"], "Bug"),
        ("BUG", ["Task", "Bug"], "Bug"),
        ("bug", ["Tehtävä", "Virhe"], "Virhe"),
        ("virhe", ["Task", "Bug"], "Bug"),
        ("story", ["Task", "User Story"], "User Story"),
        ("feature", ["Task", "Story"], "Story"),
        ("us", ["User Story"], "User Story"),
        ("parannus", ["Improvement"], "Improvement"),
        ("Tehtävä", ["Tehtävä"], "Tehtävä"),
        ("imp", ["Task", "Improvement"], "Improvement"),  # unique prefix
    ],
)
def test_resolve_type(flag, creatable, expected):
    assert cli._resolve_type(flag, creatable) == expected


def test_resolve_type_unknown_or_ambiguous():
    import typer

    with pytest.raises(typer.Exit):
        cli._resolve_type("nope", ["Task"])
    with pytest.raises(typer.Exit):
        cli._resolve_type("t", ["Task", "Test"])


def test_new_type_flag_limits_model_to_that_type(backend, model):
    result = run("new", "bf", "x", "-t", "bug", "-n")
    assert result.exit_code == 0, result.output
    assert model.calls[0]["types"] == ["Bug"]


def test_new_type_flag_unknown(backend, model):
    result = run("new", "bf", "x", "-t", "nonsense", "-n")
    assert result.exit_code == 2
    assert "Unknown type 'nonsense'. Available: Task, Bug" in result.output  # Epic / Sub-task hidden


def test_new_types_from_examples_skip_ignored_types(backend, model):
    backend.examples = [Example("a", "Bug", "d"), Example("b", "Epic", "d"), Example("c", "Task", "d")]
    result = run("new", "bf", "x", "-n")
    assert result.exit_code == 0, result.output
    assert model.calls[0]["types"] == ["Task", "Bug"]
    assert [e.issue_type for e in model.calls[0]["examples"]] == ["Bug", "Task"]


def test_new_configured_types_not_creatable_are_reported(backend, model):
    backend.types = ["Bug", "Task"]
    result = run("new", "esko", "x", "-n")  # esko: types = ["User Story", "Bug"]
    assert result.exit_code == 0, result.output
    assert "Not creatable in Esko: User Story" in " ".join(result.output.split())
    assert model.calls[0]["types"] == ["Bug"]


def test_new_without_details(backend, model):
    result = run("new", "bf", "x", "-n", "-D")
    assert result.exit_code == 0, result.output
    assert ("profile",) not in backend.calls and ("sprint",) not in backend.calls
    assert model.calls[0]["profile"].sample == 0 and model.calls[0]["sprint"] is None


def test_new_guess_details_off_in_config(backend, model, config_file):
    config_file.write_text(config_file.read_text().replace('project = "BI"', 'project = "BI"\nguess_details = false'))
    run("new", "bf", "x", "-n")
    assert ("profile",) not in backend.calls


def test_new_refresh_bypasses_cache(backend, model):
    run("new", "bf", "x", "-n")
    run("new", "bf", "x", "-n")
    assert backend.calls.count(("profile",)) == 1  # cached
    run("new", "bf", "x", "-n", "-r")
    assert backend.calls.count(("profile",)) == 2
    assert backend.calls.count(("sprint",)) == 3  # never cached


def test_new_duplicate_warning(backend, model):
    backend.examples = [Example("Export fails for big files", "Task", "d", ref="BI-77")]
    model.drafts = [Draft("Export fails for big file", "d", "Task")]
    result = run("new", "bf", "x", "-n")
    assert "Very similar to existing Task BI-77" in result.output


def test_new_unknown_alias(backend, model):
    result = run("new", "nope", "x")
    assert result.exit_code == 2
    assert "Unknown alias 'nope'. Configured: bf, edu, esko" in result.output


def test_new_picker_and_prompted_description(backend, model):
    result = run("new", input="3\nkuvaus tähän\n")
    assert result.exit_code == 1, result.output  # then cancelled at the create prompt (no more input)
    assert model.calls[0]["project"].alias == "esko"
    assert model.calls[0]["user_text"] == "kuvaus tähän"


def test_new_picker_by_alias_name(backend, model):
    run("new", input="edu\nx\nn\n")
    assert model.calls[0]["project"].alias == "edu"


def test_new_empty_description(backend, model):
    result = run("new", "bf", input="\n")
    assert result.exit_code == 2
    assert "Empty description" in result.output


NOTES = "Things to know before switching\n\nCase, not project.\ny\n\nLegacy table;Replaced by;Can switch now?\nFUNDING;F_FUNDING_GRANT;Yes\n"


def test_new_multiline_description_from_stdin_is_kept_whole(backend, model):
    # the lone "y" line is part of the notes; it must never answer the create prompt
    result = run("new", "bf", input=NOTES)
    assert model.calls[0]["user_text"] == NOTES.strip()
    assert backend.created == [] and result.exit_code == 1


def test_new_dash_reads_stdin(backend, model):
    run("new", "bf", "-", input=NOTES)
    assert model.calls[0]["user_text"] == NOTES.strip()


def test_new_interactive_prompt_reads_until_ctrl_d(backend, model, monkeypatch):
    monkeypatch.setattr(cli, "_stdin_is_tty", lambda: True)
    result = run("new", "bf", input=NOTES)
    assert "end with Ctrl-D on an empty line" in result.output
    assert model.calls[0]["user_text"] == NOTES.strip()


def test_new_paste_uses_clipboard_after_given_text(backend, model, monkeypatch):
    monkeypatch.setattr(cli, "paste_from_clipboard", lambda: NOTES)
    run("new", "bf", "-p", "-n")
    assert model.calls[0]["user_text"] == NOTES.strip()
    run("new", "bf", "pub_funding switch guide", "-p", "-n")
    assert model.calls[1]["user_text"] == "pub_funding switch guide\n\n" + NOTES.strip()


@pytest.mark.parametrize("clip, message", [(None, "No clipboard tool found"), (" \n", "The clipboard is empty")])
def test_new_paste_errors(backend, model, monkeypatch, clip, message):
    monkeypatch.setattr(cli, "paste_from_clipboard", lambda: clip)
    result = run("new", "bf", "-p")
    assert result.exit_code == 2 and message in result.output
    assert model.calls == []


# --- tasks --------------------------------------------------------------------

def item(ref, project, title="t", issue_type="Task", status="Open", updated="2026-10-01T00:00:00Z", done=False):
    return Item(ref, title, issue_type, status, project, updated, f"https://t/{ref}", done)


@pytest.fixture
def items(backend, monkeypatch):
    """Each site has its own tickets; the BI-10 duplicate (same URL) must be shown once."""
    per_site = {
        "cloud": FakeBackend(items=[
            item("BI-10", "BI", "Newest", updated="2026-10-05T00:00:00Z", status="In Progress"),
            item("BI-9", "BI", "Old", issue_type="Bug", updated="2026-01-01T00:00:00Z", status="Done", done=True),
            item("X-1", "UNALIASED", "No alias", updated="2026-10-02T00:00:00Z"),
        ]),
        "dc": FakeBackend(items=[
            item("OPHJODTA-2", "OPHJODTA", "Edu", updated="2026-10-04T00:00:00Z"),
            item("BI-10", "BI", "Newest", updated="2026-10-05T00:00:00Z"),
        ]),
        "ado": FakeBackend(items=[item("17", "Esko", "Ado", issue_type="User Story", updated="2026-10-03T00:00:00Z")]),
    }
    queries = []

    def make_backend(site, project=None):
        fake = per_site[site.name]
        fake.calls = queries  # one shared log of (my_items, project, …) calls
        fake.site_name = site.name
        return fake

    monkeypatch.setattr(cli, "make_backend", make_backend)
    return queries


def keys(output: str) -> list[str]:
    """Ticket keys of the plain table, in order (warnings may be printed above the header)."""
    lines = output.splitlines()
    lines = lines[next(i for i, line in enumerate(lines) if line.startswith("key ")) + 1:]
    return [line.split()[0] for line in lines if line.strip() and not line.startswith(" ") and "ticket(s)" not in line]


def test_tasks_plain_grouped_by_project(items):
    result = run("tasks", "--plain")
    assert result.exit_code == 0, result.output
    header = result.output.splitlines()[0].split()
    assert header == ["key", "title", "project", "type", "status", "updated"]
    # grouped by alias (bf, edu, esko), then the project key for tickets without an alias
    assert keys(result.output) == ["BI-10", "BI-9", "OPHJODTA-2", "17", "X-1"]
    assert " edu " in result.output and "UNALIASED" in result.output
    assert "5 ticket(s), open only (-a for all)" in result.output
    assert len(items) == 3  # one query per site


@pytest.mark.parametrize(
    "sort, expected",
    [
        ("updated", ["BI-10", "OPHJODTA-2", "17", "X-1", "BI-9"]),
        ("key", ["17", "BI-9", "BI-10", "OPHJODTA-2", "X-1"]),
        ("type", ["BI-9", "BI-10", "OPHJODTA-2", "X-1", "17"]),  # Bug, Task×3 (newest first), User Story
        ("status", ["BI-10", "OPHJODTA-2", "17", "X-1", "BI-9"]),  # In Progress, Open×3 (newest first), done last
    ],
)
def test_tasks_sorting(items, sort, expected):
    result = run("tasks", "-p", "-o", sort)
    assert keys(result.output) == expected


def test_tasks_bad_sort(items):
    result = run("tasks", "-o", "nope")
    assert result.exit_code == 2 and "Unknown sort" in result.output


def test_tasks_filters_become_queries(backend):
    run("tasks", "-p", "bf", "dc", "OPH", "-s", "export", "-a", "-l", "7")
    queries = [(site, call[1]) for (site, _), call in zip(backend.made, backend.calls)]
    assert sorted(queries, key=str) == sorted([
        ("cloud", "BI"),  # alias -> its site and project
        ("dc", None),  # site name -> whole site
        ("cloud", "OPH"), ("dc", "OPH"), ("ado", "OPH"),  # anything else: project key on every site
    ], key=str)
    assert {call[2:] for call in backend.calls} == {("export", True, 7)}


def test_tasks_one_failing_site_still_shows_others(items, monkeypatch):
    real = cli.make_backend

    def make_backend(site, project=None):
        return FakeBackend(fail=RuntimeError("dc is down")) if site.name == "dc" else real(site, project)

    monkeypatch.setattr(cli, "make_backend", make_backend)
    result = run("tasks", "-p")
    assert result.exit_code == 0
    assert "dc: dc is down" in result.output
    assert keys(result.output) == ["BI-10", "BI-9", "17", "X-1"]


def test_tasks_nothing_found(backend):
    result = run("tasks", "-p")
    assert result.exit_code == 0 and "No tickets found" in result.output


def test_natural_key_and_ago():
    assert sorted(["BI-100", "BI-99", "BI-9"], key=cli._natural_key) == ["BI-9", "BI-99", "BI-100"]
    assert cli._ago("not a date at all") == "not a date"
    assert cli._ago("2020-01-02T00:00:00Z") == "2020-01-02"
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    assert cli._ago(now.isoformat()) == "today"
    assert cli._ago((now - timedelta(days=3)).isoformat()) == "3d ago"


# --- show ---------------------------------------------------------------------

TICKET = Ticket(
    "BI-5", "Broken", "https://cloud.test/browse/BI-5", "Bug", "Open", "BI",
    description="Body", comments=[Comment("Ann", "2026-01-01T00:00:00Z", "Hi")],
)


@pytest.fixture
def shown(backend):
    backend.tickets = {"BI-5": TICKET, "42": TICKET}
    return backend


def test_show_prints_markdown_when_piped(shown):
    result = run("show", "BI-5")
    assert result.exit_code == 0, result.output
    assert result.output == TICKET.to_markdown()
    assert shown.made == [("cloud", None)]  # the site with an alias for BI is tried first


def test_show_accepts_copied_key_and_title(shown):
    assert run("show", "BI-5 Broken thing").exit_code == 0
    assert ("ticket", "BI-5") in shown.calls


def test_show_json(shown):
    data = json.loads(run("show", "BI-5", "--json").output)
    assert data["ref"] == "BI-5" and data["comments"][0]["author"] == "Ann"


def test_show_copy(shown, monkeypatch):
    copied = []
    monkeypatch.setattr(cli, "copy_to_clipboard", lambda text: copied.append(text) or True)
    result = run("show", "BI-5", "-c")
    assert result.exit_code == 0
    assert copied == [TICKET.to_markdown()]
    assert "Copied BI-5 Broken (1 comments" in result.output


def test_show_copy_without_clipboard_tool(shown, monkeypatch):
    monkeypatch.setattr(cli, "copy_to_clipboard", lambda text: False)
    result = run("show", "BI-5", "-c")
    assert result.exit_code == 2 and "No clipboard tool" in result.output


def test_show_tries_other_jira_sites(shown):
    shown.tickets = {}
    result = run("show", "XYZ-1")
    assert result.exit_code == 2
    assert "XYZ-1 not found on cloud, dc" in result.output
    assert [s for s, _ in shown.made] == ["cloud", "dc"]  # never Azure DevOps for a Jira key


def test_show_numeric_id_goes_to_azure_devops(shown):
    assert run("show", "42").exit_code == 0
    assert shown.made == [("ado", None)]


def test_show_prefers_alias_site(shown):
    shown.tickets = {"OPHJODTA-3": TICKET}
    run("show", "ophjodta-3")
    assert shown.made[0] == ("dc", None)


def test_show_url(shown):
    assert run("show", "https://cloud.test/browse/BI-5").exit_code == 0
    assert shown.made == [("cloud", None)] and ("ticket", "BI-5") in shown.calls
    run("show", "https://cloud.test/jira/software/projects/BI/boards/1?selectedIssue=BI-5")
    assert shown.calls[-1] == ("ticket", "BI-5")
    run("show", "https://ado.test/Org/Esko/_workitems/edit/42/")
    assert shown.made[-1] == ("ado", None) and shown.calls[-1] == ("ticket", "42")


def test_show_unknown_url(shown):
    result = run("show", "https://elsewhere.test/browse/BI-5")
    assert result.exit_code == 2 and "Can't tell which configured site" in result.output


def test_show_in_alias_or_site(shown):
    run("show", "BI-5", "-i", "edu")
    assert shown.made == [("dc", None)]
    run("show", "BI-5", "--in", "ado")
    assert shown.made[-1] == ("ado", None)
    result = run("show", "BI-5", "-i", "nowhere")
    assert result.exit_code == 2 and "neither an alias nor a site" in result.output


def test_show_reports_site_errors(shown):
    shown.fail = RuntimeError("401 Unauthorized")
    result = run("show", "BI-5")
    assert result.exit_code == 2
    assert "cloud: 401 Unauthorized" in result.output and "dc: 401 Unauthorized" in result.output


def test_show_rejects_garbage(shown):
    result = run("show", "hello")
    assert result.exit_code == 2 and "doesn't look like a Jira key" in result.output


# --- update -------------------------------------------------------------------

@pytest.fixture
def writer(shown, monkeypatch) -> FakeUpdateModel:
    fake = FakeUpdateModel()
    monkeypatch.setattr(cli.llm, "write_update", fake)
    return fake


def test_update_model_chooses_comment(shown, writer):
    result = run("update", "BI-5", "asiakas", "vahvisti", input="y\n")
    assert result.exit_code == 0, result.output
    assert writer.calls[0]["info"] == "asiakas vahvisti"
    assert writer.calls[0]["target"] is None and writer.calls[0]["ticket"] is TICKET
    assert "Comment on BI-5" in result.output and "Model chose comment: because" in result.output
    assert shown.updates == [("comment", "BI-5", "comment text 1")]
    assert "Commented on BI-5: https://cloud.test/browse/BI-5?focusedCommentId=7" in result.output


def test_update_forced_description(shown, writer):
    result = run("update", "BI-5", "-d", "new scope", "-y")
    assert result.exit_code == 0, result.output
    assert writer.calls[0]["target"] == "description"
    assert "Model chose" not in result.output  # nothing was chosen by the model
    assert shown.updates == [("description", "BI-5", "description text 1")]
    assert "Added to the description of BI-5" in result.output


def test_update_forced_comment_and_both_flags(shown, writer):
    run("update", "BI-5", "-c", "x", "-y")
    assert writer.calls[0]["target"] == "comment"
    result = run("update", "BI-5", "-c", "-d", "x")
    assert result.exit_code == 2 and "either --comment or --description" in result.output


def test_update_switch_target_regenerates(shown, writer):
    result = run("update", "BI-5", "x", input="s\ny\n")
    assert result.exit_code == 0, result.output
    assert [c["target"] for c in writer.calls] == [None, "description"]
    assert writer.calls[1]["previous"] is None
    assert shown.updates == [("description", "BI-5", "description text 2")]


def test_update_rewrite_with_feedback(shown, writer):
    run("update", "BI-5", "x", input="r\nshorter\ny\n")
    assert writer.calls[1]["feedback"] == "shorter"
    assert writer.calls[1]["previous"].text == "comment text 1"
    assert shown.updates == [("comment", "BI-5", "comment text 2")]


def test_update_edit(shown, writer, tmp_path, monkeypatch):
    script = tmp_path / "ed.py"
    script.write_text("import sys, pathlib\npathlib.Path(sys.argv[1]).write_text('edited by hand\\n')\n")
    monkeypatch.setenv("EDITOR", f"{sys.executable} {script}")
    monkeypatch.delenv("VISUAL", raising=False)
    result = run("update", "BI-5", "x", input="e\ny\n")
    assert result.exit_code == 0, result.output
    assert shown.updates == [("comment", "BI-5", "edited by hand")]
    shown.updates.clear()
    assert run("update", "BI-5", "x", input="e\nn\n").exit_code == 1
    assert shown.updates == []


def test_update_cancel_and_dry_run_write_nothing(shown, writer):
    assert run("update", "BI-5", "x", input="n\n").exit_code == 1
    result = run("update", "BI-5", "x", "-n")
    assert result.exit_code == 0 and "Comment on BI-5" in result.output
    assert shown.updates == []


def test_update_reads_clipboard_and_needs_some_text(shown, writer, monkeypatch):
    monkeypatch.setattr(cli, "paste_from_clipboard", lambda: "line 1\nline 2\n")
    run("update", "BI-5", "-p", "-n")
    assert writer.calls[0]["info"] == "line 1\nline 2"
    result = run("update", "BI-5", input="")
    assert result.exit_code == 2 and "Nothing to add" in result.output


def test_update_uses_language_of_matching_alias(shown, writer, config_file):
    config_file.write_text(config_file.read_text().replace("# Business Finland\n", '# Business Finland\nlanguage = "Finnish"\n'))
    run("update", "BI-5", "x", "-n")
    assert writer.calls[0]["language"] == "Finnish"
    shown.tickets = {"ZZ-1": TICKET.__class__(**{**TICKET.to_dict(), "comments": [], "project": "ZZ"})}
    run("update", "ZZ-1", "x", "-n")
    assert writer.calls[1]["language"] is None  # no alias for ZZ: the model detects it


def test_update_unknown_ticket(shown, writer):
    shown.tickets = {}
    result = run("update", "BI-404", "x")
    assert result.exit_code == 2 and "not found" in result.output
    assert writer.calls == []


def test_update_is_a_subcommand_not_an_alias(shown, writer, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["tiket", "update", "BI-5", "x", "-n"])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 0 and writer.calls


# --- other commands -----------------------------------------------------------

def test_projects_command(backend):
    result = run("projects", "cloud")
    assert result.exit_code == 0 and "BI" in result.output and "Opetus" in result.output
    result = run("projects", "nope")
    assert result.exit_code == 2 and "Unknown site 'nope'" in result.output


def test_doctor(backend, monkeypatch):
    import httpx

    class Tags:
        def json(self):
            return {"models": [{"name": "other"}]}

    monkeypatch.setattr(httpx, "get", lambda *a, **k: Tags())
    result = run("doctor")
    assert result.exit_code == 0
    assert "test-model not pulled (have: other)" in result.output
    assert result.output.count("me@test") == 3


def test_parse_value():
    assert cli._parse_value('{"id": "1"}') == {"id": "1"}
    assert cli._parse_value("[1, 2]") == [1, 2]
    assert cli._parse_value("[not json") == "[not json"
    assert cli._parse_value("plain") == "plain"


# --- main(): `tiket <alias> …` shorthand and error handling -------------------

def run_main(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["tiket", *args])
    with pytest.raises(SystemExit) as e:
        cli.main()
    return e.value.code


def test_main_inserts_new_for_alias(monkeypatch, backend, model, capsys):
    assert run_main(monkeypatch, "bf", "some", "text", "-n") == 0
    assert model.calls[0]["user_text"] == "some text"


def test_main_keeps_subcommands_and_help(monkeypatch, capsys):
    assert run_main(monkeypatch, "-h") == 0
    assert "tiket <alias>" in capsys.readouterr().out
    assert run_main(monkeypatch, "tasks", "-h") == 0
    assert "Browse the tickets assigned to you" in capsys.readouterr().out
    assert run_main(monkeypatch, "bf", "-h") == 0  # alias help = create help
    assert "Create a ticket from a short description" in capsys.readouterr().out


def test_main_reports_config_errors(monkeypatch, capsys):
    assert run_main(monkeypatch, "tasks") == 1  # no config in the test environment
    assert "No config found" in capsys.readouterr().out


@pytest.mark.parametrize("error", [ConfigError("bad config"), RuntimeError("api down"), ValueError("bad value")])
def test_main_turns_known_errors_into_messages(monkeypatch, capsys, error):
    def boom():
        raise error

    monkeypatch.setattr(cli, "app", boom)
    assert run_main(monkeypatch, "tasks") == 1
    assert str(error) in capsys.readouterr().out
