"""DeepAgents BackendProtocol implementation & CompositeBackend mount topology (ADR-004 §2)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import fnmatch
import json
import re
from datetime import datetime
from typing import Any, Coroutine, Literal, TypeVar

import yaml
from deepagents.backends.composite import CompositeBackend
from deepagents.backends.protocol import (
    FILE_NOT_FOUND,
    PERMISSION_DENIED,
    BackendProtocol,
    DeleteResult,
    EditResult,
    FileDownloadResponse,
    FileInfo,
    FileUploadResponse,
    GlobResult,
    GrepMatch,
    GrepResult,
    LsResult,
    ReadResult,
    WriteResult,
)
from deepagents.backends.state import StateBackend

from context_core.compression import format_utc, normalize_lf_text
from context_core.models import (
    Entity,
    KnowledgeItem,
    MemoryRecord,
    MemoryUpsertInput,
    Principal,
    Topic,
)
from context_core.ports import PolicyPort, StorePort

T = TypeVar("T")

NamespaceKind = Literal["all", "knowledge", "skills", "memories"]


def _run_coro_sync(coro: Coroutine[Any, Any, T]) -> T:
    """Safely bridge async StorePort calls into synchronous BackendProtocol methods."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _slug_filename(key: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", key.strip().lower()).strip("-") or "item"


def _render_entity_markdown(entity: Entity, claims: list[Any], topics: list[str]) -> str:
    frontmatter = {
        "okf_version": "0.2",
        "id": entity.canonical_key,
        "entity_id": entity.entity_id,
        "entity_type": entity.entity_type,
        "display_name": entity.display_name,
        "aliases": entity.aliases,
        "wall_side": entity.wall_side.value,
        "topics": sorted(set(topics)),
        "status": entity.status,
    }
    lines = [
        "---",
        yaml.safe_dump(frontmatter, sort_keys=False).strip(),
        "---",
        "",
        f"# {entity.display_name} (`{entity.canonical_key}`)",
        "",
        entity.description or "",
        "",
    ]
    facts = [c for c in claims if c.channel == "fact"]
    directives = [c for c in claims if c.channel == "directive"]
    if facts:
        lines.append("## Facts")
        for c in facts:
            ev = (
                f" [{c.evidence_redaction_label or 'Restricted — Request Access'}]"
                if c.evidence_redacted
                else (f" (source: {c.provenance.source_system})" if c.provenance.source_system else "")
            )
            lines.append(
                f"- `{c.claim_id}` **{c.predicate}** ({c.context_purpose}, {c.epistemic_status}): "
                f"`{c.object_value}`{ev}"
            )
        lines.append("")
    if directives:
        lines.append("## Directives")
        for c in directives:
            mand = " [mandatory]" if c.is_mandatory else ""
            lines.append(
                f"- `{c.claim_id}` **{c.predicate}** ({c.scope_type}{mand}): {c.object_value}"
            )
        lines.append("")
    return normalize_lf_text("\n".join(lines).strip() + "\n")


def _render_topic_markdown(topic: Topic, attached_ids: list[str]) -> str:
    fm = {
        "topic_id": topic.topic_id,
        "slug": topic.slug,
        "pref_label": topic.pref_label,
        "alt_labels": topic.alt_labels,
        "broader_topic_id": topic.broader_topic_id,
        "narrower_topic_ids": topic.narrower_topic_ids,
        "status": topic.status,
    }
    lines = [
        "---",
        yaml.safe_dump(fm, sort_keys=False).strip(),
        "---",
        "",
        f"# Topic: {topic.pref_label} (`{topic.slug}`)",
        "",
        f"Authorized attached records: {len(attached_ids)}",
    ]
    for tid in sorted(attached_ids):
        lines.append(f"- `{tid}`")
    return normalize_lf_text("\n".join(lines) + "\n")


def _render_knowledge_markdown(item: KnowledgeItem) -> str:
    if item.knowledge_kind == "skill_procedure":
        include_tools = item.metadata.get("include_tools", [])
        fm = {
            "name": item.metadata.get("name", item.slug),
            "description": item.metadata.get("description", item.display_name),
            "scope": item.metadata.get("scope", "skill"),
            "wall_side": item.wall_side.value,
            "topics": item.topics,
            "version": item.metadata.get("version", "1.0.0"),
            "metadata": {"include_tools": include_tools},
        }
    else:
        fm = {
            "okf_version": item.okf_version,
            "id": item.item_id,
            "knowledge_kind": item.knowledge_kind,
            "display_name": item.display_name,
            "wall_side": item.wall_side.value,
            "topics": item.topics,
            "related_entities": item.related_entities,
            "content_sha256": item.content_sha256,
        }
    return normalize_lf_text(
        f"---\n{yaml.safe_dump(fm, sort_keys=False).strip()}\n---\n\n{item.body_markdown.strip()}\n"
    )


