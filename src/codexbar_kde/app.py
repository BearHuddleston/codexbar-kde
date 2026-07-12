from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Iterable

from PyQt6.QtCore import QObject, QSettings, QSize, Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QStackedWidget,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

from .history import HistoryStore, Sample, burn_down_series, daily_peaks
from .model import (
    ProviderUsage,
    WindowUsage as WindowUsage,
    fleet_next_reset,
    fleet_tightest,
    normalize_payload,
    parse_iso_datetime,
    provider_display_name,
    severity_for_percent as severity_for_percent,
)
from .privacy import redact_credentials, redact_text
from .reset import (
    CodexAuthError,
    CodexResetError,
    available_reset_credits,
    credits_from_usage_payload,
    load_codex_auth,
    redeem_reset_credit,
)
from .views import (
    BG,
    GOOD,
    HAIRLINE,
    MUTED,
    SOFT_CRIT,
    SURFACE,
    TEAL,
    TEXT,
    WARN,
    BurnDownView,
    DetailsView,
    HistoryView,
    OverviewView,
    color_for_percent as color_for_percent,
    latest_reset_at,
    provider_accent_color,
    window_options,
)

DEFAULT_CODEXBAR = "/usr/bin/codexbar"
DEFAULT_REFRESH_SECONDS = 120
QT_MAX_TIMER_INTERVAL_MS = 2_147_483_647
MAX_REFRESH_SECONDS = QT_MAX_TIMER_INTERVAL_MS // 1000


def clamp_refresh_seconds(value: int) -> int:
    return max(30, min(MAX_REFRESH_SECONDS, value))


# Nerd Font / Font Awesome glyphs. These stay plain text, but render as icons
# when a Nerd Font is installed and selected/fallbacked by KDE's tooltip font.
NF_DASHBOARD = "\uf0e4"  # fa-tachometer
NF_OK = "\uf058"  # fa-check-circle
NF_WARN = "\uf071"  # fa-exclamation-triangle
# Severity dots for the tray tooltip. Plasma renders SNI tooltips as plain
# text (DefaultToolTip.qml uses Text.PlainText), so the only way to carry the
# Overview's green/amber/red severity signal is color-font glyphs: emoji
# render in color via Noto Color Emoji even inside plain text. Thresholds
# match views.color_for_percent / model.severity_for_percent exactly.
SEVERITY_DOT = {"ok": "\U0001f7e2", "warn": "\U0001f7e1", "critical": "\U0001f534"}
NF_ACCOUNT = "\uf007"  # fa-user
NF_PLAN = "\uf132"  # fa-shield
NF_CLOCK = "\uf017"  # fa-clock-o
NF_CALENDAR = "\uf073"  # fa-calendar
NF_PACE = "\uf0e7"  # fa-bolt
NF_CREDITS = "\uf09d"  # fa-credit-card
NF_RESET = "\uf021"  # fa-refresh
ICON_GAP = "   "


def nf(icon: str, text: str) -> str:
    return f"{icon}{ICON_GAP}{text}"


def build_codexbar_command(codexbar_bin: str = DEFAULT_CODEXBAR) -> list[str]:
    return [codexbar_bin, "usage", "--format", "json", "--json-only", "--pretty"]


def load_usage_payload_from_json_text(text: str) -> object:
    return json.loads(text)


def load_usage_from_json_text(text: str) -> list[ProviderUsage]:
    return normalize_payload(load_usage_payload_from_json_text(text))


class _CommandCancelled(RuntimeError):
    pass


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _stop_process_group(process: subprocess.Popen[str]) -> None:
    process_group_id = process.pid
    try:
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            if process.poll() is None:
                process.terminate()

        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            process.poll()
            if not _process_group_exists(process_group_id):
                break
            time.sleep(0.02)

        try:
            os.killpg(process_group_id, signal.SIGKILL)
        except ProcessLookupError:
            if process.poll() is None:
                process.kill()

        if process.poll() is None:
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    finally:
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()


