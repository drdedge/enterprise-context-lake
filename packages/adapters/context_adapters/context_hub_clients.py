"""Context Hub clients (ADR-008).

- ``FolderHubClient`` emulates Context Hub's commit model on local disk. It is the default, so
  export and import can be run and reviewed with no network.
- ``LangSmithHubClient`` wraps the LangSmith SDK (``push_agent``/``push_skill``/``pull_*``).

Both refuse to push to an existing repo unless the caller passes the repo's current head as
``parent_commit``, so nothing in Context Hub is silently overwritten.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from context_core.context_hub import HubFileValue, HubKind, SkillLink
from context_core.exceptions import ContextLakeError


class HubConflictError(ContextLakeError):
    """The repo's head is not the commit the lake last exported, so the push was refused."""

    code = "context_hub_conflict"
    status_code = 409


class HubNotFoundError(ContextLakeError):
    code = "context_hub_not_found"
    status_code = 404


@dataclass
class PulledRepo:
    handle: str
    kind: HubKind
    commit_hash: str
    files: dict[str, HubFileValue]


class HubClient(Protocol):
    def head(self, handle: str, kind: HubKind) -> str | None: ...

    def push(
        self,
        handle: str,
        kind: HubKind,
        files: dict[str, HubFileValue],
        *,
        parent_commit: str | None,
        description: str = "",
    ) -> str: ...

    def pull(self, handle: str, kind: HubKind, version: str | None = None) -> PulledRepo: ...


