import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import typer
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from . import llm, style_cache
from .backends import Example, Item, MissingFieldsError, make_backend
from .config import ConfigError, load_config, load_sites

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    help="AI-written tickets for Jira / Azure DevOps.",
)
console = Console()
AC_MARKER = "--- acceptance criteria ---"
DUPLICATE_RATIO = 0.8
# Short names for --type, resolved against the project's real type names (first match wins)
TYPE_SYNONYMS = {
    "bug": ["Bug", "Virhe"],
    "task": ["Task", "Tehtävä"],
    "story": ["User Story", "Story", "Tarina"],
    "feature": [
        "Feature",
        "New Feature",
        "Feature Request",
        "User Story",
        "Story",
        "Tarina",
    ],
    "improvement": ["Improvement", "Parannus"],
}
TYPE_SYNONYMS |= {
    "virhe": TYPE_SYNONYMS["bug"],
    "tehtävä": TYPE_SYNONYMS["task"],
    "us": TYPE_SYNONYMS["story"],
    "tarina": TYPE_SYNONYMS["story"],
    "parannus": TYPE_SYNONYMS["improvement"],
}
SUBCOMMANDS = {"new", "list", "projects", "doctor", "tasks"}
# Work item types that are never a sensible target or style example for a quick ticket
IGNORED_TYPES = {
    "Test Case",
    "Test Suite",
    "Test Plan",
    "Shared Steps",
    "Shared Parameter",
    "Epic",
    "Feature",
    "Sub-task",
    "Sub-Task",
    "Subtask",
    "Alitehtävä",
}


