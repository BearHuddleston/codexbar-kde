"""Usage history persistence and chart series for the CodexBar dashboard.

Samples are stored as JSON Lines under the XDG state dir so history
survives restarts without a database. Each line is one provider sample:

    {"ts": "2026-07-04T12:00:00+00:00", "provider": "codex",
     "windows": {"primary": 10.0, "secondary": 5.0}}
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .model import ProviderUsage, parse_iso_datetime


def default_history_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(state_home) / "codexbar-kde" / "history.jsonl"


@dataclass(frozen=True)
class Sample:
    ts: dt.datetime
    provider: str
    windows: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class SeriesPoint:
    ts: dt.datetime
    value: float


@dataclass(frozen=True)
class BurnDown:
    window_start: dt.datetime
    resets_at: dt.datetime
    actual: list[SeriesPoint]

    def ideal_remaining_at(self, when: dt.datetime) -> float:
        total = (self.resets_at - self.window_start).total_seconds()
        if total <= 0:
            return 0.0
        elapsed = (when - self.window_start).total_seconds()
        return max(0.0, min(100.0, 100.0 * (1.0 - elapsed / total)))


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.astimezone(dt.timezone.utc)


class HistoryStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or default_history_path()

    def record(self, providers: Iterable[ProviderUsage], *, now: dt.datetime | None = None) -> None:
        now = _utc(now or dt.datetime.now(dt.timezone.utc))
        lines = []
        for provider in providers:
            if provider.error or not provider.windows:
                continue
            windows = {w.key: w.used_percent for w in provider.windows}
            lines.append(json.dumps({
                "ts": now.isoformat(),
                "provider": provider.provider,
                "windows": windows,
            }, ensure_ascii=False))
        if not lines:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def load(self) -> list[Sample]:
        if not self.path.exists():
            return []
        samples: list[Sample] = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = parse_iso_datetime(str(data.get("ts") or ""))
                if ts is None or not isinstance(data.get("windows"), dict):
                    continue
                windows = {}
                for key, value in data["windows"].items():
                    try:
                        windows[str(key)] = float(value)
                    except (TypeError, ValueError):
                        continue
                samples.append(Sample(ts=ts, provider=str(data.get("provider") or ""), windows=windows))
        samples.sort(key=lambda s: s.ts)
        return samples

    def prune(self, *, days: int, now: dt.datetime | None = None) -> None:
        now = _utc(now or dt.datetime.now(dt.timezone.utc))
        cutoff = now - dt.timedelta(days=days)
        samples = [s for s in self.load() if s.ts >= cutoff]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".jsonl.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            for sample in samples:
                fh.write(json.dumps({
                    "ts": sample.ts.isoformat(),
                    "provider": sample.provider,
                    "windows": sample.windows,
                }, ensure_ascii=False) + "\n")
        tmp.replace(self.path)


def daily_peaks(samples: Iterable[Sample], *, provider: str, window_key: str,
                days: int, now: dt.datetime | None = None) -> list[SeriesPoint]:
    """Max used-percent per calendar day (UTC) for the last `days` days.

    Days without samples yield 0 so charts keep a stable x-axis.
    """
    now = _utc(now or dt.datetime.now(dt.timezone.utc))
    today = now.date()
    peaks: dict[dt.date, float] = {}
    for sample in samples:
        if sample.provider != provider or window_key not in sample.windows:
            continue
        day = sample.ts.date()
        peaks[day] = max(peaks.get(day, 0.0), sample.windows[window_key])
    series: list[SeriesPoint] = []
    for offset in range(days - 1, -1, -1):
        day = today - dt.timedelta(days=offset)
        midday = dt.datetime.combine(day, dt.time(12, 0), tzinfo=dt.timezone.utc)
        series.append(SeriesPoint(ts=midday, value=peaks.get(day, 0.0)))
    return series


def burn_down_series(samples: Iterable[tuple[dt.datetime, float]], *,
                     window_minutes: int, resets_at: dt.datetime) -> BurnDown:
    """Remaining-percent points for the current window plus its ideal line.

    `samples` are (timestamp, used_percent) pairs; only those inside the
    window (between window start and reset) are kept.
    """
    resets_at = _utc(resets_at)
    window_start = resets_at - dt.timedelta(minutes=window_minutes)
    actual = [
        SeriesPoint(ts=_utc(ts), value=max(0.0, 100.0 - used))
        for ts, used in samples
        if window_start <= _utc(ts) <= resets_at
    ]
    actual.sort(key=lambda p: p.ts)
    return BurnDown(window_start=window_start, resets_at=resets_at, actual=actual)
