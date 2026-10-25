import re
from urllib.parse import quote

import httpx
import markdown

from ..config import Project, Site
from .base import Example, Item, MissingFieldsError, html_to_text

API = {"api-version": "7.1"}
REPRO_STEPS = "Microsoft.VSTS.TCM.ReproSteps"
ACCEPTANCE = "Microsoft.VSTS.Common.AcceptanceCriteria"
MD_EXTENSIONS = ["fenced_code", "tables", "sane_lists"]
# States in the Completed / Removed categories across the org's processes
DONE_STATES = ["Closed", "Done", "Removed", "Inactive", "Completed", "Julkaistu", "Peruttu"]


class AzureDevOpsBackend:
    separate_acceptance_criteria = True

    def __init__(self, site: Site, project: Project | None = None):
        self.site = site
        self.project = project
        self.http = httpx.Client(
            base_url=site.url, auth=("", site.token), timeout=30,
            headers={"Accept": "application/json"},
        )

    def _req(self, method: str, path: str, **kw) -> dict:
        r = self.http.request(method, path, params={**API, **kw.pop("params", {})}, **kw)
        if r.status_code == 400 and (missing := _missing_fields(r)):
            raise MissingFieldsError(missing, r.text)
        if r.is_error:
            raise RuntimeError(f"Azure DevOps {method} {path} -> {r.status_code}: {r.text[:1000]}")
        if "json" not in r.headers.get("content-type", ""):
            # An invalid/expired PAT gets redirected to an HTML sign-in page instead of a 401
            raise RuntimeError(f"Azure DevOps {method} {path} returned non-JSON (bad or expired PAT?)")
        return r.json()

    def _proj(self) -> str:
        return quote(self.project.project)

    def check(self) -> str:
        data = self._req("GET", "/_apis/connectionData", params={"api-version": "7.1-preview"})
        user = data["authenticatedUser"]
        return user.get("properties", {}).get("Account", {}).get("$value") or user["providerDisplayName"]

    def list_projects(self) -> list[tuple[str, str]]:
        return [(p["name"], p.get("description", "")) for p in self._req("GET", "/_apis/projects")["value"]]

    def issue_types(self) -> list[str]:
        data = self._req("GET", f"/{self._proj()}/_apis/wit/workitemtypes")
        return [t["name"] for t in data["value"] if not t.get("isDisabled")]

    def my_items(self, project: str | None, text: str | None, include_done: bool, limit: int) -> list[Item]:
        clauses = ["[System.AssignedTo] = @Me"]
        if not include_done:
            clauses.append("[System.State] NOT IN (" + ", ".join(_wiql_str(s) for s in DONE_STATES) + ")")
        if project:
            clauses.append(f"[System.TeamProject] = {_wiql_str(project)}")
        if text:
            clauses.append(f"[System.Title] CONTAINS {_wiql_str(text)}")
        wiql = {"query": "SELECT [System.Id] FROM WorkItems WHERE " + " AND ".join(clauses)
                         + " ORDER BY [System.ChangedDate] DESC"}
        try:
            refs = self._req("POST", "/_apis/wit/wiql", params={"$top": limit}, json=wiql)["workItems"]
        except RuntimeError as e:
            if project and "TF200016" in str(e):  # project does not exist in this org
                return []
            raise
        if not refs:
            return []
        items = self._req("GET", "/_apis/wit/workitems", params={
            "ids": ",".join(str(r["id"]) for r in refs),
            "fields": "System.Id,System.Title,System.WorkItemType,System.State,System.TeamProject,System.ChangedDate",
        })["value"]
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
            type_filter = " AND [System.WorkItemType] IN (" + ", ".join(f"'{t}'" for t in self.project.types) + ")"
        wiql = {"query": "SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = @project"
                         f"{type_filter} ORDER BY [System.CreatedDate] DESC"}
        ids = [w["id"] for w in self._req("POST", f"/{self._proj()}/_apis/wit/wiql",
                                          params={"$top": n}, json=wiql)["workItems"]]
        if not ids:
            return []
        items = self._req("GET", "/_apis/wit/workitems", params={
            "ids": ",".join(map(str, ids)),
            "fields": f"System.Id,System.Title,System.WorkItemType,System.Description,{REPRO_STEPS},{ACCEPTANCE}",
        })["value"]
        return [
            Example(
                title=f["System.Title"],
                issue_type=f["System.WorkItemType"],
                description=html_to_text(f.get("System.Description") or f.get(REPRO_STEPS)),
                acceptance_criteria=html_to_text(f.get(ACCEPTANCE)),
                ref=str(f["System.Id"]),
            )
            for f in (i["fields"] for i in items)
        ]

    def _type_fields(self, issue_type: str) -> set[str]:
        data = self._req("GET", f"/{self._proj()}/_apis/wit/workitemtypes/{quote(issue_type)}/fields")
        return {f["referenceName"] for f in data["value"]}

    def create(self, title: str, description_md: str, issue_type: str,
               acceptance_criteria_md: str = "", extra_fields: dict | None = None) -> str:
        field = REPRO_STEPS if issue_type.lower() == "bug" else "System.Description"
        fields = {"System.Title": title, field: _html(description_md)}
        if acceptance_criteria_md and ACCEPTANCE in self._type_fields(issue_type):
            fields[ACCEPTANCE] = _html(acceptance_criteria_md)
        fields |= self.project.extra_fields | (extra_fields or {})
        if self.project.assign_to_me:
            fields["System.AssignedTo"] = self.check()
        ops = [{"op": "add", "path": f"/fields/{k}", "value": v} for k, v in fields.items()]
        item = self._req(
            "POST", f"/{self._proj()}/_apis/wit/workitems/${quote(issue_type)}",
            json=ops, headers={"Content-Type": "application/json-patch+json"},
        )
        return item["_links"]["html"]["href"]


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
            missing.append((e["fieldReferenceName"], m.group(1) if m else e["fieldReferenceName"]))
    return missing
