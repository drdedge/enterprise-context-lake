"""Domain and security exception hierarchy for Enterprise Context Lake."""

from __future__ import annotations


class ContextLakeError(Exception):
    """Base exception carrying a structured API error code and HTTP status."""

    code: str = "internal_error"
    status_code: int = 500

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code

    def to_error_dict(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code, "message": self.message}}


class AuthenticationError(ContextLakeError):
    """Raised on invalid JWT, missing credentials, or revoked principal (HTTP 401)."""

    code = "unauthorized"
    status_code = 401


class AuthorizationError(PermissionError, ContextLakeError):
    """Raised when an authenticated principal lacks write/steward privileges (HTTP 403).

    Inherits from built-in ``PermissionError`` per ADR-002 contract.
    """

    code = "forbidden"
    status_code = 403

    def __init__(self, message: str, *, code: str | None = None, status_code: int | None = None) -> None:
        PermissionError.__init__(self, message)
        self.message = message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code


class SurfacePolicyError(AuthorizationError):
    """Raised when personal memory or OBO-only operations are invoked on a shared/service surface."""

    code = "surface_policy_blocked"
    status_code = 403


class NotFoundError(ContextLakeError):
    """Raised for missing OR unauthorized read targets (HTTP 404, zero existence leakage)."""

    code = "not_found"
    status_code = 404


class OptimisticConcurrencyError(ContextLakeError):
    """Raised on TOCTOU stale data_epoch / If-Match conflict during steward approval (HTTP 409)."""

    code = "conflict"
    status_code = 409


class ValidationError(ContextLakeError):
    """Raised on semantic domain validation violations (HTTP 422)."""

    code = "validation_error"
    status_code = 422


class DirectoryUnavailableError(ContextLakeError):
    """Raised fail-closed when Entra >200-group overage directory resolution fails (HTTP 503)."""

    code = "service_unavailable"
    status_code = 503
