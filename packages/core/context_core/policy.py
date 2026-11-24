"""Fail-closed PolicyEngine, MNPI wall barriers, Entra JWT/JWKS + >200-group overage, and URI hygiene (ADR-003)."""

from __future__ import annotations

import ipaddress
import json
import os
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import jwt
from cachetools import TTLCache

from context_core.compression import to_posix_path
from context_core.exceptions import (
    AuthenticationError,
    AuthorizationError,
    DirectoryUnavailableError,
    SurfacePolicyError,
    ValidationError,
)
from context_core.models import (
    clone_model,
    REDACTED_EVIDENCE_LABEL,
    ActorMode,
    ChannelMode,
    Claim,
    Entity,
    EntityLineageEvent,
    Principal,
    WallSide,
)

# Sensitive query parameter names stripped before storage/logging/serialization (ADR-003 §5, ADR-004 §1)
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "token",
        "access_token",
        "id_token",
        "refresh_token",
        "key",
        "apikey",
        "api_key",
        "sig",
        "signature",
        "secret",
        "password",
        "passwd",
        "auth",
        "authorization",
        "sv",
        "se",
        "sp",
        "sr",
        "st",
        "spr",
        "ss",
        "srt",
    }
)

_FORBIDDEN_SSRF_SCHEMES = frozenset({"file", "gopher", "ftp", "dict", "ldap", "ldaps", "tftp", "jar"})
_ALLOWED_URI_SCHEMES = frozenset({"", "https", "http", "s3", "dataroom", "okf", "context", "gs", "azblob"})


def sanitize_uri_secrets(uri: str | None) -> str | None:
    """Strip credential/SAS/presigned query parameters from any URI while preserving POSIX path."""
    if uri is None:
        return None
    raw = to_posix_path(uri.strip())
    if not raw or "?" not in raw:
        return raw
    parsed = urlparse(raw)
    if not parsed.query:
        return raw
    kept_pairs: list[tuple[str, str]] = []
    for k, v in parse_qsl(parsed.query, keep_blank_values=True):
        kl = k.lower()
        if kl in _SENSITIVE_QUERY_KEYS or kl.startswith("x-amz-") or kl.startswith("x-goog-"):
            continue
        kept_pairs.append((k, v))
    clean_query = urlencode(kept_pairs, doseq=True)
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, clean_query, parsed.fragment))


def validate_no_ssrf(uri: str | None) -> str | None:
    """Validate that a URI does not target loopback, link-local/cloud metadata, private IPs, or dangerous schemes."""
    if uri is None:
        return None
    cleaned = sanitize_uri_secrets(uri)
    assert cleaned is not None
    if not cleaned:
        return cleaned

    parsed = urlparse(cleaned)
    scheme = parsed.scheme.lower()
    if len(scheme) == 1 and scheme.isalpha() and cleaned[1:2] == ":":
        raise ValidationError(f"Blocked forbidden local filesystem path: {cleaned}")
    if cleaned.startswith("/") or cleaned.startswith(".."):
        raise ValidationError(f"Blocked forbidden absolute/traversal path: {cleaned}")
    if scheme in _FORBIDDEN_SSRF_SCHEMES:
        raise ValidationError(f"Blocked forbidden URI scheme: {scheme}")
    if scheme and scheme not in _ALLOWED_URI_SCHEMES:
        raise ValidationError(f"Unsupported URI scheme: {scheme}")

    if scheme in ("http", "https"):
        host = (parsed.hostname or "").strip().lower()
        if not host:
            raise ValidationError("HTTP(S) URI missing host")
        if host in ("localhost", "localhost.localdomain", "metadata.google.internal", "metadata"):
            raise ValidationError(f"Blocked SSRF host: {host}")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None:
            if (
                ip.is_loopback
                or ip.is_link_local
                or ip.is_private
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            ):
                raise ValidationError(f"Blocked SSRF IP address: {host}")
    return cleaned


def wall_allows(principal_wall: WallSide | str, record_wall: WallSide | str) -> bool:
    """Enforce the ADR-003 §2 wall_side information-barrier matrix."""
    pw = principal_wall.value if isinstance(principal_wall, WallSide) else str(principal_wall)
    rw = record_wall.value if isinstance(record_wall, WallSide) else str(record_wall)
    if pw == WallSide.above_the_wall.value:
        return True
    if pw == WallSide.private_side.value:
        return rw in (WallSide.public_side.value, WallSide.private_side.value)
    return rw == WallSide.public_side.value


