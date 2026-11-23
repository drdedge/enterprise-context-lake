"""Security helpers for MCP & Code Mode: anti-SSRF URI validation, query-secret stripping, and output caps."""

from __future__ import annotations

import ipaddress
import json
import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from context_core.compression import canonical_json, to_posix_path
from context_core.exceptions import ValidationError

MAX_CODEMODE_OUTPUT_BYTES = 65_536

SENSITIVE_QUERY_KEYS = frozenset(
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
        "client_secret",
        "password",
        "passwd",
        "pwd",
        "auth",
        "authorization",
        "sv",
        "se",
        "sp",
        "sr",
        "st",
        "spr",
        "skoid",
        "sktid",
        "skt",
        "ske",
        "sks",
        "skv",
    }
)

SENSITIVE_QUERY_PREFIXES = ("x-amz-", "x-goog-", "amz-")

BLOCKED_SCHEMES = frozenset(
    {
        "file",
        "gopher",
        "ftp",
        "ftps",
        "dict",
        "ldap",
        "ldaps",
        "telnet",
        "ssh",
        "javascript",
        "data",
        "vbscript",
    }
)

ALLOWED_REMOTE_SCHEMES = frozenset({"https", "http", "s3", "context"})

BLOCKED_HOSTNAMES = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "169.254.169.254",
        "0.0.0.0",
        "::1",
        "::",
    }
)

_WINDOWS_DRIVE_RE = re.compile(r"^[a-zA-Z]:[\\/]")


def _is_sensitive_param(key: str) -> bool:
    low = key.strip().lower()
    if low in SENSITIVE_QUERY_KEYS:
        return True
    return any(low.startswith(prefix) for prefix in SENSITIVE_QUERY_PREFIXES)


def sanitize_uri_secrets(uri: str | None) -> str | None:
    """Strip sensitive query-string parameters from any URI/URL while preserving safe paths."""
    if uri is None:
        return None
    raw = uri.strip()
    if not raw:
        return raw

    # Normalize backslashes only for relative/non-URL paths
    if "://" not in raw and "?" not in raw:
        return to_posix_path(raw)

    parsed = urlparse(raw)
    if not parsed.query:
        return to_posix_path(raw) if not parsed.scheme else raw

    filtered_pairs = [
        (k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not _is_sensitive_param(k)
    ]
    new_query = urlencode(filtered_pairs, doseq=True)
    rebuilt = urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment)
    )
    return to_posix_path(rebuilt) if not parsed.scheme else rebuilt


def _is_blocked_ip(host: str) -> bool:
    cleaned = host.strip("[]").lower()
    if cleaned in BLOCKED_HOSTNAMES or cleaned.endswith(".local") or cleaned.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(cleaned)
    except ValueError:
        # Check integer/hex/octal encoded IPv4 tricks (e.g., 2130706433 or 0x7f000001)
        if re.fullmatch(r"(?:0x[0-9a-f]+|\d+)", cleaned):
            try:
                num = int(cleaned, 0)
                if 0 <= num <= 0xFFFFFFFF:
                    ip = ipaddress.IPv4Address(num)
                else:
                    return True
            except ValueError:
                return True
        else:
            return False

    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_safe_uri(uri: str | None, *, field_name: str = "evidence_uri") -> str | None:
    """Validate URI against SSRF / local-file / private-IP attacks and strip query secrets."""
    if uri is None:
        return None
    raw = uri.strip()
    if not raw:
        return None

    # Reject Windows drive letters or UNC paths (e.g. C:\Windows\... or \\server\share)
    if _WINDOWS_DRIVE_RE.match(raw) or raw.startswith("\\\\") or raw.startswith("//"):
        raise ValidationError(
            f"Blocked local/UNC URI in {field_name}",
            code="ssrf_blocked",
            status_code=400,
        )

    parsed = urlparse(raw)
    scheme = parsed.scheme.lower()
    if scheme:
        if scheme in BLOCKED_SCHEMES or scheme not in ALLOWED_REMOTE_SCHEMES:
            raise ValidationError(
                f"Blocked URI scheme '{scheme}' in {field_name}",
                code="ssrf_blocked",
                status_code=400,
            )
        if scheme in ("http", "https"):
            hostname = parsed.hostname or ""
            if not hostname or _is_blocked_ip(hostname):
                raise ValidationError(
                    f"Blocked internal/loopback/metadata host in {field_name}",
                    code="ssrf_blocked",
                    status_code=400,
                )
    else:
        # Relative path — forbid traversal or absolute root paths
        posix = to_posix_path(raw)
        if posix.startswith("/") or ".." in posix.split("/"):
            raise ValidationError(
                f"Blocked path traversal in {field_name}",
                code="ssrf_blocked",
                status_code=400,
            )

    return sanitize_uri_secrets(raw)


def sanitize_error_message(message: str) -> str:
    """Scrub query secrets from URLs embedded inside error strings or logs."""

    def _repl(match: re.Match[str]) -> str:
        return sanitize_uri_secrets(match.group(0)) or ""

    return re.sub(r"https?://[^\s'\"<>]+", _repl, message)


def truncate_output_payload(
    value: Any,
    logs: list[str],
    *,
    max_bytes: int = MAX_CODEMODE_OUTPUT_BYTES,
) -> tuple[Any, list[str], dict[str, Any]]:
    """Enforce deterministic <=64 KiB UTF-8 output truncation with structured metadata."""
    combined = {"value": value, "logs": logs}
    encoded = canonical_json(combined).encode("utf-8")
    original_bytes = len(encoded)
    if original_bytes <= max_bytes:
        return (
            value,
            logs,
            {
                "truncated": False,
                "original_bytes": original_bytes,
                "max_bytes": max_bytes,
            },
        )

    # Reserve headroom for JSON wrapper and logs summary so final serialized result <= max_bytes.
    budget = max(1024, max_bytes - 1024)
    raw_value_json = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    value_bytes = raw_value_json.encode("utf-8")
    if len(value_bytes) > budget:
        clipped_preview = value_bytes[:budget].decode("utf-8", errors="ignore")
        truncated_value: Any = {
            "_truncated": True,
            "preview": clipped_preview,
            "original_value_bytes": len(value_bytes),
        }
    else:
        truncated_value = value

    # Cap logs to remaining budget
    remaining_logs: list[str] = []
    used = len(json.dumps(truncated_value, ensure_ascii=False, default=str).encode("utf-8"))
    for entry in logs:
        eb = entry.encode("utf-8")
        if used + len(eb) + 4 > budget:
            break
        remaining_logs.append(entry)
        used += len(eb) + 4

    return (
        truncated_value,
        remaining_logs,
        {
            "truncated": True,
            "original_bytes": original_bytes,
            "max_bytes": max_bytes,
        },
    )
