import json
import re

import httpx

from . import details as ticket_details
from .backends import Choice, Draft, Example, Profile
from .config import OllamaSettings, Project

EXAMPLE_CHARS = 800

SYSTEM = """You write tickets for the issue tracker project "{project}".
Turn the user's short problem description into a ticket that looks like it was written by this team.

Existing tickets from this project, grouped by issue type:

{examples}

=== How to write the new ticket ===
- Write the title and description in {language}, even if the user writes in another language.
- First pick the issue_type, then copy the style of THAT type's examples only (different types use
  different templates; e.g. a Task should not get a Bug's steps/expected/actual sections): title style and length, tone, prefixes/tags, section headings
  (e.g. preconditions / steps / expected / actual) and how detailed they are.
- Use ONLY facts from the user's description. Never invent symptoms, causes, browsers, environments,
  URLs, versions, names or numbers. Where a section the team always has would need unknown facts,
  put one short "TODO:" line in it. Use at most 2 TODOs and skip sections you have nothing for.
- Description in Markdown (it is converted to the tracker's format afterwards).
- issue_type must be one of: {types}.
{ac_rule}{notes}
{details}"""

AC_RULE = """- acceptance_criteria: this tracker has a separate Acceptance Criteria field. Fill it (Markdown bullet
  list, same language and style as the examples' acceptance criteria) for every type except Bug; keep it to
  what the user's description actually implies. Do not repeat it in the description.
"""


def _format_examples(examples: list[Example]) -> str:
    if not examples:
        return "(no existing tickets found — use a clear, conventional ticket style)"
    by_type: dict[str, list[Example]] = {}
    for e in examples:
        by_type.setdefault(e.issue_type, []).append(e)
    parts = []
    for issue_type, group in by_type.items():
        parts.append(f"##### {issue_type} tickets #####")
        for e in group:
            desc = e.description.strip() or "(empty description)"
            if len(desc) > EXAMPLE_CHARS:
                desc = desc[:EXAMPLE_CHARS].rstrip() + " …"
            part = f"--- {issue_type} ---\nTitle: {e.title}\nDescription:\n{desc}"
            if e.acceptance_criteria.strip():
                part += f"\nAcceptance criteria:\n{e.acceptance_criteria.strip()[: EXAMPLE_CHARS // 2]}"
            parts.append(part)
    return "\n\n".join(parts)


def detect_language(examples: list[Example]) -> str:
    text = " ".join(f"{e.title} {e.description}" for e in examples).lower()
    words = re.findall(r"\w+", text)
    if not words:
        return "English"
    finnish = sum(
        1
        for w in words
        if re.search(r"[äö]", w) or w in {"ja", "on", "ei", "kun", "että", "tai"}
    )
    return "Finnish" if finnish / len(words) > 0.05 else "English"


def generate(
    settings: OllamaSettings,
    project: Project,
    examples: list[Example],
    types: list[str],
    user_text: str,
    previous: Draft | None = None,
    feedback: str | None = None,
    ac_field: bool = False,
    profile: Profile | None = None,
    sprint: Choice | None = None,
) -> Draft:
    profile = profile or Profile()
    language = project.language or settings.language
    if language == "auto":
        language = detect_language(examples)
    notes = (
        f"\nTeam style notes: {project.style_notes}\n" if project.style_notes else ""
    )
    messages = [
        {
            "role": "system",
            "content": SYSTEM.format(
                project=project.project,
                language=language,
                types=", ".join(types),
                notes=notes,
                examples=_format_examples(examples),
                ac_rule=AC_RULE if ac_field else "",
                details=ticket_details.prompt_section(profile),
            ),
        },
        {
            "role": "user",
            "content": (
                f"Problem description:\n{user_text}\n\n"
                f"Write the ticket in {language}, in this team's style, using only the facts above."
            ),
        },
    ]
    if previous:
        messages.append(
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "title": previous.title,
                        "description": previous.description,
                        "issue_type": previous.issue_type,
                        "acceptance_criteria": previous.acceptance_criteria,
                        **ticket_details.to_model(previous.details),
                    },
                    ensure_ascii=False,
                ),
            }
        )
        messages.append(
            {
                "role": "user",
                "content": "Rewrite the ticket. "
                + (feedback or "Try a different wording."),
            }
        )

    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "description": {"type": "string"},
            "issue_type": {"type": "string", "enum": types},
        },
        "required": ["title", "description", "issue_type"],
    }
    if ac_field:
        schema["properties"]["acceptance_criteria"] = {"type": "string"}
        schema["required"].append("acceptance_criteria")
    detail_props = ticket_details.schema_properties(profile)
    schema["properties"] |= detail_props
    schema["required"] += list(detail_props)
    payload = {
        "model": settings.model,
        "messages": messages,
        "stream": False,
        "format": schema,
        "options": {"temperature": 0.3 if not previous else 0.6, "num_ctx": 16384},
    }
    if settings.think:
        payload["think"] = settings.think

    try:
        r = httpx.post(
            f"{settings.url.rstrip('/')}/api/chat", json=payload, timeout=300
        )
    except httpx.ConnectError:
        raise RuntimeError(
            f"Cannot reach Ollama at {settings.url} — is `ollama serve` running?"
        )
    if r.is_error:
        raise RuntimeError(f"Ollama error {r.status_code}: {r.text[:500]}")
    content = r.json()["message"]["content"]
    data = _parse_json(content)
    issue_type = data.get("issue_type") if data.get("issue_type") in types else types[0]
    return Draft(
        title=data["title"].strip(),
        description=data["description"].strip(),
        issue_type=issue_type,
        acceptance_criteria=(data.get("acceptance_criteria") or "").strip(),
        details=ticket_details.from_model(data, profile, sprint),
    )


def _parse_json(content: str) -> dict:
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        if m := re.search(r"\{.*\}", content, re.S):
            return json.loads(m.group(0))
        raise RuntimeError(f"Model did not return JSON:\n{content[:500]}")
