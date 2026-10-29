# tiket

Write a short problem description (or paste longer notes) and get a ticket in your team's own style, created in Jira (Cloud or Data Center) or Azure DevOps. The ticket text is generated locally by Ollama (`gpt-oss:20b`), using the project's ~12 most recent tickets as style examples. Nothing leaves your machine except the final ticket.

```
tiket <alias> "short problem description"   # preview → [y]es / [r]egenerate / [e]dit / [n]o
tiket                                        # pick project, then type or paste (Ctrl-D ends)
tiket <alias> -p                             # description = clipboard (multi-line notes, tables)
tiket <alias> "summary" -p                   # short note + clipboard
pbpaste | tiket <alias> -                    # description from stdin
tiket <alias> "..." --dry-run                # just show what would be created
tiket <alias> -t bug "..."                   # set the type yourself (bug / task / story / feature /
                                             # improvement, or the exact name); omit to let the model pick
tiket <alias> "..." --refresh                # re-fetch style examples and detail history
tiket <alias> "..." -D                       # don't guess details (parent, points, sprint, ...)
tiket tasks                                  # my open tickets on every board, grouped by project
                                             #   j/k g/G ctrl-d/u move · y yank "KEY Title" · Y yank url
                                             #   c copy full ticket + comments as markdown
                                             #   o/enter open in browser · / filter · esc clear · q quit
tiket tasks --plain                          # print a table instead (also automatic when piped)
tiket tasks -o updated                       # order by: project (default), updated, key, type, status
tiket tasks bf esko -s cdc                   # filter by alias / site / project key, and title text
tiket tasks digifinland -a                   # include done/closed tickets
tiket show TIJOFI-5173                       # one ticket in full: fields, description, links, comments
tiket show TIJOFI-5173 -c                    # …copied as markdown, e.g. as context for an agent
tiket show 258694 > ticket.md                # piped = plain markdown; --json for JSON; URLs work too
tiket update BI-2314 "new info"              # add to a ticket: the model writes a comment or a
                                             # section appended to the description (-c / -d force)
tiket update BI-2314 -p                      # …from the clipboard; preview y/r/s(witch)/e/n
tiket alias                                  # list aliases; add / rename / set / remove (see -h)
tiket alias add jod eduuni OPHJOD            # checks the project exists, shows its issue types
tiket alias rename tijo omaolo
tiket alias set laak default_type=Virhe types=Tehtävä,Virhe
tiket config                                 # edit config.toml in $EDITOR, validated on save
tiket doctor | projects <site>               # check credentials / list a site's projects
tiket -h, tiket <command> -h                 # help with examples
```

Setup: `uv tool install -e .`. The config lives in `config.toml`, which is symlinked to `~/.config/tiket/config.toml`, and secrets live in `.env` next to it. Style examples are cached for 24h in `~/.cache/tiket/`.

## Ticket details

Besides the text, `tiket` fills in the parent/epic, story points, priority, components, labels, the current sprint (Azure DevOps: iteration and area) and makes you the assignee. The candidates come from your own ~50 most recent tickets in that project; the model picks among them based on the ticket's content (closed parents are skipped). The current sprint is looked up on every run: the active sprint on the board you last used, otherwise the next one. Everything is shown in the preview and can be changed with `e`. Any parent key or sprint id can be typed there, and `none` clears a field. If the tracker rejects a guessed detail, the ticket is still created and you get a warning.

## Ticket context for agents

`tiket show <KEY|id|url>` collects one ticket in a single markdown document: the main fields (assignee, priority, parent, points, sprint, labels…), the description, acceptance criteria, linked tickets, subtasks and epic children, attachment links and every comment, oldest first. The site is found from the key's project (aliases first, then every Jira site; numeric ids go to Azure DevOps), or set it with `-i <alias|site>`. In `tiket tasks`, `c` copies the same document for the selected row. An agent can also run `tiket show KEY` itself.

## Adding to an existing ticket

`tiket update <key|id|url> "…"` (or `-p` for the clipboard) reads the ticket and its comments, and the model writes the new information in the ticket's language and style. It decides where it belongs and says why:

- **comment**: progress, findings, decisions, questions, status.
- **description**: new or corrected requirements, scope, specifications, reference tables. This is added as a new section at the **end** of the description. The existing text is never rewritten: Jira gets back its original wiki markup exactly, and Azure DevOps refuses the save if someone else edited the item in the meantime.

Press `s` in the preview to switch between the two, or force one with `-c` / `-d`.

## Tests

```
uv run pytest
```

The suite never talks to a real tracker or Ollama: HTTP is mocked with respx, CLI tests use fake backends and a fake model, config and cache live in a temp dir, and opening a network socket fails the test.