def _render_memory_markdown(mem: MemoryRecord) -> str:
    fm = {
        "memory_id": mem.memory_id,
        "user_id": mem.user_id,
        "scope_type": mem.scope_type,
        "skill_id": mem.skill_id,
        "key": mem.key,
        "tags": mem.tags,
        "linked_entity_ids": mem.linked_entity_ids,
        "linked_topic_ids": mem.linked_topic_ids,
        "updated_at": format_utc(mem.updated_at),
    }
    return normalize_lf_text(
        f"---\n{yaml.safe_dump(fm, sort_keys=False).strip()}\n---\n\n{mem.content.strip()}\n"
    )


def _build_read_result(content: str, *, offset: int = 0, limit: int = 2000, modified_at: str = "") -> ReadResult:
    """Build a spec-compliant deepagents 0.7.x ReadResult satisfying ReadResult.__post_init__."""
    if limit <= 0:
        return ReadResult(
            file_data={"content": "", "encoding": "utf-8", "modified_at": modified_at},
            no_lines_requested=True,
        )
    text = normalize_lf_text(content)
    lines = text.splitlines()
    total = len(lines)
    if total == 0:
        return ReadResult(
            file_data={"content": "", "encoding": "utf-8", "modified_at": modified_at},
        )
    start_idx = max(0, int(offset))
    if start_idx >= total:
        return ReadResult(
            file_data={"content": "", "encoding": "utf-8", "modified_at": modified_at},
        )
    end_idx = min(total, start_idx + int(limit))
    sliced = "\n".join(lines[start_idx:end_idx])
    next_off = end_idx if end_idx < total else None
    return ReadResult(
        file_data={"content": sliced, "encoding": "utf-8", "modified_at": modified_at},
        total_lines=total,
        start_line=start_idx + 1,
        end_line=end_idx,
        next_offset=next_off,
    )


