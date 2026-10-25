import json
import re
import time

from .backends import Backend, Example
from .config import CACHE_DIR, Project

TTL_SECONDS = 24 * 3600


def get_examples(project: Project, backend: Backend, n: int, refresh: bool = False) -> list[Example]:
    safe_key = re.sub(r"[^\w-]", "_", "-".join([project.project, *project.types]))
    path = CACHE_DIR / f"{project.site.name}-{safe_key}.json"
    if not refresh and path.is_file():
        data = json.loads(path.read_text())
        if time.time() - data["fetched_at"] < TTL_SECONDS and data.get("n") == n:
            return [Example(**e) for e in data["examples"]]
    examples = backend.recent_examples(n)
    if not examples and project.types:
        # No tickets of the allowed types yet: the project's other tickets still show language and tone
        examples = backend.recent_examples(n, filter_types=False)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(
        {"fetched_at": time.time(), "n": n, "examples": [e.to_dict() for e in examples]},
        ensure_ascii=False, indent=2,
    ))
    return examples