class PolicyEngine:
    """Authoritative fail-closed PolicyPort implementation (ADR-003)."""

    def is_memory_allowed(self, principal: Principal) -> bool:
        """Personal memory & user/user_x_skill claims are enabled ONLY for active OBO direct sessions."""
        if principal.revoked:
            return False
        return principal.actor_mode == "obo" and principal.channel_mode == "direct"

    def can_read_record(
        self,
        principal: Principal,
        *,
        wall_side: WallSide | str,
        read_acl_groups: list[str] | frozenset[str] | set[str],
    ) -> bool:
        """Generic fail-closed record-level check (wall_side barrier + group intersection + not revoked)."""
        if principal.revoked:
            return False
        if not wall_allows(principal.wall_side, wall_side):
            return False
        if not read_acl_groups:
            return False
        return bool(principal.groups.intersection(read_acl_groups))

    def can_read_entity(self, principal: Principal, entity: Entity) -> bool:
        return self.can_read_record(
            principal,
            wall_side=entity.wall_side,
            read_acl_groups=entity.read_acl_groups,
        )

    def can_read_claim(self, principal: Principal, claim: Claim) -> bool:
        if principal.revoked:
            return False
        if claim.scope_type in ("user", "user_x_skill"):
            if not self.is_memory_allowed(principal):
                return False
            if claim.scope_type == "user" and claim.scope_id != principal.user_id:
                return False
            if claim.scope_type == "user_x_skill" and not claim.scope_id.startswith(f"{principal.user_id}:"):
                return False
        if not wall_allows(principal.wall_side, claim.acl.wall_side):
            return False
        if not claim.acl.read_acl_groups:
            return False
        return bool(principal.groups.intersection(claim.acl.read_acl_groups))

    def can_read_evidence(self, principal: Principal, claim: Claim) -> bool:
        if not self.can_read_claim(principal, claim):
            return False
        ev_groups = claim.acl.evidence_acl_groups
        if not ev_groups:
            return True
        return bool(principal.groups.intersection(ev_groups))

    def sanitize_claim_for_principal(self, principal: Principal, claim: Claim) -> Claim | None:
        """Return caller-safe deep copy of claim with server-side evidence redaction, or None if unauthorized."""
        if not self.can_read_claim(principal, claim):
            return None
        sanitized = clone_model(claim)
        if self.can_read_evidence(principal, claim):
            sanitized.evidence_redacted = False
            sanitized.evidence_redaction_label = None
            sanitized.evidence_request_group = None
            if sanitized.provenance.evidence_uri:
                sanitized.provenance.evidence_uri = sanitize_uri_secrets(sanitized.provenance.evidence_uri)
        else:
            sanitized.provenance.evidence_uri = None
            sanitized.provenance.excerpt = None
            sanitized.provenance.span_locator = None
            sanitized.evidence_redacted = True
            sanitized.evidence_redaction_label = REDACTED_EVIDENCE_LABEL
            sanitized.evidence_request_group = (
                claim.acl.evidence_acl_groups[0] if claim.acl.evidence_acl_groups else "compliance-oversight"
            )
        return sanitized

    def sanitize_lineage_for_principal(
        self, principal: Principal, event: EntityLineageEvent
    ) -> EntityLineageEvent | None:
        """Filter and redact lineage event evidence URI when evidence_acl_groups fails."""
        if not self.can_read_record(
            principal, wall_side=event.wall_side, read_acl_groups=event.read_acl_groups
        ):
            return None
        sanitized = clone_model(event)
        ev_groups = event.evidence_acl_groups
        if ev_groups and not principal.groups.intersection(ev_groups):
            sanitized.evidence_uri = None
            sanitized.evidence_redacted = True
            sanitized.evidence_redaction_label = REDACTED_EVIDENCE_LABEL
        else:
            sanitized.evidence_redacted = False
            sanitized.evidence_redaction_label = None
            if sanitized.evidence_uri:
                sanitized.evidence_uri = sanitize_uri_secrets(sanitized.evidence_uri)
        return sanitized

    def authorize_write(
        self,
        principal: Principal,
        *,
        target_entity: Entity,
        scope_type: str,
        epistemic_status: str,
        force_supersede: bool,
        is_mandatory: bool = False,
        resolving_conflict: bool = False,
    ) -> None:
        """Enforce steward write RBAC, wall barriers, surface gating, and anti-poisoning quarantine."""
        if principal.revoked:
            raise AuthenticationError("Revoked principal cannot perform write operations")

        if scope_type in ("user", "user_x_skill"):
            if not self.is_memory_allowed(principal):
                raise SurfacePolicyError(
                    "Personal memory writes are disabled on shared channels and service identities"
                )
            if not self.can_read_entity(principal, target_entity):
                raise AuthorizationError("Caller lacks read access to target entity")
            return

        if not wall_allows(principal.wall_side, target_entity.wall_side):
            raise AuthorizationError("Information barrier prohibits writing across wall_side")

        if not self.can_read_entity(principal, target_entity):
            raise AuthorizationError("Caller lacks access to target entity")

        is_privileged = (
            epistemic_status == "approved_fact"
            or is_mandatory
            or force_supersede
            or resolving_conflict
        )
        if is_privileged:
            if not target_entity.write_acl_groups or not principal.groups.intersection(
                target_entity.write_acl_groups
            ):
                raise AuthorizationError(
                    "Steward write authorization required for approved_fact, mandatory directives, "
                    "force_supersede, or conflict resolution"
                )

    def authorize_memory_access(self, principal: Principal, *, owner_user_id: str | None = None) -> None:
        """Enforce OBO + direct-channel surface gate and strict caller ownership for personal memory."""
        if principal.revoked:
            raise AuthenticationError("Revoked principal cannot access personal memory")
        if not self.is_memory_allowed(principal):
            raise SurfacePolicyError(
                "Personal memory and watchlists are automatically disabled in shared channels or service mode"
            )
        if owner_user_id is not None and owner_user_id != principal.user_id:
            raise AuthorizationError("Personal memory is strictly isolated to its owner")

    def can_access_snapshot_partition(
        self, principal: Principal, acl_tier: str, wall_side: WallSide | str
    ) -> bool:
        """Check whether a principal may download/load a compiled (acl_tier, wall_side) snapshot."""
        if principal.revoked:
            return False
        if not wall_allows(principal.wall_side, wall_side):
            return False
        return acl_tier in principal.groups

    def allowed_snapshot_partitions(self, principal: Principal) -> list[tuple[str, str]]:
        """Return ordered (acl_tier, wall_side) snapshot partitions accessible to ``principal``."""
        if principal.revoked:
            return []
        wall_candidates = [WallSide.public_side.value]
        if wall_allows(principal.wall_side, WallSide.private_side):
            wall_candidates.append(WallSide.private_side.value)
        if wall_allows(principal.wall_side, WallSide.above_the_wall):
            wall_candidates.append(WallSide.above_the_wall.value)

        preferred_tiers = [
            "firm-all",
            "custodian-legal-docs",
            "private-credit-deal-team",
            "mnpi-project-cobalt",
            "fund-stewards",
            "compliance-oversight",
        ]
        partitions: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for tier in preferred_tiers:
            if tier not in principal.groups:
                continue
            for ws in wall_candidates:
                if tier == "firm-all" and ws != WallSide.public_side.value:
                    continue
                if tier in ("private-credit-deal-team", "mnpi-project-cobalt") and ws == WallSide.public_side.value:
                    continue
                pair = (tier, ws)
                if pair not in seen:
                    seen.add(pair)
                    partitions.append(pair)
        return partitions

    def claim_eligible_for_partition(self, claim: Claim, *, acl_tier: str, wall_side: WallSide | str) -> bool:
        """Determine whether a claim may be physically stored inside a compiled (acl_tier, wall_side) snapshot.

        Enforces:
        - NEVER include personal (`user`, `user_x_skill`, `run`) claims in shared snapshots.
        - NEVER include quarantined (`unverified_observation`, `candidate_sop`) or `revoked` claims.
        - Strict wall_side isolation: a `public_side` snapshot NEVER contains `private_side` or `above_the_wall` claims!
        - Strict ACL tier match: `acl_tier` must be in `claim.acl.read_acl_groups` (or `"firm-all"` is included when
          compiling a broader `private_side` / `above_the_wall` partition only if `wall_allows(wall_side, claim.acl.wall_side)`
          and `claim.acl.read_acl_groups` contains either `acl_tier` or `"firm-all"`).
        """
        if claim.scope_type in ("user", "user_x_skill", "run"):
            return False
        if claim.epistemic_status in ("unverified_observation", "candidate_sop", "revoked"):
            return False
        ws = wall_side.value if isinstance(wall_side, WallSide) else str(wall_side)
        if not wall_allows(ws, claim.acl.wall_side):
            return False
        if acl_tier == "firm-all":
            return "firm-all" in claim.acl.read_acl_groups
        return acl_tier in claim.acl.read_acl_groups or "firm-all" in claim.acl.read_acl_groups

    def entity_eligible_for_partition(self, entity: Entity, *, acl_tier: str, wall_side: WallSide | str) -> bool:
        """Determine whether an entity may be physically stored inside a compiled (acl_tier, wall_side) snapshot."""
        ws = wall_side.value if isinstance(wall_side, WallSide) else str(wall_side)
        if not wall_allows(ws, entity.wall_side):
            return False
        if acl_tier == "firm-all":
            return "firm-all" in entity.read_acl_groups
        return acl_tier in entity.read_acl_groups or "firm-all" in entity.read_acl_groups

    def evidence_allowed_in_partition(self, claim: Claim, *, acl_tier: str) -> bool:
        """Determine whether raw evidence_uri/excerpt may be stored inside a compiled snapshot for ``acl_tier``."""
        ev_groups = claim.acl.evidence_acl_groups
        if not ev_groups:
            return True
        if acl_tier in ev_groups:
            return True
        if "firm-all" in ev_groups:
            return True
        return False


