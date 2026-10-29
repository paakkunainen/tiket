"""Prompt building and the Ollama call (mocked)."""

import json

import httpx
import pytest
import respx

from tiket import llm
from tiket.backends import Choice, Details, Draft, Example, Profile
from tiket.config import OllamaSettings

OLLAMA = "http://ollama.test/api/chat"


@pytest.fixture
def settings() -> OllamaSettings:
    return OllamaSettings(url="http://ollama.test/", model="m", language="auto", think="low")


def reply(data: dict | str) -> httpx.Response:
    content = data if isinstance(data, str) else json.dumps(data)
    return httpx.Response(200, json={"message": {"content": content}})


FI = [Example("Lataus kaatuu", "Bug", "Kun käyttäjä avaa näkymän, se ei toimi ja on hidas", ref="BI-1")]
EN = [Example("Export fails", "Bug", "When the user opens the view it is slow and fails", ref="BI-2")]


def test_detect_language():
    assert llm.detect_language(FI) == "Finnish"
    assert llm.detect_language(EN) == "English"
    assert llm.detect_language([]) == "English"


def test_format_examples_groups_by_type_and_truncates():
    examples = [
        Example("T1", "Task", "short"),
        Example("B1", "Bug", "x" * 2000, acceptance_criteria="- ac"),
        Example("T2", "Task", ""),
    ]
    text = llm._format_examples(examples)
    assert text.index("##### Task tickets") < text.index("Title: T2") < text.index("##### Bug tickets")
    assert "(empty description)" in text
    assert "x" * llm.EXAMPLE_CHARS + " …" in text and "x" * (llm.EXAMPLE_CHARS + 1) not in text
    assert "Acceptance criteria:\n- ac" in text
    assert "no existing tickets" in llm._format_examples([])


def test_parse_json_tolerates_surrounding_text():
    assert llm._parse_json('{"a": 1}') == {"a": 1}
    assert llm._parse_json('Sure!\n{"a": {"b": 2}}\nDone') == {"a": {"b": 2}}
    with pytest.raises(RuntimeError, match="did not return JSON"):
        llm._parse_json("no json here")


@respx.mock
def test_generate_builds_prompt_and_schema(settings, cloud_project):
    route = respx.post(OLLAMA).mock(
        return_value=reply({"title": " Otsikko ", "description": " Kuvaus ", "issue_type": "Bug"})
    )
    draft = llm.generate(settings, cloud_project, FI, ["Task", "Bug"], "lataus kaatuu")

    assert draft == Draft("Otsikko", "Kuvaus", "Bug", "", Details())
    payload = json.loads(route.calls.last.request.content)
    assert payload["model"] == "m"
    assert payload["think"] == "low"
    assert payload["stream"] is False
    assert payload["options"]["temperature"] == 0.3
    schema = payload["format"]
    assert schema["properties"]["issue_type"]["enum"] == ["Task", "Bug"]
    assert schema["required"] == ["title", "description", "issue_type"]
    system, user = payload["messages"]
    assert 'project "BI"' in system["content"]
    assert "in Finnish" in system["content"]  # auto-detected from the examples
    assert "Title: Lataus kaatuu" in system["content"]
    assert "acceptance_criteria" not in system["content"]
    assert "lataus kaatuu" in user["content"] and "in Finnish" in user["content"]


@respx.mock
def test_generate_language_and_style_overrides(settings, cloud_project):
    route = respx.post(OLLAMA).mock(return_value=reply({"title": "t", "description": "d", "issue_type": "Task"}))
    cloud_project.language = "Swedish"
    cloud_project.style_notes = "Always prefix [BE]"
    llm.generate(settings, cloud_project, FI, ["Task"], "x")
    system = json.loads(route.calls.last.request.content)["messages"][0]["content"]
    assert "in Swedish" in system
    assert "Team style notes: Always prefix [BE]" in system


@respx.mock
def test_generate_acceptance_criteria_and_details(settings, ado_project):
    route = respx.post(OLLAMA).mock(
        return_value=reply(
            {
                "title": "t",
                "description": "d",
                "issue_type": "User Story",
                "acceptance_criteria": " - works ",
                "parent": "1 Epic",
                "story_points": "3",
                "priority": "2",
            }
        )
    )
    profile = Profile(
        sample=4,
        parents=[Choice("1", "1 Epic", 3)],
        priorities=[Choice("2", "2", 4)],
        points=[("x", 3.0)],
        area="Esko\\T",
    )
    sprint = Choice("Esko\\S1", "S1 (active)")
    draft = llm.generate(
        settings, ado_project, EN, ["User Story"], "x", ac_field=True, profile=profile, sprint=sprint
    )
    assert draft.acceptance_criteria == "- works"
    assert draft.details == Details(
        parent=Choice("1", "1 Epic", 3), story_points=3.0, priority="2", sprint=sprint, area="Esko\\T"
    )
    payload = json.loads(route.calls.last.request.content)
    assert {"acceptance_criteria", "parent", "story_points", "priority"} <= set(payload["format"]["required"])
    assert "separate Acceptance Criteria field" in payload["messages"][0]["content"]
    assert "=== Ticket details ===" in payload["messages"][0]["content"]


