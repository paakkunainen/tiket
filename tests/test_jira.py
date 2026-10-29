"""JiraBackend against a mocked Jira (Cloud and Data Center)."""

import base64
import json
from datetime import date

import httpx
import pytest
import respx

from tiket.backends import Choice, Details, Draft, MissingFieldsError, Profile
from tiket.backends.jira import MAX_CHILDREN, JiraBackend, _parse_sprint, _sprint_summary

CLOUD = "https://cloud.test"
DC = "https://dc.test"

FIELDS = [
    {"id": "summary", "name": "Summary", "schema": {"type": "string"}},
    {"id": "customfield_10020", "name": "Sprint", "schema": {"custom": "com.pyxis.greenhopper.jira:gh-sprint"}},
    {"id": "customfield_10014", "name": "Epic Link", "schema": {"custom": "com.pyxis.greenhopper.jira:gh-epic-link"}},
    {"id": "customfield_10016", "name": "Story point estimate", "schema": {"custom": "com.pyxis.greenhopper.jira:jsw-story-points"}},
    {"id": "customfield_10036", "name": "Story Points", "schema": {"custom": "com.atlassian.jira.plugin.system.customfieldtypes:float"}},
    {"id": "customfield_99", "name": "Story points text", "schema": {"custom": "x:textfield"}},
]


def body(request: httpx.Request) -> dict:
    return json.loads(request.content) if request.content else {}


def jql_of(request: httpx.Request) -> str:
    return body(request).get("jql") if request.method == "POST" else request.url.params["jql"]


@pytest.fixture
def cloud(cloud_project):
    return JiraBackend(cloud_project.site, cloud_project)


@pytest.fixture
def dc(dc_project):
    return JiraBackend(dc_project.site, dc_project)


def issue(key, summary="s", **fields):
    return {"key": key, "fields": {"summary": summary, **fields}}


# --- auth & basics -------------------------------------------------------------

@respx.mock
def test_cloud_uses_basic_auth(cloud):
    route = respx.get(f"{CLOUD}/rest/api/2/myself").respond(json={"emailAddress": "me@cloud.test"})
    assert cloud.check() == "me@cloud.test"
    expected = "Basic " + base64.b64encode(b"me@cloud.test:tok").decode()
    assert route.calls.last.request.headers["Authorization"] == expected


@respx.mock
def test_dc_uses_bearer_pat(dc):
    route = respx.get(f"{DC}/rest/api/2/myself").respond(json={"name": "jdoe", "displayName": "J Doe"})
    assert dc.check() == "J Doe"
    assert route.calls.last.request.headers["Authorization"] == "Bearer pat"


def test_cloud_needs_email(cloud_site):
    cloud_site.email = None
    with pytest.raises(ValueError, match="needs `email`"):
        JiraBackend(cloud_site)


@respx.mock
def test_errors_include_status_and_body(cloud):
    respx.get(f"{CLOUD}/rest/api/2/myself").respond(401, text="Unauthorized token")
    with pytest.raises(RuntimeError, match="401: Unauthorized token"):
        cloud.check()


@respx.mock
def test_list_projects_and_issue_types(cloud):
    respx.get(f"{CLOUD}/rest/api/2/project").respond(json=[{"key": "BI", "name": "Business"}])
    respx.get(f"{CLOUD}/rest/api/2/issue/createmeta/BI/issuetypes").respond(
        json={"issueTypes": [{"name": "Task"}, {"name": "Bug"}, {"name": "Sub-task", "subtask": True}]}
    )
    assert cloud.list_projects() == [("BI", "Business")]
    assert cloud.issue_types() == ["Task", "Bug"]


@respx.mock
def test_issue_types_dc_values_format(dc):
    respx.get(f"{DC}/rest/api/2/issue/createmeta/OPHJODTA/issuetypes").respond(json={"values": [{"name": "Task"}]})
    assert dc.issue_types() == ["Task"]


