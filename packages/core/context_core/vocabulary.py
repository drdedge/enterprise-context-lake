"""Steward-editable vocabularies (ADR-008).

Entity types, identifier schemes, predicates and the tag values used on claims, positions, coverage
and lineage used to be hard-coded enums, so anything new needed a code change and a deploy. They now
live in a ``vocab_terms`` table that stewards edit through change sets, and each vocabulary can be a
tree (``parent_key``), so a query for a broad type also returns its narrower types.

Unknown values are never rejected. A write that uses a term nobody has defined yet stores the record
as it is and registers the term with ``status="proposed"`` (entity types hang under ``thing``), so a
steward can review it, give it a parent and a label, merge it into an existing term, or deprecate it.

Only the values that code branches on stay fixed in ``models.py``: wall side, claim channel,
epistemic status, scope type, entity status, domain nature and modality.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from context_core.compression import ensure_utc, utc_now

VocabStatus = Literal["active", "proposed", "deprecated"]

# Vocabulary name -> one-line description. A vocabulary not listed here can still hold terms
# (stewards can add one by proposing its first term); these are the ones the lake writes to itself.
VOCABULARIES: dict[str, str] = {
    "entity_type": "Kinds of entity, as a tree (for example issuer under organisation under party)",
    "identifier_scheme": "External and internal identifier schemes for the identifier crosswalk",
    "predicate": "Names of the facts and relationships claims assert",
    "lineage_event_type": "Kinds of rename, merger, spin-off and restructuring event",
    "position_type": "Kinds of fund position",
    "coverage_role": "Roles an analyst or deal lead holds on an issuer",
    "coverage_status": "Coverage status of an issuer (held, watchlist, pipeline and so on)",
    "unit": "Units of quantitative claims",
    "accounting_basis": "Accounting bases of quantitative claims",
    "epistemic_temporal": "Whether a value is an actual, estimate, forecast, guidance or pro forma",
    "actual_stage": "Audit stage of an actual value",
    "periodicity": "Period a value covers",
    "ingestion_channel": "How a claim reached the lake",
}

ROOT_ENTITY_TYPE = "thing"

_KEY_RE = re.compile(r"[^a-z0-9_.:/-]+")


def normalize_term_key(value: Any) -> str:
    """Lower-case, trimmed, spaces to underscores. Accepts enum members."""
    raw = getattr(value, "value", value)
    text = str(raw).strip().lower()
    text = _KEY_RE.sub("_", text).strip("_")
    return text


def term_id_for(vocab: str, key: str) -> str:
    return f"{vocab}:{key}"


def label_for(key: str) -> str:
    return key.replace("_", " ").replace("-", " ").strip().capitalize() or key


class VocabTerm(BaseModel):
    """One term in a vocabulary. ``term_id`` is ``<vocab>:<key>`` and never changes."""

    term_id: str = ""
    vocab: str
    key: str
    label: str = ""
    parent_key: str | None = None
    description: str = ""
    aliases: list[str] = Field(default_factory=list)
    status: VocabStatus = "active"
    merged_into_key: str | None = None
    # Free-form, per-vocabulary settings, for example a predicate's {"object": "entity"} or an
    # entity type's JSON Schema for ``attributes``. Not interpreted by the store.
    attributes: dict[str, Any] = Field(default_factory=dict)
    owner_group: str = "fund-stewards"
    created_by: str = "system"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("vocab", "key", mode="before")
    @classmethod
    def _norm_key(cls, value: Any) -> str:
        key = normalize_term_key(value)
        if not key:
            raise ValueError("Vocabulary and key must not be empty")
        return key

    @field_validator("parent_key", "merged_into_key", mode="before")
    @classmethod
    def _norm_opt_key(cls, value: Any) -> str | None:
        if value is None or value == "":
            return None
        return normalize_term_key(value)

    @field_validator("created_at", "updated_at", mode="before")
    @classmethod
    def _norm_dt(cls, value: Any) -> datetime:
        return ensure_utc(value) or utc_now()

    @model_validator(mode="after")
    def _fill(self) -> VocabTerm:
        self.term_id = term_id_for(self.vocab, self.key)
        if not self.label:
            self.label = label_for(self.key)
        if self.parent_key == self.key:
            raise ValueError("A term cannot be its own parent")
        return self


class VocabularyIndex:
    """In-memory view of all terms: lookups, aliases, merges and tree expansion."""

    def __init__(self, terms: Iterable[VocabTerm]) -> None:
        self._terms: dict[tuple[str, str], VocabTerm] = {}
        self._children: dict[tuple[str, str], list[str]] = {}
        self._aliases: dict[tuple[str, str], str] = {}
        for t in terms:
            self._terms[(t.vocab, t.key)] = t
        for t in self._terms.values():
            if t.parent_key:
                self._children.setdefault((t.vocab, t.parent_key), []).append(t.key)
            for alias in t.aliases:
                self._aliases.setdefault((t.vocab, normalize_term_key(alias)), t.key)

    def __contains__(self, item: tuple[str, str]) -> bool:
        return item in self._terms

    def get(self, vocab: str, key: str) -> VocabTerm | None:
        return self._terms.get((vocab, normalize_term_key(key)))

    def terms(self, vocab: str | None = None) -> list[VocabTerm]:
        return sorted(
            (t for (v, _k), t in self._terms.items() if vocab is None or v == vocab),
            key=lambda t: (t.vocab, t.key),
        )

    def is_known(self, vocab: str, key: str) -> bool:
        k = normalize_term_key(key)
        return (vocab, k) in self._terms or (vocab, k) in self._aliases

    def canonical(self, vocab: str, key: str) -> str:
        """Follow aliases and merges to the term a key now means (the key itself if unknown)."""
        k = normalize_term_key(key)
        k = self._aliases.get((vocab, k), k)
        seen: set[str] = set()
        while k not in seen:
            seen.add(k)
            t = self._terms.get((vocab, k))
            if t is None or not t.merged_into_key:
                break
            k = t.merged_into_key
        return k

    def descendants(self, vocab: str, key: str) -> set[str]:
        """The key itself, every narrower key, and every key merged into one of them."""
        root = self.canonical(vocab, key)
        out: set[str] = set()
        frontier = [root]
        while frontier:
            cur = frontier.pop()
            if cur in out:
                continue
            out.add(cur)
            frontier.extend(self._children.get((vocab, cur), []))
        merged = {k for (v, k), t in self._terms.items() if v == vocab and t.merged_into_key and self.canonical(vocab, k) in out}
        out |= merged
        out |= {alias for (v, alias), target in self._aliases.items() if v == vocab and target in out}
        return out

    def ancestors(self, vocab: str, key: str) -> list[str]:
        """Parents from nearest to the root, stopping at a cycle."""
        out: list[str] = []
        t = self.get(vocab, key)
        while t is not None and t.parent_key and t.parent_key not in out and t.parent_key != t.key:
            out.append(t.parent_key)
            t = self._terms.get((vocab, t.parent_key))
        return out

    def expand(self, vocab: str, keys: Sequence[str] | None) -> set[str] | None:
        if not keys:
            return None
        out: set[str] = set()
        for k in keys:
            out |= self.descendants(vocab, k)
        return out


# ---------------------------------------------------------------------------
# Default terms (seeded into an empty store)
# ---------------------------------------------------------------------------

# Upper-level entity types give every new type somewhere to hang. The existing types sit under them.
_ENTITY_TYPE_TREE: list[tuple[str, str | None, str]] = [
    (ROOT_ENTITY_TYPE, None, "Anything the lake knows about. Unclassified new types land here."),
    ("party", ROOT_ENTITY_TYPE, "A person or organisation that can hold a role"),
    ("organisation", "party", "A company, fund manager, institution or other body"),
    ("person", "party", "An individual"),
    ("product", ROOT_ENTITY_TYPE, "The firm's investment products, from strategy down to share class"),
    ("instrument", ROOT_ENTITY_TYPE, "A security, loan or other tradable instrument"),
    ("agreement", ROOT_ENTITY_TYPE, "A commitment, facility, side letter or other agreement"),
    ("event", ROOT_ENTITY_TYPE, "A corporate action, meeting, deal milestone or other event"),
    ("place", ROOT_ENTITY_TYPE, "A country, region or other place"),
    ("document", ROOT_ENTITY_TYPE, "A document or note"),
    ("system", ROOT_ENTITY_TYPE, "An application, data source or agent skill"),
    ("initiative", ROOT_ENTITY_TYPE, "A project or programme of work"),
    ("concept", ROOT_ENTITY_TYPE, "A topic or other abstract idea"),
    # Investment product hierarchy
    ("strategy", "product", "Investment strategy"),
    ("fund_family", "product", "Fund family"),
    ("reporting_group", "product", "Reporting or consolidated group"),
    ("legal_vehicle", "product", "Legal fund vehicle"),
    ("share_class", "product", "Share class of a vehicle"),
    # Market and credit hierarchy
    ("ultimate_parent", "organisation", "Ultimate parent company"),
    ("pe_sponsor", "organisation", "Private equity sponsor"),
    ("issuer", "organisation", "Issuer of instruments, held or not"),
    # Party, client and distribution
    ("legal_party", "organisation", "Canonical legal party (one per LEI) that can hold several roles"),
    ("client", "organisation", "Client or limited partner"),
    ("consultant", "organisation", "Investment consultant"),
    ("commitment", "agreement", "An LP commitment to a vehicle"),
    ("opportunity", "agreement", "A prospective allocation"),
    # Operational and knowledge
    ("app", "system", "Application"),
    ("skill", "system", "Agent skill"),
    ("data_source", "system", "Source system or data feed"),
    ("user", "person", "A user of the lake"),
    ("project", "initiative", "Project"),
    ("topic", "concept", "Topic"),
    ("document_artifact", "document", "Document or uploaded file"),
    ("knowledge_item", "document", "Knowledge factory note"),
]

_FLAT_DEFAULTS: dict[str, list[str]] = {
    "identifier_scheme": [
        "lei", "openfigi", "cusip", "isin", "sedol", "bloomberg_ticker", "admin_code", "crm_id",
        "oms_id", "gips_code", "cik", "internal_slug",
    ],
    "lineage_event_type": [
        "renamed_from", "merged_into", "spun_off_from", "acquired_by", "lme_restructured_to", "split_into",
    ],
    "position_type": ["long", "short", "unfunded_commitment", "credit_facility"],
    "coverage_role": ["primary_analyst", "secondary_analyst", "deal_lead", "pm_sponsor"],
    "coverage_status": ["held", "watchlist", "pipeline", "benchmark", "passed"],
    "unit": ["bps", "pct", "usd", "x", "count"],
    "accounting_basis": ["gaap", "ifrs", "non_gaap", "gips", "nav_net", "nav_gross"],
    "epistemic_temporal": ["actual", "estimate", "forecast", "guidance", "pro_forma"],
    "actual_stage": ["prelim", "unaudited", "audited", "restated"],
    "periodicity": ["point_in_time", "daily", "monthly", "quarterly", "ltm", "annual", "multi_year", "evergreen"],
    "ingestion_channel": ["automated_feed", "manual_upload", "human_authored", "agent_extracted"],
}

# Predicates named in ADR-001 or used by the institutional fixtures. object = "entity" means the
# claim's object_entity_id points at another entity; "literal" means object_value holds the value.
_PREDICATES: list[tuple[str, str]] = [
    ("has_fund_family", "entity"),
    ("has_reporting_group", "entity"),
    ("includes_vehicle", "entity"),
    ("has_share_class", "entity"),
    ("sponsors_issuer", "entity"),
    ("issues_instrument", "entity"),
    ("controls", "entity"),
    ("acts_as_issuer", "entity"),
    ("acts_as_lp_client", "entity"),
    ("acts_as_counterparty", "entity"),
    ("acts_as_sponsor", "entity"),
    ("holds_commitment", "entity"),
    ("pursues_opportunity", "entity"),
    ("committed_to_vehicle", "entity"),
    ("targets_vehicle", "entity"),
    ("advises_client", "entity"),
    ("hosts_skill", "entity"),
    ("maps_as", "literal"),
    ("has_admin_code", "literal"),
    ("legal_display_name", "literal"),
    ("presentation_format", "literal"),
    ("reporting_disclosure_rule", "literal"),
    ("composite_nav_usd", "literal"),
    ("composite_net_irr_pct", "literal"),
    ("underwriting_term_sheet_spread", "literal"),
    ("confidential_lme_settlement_term", "literal"),
]


def default_terms() -> list[VocabTerm]:
    now = utc_now()
    out: list[VocabTerm] = []
    for key, parent, desc in _ENTITY_TYPE_TREE:
        out.append(
            VocabTerm(vocab="entity_type", key=key, parent_key=parent, description=desc, created_at=now, updated_at=now)
        )
    for vocab, keys in _FLAT_DEFAULTS.items():
        for key in keys:
            out.append(VocabTerm(vocab=vocab, key=key, created_at=now, updated_at=now))
    for key, obj in _PREDICATES:
        out.append(VocabTerm(vocab="predicate", key=key, attributes={"object": obj}, created_at=now, updated_at=now))
    return out


def proposed_term(vocab: str, key: str, *, created_by: str = "system") -> VocabTerm:
    """A placeholder for a term first seen in data. Entity types hang under ``thing`` until reviewed."""
    return VocabTerm(
        vocab=vocab,
        key=key,
        parent_key=ROOT_ENTITY_TYPE if vocab == "entity_type" else None,
        status="proposed",
        description="Registered automatically the first time it was used; awaiting steward review.",
        created_by=created_by,
    )


# ---------------------------------------------------------------------------
# Which vocabulary terms a record uses
# ---------------------------------------------------------------------------


def _pairs(*items: tuple[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for vocab, value in items:
        if value is None or value == "":
            continue
        key = normalize_term_key(value)
        if key:
            out.append((vocab, key))
    return out


def terms_used(record_kind: str, record: Any) -> list[tuple[str, str]]:
    """(vocab, key) pairs a domain record refers to. ``record_kind`` is the state-dict key."""
    if record_kind == "entities":
        return _pairs(("entity_type", record.entity_type))
    if record_kind == "identifiers":
        return _pairs(("identifier_scheme", record.scheme))
    if record_kind == "lineage_events":
        return _pairs(("lineage_event_type", record.event_type))
    if record_kind == "positions":
        return _pairs(("position_type", record.position_type))
    if record_kind == "coverages":
        return _pairs(("coverage_role", record.coverage_role), ("coverage_status", record.coverage_status))
    if record_kind == "claims":
        f = record.facets
        return _pairs(
            ("predicate", record.predicate),
            ("unit", f.unit),
            ("accounting_basis", f.accounting_basis),
            ("epistemic_temporal", f.epistemic_temporal),
            ("actual_stage", f.actual_stage),
            ("periodicity", f.periodicity),
            ("ingestion_channel", f.ingestion_channel),
        )
    return []


def terms_used_in_state(state: dict[str, Any]) -> list[tuple[str, str]]:
    seen: dict[tuple[str, str], None] = {}
    for kind in ("entities", "identifiers", "lineage_events", "positions", "coverages", "claims"):
        for rec in state.get(kind, []) or []:
            for pair in terms_used(kind, rec):
                seen.setdefault(pair, None)
    return list(seen)


def validate_term_change(term: VocabTerm, index: VocabularyIndex) -> None:
    """Reject a parent or merge target that does not exist, or a parent chain that loops."""
    from context_core.exceptions import ValidationError

    if term.parent_key is not None and not index.is_known(term.vocab, term.parent_key):
        raise ValidationError(f"Unknown parent {term.vocab}:{term.parent_key}")
    if term.merged_into_key is not None:
        if term.merged_into_key == term.key:
            raise ValidationError("A term cannot be merged into itself")
        if not index.is_known(term.vocab, term.merged_into_key):
            raise ValidationError(f"Unknown merge target {term.vocab}:{term.merged_into_key}")
    # Walk up from the new parent; reaching this term again would make a loop.
    cur = term.parent_key
    seen: set[str] = set()
    while cur is not None and cur not in seen:
        if cur == term.key:
            raise ValidationError(f"Parent {term.parent_key} would make a loop in {term.vocab}")
        seen.add(cur)
        parent = index.get(term.vocab, cur)
        cur = parent.parent_key if parent is not None else None


__all__ = [
    "ROOT_ENTITY_TYPE",
    "VOCABULARIES",
    "VocabStatus",
    "VocabTerm",
    "VocabularyIndex",
    "default_terms",
    "label_for",
    "normalize_term_key",
    "proposed_term",
    "term_id_for",
    "terms_used",
    "terms_used_in_state",
    "validate_term_change",
]