@app.command()
def new(
    alias: str = typer.Argument(
        None, help="Project alias from config (omit for a picker)"
    ),
    text: list[str] = typer.Argument(None, help="Short description of the problem"),
    issue_type: str = typer.Option(
        None,
        "--type",
        "-t",
        help="Issue type, e.g. bug / task / story / feature or the exact name. Omit to let the model choose.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n", help="Generate only, never create"
    ),
    refresh: bool = typer.Option(
        False, "--refresh", "-r", help="Re-fetch style examples"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Create without asking"),
):
    """Create a ticket: tiket <alias> "what is broken"."""
    cfg = load_config()
    if not cfg.projects:
        _die(f"No projects configured in {cfg.path}")

    if alias is None:
        alias = _pick_alias(cfg.projects)
    elif alias not in cfg.projects:
        _die(f"Unknown alias '{alias}'. Configured: {', '.join(cfg.projects)}")
    project = cfg.projects[alias]

    user_text = " ".join(text or []).strip()
    if not user_text:
        user_text = Prompt.ask("[bold]Describe the problem[/]").strip()
    if not user_text:
        _die("Empty description")

    backend = make_backend(project.site, project)
    with console.status(f"Fetching recent tickets from {project.project}…"):
        examples = style_cache.get_examples(
            project, backend, cfg.ollama.examples, refresh=refresh
        )
        creatable = [t for t in backend.issue_types() if t not in IGNORED_TYPES]
    examples = [e for e in examples if e.issue_type not in IGNORED_TYPES]

    if issue_type:
        types = [_resolve_type(issue_type, creatable)]
    elif project.types:
        types = project.types
    else:
        types = list(
            dict.fromkeys([project.default_type, *(e.issue_type for e in examples)])
        )
    if bad := [t for t in types if t not in creatable]:
        console.print(
            f"[yellow]Not creatable in {project.project}: {', '.join(bad)}. "
            f"Available: {', '.join(creatable)}[/]"
        )
        types = [t for t in types if t in creatable] or creatable

    draft, feedback = None, None
    while True:
        with console.status(f"Writing ticket with {cfg.ollama.model}…"):
            draft = llm.generate(
                cfg.ollama,
                project,
                examples,
                types,
                user_text,
                draft,
                feedback,
                ac_field=backend.separate_acceptance_criteria,
            )
        _show(draft, project)
        _warn_duplicates(draft, examples)
        if dry_run:
            return
        if yes:
            break
        choice = Prompt.ask(
            "[bold]Create?[/] [green]y[/]es / [yellow]r[/]egenerate / [cyan]e[/]dit / [red]n[/]o",
            choices=["y", "r", "e", "n"],
            default="y",
            show_choices=False,
        )
        if choice == "y":
            break
        if choice == "n":
            raise typer.Exit(1)
        if choice == "e":
            draft = _edit(draft)
            _show(draft, project)
            if (
                Prompt.ask(
                    "[bold]Create edited ticket?[/]", choices=["y", "n"], default="y"
                )
                == "y"
            ):
                break
            raise typer.Exit(1)
        feedback = (
            Prompt.ask("Feedback for the rewrite (enter to skip)", default="") or None
        )

    extra: dict = {}
    while True:
        try:
            with console.status("Creating ticket…"):
                url = backend.create(
                    draft.title,
                    draft.description,
                    draft.issue_type,
                    draft.acceptance_criteria,
                    extra,
                )
            break
        except MissingFieldsError as e:
            console.print(
                f"[yellow]{project.project} requires more fields for {draft.issue_type}.[/] "
                "[dim]Add them to extra_fields in config to skip this next time.[/]"
            )
            for ref, label in e.fields:
                value = Prompt.ask(
                    f"[bold]{label}[/] [dim]({ref}, JSON allowed, enter to cancel)[/]"
                ).strip()
                if not value:
                    raise typer.Exit(1)
                extra[ref] = _parse_value(value)
    console.print(f"[bold green]Created:[/] {url}")


def _natural_key(ref: str) -> tuple:
    # BI-99 before BI-100, 2584 before 25840
    return tuple(int(p) if p.isdigit() else p.lower() for p in re.split(r"(\d+)", ref))


# `tiket tasks --sort` options; each maps (project label, item) to a sort key
TASK_SORTS = {
    "project": lambda label, item: label.lower(),
    "updated": None,  # rows are already newest first
    "key": lambda label, item: _natural_key(item.ref),
    "type": lambda label, item: item.issue_type.lower(),
    "status": lambda label, item: (item.done, item.status.lower()),
}


@app.command()
def tasks(
    where: list[str] = typer.Argument(
        None,
        help="Filter by project alias (esko), site name (digifinland) or project key (BI). Several allowed.",
    ),
    search: str = typer.Option(
        None, "--search", "-s", help="Only tickets whose title contains this text"
    ),
    include_done: bool = typer.Option(
        False, "--all", "-a", help="Include done / closed tickets"
    ),
    limit: int = typer.Option(
        50, "--limit", "-l", help="Max tickets per site and filter"
    ),
    sort: str = typer.Option(
        "project",
        "--sort",
        "-o",
        help="Order by: project, updated, key, type or status",
    ),
    plain: bool = typer.Option(
        False, "--plain", "-p", help="Print a table instead of the interactive view"
    ),
):
    """List tickets assigned to you across all boards."""
    if sort not in TASK_SORTS:
        _die(f"Unknown sort '{sort}'. Use one of: {', '.join(TASK_SORTS)}")
    cfg = load_config()
    sites = load_sites()

    # Each filter becomes (site, project-or-None) queries; no filter = every site, every project
    queries: list[tuple[str, str | None]] = []
    for w in where or []:
        if w in cfg.projects:
            queries.append((cfg.projects[w].site.name, cfg.projects[w].project))
        elif w in sites:
            queries.append((w, None))
        else:
            queries += [(s, w) for s in sites]
    queries = list(dict.fromkeys(queries or [(s, None) for s in sites]))

    def run(q: tuple[str, str | None]) -> tuple[str, list[Item] | Exception]:
        site, project = q
        try:
            return site, make_backend(sites[site]).my_items(
                project, search, include_done, limit
            )
        except Exception as e:  # one broken site shouldn't hide the others
            return site, e

    with console.status(
        f"Fetching your tickets from {len({s for s, _ in queries})} site(s)…"
    ):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(run, queries))

    alias_of = {(p.site.name, p.project.lower()): a for a, p in cfg.projects.items()}
    rows: list[tuple[str, Item]] = []
    seen: set[str] = set()
    failed: list[str] = []
    for site, result in results:
        if isinstance(result, Exception):
            console.print(f"[yellow]{site}: {result}[/]")
            failed.append(site)
            continue
        for item in result:
            if item.url not in seen:
                seen.add(item.url)
                rows.append(
                    (alias_of.get((site, item.project.lower()), item.project), item)
                )
    if not rows:
        console.print("[dim]No tickets found.[/]")
        return

    # Newest change first, then a stable sort on the chosen column keeps that as the tiebreak
    rows.sort(key=lambda r: r[1].updated, reverse=True)
    if sort != "updated":
        rows.sort(key=lambda r: TASK_SORTS[sort](r[0], r[1]))

    if not plain and sys.stdin.isatty() and sys.stdout.isatty():
        from .tui import TaskBrowser, TaskRow

        note = f" · ⚠ failed: {', '.join(failed)}" if failed else ""
        TaskBrowser(
            [
                TaskRow(
                    item.ref,
                    item.title,
                    label,
                    item.issue_type,
                    item.status,
                    _ago(item.updated),
                    item.url,
                    item.done,
                )
                for label, item in rows
            ],
            note=note,
        ).run()
        return

    def width(values: list[str], name: str, cap: int) -> int:
        return min(cap, max(len(v) for v in values + [name]))

    # Short columns keep their width; the title gets whatever is left of the terminal
    columns = [
        ("key", width([i.ref for _, i in rows], "key", 14)),
        ("title", 0),
        ("project", width([p for p, _ in rows], "project", 12)),
        ("type", width([i.issue_type for _, i in rows], "type", 10)),
        ("status", width([i.status for _, i in rows], "status", 14)),
        ("updated", 10),
    ]
    padding = 2 * (len(columns) - 1)
    title_width = max(20, console.width - padding - sum(w for _, w in columns))
    table = Table(box=None, header_style="bold", pad_edge=False)
    for name, w in columns:
        w = w or title_width
        table.add_column(
            name,
            no_wrap=True,
            overflow="ellipsis",
            min_width=w,
            max_width=w,
            justify="right" if name == "updated" else "left",
        )
    for where_label, item in rows:
        table.add_row(
            f"[link={item.url}]{item.ref}[/link]",
            item.title,
            where_label,
            item.issue_type,
            f"[dim]{item.status}[/]" if item.done else item.status,
            _ago(item.updated),
        )
    console.print(table)
    console.print(
        f"[dim]{len(rows)} ticket(s){'' if include_done else ', open only (-a for all)'}. "
        "Keys are links (cmd-click).[/]"
    )