# --- my_items -----------------------------------------------------------------

SEARCH_ITEM = {
    "key": "BI-7",
    "fields": {
        "summary": "Do it",
        "issuetype": {"name": "Task"},
        "status": {"name": "Done", "statusCategory": {"key": "done"}},
        "project": {"key": "BI"},
        "updated": "2026-10-01T10:00:00.000+0300",
    },
}


@respx.mock
def test_my_items_cloud(cloud):
    route = respx.post(f"{CLOUD}/rest/api/3/search/jql").respond(json={"issues": [SEARCH_ITEM]})
    items = cloud.my_items("BI", 'say "x"', include_done=False, limit=20)
    sent = body(route.calls.last.request)
    assert sent["jql"] == (
        'assignee = currentUser() AND statusCategory != Done AND project = "BI" '
        'AND summary ~ "say \\"x\\"" ORDER BY updated DESC'
    )
    assert sent["maxResults"] == 20
    (item,) = items
    assert (item.ref, item.title, item.issue_type, item.status, item.project) == ("BI-7", "Do it", "Task", "Done", "BI")
    assert item.done is True
    assert item.url == f"{CLOUD}/browse/BI-7"


@respx.mock
def test_my_items_dc_uses_get_search(dc):
    route = respx.get(f"{DC}/rest/api/2/search").respond(json={"issues": []})
    assert dc.my_items(None, None, include_done=True, limit=5) == []
    params = route.calls.last.request.url.params
    assert params["jql"] == "assignee = currentUser() ORDER BY updated DESC"
    assert params["maxResults"] == "5"
    assert "summary" in params["fields"]


@respx.mock
def test_my_items_unknown_project_on_this_site_is_empty(cloud):
    respx.post(f"{CLOUD}/rest/api/3/search/jql").respond(
        400, json={"errorMessages": ["The value 'XYZ' does not exist for the field 'project'."]}
    )
    assert cloud.my_items("XYZ", None, False, 5) == []
    with pytest.raises(RuntimeError):
        cloud.my_items(None, None, False, 5)


# --- recent_examples ----------------------------------------------------------

@respx.mock
def test_recent_examples(cloud):
    cloud.project.types = ["Task", "Bug"]
    route = respx.post(f"{CLOUD}/rest/api/3/search/jql").respond(
        json={
            "issues": [
                {
                    "key": "BI-1",
                    "fields": {"summary": "Ex", "issuetype": {"name": "Bug"}},
                    "renderedFields": {"description": "<p>Hello <b>you</b></p>"},
                }
            ]
        }
    )
    (ex,) = cloud.recent_examples(3)
    assert (ex.ref, ex.title, ex.issue_type, ex.description) == ("BI-1", "Ex", "Bug", "Hello **you**")
    sent = body(route.calls.last.request)
    assert sent["jql"] == 'project = "BI" AND issuetype in ("Task", "Bug") ORDER BY created DESC'
    assert sent["expand"] == "renderedFields"

    cloud.recent_examples(3, filter_types=False)
    assert "issuetype in" not in body(route.calls.last.request)["jql"]


# --- field profile ------------------------------------------------------------

def profile_search(issues, open_keys):
    def handler(request):
        jql = jql_of(request)
        if jql.startswith("key in"):
            return httpx.Response(200, json={"issues": [issue(k, f"Epic {k}") for k in open_keys]})
        return httpx.Response(200, json={"issues": issues})

    return handler


