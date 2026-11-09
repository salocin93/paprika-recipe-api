"""Category UID -> name resolution.

Paprika's Markdown export stores categories as UIDs, not names. Names live in
your Paprika cloud account. ``fetch_categories.py`` downloads them into a
``category_map.yaml``; this module loads that file (or any hand-written
``{uid: name}`` mapping) so reports can show readable names.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

PAPRIKA_CATEGORIES_URL = "https://www.paprikaapp.com/api/v1/sync/categories/"


@dataclass
class CategoryMap:
    names: dict[str, str] = field(default_factory=dict)
    parents: dict[str, str] = field(default_factory=dict)
    source: Path | None = None

    def name(self, uid: str) -> str:
        return self.names.get(uid, uid)

    def is_resolved(self, uid: str) -> bool:
        return uid in self.names

    def display(self, uid: str) -> str:
        """Name, prefixed with its parent (``Parent › Child``) if nested."""
        name = self.name(uid)
        parent = self.parents.get(uid)
        return f"{self.name(parent)} › {name}" if parent else name

    def __len__(self) -> int:
        return len(self.names)

    @classmethod
    def load(cls, path: Path | None) -> "CategoryMap":
        """Load a mapping file. Missing file -> empty map (UIDs shown as-is).

        Accepted formats (YAML or JSON):
          * ``{uid: name, ...}``
          * ``[{uid: ..., name: ..., parent_uid: ...}, ...]`` (Paprika API shape)
        """
        if path is None or not path.exists():
            return cls()
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"Could not parse category map {path}: {exc}") from exc

        cm = cls(source=path)
        if isinstance(data, dict) and "result" in data:
            data = data["result"]
        if isinstance(data, dict):
            cm.names = {str(k): str(v) for k, v in data.items() if v is not None}
        elif isinstance(data, list):
            for item in data:
                if not isinstance(item, dict) or "uid" not in item:
                    continue
                cm.names[str(item["uid"])] = str(item.get("name") or item["uid"])
                if item.get("parent_uid"):
                    cm.parents[str(item["uid"])] = str(item["parent_uid"])
        else:
            raise ValueError(f"Unsupported category map format in {path}")
        return cm


def fetch_paprika_categories(email: str, password: str, timeout: float = 30) -> list[dict]:
    """Fetch the category list from the Paprika sync API (HTTP basic auth)."""
    token = base64.b64encode(f"{email}:{password}".encode()).decode()
    req = urllib.request.Request(
        PAPRIKA_CATEGORIES_URL,
        headers={"Authorization": f"Basic {token}", "User-Agent": "paprika-recipe-harness"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise PermissionError("Paprika rejected the credentials (HTTP 401)") from exc
        raise
    result = payload.get("result")
    if not isinstance(result, list):
        raise ValueError(f"Unexpected response from Paprika: {str(payload)[:200]}")
    return result


def write_category_map(categories: list[dict], path: Path) -> None:
    rows = [
        {"uid": c["uid"], "name": c.get("name"), "parent_uid": c.get("parent_uid")}
        for c in sorted(categories, key=lambda c: (c.get("name") or "").casefold())
    ]
    path.write_text(
        yaml.safe_dump(rows, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
