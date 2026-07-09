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
import http.client
import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

from .privacy import redact_text, sanitize_structure

DEFAULT_BASE = "https://chatgpt.com/backend-api"
USER_AGENT = "codexbar-kde/1.0 (+local KDE dashboard)"
REQUEST_TIMEOUT = 30.0
MAX_RESPONSE_BYTES = 1024 * 1024


class CodexAuthError(RuntimeError):
    """auth.json missing or unusable — user needs `codex login`."""


class CodexResetError(RuntimeError):
    """The reset-credit API refused or failed the request."""


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_RejectRedirects())


def _open_request(req: urllib.request.Request, *, timeout: float):
    return _NO_REDIRECT_OPENER.open(req, timeout=timeout)


def _read_response(stream: Any) -> str:
    raw = stream.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise CodexResetError("reset-credit API response exceeded 1 MiB")
    return raw.decode(errors="replace")


def default_auth_path() -> Path:
    home = os.environ.get("CODEX_HOME")
    base = Path(home) if home else Path.home() / ".codex"
    return base / "auth.json"


def _first_nonempty_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def load_codex_auth(path: Path | None = None) -> tuple[str, str]:
    path = path or default_auth_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise CodexAuthError(
            f"Codex auth file not found: {path} — run `codex login` first"
        ) from None
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexAuthError(f"Codex auth file unreadable: {path}: {exc}") from None
    if not isinstance(data, dict):
        raise CodexAuthError(f"Codex auth file must contain a JSON object: {path}")
    tokens = data.get("tokens")
    if not isinstance(tokens, dict):
        tokens = {}
    token = _first_nonempty_string(data.get("access_token"), tokens.get("access_token"))
    account_id = _first_nonempty_string(
        data.get("account_id"), tokens.get("account_id")
    )
    if token is None or account_id is None:
        raise CodexAuthError(
            f"Codex auth file is missing access_token/account_id: {path}"
        )
    return token, account_id


def _request(
    method: str,
    url: str,
    *,
    token: str,
    account_id: str,
    body: dict[str, Any] | None = None,
) -> tuple[int, Any]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("ChatGPT-Account-Id", account_id)
    req.add_header("User-Agent", USER_AGENT)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    safe_url = redact_text(url)
    try:
        with _open_request(req, timeout=REQUEST_TIMEOUT) as response:
            raw = _read_response(response)
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            exc.close()
            raise CodexResetError(
                f"redirect rejected while talking to {safe_url} (HTTP {exc.code})"
            ) from None
        try:
            try:
                raw = _read_response(exc)
            except (OSError, http.client.HTTPException) as read_exc:
                detail = redact_text(str(read_exc))
                raise CodexResetError(
                    f"network error talking to {safe_url}: {detail}"
                ) from None
        finally:
            exc.close()
        status = exc.code
    except urllib.error.URLError as exc:
        detail = redact_text(str(exc.reason))
        raise CodexResetError(
            f"network error talking to {safe_url}: {detail}"
        ) from None
    except (OSError, http.client.HTTPException) as exc:
        detail = redact_text(str(exc))
        raise CodexResetError(
            f"network error talking to {safe_url}: {detail}"
        ) from None
    try:
        return status, json.loads(raw)
    except json.JSONDecodeError:
        return status, raw


def list_reset_credits(
    token: str, account_id: str, *, base: str = DEFAULT_BASE
) -> dict[str, Any]:
    """Return {"credits": [...], "available_count": N} from the backend."""
    status, payload = _request(
        "GET",
        f"{base}/wham/rate-limit-reset-credits",
        token=token,
        account_id=account_id,
    )
    if status != 200 or not isinstance(payload, dict):
        raise CodexResetError(f"listing reset credits failed (HTTP {status})")
    return payload