@respx.mock
def test_field_profile_cloud(cloud):
    respx.get(f"{CLOUD}/rest/api/2/field").respond(json=FIELDS)
    sprint = {"id": 50, "name": "BF Sprint 150", "boardId": 7}
    newer = {"id": 51, "name": "BF Sprint 151", "boardId": 7}
    issues = [
        issue("BI-3", "Third", issuetype={"name": "Task"}, parent={"key": "BI-890"}, priority={"name": "Medium"},
              components=[{"name": "@AI"}], labels=["infra"], customfield_10036=2.0, customfield_10020=[sprint, newer]),
        issue("BI-2", "Second", issuetype={"name": "Task"}, parent={"key": "BI-890"}, priority={"name": "High"},
              components=[{"name": "@AI"}, {"name": "UI"}], labels=[], customfield_10036=None),
        issue("BI-1", "First", issuetype={"name": "Task"}, parent={"key": "BI-5"}, priority={"name": "Medium"},
              customfield_10036=1.0, customfield_10016=None),
        issue("BI-0", "Sub", issuetype={"name": "Sub-task", "subtask": True}, parent={"key": "BI-3"}),
    ]
    search = respx.post(f"{CLOUD}/rest/api/3/search/jql").mock(side_effect=profile_search(issues, ["BI-890"]))
    p = cloud.field_profile()

    assert p.sample == 3  # subtasks ignored
    assert [(c.value, c.label, c.count) for c in p.parents] == [("BI-890", "BI-890 Epic BI-890", 2)]  # BI-5 is done
    assert [(c.value, c.count) for c in p.components] == [("@AI", 2), ("UI", 1)]
    assert [c.value for c in p.labels] == ["infra"]
    assert [(c.value, c.count) for c in p.priorities] == [("Medium", 2), ("High", 1)]
    assert p.points == [("Third", 2.0), ("First", 1.0)]
    assert p.field_ids["story_points"] == "customfield_10036"  # the story points field this project fills
    assert p.field_ids["sprint"] == "customfield_10020"
    assert p.sprint_hint == {"board": 7, "name": "BF Sprint 151"}
    first_jql = body(search.calls[0].request)["jql"]
    assert "(assignee = currentUser() OR reporter = currentUser())" in first_jql
    open_jql = body(search.calls[1].request)["jql"]
    assert open_jql == "key in (BI-5, BI-890) AND statusCategory != Done"


@respx.mock
def test_field_profile_dc_epic_link_and_string_sprints(dc):
    respx.get(f"{DC}/rest/api/2/field").respond(json=FIELDS)
    sprint = "com.atlassian.greenhopper.service.sprint.Sprint@1a[id=2405,rapidViewId=1127,state=ACTIVE,name=JOD TA Sprint37,startDate=x]"
    issues = [issue("OPHJODTA-9", "x", issuetype={"name": "Task"}, customfield_10014="OPHJODTA-1",
                    customfield_10020=[sprint], parent={"key": "IGNORED-ON-DC"})]
    respx.get(f"{DC}/rest/api/2/search").mock(side_effect=profile_search(issues, ["OPHJODTA-1"]))
    p = dc.field_profile()
    assert [c.value for c in p.parents] == ["OPHJODTA-1"]
    assert p.sprint_hint == {"board": 1127, "name": "JOD TA Sprint37"}
    assert "story_points" not in p.field_ids  # no field filled in


@respx.mock
def test_field_profile_without_history(cloud):
    respx.get(f"{CLOUD}/rest/api/2/field").respond(json=FIELDS)
    respx.post(f"{CLOUD}/rest/api/3/search/jql").respond(json={"issues": []})
    p = cloud.field_profile()
    assert p.sample == 0 and p.field_ids["sprint"] == "customfield_10020"


@respx.mock
def test_open_parents_lookup_failure_keeps_keys(cloud):
    def handler(request):
        if jql_of(request).startswith("key in"):
            return httpx.Response(400, text="An issue with key 'BI-404' does not exist")
        return httpx.Response(200, json={"issues": [issue("BI-1", issuetype={"name": "Task"}, parent={"key": "BI-404"})]})

    respx.get(f"{CLOUD}/rest/api/2/field").respond(json=[])
    respx.post(f"{CLOUD}/rest/api/3/search/jql").mock(side_effect=handler)
    assert [(c.value, c.label) for c in cloud.field_profile().parents] == [("BI-404", "BI-404")]


