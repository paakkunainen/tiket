"""`tiket alias …` edits config.toml in place, keeps comments, and never leaves it invalid."""

import pytest
from typer.testing import CliRunner

from fakes import FakeBackend
from tiket import aliases, cli
from tiket.config import load_config

runner = CliRunner()


@pytest.fixture
def backend(config_file, monkeypatch) -> FakeBackend:
    fake = FakeBackend(types=["Tehtävä", "Virhe"], projects=[("BI", "Business"), ("OPHJOD", "Osaamispolku")])
    monkeypatch.setattr(aliases, "make_backend", lambda site, project=None: fake)
    return fake


def alias(*args, input=None):
    # through the main app, as `tiket alias …`
    return runner.invoke(cli.app, ["alias", *args], input=input)


def test_list_is_the_default(backend, config_file):
    for result in (alias(), alias("list")):
        assert result.exit_code == 0, result.output
        assert "bf" in result.output and "OPHJODTA" in result.output
        assert "User Story, Bug" in result.output  # esko types
        assert str(config_file) in result.output.replace("\n", "")


def test_hidden_list_command_still_works(backend):
    result = runner.invoke(cli.app, ["list"])
    assert result.exit_code == 0 and "esko" in result.output


def test_add_matches_project_case_insensitively(backend, config_file):
    result = alias("add", "jod", "dc", "ophjod")
    assert result.exit_code == 0, result.output
    assert "Added jod → dc / OPHJOD (default type Tehtävä)" in result.output
    assert "Issue types: Tehtävä, Virhe" in result.output
    p = load_config().projects["jod"]
    assert (p.site.name, p.project, p.default_type) == ("dc", "OPHJOD", "Tehtävä")
    assert "# test config" in config_file.read_text()
    assert "# Business Finland" in config_file.read_text()  # inline comments survive too


def test_add_by_project_name_with_type(backend):
    result = alias("add", "jodbug", "dc", "Osaamispolku", "-t", "Virhe")
    assert result.exit_code == 0, result.output
    assert load_config().projects["jodbug"].default_type == "Virhe"


@pytest.mark.parametrize(
    "args, message",
    [
        (("bf", "cloud", "BI"), "Alias 'bf' already exists"),
        (("x", "nosite", "BI"), "Unknown site 'nosite'"),
        (("x", "cloud", "NOPE"), "No project 'NOPE' on cloud"),
        (("x", "cloud", "BI", "-t", "Story"), "'Story' is not a type in BI. Available: Tehtävä, Virhe"),
    ],
)
def test_add_rejects(backend, config_file, args, message):
    before = config_file.read_text()
    result = alias("add", *args)
    assert result.exit_code == 2
    assert message in " ".join(result.output.split())
    assert config_file.read_text() == before


def test_add_when_issue_types_are_not_readable(backend):
    backend.fail = RuntimeError("400 createmeta")
    result = alias("add", "pub", "dc", "BI")
    assert result.exit_code == 0, result.output
    assert "Could not read issue types for BI" in " ".join(result.output.split())
    assert load_config().projects["pub"].default_type == "Task"


def test_rename_keeps_position_and_settings(backend):
    result = alias("rename", "edu", "jod")
    assert result.exit_code == 0, result.output
    projects = load_config().projects
    assert list(projects) == ["bf", "jod", "esko"]
    assert projects["jod"].project == "OPHJODTA" and projects["jod"].alias == "jod"


def test_rename_errors(backend):
    assert "No alias 'nope'" in alias("rename", "nope", "x").output
    assert "Alias 'esko' already exists" in alias("rename", "bf", "esko").output


def test_remove(backend, config_file):
    assert alias("remove", "edu").exit_code == 0
    assert list(load_config().projects) == ["bf", "esko"]
    assert "[sites.dc]" in config_file.read_text()  # the site stays
    assert alias("remove", "edu").exit_code == 2


