import html
import re
from dataclasses import asdict, dataclass, field
from typing import Protocol


@dataclass
class Example:
    title: str
    issue_type: str
    description: str
    acceptance_criteria: str = ""
    ref: str = ""  # Jira key / ADO work item id

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Item:
    """A ticket assigned to the current user."""

    ref: str
    title: str
    issue_type: str
    status: str
    project: str
    updated: str  # ISO timestamp
    url: str
    done: bool = False


@dataclass
class Choice:
    value: str  # what the API needs: issue key, work item id, sprint id, iteration path…
    label: str  # what people and the model see
    count: int = 0  # how many of the user's recent tickets used it


@dataclass
class Profile:
    """How the current user usually fills in ticket details in a project, learned from their recent tickets."""

    sample: int = 0
    parents: list[Choice] = field(default_factory=list)
    components: list[Choice] = field(default_factory=list)
    labels: list[Choice] = field(default_factory=list)
    priorities: list[Choice] = field(default_factory=list)
    points: list[tuple[str, float]] = field(default_factory=list)  # (title, story points)
    area: str | None = None  # ADO area path
    sprint_hint: dict = field(default_factory=dict)  # where to look for the current sprint / iteration
    field_ids: dict = field(default_factory=dict)  # Jira custom field ids (story points, sprint, epic link)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        d = dict(d)
        for key in ("parents", "components", "labels", "priorities"):
            d[key] = [Choice(**c) for c in d.get(key, [])]
        d["points"] = [tuple(p) for p in d.get("points", [])]
        return cls(**d)


@dataclass
class Details:
    """Ticket details to set on creation; guessed from the Profile, editable before creating."""

    parent: Choice | None = None
    story_points: float | None = None
    priority: str | None = None
    components: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    sprint: Choice | None = None
    area: str | None = None


@dataclass
class Draft:
    title: str
    description: str
    issue_type: str
    acceptance_criteria: str = ""
    details: Details = field(default_factory=Details)


@dataclass
class Created:
    url: str
    warnings: list[str] = field(default_factory=list)  # details that could not be set


class MissingFieldsError(RuntimeError):
    """The tracker rejected the ticket because required fields were empty."""

    def __init__(self, fields: list[tuple[str, str]], raw: str):
        # (field reference used in the API, human readable label / message)
        self.fields = fields
        super().__init__(raw)


class Backend(Protocol):
    # True when the tracker has its own acceptance criteria field (ADO); otherwise AC goes in the description
    separate_acceptance_criteria: bool

    def check(self) -> str:
        """Return a short "logged in as ..." string, raise on failure."""

    def list_projects(self) -> list[tuple[str, str]]:
        """(key, name) pairs visible to the token."""

    def issue_types(self) -> list[str]:
        """Issue / work item type names that can be created in the project."""

    def my_items(
        self, project: str | None, text: str | None, include_done: bool, limit: int
    ) -> list[Item]:
        """Tickets assigned to me, newest change first; project/text filters are applied server side."""

    def recent_examples(self, n: int, filter_types: bool = True) -> list[Example]:
        """Newest tickets; limited to project.types when set and filter_types is true."""

    def field_profile(self) -> Profile:
        """Learn parents, components, labels, priorities, points etc. from the user's recent tickets."""

    def current_sprint(self, profile: Profile) -> Choice | None:
        """The sprint / iteration a new ticket should go into today (never cached)."""

    def create(
        self, draft: Draft, profile: Profile, extra_fields: dict | None = None
    ) -> Created:
        """Create the ticket with its details."""


def rank(values: list[tuple[str, str]]) -> list[Choice]:
    """(value, label) pairs, newest first -> Choices ordered by how often they were used (ties: newest)."""
    choices: dict[str, Choice] = {}
    for value, label in values:
        choices.setdefault(value, Choice(value, label)).count += 1
    return sorted(choices.values(), key=lambda c: -c.count)


def html_to_text(s: str | None) -> str:
    """Rough HTML -> markdown-ish text, good enough to show the LLM a ticket's structure."""
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)
    s = re.sub(r"(?i)<h([1-6])[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n- ", s)
    s = re.sub(r"(?i)</?(b|strong)>", "**", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|h[1-6]|tr|pre)>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+\n", "\n", s)
    return re.sub(r"\n{3,}", "\n\n", s).strip()