def test_parse_sprint_formats():
    assert _parse_sprint({"id": 1, "name": "S", "boardId": 2}) == {"id": 1, "name": "S", "board": 2}
    dc = "x.Sprint@ab[id=3,rapidViewId=4,state=CLOSED,name=JOD Sprint 3,goal=,startDate=2026]"
    assert _parse_sprint(dc) == {"id": "3", "name": "JOD Sprint 3", "board": "4"}


def test_sprint_summary():
    assert _sprint_summary([]) == ""
    assert _sprint_summary([{"id": 9, "name": "S9"}]) == "S9"
    assert _sprint_summary([{"id": 10, "name": "S10"}, {"id": "9", "name": "S9"}]) == "S10 (carried over from 1 earlier)"


# --- current sprint -----------------------------------------------------------

@respx.mock
def test_current_sprint_prefers_active_same_family_own_board(cloud):
    route = respx.get(f"{CLOUD}/rest/agile/1.0/board/7/sprint").respond(
        json={
            "values": [
                {"id": 1, "name": "Other team 12", "state": "active", "originBoardId": 7, "startDate": "2026-01-01"},
                {"id": 2, "name": "BF Sprint 155", "state": "active", "originBoardId": 7, "startDate": "2026-10-20"},
                {"id": 3, "name": "BF Sprint 155", "state": "active", "originBoardId": 8},
                {"id": 4, "name": "BF Sprint 156", "state": "future", "originBoardId": 7},
            ]
        }
    )
    sprint = cloud.current_sprint(Profile(sprint_hint={"board": 7, "name": "BF Sprint 151"}))
    assert sprint == Choice("2", "BF Sprint 155 (active)")
    assert route.calls.last.request.url.params["state"] == "active,future"


@respx.mock
def test_current_sprint_falls_back_to_next_future(cloud):
    respx.get(f"{CLOUD}/rest/agile/1.0/board/7/sprint").respond(
        json={"values": [
            {"id": 9, "name": "S 9", "state": "future", "startDate": "2026-12-01"},
            {"id": 8, "name": "S 8", "state": "future", "startDate": "2026-11-01"},
        ]}
    )
    assert cloud.current_sprint(Profile(sprint_hint={"board": 7, "name": "S 7"})) == Choice("8", "S 8 (next)")


@respx.mock
def test_current_sprint_none_cases(cloud):
    assert cloud.current_sprint(Profile()) is None  # no hint, no request
    respx.get(f"{CLOUD}/rest/agile/1.0/board/7/sprint").respond(json={"values": []})
    assert cloud.current_sprint(Profile(sprint_hint={"board": 7, "name": "S"})) is None
    respx.get(f"{CLOUD}/rest/agile/1.0/board/8/sprint").respond(403)
    assert cloud.current_sprint(Profile(sprint_hint={"board": 8, "name": "S"})) is None


# --- create -------------------------------------------------------------------

PROFILE = Profile(sample=5, field_ids={"story_points": "customfield_10036", "sprint": "customfield_10020"})
FULL = Details(
    parent=Choice("BI-890", "BI-890 AI"),
    story_points=2.0,
    priority="Medium",
    components=["@AI"],
    labels=["infra"],
    sprint=Choice("5609", "BF Sprint 154 (active)"),
)


def mock_myself(base, **me):
    respx.get(f"{base}/rest/api/2/myself").respond(json=me or {"accountId": "acc-1", "name": "me"})


