---
name: tiket
description: Read, create and update Jira / Azure DevOps tickets with the `tiket` CLI. Use when the user mentions a ticket (BI-2314, OPHJODTA-12, an Azure DevOps id like 258694, or a ticket URL), asks what their tickets are, wants context from a ticket, or asks to create a ticket or add a comment / new information to one, e.g. after finishing work on it.
---

# tiket

`tiket` talks to the user's Jira sites and Azure DevOps board. Ticket text is written by a local
model in each team's own style and language. You supply the facts; tiket does the wording.

## Reading (safe, no confirmation needed)

```bash
tiket show BI-2314                  # whole ticket as markdown: fields, description, links, all comments
tiket show 258694                   # Azure DevOps id; ticket URLs work too
COLUMNS=200 tiket tasks --plain     # the user's open tickets on every board
COLUMNS=200 tiket tasks --plain bf  # …for one alias; add -a for done tickets, -s TEXT to search titles
tiket alias list                    # aliases: which alias is which site / project
```

Read the ticket before working on it, or before adding to it.

## Writing: always preview first, write only after the user says yes

Creating a ticket or changing one is visible to the user's team. Never write without the user
approving the exact text in this conversation.

1. **Preview and save the draft** (`-n` = never writes). Pass the information on stdin with `-`:

   ```bash
   tiket new <alias> - -n --save /tmp/tiket-draft.json <<'EOF'
   <what the ticket is about: the facts, in plain words>
   EOF

   tiket update BI-2314 - -n --save /tmp/tiket-update.json <<'EOF'
   <the new information>
   EOF
   ```

2. **Show the user the preview**: the title, type, description and details (for `new`), or the
   text and whether it goes in a comment or the description (for `update`). Then ask whether to write it.
3. **Change it if asked.** Edit the JSON file directly for small changes. For bigger ones, run
   step 1 again with better input, or with `-t bug` etc.
4. **Write exactly that draft** once the user approves:

   ```bash
   tiket new --from /tmp/tiket-draft.json -y --json      # → {"ref": "BI-2600", "url": …, "warnings": […]}
   tiket update --from /tmp/tiket-update.json -y --json  # → {"ref": …, "url": …, "target": "comment"}
   ```

   Report the ref and url, and any warnings (details tiket could not set).

Rules:
- Always write `tiket new <alias> …`, never the `tiket <alias> "…"` shorthand.
- Never add `-y` to a command without `--from`: that writes model output nobody has seen.
- Always give the text as an argument or `-`. Otherwise tiket waits for typed input and hangs.
- Don't run `tiket tasks` without `--plain` (interactive viewer) or `tiket config` (opens an editor).
- Generation takes 20–90 s on the local model; use a Bash timeout of at least 300000 ms.

## What to put in the input

- Facts only: what was done or found, error messages, file and table names, numbers, decisions.
  The model is told not to invent anything, so anything missing from your input stays missing.
- Long notes, lists and tables are fine; every item is kept.
- Any language works. The ticket comes out in the team's language (often Finnish).
- Don't write headings or a title for `new`; tiket follows the team's template.

## `new`: picking the alias and type

- Run `tiket alias list` to find the alias. If the repo's CLAUDE.md doesn't say which alias it
  uses, and it isn't obvious, ask the user.
- `-t bug|task|story|feature|improvement` sets the type; otherwise the model picks.
- tiket guesses details from the user's recent tickets: parent/epic, story points, priority,
  components, labels and the current sprint, with the user as assignee. Show them in the preview.
  `-D` turns the guessing off.

The draft JSON (`draft` key) has `title`, `description` (markdown), `issue_type`,
`acceptance_criteria`, and `details`: `parent` {value, label} or null, `story_points`,
`priority`, `components`, `labels`, `sprint` {value, label} or null, `area`. Keep `issue_type`
one the preview showed.

## `update`: comment or description

The model decides where the information goes and says why (`update.reason`):
- **comment**: progress, findings, decisions, questions.
- **description**: new or changed requirements or scope. It's added as a new section at the end
  of the description; the existing text is never rewritten.

Force one with `-c` (comment) or `-d` (description) when the user asked for it. To switch after
a preview, change `update.target` in the saved file. The text was written for the other target,
so regenerating with `-c` / `-d` is usually better.

## Exit codes

0 ok · 1 cancelled or a tracker / Ollama error (message on stderr) · 2 bad input (unknown
alias, ticket not found, …).