# ---------------------------------------------------------------------------
# Principal Directory, Entra >200-Group Overage Resolver & JWT Verifier
# ---------------------------------------------------------------------------


class MockDirectoryGroupResolver:
    """Deterministic Entra >200-group overage resolver backed by fixtures/principals.json."""

    def __init__(
        self,
        principals_path: Path | str | None = None,
        *,
        fail_mode: bool = False,
        cache_ttl_seconds: float = 60.0,
    ) -> None:
        self._fail_mode = fail_mode
        self._cache: TTLCache[tuple[str, int], frozenset[str]] = TTLCache(
            maxsize=512, ttl=cache_ttl_seconds
        )
        self._directory: dict[str, frozenset[str]] = {}
        if principals_path is not None:
            self.load_from_file(Path(principals_path))

    def set_fail_mode(self, fail_mode: bool) -> None:
        self._fail_mode = fail_mode

    def load_from_file(self, path: Path) -> None:
        doc = json.loads(path.read_text(encoding="utf-8"))
        for entry in doc.get("principals", []):
            uid = entry["user_id"]
            if entry.get("groups_overage"):
                core = list(entry.get("directory_core_groups", []))
                prefix = entry.get("directory_extra_group_prefix", "entra-sec-committee-")
                count = int(entry.get("directory_extra_group_count", 0))
                extra = [f"{prefix}{i:03d}" for i in range(1, count + 1)]
                self._directory[uid] = frozenset(core + extra)
            else:
                self._directory[uid] = frozenset(entry.get("groups", []))

    def register_user_groups(self, user_id: str, groups: set[str] | frozenset[str] | list[str]) -> None:
        self._directory[user_id] = frozenset(groups)

    def resolve_groups(self, user_id: str, *, policy_epoch: int = 1) -> frozenset[str]:
        if self._fail_mode:
            raise DirectoryUnavailableError(
                f"Entra group overage directory lookup failed for {user_id} (fail-closed)"
            )
        cache_key = (user_id, policy_epoch)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        if user_id not in self._directory:
            raise DirectoryUnavailableError(
                f"Entra group overage directory has no record for {user_id} (fail-closed)"
            )
        resolved = self._directory[user_id]
        self._cache[cache_key] = resolved
        return resolved


