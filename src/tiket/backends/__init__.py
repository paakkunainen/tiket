from ..config import Project, Site
from .azure_devops import AzureDevOpsBackend
from .base import Backend, Example, Item, MissingFieldsError
from .jira import JiraBackend


def make_backend(site: Site, project: Project | None = None) -> Backend:
    if site.kind in ("jira_cloud", "jira_dc"):
        return JiraBackend(site, project)
    if site.kind == "azure_devops":
        return AzureDevOpsBackend(site, project)
    raise ValueError(f"Unknown site kind '{site.kind}' (use jira_cloud, jira_dc or azure_devops)")


__all__ = ["Backend", "Example", "Item", "MissingFieldsError", "make_backend"]
