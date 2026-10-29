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

from . import aliases
from . import details as ticket_details
from . import llm, style_cache
from .backends import Draft, Example, Item, MissingFieldsError, Profile, Ticket, make_backend
from .clipboard import copy_to_clipboard, paste_from_clipboard
from .config import ConfigError, Site, load_config, load_sites

app = typer.Typer(
    add_completion=False,
    no_args_is_help=False,
    rich_markup_mode="markdown",
    context_settings={"help_option_names": ["-h", "--help"]},
    help="""Create tickets in Jira / Azure DevOps from a short description or pasted notes, written by a local
model (Ollama) in each team's own style, and browse the tickets assigned to you.

`tiket <alias> "…"` is short for `tiket new <alias> "…"`. Run `tiket <command> -h` for details.""",
    epilog="""**Examples**

```
tiket bf "dashboard latautuu hitaasti koko vuoden aikavälillä"
tiket esko -t bug "kirjautuminen kaatuu"   # set the type yourself
tiket bf -p                                # long notes from the clipboard
tiket                                      # pick project, then describe
tiket tasks                                # browse your tickets, y = copy
tiket show TIJOFI-5173 -c                  # copy ticket + comments
tiket alias                                # manage project aliases
tiket doctor                               # check Ollama and credentials
```
""",
)
app.add_typer(aliases.app, name="alias")
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
SUBCOMMANDS = {"new", "list", "projects", "doctor", "tasks", "show", "alias", "config"}
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


@app.command(
    epilog="""**Examples**

```
tiket bf "power bi raportin päivitys epäonnistuu yöllä"
tiket tijo -t bug "aineistopäivitys kaatuu"    # bug → Virhe on TIJOFI
tiket esko "…" -n                              # preview only
tiket edu "…" -D                               # no guessed details
tiket edu "…" -r                               # re-read recent tickets
tiket bf -p                                    # clipboard as description
tiket bf "summary line" -p                     # short note + clipboard
pbpaste | tiket bf -                           # description from stdin
```
"""
)
def new(
    alias: str = typer.Argument(
        None, help="Project alias from config (omit for a picker)"
    ),
    text: list[str] = typer.Argument(
        None,
        help="Description of the problem: a few words or longer notes. `-` reads it from stdin; "
        "asked for if left out",
    ),
    issue_type: str = typer.Option(
        None,
        "--type",
        "-t",
        help="Issue type: bug, task, story, feature, improvement (or Finnish: virhe, tehtävä, "
        "tarina, parannus), or the exact type name. Omit to let the model choose.",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-n", help="Generate only, never create"
    ),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        "-r",
        help="Re-read recent tickets (style and detail history are cached for 24 h)",
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Create without asking"),
    paste: bool = typer.Option(
        False,
        "--paste",
        "-p",
        help="Use the clipboard as the description (after any text you give, e.g. a one-line summary)",
    ),
    no_details: bool = typer.Option(
        False,
        "--no-details",
        "-D",
        help="Don't guess parent, story points, priority, components, labels or sprint",
    ),
):
    """Create a ticket from a short description.

    The model reads the project's recent tickets and writes the title, description (and acceptance
    criteria on Azure DevOps) in the same language and style. It also fills in **details** guessed
    from your own recent tickets there: parent/epic, story points, priority, components, labels,
    the current sprint / iteration and area, with you as the assignee.

    You get a preview first: **y** create · **r** rewrite (optionally with feedback such as
    "shorter") · **e** edit everything in $EDITOR, where `none` clears a detail · **n** cancel.
    """
    cfg = load_config()
    if not cfg.projects:
        _die(f"No projects configured in {cfg.path}")

    if alias is None:
        alias = _pick_alias(cfg.projects)
    elif alias not in cfg.projects:
        _die(f"Unknown alias '{alias}'. Configured: {', '.join(cfg.projects)}")
    project = cfg.projects[alias]

    user_text = _read_description(text or [], paste)
    if not user_text:
        _die("Empty description")

    backend = make_backend(project.site, project)
    with console.status(f"Fetching recent tickets from {project.project}…"):
        examples = style_cache.get_examples(
            project, backend, cfg.ollama.examples, refresh=refresh
        )
        creatable = [t for t in backend.issue_types() if t not in IGNORED_TYPES]
        profile, sprint = Profile(), None
        if project.guess_details and not no_details:
            profile = style_cache.get_profile(project, backend, refresh=refresh)
            sprint = backend.current_sprint(profile)
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
                profile=profile,
                sprint=sprint,
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
                created = backend.create(draft, profile, extra)
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
    console.print(f"[bold green]Created:[/] {created.url}")
    for warning in created.warnings:
        console.print(f"[yellow]⚠ {warning}[/]")


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