@app.command("list")
def list_cmd():
    """Show configured project aliases."""
    cfg = load_config()
    table = Table("alias", "site", "kind", "project", "default type")
    for a, p in cfg.projects.items():
        table.add_row(a, p.site.name, p.site.kind, p.project, p.default_type)
    console.print(table)
    console.print(f"[dim]config: {cfg.path}[/]")


@app.command()
def projects(site: str = typer.Argument(..., help="Site name from config")):
    """List the project keys a site's token can see (to fill in config)."""
    sites = load_sites()
    if site not in sites:
        _die(f"Unknown site '{site}'. Configured: {', '.join(sites)}")
    table = Table("key", "name")
    for key, name in make_backend(sites[site]).list_projects():
        table.add_row(key, name)
    console.print(table)


@app.command()
def doctor():
    """Check Ollama and every site's credentials."""
    cfg = load_config()
    import httpx

    try:
        models = [
            m["name"]
            for m in httpx.get(f"{cfg.ollama.url}/api/tags", timeout=5).json()["models"]
        ]
        ok = cfg.ollama.model in models
        console.print(
            f"{'[green]✓' if ok else '[red]✗'}[/] ollama  {cfg.ollama.model}"
            + ("" if ok else f" not pulled (have: {', '.join(models)})")
        )
    except Exception as e:
        console.print(f"[red]✗[/] ollama  {e}")
    for name, site in load_sites().items():
        try:
            console.print(f"[green]✓[/] {name:16} {make_backend(site).check()}")
        except Exception as e:
            console.print(f"[red]✗[/] {name:16} {e}")