def consume_reset_credit(
    token: str, account_id: str, credit_id: str, *, base: str = DEFAULT_BASE
) -> dict[str, Any]:
    """Redeem one banked credit. Returns the consume response
    ({"code": "reset", "windows_reset": 1, "credit": {...}})."""
    status, payload = _request(
        "POST",
        f"{base}/wham/rate-limit-reset-credits/consume",
        token=token,
        account_id=account_id,
        body={"credit_id": credit_id, "redeem_request_id": str(uuid.uuid4())},
    )
    if status != 200 or not isinstance(payload, dict):
        if isinstance(payload, (dict, list)):
            detail = json.dumps(sanitize_structure(payload))[:300]
        else:
            detail = redact_text(str(payload))[:300]
        raise CodexResetError(
            f"consuming reset credit failed (HTTP {status}): {detail}"
        )
    response_credit = payload.get("credit")
    windows_reset = payload.get("windows_reset")
    valid_windows = (
        isinstance(windows_reset, int)
        and not isinstance(windows_reset, bool)
        and windows_reset > 0
    )
    if (
        payload.get("code") != "reset"
        or not valid_windows
        or not isinstance(response_credit, dict)
        or response_credit.get("id") != credit_id
        or response_credit.get("status") != "redeemed"
    ):
        raise CodexResetError("invalid consume response: redemption was not confirmed")
    return payload


def _parse_expiry(credit: dict[str, Any]) -> dt.datetime | None:
    expires = str(credit.get("expires_at") or "").strip()
    if not expires:
        return None
    try:
        parsed = dt.datetime.fromisoformat(expires.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except (ValueError, OverflowError):
        return None


def _expiry_key(credit: dict[str, Any]) -> tuple[int, dt.datetime]:
    parsed = _parse_expiry(credit)
    # credits without an expiry sort after all dated ones
    return (
        (1, dt.datetime.max.replace(tzinfo=dt.timezone.utc))
        if parsed is None
        else (0, parsed)
    )


def available_reset_credits(
    credits: list[dict[str, Any]] | None,
    *,
    now: dt.datetime | None = None,
) -> list[dict[str, Any]]:
    """Return credits that are available, unexpired, and have valid expiries."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    now = now.astimezone(dt.timezone.utc)
    available = []
    for credit in credits or []:
        if not isinstance(credit, dict) or credit.get("status") != "available":
            continue
        expiry = _parse_expiry(credit)
        if str(credit.get("expires_at") or "").strip() and expiry is None:
            continue
        if expiry is not None and expiry <= now:
            continue
        available.append(credit)
    return available


def pick_next_expiring(
    credits: list[dict[str, Any]] | None,
    *,
    now: dt.datetime | None = None,
) -> dict[str, Any] | None:
    """The available credit closest to expiry — the one worth spending first."""
    available = available_reset_credits(credits, now=now)
    if not available:
        return None
    return min(available, key=_expiry_key)


def redeem_reset_credit(
    token: str,
    account_id: str,
    credit_id: str,
    *,
    base: str = DEFAULT_BASE,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Revalidate an available credit immediately before consuming it."""
    listing = list_reset_credits(token, account_id, base=base)
    credits = listing.get("credits")
    if not isinstance(credits, list):
        raise CodexResetError("reset-credit listing did not contain a credit list")
    fresh_target = pick_next_expiring(credits, now=now)
    if fresh_target is None:
        raise CodexResetError("selected reset credit is no longer available")
    if fresh_target.get("id") != credit_id:
        raise CodexResetError(
            "available credits changed; review and confirm the new soonest-expiring credit"
        )
    try:
        return consume_reset_credit(token, account_id, credit_id, base=base)
    except CodexResetError as consume_error:
        try:
            reconciled = list_reset_credits(token, account_id, base=base)
        except CodexResetError:
            raise CodexResetError(
                f"{consume_error}; redemption state could not be reconciled"
            ) from consume_error
        reconciled_credits = reconciled.get("credits")
        if not isinstance(reconciled_credits, list):
            raise CodexResetError(
                f"{consume_error}; redemption state could not be reconciled"
            ) from consume_error
        matched = next(
            (
                credit
                for credit in reconciled_credits
                if isinstance(credit, dict) and credit.get("id") == credit_id
            ),
            None,
        )
        if matched is not None and matched.get("status") == "redeemed":
            return {
                "code": "reset",
                "windows_reset": None,
                "credit": matched,
                "reconciled": True,
            }
        if matched is not None and pick_next_expiring([matched], now=now) is not None:
            raise consume_error
        raise CodexResetError(
            f"{consume_error}; redemption outcome uncertain; refresh before retrying"
        ) from consume_error


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
        if isinstance(reset_credits, dict) and isinstance(
            reset_credits.get("credits"), list
        ):
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