@app.command(
    epilog="""**Keys** (interactive view)

```
j / k, arrows     move              y        copy "KEY Title"
g / G             first / last      Y        copy the URL
ctrl-d / ctrl-u   half page         c        copy full ticket as markdown
/                 filter            o, enter open in browser
q                 quit              esc      clear filter
```

**Examples**

```
tiket tasks                       # all open tickets, grouped by project
tiket tasks bf esko               # only these aliases
tiket tasks digifinland -a        # a whole site, including done tickets
tiket tasks -s snowflake -o updated
tiket tasks --plain | grep BI-    # plain table (automatic when piped)
```
"""
)
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
    """Browse the tickets assigned to you on every board.

    Opens an interactive list with vim keys; **y** copies "KEY Title" to the clipboard.
    """
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
            item.site = site
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
                    item.site,
                )
                for label, item in rows
            ],
            note=note,
            fetch=lambda row: _ticket_summary(
                make_backend(sites[row.site]).ticket(row.key)
            ),
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


@app.command(
    epilog="""**Examples**

```
tiket show TIJOFI-5173            # read it in the terminal
tiket show TIJOFI-5173 -c         # copy as markdown for an agent chat
tiket show 258694 > ticket.md     # piped = plain markdown
tiket show https://jira.eduuni.fi/browse/OPHJODTA-3292
tiket show "BI-123 Some title"    # what y in tiket tasks copies
tiket show BI-123 --json | jq .comments
```

An agent can run `tiket show <KEY>` itself to read a ticket.
"""
)
def show(
    ref: str = typer.Argument(
        ...,
        help="Jira key (BI-123), Azure DevOps id (258694) or the ticket's URL",
    ),
    where: str = typer.Option(
        None,
        "--in",
        "-i",
        help="Alias or site to look in; normally found from the key's project or by trying each site",
    ),
    copy: bool = typer.Option(
        False, "--copy", "-c", help="Copy the markdown to the clipboard instead of printing it"
    ),
    as_json: bool = typer.Option(False, "--json", help="Print JSON instead of markdown"),
    raw: bool = typer.Option(
        False, "--raw", "-r", help="Print plain markdown in the terminal too (no formatting)"
    ),
):
    """Show one ticket in full: fields, description, links, attachments and every comment.

    The output is markdown, handy as context for an AI agent: copy it with **-c**, or pipe it.
    """
    ticket = _find_ticket(ref, where)
    if as_json:
        print(json.dumps(ticket.to_dict(), ensure_ascii=False, indent=2))
        return
    text, summary = _ticket_summary(ticket)
    if copy:
        if not copy_to_clipboard(text):
            _die("No clipboard tool found (pbcopy / wl-copy / xclip); pipe the output instead.")
        console.print(f"[green]Copied[/] {summary}")
    elif raw or not sys.stdout.isatty():
        print(text, end="")
    else:
        console.print(Markdown(text))


def _ticket_summary(ticket: Ticket) -> tuple[str, str]:
    text = ticket.to_markdown()
    return text, f"{ticket.ref} {ticket.title} ({len(ticket.comments)} comments, {len(text):,} chars)"