def _pick_alias(projects) -> str:
    aliases = list(projects)
    for i, a in enumerate(aliases, 1):
        p = projects[a]
        console.print(f"  [bold]{i}[/]) {a:8} [dim]{p.site.name} / {p.project}[/]")
    choice = Prompt.ask(
        "Project",
        choices=[str(i) for i in range(1, len(aliases) + 1)] + aliases,
        show_choices=False,
    )
    return aliases[int(choice) - 1] if choice.isdigit() else choice


def _show(draft: llm.Draft, project) -> None:
    body = draft.description
    if draft.acceptance_criteria:
        body += "\n\n---\n\n**Acceptance criteria**\n\n" + draft.acceptance_criteria
    console.print(
        Panel(
            Markdown(body),
            title=f"[bold]{draft.title}[/]",
            subtitle=f"{draft.issue_type} → {project.site.name} / {project.project}",
            expand=True,
        )
    )


def _edit(draft: llm.Draft) -> llm.Draft:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
        f.write(
            f"Type: {draft.issue_type}\nTitle: {draft.title}\n---\n{draft.description}\n"
        )
        if draft.acceptance_criteria:
            f.write(f"\n{AC_MARKER}\n{draft.acceptance_criteria}\n")
        path = f.name
    try:
        subprocess.run(f'{editor} "{path}"', shell=True, check=True)
        content = open(path).read()
    finally:
        os.unlink(path)
    head, _, body = content.partition("\n---\n")
    body, _, acceptance = body.partition(AC_MARKER)
    meta = dict(line.split(":", 1) for line in head.splitlines() if ":" in line)
    return llm.Draft(
        title=meta.get("Title", draft.title).strip(),
        issue_type=meta.get("Type", draft.issue_type).strip(),
        description=body.strip(),
        acceptance_criteria=acceptance.strip(),
    )


def _warn_duplicates(draft: llm.Draft, examples: list[Example]) -> None:
    title = draft.title.lower()
    for e in examples:
        if (
            difflib.SequenceMatcher(None, title, e.title.lower()).ratio()
            >= DUPLICATE_RATIO
        ):
            console.print(
                f"[yellow]⚠ Very similar to existing {e.issue_type} {e.ref}: {e.title}[/]"
            )


def _resolve_type(wanted: str, creatable: list[str]) -> str:
    by_lower = {t.lower(): t for t in creatable}
    key = wanted.strip().lower()
    if key in by_lower:
        return by_lower[key]
    for candidate in TYPE_SYNONYMS.get(key, []):
        if candidate.lower() in by_lower:
            return by_lower[candidate.lower()]
    prefixed = [t for t in creatable if t.lower().startswith(key)]
    if len(prefixed) == 1:
        return prefixed[0]
    _die(f"Unknown type '{wanted}'. Available: {', '.join(creatable)}")


def _ago(iso: str) -> str:
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:10]
    days = (datetime.now(then.tzinfo) - then).days
    if days < 1:
        return "today"
    if days < 30:
        return f"{days}d ago"
    return then.strftime("%Y-%m-%d")


def _parse_value(value: str):
    if value[0] in "{[":
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            pass
    return value


def _die(msg: str):
    console.print(f"[red]{msg}[/]")
    raise typer.Exit(2)


def main():
    # `tiket bf "text"` → `tiket new bf "text"`
    args = sys.argv[1:]
    if not args or (args[0] not in SUBCOMMANDS and args[0] not in ("--help", "-h")):
        sys.argv.insert(1, "new")
    try:
        app()
    except (ConfigError, RuntimeError, ValueError) as e:
        console.print(f"[red]{e}[/]")
        sys.exit(1)