@respx.mock
def test_create_cloud_sends_everything(cloud):
    mock_myself(CLOUD)
    post = respx.post(f"{CLOUD}/rest/api/2/issue").respond(201, json={"key": "BI-100"})
    sprint = respx.post(f"{CLOUD}/rest/agile/1.0/sprint/5609/issue").respond(204)
    draft = Draft("Title", "## Kuvaus\n- **a**", "Task", "- ac", FULL)

    created = cloud.create(draft, PROFILE, {"customfield_1": "x"})

    assert created.url == f"{CLOUD}/browse/BI-100" and created.warnings == []
    fields = body(post.calls.last.request)["fields"]
    assert fields == {
        "project": {"key": "BI"},
        "summary": "Title",
        "description": "h2. Kuvaus\n* *a*\n\n* ac",  # AC appended, converted to wiki markup
        "issuetype": {"name": "Task"},
        "parent": {"key": "BI-890"},
        "customfield_10036": 2.0,
        "priority": {"name": "Medium"},
        "components": [{"name": "@AI"}],
        "labels": ["infra"],
        "assignee": {"accountId": "acc-1"},
        "customfield_1": "x",
    }
    assert body(sprint.calls.last.request) == {"issues": ["BI-100"]}


@respx.mock
def test_create_dc_uses_epic_link_and_username(dc):
    mock_myself(DC)
    post = respx.post(f"{DC}/rest/api/2/issue").respond(201, json={"key": "OPHJODTA-1"})
    dc.project.extra_fields = {"labels": ["from-config"]}
    dc.create(Draft("T", "D", "Task", details=Details(parent=Choice("OPHJODTA-2", "x"), labels=["guess"])),
              Profile(field_ids={"epic_link": "customfield_10014"}))
    fields = body(post.calls.last.request)["fields"]
    assert fields["customfield_10014"] == "OPHJODTA-2"
    assert "parent" not in fields
    assert fields["assignee"] == {"name": "me"}
    assert fields["labels"] == ["from-config"]  # config extra_fields win over guesses


@respx.mock
def test_create_without_assignee_or_details(cloud):
    cloud.project.assign_to_me = False
    post = respx.post(f"{CLOUD}/rest/api/2/issue").respond(201, json={"key": "BI-1"})
    cloud.create(Draft("T", "D", "Task"), Profile())
    assert set(body(post.calls.last.request)["fields"]) == {"project", "summary", "description", "issuetype"}


@respx.mock
def test_create_defers_off_screen_fields_and_drops_rejected_ones(cloud):
    mock_myself(CLOUD)
    attempts = []

    def handler(request):
        fields = body(request)["fields"]
        attempts.append(set(fields))
        errors = {}
        if "customfield_10036" in fields:
            errors["customfield_10036"] = "Field 'customfield_10036' cannot be set. It is not on the appropriate screen, or unknown."
        if "components" in fields:
            errors["components"] = "Component name '@AI' is not valid"
        if errors:
            return httpx.Response(400, json={"errors": errors})
        return httpx.Response(201, json={"key": "BI-5"})

    respx.post(f"{CLOUD}/rest/api/2/issue").mock(side_effect=handler)
    put = respx.put(f"{CLOUD}/rest/api/2/issue/BI-5").respond(204)
    respx.post(f"{CLOUD}/rest/agile/1.0/sprint/5609/issue").respond(500, text="sprint closed")

    created = cloud.create(Draft("T", "D", "Task", details=FULL), PROFILE)

    assert len(attempts) == 2
    assert body(put.calls.last.request) == {"fields": {"customfield_10036": 2.0}}
    assert created.warnings[0] == "Skipped components: Component name '@AI' is not valid"
    assert created.warnings[1].startswith("Could not add to sprint BF Sprint 154 (active)")
    assert len(created.warnings) == 2


@respx.mock
def test_create_deferred_field_failure_is_a_warning(cloud):
    mock_myself(CLOUD)
    respx.post(f"{CLOUD}/rest/api/2/issue").mock(side_effect=[
        httpx.Response(400, json={"errors": {"customfield_10036": "cannot be set. not on the appropriate screen"}}),
        httpx.Response(201, json={"key": "BI-6"}),
    ])
    respx.put(f"{CLOUD}/rest/api/2/issue/BI-6").respond(400, text="nope")
    created = cloud.create(Draft("T", "D", "Task", details=Details(story_points=3.0)), PROFILE)
    assert created.warnings[0].startswith("Could not set story points")