def _find_ticket(ref: str, where: str | None) -> Ticket:
    """Resolve a key / id / URL to a ticket, trying the most likely sites first."""
    cfg = load_config()
    sites = load_sites()
    ref = ref.strip()
    candidates: list[Site] = []

    if re.match(r"https?://", ref):
        site = next(
            (s for s in sites.values() if ref.lower().startswith(s.url.lower().rstrip("/"))),
            None,
        )
        m = re.search(
            r"(?:/browse/|selectedIssue=)([A-Za-z][A-Za-z0-9_]*-\d+)|/(?:_workitems/edit|workitems)/(\d+)",
            ref,
            re.IGNORECASE,
        )
        if not site or not m:
            _die(f"Can't tell which configured site or ticket that URL points to: {ref}")
        ref, candidates = m[1] or m[2], [site]
    else:
        ref = ref.split()[0]  # "BI-123 Some title" → BI-123

    if where:
        if where in cfg.projects:
            candidates = [cfg.projects[where].site]
        elif where in sites:
            candidates = [sites[where]]
        else:
            _die(f"'{where}' is neither an alias nor a site. Aliases: {', '.join(cfg.projects)}")
    if not candidates:
        jira = ref.split("-")[0].lower() if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*-\d+", ref) else None
        if not jira and not ref.isdigit():
            _die(f"'{ref}' doesn't look like a Jira key (BI-123) or an Azure DevOps id (258694).")
        kinds = ("jira_cloud", "jira_dc") if jira else ("azure_devops",)
        # Sites with an alias for this project key first, then the other sites of the right kind
        preferred = [p.site for p in cfg.projects.values() if jira and p.project.lower() == jira]
        candidates = list(
            {s.name: s for s in preferred + [s for s in sites.values() if s.kind in kinds]}.values()
        )

    errors = []
    with console.status(f"Fetching {ref}…"):
        for site in candidates:
            try:
                return make_backend(site).ticket(ref)
            except LookupError:
                continue
            except RuntimeError as e:
                errors.append(f"{site.name}: {e}")
    _die("\n".join(errors) or f"{ref} not found on {', '.join(s.name for s in candidates)}.")


@app.command("list", hidden=True)
def list_cmd():
    """Show project aliases (same as `tiket alias`)."""
    aliases.list_aliases()


@app.command("config")
def config_cmd():
    """Open config.toml in your editor; it is validated when you close it."""
    aliases.edit_config()


@app.command()
def projects(site: str = typer.Argument(..., help="Site name from config")):
    """List the projects a site's token can see, e.g. before `tiket alias add`."""
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


def _read_description(text: list[str], paste: bool) -> str:
    """Join the words given, stdin (`-` or a pipe) and the clipboard (-p); prompt if all are empty."""
    parts = []
    if text == ["-"]:
        parts.append(_read_stdin())
    elif text:
        parts.append(" ".join(text))
    if paste:
        clip = paste_from_clipboard()
        if clip is None:
            _die("No clipboard tool found (pbpaste, wl-paste or xclip)")
        if not clip.strip():
            _die("The clipboard is empty")
        parts.append(clip)
    if not parts and not _stdin_is_tty():
        parts.append(_read_stdin())  # `pbpaste | tiket bf`
    if not parts:
        console.print(
            "[bold]Describe the problem[/] [dim](paste or type; end with Ctrl-D on an empty line)[/]"
        )
        parts.append(sys.stdin.read())
    return "\n\n".join(p.strip() for p in parts if p.strip())


def _stdin_is_tty() -> bool:
    return sys.stdin.isatty()


def _read_stdin() -> str:
    data = sys.stdin.read()
    # the y/r/e/n prompts come after a piped description: read them from the terminal
    if sys.stdin is sys.__stdin__ and not sys.stdin.isatty():
        try:
            sys.stdin = open("/dev/tty")
        except OSError:
            pass
    return data


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


def _show(draft: Draft, project) -> None:
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
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="dim", no_wrap=True)
    grid.add_column()
    for name, value in ticket_details.rows(draft.details, project.assign_to_me):
        grid.add_row(name, value)
    console.print(grid)


def _edit(draft: Draft) -> Draft:
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    with tempfile.NamedTemporaryFile("w+", suffix=".md", delete=False) as f:
        f.write(
            f"Type: {draft.issue_type}\nTitle: {draft.title}\n"
            f"{ticket_details.to_editor(draft.details)}\n---\n{draft.description}\n"
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
    meta = {
        k.strip(): v.strip()
        for k, v in (line.split(":", 1) for line in head.splitlines() if ":" in line)
    }
    return Draft(
        title=meta.get("Title", draft.title),
        issue_type=meta.get("Type", draft.issue_type),
        description=body.strip(),
        acceptance_criteria=acceptance.strip(),
        details=ticket_details.from_editor(meta, draft.details),
    )


def _warn_duplicates(draft: Draft, examples: list[Example]) -> None:
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
    # `tiket bf "text"` → `tiket new bf "text"`; bare `tiket -h` stays the overview
    args = sys.argv[1:]
    if not args or (args[0] not in SUBCOMMANDS and args[0] not in ("--help", "-h")):
        sys.argv.insert(1, "new")
    try:
        app()
    except (ConfigError, RuntimeError, ValueError) as e:
        console.print(f"[red]{e}[/]")
        sys.exit(1)
