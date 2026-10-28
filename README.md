# tiket

Write a one-line problem description and get a ticket in your team's own style, created in Jira (Cloud or Data Center) or Azure DevOps. The ticket text is generated locally by Ollama (`gpt-oss:20b`), using the project's ~12 most recent tickets as style examples. Nothing leaves your machine except the final ticket.

```
tiket <alias> "short problem description"   # preview → [y]es / [r]egenerate / [e]dit / [n]o
tiket                                        # pick project + type description interactively
tiket <alias> "..." --dry-run                # just show what would be created
tiket <alias> -t bug "..."                   # set the type yourself (bug / task / story / feature /
                                             # improvement, or the exact name); omit to let the model pick
tiket <alias> "..." --refresh                # re-fetch style examples and detail history
tiket <alias> "..." -D                       # don't guess details (parent, points, sprint, ...)
tiket tasks                                  # my open tickets on every board, grouped by project
                                             #   j/k g/G ctrl-d/u move · y yank "KEY Title" · Y yank url
                                             #   o/enter open in browser · / filter · esc clear · q quit
tiket tasks --plain                          # print a table instead (also automatic when piped)
tiket tasks -o updated                       # order by: project (default), updated, key, type, status
tiket tasks bf esko -s cdc                   # filter by alias / site / project key, and title text
tiket tasks digifinland -a                   # include done/closed tickets
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