@respx.mock
def test_create_required_fields_raise(cloud):
    mock_myself(CLOUD)
    respx.post(f"{CLOUD}/rest/api/2/issue").respond(
        400, json={"errors": {"customfield_200": "Team is required."}}
    )
    with pytest.raises(MissingFieldsError) as e:
        cloud.create(Draft("T", "D", "Task"), Profile())
    assert e.value.fields == [("customfield_200", "Team is required.")]


@respx.mock
def test_create_base_field_errors_are_not_swallowed(cloud):
    mock_myself(CLOUD)
    respx.post(f"{CLOUD}/rest/api/2/issue").respond(
        400, json={"errors": {"issuetype": "The issue type selected is invalid."}}
    )
    with pytest.raises(RuntimeError, match="issue type selected is invalid"):
        cloud.create(Draft("T", "D", "Nope"), Profile())


# --- ticket (show) ------------------------------------------------------------

def full_issue(**overrides):
    fields = {
        "summary": "Broken export",
        "issuetype": {"name": "Task"},
        "status": {"name": "In Progress"},
        "project": {"key": "BI"},
        "priority": {"name": "Medium"},
        "assignee": {"displayName": "Me Myself"},
        "reporter": {"displayName": "Boss"},
        "resolution": None,
        "created": "2026-01-01T10:00:00.000+0200",
        "updated": "2026-01-02T10:00:00.000+0200",
        "labels": ["infra", "ai"],
        "components": [{"name": "@AI"}],
        "fixVersions": [{"name": "1.0"}],
        "parent": {"key": "BI-890", "fields": {"summary": "Toiminto: AI"}},
        "issuelinks": [
            {"type": {"inward": "is blocked by", "outward": "blocks"}, "outwardIssue": {"key": "BI-2", "fields": {"summary": "Other"}}},
            {"type": {"inward": "relates to", "outward": "relates to"}, "inwardIssue": {"key": "BI-3", "fields": {"summary": "Third"}}},
        ],
        "subtasks": [{"key": "BI-4", "fields": {"summary": "Sub", "status": {"name": "Done"}}}],
        "attachment": [{"filename": "log.txt", "content": f"{CLOUD}/att/1"}],
        "description": "raw wiki",
        "customfield_10036": 3.0,
        "customfield_10020": [{"id": 2, "name": "S2", "boardId": 7}, {"id": 1, "name": "S1", "boardId": 7}],
    }
    fields |= overrides
    return {"key": "BI-1", "fields": fields, "renderedFields": {"description": "<p>Export <b>fails</b></p>"}}


