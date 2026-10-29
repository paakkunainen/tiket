"""AzureDevOpsBackend against a mocked Azure DevOps organization."""

import base64
import json
from datetime import date, timedelta

import httpx
import pytest
import respx

from tiket.backends import Choice, Details, Draft, MissingFieldsError, Profile
from tiket.backends.azure_devops import (
    ACCEPTANCE,
    PRIORITY,
    REPRO_STEPS,
    STORY_POINTS,
    AzureDevOpsBackend,
)

ORG = "https://ado.test/Org"


def body(request: httpx.Request):
    return json.loads(request.content) if request.content else None


def json_response(data, status=200):
    return httpx.Response(status, json=data)


@pytest.fixture
def ado(ado_project):
    return AzureDevOpsBackend(ado_project.site, ado_project)


def mock_connection():
    return respx.get(f"{ORG}/_apis/connectionData").respond(
        json={"authenticatedUser": {"providerDisplayName": "Me", "properties": {"Account": {"$value": "me@esko.test"}}}}
    )


# --- basics -------------------------------------------------------------------

@respx.mock
def test_check_uses_pat_basic_auth(ado):
    route = mock_connection()
    assert ado.check() == "me@esko.test"
    request = route.calls.last.request
    assert request.headers["Authorization"] == "Basic " + base64.b64encode(b":pat").decode()
    assert request.url.params["api-version"] == "7.1-preview"


@respx.mock
def test_check_falls_back_to_display_name(ado):
    respx.get(f"{ORG}/_apis/connectionData").respond(json={"authenticatedUser": {"providerDisplayName": "Me"}})
    assert ado.check() == "Me"


@respx.mock
def test_expired_pat_html_redirect_is_an_error(ado):
    respx.get(f"{ORG}/_apis/projects").respond(200, html="<html>Sign in</html>")
    with pytest.raises(RuntimeError, match="bad or expired PAT"):
        ado.list_projects()


@respx.mock
def test_list_projects_and_types(ado):
    respx.get(f"{ORG}/_apis/projects").respond(json={"value": [{"name": "Esko", "description": "Main"}, {"name": "X"}]})
    route = respx.get(f"{ORG}/Esko/_apis/wit/workitemtypes").respond(
        json={"value": [{"name": "Bug"}, {"name": "Old", "isDisabled": True}, {"name": "User Story"}]}
    )
    assert ado.list_projects() == [("Esko", "Main"), ("X", "")]
    assert ado.issue_types() == ["Bug", "User Story"]
    assert route.calls.last.request.url.params["api-version"] == "7.1"


@respx.mock
def test_project_names_are_url_encoded(ado_site):
    from tiket.config import Project

    backend = AzureDevOpsBackend(ado_site, Project("x", ado_site, "My Project"))
    route = respx.get(f"{ORG}/My%20Project/_apis/wit/workitemtypes").respond(json={"value": []})
    backend.issue_types()
    assert route.called


# --- my_items -----------------------------------------------------------------

def item_fields(i, **extra):
    return {
        "System.Id": i,
        "System.Title": f"Item {i}",
        "System.WorkItemType": "User Story",
        "System.State": "Active",
        "System.TeamProject": "Esko",
        "System.ChangedDate": "2026-10-01T00:00:00Z",
        **extra,
    }


@respx.mock
def test_my_items(ado):
    wiql = respx.post(f"{ORG}/_apis/wit/wiql").respond(json={"workItems": [{"id": 2}, {"id": 1}, {"id": 3}]})
    respx.get(f"{ORG}/_apis/wit/workitems").respond(
        json={"value": [  # returned in a different order, and 3 is missing (deleted meanwhile)
            {"id": 1, "fields": item_fields(1, **{"System.State": "Closed"})},
            {"id": 2, "fields": item_fields(2, **{"System.TeamProject": "Other Proj"})},
        ]}
    )
    items = ado.my_items("Esko", "it's", include_done=False, limit=10)
    query = body(wiql.calls.last.request)["query"]
    assert "[System.AssignedTo] = @Me" in query
    assert "[System.State] NOT IN ('Closed', 'Done'" in query
    assert "[System.TeamProject] = 'Esko'" in query
    assert "[System.Title] CONTAINS 'it''s'" in query
    assert wiql.calls.last.request.url.params["$top"] == "10"
    assert [i.ref for i in items] == ["2", "1"]  # WIQL order kept
    assert items[0].url == f"{ORG}/Other%20Proj/_workitems/edit/2"
    assert items[1].done is True


