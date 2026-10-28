"""Guessing ticket details (parent, story points, priority, components, labels, sprint, area).

The backend learns a Profile from the user's recent tickets; the model then picks values for the new
ticket from those candidates only, so it can't invent epics or labels that don't exist."""

from .backends import Choice, Details, Profile

NONE = "none"
MAX_PARENTS = 8
MAX_POINT_EXAMPLES = 12


def _fmt_points(v: float) -> str:
    return f"{v:g}"


def point_values(profile: Profile) -> list[str]:
    return [_fmt_points(v) for v in sorted({v for _, v in profile.points})]


def prompt_section(profile: Profile) -> str:
    if not profile.sample:
        return ""
    n = profile.sample
    lines = [
        "=== Ticket details ===",
        f"Also choose the ticket details. The candidates come from this user's {n} most recent tickets in",
        "this project, with how many of them used each value. Use the most common value unless the",
        "ticket content clearly fits another candidate better.",
    ]
    if profile.parents:
        lines.append('- parent (epic / parent issue), or "none":')
        lines += [f"    {c.label}  ({c.count}/{n})" for c in profile.parents[:MAX_PARENTS]]
    if profile.points:
        lines.append(f"- story_points, one of {', '.join(point_values(profile))} or \"none\". Recent tickets:")
        lines += [
            f"    {_fmt_points(v)} points: {title}"
            for title, v in profile.points[:MAX_POINT_EXAMPLES]
        ]
    if profile.priorities:
        counts = ", ".join(f"{c.label} ({c.count}/{n})" for c in profile.priorities)
        lines.append(
            f"- priority, one of: {counts}. Use {profile.priorities[0].label} unless the description "
            "signals urgency or low importance."
        )
    if profile.components:
        counts = ", ".join(f"{c.label} ({c.count}/{n})" for c in profile.components)
        lines.append(f"- components (zero or more): {counts}")
    if profile.labels:
        counts = ", ".join(f"{c.label} ({c.count}/{n})" for c in profile.labels)
        lines.append(
            f"- labels (zero or more): {counts}. Only add a label when the ticket is clearly about it."
        )
    return "\n".join(lines) + "\n"


def schema_properties(profile: Profile) -> dict:
    props: dict = {}
    if not profile.sample:
        return props
    if profile.parents:
        props["parent"] = {"type": "string", "enum": [c.label for c in profile.parents[:MAX_PARENTS]] + [NONE]}
    if profile.points:
        props["story_points"] = {"type": "string", "enum": point_values(profile) + [NONE]}
    if profile.priorities:
        props["priority"] = {"type": "string", "enum": [c.label for c in profile.priorities]}
    if profile.components:
        props["components"] = {"type": "array", "items": {"type": "string", "enum": [c.label for c in profile.components]}}
    if profile.labels:
        props["labels"] = {"type": "array", "items": {"type": "string", "enum": [c.label for c in profile.labels]}}
    return props


def from_model(data: dict, profile: Profile, sprint: Choice | None) -> Details:
    """Map the model's answer back to real values; anything off-list falls back to the usual value."""

    def most_common(choices: list[Choice]) -> Choice | None:
        return choices[0] if choices else None

    parent = most_common(profile.parents)
    if (label := data.get("parent")) == NONE:
        parent = None
    elif chosen := next((c for c in profile.parents if c.label == label), None):
        parent = chosen

    points = None
    if profile.points:
        raw = data.get("story_points")
        points = float(raw) if raw in point_values(profile) else None

    priority = most_common(profile.priorities)
    priority_label = data.get("priority") if data.get("priority") in {c.label for c in profile.priorities} else None

    def subset(key: str, choices: list[Choice]) -> list[str]:
        allowed = {c.label for c in choices}
        return [v for v in dict.fromkeys(data.get(key) or []) if v in allowed]

    return Details(
        parent=parent,
        story_points=points,
        priority=priority_label or (priority.value if priority else None),
        components=subset("components", profile.components),
        labels=subset("labels", profile.labels),
        sprint=sprint,
        area=profile.area,
    )


def to_model(details: Details) -> dict:
    """The details as the model chose them, for the regenerate conversation."""
    return {
        "parent": details.parent.label if details.parent else NONE,
        "story_points": _fmt_points(details.story_points) if details.story_points is not None else NONE,
        "priority": details.priority,
        "components": details.components,
        "labels": details.labels,
    }


def rows(details: Details, assign_to_me: bool) -> list[tuple[str, str]]:
    """(name, value) pairs for the preview."""
    out = [
        ("Parent", details.parent.label if details.parent else "—"),
        ("Story points", _fmt_points(details.story_points) if details.story_points is not None else "—"),
        ("Priority", details.priority or "—"),
        ("Components", ", ".join(details.components) or "—"),
        ("Labels", ", ".join(details.labels) or "—"),
        ("Sprint", details.sprint.label if details.sprint else "—"),
    ]
    if details.area:
        out.append(("Area", details.area))
    out.append(("Assignee", "me" if assign_to_me else "—"))
    return out


# --- $EDITOR round trip ------------------------------------------------------

def to_editor(details: Details) -> str:
    sprint = details.sprint.value if details.sprint and "\\" in details.sprint.value else (
        f"{details.sprint.value} {details.sprint.label}" if details.sprint else NONE
    )
    return "\n".join([
        f"Parent: {details.parent.label if details.parent else NONE}",
        f"Story points: {_fmt_points(details.story_points) if details.story_points is not None else NONE}",
        f"Priority: {details.priority or NONE}",
        f"Components: {', '.join(details.components)}",
        f"Labels: {', '.join(details.labels)}",
        f"Sprint: {sprint}",
        *([f"Area: {details.area}"] if details.area else []),
    ])


def from_editor(meta: dict[str, str], old: Details) -> Details:
    """Parse the header lines back. Parent and sprint keep their id (first word, or the whole
    iteration path for Azure DevOps), so any existing parent key / sprint id can be typed in."""

    def get(name: str) -> str | None:
        value = meta.get(name)
        return None if value is None else value.strip()

    def is_none(v: str | None) -> bool:
        return v is not None and v.lower() in ("", NONE, "-", "—")

    def split(v: str | None, fallback: list[str]) -> list[str]:
        return fallback if v is None else [x.strip() for x in v.split(",") if x.strip()]

    def choice(v: str | None, current: Choice | None) -> Choice | None:
        if v is None:
            return current
        if is_none(v):
            return None
        if current and v in (current.label, current.value, f"{current.value} {current.label}"):
            return current
        if "\\" in v:  # Azure DevOps iteration path
            return Choice(v, v.split("\\")[-1])
        return Choice(v.split()[0], v)

    points = get("Story points")
    return Details(
        parent=choice(get("Parent"), old.parent),
        story_points=old.story_points if points is None else (None if is_none(points) else float(points)),
        priority=old.priority if get("Priority") is None else (None if is_none(get("Priority")) else get("Priority")),
        components=split(get("Components"), old.components),
        labels=split(get("Labels"), old.labels),
        sprint=choice(get("Sprint"), old.sprint),
        area=old.area if get("Area") is None else (None if is_none(get("Area")) else get("Area")),
    )
