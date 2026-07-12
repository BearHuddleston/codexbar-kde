from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .privacy import redact_text, sanitize_structure


WINDOW_LABELS = {
    "primary": "5h/session",
    "secondary": "weekly",
    "tertiary": "tertiary",
}

PROVIDER_NAMES = {
    "codex": "Codex",
    "claude": "Claude",
    "openai": "OpenAI API",
    "gemini": "Gemini",
    "copilot": "Copilot",
    "openrouter": "OpenRouter",
    "grok": "Grok",
    "groq": "GroqCloud",
    "bedrock": "AWS Bedrock",
    "vertexai": "Vertex AI",
    "kimi": "Kimi",
    "kimik2": "Kimi K2",
    "zai": "z.ai",
    "minimax": "MiniMax",
    "ollama": "Ollama",
}

_IDENTIFIER_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


def safe_identifier(value: Any, fallback: str = "") -> str:
    """Return a bounded non-sensitive storage/UI identifier or ``fallback``."""
    if not isinstance(value, str):
        return fallback
    text = value.strip()
    if not text or len(text) > 64:
        return fallback
    text = text.lower()
    if not _IDENTIFIER_RE.fullmatch(text) or redact_text(text) != text:
        return fallback
    return text


@dataclass(frozen=True)
class WindowUsage:
    key: str
    label: str
    used_percent: float
    resets_at: str | None = None
    reset_description: str = ""
    reset_countdown: str = ""
    pace_note: str = ""
    window_minutes: int | None = None
    expected_used_percent: float | None = None


@dataclass(frozen=True)
class ProviderUsage:
    provider: str
    display_name: str
    source: str
    version: str
    windows: list[WindowUsage]
    credits_remaining: float | int | None = None
    updated_at: str | None = None
    error: str = ""

    @property
    def tightest_window(self) -> WindowUsage | None:
        return max(self.windows, key=lambda w: w.used_percent, default=None)

    @property
    def max_used_percent(self) -> float:
        window = self.tightest_window
        return window.used_percent if window else 0.0


def provider_display_name(provider: str | None) -> str:
    key = (provider or "").strip().lower()
    if key in PROVIDER_NAMES:
        return PROVIDER_NAMES[key]
    if not key:
        return "Provider"
    return key[:1].upper() + key[1:]


def severity_for_percent(percent: float | int | None) -> str:
    if percent is None:
        return "unknown"
    value = float(percent)
    if value >= 90:
        return "critical"
    if value >= 70:
        return "warn"
    return "ok"