@respx.mock
def test_regenerate_sends_previous_draft_and_feedback(settings, cloud_project):
    route = respx.post(OLLAMA).mock(return_value=reply({"title": "t2", "description": "d", "issue_type": "Task"}))
    previous = Draft("t1", "d1", "Task")
    llm.generate(settings, cloud_project, EN, ["Task"], "x", previous=previous, feedback="shorter")
    payload = json.loads(route.calls.last.request.content)
    assert payload["options"]["temperature"] == 0.6
    assistant, user = payload["messages"][2:]
    assert json.loads(assistant["content"])["title"] == "t1"
    assert user["content"] == "Rewrite the ticket. shorter"

    llm.generate(settings, cloud_project, EN, ["Task"], "x", previous=previous)
    assert json.loads(route.calls.last.request.content)["messages"][3]["content"].endswith("different wording.")


@respx.mock
def test_unknown_issue_type_falls_back_to_first(settings, cloud_project):
    respx.post(OLLAMA).mock(return_value=reply({"title": "t", "description": "d", "issue_type": "Epic"}))
    assert llm.generate(settings, cloud_project, EN, ["Task", "Bug"], "x").issue_type == "Task"


@respx.mock
def test_think_can_be_disabled(settings, cloud_project):
    settings.think = None
    route = respx.post(OLLAMA).mock(return_value=reply({"title": "t", "description": "d", "issue_type": "Task"}))
    llm.generate(settings, cloud_project, EN, ["Task"], "x")
    assert "think" not in json.loads(route.calls.last.request.content)


@respx.mock
def test_ollama_errors(settings, cloud_project):
    respx.post(OLLAMA).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(RuntimeError, match="Cannot reach Ollama"):
        llm.generate(settings, cloud_project, EN, ["Task"], "x")

    respx.post(OLLAMA).mock(return_value=httpx.Response(500, text="model not found"))
    with pytest.raises(RuntimeError, match="Ollama error 500: model not found"):
        llm.generate(settings, cloud_project, EN, ["Task"], "x")


# --- write_update ---------------------------------------------------------------

def a_ticket(**kw):
    from tiket.backends import Comment, Ticket

    return Ticket(
        "BI-5", "Raportti kaatuu", "https://t/BI-5", "Task", "Open", "BI",
        description=kw.pop("description", "Kun raportti ajetaan yöllä, se kaatuu ja on hidas"),
        comments=kw.pop("comments", [Comment("Ann", "2026-01-01T00:00", "Tarkistin lokit")]),
    )


@respx.mock
def test_write_update_lets_model_choose(settings):
    route = respx.post(OLLAMA).mock(
        return_value=reply({"target": "description", "text": " ### Lisätiedot\n- x ", "reason": "new scope"})
    )
    update = llm.write_update(settings, a_ticket(), "customer wants csv too")
    assert (update.target, update.text, update.reason) == ("description", "### Lisätiedot\n- x", "new scope")
    sent = json.loads(route.calls.last.request.content)
    system, user = sent["messages"][0]["content"], sent["messages"][1]["content"]
    assert "# BI-5: Raportti kaatuu" in system and "Tarkistin lokit" in system  # the whole ticket
    assert "Write in Finnish" in system  # detected from the ticket text
    assert "target must be" not in system
    assert "customer wants csv too" in user
    assert sent["format"]["properties"]["target"]["enum"] == ["comment", "description"]


@respx.mock
def test_write_update_forced_target_and_language(settings):
    route = respx.post(OLLAMA).mock(return_value=reply({"target": "comment", "text": "t", "reason": ""}))
    update = llm.write_update(settings, a_ticket(), "x", target="description", language="English")
    sent = json.loads(route.calls.last.request.content)
    assert sent["format"]["properties"]["target"]["enum"] == ["description"]
    assert 'target must be "description"' in sent["messages"][0]["content"]
    assert "Write in English" in sent["messages"][0]["content"]
    assert update.target == "description"  # an off-list answer falls back to the forced target


@respx.mock
def test_write_update_rewrite_sends_previous_and_feedback(settings):
    from tiket.backends import Update

    route = respx.post(OLLAMA).mock(return_value=reply({"target": "comment", "text": "t2", "reason": "r"}))
    llm.write_update(settings, a_ticket(), "x", previous=Update("comment", "t1", "r"), feedback="shorter")
    sent = json.loads(route.calls.last.request.content)
    assert json.loads(sent["messages"][2]["content"]) == {"target": "comment", "text": "t1", "reason": "r"}
    assert sent["messages"][3]["content"] == "Rewrite the addition. shorter"
    assert sent["options"]["temperature"] == 0.6


def test_long_ticket_context_drops_oldest_comments_first(monkeypatch):
    from tiket.backends import Comment

    monkeypatch.setattr(llm, "TICKET_CHARS", 400)
    comments = [Comment("A", "2026-01-0%dT00:00" % i, f"comment {i} " + "x" * 80) for i in range(1, 6)]
    text = llm._ticket_context(a_ticket(comments=comments))
    assert len(text) <= 400
    assert "comment 5" in text and "comment 1" not in text
    assert "## Description" in text
    assert len(llm._ticket_context(a_ticket(description="y" * 1000, comments=[]))) == 400
