"""Small durable JSON store for state that must survive restarts: the LLM
spend ledger (budget.py) and the private Qloo replay packs (replay.py).

Render's free disk is wiped on every restart and deploy, so on Render the store
is a **private** GitHub repo, written through the Contents API with a
fine-grained token limited to that one repo. Locally it's a git-ignored folder.

    FOOTFALL_STORE_REPO   owner/name of the private repo (e.g. JannetEkka/footfall-private)
    FOOTFALL_STORE_TOKEN  fine-grained token: Contents read and write, that repo only
    FOOTFALL_STATE_DIR    local folder instead (default .state/, git-ignored)

Nothing in here may ever be committed to the public repo: the replay packs are
Qloo responses, which Qloo's terms keep out of public repos.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import httpx


def _digest(text: str) -> str:
    return hashlib.sha1(text.encode()).hexdigest()


class StoreError(RuntimeError):
    pass


class Conflict(StoreError):
    """Someone else wrote the file since it was read."""


class DirStore:
    durable = False  # survives restarts on a laptop, not on Render

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = asyncio.Lock()

    def _file(self, name: str) -> Path:
        p = (self.path / name).resolve()
        if self.path.resolve() not in p.parents:
            raise StoreError(f"bad name {name!r}")
        return p

    async def read(self, name: str) -> tuple[Any, str | None]:
        f = self._file(name)
        if not f.exists():
            return None, None
        text = f.read_text()
        return json.loads(text), _digest(text)

    async def write(self, name: str, data: Any, version: str | None, message: str = "") -> str:
        async with self._lock:
            f = self._file(name)
            current = _digest(f.read_text()) if f.exists() else None
            if current != version:
                raise Conflict(name)
            f.parent.mkdir(parents=True, exist_ok=True)
            text = json.dumps(data, separators=(",", ":"))
            tmp = f.with_suffix(f.suffix + ".tmp")
            tmp.write_text(text)
            tmp.replace(f)
            return _digest(text)


class GitHubStore:
    durable = True

    def __init__(self, repo: str, token: str, *, branch: str = "main",
                 transport: httpx.AsyncBaseTransport | None = None):
        self.repo = repo
        self.branch = branch
        self._http = httpx.AsyncClient(
            base_url="https://api.github.com", transport=transport, timeout=20,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "footfall"})

    def _url(self, name: str) -> str:
        if ".." in name.split("/") or name.startswith("/"):
            raise StoreError(f"bad name {name!r}")
        return f"/repos/{self.repo}/contents/{name}"

    async def read(self, name: str) -> tuple[Any, str | None]:
        try:
            r = await self._http.get(self._url(name), params={"ref": self.branch})
        except httpx.HTTPError as e:
            raise StoreError(f"store unreachable: {type(e).__name__}") from e
        if r.status_code == 404:
            return None, None
        if r.status_code != 200:
            raise StoreError(f"store read {name}: {r.status_code}")
        body = r.json()
        if body.get("encoding") != "base64":  # files over 1 MB come back without content
            raise StoreError(f"store read {name}: too large for the contents API")
        return json.loads(base64.b64decode(body["content"])), body["sha"]

    async def write(self, name: str, data: Any, version: str | None, message: str = "") -> str:
        payload = {"message": message or f"update {name}", "branch": self.branch,
                   "content": base64.b64encode(json.dumps(data, separators=(",", ":")).encode()).decode()}
        if version:
            payload["sha"] = version
        try:
            r = await self._http.put(self._url(name), json=payload)
        except httpx.HTTPError as e:
            raise StoreError(f"store unreachable: {type(e).__name__}") from e
        if r.status_code in (409, 422):  # stale sha, or the file appeared since we read it
            raise Conflict(name)
        if r.status_code not in (200, 201):
            raise StoreError(f"store write {name}: {r.status_code}")
        return r.json()["content"]["sha"]


def from_env() -> DirStore | GitHubStore:
    repo, token = os.environ.get("FOOTFALL_STORE_REPO"), os.environ.get("FOOTFALL_STORE_TOKEN")
    if repo and token:
        return GitHubStore(repo, token, branch=os.environ.get("FOOTFALL_STORE_BRANCH", "main"))
    return DirStore(os.environ.get("FOOTFALL_STATE_DIR", ".state"))