@respx.mock
def test_my_items_all_and_empty(ado):
    wiql = respx.post(f"{ORG}/_apis/wit/wiql").respond(json={"workItems": []})
    assert ado.my_items(None, None, include_done=True, limit=5) == []
    query = body(wiql.calls.last.request)["query"]
    assert "NOT IN" not in query and "TeamProject" not in query


@respx.mock
def test_my_items_unknown_project_is_empty(ado):
    respx.post(f"{ORG}/_apis/wit/wiql").respond(400, json={"message": "TF200016: The following project does not exist"})
    assert ado.my_items("Nope", None, False, 5) == []


# --- examples & profile -------------------------------------------------------

@respx.mock
def test_recent_examples(ado):
    ado.project.types = ["User Story", "Bug"]
    wiql = respx.post(f"{ORG}/Esko/_apis/wit/wiql").respond(json={"workItems": [{"id": 1}, {"id": 2}]})
    items = respx.get(f"{ORG}/_apis/wit/workitems").respond(
        json={"value": [
            {"fields": {"System.Id": 1, "System.Title": "Story", "System.WorkItemType": "User Story",
                        "System.Description": "<p>Desc</p>", ACCEPTANCE: "<ul><li>ok</li></ul>"}},
            {"fields": {"System.Id": 2, "System.Title": "Bug", "System.WorkItemType": "Bug", REPRO_STEPS: "<p>Steps</p>"}},
        ]}
    )
    story, bug = ado.recent_examples(4)
    assert (story.ref, story.description, story.acceptance_criteria) == ("1", "Desc", "- ok")
    assert (bug.description, bug.acceptance_criteria) == ("Steps", "")
    assert "[System.WorkItemType] IN ('User Story', 'Bug')" in body(wiql.calls.last.request)["query"]
    assert items.calls.last.request.url.params["ids"] == "1,2"


@respx.mock
def test_recent_examples_empty(ado):
    respx.post(f"{ORG}/Esko/_apis/wit/wiql").respond(json={"workItems": []})
    assert ado.recent_examples(4) == []


@respx.mock
def test_field_profile(ado):
    respx.post(f"{ORG}/Esko/_apis/wit/wiql").respond(json={"workItems": [{"id": 1}, {"id": 2}, {"id": 3}]})

    def workitems(request):
        ids = request.url.params["ids"]
        if ids == "1,2,3":
            return json_response({"value": [
                {"id": 1, "fields": {"System.Title": "A", "System.AreaPath": "Esko\\1 TK\\Analytiikka",
                                     "System.IterationPath": "Esko\\1 TK\\Ink 42\\P132", "System.Parent": 100,
                                     STORY_POINTS: 3.0, PRIORITY: 1}},
                {"id": 2, "fields": {"System.Title": "B", "System.AreaPath": "Esko\\1 TK\\Analytiikka",
                                     "System.IterationPath": "Esko\\1 TK\\Ink 41\\P130", "System.Parent": 200,
                                     PRIORITY: 2}},
                {"id": 3, "fields": {"System.Title": "C", "System.AreaPath": "Esko\\Other",
                                     "System.IterationPath": "Esko", "System.Parent": 100, PRIORITY: 2}},
            ]})
        assert ids == "100,200"
        return json_response({"value": [
            {"id": 100, "fields": {"System.Title": "Open feature", "System.State": "Active"}},
            {"id": 200, "fields": {"System.Title": "Old feature", "System.State": "Closed"}},
        ]})

    respx.get(f"{ORG}/_apis/wit/workitems").mock(side_effect=workitems)
    p = ado.field_profile()
    assert p.sample == 3
    assert [(c.value, c.label, c.count) for c in p.parents] == [("100", "100 Open feature", 2)]
    assert [(c.value, c.count) for c in p.priorities] == [("2", 2), ("1", 1)]
    assert p.points == [("A", 3.0)]
    assert p.area == "Esko\\1 TK\\Analytiikka"
    assert p.sprint_hint == {"root": "Esko\\1 TK"}


