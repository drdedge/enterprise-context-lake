"""Quarantined manual upload ingestion pipeline (PDF, CSV, Markdown, JSON) with LF SHA-256 provenance."""

from __future__ import annotations

import csv
import io
import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml
from pypdf import PdfReader

from context_core.compression import (
    ensure_utc,
    format_utc,
    lf_sha256_bytes,
    normalize_lf_bytes,
    normalize_lf_text,
    utc_now,
)
from context_core.exceptions import AuthorizationError, NotFoundError, ValidationError
from context_core.models import (
    REDACTED_EVIDENCE_LABEL,
    ClaimCreateInput,
    ClaimFacets,
    Principal,
    TopicAttachment,
    UploadIngestResult,
    WallSide,
)
from context_core.policy import wall_allows
from context_core.ports import PolicyPort, StorePort
from context_adapters.blob_store import BlobStore, LocalBlobStore, get_json, put_json

TEXT_MEDIA_SUFFIXES = frozenset({".csv", ".md", ".markdown", ".txt", ".json", ".yaml", ".yml"})
SAFE_FILENAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")
ADMIN_CODE_RE = re.compile(r"\b(ADM-[A-Z0-9-]+|CITCO-[A-Z0-9-]+|GIPS-[A-Z0-9-]+)\b")


def _parse_frontmatter_text(text: str) -> tuple[dict[str, Any], str]:
    norm = normalize_lf_text(text)
    if not norm.startswith("---\n"):
        return {}, norm
    parts = norm.split("---\n", 2)
    if len(parts) < 3:
        return {}, norm
    fm = yaml.safe_load(parts[1]) or {}
    return (fm if isinstance(fm, dict) else {}), parts[2].strip()