class PrincipalDirectory:
    """Loads seeded local mock personas and resolves principals cleanly for local dev and tests."""

    def __init__(
        self,
        principals_path: Path | str | None = None,
        *,
        group_resolver: MockDirectoryGroupResolver | None = None,
    ) -> None:
        self._raw_principals: dict[str, dict[str, Any]] = {}
        self.canaries: list[dict[str, Any]] = []
        self.expected_visibility: dict[str, dict[str, Any]] = {}
        self.group_resolver = group_resolver or MockDirectoryGroupResolver()
        if principals_path is not None:
            self.load_from_file(Path(principals_path))

    @classmethod
    def from_default_fixtures(cls, repo_root: Path | None = None) -> PrincipalDirectory:
        root = repo_root or Path(__file__).resolve().parents[3]
        path = root / "fixtures" / "principals.json"
        return cls(path)

    def load_from_file(self, path: Path) -> None:
        doc = json.loads(path.read_text(encoding="utf-8"))
        self._raw_principals = {p["user_id"]: p for p in doc.get("principals", [])}
        self.canaries = list(doc.get("canaries", []))
        self.expected_visibility = dict(doc.get("expected_visibility", {}))
        self.group_resolver.load_from_file(path)

    def list_personas(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for p in self._raw_principals.values():
            result.append(
                {
                    "user_id": p["user_id"],
                    "display_name": p["display_name"],
                    "email": p["email"],
                    "role_summary": p.get("role_summary", ""),
                    "roles": list(p.get("roles", [])),
                    "wall_side": p["wall_side"],
                    "revoked": bool(p.get("revoked", False)),
                    "groups_overage": bool(p.get("groups_overage", False)),
                }
            )
        return result

    def resolve_mock_principal(
        self,
        user_id: str,
        *,
        actor_mode: ActorMode = "obo",
        channel_mode: ChannelMode = "direct",
        force_overage: bool = False,
        policy_epoch: int = 1,
        revoked_override: bool | None = None,
    ) -> Principal:
        if os.environ.get("ENVIRONMENT", "").lower() == "production":
            raise AuthenticationError("AUTH_MODE=mock is prohibited when ENVIRONMENT=production")

        # Support synthetic service identity
        if actor_mode == "service" and user_id not in self._raw_principals:
            return Principal(
                user_id=user_id,
                display_name=f"Service Principal ({user_id})",
                email=f"{user_id.replace(':', '_')}@service.meridian.local",
                roles=frozenset({"service_runtime"}),
                groups=frozenset({"firm-all"}),
                wall_side=WallSide.public_side,
                auth_mode="local_mock",
                actor_mode="service",
                channel_mode=channel_mode,
                revoked=bool(revoked_override) if revoked_override is not None else False,
            )

        raw = self._raw_principals.get(user_id)
        if raw is None:
            raise AuthenticationError(f"Unknown mock principal: {user_id}")

        is_overage = bool(raw.get("groups_overage", False)) or force_overage
        groups_overage_resolved = False
        if is_overage:
            groups = self.group_resolver.resolve_groups(user_id, policy_epoch=policy_epoch)
            groups_overage_resolved = True
        else:
            groups = frozenset(raw.get("groups", []))

        revoked = bool(raw.get("revoked", False))
        if revoked_override is not None:
            revoked = revoked or revoked_override

        return Principal(
            user_id=raw["user_id"],
            display_name=raw["display_name"],
            email=raw["email"],
            roles=frozenset(raw.get("roles", [])),
            groups=groups,
            wall_side=WallSide(raw["wall_side"]),
            auth_mode="local_mock",
            actor_mode=actor_mode,
            channel_mode=channel_mode,
            revoked=revoked,
            groups_overage_resolved=groups_overage_resolved,
        )


class EntraAuthVerifier:
    """Local Entra OIDC RS256/JWKS JWT verifier with >200-group overage handling and mock fallback."""

    def __init__(
        self,
        *,
        jwks_url: str | None = None,
        public_key_pem: str | bytes | None = None,
        hs256_secret: str | None = None,
        issuer: str | None = None,
        audience: str | None = None,
        group_resolver: MockDirectoryGroupResolver | None = None,
        jwks_cache_ttl: int = 3600,
    ) -> None:
        self._jwks_url = jwks_url
        self._public_key_pem = public_key_pem
        self._hs256_secret = hs256_secret
        self._issuer = issuer
        self._audience = audience
        self._group_resolver = group_resolver
        self._jwk_client = (
            jwt.PyJWKClient(jwks_url, lifespan=jwks_cache_ttl) if jwks_url else None
        )

    def verify_jwt(
        self,
        token: str,
        *,
        policy_epoch: int = 1,
        actor_mode: ActorMode | None = None,
        channel_mode: ChannelMode = "direct",
    ) -> Principal:
        """Validate JWT locally (RS256 via JWKS/PEM or HS256 in non-prod) and resolve >200-group overage."""
        try:
            header = jwt.get_unverified_header(token)
            alg = str(header.get("alg", "RS256"))
            if alg == "RS256":
                if self._public_key_pem is not None:
                    key: Any = self._public_key_pem
                elif self._jwk_client is not None:
                    key = self._jwk_client.get_signing_key_from_jwt(token).key
                else:
                    raise AuthenticationError("RS256 verifier has no JWKS URL or public key configured")
                algorithms = ["RS256"]
            elif alg == "HS256":
                if os.environ.get("ENVIRONMENT", "").lower() == "production":
                    raise AuthenticationError("HS256 test tokens are prohibited in production")
                if not self._hs256_secret:
                    raise AuthenticationError("HS256 secret not configured")
                key = self._hs256_secret
                algorithms = ["HS256"]
            else:
                raise AuthenticationError(f"Unsupported JWT signing algorithm: {alg}")

            decode_kwargs: dict[str, Any] = {
                "algorithms": algorithms,
                "options": {
                    "verify_exp": True,
                    "verify_nbf": True,
                    "verify_aud": self._audience is not None,
                    "verify_iss": self._issuer is not None,
                },
            }
            if self._audience is not None:
                decode_kwargs["audience"] = self._audience
            if self._issuer is not None:
                decode_kwargs["issuer"] = self._issuer

            claims: dict[str, Any] = jwt.decode(token, key, **decode_kwargs)
        except AuthenticationError:
            raise
        except jwt.PyJWTError as exc:
            raise AuthenticationError(f"JWT validation failed: {exc}") from exc

        user_id = str(claims.get("sub") or claims.get("oid") or "").strip()
        if not user_id:
            raise AuthenticationError("JWT missing sub/oid claim")

        # Detect Entra >200-group overage (_claim_names.groups or hasgroups == True)
        claim_names = claims.get("_claim_names") or {}
        has_overage = bool(claims.get("hasgroups")) or (
            isinstance(claim_names, dict) and "groups" in claim_names
        )

        groups_overage_resolved = False
        if has_overage:
            if self._group_resolver is None:
                raise DirectoryUnavailableError(
                    "Entra group overage detected but no GroupResolver is configured (fail-closed)"
                )
            groups = self._group_resolver.resolve_groups(user_id, policy_epoch=policy_epoch)
            groups_overage_resolved = True
        else:
            raw_groups = claims.get("groups") or []
            groups = frozenset(str(g) for g in raw_groups)

        wall_raw = str(claims.get("wall_side") or WallSide.public_side.value)
        try:
            wall_side = WallSide(wall_raw)
        except ValueError as exc:
            raise AuthenticationError(f"Invalid wall_side claim: {wall_raw}") from exc

        resolved_actor: ActorMode = actor_mode or (
            "service" if claims.get("actor_mode") == "service" or claims.get("idtyp") == "app" else "obo"
        )
        roles = frozenset(str(r) for r in (claims.get("roles") or []))

        return Principal(
            user_id=user_id,
            display_name=str(claims.get("name") or user_id),
            email=str(claims.get("preferred_username") or claims.get("email") or f"{user_id}@meridian.local"),
            roles=roles,
            groups=groups,
            wall_side=wall_side,
            auth_mode="entra_jwt",
            actor_mode=resolved_actor,
            channel_mode=channel_mode,
            revoked=False,
            groups_overage_resolved=groups_overage_resolved,
            token_jti=claims.get("jti"),
        )


def make_local_test_jwt(
    principal: Principal,
    *,
    private_key_pem: str | bytes | None = None,
    hs256_secret: str | None = None,
    issuer: str = "https://login.microsoftonline.com/meridian-tenant/v2.0",
    audience: str = "api://enterprise-context-lake",
    groups_overage: bool = False,
    ttl_seconds: int = 3600,
) -> str:
    """Helper for generating signed RS256 or HS256 test JWTs (including Entra overage tokens)."""
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": principal.user_id,
        "oid": principal.user_id,
        "name": principal.display_name,
        "preferred_username": principal.email,
        "roles": sorted(principal.roles),
        "wall_side": principal.wall_side.value,
        "actor_mode": principal.actor_mode,
        "iss": issuer,
        "aud": audience,
        "iat": now,
        "nbf": now - 5,
        "exp": now + ttl_seconds,
        "jti": principal.token_jti or f"jti-{principal.user_id}-{now}",
    }
    if groups_overage:
        payload["hasgroups"] = True
        payload["_claim_names"] = {"groups": "src1"}
        payload["_claim_sources"] = {
            "src1": {"endpoint": f"https://graph.microsoft.com/v1.0/users/{principal.user_id}/getMemberObjects"}
        }
    else:
        payload["groups"] = sorted(principal.groups)

    if private_key_pem is not None:
        return jwt.encode(payload, private_key_pem, algorithm="RS256")
    if hs256_secret is not None:
        return jwt.encode(payload, hs256_secret, algorithm="HS256")
    raise ValueError("Provide either private_key_pem or hs256_secret")