@respx.mock
def test_field_profile_without_history(ado):
    respx.post(f"{ORG}/Esko/_apis/wit/wiql").respond(json={"workItems": []})
    assert ado.field_profile() == Profile()


# --- current sprint -----------------------------------------------------------

def node(name, start=None, finish=None, children=()):
    n = {"name": name, "children": list(children)}
    if start:
        n["attributes"] = {"startDate": f"{start}T00:00:00Z", "finishDate": f"{finish}T00:00:00Z"}
    return n


def days(n: int) -> str:
    return (date.today() + timedelta(days=n)).isoformat()


@respx.mock
def test_current_sprint_picks_deepest_running_iteration(ado):
    tree = node("Esko", children=[
        node("1 TK", children=[
            node("Ink 42", days(-30), days(30), children=[
                node("P133", days(-20), days(-6)),
                node("P134", days(-5), days(5)),
                node("P135", days(6), days(15)),
            ]),
        ]),
        node("Other team", children=[node("Now", days(-1), days(1))]),
    ])
    route = respx.get(f"{ORG}/Esko/_apis/wit/classificationnodes/Iterations").respond(json=tree)
    sprint = ado.current_sprint(Profile(sprint_hint={"root": "Esko\\1 TK"}))
    assert sprint == Choice("Esko\\1 TK\\Ink 42\\P134", "P134 (active)")
    assert route.calls.last.request.url.params["$depth"] == "6"


@respx.mock
def test_current_sprint_next_upcoming_when_none_running(ado):
    tree = node("Esko", children=[node("1 TK", children=[
        node("Later", days(20), days(30)),
        node("Soon", days(3), days(10)),
        node("Past", days(-10), days(-3)),
    ])])
    respx.get(f"{ORG}/Esko/_apis/wit/classificationnodes/Iterations").respond(json=tree)
    assert ado.current_sprint(Profile(sprint_hint={"root": "Esko\\1 TK"})) == Choice("Esko\\1 TK\\Soon", "Soon (next)")


@respx.mock
def test_current_sprint_root_prefix_is_exact(ado):
    # "Esko\1 TK2" must not count as being under "Esko\1 TK"
    tree = node("Esko", children=[node("1 TK2", children=[node("Now", days(-1), days(1))])])
    respx.get(f"{ORG}/Esko/_apis/wit/classificationnodes/Iterations").respond(json=tree)
    assert ado.current_sprint(Profile(sprint_hint={"root": "Esko\\1 TK"})) is None
    assert ado.current_sprint(Profile()) is None


# --- create -------------------------------------------------------------------

TYPE_FIELDS = ["System.Title", "System.Description", STORY_POINTS, PRIORITY, ACCEPTANCE]


def mock_type_fields(issue_type="User%20Story", fields=TYPE_FIELDS):
    return respx.get(f"{ORG}/Esko/_apis/wit/workitemtypes/{issue_type}/fields").respond(
        json={"value": [{"referenceName": f} for f in fields]}
    )


def ops_dict(request) -> dict:
    return {op["path"]: op["value"] for op in body(request)}


DETAILS = Details(
    parent=Choice("257609", "257609 Feature"),
    story_points=2.0,
    priority="1",
    sprint=Choice("Esko\\1 TK\\Ink 42\\P134", "P134"),
    area="Esko\\1 TK\\Analytiikka",
)


