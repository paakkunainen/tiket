"""Guessing ticket details: prompt, schema, mapping the model's answer back, editor round trip."""

import pytest

from tiket import details
from tiket.backends import Choice, Details, Profile


@pytest.fixture
def profile() -> Profile:
    return Profile(
        sample=10,
        parents=[Choice("BI-890", "BI-890 Toiminto: AI", 6), Choice("BI-12", "BI-12 Raportointi", 2)],
        components=[Choice("@AI", "@AI", 5), Choice("UI", "UI", 1)],
        labels=[Choice("infra", "infra", 2)],
        priorities=[Choice("Medium", "Medium", 9), Choice("High", "High", 1)],
        points=[("Small thing", 1.0), ("Big thing", 5.0), ("Mid thing", 2.0), ("Other", 2.0)],
    )


def test_prompt_section_lists_candidates_with_counts(profile):
    text = details.prompt_section(profile)
    assert "10 most recent tickets" in text
    assert "BI-890 Toiminto: AI  (6/10)" in text
    assert 'story_points, one of 1, 2, 5 or "none"' in text
    assert "5 points: Big thing" in text
    assert "Medium (9/10), High (1/10)" in text
    assert "Use Medium unless" in text
    assert "@AI (5/10), UI (1/10)" in text
    assert "infra (2/10)" in text


def test_prompt_section_empty_without_history():
    assert details.prompt_section(Profile()) == ""
    assert details.schema_properties(Profile()) == {}


def test_prompt_section_caps_parents():
    many = Profile(sample=20, parents=[Choice(f"P-{i}", f"P-{i} x", 1) for i in range(20)])
    text = details.prompt_section(many)
    assert "P-7 x" in text and "P-8 x" not in text


def test_schema_properties_are_enums(profile):
    props = details.schema_properties(profile)
    assert props["parent"]["enum"] == ["BI-890 Toiminto: AI", "BI-12 Raportointi", "none"]
    assert props["story_points"]["enum"] == ["1", "2", "5", "none"]
    assert props["priority"]["enum"] == ["Medium", "High"]  # no "none": always a priority
    assert props["components"]["items"]["enum"] == ["@AI", "UI"]
    assert props["labels"]["items"]["enum"] == ["infra"]


def test_from_model_maps_labels_back_to_values(profile):
    sprint = Choice("55", "Sprint 5 (active)")
    d = details.from_model(
        {
            "parent": "BI-12 Raportointi",
            "story_points": "2",
            "priority": "High",
            "components": ["UI", "UI", "made-up"],
            "labels": ["infra"],
        },
        profile,
        sprint,
    )
    assert d.parent.value == "BI-12"
    assert d.story_points == 2.0
    assert d.priority == "High"
    assert d.components == ["UI"]  # deduplicated, unknown dropped
    assert d.labels == ["infra"]
    assert d.sprint is sprint


def test_from_model_falls_back_to_most_common(profile):
    d = details.from_model({"parent": "invented", "story_points": "13", "priority": "Blocker"}, profile, None)
    assert d.parent.value == "BI-890"
    assert d.story_points is None
    assert d.priority == "Medium"
    assert d.components == [] and d.labels == []


def test_from_model_none_clears_parent(profile):
    assert details.from_model({"parent": "none"}, profile, None).parent is None


def test_from_model_keeps_area():
    assert details.from_model({}, Profile(sample=1, area="Esko\\Team"), None).area == "Esko\\Team"


def test_to_model_round_trip(profile):
    d = Details(parent=profile.parents[0], story_points=2.0, priority="Medium", components=["@AI"], labels=[])
    assert details.to_model(d) == {
        "parent": "BI-890 Toiminto: AI",
        "story_points": "2",
        "priority": "Medium",
        "components": ["@AI"],
        "labels": [],
    }
    assert details.to_model(Details())["parent"] == "none"
    assert details.to_model(Details())["story_points"] == "none"


def test_rows_for_preview():
    d = Details(
        parent=Choice("1", "1 Epic"),
        story_points=0.5,
        priority="2",
        components=["a", "b"],
        sprint=Choice("x", "S1 (active)"),
        area="Esko\\T",
    )
    assert details.rows(d, assign_to_me=True) == [
        ("Parent", "1 Epic"),
        ("Story points", "0.5"),
        ("Priority", "2"),
        ("Components", "a, b"),
        ("Labels", "—"),
        ("Sprint", "S1 (active)"),
        ("Area", "Esko\\T"),
        ("Assignee", "me"),
    ]
    rows = dict(details.rows(Details(), assign_to_me=False))
    assert rows["Parent"] == "—" and rows["Assignee"] == "—" and "Area" not in rows


def _meta(text: str) -> dict[str, str]:
    return {k.strip(): v.strip() for k, v in (line.split(":", 1) for line in text.splitlines())}


def test_editor_round_trip_unchanged():
    d = Details(
        parent=Choice("BI-890", "BI-890 Toiminto: AI"),
        story_points=3.0,
        priority="Medium",
        components=["@AI", "UI"],
        labels=["x"],
        sprint=Choice("5609", "BF Sprint 154 (active)"),
    )
    text = details.to_editor(d)
    assert "Sprint: 5609 BF Sprint 154 (active)" in text
    assert details.from_editor(_meta(text), d) == d


def test_editor_round_trip_ado_iteration_path():
    d = Details(sprint=Choice("Esko\\1 TK\\Ink 42\\P134", "P134 (active)"), area="Esko\\1 TK")
    text = details.to_editor(d)
    assert "Sprint: Esko\\1 TK\\Ink 42\\P134" in text
    assert "Area: Esko\\1 TK" in text
    assert details.from_editor(_meta(text), d) == d


def test_editor_changes():
    old = Details(
        parent=Choice("BI-890", "BI-890 Toiminto: AI"),
        story_points=3.0,
        priority="Medium",
        components=["@AI"],
        labels=["x"],
        sprint=Choice("5609", "S154"),
        area="A",
    )
    new = details.from_editor(
        {
            "Parent": "BI-999",
            "Story points": "none",
            "Priority": "",
            "Components": "UI, Backend ,",
            "Labels": "",
            "Sprint": "Esko\\Ink\\P135",
            "Area": "—",
        },
        old,
    )
    assert new.parent == Choice("BI-999", "BI-999")  # any typed key works
    assert new.story_points is None
    assert new.priority is None
    assert new.components == ["UI", "Backend"]
    assert new.labels == []
    assert new.sprint == Choice("Esko\\Ink\\P135", "P135")
    assert new.area is None


def test_editor_missing_lines_keep_old_values():
    old = Details(parent=Choice("1", "1 x"), story_points=2.0, labels=["l"], area="A")
    assert details.from_editor({}, old) == old


def test_editor_typed_sprint_id_with_name():
    new = details.from_editor({"Sprint": "6000 Next sprint"}, Details())
    assert new.sprint == Choice("6000", "6000 Next sprint")
