"""Usage history persistence and chart series for the CodexBar dashboard.

Samples are stored as JSON Lines under the XDG state dir so history
survives restarts without a database. Each line is one provider sample:

    {"ts": "2026-07-04T12:00:00+00:00", "provider": "codex",
     "windows": {"primary": 10.0, "secondary": 5.0}}
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import math
import os
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

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
        self._manage_parent_permissions = path is None or self.path == default_history_path()
        self._thread_lock = threading.RLock()
        self._next_prune_at: dt.datetime | None = None

    def _ensure_private_parent(self) -> None:
        existed = self.path.parent.exists()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not existed or self._manage_parent_permissions:
            self.path.parent.chmod(0o700)

    def _fsync_parent(self) -> None:
        dir_fd = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        with self._thread_lock:
            self._ensure_private_parent()
            lock_path = self.path.with_name(f"{self.path.name}.lock")
            fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                os.fchmod(fd, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                if self.path.exists():
                    self.path.chmod(0o600)
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    @staticmethod
    def _serialize(sample: Sample) -> str:
        return json.dumps(
            {
                "ts": sample.ts.isoformat(),
                "provider": sample.provider,
                "windows": sample.windows,
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _parse_record(data: object) -> Sample | None:
        if not isinstance(data, dict):
            return None
        raw_ts = data.get("ts")
        provider = data.get("provider")
        raw_windows = data.get("windows")
        if not isinstance(raw_ts, str) or not isinstance(provider, str):
            return None
        provider = provider.strip()
        try:
            ts = parse_iso_datetime(raw_ts)
            if ts is not None:
                ts = _utc(ts)
        except (ValueError, OverflowError):
            return None
        if ts is None or not provider or not isinstance(raw_windows, dict):
            return None
        windows: dict[str, float] = {}
        for key, raw_value in raw_windows.items():
            if not isinstance(key, str) or not key or isinstance(raw_value, bool):
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError, OverflowError):
                continue
            if math.isfinite(value) and 0.0 <= value <= 100.0:
                windows[key] = value
        if not windows:
            return None
        return Sample(ts=ts, provider=provider, windows=windows)

    def record(
        self,
        providers: Iterable[ProviderUsage],
        *,
        now: dt.datetime | None = None,
    ) -> list[Sample]:
        now = _utc(now or dt.datetime.now(dt.timezone.utc))
        samples: list[Sample] = []
        for provider in providers:
            if provider.error or not provider.windows:
                continue
            windows = {w.key: w.used_percent for w in provider.windows}
            samples.append(Sample(ts=now, provider=provider.provider, windows=windows))
        if not samples:
            return []
        payload = ("\n".join(self._serialize(sample) for sample in samples) + "\n").encode("utf-8")
        with self._locked(exclusive=True):
            created = not self.path.exists()
            fd = os.open(
                self.path,
                os.O_CREAT | os.O_APPEND | os.O_RDWR,
                0o600,
            )
            try:
                os.fchmod(fd, 0o600)
                size = os.fstat(fd).st_size
                prefix = b"\n" if size and os.pread(fd, 1, size - 1) != b"\n" else b""
                with os.fdopen(fd, "ab") as fh:
                    fd = -1
                    fh.write(prefix + payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                if created:
                    self._fsync_parent()
            finally:
                if fd >= 0:
                    os.close(fd)
        return samples

    def _load_unlocked(self) -> list[Sample]:
        if not self.path.exists():
            return []
        samples: list[Sample] = []
        with open(self.path, "rb") as fh:
            for raw_line in fh:
                try:
                    line = raw_line.decode("utf-8").strip()
                except UnicodeDecodeError:
                    continue
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except (json.JSONDecodeError, ValueError, RecursionError):
                    continue
                sample = self._parse_record(data)
                if sample is not None:
                    samples.append(sample)
        samples.sort(key=lambda sample: sample.ts)
        return samples

    def load(self) -> list[Sample]:
        if not self.path.exists():
            return []
        with self._locked(exclusive=False):
            return self._load_unlocked()

    def prune(
        self, *, days: int, now: dt.datetime | None = None
    ) -> list[Sample]:
        if days < 0:
            raise ValueError("days must be non-negative")
        now = _utc(now or dt.datetime.now(dt.timezone.utc))
        cutoff = now - dt.timedelta(days=days)
        with self._locked(exclusive=True):
            if not self.path.exists():
                return []
            samples = [sample for sample in self._load_unlocked() if sample.ts >= cutoff]
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
            )
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fd = -1
                    for sample in samples:
                        fh.write(self._serialize(sample) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp_name, self.path)
                self._fsync_parent()
            finally:
                if fd >= 0:
                    os.close(fd)
                try:
                    os.unlink(tmp_name)
                except FileNotFoundError:
                    pass
        return samples

    def prune_if_due(
        self,
        *,
        days: int,
        now: dt.datetime | None = None,
        interval: dt.timedelta = dt.timedelta(days=1),
    ) -> list[Sample] | None:
        now = _utc(now or dt.datetime.now(dt.timezone.utc))
        with self._thread_lock:
            if self._next_prune_at is not None and now < self._next_prune_at:
                return None
            samples = self.prune(days=days, now=now)
            self._next_prune_at = now + interval
            return samples


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