@respx.mock
def test_ticket_cloud(cloud):
    respx.get(f"{CLOUD}/rest/api/2/field").respond(json=FIELDS)
    get = respx.get(f"{CLOUD}/rest/api/2/issue/BI-1").respond(json=full_issue())
    children = respx.post(f"{CLOUD}/rest/api/3/search/jql").respond(
        json={"issues": [
            issue("BI-9", "Child", status={"name": "To Do"}, issuetype={"name": "Task"}),
            issue("BI-4", "Sub", status={"name": "Done"}, issuetype={"name": "Sub-task", "subtask": True}),
        ]}
    )
    comments = respx.get(f"{CLOUD}/rest/api/2/issue/BI-1/comment").mock(side_effect=[
        httpx.Response(200, json={"total": 3, "comments": [
            {"author": {"displayName": "Ann"}, "created": "2026-01-03T08:00:00.000+0200", "renderedBody": "<p>First</p>"},
            {"author": {"displayName": "Bob"}, "created": "2026-01-04T08:00:00.000+0200", "renderedBody": "", "body": "raw"},
        ]}),
        httpx.Response(200, json={"total": 3, "comments": [
            {"created": "2026-01-05T08:00:00.000+0200", "renderedBody": "<p>Third</p>"},
        ]}),
    ])

    t = cloud.ticket("bi-1")

    assert get.calls.last.request.url.params["expand"] == "renderedFields"
    assert "customfield_10036" in get.calls.last.request.url.params["fields"]
    assert (t.ref, t.title, t.status, t.project, t.url) == ("BI-1", "Broken export", "In Progress", "BI", f"{CLOUD}/browse/BI-1")
    assert t.description == "Export **fails**"
    assert t.fields == {
        "Assignee": "Me Myself",
        "Reporter": "Boss",
        "Priority": "Medium",
        "Resolution": "",
        "Parent": "BI-890 Toiminto: AI",
        "Story points": "3",
        "Sprint": "S2 (carried over from 1 earlier)",
        "Components": "@AI",
        "Labels": "infra, ai",
        "Fix versions": "1.0",
    }
    assert t.links == [
        "blocks BI-2 Other",
        "relates to BI-3 Third",
        "subtask BI-4 Sub (Done)",
        "child BI-9 Child (To Do)",
    ]
    assert body(children.calls.last.request)["jql"] == "parent = BI-1 ORDER BY created DESC"
    assert t.attachments == [f"log.txt ({CLOUD}/att/1)"]
    assert [(c.author, c.body) for c in t.comments] == [("Ann", "First"), ("Bob", "raw"), ("?", "Third")]
    assert comments.calls[1].request.url.params["startAt"] == "2"


@respx.mock
def test_ticket_unassigned_and_children_capped(cloud):
    respx.get(f"{CLOUD}/rest/api/2/field").respond(json=FIELDS)
    respx.get(f"{CLOUD}/rest/api/2/issue/BI-1").respond(json=full_issue(assignee=None, parent=None, customfield_10036=None))
    many = [issue(f"BI-{i}", "c", status={"name": "x"}, issuetype={"name": "Task"}) for i in range(MAX_CHILDREN)]
    respx.post(f"{CLOUD}/rest/api/3/search/jql").respond(json={"issues": many})
    respx.get(f"{CLOUD}/rest/api/2/issue/BI-1/comment").respond(json={"total": 0, "comments": []})
    t = cloud.ticket("BI-1")
    assert t.fields["Assignee"] == "unassigned"
    assert t.fields["Parent"] == "" and t.fields["Story points"] == ""
    assert t.links[-1] == f"… only the newest {MAX_CHILDREN} children are listed"
    assert t.comments == []


@respx.mock
def test_ticket_dc_epic(dc):
    respx.get(f"{DC}/rest/api/2/field").respond(json=FIELDS)
    data = full_issue(parent=None, issuetype={"name": "Epic"}, customfield_10014="OPHJODTA-1")
    data["key"] = "OPHJODTA-5"
    respx.get(f"{DC}/rest/api/2/issue/OPHJODTA-5").respond(json=data)

    def search(request):
        jql = request.url.params["jql"]
        if jql == "key = OPHJODTA-1":
            return httpx.Response(200, json={"issues": [issue("OPHJODTA-1", "Big epic")]})
        assert jql == "cf[10014] = OPHJODTA-5 ORDER BY created DESC"
        return httpx.Response(200, json={"issues": [issue("OPHJODTA-6", "Story", status={"name": "Open"}, issuetype={"name": "Task"})]})

    respx.get(f"{DC}/rest/api/2/search").mock(side_effect=search)
    respx.get(f"{DC}/rest/api/2/issue/OPHJODTA-5/comment").respond(json={"total": 0, "comments": []})
    t = dc.ticket("OPHJODTA-5")
    assert t.fields["Parent"] == "OPHJODTA-1 Big epic"
    assert t.links[-1] == "child OPHJODTA-6 Story (Open)"