def _same_commit(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return n >= 8 and a[:n] == b[:n]


# ---------------------------------------------------------------------------
# Local folder emulation
# ---------------------------------------------------------------------------


class FolderHubClient:
    """Stores each repo as ``<root>/<handle>/`` (current files) plus ``.hub/`` commits and tags."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self._lock = threading.Lock()

    def _repo(self, handle: str) -> Path:
        if not re.match(r"^[a-z][a-z0-9-_]*$", handle):
            raise HubNotFoundError(f"Invalid repo handle: {handle}")
        return self.root / handle

    def _meta(self, handle: str) -> dict[str, Any] | None:
        p = self._repo(handle) / ".hub" / "repo.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None

    def head(self, handle: str, kind: HubKind) -> str | None:
        meta = self._meta(handle)
        return meta.get("head") if meta else None

    def push(
        self,
        handle: str,
        kind: HubKind,
        files: dict[str, HubFileValue],
        *,
        parent_commit: str | None,
        description: str = "",
    ) -> str:
        with self._lock:
            repo = self._repo(handle)
            meta = self._meta(handle)
            head = meta.get("head") if meta else None
            if head is not None and not _same_commit(parent_commit, head):
                raise HubConflictError(
                    f"Context Hub repo {handle} is at {head[:8]}, not {str(parent_commit)[:8] if parent_commit else 'unknown'}; "
                    "it changed since the last export. Import those edits first."
                )
            current = self._read_commit(handle, head)["files"] if head else {}
            for path, value in files.items():
                if value is None:
                    current.pop(path, None)
                elif isinstance(value, SkillLink):
                    current[path] = {"type": "skill", "repo_handle": value.repo_handle, "commit_hash": value.commit_hash}
                else:
                    current[path] = {"type": "file", "content": value}
            body = json.dumps({"parent": head, "files": current}, sort_keys=True)
            commit = hashlib.sha256(body.encode("utf-8")).hexdigest()
            hub = repo / ".hub"
            (hub / "commits").mkdir(parents=True, exist_ok=True)
            (hub / "commits" / f"{commit}.json").write_text(body, encoding="utf-8")
            (hub / "repo.json").write_text(
                json.dumps({"kind": kind, "head": commit, "description": description}, indent=2), encoding="utf-8"
            )
            self._materialize(repo, current)
            return commit

    def _read_commit(self, handle: str, commit: str) -> dict[str, Any]:
        commits = self._repo(handle) / ".hub" / "commits"
        matches = sorted(commits.glob(f"{commit}*.json")) if commits.is_dir() else []
        if len(matches) != 1:
            raise HubNotFoundError(f"Commit {commit} not found in {handle}")
        data = json.loads(matches[0].read_text(encoding="utf-8"))
        data["hash"] = matches[0].stem
        return data

    def _materialize(self, repo: Path, files: dict[str, dict[str, Any]]) -> None:
        for existing in sorted(repo.rglob("*"), reverse=True):
            if ".hub" in existing.relative_to(repo).parts:
                continue
            if existing.is_file():
                existing.unlink()
            elif existing.is_dir() and not any(existing.iterdir()):
                existing.rmdir()
        for path, entry in files.items():
            target = repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if entry["type"] == "file":
                target.write_text(entry["content"], encoding="utf-8", newline="\n")
            else:
                target.with_name(target.name + ".link.json").write_text(json.dumps(entry, indent=2), encoding="utf-8")

    def tag(self, handle: str, tag: str, commit: str) -> None:
        with self._lock:
            meta = self._meta(handle)
            if meta is None:
                raise HubNotFoundError(f"Repo not found: {handle}")
            tags = meta.setdefault("tags", {})
            tags[tag] = self._read_commit(handle, commit)["hash"]
            (self._repo(handle) / ".hub" / "repo.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")

    def pull(self, handle: str, kind: HubKind, version: str | None = None) -> PulledRepo:
        meta = self._meta(handle)
        if meta is None:
            raise HubNotFoundError(f"Context Hub repo not found: {handle}")
        commit = meta.get("head")
        if version:
            commit = (meta.get("tags") or {}).get(version, version)
        data = self._read_commit(handle, commit)
        files: dict[str, HubFileValue] = {}
        for path, entry in data["files"].items():
            if entry["type"] == "file":
                files[path] = entry["content"]
            else:
                files[path] = SkillLink(entry["repo_handle"], entry.get("commit_hash"))
        return PulledRepo(handle=handle, kind=meta.get("kind", kind), commit_hash=data["hash"], files=files)


# ---------------------------------------------------------------------------
# LangSmith SDK
# ---------------------------------------------------------------------------

_COMMIT_FROM_URL = re.compile(r"/context/[^/]+/([0-9a-f]{8,64})")


class LangSmithHubClient:
    """Context Hub through the LangSmith SDK (``langsmith>=0.16``). Needs ``LANGSMITH_API_KEY``."""

    def __init__(self, client: Any | None = None, owner: str | None = None) -> None:
        if client is None:
            from langsmith import Client

            client = Client()
        self._client = client
        self._owner = owner or os.environ.get("CONTEXT_HUB_OWNER") or None

    def _ident(self, handle: str, version: str | None = None) -> str:
        ident = f"{self._owner}/{handle}" if self._owner else handle
        return f"{ident}:{version}" if version else ident

    def _exists(self, handle: str, kind: HubKind) -> bool:
        fn = self._client.skill_exists if kind == "skill" else self._client.agent_exists
        return bool(fn(self._ident(handle)))

    def head(self, handle: str, kind: HubKind) -> str | None:
        if not self._exists(handle, kind):
            return None
        return self.pull(handle, kind).commit_hash

    def push(
        self,
        handle: str,
        kind: HubKind,
        files: dict[str, HubFileValue],
        *,
        parent_commit: str | None,
        description: str = "",
    ) -> str:
        from langsmith.schemas import FileEntry, SkillEntry

        head = self.head(handle, kind)
        if head is not None and not _same_commit(parent_commit, head):
            raise HubConflictError(
                f"Context Hub repo {handle} is at {head[:8]}, not {str(parent_commit)[:8] if parent_commit else 'unknown'}; "
                "it changed since the last export. Import those edits first."
            )
        entries: dict[str, Any] = {}
        for path, value in files.items():
            if value is None:
                entries[path] = None
            elif isinstance(value, SkillLink):
                entries[path] = SkillEntry(repo_handle=value.repo_handle, commit_hash=value.commit_hash)
            else:
                entries[path] = FileEntry(content=value)
        push = self._client.push_skill if kind == "skill" else self._client.push_agent
        url = push(
            self._ident(handle),
            files=entries,
            parent_commit=head,
            description=description[:500] or None,
            is_public=False,
        )
        m = _COMMIT_FROM_URL.search(str(url))
        return m.group(1) if m else self.pull(handle, kind).commit_hash

    def pull(self, handle: str, kind: HubKind, version: str | None = None) -> PulledRepo:
        pull = self._client.pull_skill if kind == "skill" else self._client.pull_agent
        ctx = pull(self._ident(handle, version))
        files: dict[str, HubFileValue] = {}
        for path, entry in (ctx.files or {}).items():
            etype = getattr(entry, "type", None)
            if etype == "file":
                files[path] = entry.content
            elif etype in ("skill", "agent"):
                files[path] = SkillLink(entry.repo_handle, getattr(entry, "commit_hash", None))
        return PulledRepo(handle=handle, kind=kind, commit_hash=ctx.commit_hash, files=files)


def hub_client_from_env(runtime_dir: str | Path) -> HubClient:
    """``CONTEXT_HUB_BACKEND=langsmith`` uses LangSmith; anything else uses the local folder emulator."""
    backend = (os.environ.get("CONTEXT_HUB_BACKEND") or "folder").strip().lower()
    if backend == "langsmith":
        return LangSmithHubClient()
    root = os.environ.get("CONTEXT_HUB_FOLDER") or str(Path(runtime_dir) / "context_hub")
    return FolderHubClient(root)
