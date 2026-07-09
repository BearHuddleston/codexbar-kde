"""Privacy helpers for data that may cross into logs or UI text."""

from __future__ import annotations

import re
from typing import Any

_REDACTED = "[REDACTED]"
_AUTH_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>\bauthorization[\"']?\s*[:=]\s*[\"']?)"
    r"(?P<value>[^\"'\r\n,;}]+)",
    re.IGNORECASE,
)
_AUTH_HEADER_RE = re.compile(
    r"(?P<prefix>\b(?:(?:authorization\s*:\s*)(?:basic|bearer)|bearer)\s+)"
    r"(?P<value>[^\s,;]+)",
    re.IGNORECASE,
)
_QUOTED_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>\b(?:access[_-]?token|refresh[_-]?token|account[_-]?id|"
    r"token|secret|password|cookie|api[_-]?key|authorization)"
    r"[\"']?\s*[=:]\s*)(?P<quote>[\"'])"
    r"(?P<value>(?:\\.|(?!(?P=quote)).)*)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)
_ASSIGNMENT_RE = re.compile(
    r"(?P<prefix>\b(?:access[_-]?token|refresh[_-]?token|account[_-]?id|"
    r"token|secret|password|cookie|api[_-]?key|authorization)"
    r"[\"']?\s*[=:]\s*[\"']?)"
    r"(?P<value>(?!\[REDACTED\])[^\s,;}\]\"']+)",
    re.IGNORECASE,
)
_CREDENTIAL_URL_RE = re.compile(
    r"(?P<scheme>https?://)(?P<user>[^/@\s:]+):(?P<password>[^/@\s]+)@",
    re.IGNORECASE,
)
_JWT_RE = re.compile(
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)
_PROVIDER_CREDENTIAL_RE = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"sk-[A-Za-z0-9_-]{16,}|"
    r"xai-[A-Za-z0-9_-]{16,}|"
    r"AIza[A-Za-z0-9_-]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}"
    r")(?![A-Za-z0-9_-])"
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_SENSITIVE_KEYS = {
    "account",
    "bearer",
    "credentials",
    "identity",
    "org",
    "organization",
    "user",
}
_SENSITIVE_KEY_PARTS = (
    "token",
    "secret",
    "password",
    "passwd",
    "cookie",
    "api_key",
    "apikey",
    "authorization",
    "account_id",
    "accountid",
    "account_email",
    "accountemail",
    "email",
    "credential",
)


def redact_text(text: str) -> str:
    """Redact common credential and identity forms from untrusted text."""
    if not text:
        return ""
    result = _CREDENTIAL_URL_RE.sub(
        lambda match: f"{match.group('scheme')}{match.group('user')}:{_REDACTED}@",
        str(text),
    )
    result = _QUOTED_ASSIGNMENT_RE.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}"
            f"{_REDACTED}{match.group('quote')}"
        ),
        result,
    )
    result = _AUTH_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('prefix')}{_REDACTED}", result
    )
    result = _AUTH_HEADER_RE.sub(
        lambda match: f"{match.group('prefix')}{_REDACTED}", result
    )
    result = _ASSIGNMENT_RE.sub(
        lambda match: f"{match.group('prefix')}{_REDACTED}", result
    )
    result = _PROVIDER_CREDENTIAL_RE.sub(_REDACTED, result)
    result = _JWT_RE.sub(_REDACTED, result)
    return _EMAIL_RE.sub(_REDACTED, result)


def sanitize_structure(value: Any) -> Any:
    """Return a recursively redacted copy of JSON-like data."""
    if isinstance(value, dict):
        safe: dict[Any, Any] = {}
        for key, item in value.items():
            normalized = str(key).replace("-", "_").lower()
            if normalized in _SENSITIVE_KEYS or any(
                part in normalized for part in _SENSITIVE_KEY_PARTS
            ):
                safe[key] = _REDACTED
            else:
                safe[key] = sanitize_structure(item)
        return safe
    if isinstance(value, list):
        return [sanitize_structure(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize_structure(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value