class UploadService:
    """Stores immutable uploaded artifacts and extracts quarantined unverified_observation claims.

    Guarantees:
    - Computes deterministic LF-normalized SHA-256 for text files and raw SHA-256 for PDFs.
    - NEVER overwrites live `approved_fact` claims (`epistemic_status="unverified_observation"`, `force_supersede=False`).
    - Enforces decoupled `evidence_acl_groups` and `wall_side` on source listing & retrieval.
    """

    def __init__(self, *, storage_dir: Path, repo_root: Path | None = None, blobs: BlobStore | None = None) -> None:
        self._storage_dir = storage_dir
        self._repo_root = repo_root or Path(__file__).resolve().parents[3]
        # Uploaded files (and, on S3, the upload records) go to the blob store: a local folder by
        # default, S3 with SSE-KMS in production (ADR-010)
        self._blobs: BlobStore = blobs if blobs is not None else LocalBlobStore(storage_dir, repo_root=self._repo_root)
        self._persist_records = not isinstance(self._blobs, LocalBlobStore)
        self._records: dict[str, dict[str, Any]] = {}
        self._loaded = not self._persist_records

    def clear(self) -> None:
        self._records.clear()

    def _load_records(self) -> None:
        """Read upload records written by earlier runs (S3 only; local records live in memory)."""
        if self._loaded:
            return
        for key in self._blobs.list_keys("records"):
            if key.endswith(".json"):
                rec = get_json(self._blobs, key)
                self._records.setdefault(rec["upload_id"], rec)
        self._loaded = True

    async def ingest_upload(
        self,
        *,
        filename: str,
        content_type: str | None,
        raw_bytes: bytes,
        principal: Principal,
        store: StorePort,
        policy: PolicyPort,
        subject_canonical_key: str | None = None,
        topic_slug: str | None = None,
        wall_side: str = "public_side",
        evidence_acl_groups: list[str] | None = None,
        notes: str | None = None,
    ) -> UploadIngestResult:
        if not raw_bytes:
            raise ValidationError("Uploaded file is empty")

        try:
            ws = WallSide(wall_side)
        except ValueError as exc:
            raise ValidationError(f"Invalid wall_side: {wall_side}") from exc

        if not wall_allows(principal.wall_side, ws):
            raise AuthorizationError("Cannot upload artifact across an unauthorized wall_side")

        safe_name = SAFE_FILENAME_RE.sub("_", Path(filename or "upload.txt").name).strip("._") or "upload.txt"
        suffix = Path(safe_name).suffix.lower()
        is_text = suffix in TEXT_MEDIA_SUFFIXES
        stored_bytes = normalize_lf_bytes(raw_bytes) if is_text else raw_bytes
        content_sha256 = lf_sha256_bytes(raw_bytes, is_text=is_text)

        upload_id = f"upl-{uuid.uuid4().hex[:12]}"
        media_type = content_type or (
            "application/pdf"
            if suffix == ".pdf"
            else "text/csv"
            if suffix == ".csv"
            else "text/markdown"
            if suffix in (".md", ".markdown")
            else "application/json"
            if suffix == ".json"
            else "text/plain"
        )
        rel_uri = self._blobs.put(f"{upload_id}_{safe_name}", stored_bytes, content_type=media_type)

        read_groups = (
            ["firm-all"]
            if ws == WallSide.public_side
            else sorted(principal.groups.intersection({"private-credit-deal-team", "mnpi-project-cobalt", "compliance-oversight"}))
            or ["private-credit-deal-team"]
        )
        ev_groups = [g.strip() for g in (evidence_acl_groups or []) if g and g.strip()]

        # Extract candidate claims strictly into quarantined unverified_observation
        extracted_preview, candidates = await self._extract_candidates(
            suffix=suffix,
            stored_bytes=stored_bytes,
            principal=principal,
            store=store,
            default_subject_key=subject_canonical_key,
            notes=notes,
        )

        # Resolve fallback entity if a candidate doesn't specify one
        fallback_entity = None
        if subject_canonical_key:
            fallback_entity = await store.get_entity(subject_canonical_key, principal=principal)
        if fallback_entity is None:
            ents = await store.list_entities(principal=principal, limit=50)
            fallback_entity = next((e for e in ents if e.canonical_key == "veh:alpha-credit-offshore-lp"), None) or (
                ents[0] if ents else None
            )
        if fallback_entity is None:
            raise NotFoundError("No authorized target entity available for upload ingestion")

        quarantined_ids: list[str] = []
        for cand in candidates:
            target = fallback_entity
            if cand.get("subject_key"):
                found = await store.get_entity(cand["subject_key"], principal=principal)
                if found is not None:
                    target = found

            claim_payload = ClaimCreateInput(
                subject_id=target.entity_id,
                subject_canonical_key=target.canonical_key,
                predicate=cand["predicate"],
                object_value=cand["object_value"],
                context_purpose=cand.get("context_purpose", "general"),
                scope_type="firm",
                scope_id="firm",
                channel="fact",
                epistemic_status="unverified_observation",
                kind="observation",
                is_mandatory=False,
                facets=cand.get("facets")
                or ClaimFacets(
                    domain_nature="non_financial",
                    modality="qualitative",
                    epistemic_temporal="actual",
                    periodicity="point_in_time",
                    ingestion_channel="manual_upload",
                ),
                source_system=f"manual-upload:{safe_name}",
                evidence_uri=rel_uri,
                content_sha256=content_sha256,
                span_locator=cand.get("span_locator", "file=1"),
                excerpt=cand.get("excerpt", extracted_preview[:240]),
                wall_side=ws,
                read_acl_groups=list(read_groups),
                write_acl_groups=list(target.write_acl_groups),
                evidence_acl_groups=list(ev_groups),
                valid_from=cand.get("valid_from"),
                force_supersede=False,
            )
            res = await store.write_claim(claim_payload, principal=principal)
            quarantined_ids.append(res.claim.claim_id)

            # Attach topic if specified
            if topic_slug:
                topics = await store.list_topics(principal=principal)
                matched_topic = next((t for t in topics if t.slug == topic_slug or t.topic_id == topic_slug), None)
                if matched_topic is not None:
                    await store.attach_topic(
                        TopicAttachment(
                            attachment_id=f"att-upl-{uuid.uuid4().hex[:10]}",
                            topic_id=matched_topic.topic_id,
                            target_type="claim",
                            target_id=res.claim.claim_id,
                            attachment_source="user",
                            wall_side=ws,
                            read_acl_groups=list(read_groups),
                        )
                    )

        # Redact preview in returned upload DTO ONLY if caller themselves lacks evidence_acl_groups
        caller_can_read_ev = (not ev_groups) or bool(principal.groups.intersection(ev_groups))
        record = {
            "upload_id": upload_id,
            "filename": safe_name,
            "media_type": media_type,
            "content_sha256": content_sha256,
            "relative_path": rel_uri,
            "wall_side": ws.value,
            "read_acl_groups": list(read_groups),
            "evidence_acl_groups": list(ev_groups),
            "uploaded_by": principal.user_id,
            "uploaded_at": format_utc(utc_now()),
            "extracted_preview": extracted_preview,
            "quarantined_claim_ids": quarantined_ids,
            "notes": notes or "",
            "epistemic_status": "unverified_observation",
        }
        self._records[upload_id] = record
        if self._persist_records:
            put_json(self._blobs, f"records/{upload_id}.json", record)

        return UploadIngestResult(
            upload_id=upload_id,
            filename=safe_name,
            media_type=media_type,
            content_sha256=content_sha256,
            extracted_preview=extracted_preview if caller_can_read_ev else REDACTED_EVIDENCE_LABEL,
            quarantined_claim_ids=quarantined_ids,
            epistemic_status="unverified_observation",
        )

    async def _extract_candidates(
        self,
        *,
        suffix: str,
        stored_bytes: bytes,
        principal: Principal,
        store: StorePort,
        default_subject_key: str | None,
        notes: str | None,
    ) -> tuple[str, list[dict[str, Any]]]:
        candidates: list[dict[str, Any]] = []

        if suffix == ".pdf":
            try:
                reader = PdfReader(io.BytesIO(stored_bytes))
                pages = [page.extract_text() or "" for page in reader.pages]
                full_text = normalize_lf_text("\n".join(pages)).strip()
            except Exception as exc:
                raise ValidationError(f"Failed to parse PDF artifact: {exc}") from exc

            preview = full_text[:500]
            codes = ADMIN_CODE_RE.findall(full_text)
            if codes:
                candidates.append(
                    {
                        "subject_key": default_subject_key or "veh:alpha-credit-offshore-lp",
                        "predicate": "has_admin_code",
                        "object_value": codes[0],
                        "context_purpose": "legal_vehicle",
                        "span_locator": "page=1",
                        "excerpt": preview[:280],
                        "facets": ClaimFacets(
                            domain_nature="non_financial",
                            modality="qualitative",
                            epistemic_temporal="actual",
                            actual_stage="unaudited",
                            periodicity="evergreen",
                            ingestion_channel="manual_upload",
                        ),
                    }
                )
            else:
                candidates.append(
                    {
                        "subject_key": default_subject_key,
                        "predicate": "uploaded_document_observation",
                        "object_value": (notes or preview[:160] or "Uploaded PDF artifact").strip(),
                        "context_purpose": "general",
                        "span_locator": "page=1",
                        "excerpt": preview[:280],
                        "facets": ClaimFacets(
                            domain_nature="non_financial",
                            modality="qualitative",
                            epistemic_temporal="actual",
                            periodicity="point_in_time",
                            ingestion_channel="manual_upload",
                        ),
                    }
                )
            return preview, candidates

        text = stored_bytes.decode("utf-8", errors="replace")

        if suffix == ".csv":
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
            preview = "\n".join(text.splitlines()[:8])
            for idx, raw_row in enumerate(rows[:25], start=2):
                row = {
                    (k or "").strip().lower().replace(" ", "_"): (v or "").strip()
                    for k, v in raw_row.items()
                    if k is not None
                }
                subj = (
                    row.get("vehicle_key")
                    or row.get("canonical_key")
                    or row.get("subject_canonical_key")
                    or row.get("entity_key")
                    or default_subject_key
                )
                if "proposed_admin_code" in row or "admin_code" in row:
                    val = row.get("proposed_admin_code") or row.get("admin_code") or ""
                    pred = "has_admin_code"
                    purpose = "legal_vehicle"
                else:
                    pred = row.get("predicate") or "csv_row_observation"
                    val = row.get("object_value") or row.get("value") or json.dumps(row)
                    purpose = row.get("context_purpose") or "general"

                eff_dt: datetime | None = None
                if row.get("effective_date"):
                    try:
                        eff_dt = ensure_utc(row["effective_date"])
                    except Exception:
                        eff_dt = None

                if val:
                    candidates.append(
                        {
                            "subject_key": subj,
                            "predicate": pred,
                            "object_value": val,
                            "context_purpose": purpose,
                            "span_locator": f"row={idx}",
                            "excerpt": row.get("source_note") or f"CSV row {idx}: {pred}={val}",
                            "valid_from": eff_dt,
                            "facets": ClaimFacets(
                                domain_nature="non_financial",
                                modality="qualitative",
                                epistemic_temporal="actual",
                                actual_stage="unaudited",
                                periodicity="evergreen",
                                ingestion_channel="manual_upload",
                            ),
                        }
                    )
            if not candidates:
                candidates.append(
                    {
                        "subject_key": default_subject_key,
                        "predicate": "uploaded_csv_observation",
                        "object_value": notes or preview[:160] or "Uploaded CSV",
                        "span_locator": "row=1",
                        "excerpt": preview[:240],
                    }
                )
            return preview, candidates

        if suffix == ".json":
            preview = text[:500]
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"Invalid JSON upload: {exc}") from exc

            entries = parsed if isinstance(parsed, list) else parsed.get("claims", [parsed])
            for idx, item in enumerate(entries[:20]):
                if isinstance(item, dict):
                    candidates.append(
                        {
                            "subject_key": item.get("subject_canonical_key")
                            or item.get("canonical_key")
                            or default_subject_key,
                            "predicate": str(item.get("predicate") or "uploaded_json_observation"),
                            "object_value": str(item.get("object_value") or item.get("value") or json.dumps(item)[:200]),
                            "context_purpose": str(item.get("context_purpose") or "general"),
                            "span_locator": f"item={idx}",
                            "excerpt": str(item.get("excerpt") or notes or preview[:200]),
                        }
                    )
            if not candidates:
                candidates.append(
                    {
                        "subject_key": default_subject_key,
                        "predicate": "uploaded_json_observation",
                        "object_value": notes or preview[:160],
                        "span_locator": "root",
                        "excerpt": preview[:240],
                    }
                )
            return preview, candidates

        # Markdown / plain text
        fm, body = _parse_frontmatter_text(text)
        preview = (body or text)[:500]
        subj = (
            fm.get("subject_canonical_key")
            or fm.get("issuer_key")
            or fm.get("entity_key")
            or (fm.get("related_entities")[0] if isinstance(fm.get("related_entities"), list) and fm.get("related_entities") else None)
            or default_subject_key
        )
        ep_temp = fm.get("epistemic_temporal") if fm.get("epistemic_temporal") in ("actual", "estimate", "forecast", "guidance", "pro_forma") else "actual"
        domain_nat = fm.get("domain_nature") if fm.get("domain_nature") in ("financial", "non_financial") else ("financial" if ep_temp in ("forecast", "estimate", "guidance") else "non_financial")
        modality = fm.get("modality") if fm.get("modality") in ("quantitative", "qualitative") else "qualitative"
        candidates.append(
            {
                "subject_key": subj,
                "predicate": str(fm.get("predicate") or ("analyst_forecast" if ep_temp == "forecast" else "uploaded_memo_observation")),
                "object_value": str(fm.get("headline") or fm.get("title") or notes or body.splitlines()[0] if body.splitlines() else "Uploaded memo")[:240],
                "context_purpose": str(fm.get("context_purpose") or "general"),
                "span_locator": "lines=1-20",
                "excerpt": preview[:280],
                "facets": ClaimFacets(
                    domain_nature=domain_nat,
                    modality=modality,
                    epistemic_temporal=ep_temp,
                    periodicity=fm.get("periodicity") if fm.get("periodicity") in ("point_in_time", "daily", "monthly", "quarterly", "ltm", "annual", "multi_year", "evergreen") else "point_in_time",
                    fiscal_period=fm.get("fiscal_period"),
                    ingestion_channel="manual_upload",
                ),
            }
        )
        return preview, candidates

    def _sanitize_source_for_principal(self, rec: dict[str, Any], principal: Principal) -> dict[str, Any] | None:
        if principal.revoked:
            return None
        if not wall_allows(principal.wall_side, rec["wall_side"]):
            return None
        if not principal.groups.intersection(rec["read_acl_groups"]):
            return None
        ev_groups = rec.get("evidence_acl_groups") or []
        can_ev = (not ev_groups) or bool(principal.groups.intersection(ev_groups))
        out = dict(rec)
        if not can_ev:
            out["relative_path"] = None
            out["extracted_preview"] = None
            out["evidence_redacted"] = True
            out["evidence_redaction_label"] = REDACTED_EVIDENCE_LABEL
            out["evidence_request_group"] = ev_groups[0]
        else:
            out["evidence_redacted"] = False
            out["evidence_redaction_label"] = None
            out["evidence_request_group"] = None
        return out

    def list_sources(self, *, principal: Principal) -> list[dict[str, Any]]:
        self._load_records()
        out: list[dict[str, Any]] = []
        for rec in self._records.values():
            san = self._sanitize_source_for_principal(rec, principal)
            if san is not None:
                out.append(san)
        out.sort(key=lambda r: r.get("uploaded_at") or "", reverse=True)
        return out

    def get_source(self, upload_id: str, *, principal: Principal) -> dict[str, Any] | None:
        self._load_records()
        rec = self._records.get(upload_id)
        if rec is None:
            return None
        return self._sanitize_source_for_principal(rec, principal)