@respx.mock
def test_create_user_story(ado):
    mock_type_fields()
    mock_connection()
    post = respx.post(f"{ORG}/Esko/_apis/wit/workitems/$User%20Story").respond(
        json={"_links": {"html": {"href": f"{ORG}/Esko/_workitems/edit/1"}}}
    )
    created = ado.create(Draft("Title", "**Desc**", "User Story", "- works", DETAILS), Profile(), {"Custom.X": "y"})

    assert created.url == f"{ORG}/Esko/_workitems/edit/1" and created.warnings == []
    request = post.calls.last.request
    assert request.headers["Content-Type"] == "application/json-patch+json"
    ops = ops_dict(request)
    assert ops == {
        "/fields/System.Title": "Title",
        "/fields/System.Description": "<p><strong>Desc</strong></p>",
        f"/fields/{ACCEPTANCE}": "<ul>\n<li>works</li>\n</ul>",
        "/fields/System.AssignedTo": "me@esko.test",
        "/fields/System.AreaPath": "Esko\\1 TK\\Analytiikka",
        "/fields/System.IterationPath": "Esko\\1 TK\\Ink 42\\P134",
        f"/fields/{STORY_POINTS}": 2.0,
        f"/fields/{PRIORITY}": 1,
        "/fields/Custom.X": "y",
        "/relations/-": {
            "rel": "System.LinkTypes.Hierarchy-Reverse",
            "url": f"{ORG}/_apis/wit/workItems/257609",
        },
    }


@respx.mock
def test_create_bug_uses_repro_steps_and_skips_missing_fields(ado):
    ado.project.assign_to_me = False
    mock_type_fields("Bug", ["System.Title", REPRO_STEPS])
    post = respx.post(f"{ORG}/Esko/_apis/wit/workitems/$Bug").respond(json={"_links": {"html": {"href": "u"}}})
    ado.create(Draft("T", "Steps", "Bug", "- ignored", Details(story_points=1.0, priority="2")), Profile())
    ops = ops_dict(post.calls.last.request)
    assert ops == {"/fields/System.Title": "T", f"/fields/{REPRO_STEPS}": "<p>Steps</p>"}


@respx.mock
def test_create_retries_without_rejected_details(ado):
    ado.project.assign_to_me = False
    mock_type_fields()
    post = respx.post(f"{ORG}/Esko/_apis/wit/workitems/$User%20Story").mock(side_effect=[
        httpx.Response(400, json={"message": "TF401347: Invalid tree name given for work item -1, field 'System.IterationPath'."}),
        httpx.Response(200, json={"_links": {"html": {"href": "u"}}}),
    ])
    created = ado.create(Draft("T", "D", "User Story", details=DETAILS), Profile())
    retry = ops_dict(post.calls[1].request)
    assert "/relations/-" not in retry and "/fields/System.IterationPath" not in retry
    assert created.warnings[0].startswith("Created without parent/area/iteration/points/priority")


@respx.mock
def test_create_error_without_details_is_raised(ado):
    ado.project.assign_to_me = False
    mock_type_fields()
    respx.post(f"{ORG}/Esko/_apis/wit/workitems/$User%20Story").respond(400, json={"message": "bad title"})
    with pytest.raises(RuntimeError, match="bad title"):
        ado.create(Draft("T", "D", "User Story"), Profile())


@respx.mock
def test_create_missing_required_field(ado):
    ado.project.assign_to_me = False
    mock_type_fields()
    respx.post(f"{ORG}/Esko/_apis/wit/workitems/$User%20Story").respond(
        400,
        json={
            "message": "TF401320: Rule Error",
            "customProperties": {"RuleValidationErrors": [
                {"fieldReferenceName": ACCEPTANCE, "fieldStatusFlags": "required, hasValues",
                 "errorMessage": "TF401320: Rule Error for field Acceptance Criteria. Error code: Required, HasValues."},
                {"fieldReferenceName": "Other", "fieldStatusFlags": "readOnly", "errorMessage": "x"},
            ]},
        },
    )
    with pytest.raises(MissingFieldsError) as e:
        ado.create(Draft("T", "D", "User Story", details=DETAILS), Profile())
    assert e.value.fields == [(ACCEPTANCE, "Acceptance Criteria")]


