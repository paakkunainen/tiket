import re
from collections import Counter
from datetime import date
from urllib.parse import quote

import httpx
import markdown

from ..config import Project, Site
from .base import (
    Choice,
    Comment,
    Created,
    Draft,
    Example,
    Item,
    MissingFieldsError,
    Profile,
    Ticket,
    html_to_text,
    rank,
)

API = {"api-version": "7.1"}
REPRO_STEPS = "Microsoft.VSTS.TCM.ReproSteps"
ACCEPTANCE = "Microsoft.VSTS.Common.AcceptanceCriteria"
STORY_POINTS = "Microsoft.VSTS.Scheduling.StoryPoints"
PRIORITY = "Microsoft.VSTS.Common.Priority"
PROFILE_SAMPLE = 50
MD_EXTENSIONS = ["fenced_code", "tables", "sane_lists"]
# States in the Completed / Removed categories across the org's processes
DONE_STATES = [
    "Closed",
    "Done",
    "Removed",
    "Inactive",
    "Completed",
    "Julkaistu",
    "Peruttu",
]


class AzureDevOpsBackend:
    separate_acceptance_criteria = True

    def __init__(self, site: Site, project: Project | None = None):
        self.site = site
        self.project = project
        self.http = httpx.Client(
            base_url=site.url,
            auth=("", site.token),
            timeout=30,
            headers={"Accept": "application/json"},
        )

    def _req(self, method: str, path: str, **kw) -> dict:
        r = self.http.request(
            method, path, params={**API, **kw.pop("params", {})}, **kw
        )
        if r.status_code == 400 and (missing := _missing_fields(r)):
            raise MissingFieldsError(missing, r.text)
        if r.is_error:
            raise RuntimeError(
                f"Azure DevOps {method} {path} -> {r.status_code}: {r.text[:1000]}"
            )
        if "json" not in r.headers.get("content-type", ""):
            # An invalid/expired PAT gets redirected to an HTML sign-in page instead of a 401
            raise RuntimeError(
                f"Azure DevOps {method} {path} returned non-JSON (bad or expired PAT?)"
            )
        return r.json()

    def _proj(self) -> str:
        return quote(self.project.project)

    def check(self) -> str:
        data = self._req(
            "GET", "/_apis/connectionData", params={"api-version": "7.1-preview"}
        )
        user = data["authenticatedUser"]
        return (
            user.get("properties", {}).get("Account", {}).get("$value")
            or user["providerDisplayName"]
        )

    def list_projects(self) -> list[tuple[str, str]]:
        return [
            (p["name"], p.get("description", ""))
            for p in self._req("GET", "/_apis/projects")["value"]
        ]

    def issue_types(self) -> list[str]:
        data = self._req("GET", f"/{self._proj()}/_apis/wit/workitemtypes")
        return [t["name"] for t in data["value"] if not t.get("isDisabled")]

    def my_items(
        self, project: str | None, text: str | None, include_done: bool, limit: int
    ) -> list[Item]:
        clauses = ["[System.AssignedTo] = @Me"]
        if not include_done:
            clauses.append(
                "[System.State] NOT IN ("
                + ", ".join(_wiql_str(s) for s in DONE_STATES)
                + ")"
            )
        if project:
            clauses.append(f"[System.TeamProject] = {_wiql_str(project)}")
        if text:
            clauses.append(f"[System.Title] CONTAINS {_wiql_str(text)}")
        wiql = {
            "query": "SELECT [System.Id] FROM WorkItems WHERE "
            + " AND ".join(clauses)
            + " ORDER BY [System.ChangedDate] DESC"
        }
        try:
            refs = self._req(
                "POST", "/_apis/wit/wiql", params={"$top": limit}, json=wiql
            )["workItems"]
        except RuntimeError as e:
            # project does not exist in this org
            if project and "TF200016" in str(e):
                return []
            raise
        if not refs:
            return []
        items = self._req(
            "GET",
            "/_apis/wit/workitems",
            params={
                "ids": ",".join(str(r["id"]) for r in refs),
                "fields": "System.Id,System.Title,System.WorkItemType,System.State,System.TeamProject,System.ChangedDate",
            },
        )["value"]
        by_id = {i["id"]: i["fields"] for i in items}
        return [
            Item(
                ref=str(r["id"]),
                title=f["System.Title"],
                issue_type=f["System.WorkItemType"],
                status=f["System.State"],
                project=f["System.TeamProject"],
                updated=f["System.ChangedDate"],
                url=f"{self.site.url}/{quote(f['System.TeamProject'])}/_workitems/edit/{r['id']}",
                done=f["System.State"] in DONE_STATES,
            )
            for r in refs
            if (f := by_id.get(r["id"]))
        ]

    def recent_examples(self, n: int, filter_types: bool = True) -> list[Example]:
        type_filter = ""
        if filter_types and self.project.types:
            type_filter = (
                " AND [System.WorkItemType] IN ("
                + ", ".join(f"'{t}'" for t in self.project.types)
                + ")"
            )
        wiql = {
            "query": "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project"
            f"{type_filter} ORDER BY [System.CreatedDate] DESC"
        }
        ids = [
            w["id"]
            for w in self._req(
                "POST", f"/{self._proj()}/_apis/wit/wiql", params={"$top": n}, json=wiql
            )["workItems"]
        ]
        if not ids:
            return []
        items = self._req(
            "GET",
            "/_apis/wit/workitems",
            params={
                "ids": ",".join(map(str, ids)),
                "fields": f"System.Id,System.Title,System.WorkItemType,System.Description,{REPRO_STEPS},{ACCEPTANCE}",
            },
        )["value"]
        return [
            Example(
                title=f["System.Title"],
                issue_type=f["System.WorkItemType"],
                description=html_to_text(
                    f.get("System.Description") or f.get(REPRO_STEPS)
                ),
                acceptance_criteria=html_to_text(f.get(ACCEPTANCE)),
                ref=str(f["System.Id"]),
            )
            for f in (i["fields"] for i in items)
        ]

    def _type_fields(self, issue_type: str) -> set[str]:
        data = self._req(
            "GET", f"/{self._proj()}/_apis/wit/workitemtypes/{quote(issue_type)}/fields"
        )
        return {f["referenceName"] for f in data["value"]}

    # --- ticket details ------------------------------------------------------

    def _work_items(self, ids: list[int], fields: list[str]) -> list[dict]:
        if not ids:
            return []
        return self._req(
            "GET",
            "/_apis/wit/workitems",
            params={"ids": ",".join(map(str, ids)), "fields": ",".join(fields)},
        )["value"]

    def field_profile(self) -> Profile:
        wiql = {
            "query": "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project"
            " AND ([System.AssignedTo] = @Me OR [System.CreatedBy] = @Me)"
            " ORDER BY [System.CreatedDate] DESC"
        }
        refs = self._req(
            "POST",
            f"/{self._proj()}/_apis/wit/wiql",
            params={"$top": PROFILE_SAMPLE},
            json=wiql,
        )["workItems"]
        fields = ["System.Title", "System.AreaPath", "System.IterationPath", "System.Parent", STORY_POINTS, PRIORITY]
        items = [i["fields"] for i in self._work_items([r["id"] for r in refs], fields)]
        if not items:
            return Profile()

        parent_ids = [f["System.Parent"] for f in items if f.get("System.Parent")]
        open_parents = {
            p["id"]: f"{p['id']} {p['fields']['System.Title']}"
            for p in self._work_items(sorted(set(parent_ids)), ["System.Title", "System.State"])
            if p["fields"]["System.State"] not in DONE_STATES
        }
        # Iterations live under a team root such as "Esko\1 TK"; remember the one the user works in
        roots = Counter(
            "\\".join(f["System.IterationPath"].split("\\")[:2])
            for f in items
            if f["System.IterationPath"].count("\\") >= 1
        )
        areas = Counter(f["System.AreaPath"] for f in items)
        return Profile(
            sample=len(items),
            parents=rank([(str(p), open_parents[p]) for p in parent_ids if p in open_parents]),
            priorities=rank([(str(f[PRIORITY]), str(f[PRIORITY])) for f in items if f.get(PRIORITY)]),
            points=[(f["System.Title"], float(f[STORY_POINTS])) for f in items if f.get(STORY_POINTS)],
            area=areas.most_common(1)[0][0],
            sprint_hint={"root": roots.most_common(1)[0][0]} if roots else {},
        )

    def current_sprint(self, profile: Profile) -> Choice | None:
        root = profile.sprint_hint.get("root")
        if not root:
            return None
        tree = self._req(
            "GET",
            f"/{self._proj()}/_apis/wit/classificationnodes/Iterations",
            params={"$depth": 6},
        )
        today = date.today().isoformat()
        # (depth, start, path) of iterations under the user's root that run today / start later
        current, upcoming = [], []

        def walk(node: dict, path: str, depth: int) -> None:
            attrs = node.get("attributes") or {}
            start, finish = attrs.get("startDate", "")[:10], attrs.get("finishDate", "")[:10]
            if start and finish and (path + "\\").startswith(root + "\\"):
                if start <= today <= finish:
                    current.append((depth, start, path))
                elif start > today:
                    upcoming.append((-depth, start, path))
            for child in node.get("children", []):
                walk(child, f"{path}\\{child['name']}", depth + 1)

        walk(tree, tree["name"], 0)
        if current:
            path = max(current)[2]  # deepest = the sprint, not the increment around it
            return Choice(path, f"{path.split(chr(92))[-1]} (active)")
        if upcoming:
            path = min(upcoming, key=lambda u: (u[1], u[0]))[2]
            return Choice(path, f"{path.split(chr(92))[-1]} (next)")
        return None

    def create(
        self, draft: Draft, profile: Profile, extra_fields: dict | None = None
    ) -> Created:
        d = draft.details
        type_fields = self._type_fields(draft.issue_type)
        field = REPRO_STEPS if draft.issue_type.lower() == "bug" else "System.Description"
        fields = {"System.Title": draft.title, field: _html(draft.description)}
        if draft.acceptance_criteria and ACCEPTANCE in type_fields:
            fields[ACCEPTANCE] = _html(draft.acceptance_criteria)
        if self.project.assign_to_me:
            fields["System.AssignedTo"] = self.check()

        details: dict = {}
        if d.area:
            details["System.AreaPath"] = d.area
        if d.sprint:
            details["System.IterationPath"] = d.sprint.value
        if d.story_points is not None and STORY_POINTS in type_fields:
            details[STORY_POINTS] = d.story_points
        if d.priority and PRIORITY in type_fields:
            details[PRIORITY] = int(d.priority)
        relations = []
        if d.parent:
            relations.append({
                "rel": "System.LinkTypes.Hierarchy-Reverse",
                "url": f"{self.site.url}/_apis/wit/workItems/{d.parent.value}",
            })
        overrides = self.project.extra_fields | (extra_fields or {})

        def post(with_details: bool) -> dict:
            all_fields = fields | (details if with_details else {}) | overrides
            ops = [{"op": "add", "path": f"/fields/{k}", "value": v} for k, v in all_fields.items()]
            if with_details:
                ops += [{"op": "add", "path": "/relations/-", "value": r} for r in relations]
            return self._req(
                "POST",
                f"/{self._proj()}/_apis/wit/workitems/${quote(draft.issue_type)}",
                json=ops,
                headers={"Content-Type": "application/json-patch+json"},
            )

        warnings = []
        try:
            item = post(with_details=True)
        except MissingFieldsError:
            raise
        except RuntimeError as e:
            if not (details or relations):
                raise
            # A guessed detail was rejected (e.g. parent closed meanwhile); still create the ticket
            item = post(with_details=False)
            warnings.append(f"Created without parent/area/iteration/points/priority: {e}")
        return Created(item["_links"]["html"]["href"], warnings)

    # --- one ticket in full ---------------------------------------------------

    def ticket(self, ref: str) -> Ticket:
        if not ref.isdigit():
            raise LookupError(f"Azure DevOps work item ids are numbers, got '{ref}'")
        try:
            item = self._req("GET", f"/_apis/wit/workitems/{ref}", params={"$expand": "relations"})
        except RuntimeError as e:
            if "TF401232" in str(e) or "404" in str(e):
                raise LookupError(f"Work item {ref} not found on {self.site.name}") from None
            raise
        f = item["fields"]
        project = f["System.TeamProject"]

        def person(v) -> str:
            return v.get("displayName", "") if isinstance(v, dict) else (v or "")

        def num(v) -> str:
            return f"{v:g}" if isinstance(v, (int, float)) else ""

        parent = ""
        links, attachments = [], []
        rels = item.get("relations") or []
        linked = [r for r in rels if "/workItems/" in r["url"]]
        ids = [int(r["url"].rsplit("/", 1)[1]) for r in linked]
        titles = {
            w["id"]: w["fields"]
            for w in self._work_items(sorted(set(ids)), ["System.Title", "System.State", "System.WorkItemType"])
        }
        for r, wid in zip(linked, ids):
            w = titles.get(wid, {})
            label = f"{wid} {w['System.Title']} ({w['System.WorkItemType']}, {w['System.State']})" if w else str(wid)
            kind = (r.get("attributes") or {}).get("name") or r["rel"].rsplit(".", 1)[-1]
            if r["rel"] == "System.LinkTypes.Hierarchy-Reverse":
                parent = label
            else:
                links.append(f"{kind.lower()} {label}")
        for r in rels:
            attrs = r.get("attributes") or {}
            if r["rel"] == "AttachedFile":
                attachments.append(f"{attrs.get('name', 'file')} ({r['url']})")
            elif r["rel"] == "Hyperlink":
                links.append(f"link {r['url']}" + (f" ({attrs['comment']})" if attrs.get("comment") else ""))
            elif r["rel"] == "ArtifactLink":
                links.append(f"{attrs.get('name', 'artifact').lower()} {r['url']}")

        return Ticket(
            ref=str(item["id"]),
            title=f["System.Title"],
            url=f"{self.site.url}/{quote(project)}/_workitems/edit/{item['id']}",
            issue_type=f["System.WorkItemType"],
            status=f["System.State"],
            project=project,
            created=f.get("System.CreatedDate", ""),
            updated=f.get("System.ChangedDate", ""),
            fields={
                "Assignee": person(f.get("System.AssignedTo")) or "unassigned",
                "Created by": person(f.get("System.CreatedBy")),
                "Priority": num(f.get(PRIORITY)),
                "Parent": parent,
                "Story points": num(f.get(STORY_POINTS)),
                "Iteration": f.get("System.IterationPath", ""),
                "Area": f.get("System.AreaPath", ""),
                "Tags": f.get("System.Tags", ""),
                "Reason": f.get("System.Reason", ""),
            },
            description=html_to_text(f.get("System.Description") or f.get(REPRO_STEPS)),
            acceptance_criteria=html_to_text(f.get(ACCEPTANCE)),
            links=links,
            attachments=attachments,
            comments=self._comments(project, item["id"]),
        )

    def _comments(self, project: str, wid: int) -> list[Comment]:
        comments: list[Comment] = []
        token = None
        while True:
            params = {"api-version": "7.1-preview.4", "$top": 200, "order": "asc"}
            if token:
                params["continuationToken"] = token
            data = self._req("GET", f"/{quote(project)}/_apis/wit/workItems/{wid}/comments", params=params)
            comments += [
                Comment(
                    author=(c.get("createdBy") or {}).get("displayName", "?"),
                    created=c.get("createdDate", ""),
                    body=html_to_text(c.get("text")),
                )
                for c in data.get("comments", [])
                if not c.get("isDeleted")
            ]
            token = data.get("continuationToken")
            if not token:
                return comments


def _wiql_str(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _html(md: str) -> str:
    return markdown.markdown(md, extensions=MD_EXTENSIONS)


def _missing_fields(r: httpx.Response) -> list[tuple[str, str]]:
    try:
        errors = r.json().get("customProperties", {}).get("RuleValidationErrors") or []
    except ValueError:
        return []
    missing = []
    for e in errors:
        if "required" in (e.get("fieldStatusFlags") or ""):
            m = re.search(r"field (.+?)\. Error", e.get("errorMessage", ""))
            missing.append(
                (e["fieldReferenceName"], m.group(1) if m else e["fieldReferenceName"])
            )
    return missing
