import difflib
import re

import httpx

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

PROFILE_SAMPLE = 50
MAX_CHILDREN = 100  # an epic's children listed by `tiket show`; Cloud returns at most 100 per page
TICKET_FIELDS = [
    "summary", "issuetype", "status", "project", "priority", "assignee", "reporter", "created",
    "updated", "resolution", "labels", "components", "fixVersions", "parent", "description",
    "issuelinks", "subtasks", "attachment",
]
# Fields every ticket needs; anything else we set is a "detail" that may be dropped if Jira rejects it
BASE_FIELDS = {"project", "summary", "description", "issuetype"}


_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?(\s*:?-{3,}:?\s*\|)+\s*(:?-{3,}:?\s*)?$")


def md_to_jira(md: str) -> str:
    """Convert the subset of markdown the LLM produces into Jira wiki markup."""
    out: list[str] = []
    in_code = False
    lines = md.splitlines()
    for i, line in enumerate(lines):
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
        if _TABLE_SEP.match(line):
            continue
        if _TABLE_ROW.match(line):
            header = i + 1 < len(lines) and _TABLE_SEP.match(lines[i + 1])
            bar = "||" if header else "|"
            cells = [_inline(c.strip()) or " " for c in line.strip().strip("|").split("|")]
            out.append(bar + bar.join(cells) + bar)
        elif m := re.match(r"^(#{1,6})\s+(.*)", line):
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
    s = re.sub(
        r"\*\*(.+?)\*\*|__(.+?)__", lambda m: "\x02" + (m[1] or m[2]) + "\x02", s
    )
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
                raise ValueError(
                    f"Site '{site.name}' is Jira Cloud and needs `email` in config"
                )
            auth, headers = (site.email, site.token), {}
        else:
            auth, headers = None, {"Authorization": f"Bearer {site.token}"}
        self.http = httpx.Client(
            base_url=site.url,
            auth=auth,
            timeout=30,
            headers={"Accept": "application/json", **headers},
        )

    def _req(self, method: str, path: str, **kw) -> dict | list:
        r = self.http.request(method, path, **kw)
        if r.is_error:
            raise RuntimeError(
                f"Jira {method} {path} -> {r.status_code}: {r.text[:1000]}"
            )
        return r.json() if r.content else {}

    def _myself(self) -> dict:
        return self._req("GET", "/rest/api/2/myself")

    def check(self) -> str:
        me = self._myself()
        return me.get("emailAddress") or me.get("displayName") or me.get("name", "?")

    def list_projects(self) -> list[tuple[str, str]]:
        return [(p["key"], p["name"]) for p in self._req("GET", "/rest/api/2/project")]

    def issue_types(self) -> list[str]:
        data = self._req(
            "GET", f"/rest/api/2/issue/createmeta/{self.project.project}/issuetypes"
        )
        types = data.get("issueTypes") or data.get("values") or []
        return [t["name"] for t in types if not t.get("subtask")]

    def _search(self, jql: str, n: int, fields: list[str], **extra) -> list[dict]:
        if self.cloud:
            data = self._req(
                "POST",
                "/rest/api/3/search/jql",
                json={"jql": jql, "maxResults": n, "fields": fields, **extra},
            )
        else:
            data = self._req(
                "GET",
                "/rest/api/2/search",
                params={
                    "jql": jql,
                    "maxResults": n,
                    "fields": ",".join(fields),
                    **extra,
                },
            )
        return data.get("issues", [])

    def my_items(
        self, project: str | None, text: str | None, include_done: bool, limit: int
    ) -> list[Item]:
        clauses = ["assignee = currentUser()"]
        if not include_done:
            clauses.append("statusCategory != Done")
        if project:
            clauses.append(f"project = {_jql_str(project)}")
        if text:
            clauses.append(f"summary ~ {_jql_str(text)}")
        jql = " AND ".join(clauses) + " ORDER BY updated DESC"
        try:
            issues = self._search(
                jql, limit, ["summary", "status", "issuetype", "project", "updated"]
            )
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
            type_filter = (
                " AND issuetype in ("
                + ", ".join(f'"{t}"' for t in self.project.types)
                + ")"
            )
        jql = f'project = "{self.project.project}"{type_filter} ORDER BY created DESC'
        fields = ["summary", "issuetype", "description"]
        issues = self._search(jql, n, fields, expand="renderedFields")
        return [
            Example(
                ref=i["key"],
                title=i["fields"]["summary"],
                issue_type=i["fields"]["issuetype"]["name"],
                description=html_to_text(
                    (i.get("renderedFields") or {}).get("description")
                ),
            )
            for i in issues
        ]

    # --- ticket details ------------------------------------------------------

    def _field_ids(self) -> tuple[dict[str, str], list[str]]:
        """Custom field ids differ per Jira instance; find them by type."""
        ids: dict[str, str] = {}
        story_point_fields = []
        for f in self._req("GET", "/rest/api/2/field"):
            custom = (f.get("schema") or {}).get("custom") or ""
            if custom.endswith(":gh-sprint"):
                ids.setdefault("sprint", f["id"])
            elif custom.endswith(":gh-epic-link"):
                ids.setdefault("epic_link", f["id"])
            elif "story point" in f["name"].lower() and custom.endswith(
                (":float", ":jsw-story-points")
            ):
                story_point_fields.append(f["id"])
        return ids, story_point_fields

    def field_profile(self) -> Profile:
        ids, story_point_fields = self._field_ids()
        fields = ["summary", "issuetype", "parent", "priority", "components", "labels"]
        fields += [*ids.values(), *story_point_fields]
        jql = (
            f"project = {_jql_str(self.project.project)} AND "
            "(assignee = currentUser() OR reporter = currentUser()) ORDER BY created DESC"
        )
        issues = [
            i
            for i in self._search(jql, PROFILE_SAMPLE, fields)
            if not i["fields"]["issuetype"].get("subtask")
        ]
        if not issues:
            return Profile(field_ids=ids)

        # Several "story points" fields may exist; use the one this project fills in
        def filled(fid: str) -> int:
            return sum(1 for i in issues if i["fields"].get(fid) is not None)

        if story_point_fields and filled(best := max(story_point_fields, key=filled)):
            ids["story_points"] = best

        parent_keys = []
        for i in issues:
            f = i["fields"]
            key = (f.get("parent") or {}).get("key") if self.cloud else None
            if key := key or f.get(ids.get("epic_link", "")):
                parent_keys.append(key)
        open_parents = self._open_issue_labels(set(parent_keys))

        def values(name: str) -> list[dict]:
            return [v for i in issues for v in i["fields"].get(name) or []]

        sp = ids.get("story_points")
        return Profile(
            sample=len(issues),
            parents=rank([(k, open_parents[k]) for k in parent_keys if k in open_parents]),
            components=rank([(c["name"], c["name"]) for c in values("components")]),
            labels=rank([(label, label) for label in values("labels")]),
            priorities=rank(
                [(p["name"], p["name"]) for i in issues if (p := i["fields"].get("priority"))]
            ),
            points=[
                (i["fields"]["summary"], float(i["fields"][sp]))
                for i in issues
                if sp and i["fields"].get(sp) is not None
            ],
            sprint_hint=self._sprint_hint(issues, ids.get("sprint")),
            field_ids=ids,
        )

    def _open_issue_labels(self, keys: set[str]) -> dict[str, str]:
        """{key: "KEY summary"} for the given issues that are not done."""
        if not keys:
            return {}
        jql = f"key in ({', '.join(sorted(keys))}) AND statusCategory != Done"
        try:
            issues = self._search(jql, len(keys), ["summary"])
        except RuntimeError:  # e.g. a parent was deleted or moved; keep them unlabelled
            return {k: k for k in keys}
        return {i["key"]: f"{i['key']} {i['fields']['summary']}" for i in issues}

    def _sprint_hint(self, issues: list[dict], sprint_field: str | None) -> dict:
        """Board and name of the newest sprint the user's recent tickets were in."""
        for i in issues:
            sprints = [_parse_sprint(s) for s in i["fields"].get(sprint_field or "") or []]
            if sprints := [s for s in sprints if s.get("board")]:
                latest = max(sprints, key=lambda s: int(s["id"]))
                return {"board": int(latest["board"]), "name": latest["name"]}
        return {}

    def current_sprint(self, profile: Profile) -> Choice | None:
        hint = profile.sprint_hint
        if not hint.get("board"):
            return None
        try:
            data = self._req(
                "GET",
                f"/rest/agile/1.0/board/{hint['board']}/sprint",
                params={"state": "active,future", "maxResults": 50},
            )
        except RuntimeError:
            return None
        sprints = data.get("values", [])
        # Boards can show sprints from other boards; prefer the ones that belong to this board
        own = [s for s in sprints if s.get("originBoardId", hint["board"]) == hint["board"]]
        sprints = own or sprints
        pool = [s for s in sprints if s["state"] == "active"] or [
            s for s in sprints if s["state"] == "future"
        ]
        if not pool:
            return None
        family = _sprint_family(hint["name"])

        def score(s: dict) -> tuple:
            similarity = difflib.SequenceMatcher(None, _sprint_family(s["name"]), family).ratio()
            return (-round(similarity, 1), s.get("startDate") or "9999", s["id"])

        best = min(pool, key=score)
        when = "active" if best["state"] == "active" else "next"
        return Choice(str(best["id"]), f"{best['name']} ({when})")

    def create(
        self, draft: Draft, profile: Profile, extra_fields: dict | None = None
    ) -> Created:
        d = draft.details
        ids = profile.field_ids
        description_md = draft.description
        if draft.acceptance_criteria:
            description_md += "\n\n" + draft.acceptance_criteria
        fields = {
            "project": {"key": self.project.project},
            "summary": draft.title,
            "description": md_to_jira(description_md),
            "issuetype": {"name": draft.issue_type},
        }
        if d.parent:
            if self.cloud:
                fields["parent"] = {"key": d.parent.value}
            elif "epic_link" in ids:
                fields[ids["epic_link"]] = d.parent.value
        if d.story_points is not None and "story_points" in ids:
            fields[ids["story_points"]] = d.story_points
        if d.priority:
            fields["priority"] = {"name": d.priority}
        if d.components:
            fields["components"] = [{"name": c} for c in d.components]
        if d.labels:
            fields["labels"] = d.labels
        if self.project.assign_to_me:
            me = self._myself()
            fields["assignee"] = (
                {"accountId": me["accountId"]} if self.cloud else {"name": me["name"]}
            )
        fields |= self.project.extra_fields | (extra_fields or {})

        names = {v: k.replace("_", " ") for k, v in ids.items()}
        key, deferred, warnings = self._create_issue(fields, names)
        if deferred:
            try:
                self._req("PUT", f"/rest/api/2/issue/{key}", json={"fields": deferred})
            except RuntimeError as e:
                labels = ", ".join(names.get(k, k) for k in deferred)
                warnings.append(f"Could not set {labels}: {e}")
        if d.sprint:
            try:
                self._req(
                    "POST",
                    f"/rest/agile/1.0/sprint/{d.sprint.value}/issue",
                    json={"issues": [key]},
                )
            except RuntimeError as e:
                warnings.append(f"Could not add to sprint {d.sprint.label}: {e}")
        return Created(f"{self.site.url}/browse/{key}", warnings, key)

    def _create_issue(
        self, fields: dict, names: dict[str, str]
    ) -> tuple[str, dict, list[str]]:
        """POST the issue. Details that aren't on the create screen are returned for a follow-up edit;
        details Jira rejects outright are dropped with a warning, so the ticket itself still gets created."""
        deferred: dict = {}
        warnings: list[str] = []
        for _ in range(4):
            r = self.http.post("/rest/api/2/issue", json={"fields": fields})
            if r.is_success:
                return r.json()["key"], deferred, warnings
            errors = (r.json().get("errors") or {}) if r.status_code == 400 else {}
            if missing := [(k, v) for k, v in errors.items() if "required" in v.lower()]:
                raise MissingFieldsError(missing, r.text)
            details = [k for k in errors if k in fields and k not in BASE_FIELDS]
            if not details:
                break
            for k in details:
                if "appropriate screen" in errors[k] or "cannot be set" in errors[k]:
                    deferred[k] = fields.pop(k)
                else:
                    fields.pop(k)
                    warnings.append(f"Skipped {names.get(k, k)}: {errors[k]}")
        raise RuntimeError(f"Jira POST /rest/api/2/issue -> {r.status_code}: {r.text[:1000]}")


    # --- one ticket in full ---------------------------------------------------

    def ticket(self, ref: str) -> Ticket:
        key = ref.upper()
        ids, story_point_fields = self._field_ids()
        r = self.http.get(
            f"/rest/api/2/issue/{key}",
            params={
                "fields": ",".join(TICKET_FIELDS + list(ids.values()) + story_point_fields),
                "expand": "renderedFields",
            },
        )
        if r.status_code == 404:
            raise LookupError(f"{key} not found on {self.site.name}")
        if r.is_error:
            raise RuntimeError(f"Jira GET /rest/api/2/issue/{key} -> {r.status_code}: {r.text[:1000]}")
        issue = r.json()
        f, rendered = issue["fields"], issue.get("renderedFields") or {}

        def name(v: dict | None) -> str:
            return (v or {}).get("displayName") or (v or {}).get("name") or ""

        parent = ""
        if p := f.get("parent"):
            parent = f"{p['key']} {p['fields']['summary']}"
        elif epic := f.get(ids.get("epic_link", "")):
            parent = self._issue_label(epic)
        sprints = [_parse_sprint(s) for s in f.get(ids.get("sprint", "")) or []]
        points = next((f[fid] for fid in story_point_fields if f.get(fid) is not None), None)
        fields = {
            "Assignee": name(f.get("assignee")) or "unassigned",
            "Reporter": name(f.get("reporter")),
            "Priority": name(f.get("priority")),
            "Resolution": name(f.get("resolution")),
            "Parent": parent,
            "Story points": f"{points:g}" if isinstance(points, (int, float)) else "",
            "Sprint": _sprint_summary(sprints),
            "Components": ", ".join(c["name"] for c in f.get("components") or []),
            "Labels": ", ".join(f.get("labels") or []),
            "Fix versions": ", ".join(v["name"] for v in f.get("fixVersions") or []),
        }

        links = []
        for link in f.get("issuelinks") or []:
            if other := link.get("outwardIssue"):
                links.append(f"{link['type']['outward']} {other['key']} {other['fields']['summary']}")
            elif other := link.get("inwardIssue"):
                links.append(f"{link['type']['inward']} {other['key']} {other['fields']['summary']}")
        links += [f"subtask {t['key']} {t['fields']['summary']} ({t['fields']['status']['name']})" for t in f.get("subtasks") or []]
        links += self._children(key, f["issuetype"]["name"], ids)

        return Ticket(
            ref=issue["key"],
            title=f["summary"],
            url=f"{self.site.url}/browse/{issue['key']}",
            issue_type=f["issuetype"]["name"],
            status=f["status"]["name"],
            project=f["project"]["key"],
            created=f.get("created", ""),
            updated=f.get("updated", ""),
            fields=fields,
            description=html_to_text(rendered.get("description")),
            links=links,
            attachments=[f"{a['filename']} ({a['content']})" for a in f.get("attachment") or []],
            comments=self._comments(issue["key"]),
        )

    # --- adding to a ticket ----------------------------------------------------

    def add_comment(self, ticket: Ticket, text: str) -> str:
        comment = self._req(
            "POST", f"/rest/api/2/issue/{ticket.ref}/comment", json={"body": md_to_jira(text)}
        )
        return f"{ticket.url}?focusedCommentId={comment['id']}"

    def append_description(self, ticket: Ticket, text: str) -> str:
        # v2 returns the raw wiki markup, so the existing text goes back exactly as it was
        current = self._req(
            "GET", f"/rest/api/2/issue/{ticket.ref}", params={"fields": "description"}
        )["fields"].get("description") or ""
        description = (current.rstrip() + "\n\n" if current.strip() else "") + md_to_jira(text)
        self._req("PUT", f"/rest/api/2/issue/{ticket.ref}", json={"fields": {"description": description}})
        return ticket.url

    def _issue_label(self, key: str) -> str:
        try:
            issues = self._search(f"key = {key}", 1, ["summary"])
        except RuntimeError:
            return key
        return f"{key} {issues[0]['fields']['summary']}" if issues else key

    def _children(self, key: str, issue_type: str, ids: dict) -> list[str]:
        """Issues under an epic / parent (subtasks come with the issue itself)."""
        if self.cloud:
            jql = f"parent = {key}"
        elif "epic_link" in ids and issue_type.lower() == "epic":
            jql = f"cf[{ids['epic_link'].split('_')[-1]}] = {key}"
        else:
            return []
        try:
            issues = self._search(jql + " ORDER BY created DESC", MAX_CHILDREN, ["summary", "status", "issuetype"])
        except RuntimeError:
            return []
        children = [
            f"child {i['key']} {i['fields']['summary']} ({i['fields']['status']['name']})"
            for i in issues
            if not i["fields"]["issuetype"].get("subtask")  # already listed as subtasks
        ]
        if len(issues) >= MAX_CHILDREN:
            children.append(f"… only the newest {MAX_CHILDREN} children are listed")
        return children

    def _comments(self, key: str) -> list[Comment]:
        comments: list[Comment] = []
        while True:
            data = self._req(
                "GET",
                f"/rest/api/2/issue/{key}/comment",
                params={"expand": "renderedBody", "startAt": len(comments), "maxResults": 100},
            )
            page = data.get("comments", [])
            comments += [
                Comment(
                    author=(c.get("author") or {}).get("displayName", "?"),
                    created=c.get("created", ""),
                    body=html_to_text(c.get("renderedBody")) or c.get("body", ""),
                )
                for c in page
            ]
            if not page or len(comments) >= data.get("total", 0):
                return comments


def _parse_sprint(s: dict | str) -> dict:
    """Cloud returns sprint objects, Data Center a "...Sprint@1a2b[id=1,name=x,...]" string."""
    if isinstance(s, dict):
        return {"id": s.get("id"), "name": s.get("name", ""), "board": s.get("boardId")}
    attrs = dict(re.findall(r"(\w+)=([^,\]]*)", s))
    return {"id": attrs.get("id", 0), "name": attrs.get("name", ""), "board": attrs.get("rapidViewId")}


def _sprint_summary(sprints: list[dict]) -> str:
    if not sprints:
        return ""
    *earlier, latest = sorted(sprints, key=lambda s: int(s["id"] or 0))
    return latest["name"] + (f" (carried over from {len(earlier)} earlier)" if earlier else "")


def _sprint_family(name: str) -> str:
    # "JOD TA Ylläpito Sprint37" and "...Sprint38" are the same series
    return re.sub(r"\d+", "#", name.lower())