# --- ticket (show) ------------------------------------------------------------

@respx.mock
def test_ticket(ado):
    get = respx.get(f"{ORG}/_apis/wit/workitems/42").respond(json={
        "id": 42,
        "fields": {
            "System.Title": "Story", "System.WorkItemType": "User Story", "System.State": "Active",
            "System.TeamProject": "Esko", "System.CreatedDate": "2026-01-01T00:00:00Z",
            "System.ChangedDate": "2026-01-02T00:00:00Z", "System.AssignedTo": {"displayName": "Me"},
            "System.CreatedBy": {"displayName": "Boss"}, PRIORITY: 2, STORY_POINTS: 3.5,
            "System.IterationPath": "Esko\\S1", "System.AreaPath": "Esko\\A", "System.Tags": "P1; P2",
            "System.Reason": "New", "System.Description": "<p>Desc</p>", ACCEPTANCE: "<p>Done</p>",
        },
        "relations": [
            {"rel": "System.LinkTypes.Hierarchy-Reverse", "url": f"{ORG}/_apis/wit/workItems/10", "attributes": {"name": "Parent"}},
            {"rel": "System.LinkTypes.Hierarchy-Forward", "url": f"{ORG}/_apis/wit/workItems/43", "attributes": {"name": "Child"}},
            {"rel": "System.LinkTypes.Related", "url": f"{ORG}/_apis/wit/workItems/44", "attributes": {}},
            {"rel": "AttachedFile", "url": f"{ORG}/_apis/wit/attachments/abc", "attributes": {"name": "shot.png"}},
            {"rel": "Hyperlink", "url": "https://wiki.test/page", "attributes": {"comment": "spec"}},
            {"rel": "ArtifactLink", "url": "vstfs:///Git/PullRequestId/1", "attributes": {"name": "Pull Request"}},
        ],
    })
    respx.get(f"{ORG}/_apis/wit/workitems").respond(json={"value": [
        {"id": 10, "fields": {"System.Title": "Feature", "System.State": "New", "System.WorkItemType": "Feature"}},
        {"id": 43, "fields": {"System.Title": "Kid", "System.State": "Closed", "System.WorkItemType": "Task"}},
    ]})  # 44 not visible
    comments = respx.get(f"{ORG}/Esko/_apis/wit/workItems/42/comments").mock(side_effect=[
        json_response({"comments": [
            {"createdBy": {"displayName": "Ann"}, "createdDate": "2026-01-03T00:00:00Z", "text": "<div>Hi</div>"},
            {"isDeleted": True, "text": "gone"},
        ], "continuationToken": "next"}),
        json_response({"comments": [{"createdBy": {"displayName": "Bob"}, "createdDate": "2026-01-04T00:00:00Z", "text": "Bye"}]}),
    ])

    t = ado.ticket("42")

    assert get.calls.last.request.url.params["$expand"] == "relations"
    assert (t.ref, t.title, t.issue_type, t.status, t.project) == ("42", "Story", "User Story", "Active", "Esko")
    assert t.url == f"{ORG}/Esko/_workitems/edit/42"
    assert t.fields == {
        "Assignee": "Me", "Created by": "Boss", "Priority": "2", "Parent": "10 Feature (Feature, New)",
        "Story points": "3.5", "Iteration": "Esko\\S1", "Area": "Esko\\A", "Tags": "P1; P2", "Reason": "New",
    }
    assert (t.description, t.acceptance_criteria) == ("Desc", "Done")
    assert t.links == [
        "child 43 Kid (Task, Closed)",
        "related 44",  # not visible to the token: id only
        "link https://wiki.test/page (spec)",
        "pull request vstfs:///Git/PullRequestId/1",
    ]
    assert t.attachments == [f"shot.png ({ORG}/_apis/wit/attachments/abc)"]
    assert [(c.author, c.body) for c in t.comments] == [("Ann", "Hi"), ("Bob", "Bye")]
    first, second = (c.request.url.params for c in comments.calls)
    assert first["api-version"] == "7.1-preview.4" and "continuationToken" not in first
    assert second["continuationToken"] == "next"


