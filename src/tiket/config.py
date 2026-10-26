import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

CONFIG_DIR = Path.home() / ".config" / "tiket"
CACHE_DIR = Path.home() / ".cache" / "tiket"


class ConfigError(Exception):
    pass


@dataclass
class Site:
    name: str
    kind: str  # jira_cloud | jira_dc | azure_devops
    url: str
    token: str
    email: str | None = None


@dataclass
class Project:
    alias: str
    site: Site
    project: str
    default_type: str = "Task"
    types: list[str] = field(default_factory=list)
    language: str | None = None
    style_notes: str = ""
    extra_fields: dict = field(default_factory=dict)
    assign_to_me: bool = False


@dataclass
class OllamaSettings:
    url: str = "http://localhost:11434"
    model: str = "gpt-oss:20b"
    language: str = "auto"
    think: str | None = "medium"
    examples: int = 8


@dataclass
class Config:
    path: Path
    ollama: OllamaSettings
    projects: dict[str, Project]


def find_config_path() -> Path:
    candidates = []
    if env := os.environ.get("TIKET_CONFIG"):
        candidates.append(Path(env).expanduser())
    candidates += [CONFIG_DIR / "config.toml", Path.cwd() / "config.toml"]
    for p in candidates:
        if p.is_file():
            return p
    raise ConfigError(
        "No config found. Looked in: " + ", ".join(str(p) for p in candidates)
    )


def _expand(value: str) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name not in os.environ:
            raise ConfigError(
                f"Environment variable {name} is not set (check your .env)"
            )
        return os.environ[name]

    return re.sub(r"\$\{(\w+)\}", repl, value)


def _load_raw() -> tuple[Path, dict]:
    path = find_config_path()
    # .env next to the config (resolving symlinks) and in ~/.config/tiket
    for env_file in {
        path.parent / ".env",
        path.resolve().parent / ".env",
        CONFIG_DIR / ".env",
    }:
        if env_file.is_file():
            load_dotenv(env_file, override=False)
    return path, tomllib.loads(path.read_text())


def _site(name: str, s: dict) -> Site:
    return Site(
        name=name,
        kind=s["kind"],
        url=s["url"].rstrip("/"),
        token=_expand(s["token"]),
        email=s.get("email"),
    )


def load_config() -> Config:
    path, raw = _load_raw()
    ollama = OllamaSettings(**raw.get("ollama", {}))

    sites: dict[str, dict] = raw.get("sites", {})
    projects: dict[str, Project] = {}
    for alias, p in raw.get("projects", {}).items():
        site_name = p.get("site")
        if site_name not in sites:
            raise ConfigError(f"Project '{alias}' refers to unknown site '{site_name}'")
        projects[alias] = Project(
            alias=alias,
            site=_site(site_name, sites[site_name]),
            project=p["project"],
            default_type=p.get("default_type", "Task"),
            types=p.get("types", []),
            language=p.get("language"),
            style_notes=p.get("style_notes", ""),
            extra_fields=p.get("extra_fields", {}),
            assign_to_me=p.get("assign_to_me", False),
        )
    return Config(path=path, ollama=ollama, projects=projects)


def load_sites() -> dict[str, Site]:
    """All sites, including ones without projects yet (for `tiket projects` / `doctor`)."""
    _, raw = _load_raw()
    return {name: _site(name, s) for name, s in raw.get("sites", {}).items()}