def parse_iso_datetime(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = dt.datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except (ValueError, OverflowError):
        return None


def fleet_tightest(
    providers: Iterable[ProviderUsage],
) -> tuple[ProviderUsage, WindowUsage] | None:
    """The fleet-wide window closest to exhaustion (highest used percent)."""
    tightest: tuple[ProviderUsage, WindowUsage] | None = None
    for provider in providers:
        window = provider.tightest_window
        if window is not None and (
            tightest is None or window.used_percent > tightest[1].used_percent
        ):
            tightest = (provider, window)
    return tightest


def fleet_next_reset(
    providers: Iterable[ProviderUsage], *, now: dt.datetime | None = None
) -> tuple[dt.datetime, ProviderUsage, WindowUsage] | None:
    """The soonest future window reset across the fleet."""
    now = now or dt.datetime.now(dt.timezone.utc)
    soonest: tuple[dt.datetime, ProviderUsage, WindowUsage] | None = None
    for provider in providers:
        for window in provider.windows:
            resets = parse_iso_datetime(window.resets_at)
            if resets is None or resets <= now:
                continue
            if soonest is None or resets < soonest[0]:
                soonest = (resets, provider, window)
    return soonest


def format_reset_countdown(value: str | None, *, now: dt.datetime | None = None) -> str:
    reset = parse_iso_datetime(value)
    if reset is None:
        return ""
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    now = now.astimezone(dt.timezone.utc)
    seconds = max(0, int(round((reset - now).total_seconds())))
    minutes = int(round(seconds / 60))
    if minutes < 1:
        return "now"
    hours, minute = divmod(minutes, 60)
    days, hour = divmod(hours, 24)
    if days:
        return f"{days}d {hour}h"
    if hours:
        return f"{hours}h {minute}m"
    return f"{minute}m"


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            number = float(value)
        elif isinstance(value, str):
            number = float(value.strip().rstrip("%"))
        else:
            return None
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _used_percent(window: dict[str, Any]) -> float | None:
    for key in ("usedPercent", "used_percent", "used", "usagePercent"):
        value = _number(window.get(key))
        if value is not None:
            return max(0.0, min(100.0, value))
    for key in ("percentLeft", "remainingPercent", "remaining_percent", "leftPercent"):
        value = _number(window.get(key))
        if value is not None:
            return max(0.0, min(100.0, 100.0 - value))
    return None


def _window_label(key: str, window: dict[str, Any]) -> str:
    raw = (
        window.get("label")
        or window.get("name")
        or window.get("window")
        or WINDOW_LABELS.get(key, key)
    )
    return str(raw).strip() or WINDOW_LABELS.get(key, key)


def _reset_value(window: dict[str, Any]) -> str | None:
    for key in ("resetsAt", "resetAt", "resetTime", "reset_at", "resets_at"):
        value = window.get(key)
        if value:
            return str(value)
    return None


def _reset_description(window: dict[str, Any]) -> str:
    for key in ("resetDescription", "reset_description"):
        value = window.get(key)
        if value:
            return str(value).strip()
    return ""


MAX_WINDOW_MINUTES = 525_600


def _window_minutes(window: dict[str, Any]) -> int | None:
    value = _number(window.get("windowMinutes"))
    if value is None or value <= 0 or value > MAX_WINDOW_MINUTES:
        return None
    return int(value)


def _error_message(entry: dict[str, Any]) -> str:
    error = entry.get("error")
    if isinstance(error, dict):
        msg = error.get("message") or error.get("detail") or error.get("kind")
        return redact_text(str(msg or "").strip())
    if error:
        return redact_text(str(error).strip())
    return ""


def _pace_note(pace_window: Any) -> str:
    if not isinstance(pace_window, dict):
        return ""
    delta = _number(pace_window.get("deltaPercent"))
    expected = _number(pace_window.get("expectedUsedPercent"))
    if delta is None and expected is None:
        return ""
    bits: list[str] = []
    if delta is not None:
        if delta < 0:
            bits.append(f"{abs(delta):g}% reserve")
        elif delta > 0:
            bits.append(f"{delta:g}% over pace")
        else:
            bits.append("on pace")
    if expected is not None:
        bits.append(f"expected {expected:g}%")
    will_last = pace_window.get("willLastToReset")
    if will_last is True:
        bits.append("lasts to reset")
    elif will_last is False:
        bits.append("may run out before reset")
    return " · ".join(bits)


def _pace_expected(pace_window: Any) -> float | None:
    if not isinstance(pace_window, dict):
        return None
    expected = _number(pace_window.get("expectedUsedPercent"))
    if expected is None:
        return None
    return max(0.0, min(100.0, expected))


def normalize_payload(
    payload: Any, *, now: dt.datetime | None = None
) -> list[ProviderUsage]:
    entries = payload if isinstance(payload, list) else [payload]
    providers: list[ProviderUsage] = []
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            continue
        provider = safe_identifier(raw_entry.get("provider"), "provider")
        raw_usage = raw_entry.get("usage")
        usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
        raw_pace = raw_entry.get("pace")
        pace = raw_pace if isinstance(raw_pace, dict) else {}
        windows: list[WindowUsage] = []

        for key in ("primary", "secondary", "tertiary"):
            window = usage.get(key)
            if not isinstance(window, dict):
                continue
            used = _used_percent(window)
            if used is None:
                continue
            reset = _reset_value(window)
            windows.append(
                WindowUsage(
                    key=key,
                    label=_window_label(key, window),
                    used_percent=round(used, 1),
                    resets_at=reset,
                    reset_description=_reset_description(window),
                    reset_countdown=format_reset_countdown(reset, now=now),
                    pace_note=_pace_note(pace.get(key)),
                    window_minutes=_window_minutes(window),
                    expected_used_percent=_pace_expected(pace.get(key)),
                )
            )

        extra_windows = usage.get("extraRateWindows")
        if isinstance(extra_windows, list):
            for index, extra in enumerate(extra_windows, start=1):
                if not isinstance(extra, dict):
                    continue
                nested = extra.get("window")
                window = nested if isinstance(nested, dict) else extra
                used = _used_percent(window)
                if used is None:
                    continue
                reset = _reset_value(window)
                key = safe_identifier(
                    extra.get("key") or extra.get("id"), f"extra{index}"
                )
                label = str(extra.get("title") or "").strip() or _window_label(
                    key, window
                )
                windows.append(
                    WindowUsage(
                        key=key,
                        label=label,
                        used_percent=round(used, 1),
                        resets_at=reset,
                        reset_description=_reset_description(window),
                        reset_countdown=format_reset_countdown(reset, now=now),
                        window_minutes=_window_minutes(window),
                    )
                )

        credits = raw_entry.get("credits")
        credits_remaining = None
        if isinstance(credits, dict):
            credits_remaining = _number(credits.get("remaining"))
            if credits_remaining is not None and credits_remaining.is_integer():
                credits_remaining = int(credits_remaining)

        providers.append(
            ProviderUsage(
                provider=provider,
                display_name=provider_display_name(provider),
                source=str(raw_entry.get("source") or ""),
                version=str(raw_entry.get("version") or ""),
                windows=windows,
                credits_remaining=credits_remaining,
                updated_at=str(usage.get("updatedAt") or "") or None,
                error=_error_message(raw_entry),
            )
        )
    return providers


def sanitize_for_debug(value: Any) -> Any:
    """Return a copy safe for logs; not used for normal UI rendering."""
    return sanitize_structure(value)
