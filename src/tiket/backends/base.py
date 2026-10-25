import html
import re
from dataclasses import asdict, dataclass
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


class MissingFieldsError(RuntimeError):
    """The tracker rejected the ticket because required fields were empty."""

    def __init__(self, fields: list[tuple[str, str]], raw: str):
        self.fields = fields  # (field reference used in the API, human readable label / message)
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

    def my_items(self, project: str | None, text: str | None, include_done: bool, limit: int) -> list[Item]:
        """Tickets assigned to me, newest change first; project/text filters are applied server side."""

    def recent_examples(self, n: int, filter_types: bool = True) -> list[Example]:
        """Newest tickets; limited to project.types when set and filter_types is true."""

    def create(self, title: str, description_md: str, issue_type: str,
               acceptance_criteria_md: str = "", extra_fields: dict | None = None) -> str:
        """Create the ticket, return its browser URL."""


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
