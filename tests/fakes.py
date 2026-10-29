"""In-memory stand-ins for a tracker backend and the model, for CLI tests."""

from dataclasses import dataclass, field

from tiket.backends import Choice, Created, Draft, Example, Item, MissingFieldsError, Profile, Ticket


@dataclass
class FakeBackend:
    site: object = None
    project: object = None
    separate_acceptance_criteria: bool = False
    types: list[str] = field(default_factory=lambda: ["Task", "Bug", "Epic", "Sub-task"])
    examples: list[Example] = field(default_factory=lambda: [Example("Existing ticket", "Task", "d", ref="BI-1")])
    profile: Profile = field(default_factory=lambda: Profile(sample=1, parents=[Choice("BI-890", "BI-890 AI", 1)]))
    sprint: Choice | None = field(default_factory=lambda: Choice("5", "S5 (active)"))
    items: list[Item] = field(default_factory=list)
    tickets: dict[str, Ticket] = field(default_factory=dict)
    projects: list[tuple[str, str]] = field(default_factory=lambda: [("BI", "Business"), ("OPH", "Opetus")])
    required: list[tuple[str, str]] = field(default_factory=list)  # fields create() demands once
    fail: Exception | None = None
    created: list[tuple[Draft, dict]] = field(default_factory=list)
    calls: list = field(default_factory=list)

    def check(self):
        if self.fail:
            raise self.fail
        return "me@test"

    def list_projects(self):
        return self.projects

    def issue_types(self):
        if self.fail:
            raise self.fail
        return self.types

    def my_items(self, project, text, include_done, limit):
        self.calls.append(("my_items", project, text, include_done, limit))
        if self.fail:
            raise self.fail
        return [i for i in self.items if (not project or i.project == project)]

    def recent_examples(self, n, filter_types=True):
        self.calls.append(("examples", n))
        return self.examples

    def field_profile(self):
        self.calls.append(("profile",))
        return self.profile

    def current_sprint(self, profile):
        self.calls.append(("sprint",))
        return self.sprint

    def create(self, draft, profile, extra_fields=None):
        extra = dict(extra_fields or {})
        missing = [(ref, label) for ref, label in self.required if ref not in extra]
        if missing:
            raise MissingFieldsError(missing, "required")
        self.created.append((draft, extra))
        return Created("https://tracker.test/BI-100", ["Could not set x"])

    def ticket(self, ref):
        self.calls.append(("ticket", ref))
        if self.fail:
            raise self.fail
        if ref not in self.tickets:
            raise LookupError(f"{ref} not found")
        return self.tickets[ref]


class FakeModel:
    """Replaces llm.generate; returns the queued drafts in order (the last one repeats)."""

    def __init__(self, *drafts: Draft):
        self.drafts = list(drafts) or [Draft("Generated title", "Generated **body**", "Task")]
        self.calls: list[dict] = []

    def __call__(self, settings, project, examples, types, user_text, previous=None, feedback=None, **kw):
        self.calls.append(dict(project=project, examples=examples, types=types, user_text=user_text,
                               previous=previous, feedback=feedback, **kw))
        return self.drafts[min(len(self.calls), len(self.drafts)) - 1]
