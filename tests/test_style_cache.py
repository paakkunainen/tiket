import json
import time

from tiket import style_cache
from tiket.backends import Choice, Example, Profile


class CountingBackend:
    def __init__(self, filtered=None, unfiltered=None, profile=None):
        self.filtered = filtered if filtered is not None else [Example("t", "Task", "d", ref="BI-1")]
        self.unfiltered = unfiltered or []
        self.profile = profile or Profile(sample=1, parents=[Choice("BI-1", "BI-1 x", 1)])
        self.calls = []

    def recent_examples(self, n, filter_types=True):
        self.calls.append(("examples", n, filter_types))
        return self.filtered if filter_types else self.unfiltered

    def field_profile(self):
        self.calls.append(("profile",))
        return self.profile


def test_examples_are_cached(cloud_project):
    backend = CountingBackend()
    first = style_cache.get_examples(cloud_project, backend, 5)
    second = style_cache.get_examples(cloud_project, backend, 5)
    assert first == second == backend.filtered
    assert backend.calls == [("examples", 5, True)]
    path = style_cache.CACHE_DIR / "cloud-BI.json"
    assert json.loads(path.read_text())["n"] == 5


def test_examples_refetched_when_refreshing_expired_or_n_changes(cloud_project, monkeypatch):
    backend = CountingBackend()
    style_cache.get_examples(cloud_project, backend, 5)
    style_cache.get_examples(cloud_project, backend, 5, refresh=True)
    style_cache.get_examples(cloud_project, backend, 8)
    later = time.time() + style_cache.TTL_SECONDS + 1
    monkeypatch.setattr(style_cache.time, "time", lambda: later)
    style_cache.get_examples(cloud_project, backend, 8)
    assert len(backend.calls) == 4


def test_cache_key_includes_types(cloud_project):
    backend = CountingBackend()
    style_cache.get_examples(cloud_project, backend, 5)
    cloud_project.types = ["Bug", "User Story"]
    style_cache.get_examples(cloud_project, backend, 5)
    assert len(backend.calls) == 2
    assert (style_cache.CACHE_DIR / "cloud-BI-Bug-User_Story.json").is_file()


def test_falls_back_to_unfiltered_examples(cloud_project):
    cloud_project.types = ["Virhe"]
    backend = CountingBackend(filtered=[], unfiltered=[Example("x", "Tehtävä", "d")])
    assert style_cache.get_examples(cloud_project, backend, 5) == backend.unfiltered
    assert backend.calls == [("examples", 5, True), ("examples", 5, False)]


def test_no_fallback_without_type_filter(cloud_project):
    backend = CountingBackend(filtered=[])
    assert style_cache.get_examples(cloud_project, backend, 5) == []
    assert backend.calls == [("examples", 5, True)]


def test_profile_is_cached(cloud_project, monkeypatch):
    backend = CountingBackend()
    assert style_cache.get_profile(cloud_project, backend) == backend.profile
    assert style_cache.get_profile(cloud_project, backend) == backend.profile
    assert backend.calls == [("profile",)]
    assert (style_cache.CACHE_DIR / "cloud-BI.profile.json").is_file()

    style_cache.get_profile(cloud_project, backend, refresh=True)
    later = time.time() + style_cache.TTL_SECONDS + 1
    monkeypatch.setattr(style_cache.time, "time", lambda: later)
    style_cache.get_profile(cloud_project, backend)
    assert len(backend.calls) == 3