class ContextLakeBackend(BackendProtocol):
    """Real DeepAgents BackendProtocol projecting authorized Context Lake namespaces as virtual files.

    Mount topology (ADR-004 §2):
    - `/knowledge/` -> read-only authorized entities, claims (evidence-redacted), SKOS topics, data access guidance
    - `/skills/`    -> read-only SKILL.md procedures with YAML `metadata.include_tools`
    - `/memories/`  -> caller-scoped read-write personal memory (auto-disabled on shared channels / service mode)
    """

    def __init__(
        self,
        store: StorePort,
        policy: PolicyPort,
        principal: Principal,
        *,
        namespace: NamespaceKind = "all",
        read_only: bool | None = None,
        as_of: datetime | None = None,
        recorded_as_of: datetime | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._principal = principal
        self._namespace: NamespaceKind = namespace
        if read_only is None:
            self._read_only = namespace in ("knowledge", "skills")
        else:
            self._read_only = read_only
        self._as_of = as_of
        self._recorded_as_of = recorded_as_of

    def _normalize_path(self, path: str) -> str:
        cleaned = "/" + path.replace("\\", "/").lstrip("/")
        if len(cleaned) > 1 and cleaned.endswith("/"):
            cleaned = cleaned.rstrip("/")
        return cleaned

    def _is_writable_path(self, norm_path: str) -> tuple[bool, str | None]:
        if self._principal.revoked:
            return False, "permission_denied: revoked principal"
        if self._read_only:
            return False, f"permission_denied: /{self._namespace}/ mount is read-only"
        if self._namespace == "all":
            if norm_path.startswith("/knowledge") or norm_path.startswith("/skills"):
                return False, f"permission_denied: {norm_path} is read-only"
            if not norm_path.startswith("/memories"):
                return False, f"permission_denied: writes permitted only under /memories/"
        if not self._policy.is_memory_allowed(self._principal):
            return False, "surface_policy_blocked: personal memories are disabled in shared/service mode"
        return True, None

    async def _materialize_virtual_files(self) -> dict[str, tuple[str, str]]:
        """Project caller-authorized records into `{virtual_path: (content, modified_at_iso)}`."""
        files: dict[str, tuple[str, str]] = {}
        if self._principal.revoked or await self._store.is_principal_revoked(self._principal.user_id):
            return files

        prefix_k = "/knowledge" if self._namespace == "all" else ""
        prefix_s = "/skills" if self._namespace == "all" else ""
        prefix_m = "/memories" if self._namespace == "all" else ""

        if self._namespace in ("all", "knowledge"):
            entities = await self._store.list_entities(principal=self._principal, limit=500)
            all_claims = await self._store.query_claims(
                principal=self._principal,
                as_of=self._as_of,
                recorded_as_of=self._recorded_as_of,
            )
            claims_by_subj: dict[str, list[Any]] = {}
            for c in all_claims:
                claims_by_subj.setdefault(c.subject_id, []).append(c)

            topics = await self._store.list_topics(principal=self._principal)
            topics_by_id = {t.topic_id: t for t in topics}
            attachments = await self._store.list_topic_attachments(principal=self._principal)
            topics_for_target: dict[str, list[str]] = {}
            targets_for_topic: dict[str, list[str]] = {}
            for att in attachments:
                t = topics_by_id.get(att.topic_id)
                if t is not None:
                    topics_for_target.setdefault(att.target_id, []).append(t.slug)
                    targets_for_topic.setdefault(att.topic_id, []).append(att.target_id)

            for ent in entities:
                fname = f"{prefix_k}/entities/{_slug_filename(ent.canonical_key)}.md"
                md = _render_entity_markdown(
                    ent,
                    claims_by_subj.get(ent.entity_id, []),
                    topics_for_target.get(ent.entity_id, []),
                )
                files[fname] = (md, format_utc(ent.updated_at) or "")

            for topic in topics:
                fname = f"{prefix_k}/topics/{_slug_filename(topic.slug)}.md"
                files[fname] = (_render_topic_markdown(topic, targets_for_topic.get(topic.topic_id, [])), "")

            kitems = await self._store.list_knowledge_items(principal=self._principal)
            for ki in kitems:
                if ki.knowledge_kind == "domain_knowledge":
                    fname = f"{prefix_k}/domain/{_slug_filename(ki.slug)}.md"
                    files[fname] = (_render_knowledge_markdown(ki), format_utc(ki.recorded_at) or "")
                elif ki.knowledge_kind == "data_access_guidance":
                    fname = f"{prefix_k}/data_access_guidance/{_slug_filename(ki.slug)}.md"
                    files[fname] = (_render_knowledge_markdown(ki), format_utc(ki.recorded_at) or "")

        if self._namespace in ("all", "skills"):
            skills = await self._store.list_knowledge_items(
                principal=self._principal, kind="skill_procedure"
            )
            for sk in skills:
                slug = _slug_filename(sk.slug)
                fname = f"{prefix_s}/{slug}/SKILL.md"
                files[fname] = (_render_knowledge_markdown(sk), format_utc(sk.recorded_at) or "")

        if self._namespace in ("all", "memories"):
            if self._policy.is_memory_allowed(self._principal):
                user_slug = _slug_filename(self._principal.user_id)
                mems = await self._store.list_memories(principal=self._principal)
                for m in mems:
                    kslug = _slug_filename(m.key)
                    fname = f"{prefix_m}/{user_slug}/{kslug}.md"
                    files[fname] = (_render_memory_markdown(m), format_utc(m.updated_at) or "")
                wl = await self._store.get_watchlist(principal=self._principal)
                wl_path = f"{prefix_m}/{user_slug}/watchlist.json"
                files[wl_path] = (
                    json.dumps(wl.model_dump(mode="json"), indent=2) + "\n",
                    format_utc(wl.updated_at) or "",
                )

        return files

    def _extract_memory_key(self, norm_path: str) -> tuple[str, str | None, str]:
        """Map a write path under /memories/ into (key, skill_id, scope_type)."""
        rel = norm_path
        if rel.startswith("/memories/"):
            rel = rel[len("/memories/") :]
        rel = rel.lstrip("/")
        user_slug = _slug_filename(self._principal.user_id)
        parts = [p for p in rel.split("/") if p]
        if parts and parts[0] in (self._principal.user_id, user_slug):
            parts = parts[1:]
        if not parts:
            parts = ["note.md"]
        if len(parts) >= 2 and parts[0] == "skills":
            skill_id = parts[1]
            key = "/".join(parts[2:]) or "note"
            scope = "user_x_skill"
        else:
            skill_id = None
            key = "/".join(parts)
            scope = "user"
        if key.endswith(".md"):
            key = key[:-3]
        return key, skill_id, scope

    # ---------------- Async BackendProtocol Implementations ----------------

    async def als(self, path: str) -> LsResult:
        norm = self._normalize_path(path)
        if self._namespace == "memories" and not self._policy.is_memory_allowed(self._principal):
            return LsResult(entries=[])

        files = await self._materialize_virtual_files()
        prefix = "/" if norm == "/" else f"{norm}/"
        dirs: set[str] = set()
        entries: list[FileInfo] = []

        for fpath, (content, mod_at) in sorted(files.items()):
            if not fpath.startswith(prefix):
                continue
            rest = fpath[len(prefix) :]
            if "/" in rest:
                sub = rest.split("/", 1)[0]
                dir_path = f"{prefix}{sub}/"
                if dir_path not in dirs:
                    dirs.add(dir_path)
                    entries.append(FileInfo(path=dir_path, is_dir=True, size=0, modified_at=""))
            else:
                entries.append(
                    FileInfo(
                        path=fpath,
                        is_dir=False,
                        size=len(content.encode("utf-8")),
                        modified_at=mod_at,
                    )
                )

        entries.sort(key=lambda e: e["path"])
        return LsResult(entries=entries)

    async def aread(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        norm = self._normalize_path(file_path)
        if (self._namespace == "memories" or norm.startswith("/memories")) and not self._policy.is_memory_allowed(
            self._principal
        ):
            return ReadResult(error="surface_policy_blocked: personal memories are disabled")

        files = await self._materialize_virtual_files()
        item = files.get(norm)
        if item is None:
            return ReadResult(error=f"File '{norm}' not found")
        content, mod_at = item
        return _build_read_result(content, offset=offset, limit=limit, modified_at=mod_at)

    async def awrite(self, file_path: str, content: str) -> WriteResult:
        norm = self._normalize_path(file_path)
        allowed, err = self._is_writable_path(norm)
        if not allowed:
            return WriteResult(error=err or "permission_denied")

        key, skill_id, scope_type = self._extract_memory_key(norm)
        if key == "watchlist.json":
            return WriteResult(error="permission_denied: update watchlist via watchlist API")
        await self._store.upsert_memory(
            MemoryUpsertInput(
                scope_type=scope_type,  # type: ignore[arg-type]
                skill_id=skill_id,
                key=key,
                content=normalize_lf_text(content),
            ),
            principal=self._principal,
        )
        return WriteResult(path=norm)

    async def aedit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        norm = self._normalize_path(file_path)
        allowed, err = self._is_writable_path(norm)
        if not allowed:
            return EditResult(error=err or "permission_denied")

        key, skill_id, scope_type = self._extract_memory_key(norm)
        mems = await self._store.list_memories(principal=self._principal, skill_id=skill_id)
        target = next((m for m in mems if _slug_filename(m.key) == _slug_filename(key)), None)
        if target is None:
            return EditResult(error=f"File '{norm}' not found")

        count = target.content.count(old_string)
        if count == 0:
            return EditResult(error=f"String not found in '{norm}'")
        if count > 1 and not replace_all:
            return EditResult(error=f"Multiple occurrences ({count}) found; set replace_all=True")

        updated = (
            target.content.replace(old_string, new_string)
            if replace_all
            else target.content.replace(old_string, new_string, 1)
        )
        await self._store.upsert_memory(
            MemoryUpsertInput(
                memory_id=target.memory_id,
                scope_type=scope_type,  # type: ignore[arg-type]
                skill_id=target.skill_id,
                key=target.key,
                content=normalize_lf_text(updated),
                tags=target.tags,
                linked_entity_ids=target.linked_entity_ids,
                linked_topic_ids=target.linked_topic_ids,
            ),
            principal=self._principal,
        )
        return EditResult(path=norm, occurrences=count if replace_all else 1)

    async def adelete(self, file_path: str) -> DeleteResult:
        norm = self._normalize_path(file_path)
        allowed, err = self._is_writable_path(norm)
        if not allowed:
            return DeleteResult(error=err or "permission_denied")

        key, skill_id, _ = self._extract_memory_key(norm)
        mems = await self._store.list_memories(principal=self._principal, skill_id=skill_id)
        target = next((m for m in mems if _slug_filename(m.key) == _slug_filename(key)), None)
        if target is None:
            return DeleteResult(error=f"File '{norm}' not found")
        deleted = await self._store.delete_memory(target.memory_id, principal=self._principal)
        if not deleted:
            return DeleteResult(error=f"File '{norm}' not found")
        return DeleteResult(path=norm)

    async def agrep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        files = await self._materialize_virtual_files()
        base = self._normalize_path(path) if path else "/"
        matches: list[GrepMatch] = []
        truncated = False
        needle = pattern.lower()

        for fpath, (content, _) in sorted(files.items()):
            if base != "/" and not (fpath == base or fpath.startswith(f"{base}/")):
                continue
            if glob and not (fnmatch.fnmatch(fpath, glob) or fnmatch.fnmatch(fpath.rsplit("/", 1)[-1], glob)):
                continue
            for idx, line in enumerate(content.splitlines(), start=1):
                if needle in line.lower():
                    matches.append(GrepMatch(path=fpath, line=idx, text=line))
                    if max_count is not None and len(matches) >= max_count:
                        truncated = True
                        return GrepResult(matches=matches, truncated=truncated)
        return GrepResult(matches=matches, truncated=truncated)

    async def aglob(self, pattern: str, path: str | None = None) -> GlobResult:
        files = await self._materialize_virtual_files()
        base = self._normalize_path(path) if path else "/"
        matches: list[FileInfo] = []

        for fpath, (content, mod_at) in sorted(files.items()):
            if base != "/" and not fpath.startswith(f"{base}/"):
                continue
            rel = fpath[len(base) :].lstrip("/") if base != "/" else fpath.lstrip("/")
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(fpath, pattern) or fnmatch.fnmatch(
                fpath.rsplit("/", 1)[-1], pattern
            ):
                matches.append(
                    FileInfo(
                        path=fpath,
                        is_dir=False,
                        size=len(content.encode("utf-8")),
                        modified_at=mod_at,
                    )
                )
        return GlobResult(matches=matches, truncated=False)

    async def adownload_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        files = await self._materialize_virtual_files()
        results: list[FileDownloadResponse] = []
        for p in paths:
            norm = self._normalize_path(p)
            item = files.get(norm)
            if item is None:
                results.append(FileDownloadResponse(path=p, content=None, error=FILE_NOT_FOUND))
            else:
                results.append(FileDownloadResponse(path=p, content=item[0].encode("utf-8"), error=None))
        return results

    async def aupload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        results: list[FileUploadResponse] = []
        for path, raw in files:
            norm = self._normalize_path(path)
            allowed, err = self._is_writable_path(norm)
            if not allowed:
                results.append(FileUploadResponse(path=path, error=PERMISSION_DENIED))
                continue
            res = await self.awrite(norm, raw.decode("utf-8", errors="replace"))
            results.append(FileUploadResponse(path=path, error=res.error))
        return results

    # ---------------- Synchronous BackendProtocol Bridges ----------------

    def ls(self, path: str) -> LsResult:
        return _run_coro_sync(self.als(path))

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        return _run_coro_sync(self.aread(file_path, offset=offset, limit=limit))

    def write(self, file_path: str, content: str) -> WriteResult:
        return _run_coro_sync(self.awrite(file_path, content))

    def edit(
        self,
        file_path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        return _run_coro_sync(self.aedit(file_path, old_string, new_string, replace_all=replace_all))

    def delete(self, file_path: str) -> DeleteResult:
        return _run_coro_sync(self.adelete(file_path))

    def grep(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        *,
        max_count: int | None = None,
    ) -> GrepResult:
        return _run_coro_sync(self.agrep(pattern, path=path, glob=glob, max_count=max_count))

    def glob(self, pattern: str, path: str | None = None) -> GlobResult:
        return _run_coro_sync(self.aglob(pattern, path=path))

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        return _run_coro_sync(self.adownload_files(paths))

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        return _run_coro_sync(self.aupload_files(files))


class EphemeralStateBackend(StateBackend):
    """StateBackend subclass that works both inside LangGraph runs and in standalone unit tests."""

    def __init__(self) -> None:
        super().__init__()
        self._standalone_files: dict[str, tuple[str, str]] = {}

    def _in_langgraph(self) -> bool:
        try:
            self._get_config()
            return True
        except RuntimeError:
            return False

    def ls(self, path: str) -> LsResult:
        if self._in_langgraph():
            return super().ls(path)
        norm = "/" + path.replace("\\", "/").lstrip("/")
        if len(norm) > 1 and norm.endswith("/"):
            norm = norm.rstrip("/")
        prefix = "/" if norm == "/" else f"{norm}/"
        entries: list[FileInfo] = []
        dirs: set[str] = set()
        for fpath, (content, mod_at) in sorted(self._standalone_files.items()):
            if not fpath.startswith(prefix):
                continue
            rest = fpath[len(prefix) :]
            if "/" in rest:
                sub = f"{prefix}{rest.split('/', 1)[0]}/"
                if sub not in dirs:
                    dirs.add(sub)
                    entries.append(FileInfo(path=sub, is_dir=True, size=0, modified_at=""))
            else:
                entries.append(FileInfo(path=fpath, is_dir=False, size=len(content.encode("utf-8")), modified_at=mod_at))
        return LsResult(entries=entries)

    def read(self, file_path: str, offset: int = 0, limit: int = 2000) -> ReadResult:
        if self._in_langgraph():
            return super().read(file_path, offset=offset, limit=limit)
        norm = "/" + file_path.replace("\\", "/").lstrip("/")
        item = self._standalone_files.get(norm)
        if item is None:
            return ReadResult(error=f"File '{norm}' not found")
        return _build_read_result(item[0], offset=offset, limit=limit, modified_at=item[1])

    def write(self, file_path: str, content: str) -> WriteResult:
        if self._in_langgraph():
            return super().write(file_path, content)
        norm = "/" + file_path.replace("\\", "/").lstrip("/")
        self._standalone_files[norm] = (normalize_lf_text(content), "")
        return WriteResult(path=norm)

    def edit(self, file_path: str, old_string: str, new_string: str, replace_all: bool = False) -> EditResult:
        if self._in_langgraph():
            return super().edit(file_path, old_string, new_string, replace_all=replace_all)
        norm = "/" + file_path.replace("\\", "/").lstrip("/")
        item = self._standalone_files.get(norm)
        if item is None:
            return EditResult(error=f"File '{norm}' not found")
        cnt = item[0].count(old_string)
        if cnt == 0:
            return EditResult(error=f"String not found in '{norm}'")
        if cnt > 1 and not replace_all:
            return EditResult(error=f"Multiple occurrences ({cnt}) found")
        updated = item[0].replace(old_string, new_string) if replace_all else item[0].replace(old_string, new_string, 1)
        self._standalone_files[norm] = (updated, item[1])
        return EditResult(path=norm, occurrences=cnt if replace_all else 1)

    def delete(self, file_path: str) -> DeleteResult:
        if self._in_langgraph():
            return super().delete(file_path)
        norm = "/" + file_path.replace("\\", "/").lstrip("/")
        if norm not in self._standalone_files:
            return DeleteResult(error=f"File '{norm}' not found")
        del self._standalone_files[norm]
        return DeleteResult(path=norm)


def create_context_composite_backend(
    store: StorePort,
    policy: PolicyPort,
    principal: Principal,
    *,
    as_of: datetime | None = None,
    recorded_as_of: datetime | None = None,
) -> CompositeBackend:
    """Create the canonical DeepAgents CompositeBackend mount topology (ADR-004 §2)."""
    return CompositeBackend(
        default=EphemeralStateBackend(),
        routes={
            "/knowledge/": ContextLakeBackend(
                store,
                policy,
                principal,
                namespace="knowledge",
                read_only=True,
                as_of=as_of,
                recorded_as_of=recorded_as_of,
            ),
            "/skills/": ContextLakeBackend(
                store,
                policy,
                principal,
                namespace="skills",
                read_only=True,
                as_of=as_of,
                recorded_as_of=recorded_as_of,
            ),
            "/memories/": ContextLakeBackend(
                store,
                policy,
                principal,
                namespace="memories",
                read_only=False,
                as_of=as_of,
                recorded_as_of=recorded_as_of,
            ),
        },
    )


async def load_skill_include_tools(
    store: StorePort,
    principal: Principal,
    skill_slug: str,
) -> list[str]:
    """Read an authorized SKILL.md procedure and return its declared `metadata.include_tools` list."""
    skills = await store.list_knowledge_items(principal=principal, kind="skill_procedure")
    norm = skill_slug.strip().lower().removeprefix("skill:")
    for sk in skills:
        if sk.slug.lower() == norm or str(sk.metadata.get("name", "")).lower() == norm:
            tools = sk.metadata.get("include_tools")
            if isinstance(tools, list):
                return [str(t) for t in tools]
    return []
