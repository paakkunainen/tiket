import os

import pytest

from tiket import config
from tiket.config import ConfigError, find_config_path, load_config, load_sites


def test_loads_sites_projects_and_defaults(config_file):
    cfg = load_config()
    assert cfg.path == config_file
    assert cfg.ollama.url == "http://ollama.test"
    assert cfg.ollama.model == "test-model"
    assert cfg.ollama.examples == 5
    assert cfg.ollama.think == "medium"  # default kept when not in the file

    assert list(cfg.projects) == ["bf", "edu", "esko"]
    bf = cfg.projects["bf"]
    assert bf.site.kind == "jira_cloud"
    assert bf.site.url == "https://cloud.test"  # trailing slash stripped
    assert bf.site.email == "me@cloud.test"
    assert bf.site.token == "secret-token_cloud"  # ${VAR} expanded
    assert (bf.assign_to_me, bf.guess_details, bf.types, bf.extra_fields) == (True, True, [], {})

    edu = cfg.projects["edu"]
    assert edu.default_type == "Task"  # default
    assert edu.site.email is None

    esko = cfg.projects["esko"]
    assert esko.types == ["User Story", "Bug"]
    assert esko.default_type == "User Story"


def test_optional_project_settings(config_file):
    config_file.write_text(
        config_file.read_text()
        + """
[projects.full]
site = "cloud"
project = "FULL"
language = "Finnish"
style_notes = "short"
assign_to_me = false
guess_details = false
extra_fields = { labels = ["x"] }
"""
    )
    p = load_config().projects["full"]
    assert p.language == "Finnish"
    assert p.style_notes == "short"
    assert p.assign_to_me is False and p.guess_details is False
    assert p.extra_fields == {"labels": ["x"]}


def test_missing_env_var_is_a_config_error(config_file, monkeypatch):
    monkeypatch.delenv("TOKEN_DC")
    with pytest.raises(ConfigError, match="TOKEN_DC"):
        load_config()


def test_unknown_site_is_a_config_error(config_file):
    config_file.write_text(config_file.read_text() + '\n[projects.bad]\nsite = "nope"\nproject = "X"\n')
    with pytest.raises(ConfigError, match="unknown site 'nope'"):
        load_config()


def test_load_sites_includes_sites_without_projects(config_file):
    config_file.write_text(
        config_file.read_text()
        + '\n[sites.spare]\nkind = "jira_dc"\nurl = "https://spare.test"\ntoken = "plain"\n'
    )
    sites = load_sites()
    assert set(sites) == {"cloud", "dc", "ado", "spare"}
    assert sites["spare"].token == "plain"


def test_no_config_lists_where_it_looked():
    with pytest.raises(ConfigError, match="No config found"):
        find_config_path()


def test_config_lookup_order(tmp_path, monkeypatch):
    cwd_cfg = tmp_path / "config.toml"  # tests run with cwd = tmp_path
    cwd_cfg.write_text("")
    assert find_config_path() == cwd_cfg

    home_cfg = config.CONFIG_DIR / "config.toml"
    home_cfg.write_text("")
    assert find_config_path() == home_cfg

    env_cfg = tmp_path / "elsewhere.toml"
    env_cfg.write_text("")
    monkeypatch.setenv("TIKET_CONFIG", str(env_cfg))
    assert find_config_path() == env_cfg


def test_env_file_next_to_config_is_loaded(config_file, monkeypatch):
    monkeypatch.delenv("TOKEN_DC")
    (config_file.parent / ".env").write_text("TOKEN_DC=from-dotenv\n")
    assert load_config().projects["edu"].site.token == "from-dotenv"
    os.environ.pop("TOKEN_DC", None)


def test_env_file_next_to_symlink_target_is_loaded(tmp_path, monkeypatch, config_file):
    # ~/.config/tiket/config.toml is a symlink into the repo, where .env lives
    monkeypatch.delenv("TOKEN_ADO")
    (config_file.parent / ".env").write_text("TOKEN_ADO=from-repo\n")
    link = tmp_path / "link" / "config.toml"
    link.parent.mkdir()
    link.symlink_to(config_file)
    monkeypatch.setenv("TIKET_CONFIG", str(link))
    assert load_config().projects["esko"].site.token == "from-repo"
    os.environ.pop("TOKEN_ADO", None)


def test_real_environment_wins_over_dotenv(config_file):
    (config_file.parent / ".env").write_text("TOKEN_DC=from-dotenv\n")
    assert load_config().projects["edu"].site.token == "secret-token_dc"


def test_make_backend_by_site_kind(cloud_site, dc_site, ado_site, cloud_project):
    from tiket.backends import make_backend
    from tiket.backends.azure_devops import AzureDevOpsBackend
    from tiket.backends.jira import JiraBackend

    assert isinstance(make_backend(cloud_site), JiraBackend) and make_backend(cloud_site).cloud
    assert isinstance(make_backend(dc_site), JiraBackend) and not make_backend(dc_site).cloud
    assert isinstance(make_backend(ado_site), AzureDevOpsBackend)
    assert make_backend(cloud_site, cloud_project).project is cloud_project
    dc_site.kind = "gitlab"
    with pytest.raises(ValueError, match="Unknown site kind 'gitlab'"):
        make_backend(dc_site)