@respx.mock
def test_ticket_dc_non_epic_has_no_children_query(dc):
    respx.get(f"{DC}/rest/api/2/field").respond(json=FIELDS)
    data = full_issue(parent=None, issuelinks=[], subtasks=[])
    respx.get(f"{DC}/rest/api/2/issue/BI-1").respond(json=data)
    search = respx.get(f"{DC}/rest/api/2/search")
    respx.get(f"{DC}/rest/api/2/issue/BI-1/comment").respond(json={"total": 0, "comments": []})
    assert dc.ticket("BI-1").links == []
    assert not search.called


@respx.mock
def test_ticket_not_found_and_errors(cloud):
    respx.get(f"{CLOUD}/rest/api/2/field").respond(json=[])
    respx.get(f"{CLOUD}/rest/api/2/issue/BI-404").respond(404)
    with pytest.raises(LookupError, match="BI-404 not found on cloud"):
        cloud.ticket("BI-404")
    respx.get(f"{CLOUD}/rest/api/2/issue/BI-500").respond(500, text="boom")
    with pytest.raises(RuntimeError, match="500: boom"):
        cloud.ticket("BI-500")


@respx.mock
@pytest.mark.parametrize(
    "message",
    [
        "Field 'customfield_10036' cannot be set.",
        "Field is not on the appropriate screen, or unknown.",
    ],
)
def test_create_defers_on_either_jira_wording(cloud, message):
    mock_myself(CLOUD)
    respx.post(f"{CLOUD}/rest/api/2/issue").mock(side_effect=[
        httpx.Response(400, json={"errors": {"customfield_10036": message}}),
        httpx.Response(201, json={"key": "BI-7"}),
    ])
    put = respx.put(f"{CLOUD}/rest/api/2/issue/BI-7").respond(204)
    created = cloud.create(Draft("T", "D", "Task", details=Details(story_points=5.0)), PROFILE)
    assert body(put.calls.last.request) == {"fields": {"customfield_10036": 5.0}}
    assert created.warnings == []


# --- adding to a ticket -------------------------------------------------------

def jira_ticket(url=f"{CLOUD}/browse/BI-5"):
    from tiket.backends import Ticket

    return Ticket("BI-5", "t", url, "Task", "Open", "BI")


@respx.mock
def test_add_comment_posts_wiki_markup(cloud):
    route = respx.post(f"{CLOUD}/rest/api/2/issue/BI-5/comment").respond(201, json={"id": "10042"})
    url = cloud.add_comment(jira_ticket(), "**Done**\n\n- `x`")
    assert body(route.calls.last.request) == {"body": "*Done*\n\n* {{x}}"}
    assert url == f"{CLOUD}/browse/BI-5?focusedCommentId=10042"


@respx.mock
@pytest.mark.parametrize(
    "current, expected",
    [
        ("h3. Tausta\nVanha {color:red}teksti{color}\n\n", "h3. Tausta\nVanha {color:red}teksti{color}\n\nh3. Lisätiedot\n* uusi"),
        (None, "h3. Lisätiedot\n* uusi"),
        ("  \n", "h3. Lisätiedot\n* uusi"),
    ],
)
def test_append_description_keeps_existing_wiki_markup(dc, current, expected):
    get = respx.get(f"{DC}/rest/api/2/issue/BI-5").respond(json={"fields": {"description": current}})
    put = respx.put(f"{DC}/rest/api/2/issue/BI-5").respond(204)
    assert dc.append_description(jira_ticket(f"{DC}/browse/BI-5"), "### Lisätiedot\n- uusi") == f"{DC}/browse/BI-5"
    assert get.calls.last.request.url.params["fields"] == "description"
    assert body(put.calls.last.request) == {"fields": {"description": expected}}


@respx.mock
def test_append_description_errors_are_raised(cloud):
    respx.get(f"{CLOUD}/rest/api/2/issue/BI-5").respond(json={"fields": {"description": "x"}})
    respx.put(f"{CLOUD}/rest/api/2/issue/BI-5").respond(403, text="no edit permission")
    with pytest.raises(RuntimeError, match="403: no edit permission"):
        cloud.append_description(jira_ticket(), "y")
