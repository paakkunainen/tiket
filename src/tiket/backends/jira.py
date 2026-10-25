import re

import httpx

from ..config import Project, Site
from .base import Example, Item, MissingFieldsError, html_to_text


def md_to_jira(md: str) -> str:
    """Convert the subset of markdown the LLM produces into Jira wiki markup."""
    out: list[str] = []
    in_code = False
    for line in md.splitlines():
        if line.strip().startswith("```"):
            if in_code:
                out.append("{code}")
            else:
                lang = line.strip()[3:].strip()
                out.append(f"{{code:{lang}}}" if lang else "{code}")
            in_code = not in_code
            continue
        if in_code:
            out.append(line)
            continue
        if m := re.match(r"^(#{1,6})\s+(.*)", line):
            out.append(f"h{len(m[1])}. {_inline(m[2])}")
        elif m := re.match(r"^(\s*)[-*+]\s+(.*)", line):
            out.append("*" * (len(m[1].expandtabs(2)) // 2 + 1) + " " + _inline(m[2]))
        elif m := re.match(r"^(\s*)\d+[.)]\s+(.*)", line):
            out.append("#" * (len(m[1].expandtabs(2)) // 2 + 1) + " " + _inline(m[2]))
        elif re.match(r"^\s*([-*_])\s*\1\s*\1[\s\1]*$", line):
            out.append("----")
        else:
            out.append(_inline(line))
    if in_code:
        out.append("{code}")
    return "\n".join(out)


def _inline(s: str) -> str:
    codes: list[str] = []

    def stash(m: re.Match) -> str:
        codes.append(m[1])
        return f"\x01{len(codes) - 1}\x01"

    s = re.sub(r"`([^`]+)`", stash, s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"[\1|\2]", s)
    s = re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: "\x02" + (m[1] or m[2]) + "\x02", s)
    s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"_\1_", s)
    s = s.replace("\x02", "*")
    return re.sub(r"\x01(\d+)\x01", lambda m: "{{" + codes[int(m[1])] + "}}", s)


def _jql_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


class JiraBackend:
    separate_acceptance_criteria = False

    def __init__(self, site: Site, project: Project | None = None):
        self.site = site
        self.project = project
        self.cloud = site.kind == "jira_cloud"
        if self.cloud:
            if not site.email:
                raise ValueError(f"Site '{site.name}' is Jira Cloud and needs `email` in config")
            auth, headers = (site.email, site.token), {}
        else:
            auth, headers = None, {"Authorization": f"Bearer {site.token}"}
        self.http = httpx.Client(
            base_url=site.url, auth=auth, timeout=30,
            headers={"Accept": "application/json", **headers},
        )

    def _req(self, method: str, path: str, **kw) -> dict | list:
        r = self.http.request(method, path, **kw)
        if r.status_code == 400 and method == "POST" and path.endswith("/issue"):
            errors = r.json().get("errors") or {}
            if missing := [(k, v) for k, v in errors.items() if "required" in v.lower()]:
                raise MissingFieldsError(missing, r.text)
        if r.is_error:
            raise RuntimeError(f"Jira {method} {path} -> {r.status_code}: {r.text[:1000]}")
        return r.json() if r.content else {}

    def _myself(self) -> dict:
        return self._req("GET", "/rest/api/2/myself")

    def check(self) -> str:
        me = self._myself()
        return me.get("emailAddress") or me.get("displayName") or me.get("name", "?")

    def list_projects(self) -> list[tuple[str, str]]:
        return [(p["key"], p["name"]) for p in self._req("GET", "/rest/api/2/project")]

    def issue_types(self) -> list[str]:
        data = self._req("GET", f"/rest/api/2/issue/createmeta/{self.project.project}/issuetypes")
        types = data.get("issueTypes") or data.get("values") or []
        return [t["name"] for t in types if not t.get("subtask")]

    def _search(self, jql: str, n: int, fields: list[str], **extra) -> list[dict]:
        if self.cloud:
            data = self._req("POST", "/rest/api/3/search/jql",
                             json={"jql": jql, "maxResults": n, "fields": fields, **extra})
        else:
            data = self._req("GET", "/rest/api/2/search",
                             params={"jql": jql, "maxResults": n, "fields": ",".join(fields), **extra})
        return data.get("issues", [])

    def my_items(self, project: str | None, text: str | None, include_done: bool, limit: int) -> list[Item]:
        clauses = ["assignee = currentUser()"]
        if not include_done:
            clauses.append("statusCategory != Done")
        if project:
            clauses.append(f"project = {_jql_str(project)}")
        if text:
            clauses.append(f"summary ~ {_jql_str(text)}")
        jql = " AND ".join(clauses) + " ORDER BY updated DESC"
        try:
            issues = self._search(jql, limit, ["summary", "status", "issuetype", "project", "updated"])
        except RuntimeError as e:
            if project and "does not exist" in str(e):
                return []  # the project lives on another site
            raise
        return [
            Item(
                ref=i["key"],
                title=f["summary"],
                issue_type=f["issuetype"]["name"],
                status=f["status"]["name"],
                project=f["project"]["key"],
                updated=f["updated"],
                url=f"{self.site.url}/browse/{i['key']}",
                done=f["status"].get("statusCategory", {}).get("key") == "done",
            )
            for i in issues
            for f in [i["fields"]]
        ]

    def recent_examples(self, n: int, filter_types: bool = True) -> list[Example]:
        type_filter = ""
        if filter_types and self.project.types:
            type_filter = " AND issuetype in (" + ", ".join(f'"{t}"' for t in self.project.types) + ")"
        jql = f'project = "{self.project.project}"{type_filter} ORDER BY created DESC'
        fields = ["summary", "issuetype", "description"]
        issues = self._search(jql, n, fields, expand="renderedFields")
        return [
            Example(
                ref=i["key"],
                title=i["fields"]["summary"],
                issue_type=i["fields"]["issuetype"]["name"],
                description=html_to_text((i.get("renderedFields") or {}).get("description")),
            )
            for i in issues
        ]

    def create(self, title: str, description_md: str, issue_type: str,
               acceptance_criteria_md: str = "", extra_fields: dict | None = None) -> str:
        if acceptance_criteria_md:
            description_md += "\n\n" + acceptance_criteria_md
        fields = {
            "project": {"key": self.project.project},
            "summary": title,
            "description": md_to_jira(description_md),
            "issuetype": {"name": issue_type},
            **self.project.extra_fields,
            **(extra_fields or {}),
        }
        if self.project.assign_to_me:
            me = self._myself()
            fields["assignee"] = {"accountId": me["accountId"]} if self.cloud else {"name": me["name"]}
        key = self._req("POST", "/rest/api/2/issue", json={"fields": fields})["key"]
        return f"{self.site.url}/browse/{key}"
