"""Codex banked rate-limit reset credits: list, pick, and redeem.

Talks to the same undocumented ChatGPT backend endpoints the official
Codex desktop/VS Code extension uses, authenticating with the OAuth token
`codex login` already stored in ~/.codex/auth.json (or $CODEX_HOME).

Endpoints (base: https://chatgpt.com/backend-api):
  GET  /wham/rate-limit-reset-credits          — list banked credits
  POST /wham/rate-limit-reset-credits/consume  — redeem one credit

This does not bypass any limit — it only spends a credit OpenAI granted
to the account. Endpoint shape confirmed by openai/codex PR #28143 and
the aaamosh/codex-reset CLI (MIT).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

DEFAULT_BASE = "https://chatgpt.com/backend-api"
USER_AGENT = "codexbar-kde/1.0 (+local KDE dashboard)"
REQUEST_TIMEOUT = 30.0


class CodexAuthError(RuntimeError):
    """auth.json missing or unusable — user needs `codex login`."""


class CodexResetError(RuntimeError):
    """The reset-credit API refused or failed the request."""


def default_auth_path() -> Path:
    home = os.environ.get("CODEX_HOME")
    base = Path(home) if home else Path.home() / ".codex"
    return base / "auth.json"


def load_codex_auth(path: Path | None = None) -> tuple[str, str]:
    path = path or default_auth_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise CodexAuthError(f"Codex auth file not found: {path} — run `codex login` first") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexAuthError(f"Codex auth file unreadable: {path}: {exc}") from None
    tokens = data.get("tokens") or {}
    token = data.get("access_token") or tokens.get("access_token")
    account_id = data.get("account_id") or tokens.get("account_id")
    if not token or not account_id:
        raise CodexAuthError(f"Codex auth file is missing access_token/account_id: {path}")
    return str(token), str(account_id)


def _request(method: str, url: str, *, token: str, account_id: str,
             body: dict[str, Any] | None = None) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("ChatGPT-Account-Id", account_id)
    req.add_header("User-Agent", USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as response:
            raw = response.read().decode()
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode(errors="replace")
        status = exc.code
    except urllib.error.URLError as exc:
        raise CodexResetError(f"network error talking to {url}: {exc.reason}") from None
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def list_reset_credits(token: str, account_id: str, *, base: str = DEFAULT_BASE) -> dict[str, Any]:
    """Return {"credits": [...], "available_count": N} from the backend."""
    status, payload = _request("GET", f"{base}/wham/rate-limit-reset-credits",
                               token=token, account_id=account_id)
    if status != 200 or not isinstance(payload, dict):
        raise CodexResetError(f"listing reset credits failed (HTTP {status})")
    return payload


def consume_reset_credit(token: str, account_id: str, credit_id: str, *,
                         base: str = DEFAULT_BASE) -> dict[str, Any]:
    """Redeem one banked credit. Returns the consume response
    ({"code": "reset", "windows_reset": 1, "credit": {...}})."""
    status, payload = _request(
        "POST", f"{base}/wham/rate-limit-reset-credits/consume",
        token=token, account_id=account_id,
        body={"credit_id": credit_id, "redeem_request_id": str(uuid.uuid4())},
    )
    if status != 200 or not isinstance(payload, dict):
        detail = json.dumps(payload)[:300] if isinstance(payload, dict) else str(payload)[:300]
        raise CodexResetError(f"consuming reset credit failed (HTTP {status}): {detail}")
    return payload


def _expiry_key(credit: dict[str, Any]) -> tuple[int, str]:
    expires = str(credit.get("expires_at") or "").strip()
    # credits without an expiry sort after all dated ones
    return (1, "") if not expires else (0, expires)


def pick_next_expiring(credits: list[dict[str, Any]] | None) -> dict[str, Any] | None:
    """The available credit closest to expiry — the one worth spending first."""
    available = [c for c in (credits or [])
                 if isinstance(c, dict) and c.get("status") == "available"]
    if not available:
        return None
    return sorted(available, key=_expiry_key)[0]


def credits_from_usage_payload(raw_payload: object) -> list[dict[str, Any]]:
    """Extract the codexResetCredits list from a codexbar usage payload."""
    entries = raw_payload if isinstance(raw_payload, list) else [raw_payload]
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("provider") != "codex":
            continue
        usage = entry.get("usage")
        if not isinstance(usage, dict):
            continue
        reset_credits = usage.get("codexResetCredits")
        if isinstance(reset_credits, dict) and isinstance(reset_credits.get("credits"), list):
            return [c for c in reset_credits["credits"] if isinstance(c, dict)]
    return []


def format_expiry(credit: dict[str, Any], *, now: dt.datetime | None = None) -> str:
    """Human note like 'expires in 7d' / 'expires in 3h' for button labels."""
    expires = str(credit.get("expires_at") or "").strip()
    if not expires:
        return ""
    try:
        parsed = dt.datetime.fromisoformat(expires.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    now = now or dt.datetime.now(dt.timezone.utc)
    seconds = int((parsed - now).total_seconds())
    if seconds <= 0:
        return "expired"
    days, rem = divmod(seconds, 86400)
    hours = rem // 3600
    if days > 0:
        return f"expires in {days}d"
    if hours > 0:
        return f"expires in {hours}h"
    return f"expires in {max(1, rem // 60)}m"
