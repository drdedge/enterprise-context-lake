"""Authentication dependency resolving Entra JWT/JWKS, >200-group overage, mock headers, and revocation."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Literal

from context_core.exceptions import AuthenticationError
from context_core.models import ActorMode, ChannelMode, Principal
from context_core.policy import EntraAuthVerifier, PrincipalDirectory
from context_core.ports import StorePort
from context_core.tracing import mark_trace_principal


def _ci_header(headers: Mapping[str, str], *names: str) -> str | None:
    lower_map = {k.lower(): v for k, v in headers.items()}
    for name in names:
        val = lower_map.get(name.lower())
        if val is not None and val.strip():
            return val.strip()
    return None


def parse_if_match_epoch(headers: Mapping[str, str]) -> int | None:
    """Parse optional HTTP `If-Match` header into an integer `expected_data_epoch`."""
    raw = _ci_header(headers, "if-match")
    if not raw:
        return None
    cleaned = raw.strip()
    if cleaned.startswith("W/"):
        cleaned = cleaned[2:].strip()
    cleaned = cleaned.strip('"').strip("'")
    if cleaned.startswith("epoch:"):
        cleaned = cleaned[len("epoch:") :]
    if cleaned.isdigit():
        return int(cleaned)
    return None


class AuthService:
    """Unified fail-closed principal resolver supporting Entra RS256/JWKS JWT + deterministic local mock."""

    def __init__(
        self,
        *,
        directory: PrincipalDirectory,
        jwt_verifier: EntraAuthVerifier | None = None,
        auth_mode: Literal["mock", "entra_jwt"] | None = None,
        default_user_id: str | None = "user:fund-steward",
        default_actor_mode: ActorMode = "obo",
        default_channel_mode: ChannelMode = "direct",
    ) -> None:
        self.directory = directory
        self.jwt_verifier = jwt_verifier or EntraAuthVerifier(
            jwks_url=os.environ.get("ENTRA_JWKS_URL"),
            hs256_secret=os.environ.get("LOCAL_JWT_HS256_SECRET", "meridian-local-dev-jwt-secret"),
            issuer=os.environ.get("ENTRA_ISSUER"),
            audience=os.environ.get("ENTRA_AUDIENCE"),
            group_resolver=directory.group_resolver,
        )
        env_mode = os.environ.get("AUTH_MODE", "mock").strip().lower()
        self.auth_mode: Literal["mock", "entra_jwt"] = auth_mode or (
            "entra_jwt" if env_mode == "entra_jwt" else "mock"
        )
        self.default_user_id = default_user_id
        self.default_actor_mode: ActorMode = default_actor_mode
        self.default_channel_mode: ChannelMode = default_channel_mode

    async def resolve_principal(
        self,
        headers: Mapping[str, str],
        *,
        store: StorePort,
    ) -> Principal:
        """Resolve, verify, and revocation-check the calling Principal from HTTP/MCP headers."""
        epochs = await store.get_epochs()
        actor_raw = _ci_header(headers, "x-mock-actor-mode", "x-actor-mode")
        channel_raw = _ci_header(headers, "x-mock-channel-mode", "x-channel-mode")
        overage_raw = _ci_header(headers, "x-mock-groups-overage", "x-groups-overage")

        actor_mode: ActorMode = (
            "service"
            if (actor_raw or self.default_actor_mode).lower() == "service"
            else "obo"
        )
        channel_mode: ChannelMode = (
            "shared"
            if (channel_raw or self.default_channel_mode).lower() == "shared"
            else "direct"
        )
        force_overage = (overage_raw or "").lower() in ("true", "1", "yes")

        auth_header = _ci_header(headers, "authorization")
        principal: Principal | None = None

        if auth_header:
            if not auth_header.lower().startswith("bearer "):
                raise AuthenticationError("Invalid Authorization header; expected Bearer token")
            token = auth_header[7:].strip()
            if not token:
                raise AuthenticationError("Empty Bearer token")

            if token.startswith("mock:"):
                if os.environ.get("ENVIRONMENT", "").lower() == "production" or self.auth_mode == "entra_jwt":
                    raise AuthenticationError("Mock bearer tokens are disabled in Entra JWT / production mode")
                mock_uid = token[5:].strip()
                principal = self.directory.resolve_mock_principal(
                    mock_uid,
                    actor_mode=actor_mode,
                    channel_mode=channel_mode,
                    force_overage=force_overage,
                    policy_epoch=epochs.policy_epoch,
                )
            else:
                principal = self.jwt_verifier.verify_jwt(
                    token,
                    policy_epoch=epochs.policy_epoch,
                    actor_mode=actor_mode if actor_raw else None,
                    channel_mode=channel_mode,
                )
        else:
            if os.environ.get("ENVIRONMENT", "").lower() == "production" or self.auth_mode == "entra_jwt":
                raise AuthenticationError("Missing Bearer JWT in Entra authentication mode")
            mock_user = _ci_header(headers, "x-mock-user", "x-principal-id") or self.default_user_id
            if not mock_user:
                raise AuthenticationError("Missing X-Mock-User / X-Principal-Id or Authorization header")
            principal = self.directory.resolve_mock_principal(
                mock_user,
                actor_mode=actor_mode,
                channel_mode=channel_mode,
                force_overage=force_overage,
                policy_epoch=epochs.policy_epoch,
            )

        # Enforce immediate revocation against both fixture flag and live store revocation table
        if principal.revoked or await store.is_principal_revoked(principal.user_id):
            raise AuthenticationError("Principal account has been revoked")

        mark_trace_principal(principal)
        return principal