def test_set_values(backend):
    result = alias("set", "bf", "default_type=Bug", "types=Bug, Task,", "assign_to_me=no", "guess_details=TRUE",
                   "language=Finnish", "style_notes=Keep it short")
    assert result.exit_code == 0, result.output
    p = load_config().projects["bf"]
    assert p.default_type == "Bug"
    assert p.types == ["Bug", "Task"]
    assert p.assign_to_me is False and p.guess_details is True
    assert p.language == "Finnish" and p.style_notes == "Keep it short"


def test_set_empty_value_restores_default(backend, config_file):
    assert alias("set", "esko", "types=", "default_type=").exit_code == 0
    p = load_config().projects["esko"]
    assert p.types == [] and p.default_type == "Task"
    assert "types" not in config_file.read_text().split("[projects.esko]")[1]


def test_set_moves_alias_to_other_site(backend):
    assert alias("set", "bf", "site=dc", "project=OPH").exit_code == 0
    p = load_config().projects["bf"]
    assert (p.site.name, p.project) == ("dc", "OPH")


@pytest.mark.parametrize(
    "assignment, message",
    [
        ("colour=red", "Expected key=value with key one of"),
        ("default_type", "Expected key=value"),
        ("assign_to_me=maybe", "expected true/false"),
    ],
)
def test_set_rejects_bad_input(backend, config_file, assignment, message):
    before = config_file.read_text()
    result = alias("set", "bf", assignment)
    assert result.exit_code == 2
    assert message in result.output
    assert config_file.read_text() == before


def test_set_unknown_alias(backend):
    assert "No alias 'zzz'" in alias("set", "zzz", "language=x").output


def test_invalid_result_is_reverted(backend, config_file):
    before = config_file.read_text()
    result = alias("set", "bf", "site=nosuchsite")
    assert result.exit_code == 2
    assert "Change reverted" in result.output
    assert config_file.read_text() == before


def test_edits_go_through_symlinks(backend, config_file, tmp_path, monkeypatch):
    link = tmp_path / "link.toml"
    link.symlink_to(config_file)
    monkeypatch.setenv("TIKET_CONFIG", str(link))
    assert alias("rename", "bf", "biz").exit_code == 0
    assert link.is_symlink()  # the real file was edited, the link is intact
    assert "[projects.biz]" in config_file.read_text()


def test_config_command_validates_after_editing(backend, config_file, tmp_path, monkeypatch):
    # First edit breaks the config, the user reopens and the second edit fixes it
    script = tmp_path / "ed.py"
    state = tmp_path / "count"
    script.write_text(
        "import sys, pathlib\n"
        f"state = pathlib.Path({str(state)!r})\n"
        "n = int(state.read_text()) if state.exists() else 0\n"
        "state.write_text(str(n + 1))\n"
        "p = pathlib.Path(sys.argv[1])\n"
        "t = p.read_text()\n"
        "p.write_text(t.replace('site = \"cloud\"', 'site = \"broken\"') if n == 0 else t.replace('broken', 'cloud'))\n"
    )
    import sys

    monkeypatch.setenv("EDITOR", f"{sys.executable} {script}")
    monkeypatch.delenv("VISUAL", raising=False)
    result = runner.invoke(cli.app, ["config"], input="y\n")
    assert result.exit_code == 0, result.output
    assert "unknown site 'broken'" in result.output
    assert "Config OK (3 aliases: bf, edu, esko)" in result.output
    assert state.read_text() == "2"


def test_config_command_gives_up_when_declined(backend, config_file, monkeypatch, tmp_path):
    import sys

    script = tmp_path / "ed.py"
    script.write_text("import sys, pathlib\np = pathlib.Path(sys.argv[1])\np.write_text('not = [valid')\n")
    monkeypatch.setenv("EDITOR", f"{sys.executable} {script}")
    monkeypatch.delenv("VISUAL", raising=False)
    result = runner.invoke(cli.app, ["config"], input="n\n")
    assert result.exit_code == 1
    assert "Config has a problem" in result.output