def _run_codexbar_command(
    command: list[str],
    *,
    timeout: float,
    cancel_requested: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess[str]:
    if cancel_requested is not None and cancel_requested():
        raise _CommandCancelled("codexbar command cancelled")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    deadline = time.monotonic() + timeout
    while True:
        if cancel_requested is not None and cancel_requested():
            _stop_process_group(process)
            raise _CommandCancelled("codexbar command cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _stop_process_group(process)
            raise subprocess.TimeoutExpired(command, timeout)
        try:
            stdout, stderr = process.communicate(timeout=min(0.1, remaining))
        except subprocess.TimeoutExpired:
            continue
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            stdout=stdout,
            stderr=stderr,
        )


def load_usage_payload_from_command(
    codexbar_bin: str = DEFAULT_CODEXBAR,
    *,
    timeout: int = 90,
    cancel_requested: Callable[[], bool] | None = None,
) -> object:
    command = build_codexbar_command(codexbar_bin)
    completed = _run_codexbar_command(
        command,
        timeout=timeout,
        cancel_requested=cancel_requested,
    )
    stdout = completed.stdout or ""
    if stdout.strip():
        try:
            return load_usage_payload_from_json_text(stdout)
        except json.JSONDecodeError:
            if completed.returncode == 0:
                raise RuntimeError("codexbar returned invalid JSON") from None
    if completed.returncode != 0:
        detail = redact_text((completed.stderr or completed.stdout or "").strip())
        message = f"codexbar exited with status {completed.returncode}"
        if detail:
            message += f": {detail.splitlines()[0][:220]}"
        raise RuntimeError(message)
    raise RuntimeError("codexbar returned empty output")


def load_usage_from_command(
    codexbar_bin: str = DEFAULT_CODEXBAR, *, timeout: int = 90
) -> list[ProviderUsage]:
    return normalize_payload(
        load_usage_payload_from_command(codexbar_bin, timeout=timeout)
    )


def _normalized_reset_text(window: WindowUsage) -> str:
    if window.reset_countdown:
        return f"resets in {window.reset_countdown}"
    return window.reset_description


def provider_summary_lines(providers: Iterable[ProviderUsage]) -> list[str]:
    lines: list[str] = []
    for provider in providers:
        if provider.error:
            lines.append(
                f"{provider.display_name}: error: {redact_text(provider.error)}"
            )
            continue
        bits = []
        for window in provider.windows:
            reset_text = _normalized_reset_text(window)
            reset = f", {reset_text}" if reset_text else ""
            bits.append(f"{window.label} {window.used_percent:.0f}%{reset}")
        if provider.credits_remaining is not None:
            bits.append(f"credits {provider.credits_remaining:g}")
        if not bits:
            bits.append("no usage windows")
        lines.append(f"{provider.display_name}: " + "; ".join(bits))
    return [redact_text(line) for line in lines]


def _provider_header(provider: ProviderUsage) -> str:
    meta = []
    if provider.source:
        meta.append(provider.source)
    if provider.version:
        meta.append(f"v{provider.version}")
    suffix = f" ({', '.join(meta)})" if meta else ""
    return f"{provider.display_name}{suffix}"


def _tray_detail_lines(provider: ProviderUsage) -> list[str]:
    lines = []
    for window in provider.windows:
        line = f"  {window.label}: {window.used_percent:.0f}% used"
        reset_text = _normalized_reset_text(window)
        if reset_text:
            line += f" · {reset_text}"
        lines.append(line)
        if window.pace_note:
            lines.append(f"    pace: {window.pace_note}")
    if provider.credits_remaining is not None:
        lines.append(f"  credits remaining: {provider.credits_remaining:g}")
    if provider.updated_at:
        lines.append(f"  updated: {format_updated_age(provider.updated_at)}")
    if not lines:
        lines.append("  no usage windows")
    return lines


def _as_dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


TOOLTIP_WIDTH = 46


def _wrap_indented(
    text: str, *, prefix: str, cont_indent: str, width: int = TOOLTIP_WIDTH
) -> list[str]:
    """Word-wrap `text` so the first line starts with `prefix` and
    continuations align under `cont_indent`, all within `width` chars."""
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = prefix + words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if len(candidate) > width:
            lines.append(current)
            current = cont_indent + word
        else:
            current = candidate
    lines.append(current)
    return lines


def _compact_percent(value: object) -> str:
    percent = _percent_value(value)
    if percent is None:
        return "?%"
    return f"{percent:g}%"


def _finite_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = (
            float(value)
            if isinstance(value, (int, float))
            else float(str(value).strip().rstrip("%"))
        )
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _percent_value(value: object) -> float | None:
    number = _finite_number(value)
    if number is None:
        return None
    return max(0.0, min(100.0, number))


def _usage_bar(value: object, *, width: int = 10) -> str:
    percent = _percent_value(value) or 0.0
    filled = 0 if percent <= 0 else max(1, min(width, int(percent // (100 / width))))
    return "▰" * filled + "▱" * (width - filled)


def _compact_reset_description(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("Resets"):
        text = text[len("Resets") :]
        text = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", text)
        text = re.sub(r"(?<=\d)(?=[A-Za-z])", " ", text)
        text = text.replace(",", ", ")
    # Timezone region names ("(America/Chicago)") eat half the tooltip
    # width for no signal — the times are already local. Drop them.
    text = re.sub(r"\s*\([^)]*\)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _compact_reset(window: dict[str, object]) -> str:
    description = _compact_reset_description(window.get("resetDescription"))
    if description:
        return f"resets {description}"
    resets_at = window.get("resetsAt")
    if resets_at:
        return f"resets at {resets_at}"
    return ""


def _compact_date(value: object) -> str:
    text = str(value or "").strip()
    if "T" in text:
        return text.split("T", 1)[0]
    return text


def format_updated_age(value: object, *, now: "dt.datetime | None" = None) -> str:
    """Render an ISO timestamp as a short relative age like '25m ago'."""
    text = str(value or "").strip()
    parsed = parse_iso_datetime(text)
    if parsed is None:
        return text
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    seconds = max(0, int((now.astimezone(dt.timezone.utc) - parsed).total_seconds()))
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def _compact_provider_header(entry: dict[str, object], index: int) -> str:
    provider_key = str(entry.get("provider") or f"provider {index}").strip()
    provider = provider_display_name(provider_key)
    meta = []
    source = entry.get("source")
    version = entry.get("version")
    if source:
        meta.append(str(source))
    if version:
        meta.append(f"v{version}")
    suffix = f" ({', '.join(meta)})" if meta else ""
    return f"{provider}{suffix}"


def _window_icon(label: str) -> str:
    lowered = label.lower()
    if "week" in lowered or "calendar" in lowered:
        return NF_CALENDAR
    return NF_CLOCK


def _compact_window_block(
    label: str, window: dict[str, object], pace_window: dict[str, object] | None = None
) -> list[str]:
    """One usage window as short lines that survive KDE's ~46-char wrap:
    meter inline with the label, reset and pace as indented sub-lines."""
    used = window.get("usedPercent")
    lines = [
        f"  {nf(_window_icon(label), f'{label}: {_compact_percent(used)} used')} {_usage_bar(used)}"
    ]
    reset = _compact_reset(window)
    if reset:
        lines.append(f"      ↳ {reset}")
    pace = _compact_pace_line(pace_window or {})
    if pace:
        lines.append(pace)
    return lines


def _compact_pace_line(pace_window: dict[str, object]) -> str | None:
    delta = pace_window.get("deltaPercent")
    expected = pace_window.get("expectedUsedPercent")
    delta_value = _finite_number(delta)
    if delta_value is not None and not -100.0 <= delta_value <= 100.0:
        delta_value = None
    expected_value = _percent_value(expected)
    bits: list[str] = []
    if delta_value is not None:
        if delta_value < 0:
            bits.append(f"{abs(delta_value):g}% reserve")
        elif delta_value > 0:
            bits.append(f"{delta_value:g}% over pace")
        else:
            bits.append("on pace")
    if expected_value is not None:
        bits.append(f"expected {expected_value:g}%")
    if not bits:
        return None
    line = " · ".join(bits)
    will_last = pace_window.get("willLastToReset")
    if will_last is True:
        line += " ✓"
    elif will_last is False:
        line += " ⚠"
    return f"  {nf(NF_PACE, line)}"


def _append_compact_reset_credits(
    lines: list[str], reset_credits: dict[str, object]
) -> None:
    available = _finite_number(reset_credits.get("availableCount"))
    if available is not None and 0 <= available <= 1_000_000:
        lines.append(f"  {nf(NF_RESET, f'Reset credits: {available:g} available')}")
    groups: dict[tuple[str, str], list[str]] = {}
    for credit in _as_list(reset_credits.get("credits")):
        credit_dict = _as_dict(credit)
        if not credit_dict:
            continue
        title = str(credit_dict.get("title") or "reset credit")
        status = str(credit_dict.get("status") or "")
        expires = _compact_date(credit_dict.get("expires_at"))
        groups.setdefault((title, status), []).append(expires)
    for (title, status), expiries in groups.items():
        head = title if len(expiries) == 1 else f"{title} ×{len(expiries)}"
        if status and status != "available":
            head += f" · {status}"
        lines.append(f"    {head}")
        dated = sorted(expiry for expiry in expiries if expiry)
        if dated:
            note = (
                f"expires {dated[0]}"
                if len(expiries) == 1
                else f"next expires {dated[0]}"
            )
            lines.append(f"      ↳ {note}")


def _primary_usage_windows(usage: dict[str, object]) -> list[dict[str, object]]:
    windows: list[dict[str, object]] = []
    for key in ("primary", "secondary", "tertiary"):
        window = _as_dict(usage.get(key))
        if window:
            windows.append(window)
    for extra in _as_list(usage.get("extraRateWindows")):
        extra_dict = _as_dict(extra)
        window = _as_dict(extra_dict.get("window")) or extra_dict
        if window:
            windows.append(window)
    return windows


def _peak_usage_percent(usage: dict[str, object]) -> float:
    values: list[float] = []
    for window in _primary_usage_windows(usage):
        value = _percent_value(window.get("usedPercent"))
        if value is not None:
            values.append(value)
    return max(values, default=0.0)


def _compact_provider_lines(
    entry: dict[str, object],
    index: int,
    *,
    privacy_mode: bool,
) -> list[str]:
    usage = _as_dict(entry.get("usage"))
    identity = _as_dict(usage.get("identity"))
    credits = _as_dict(entry.get("credits"))
    pace = _as_dict(entry.get("pace"))

    error = entry.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("detail") or error.get("kind")
    else:
        message = error

    header = _compact_provider_header(entry, index)
    if message:
        lines = [nf(NF_WARN, header)]
        lines.extend(
            _wrap_indented(
                f"Error: {redact_text(str(message))}", prefix="  ", cont_indent="    "
            )
        )
        return lines

    lines = [nf(NF_OK, f"{header}  {_peak_usage_percent(usage):g}% peak")]

    account = (
        usage.get("accountEmail")
        or entry.get("accountEmail")
        or identity.get("accountEmail")
        or identity.get("email")
    )
    if account:
        account_text = "[REDACTED]" if privacy_mode else str(account)
        lines.append(f"  {nf(NF_ACCOUNT, f'Account: {account_text}')}")

    plan = usage.get("loginMethod") or identity.get("loginMethod")
    confidence = usage.get("dataConfidence")
    plan_bits = []
    if plan:
        plan_bits.append(f"Plan: {plan}")
    if confidence:
        plan_bits.append(f"confidence: {confidence}")
    if plan_bits:
        lines.append("  " + nf(NF_PLAN, " · ".join(plan_bits)))

    for key, label in (
        ("primary", "5h/session"),
        ("secondary", "weekly"),
        ("tertiary", "tertiary"),
    ):
        window = _as_dict(usage.get(key))
        if window:
            lines.extend(_compact_window_block(label, window, _as_dict(pace.get(key))))

    extra_windows = _as_list(usage.get("extraRateWindows"))
    for extra in extra_windows:
        extra_dict = _as_dict(extra)
        window = _as_dict(extra_dict.get("window")) or extra_dict
        if not window:
            continue
        title = str(extra_dict.get("title") or extra_dict.get("id") or "extra window")
        lines.extend(_compact_window_block(title, window))

    if "remaining" in credits:
        lines.append(
            "  " + nf(NF_CREDITS, f"Credits: {credits.get('remaining')} remaining")
        )
    credit_events = _as_list(credits.get("events"))
    if credit_events:
        lines.append("  " + nf(NF_CREDITS, f"Credit events: {len(credit_events)}"))

    reset_credits = _as_dict(usage.get("codexResetCredits"))
    if reset_credits:
        _append_compact_reset_credits(lines, reset_credits)

    updated = (
        usage.get("updatedAt")
        or credits.get("updatedAt")
        or reset_credits.get("updatedAt")
    )
    if updated:
        lines.append("  " + nf(NF_CLOCK, f"Updated: {format_updated_age(updated)}"))

    return lines


def format_payload_lines(
    raw_payload: object,
    *,
    privacy_mode: bool = True,
) -> list[str]:
    entries = raw_payload if isinstance(raw_payload, list) else [raw_payload]
    lines: list[str] = []
    for index, entry in enumerate(entries, start=1):
        if index > 1:
            lines.append("")
        if isinstance(entry, dict):
            lines.extend(
                _compact_provider_lines(
                    entry,
                    index,
                    privacy_mode=privacy_mode,
                )
            )
    redactor = redact_text if privacy_mode else redact_credentials
    return [redactor(line) for line in lines]


def _provider_count(
    raw_payload: object | None, providers: Iterable[ProviderUsage]
) -> int:
    if isinstance(raw_payload, list):
        return len([entry for entry in raw_payload if isinstance(entry, dict)])
    if isinstance(raw_payload, dict):
        return 1
    return len(list(providers))


def _glance_reset_text(window: WindowUsage) -> str:
    if window.reset_countdown:
        return f"resets {window.reset_countdown}"
    description = _compact_reset_description(window.reset_description)
    return f"resets {description}" if description else ""


def build_tray_tooltip(
    providers: Iterable[ProviderUsage],
    error: str = "",
    raw_payload: object | None = None,
    *,
    privacy_mode: bool = True,
) -> str:
    """Build the hover text shown by the system tray icon.

    A glance card, not a report: one line per provider (its tightest window
    only) plus the aggregate answers — tightest overall, next reset, banked
    reset credits. Depth lives one click away in the dashboard. KDE/Qt tray
    tooltips are plain text, so this uses compact typography, status glyphs,
    and Unicode meters rather than HTML/CSS. Error text is still redacted.
    """
    provider_list = list(providers)
    count = _provider_count(raw_payload, provider_list)
    noun = "provider" if count == 1 else "providers"
    lines = [nf(NF_DASHBOARD, f"CodexBar KDE • {count} {noun}")]
    if error:
        first_line = redact_text(error).splitlines()[0][:220]
        lines.append(nf(NF_WARN, "Refresh failed"))
        lines.extend(_wrap_indented(first_line, prefix="  ", cont_indent="    "))
        return "\n".join(lines)

    if not provider_list:
        lines.append("No provider data yet")
    else:
        lines.append("────────────────────────")
        ok_providers = [p for p in provider_list if not p.error]
        tightest = fleet_tightest(ok_providers)
        soonest = fleet_next_reset(ok_providers)
        for provider in provider_list:
            if provider.error:
                lines.append(nf(NF_WARN, _provider_header(provider)))
                lines.extend(
                    _wrap_indented(
                        f"Error: {redact_text(provider.error)}",
                        prefix="  ",
                        cont_indent="    ",
                    )
                )
                continue
            window = provider.tightest_window
            if window is None:
                lines.append(nf(NF_OK, f"{provider.display_name}  no usage windows"))
                continue
            left = 100 - window.used_percent
            glyph = SEVERITY_DOT[severity_for_percent(window.used_percent)]
            line = f"{provider.display_name}  {left:.0f}% left {_usage_bar(left)}"
            credits_text = (
                f"credits {provider.credits_remaining:g}"
                if provider.credits_remaining
                else ""
            )
            for extra in (_glance_reset_text(window), credits_text):
                if extra and len(nf(glyph, f"{line} · {extra}")) <= TOOLTIP_WIDTH:
                    line = f"{line} · {extra}"
            lines.append(nf(glyph, line))

        footer: list[str] = []
        if tightest is not None:
            provider, window = tightest
            left_text = f"{100 - window.used_percent:.0f}% left"
            full = nf(
                NF_PACE,
                f"Tightest: {provider.display_name} {window.label} · {left_text}",
            )
            short = nf(NF_PACE, f"Tightest: {provider.display_name} · {left_text}")
            footer.append(full if len(full) <= TOOLTIP_WIDTH else short)
        if soonest is not None:
            _, provider, window = soonest
            when = (
                window.reset_countdown
                or _compact_reset_description(window.reset_description)
                or "soon"
            )
            full = nf(NF_CLOCK, f"Next reset: {when} · {provider.display_name}")
            short = nf(NF_CLOCK, f"Next reset: {when}")
            footer.append(full if len(full) <= TOOLTIP_WIDTH else short)
        credit_count = len(
            available_reset_credits(credits_from_usage_payload(raw_payload))
        )
        if credit_count:
            footer.append(nf(NF_RESET, f"Reset credits: {credit_count} available"))
        if footer:
            lines.append("────────────────────────")
            lines.extend(footer)
    lines.append("")
    lines.append("Click: dashboard  •  Right-click: menu")
    redactor = redact_text if privacy_mode else redact_credentials
    return "\n".join(redactor(line) for line in lines)


class UsageWorker(QThread):
    finished_with_result = pyqtSignal(object, str, object)
    history_updated = pyqtSignal(object, bool)

    def __init__(
        self,
        codexbar_bin: str,
        parent: QObject | None = None,
        *,
        history_store: HistoryStore | None = None,
    ) -> None:
        super().__init__(parent)
        self.codexbar_bin = codexbar_bin
        self.history_store = history_store
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self.requestInterruption()
        self._cancel_event.set()

    def _record_history(self, providers: list[ProviderUsage]) -> list[Sample]:
        if self.history_store is None or self._cancel_event.is_set():
            return []
        try:
            return self.history_store.record(providers)
        except OSError:
            return []

    def _update_history(self, recorded: list[Sample]) -> None:
        if self.history_store is None or self._cancel_event.is_set():
            return
        try:
            pruned = self.history_store.prune_if_due(days=60)
        except OSError:
            return
        if pruned is not None:
            self.history_updated.emit(pruned, True)
        elif recorded:
            self.history_updated.emit(recorded, False)

    def run(self) -> None:
        try:
            raw_payload = load_usage_payload_from_command(
                self.codexbar_bin,
                cancel_requested=self._cancel_event.is_set,
            )
            providers = normalize_payload(raw_payload)
            recorded = self._record_history(providers)
            self.finished_with_result.emit(providers, "", raw_payload)
            self._update_history(recorded)
        except _CommandCancelled:
            return
        except Exception as exc:  # GUI boundary: show sanitized concise error
            self.finished_with_result.emit([], redact_text(str(exc)), None)
            self._update_history([])


class RedeemWorker(QThread):
    """Consumes one Codex banked reset credit off the UI thread."""

    finished_with_result = pyqtSignal(bool, str)

    def __init__(self, credit_id: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.credit_id = credit_id

    def run(self) -> None:
        try:
            token, account_id = load_codex_auth()
            result = redeem_reset_credit(token, account_id, self.credit_id)
            windows = result.get("windows_reset")
            redeemed_at = (result.get("credit") or {}).get("redeemed_at") or ""
            if result.get("reconciled"):
                message = "Redeemed — confirmed after status refresh"
            else:
                message = f"Redeemed — windows reset: {windows}"
            if redeemed_at:
                message += f" · {redeemed_at}"
            self.finished_with_result.emit(True, message)
        except (CodexAuthError, CodexResetError) as exc:
            self.finished_with_result.emit(False, redact_text(str(exc)))
        except Exception as exc:  # network layer edge cases
            self.finished_with_result.emit(False, redact_text(str(exc)))


NAV_ICON_PX = 20


def _glyph_icon(
    glyph: str, color: str, active_color: str | None = None, px: int = NAV_ICON_PX
) -> QIcon:
    """Render a text glyph into a fixed-size icon so nav labels align on a
    uniform column regardless of per-glyph advance widths. When
    ``active_color`` is given it is used for the checked (On) state."""

    def _pixmap(fill: str) -> QPixmap:
        pixmap = QPixmap(px, px)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont()
        font.setPixelSize(px - 9 if len(glyph) > 1 else px - 5)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(fill))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
        painter.end()
        return pixmap

    icon = QIcon(_pixmap(color))
    if active_color:
        icon.addPixmap(_pixmap(active_color), QIcon.Mode.Normal, QIcon.State.On)
    return icon


def make_app_icon() -> QIcon:
    pixmap = QPixmap(128, 128)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor("#151926"))
    painter.setPen(QColor("#3f4a68"))
    painter.drawRoundedRect(8, 8, 112, 112, 22, 22)
    colors = [QColor("#41d17d"), QColor("#f1c857"), QColor("#ff6b6b")]
    widths = [78, 54, 30]
    y_values = [34, 60, 86]
    for color, width, y in zip(colors, widths, y_values, strict=True):
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#2d3448"))
        painter.drawRoundedRect(28, y, 72, 12, 6, 6)
        painter.setBrush(color)
        painter.drawRoundedRect(28, y, width, 12, 6, 6)
    painter.end()
    return QIcon(pixmap)


# ---------------------------------------------------------------------------
# UI — CodexBar-inspired flat dark shell with multiple statistic views.
# Chart widgets and view classes live in views.py; this file wires them
# to the refresh worker, history store, and tray.
# ---------------------------------------------------------------------------


def _privacy_window_options(
    providers: list[ProviderUsage], *, privacy_mode: bool
) -> list[tuple[str, str, str]]:
    return [
        (
            provider_key,
            window_key,
            redact_text(label, redact_emails=privacy_mode),
        )
        for provider_key, window_key, label in window_options(providers)
    ]


class DashboardWindow(QMainWindow):
    data_changed = pyqtSignal(object, str, object)

    VIEWS = ("Overview", "History", "Burn-down", "Details")
    # collapsed-rail glyphs: plain geometric Unicode, no font dependency
    VIEW_GLYPHS = {
        "Overview": "\u25a6",  # ▦
        "History": "\u25a4",  # ▤
        "Burn-down": "\u25e2",  # ◢
        "Details": "{ }",
    }
    SIDEBAR_WIDTH = 190
    RAIL_WIDTH = 52
    MIN_WIDTH = 860
    MIN_HEIGHT = 560

    def __init__(
        self,
        *,
        codexbar_bin: str = DEFAULT_CODEXBAR,
        refresh_seconds: int = DEFAULT_REFRESH_SECONDS,
        history_store: HistoryStore | None = None,
        history_samples: Iterable[Sample] | None = None,
        privacy_mode: bool = True,
    ) -> None:
        super().__init__()
        self.codexbar_bin = codexbar_bin
        self.refresh_seconds = clamp_refresh_seconds(refresh_seconds)
        self.privacy_mode = bool(privacy_mode)
        self.worker: UsageWorker | None = None
        self.redeem_worker: RedeemWorker | None = None
        self._shutting_down = False
        self.history = history_store or HistoryStore()
        self._history_samples = sorted(
            (sample for sample in history_samples or [] if isinstance(sample, Sample)),
            key=lambda sample: sample.ts,
        )
        self.providers: list[ProviderUsage] = []
        self.raw_payload: object | None = None
        self.last_error = ""
        self.setWindowTitle("CodexBar KDE")
        self.setWindowIcon(make_app_icon())

        root = QWidget()
        self.setCentralWidget(root)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        # ---- sidebar -------------------------------------------------
        # expanded: brand + labeled nav + status text + Refresh button.
        # collapsed: 52px glyph rail — same buttons relabeled, status as dot.
        self.sidebar = QFrame()
        sidebar = self.sidebar
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(self.SIDEBAR_WIDTH)
        side = QVBoxLayout(sidebar)
        self._side_layout = side
        side.setContentsMargins(14, 16, 14, 16)
        side.setSpacing(4)
        # top row: toggle left of the brand — one shared icon column with
        # the nav buttons below; subtitle stays under the brand text
        head = QHBoxLayout()
        head.setSpacing(8)
        # 5px lead margin puts the toggle's center on the same x as the nav
        # icons (expanded: 14+5+15 == 14+10+10; rail: 6+5+15 == rail mid 26)
        head.setContentsMargins(5, 0, 0, 0)
        self.nav_toggle = QPushButton("\u2630")
        self.nav_toggle.setObjectName("NavToggle")
        self.nav_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self.nav_toggle.setToolTip("Toggle sidebar (Ctrl+B)")
        self.nav_toggle.setFixedSize(30, 30)
        self.nav_toggle.clicked.connect(self.toggle_sidebar)
        head.addWidget(self.nav_toggle)
        self.brand = QLabel("CodexBar")
        self.brand.setStyleSheet(
            f"color: {TEXT}; font-size: 16px; font-weight: 800; background: transparent;"
        )
        head.addWidget(self.brand)
        head.addStretch(1)
        side.addLayout(head)
        self.brand_sub = QLabel("usage dashboard")
        self.brand_sub.setStyleSheet(
            f"color: {MUTED}; font-size: 11px; background: transparent;"
        )
        self.brand_sub.setIndent(38)  # under the brand text, past the icon column
        side.addWidget(self.brand_sub)
        side.addSpacing(16)
        self.nav_buttons: dict[str, QPushButton] = {}
        for name in self.VIEWS:
            button = QPushButton(name)
            button.setIcon(_glyph_icon(self.VIEW_GLYPHS[name], MUTED, TEXT))
            button.setIconSize(QSize(NAV_ICON_PX, NAV_ICON_PX))
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _, n=name: self.show_view(n))
            button.setObjectName("NavButton")
            side.addWidget(button)
            self.nav_buttons[name] = button
        side.addStretch(1)
        self.status_label = QLabel()  # rendered by _render_status
        self.status_label.setWordWrap(True)
        side.addWidget(self.status_label)
        side.addSpacing(8)  # breathing room between status and Refresh
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setObjectName("RefreshButton")
        self.refresh_button.clicked.connect(self.refresh_now)
        side.addWidget(self.refresh_button)
        shell.addWidget(sidebar)

        # ---- content -------------------------------------------------
        content = QVBoxLayout()
        content.setContentsMargins(22, 18, 18, 14)
        content.setSpacing(10)

        header = QHBoxLayout()
        self.view_title = QLabel(self.VIEWS[0])
        self.view_title.setStyleSheet(
            f"color: {TEXT}; font-size: 21px; font-weight: 800; letter-spacing: -0.4px; background: transparent;"
        )
        header.addWidget(self.view_title)
        header.addStretch(1)
        content.addLayout(header)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(
            "background: #241117; color: #ffb2b2; border: 1px solid #5a2c34; border-radius: 6px; padding: 8px 10px; font-size: 12px;"
        )
        self.error_label.hide()
        content.addWidget(self.error_label)

        self.stack = QStackedWidget()
        self.view_overview = OverviewView()
        self.view_overview.reset_panel.redeem_requested.connect(
            self._redeem_reset_credit
        )
        self.view_history = HistoryView()
        self.view_history.selection_changed.connect(self._update_history_chart)
        self.view_burndown = BurnDownView()
        self.view_burndown.selection_changed.connect(self._update_burndown_chart)
        self.view_details = DetailsView()
        self.view_details.set_privacy_mode(self.privacy_mode)
        self.view_details.privacy_changed.connect(self.set_privacy_mode)
        self._views: dict[str, QWidget] = {
            "Overview": self.view_overview,
            "History": self.view_history,
            "Burn-down": self.view_burndown,
            "Details": self.view_details,
        }
        for name in self.VIEWS:
            self.stack.addWidget(self._views[name])
        content.addWidget(self.stack, 1)
        shell.addLayout(content, 1)

        self.setStyleSheet(f"""
            QMainWindow, QWidget {{
                background: {BG};
                color: {TEXT};
                font-family: Inter, system-ui, -apple-system, Segoe UI, sans-serif;
            }}
            QLabel {{ background: transparent; }}
            QFrame#Sidebar {{
                background: {SURFACE};
                border-right: 1px solid {HAIRLINE};
            }}
            QPushButton#NavButton {{
                background: transparent;
                color: {MUTED};
                border: none;
                border-radius: 6px;
                padding: 8px 10px;
                text-align: left;
                font-size: 13px;
                font-weight: 600;
                min-height: 24px;
                max-height: 24px;
            }}
            QPushButton#NavButton:hover {{ background: #1a1a1f; color: {TEXT}; }}
            QPushButton#NavButton:checked {{ background: #202027; color: {TEXT}; }}
            QPushButton#NavButton[rail="true"] {{
                text-align: center;
                padding: 8px 0;
            }}
            QPushButton#NavToggle {{
                background: transparent;
                color: {MUTED};
                border: none;
                border-radius: 6px;
                font-size: 15px;
            }}
            QPushButton#NavToggle:hover {{ background: #1a1a1f; color: {TEXT}; }}
            QPushButton#RefreshButton {{
                background: #202027;
                color: {TEXT};
                border: 1px solid {HAIRLINE};
                border-radius: 6px;
                padding: 7px 10px;
                font-weight: 700;
                font-size: 12px;
            }}
            QPushButton#RefreshButton:hover {{ background: #2a2a32; }}
            QPushButton#RefreshButton:disabled {{ color: {MUTED}; }}
            QComboBox {{
                background: {SURFACE};
                color: {TEXT};
                border: 1px solid {HAIRLINE};
                border-radius: 6px;
                padding: 5px 10px;
                font-size: 12px;
            }}
            QComboBox::drop-down {{ border: none; width: 20px; }}
            QComboBox QAbstractItemView {{
                background: {SURFACE};
                color: {TEXT};
                border: 1px solid {HAIRLINE};
                selection-background-color: #24242c;
            }}
            QScrollBar:vertical {{ background: {BG}; width: 9px; margin: 2px; }}
            QScrollBar::handle:vertical {{ background: #2b2b33; border-radius: 4px; min-height: 32px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
        """)

        self.show_view(self.VIEWS[0])

        # ---- sidebar toggle ------------------------------------------
        self._sidebar_expanded = True
        self._status_text, self._status_color = "Not refreshed yet", MUTED
        self._settings = QSettings("codexbar-kde", "codexbar-kde")
        toggle = QShortcut(QKeySequence("Ctrl+B"), self)
        toggle.activated.connect(self.toggle_sidebar)
        for index, name in enumerate(self.VIEWS, start=1):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index}"), self)
            shortcut.activated.connect(lambda n=name: self.show_view(n))
        self.set_sidebar_visible(
            self._settings.value("sidebar_visible", True, type=bool)
        )

        self.timer = QTimer(self)
        self.timer.setInterval(self.refresh_seconds * 1000)
        self.timer.timeout.connect(self.refresh_now)
        self.timer.start()

    # ---- view API ----------------------------------------------------

    def toggle_sidebar(self) -> None:
        self.set_sidebar_visible(not self._sidebar_expanded)

    def set_sidebar_visible(self, visible: bool) -> None:
        """Expanded sidebar (labels) vs collapsed glyph rail — never gone."""
        self._sidebar_expanded = visible
        self.sidebar.setFixedWidth(self.SIDEBAR_WIDTH if visible else self.RAIL_WIDTH)
        margin = 14 if visible else 6
        self._side_layout.setContentsMargins(margin, 16, margin, 16)
        # toggle leads the head row; collapsed just hides the brand text and
        # drops nav labels — the icon column keeps everything aligned
        self.brand.setVisible(visible)
        # subtitle stays as a blank spacer on the rail so nav buttons keep
        # their expanded y-positions (pixel-exact alignment invariant)
        self.brand_sub.setText("usage dashboard" if visible else "")
        for name, button in self.nav_buttons.items():
            button.setText(name if visible else "")
            button.setToolTip("" if visible else name)
            style = button.style()
            if style is not None:
                button.setProperty("rail", not visible)
                style.unpolish(button)
                style.polish(button)
        self.refresh_button.setText("Refresh" if visible else "\u21bb")
        self.refresh_button.setToolTip("" if visible else "Refresh")
        self._settings.setValue("sidebar_visible", visible)
        self._render_status()
        self._sync_minimum_size()

    def _sync_minimum_size(self) -> None:
        """An explicit window minimum overrides the layout-derived one, so
        retune it whenever sidebar state or content changes — otherwise a
        narrow window silently clips content (e.g. the credits panel with
        the sidebar expanded)."""
        root = self.centralWidget()
        layout = root.layout() if root else None
        if layout is None:
            return
        layout.activate()
        width = max(self.MIN_WIDTH, layout.minimumSize().width())
        if width != self.minimumWidth():
            self.setMinimumSize(width, self.MIN_HEIGHT)

    def _set_status(self, text: str, color: str = MUTED) -> None:
        self._status_text, self._status_color = text, color
        self._render_status()

    def _render_status(self) -> None:
        """Full status text expanded; just its colored dot on the rail."""
        expanded = self._sidebar_expanded
        self.status_label.setText(self._status_text if expanded else "●")
        self.status_label.setToolTip("" if expanded else self._status_text)
        self.status_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft if expanded else Qt.AlignmentFlag.AlignHCenter
        )
        self.status_label.setStyleSheet(
            f"color: {self._status_color}; font-size: 11px; font-weight: 700;"
            " background: transparent;"
        )

    def view_names(self) -> list[str]:
        return list(self.VIEWS)

    def show_view(self, name: str) -> None:
        if name not in self._views:
            return
        self.stack.setCurrentWidget(self._views[name])
        self.view_title.setText(name)
        for button_name, button in self.nav_buttons.items():
            button.setChecked(button_name == name)

    def current_view_name(self) -> str:
        current = self.stack.currentWidget()
        for name, widget in self._views.items():
            if widget is current:
                return name
        return ""

    def overview_summary_text(self) -> str:
        return self.view_overview.summary_text()

    def details_text(self) -> str:
        return self.view_details.plain_text()

    def set_privacy_mode(self, enabled: bool) -> None:
        self.privacy_mode = bool(enabled)
        self.view_details.set_privacy_mode(self.privacy_mode)
        self.view_overview.set_providers(
            self.providers,
            privacy_mode=self.privacy_mode,
        )
        options = _privacy_window_options(
            self.providers,
            privacy_mode=self.privacy_mode,
        )
        self.view_history.set_options(options)
        self.view_burndown.set_options(options)
        if self.last_error:
            self.error_label.setText(
                redact_text(self.last_error, redact_emails=self.privacy_mode)
            )
        self._render_details()
        self.data_changed.emit(self.providers, self.last_error, self.raw_payload)

    def _render_details(self) -> None:
        if self.raw_payload is not None:
            payload_lines = format_payload_lines(
                self.raw_payload,
                privacy_mode=self.privacy_mode,
            )
            self.view_details.set_text("\n".join(payload_lines))
        elif self.providers:
            provider_lines: list[str] = []
            for index, provider in enumerate(self.providers):
                if index:
                    provider_lines.append("")
                provider_lines.append(_provider_header(provider))
                provider_lines.extend(_tray_detail_lines(provider))
            self.view_details.set_text("\n".join(provider_lines))
        else:
            self.view_details.set_text("No data yet.")

    # ---- data --------------------------------------------------------

    def set_providers(
        self,
        providers: list[ProviderUsage],
        error: str = "",
        raw_payload: object | None = None,
    ) -> None:
        self.providers = providers
        self.raw_payload = raw_payload
        self.last_error = error
        self.error_label.setVisible(bool(error))
        self.error_label.setText(redact_text(error, redact_emails=self.privacy_mode))
        if not providers and not error:
            self.error_label.setVisible(True)
            self.error_label.setText(
                "No providers returned by codexbar. Enable providers with the CodexBar CLI config."
            )

        self.view_overview.set_providers(
            providers,
            privacy_mode=self.privacy_mode,
        )
        self.view_overview.set_reset_credits(
            credits_from_usage_payload(raw_payload),
            privacy_mode=self.privacy_mode,
        )
        options = _privacy_window_options(
            providers,
            privacy_mode=self.privacy_mode,
        )
        self.view_history.set_options(options)
        self.view_burndown.set_options(options)
        self._update_history_chart()
        self._update_burndown_chart()
        self._render_details()
        self._sync_minimum_size()  # after all views update: widths depend on data

        if providers:
            error_count = sum(1 for provider in providers if provider.error)
            ok_count = len(providers) - error_count
            if error_count:
                self._set_status(f"● {ok_count} online · {error_count} warning", WARN)
            else:
                self._set_status(f"● {len(providers)} providers live", GOOD)
        else:
            self._set_status("Refresh failed", SOFT_CRIT)
        self.data_changed.emit(providers, error, raw_payload)

    def _accent_for(self, provider_key: str) -> str:
        return provider_accent_color(provider_key)

    # ---- reset credits -------------------------------------------------

    def _redeem_reset_credit(self, credit_id: str) -> None:
        if self._shutting_down or self.redeem_worker is not None:
            return
        panel = self.view_overview.reset_panel
        confirm = QMessageBox.question(
            self,
            "Redeem Codex reset credit",
            "Spend the banked reset credit that expires soonest?\n\n"
            "This resets your Codex rate-limit windows now. The credit is "
            "consumed permanently and cannot be un-redeemed.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        panel.set_busy(True, "Redeeming…")
        worker = RedeemWorker(credit_id, self)
        self.redeem_worker = worker
        worker.finished_with_result.connect(self._redeem_finished)
        worker.finished.connect(
            lambda worker=worker: self._redeem_worker_stopped(worker)
        )
        worker.start()

    def _redeem_finished(self, ok: bool, message: str) -> None:
        panel = self.view_overview.reset_panel
        panel.set_busy(False)
        panel.show_result(message, ok=ok)
        if ok:
            # Pull fresh usage so windows and the credit list update.
            self.refresh_now()

    def _redeem_worker_stopped(self, worker: RedeemWorker) -> None:
        if self.redeem_worker is worker:
            self.redeem_worker = None
        worker.deleteLater()

    def _update_history_chart(self) -> None:
        selection = self.view_history.current_selection()
        if not selection:
            self.view_history.set_series([], TEAL)
            return
        provider_key, window_key = selection
        samples = self._history_samples
        series = daily_peaks(
            samples, provider=provider_key, window_key=window_key, days=30
        )
        message = "" if any(p.value > 0 for p in series) else "No recorded days yet"
        self.view_history.set_series(series, self._accent_for(provider_key), message)

    def _update_burndown_chart(self) -> None:
        selection = self.view_burndown.current_selection()
        if not selection:
            self.view_burndown.set_burn_down(None, TEAL, "")
            return
        provider_key, window_key = selection
        resets_at, window_minutes = latest_reset_at(
            self.providers, provider_key, window_key
        )
        if resets_at is None or not window_minutes:
            self.view_burndown.set_burn_down(
                None, TEAL, "This window does not report a reset time."
            )
            return
        samples = [
            (s.ts, s.windows[window_key])
            for s in self._history_samples
            if s.provider == provider_key and window_key in s.windows
        ]
        burn = burn_down_series(
            samples, window_minutes=window_minutes, resets_at=resets_at
        )
        message = "" if burn.actual else "No samples in the current window yet."
        self.view_burndown.set_burn_down(burn, self._accent_for(provider_key), message)

    # ---- refresh -----------------------------------------------------

    def refresh_now(self) -> None:
        if self._shutting_down or self.worker is not None:
            return
        self.refresh_button.setEnabled(False)
        self._set_status("Refreshing…")
        worker = UsageWorker(
            self.codexbar_bin,
            self,
            history_store=self.history,
        )
        self.worker = worker
        worker.finished_with_result.connect(self._refresh_finished)
        worker.history_updated.connect(self._history_worker_updated)
        worker.finished.connect(
            lambda worker=worker: self._usage_worker_stopped(worker)
        )
        worker.start()

    def _refresh_finished(
        self, providers: object, error: str, raw_payload: object
    ) -> None:
        self.refresh_button.setEnabled(True)
        typed_providers = providers if isinstance(providers, list) else []
        self.set_providers(typed_providers, error, raw_payload)

    def _history_worker_updated(self, samples: object, replace: bool) -> None:
        typed_samples = (
            [sample for sample in samples if isinstance(sample, Sample)]
            if isinstance(samples, list)
            else []
        )
        if replace:
            self._history_samples = typed_samples
        else:
            self._history_samples.extend(typed_samples)
            self._history_samples.sort(key=lambda sample: sample.ts)
        self._update_history_chart()
        self._update_burndown_chart()

    def _usage_worker_stopped(self, worker: UsageWorker) -> None:
        if self.worker is worker:
            self.worker = None
        worker.deleteLater()

    def shutdown_workers(self) -> None:
        """Stop cancellable work and wait for irreversible work before teardown."""
        if self._shutting_down:
            return
        self._shutting_down = True
        self.timer.stop()

        usage_worker = self.worker
        if usage_worker is not None:
            usage_worker.cancel()
            usage_worker.wait()
            self.worker = None
            usage_worker.deleteLater()

        redeem_worker = self.redeem_worker
        if redeem_worker is not None:
            # Redemption may already have issued its irreversible POST. Its
            # network calls have finite timeouts, so wait rather than aborting.
            redeem_worker.wait()
            self.redeem_worker = None
            redeem_worker.deleteLater()


class TrayController(QObject):
    def __init__(self, window: DashboardWindow, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.window = window
        self.tray = QSystemTrayIcon(make_app_icon(), self)
        self.tray.setToolTip(build_tray_tooltip([]))
        menu = QMenu()
        open_action = QAction("Open dashboard", self)
        open_action.triggered.connect(self.show_window)
        menu.addAction(open_action)
        view_menu = menu.addMenu("Views")
        if view_menu is None:
            raise RuntimeError("Could not create tray Views menu")
        for name in DashboardWindow.VIEWS:
            action = QAction(name, self)
            action.triggered.connect(lambda _, n=name: self.show_view(n))
            view_menu.addAction(action)
        refresh_action = QAction("Refresh", self)
        refresh_action.triggered.connect(self.window.refresh_now)
        menu.addAction(refresh_action)
        menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(QApplication.instance().quit)  # type: ignore[union-attr]
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._activated)
        self.window.data_changed.connect(self._data_changed)
        self.tray.show()

    def _data_changed(self, providers: object, error: str, raw_payload: object) -> None:
        typed_providers = providers if isinstance(providers, list) else []
        self.tray.setToolTip(
            build_tray_tooltip(
                typed_providers,
                error,
                raw_payload,
                privacy_mode=self.window.privacy_mode,
            )
        )

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_window()

    def show_view(self, name: str) -> None:
        self.window.show_view(name)
        self.show_window()

    def show_window(self) -> None:
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()


def run_once(codexbar_bin: str) -> int:
    providers = load_usage_from_command(codexbar_bin)
    for line in provider_summary_lines(providers):
        print(line)
    return 0


def run_test_render(codexbar_bin: str) -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication(["codexbar-kde", "--test-render"])
    raw_payload = [
        {
            "provider": "codex",
            "source": "render-smoke",
            "usage": {
                "primary": {"usedPercent": 12, "windowMinutes": 300},
                "secondary": {"usedPercent": 34, "windowMinutes": 10080},
            },
        },
        {
            "provider": "claude",
            "source": "render-smoke",
            "usage": {"primary": {"usedPercent": 23}},
        },
    ]
    providers = normalize_payload(raw_payload)
    window = DashboardWindow(codexbar_bin=codexbar_bin, refresh_seconds=3600)
    window.set_providers(providers, raw_payload=raw_payload)
    window.show()
    app.processEvents()
    for name in window.view_names():
        window.show_view(name)
        app.processEvents()
    window.show_view(window.VIEWS[0])
    print(
        f"rendered {len(providers)} providers across {len(window.view_names())} views"
    )
    window.close()
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    from . import __version__

    parser = argparse.ArgumentParser(
        description="Local CodexBar usage dashboard for KDE/Linux"
    )
    parser.add_argument(
        "--version", action="version", version=f"codexbar-kde {__version__}"
    )
    parser.add_argument(
        "--codexbar-bin", default=DEFAULT_CODEXBAR, help="Path to codexbar CLI"
    )
    parser.add_argument(
        "--refresh-seconds",
        type=int,
        default=DEFAULT_REFRESH_SECONDS,
        help="Auto-refresh interval",
    )
    parser.add_argument(
        "--once", action="store_true", help="Print a privacy-safe text summary and exit"
    )
    parser.add_argument(
        "--test-render",
        action="store_true",
        help="Create the Qt UI offscreen once and exit",
    )
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="Show only a normal window, without a system tray icon",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(list(sys.argv[1:] if argv is None else argv))
    if args.once:
        return run_once(args.codexbar_bin)
    if args.test_render:
        return run_test_render(args.codexbar_bin)

    app = QApplication(sys.argv)
    app.setApplicationName("CodexBar KDE")
    app.setApplicationDisplayName("CodexBar KDE")
    try:
        app.setDesktopFileName("io.github.BearHuddleston.codexbar_kde")
    except AttributeError:
        pass
    app.setQuitOnLastWindowClosed(args.no_tray)
    icon = make_app_icon()
    app.setWindowIcon(icon)

    window = DashboardWindow(
        codexbar_bin=args.codexbar_bin, refresh_seconds=args.refresh_seconds
    )
    app.aboutToQuit.connect(window.shutdown_workers)
    tray_controller = None
    if not args.no_tray and QSystemTrayIcon.isSystemTrayAvailable():
        tray_controller = TrayController(window, app)
        # Keep reference alive via QApplication dynamic property.
        app.setProperty("tray_controller", tray_controller)
    elif not args.no_tray:
        # Tray requested but unavailable: fall back to a plain window and make
        # sure closing it quits instead of leaving an unreachable process.
        app.setQuitOnLastWindowClosed(True)
        QMessageBox.information(
            window,
            "CodexBar KDE",
            "System tray is unavailable; opening as a normal window.",
        )
    window.show()
    window.refresh_now()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
