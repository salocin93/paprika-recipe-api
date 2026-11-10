"""Minimal client for the Paprika sync API (v1, HTTP basic auth).

Only what the harness needs: list recipes, read one, upload one. Uploads send
the *server's* copy of a recipe back with a small change, so fields this
harness doesn't know about are never lost.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import urllib.error
import urllib.request
import uuid

BASE_URL = "https://www.paprikaapp.com/api/v1/sync/"


class PaprikaError(RuntimeError):
    pass


def recipe_hash(recipe: dict) -> str:
    """A new content hash. Paprika clients re-download a recipe when it changes."""
    data = {k: v for k, v in recipe.items() if k != "hash"}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode("utf-8")).hexdigest().upper()


def patch_categories(server: list[str], baseline: list[str], local: list[str]) -> list[str]:
    """Apply the local change (baseline -> local) to the server's category list.

    Categories the user changed in the app since the export are kept: only
    what was added or removed locally is added or removed on the server.
    """
    removed = set(baseline) - set(local)
    added = [c for c in local if c not in baseline]
    out = [c for c in server if c not in removed]
    out += [c for c in added if c not in out]
    return out


class PaprikaClient:
    def __init__(self, email: str, password: str, *, timeout: float = 30, base_url: str = BASE_URL) -> None:
        token = base64.b64encode(f"{email}:{password}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "User-Agent": "paprika-recipe-harness"}
        self.timeout = timeout
        self.base_url = base_url

    def _request(self, path: str, data: bytes | None = None, headers: dict | None = None):
        req = urllib.request.Request(self.base_url + path, data=data, headers={**self._headers, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise PaprikaError("Paprika rejected the credentials (HTTP 401)") from exc
            raise PaprikaError(f"Paprika API error for {path}: HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise PaprikaError(f"Paprika API request failed for {path}: {exc}") from exc
        if isinstance(payload, dict) and "error" in payload:
            raise PaprikaError(f"Paprika API error for {path}: {payload['error']}")
        return payload.get("result") if isinstance(payload, dict) else payload

    def list_recipes(self) -> dict[str, str]:
        """``{uid: hash}`` for every recipe in the account."""
        return {r["uid"]: r["hash"] for r in self._request("recipes/")}

    def get_recipe(self, uid: str) -> dict:
        recipe = self._request(f"recipe/{uid}/")
        if not isinstance(recipe, dict) or recipe.get("uid") != uid:
            raise PaprikaError(f"unexpected response for recipe {uid}")
        return recipe

    def upload_recipe(self, recipe: dict) -> None:
        """Upload a full recipe object (multipart form, gzipped JSON in ``data``)."""
        body = gzip.compress(json.dumps(recipe).encode("utf-8"))
        boundary = uuid.uuid4().hex
        payload = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="data"; filename="data"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + body + f"\r\n--{boundary}--\r\n".encode()
        result = self._request(
            f"recipe/{recipe['uid']}/", data=payload,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        if result is not True:
            raise PaprikaError(f"upload of {recipe['uid']} not confirmed: {result!r}")

    def set_categories(self, server_recipe: dict, categories: list[str]) -> dict:
        """Upload ``server_recipe`` with new categories and a new hash; return what was sent."""
        new = dict(server_recipe)
        new["categories"] = list(categories)
        new["hash"] = recipe_hash(new)
        self.upload_recipe(new)
        return new
