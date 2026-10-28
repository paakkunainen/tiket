"""`tiket alias …` and `tiket config`: manage project aliases without hand-editing TOML.

Edits go through tomlkit so comments and formatting in config.toml survive."""

import os
import subprocess

import tomlkit
import typer
from rich.console import Console
from rich.table import Table

from .backends import make_backend
from .config import ConfigError, Project, find_config_path, load_config, load_sites

console = Console()
app = typer.Typer(
    rich_markup_mode="markdown",
    context_settings={"help_option_names": ["-h", "--help"]},
    help="""Manage project aliases (the short names like `bf` or `esko` used in `tiket bf "…"`).

Edits keep the comments in config.toml. For extra_fields or site settings use `tiket config`.""",
    epilog="""**Examples**

```
tiket alias                                  # list aliases
tiket alias add jod eduuni OPHJOD            # new alias for project OPHJOD on site eduuni
tiket alias add jodbug eduuni OPHJOD -t Bug  # …with Bug as the default type
tiket alias rename tijo omaolo               # tiket omaolo "…" from now on
tiket alias set laak default_type=Virhe types=Tehtävä,Virhe
tiket alias set bf assign_to_me=false guess_details=false
tiket alias remove laak
```
""",
    invoke_without_command=True,
)

# Settings `tiket alias set` may change, with how to parse the value
SETTABLE = {
    "site": str,
    "project": str,
    "default_type": str,
    "types": lambda v: [t.strip() for t in v.split(",") if t.strip()],
    "language": str,
    "style_notes": str,
    "assign_to_me": lambda v: _bool(v),
    "guess_details": lambda v: _bool(v),
}


def _bool(v: str) -> bool:
    if v.lower() in ("true", "yes", "1", "on"):
        return True
    if v.lower() in ("false", "no", "0", "off"):
        return False
    raise typer.BadParameter(f"expected true/false, got '{v}'")


def _die(msg: str):
    console.print(f"[red]{msg}[/]")
    raise typer.Exit(2)


def _load() -> tuple:
    path = find_config_path().resolve()  # ~/.config/tiket/config.toml may be a symlink
    return path, tomlkit.parse(path.read_text())


def _save(path, doc) -> None:
    backup = path.read_text()
    path.write_text(tomlkit.dumps(doc))
    try:
        load_config()  # never leave a config behind that the tool can't read
    except Exception as e:
        path.write_text(backup)
        _die(f"Change reverted, config would be invalid: {e}")


def _projects(doc) -> dict:
    if "projects" not in doc:
        doc["projects"] = tomlkit.table(is_super_table=True)
    return doc["projects"]


