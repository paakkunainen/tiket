# tiket

Write a one-line problem description and get a ticket in your team's own style, created in Jira (Cloud or Data Center) or Azure DevOps. The ticket text is generated locally by Ollama (`gpt-oss:20b`), using the project's ~12 most recent tickets as style examples. Nothing leaves your machine except the final ticket.

```
tiket <alias> "short problem description"   # preview → [y]es / [r]egenerate / [e]dit / [n]o
tiket                                        # pick project + type description interactively
tiket <alias> "..." --dry-run                # just show what would be created
tiket <alias> -t bug "..."                   # set the type yourself (bug / task / story / feature /
                                             # improvement, or the exact name); omit to let the model pick
tiket <alias> "..." --refresh                # re-fetch style examples
tiket tasks                                  # my open tickets on every board, grouped by project
                                             #   j/k g/G ctrl-d/u move · y yank "KEY Title" · Y yank url
                                             #   o/enter open in browser · / filter · esc clear · q quit
tiket tasks --plain                          # print a table instead (also automatic when piped)
tiket tasks -o updated                       # order by: project (default), updated, key, type, status
tiket tasks bf esko -s cdc                   # filter by alias / site / project key, and title text
tiket tasks digifinland -a                   # include done/closed tickets
tiket list | doctor | projects <site>
```

Setup: `uv tool install -e .`. The config lives in `config.toml`, which is symlinked to `~/.config/tiket/config.toml`, and secrets live in `.env` next to it. Style examples are cached for 24h in `~/.cache/tiket/`.