@respx.mock
def test_ticket_bug_without_relations(ado):
    respx.get(f"{ORG}/_apis/wit/workitems/7").respond(json={"id": 7, "fields": {
        "System.Title": "B", "System.WorkItemType": "Bug", "System.State": "New",
        "System.TeamProject": "Esko", REPRO_STEPS: "<p>Repro</p>",
    }})
    respx.get(f"{ORG}/Esko/_apis/wit/workItems/7/comments").respond(json={"comments": []})
    t = ado.ticket("7")
    assert t.description == "Repro"
    assert t.fields["Assignee"] == "unassigned"
    assert t.links == [] and t.attachments == [] and t.comments == []


@respx.mock
def test_ticket_not_found(ado):
    with pytest.raises(LookupError, match="numbers"):
        ado.ticket("BI-1")
    respx.get(f"{ORG}/_apis/wit/workitems/9").respond(404, json={"message": "TF401232: Work item 9 does not exist"})
    with pytest.raises(LookupError, match="9 not found on ado"):
        ado.ticket("9")
    respx.get(f"{ORG}/_apis/wit/workitems/8").respond(500, text="down")
    with pytest.raises(RuntimeError, match="500"):
        ado.ticket("8")


# --- adding to a ticket -------------------------------------------------------

def ado_ticket():
    from tiket.backends import Ticket

    return Ticket("42", "t", f"{ORG}/Esko%20Two/_workitems/edit/42", "User Story", "New", "Esko Two")


@respx.mock
def test_add_comment_posts_html_to_the_items_project(ado):
    route = respx.post(f"{ORG}/Esko%20Two/_apis/wit/workItems/42/comments").respond(json={"id": 1})
    assert ado.add_comment(ado_ticket(), "**Valmis**") == f"{ORG}/Esko%20Two/_workitems/edit/42"
    request = route.calls.last.request
    assert body(request) == {"text": "<p><strong>Valmis</strong></p>"}
    assert request.url.params["api-version"] == "7.1-preview.4"


@respx.mock
@pytest.mark.parametrize(
    "item_type, field", [("User Story", "System.Description"), ("Bug", REPRO_STEPS)]
)
def test_append_description_adds_html_to_the_shown_field(ado, item_type, field):
    respx.get(f"{ORG}/_apis/wit/workitems/42").respond(
        json={"id": 42, "rev": 7, "fields": {"System.WorkItemType": item_type, field: "<div>Old</div>"}}
    )
    patch = respx.patch(f"{ORG}/_apis/wit/workitems/42").respond(json={"id": 42})
    ado.append_description(ado_ticket(), "## Update\n\n| a | b |\n|---|---|\n| 1 | 2 |")
    request = patch.calls.last.request
    assert request.headers["Content-Type"] == "application/json-patch+json"
    test, add = body(request)
    assert test == {"op": "test", "path": "/rev", "value": 7}  # refuses to overwrite a newer save
    assert add["path"] == f"/fields/{field}"
    assert add["value"].startswith("<div>Old</div><h2>Update</h2>")
    assert "<td>1</td>" in add["value"]


@respx.mock
def test_append_description_to_empty_field(ado):
    respx.get(f"{ORG}/_apis/wit/workitems/42").respond(
        json={"id": 42, "rev": 1, "fields": {"System.WorkItemType": "Task"}}
    )
    patch = respx.patch(f"{ORG}/_apis/wit/workitems/42").respond(json={"id": 42})
    ado.append_description(ado_ticket(), "new")
    assert body(patch.calls.last.request)[1]["value"] == "<p>new</p>"
