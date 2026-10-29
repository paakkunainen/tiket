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
    site: str = ""  # config site name, filled in by the caller


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


@dataclass
class Comment:
    author: str
    created: str  # ISO timestamp
    body: str  # markdown-ish text


@dataclass
class Ticket:
    """Everything worth knowing about one ticket, e.g. as context for an agent."""

    ref: str
    title: str
    url: str
    issue_type: str
    status: str
    project: str
    created: str = ""
    updated: str = ""
    fields: dict[str, str] = field(default_factory=dict)  # assignee, priority, sprint, parent… (shown when set)
    description: str = ""
    acceptance_criteria: str = ""
    links: list[str] = field(default_factory=list)  # "blocks BI-12 Title", "child 1234 Title"
    attachments: list[str] = field(default_factory=list)  # "name (url)"
    comments: list[Comment] = field(default_factory=list)  # oldest first

    def to_dict(self) -> dict:
        return asdict(self)

    def to_markdown(self) -> str:
        out = [f"# {self.ref}: {self.title}", ""]
        meta = {
            "URL": self.url,
            "Project": self.project,
            "Type": self.issue_type,
            "Status": self.status,
            **self.fields,
            "Created": self.created[:16].replace("T", " "),
            "Updated": self.updated[:16].replace("T", " "),
        }
        out += [f"- **{k}:** {v}" for k, v in meta.items() if v]
        out += ["", "## Description", "", _demote(self.description, 2) or "_(empty)_"]
        if self.acceptance_criteria:
            out += ["", "## Acceptance criteria", "", _demote(self.acceptance_criteria, 2)]
        if self.links:
            out += ["", "## Linked tickets", ""] + [f"- {link}" for link in self.links]
        if self.attachments:
            out += ["", "## Attachments", ""] + [f"- {a}" for a in self.attachments]
        out += ["", f"## Comments ({len(self.comments)})"]
        for c in self.comments:
            out += ["", f"### {c.author}, {c.created[:16].replace('T', ' ')}", "", _demote(c.body, 3) or "_(empty)_"]
        return "\n".join(out).rstrip() + "\n"


def _demote(md: str, levels: int) -> str:
    """Push headings inside ticket text below the document's own ## sections."""
    out, in_code = [], False
    for line in md.splitlines():
        if line.startswith("```"):
            in_code = not in_code
        elif not in_code and re.match(r"#{1,6} ", line):
            line = "#" * min(6, line.index(" ") + levels) + line[line.index(" "):]
        out.append(line)
    return "\n".join(out)


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

    def ticket(self, ref: str) -> Ticket:
        """Full ticket with description, fields, links and all comments; raises LookupError if not found."""


def rank(values: list[tuple[str, str]]) -> list[Choice]:
    """(value, label) pairs, newest first -> Choices ordered by how often they were used (ties: newest)."""
    choices: dict[str, Choice] = {}
    for value, label in values:
        choices.setdefault(value, Choice(value, label)).count += 1
    return sorted(choices.values(), key=lambda c: -c.count)


def _code_block(m: re.Match) -> str:
    return "\n```\n" + re.sub(r"<[^>]+>", "", m[1]).strip("\n") + "\n```\n"


def _link(m: re.Match) -> str:
    href, text = m[1], re.sub(r"<[^>]+>", "", m[2])
    # Mentions, issue keys and anchors read fine as plain text
    if href.startswith("#") or href == text or re.search(r"user-hover|issue-link|mention", m[0]):
        return text
    return f"[{text}]({href})"


def html_to_text(s: str | None) -> str:
    """Rough HTML -> markdown-ish text, good enough to show the LLM a ticket's structure."""
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style).*?</\1>", "", s)
    s = re.sub(r"(?is)<pre[^>]*>(.*?)</pre>", _code_block, s)
    s = re.sub(r"(?is)<code[^>]*>(.*?)</code>", r"`\1`", s)
    s = re.sub(r'(?is)<a [^>]*href="([^"]+)"[^>]*>(.*?)</a>', _link, s)
    s = re.sub(r"(?i)</t[dh]>\s*<t[dh][^>]*>", " | ", s)
    s = re.sub(r"(?i)<h([1-6])[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n- ", s)
    s = re.sub(r"(?i)</?(b|strong)>", "**", s)
    s = re.sub(r"(?i)</(ul|ol|table)>", "\n\n", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|h[1-6]|tr|pre)>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    # <li><p>…</p></li> and table rows come out double spaced; keep lists and tables tight
    return re.sub(r"(?m)^((?:- | *\S.* \| ).*)\n\n(?=- |\S.* \| )", r"\1\n", s)