@app.callback()
def default(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        list_aliases()


@app.command("list")
def list_aliases():
    """Show configured aliases."""
    cfg = load_config()
    table = Table("alias", "site", "project", "default type", "types", "assign", "details", box=None)
    for a, p in cfg.projects.items():
        table.add_row(
            a, p.site.name, p.project, p.default_type, ", ".join(p.types) or "auto",
            "me" if p.assign_to_me else "—", "guess" if p.guess_details else "off",
        )
    console.print(table)
    console.print(f"[dim]config: {cfg.path}[/]")


@app.command()
def add(
    alias: str = typer.Argument(..., help="New short name, e.g. `jod`"),
    site: str = typer.Argument(..., help="Site name from config (see `tiket alias`)"),
    project: str = typer.Argument(..., help="Project key (Jira) or name (Azure DevOps); see `tiket projects <site>`"),
    default_type: str = typer.Option(None, "--type", "-t", help="Default issue type; default: first creatable of Task/Tehtävä"),
):
    """Add an alias. Checks that the project exists and shows its issue types."""
    path, doc = _load()
    projects = _projects(doc)
    if alias in projects:
        _die(f"Alias '{alias}' already exists; use `tiket alias set` or `tiket alias remove` first.")
    sites = load_sites()
    if site not in sites:
        _die(f"Unknown site '{site}'. Configured: {', '.join(sites)}")

    backend = make_backend(sites[site])
    available = backend.list_projects()
    match = next((k for k, name in available if project.lower() in (k.lower(), name.lower())), None)
    if not match:
        _die(f"No project '{project}' on {site}. Run `tiket projects {site}` to see the keys.")

    try:
        types = make_backend(sites[site], Project(alias, sites[site], match)).issue_types()
    except RuntimeError:
        types = []
        console.print(
            f"[yellow]Could not read issue types for {match}; your token may not be allowed to create "
            "issues there. The alias still works for `tiket tasks`.[/]"
        )
    if default_type is None:
        default_type = next((t for t in ("Task", "Tehtävä") if t in types), types[0] if types else "Task")
    elif types and default_type not in types:
        _die(f"'{default_type}' is not a type in {match}. Available: {', '.join(types)}")

    entry = tomlkit.table()
    entry["site"] = site
    entry["project"] = match
    entry["default_type"] = default_type
    projects[alias] = entry
    _save(path, doc)
    console.print(f"[green]Added[/] {alias} → {site} / {match} (default type {default_type})")
    if types:
        console.print(f"[dim]Issue types: {', '.join(types)}[/]")


@app.command()
def rename(old: str = typer.Argument(..., help="Current alias"), new: str = typer.Argument(..., help="New alias")):
    """Rename an alias (settings and cached history are kept)."""
    path, doc = _load()
    projects = _projects(doc)
    if old not in projects:
        _die(f"No alias '{old}'. Configured: {', '.join(projects)}")
    if new in projects:
        _die(f"Alias '{new}' already exists.")
    # Rebuild to keep the alias in its original position
    items = [(new if k == old else k, v) for k, v in projects.items()]
    for k in list(projects):
        del projects[k]
    for k, v in items:
        projects[k] = v
    _save(path, doc)
    console.print(f"[green]Renamed[/] {old} → {new}")


@app.command()
def remove(alias: str = typer.Argument(..., help="Alias to delete")):
    """Remove an alias (the project itself is untouched)."""
    path, doc = _load()
    projects = _projects(doc)
    if alias not in projects:
        _die(f"No alias '{alias}'. Configured: {', '.join(projects)}")
    del projects[alias]
    _save(path, doc)
    console.print(f"[green]Removed[/] {alias}")


@app.command("set")
def set_values(
    alias: str = typer.Argument(..., help="Alias to change"),
    assignments: list[str] = typer.Argument(..., help="One or more key=value"),
):
    """Change alias settings with key=value pairs.

    Keys: **site**, **project**, **default_type**, **types** (comma separated; empty = auto),
    **language**, **style_notes**, **assign_to_me** and **guess_details** (true/false).
    """
    path, doc = _load()
    projects = _projects(doc)
    if alias not in projects:
        _die(f"No alias '{alias}'. Configured: {', '.join(projects)}")
    for assignment in assignments:
        key, sep, value = assignment.partition("=")
        key = key.strip()
        if not sep or key not in SETTABLE:
            _die(f"Expected key=value with key one of: {', '.join(SETTABLE)}")
        parsed = SETTABLE[key](value.strip())
        if parsed in ("", []):
            projects[alias].pop(key, None)  # empty value = back to the default
        else:
            projects[alias][key] = parsed
    _save(path, doc)
    console.print(f"[green]Updated[/] {alias}")


def edit_config() -> None:
    """Open config.toml in $EDITOR and validate it afterwards."""
    path = find_config_path().resolve()
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    while True:
        subprocess.run(f'{editor} "{path}"', shell=True, check=True)
        try:
            cfg = load_config()
        except (ConfigError, Exception) as e:
            console.print(f"[red]Config has a problem:[/] {e}")
            if typer.confirm("Open the editor again?", default=True):
                continue
            raise typer.Exit(1)
        console.print(f"[green]Config OK[/] ({len(cfg.projects)} aliases: {', '.join(cfg.projects)})")
        return
