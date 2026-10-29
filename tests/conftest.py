"""Shared fixtures. Every test runs against a throw-away config and cache, and real network access
is blocked: HTTP is mocked with respx (or fake backends), so no test can touch a real tracker."""

import socket
from pathlib import Path

import pytest

from tiket import config, style_cache
from tiket.config import Project, Site

CONFIG = """\
# test config
[ollama]
url = "http://ollama.test"
model = "test-model"
examples = 5

[sites.cloud]
kind = "jira_cloud"
url = "https://cloud.test/"
email = "me@cloud.test"
token = "${TOKEN_CLOUD}"

[sites.dc]
kind = "jira_dc"
url = "https://dc.test"
token = "${TOKEN_DC}"

[sites.ado]
kind = "azure_devops"
url = "https://ado.test/Org"
token = "${TOKEN_ADO}"

[projects.bf]  # Business Finland
site = "cloud"
project = "BI"
default_type = "Task"

[projects.edu]
site = "dc"
project = "OPHJODTA"

[projects.esko]
site = "ado"
project = "Esko"
default_type = "User Story"
types = ["User Story", "Bug"]
"""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    real_connect = socket.socket.connect

    def guarded(self, address):
        if isinstance(address, tuple):  # AF_INET/AF_INET6; unix sockets (asyncio internals) are fine
            raise RuntimeError(f"Test tried to open a real network connection to {address}")
        return real_connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Never read ~/.config/tiket or write ~/.cache/tiket."""
    home_config = tmp_path / "home-config"
    home_config.mkdir()
    cache = tmp_path / "cache"
    monkeypatch.setattr(config, "CONFIG_DIR", home_config)
    monkeypatch.setattr(config, "CACHE_DIR", cache)
    monkeypatch.setattr(style_cache, "CACHE_DIR", cache)
    monkeypatch.delenv("TIKET_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    for name in ("TOKEN_CLOUD", "TOKEN_DC", "TOKEN_ADO"):
        monkeypatch.setenv(name, f"secret-{name.lower()}")
    return tmp_path


@pytest.fixture
def config_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "cfg" / "config.toml"
    path.parent.mkdir()
    path.write_text(CONFIG)
    monkeypatch.setenv("TIKET_CONFIG", str(path))
    return path


@pytest.fixture
def cloud_site() -> Site:
    return Site("cloud", "jira_cloud", "https://cloud.test", "tok", "me@cloud.test")


@pytest.fixture
def dc_site() -> Site:
    return Site("dc", "jira_dc", "https://dc.test", "pat")


@pytest.fixture
def ado_site() -> Site:
    return Site("ado", "azure_devops", "https://ado.test/Org", "pat")


@pytest.fixture
def cloud_project(cloud_site) -> Project:
    return Project("bf", cloud_site, "BI")


@pytest.fixture
def dc_project(dc_site) -> Project:
    return Project("edu", dc_site, "OPHJODTA")


@pytest.fixture
def ado_project(ado_site) -> Project:
    return Project("esko", ado_site, "Esko")
