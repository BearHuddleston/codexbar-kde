import datetime as dt
import tempfile
import unittest
from pathlib import Path

from codexbar_kde.history import (
    HistoryStore,
    burn_down_series,
    daily_peaks,
)
from codexbar_kde.model import normalize_payload


def _providers(percent_primary: float, percent_secondary: float = 0.0):
    return normalize_payload({
        "provider": "codex",
        "usage": {
            "primary": {"usedPercent": percent_primary, "windowMinutes": 300},
            "secondary": {"usedPercent": percent_secondary, "windowMinutes": 10080},
        },
    })


class HistoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "history.jsonl"
        self.addCleanup(self.tmp.cleanup)

    def test_record_appends_samples_and_load_returns_them(self):
        store = HistoryStore(self.path)
        now = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)

        store.record(_providers(10, 5), now=now)
        store.record(_providers(20, 6), now=now + dt.timedelta(minutes=30))

        samples = store.load()
        self.assertEqual(len(samples), 2)
        self.assertEqual(samples[0].provider, "codex")
        self.assertEqual(samples[0].windows["primary"], 10.0)
        self.assertEqual(samples[1].windows["primary"], 20.0)
        self.assertEqual(samples[1].windows["secondary"], 6.0)

    def test_load_survives_corrupt_lines(self):
        store = HistoryStore(self.path)
        now = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
        store.record(_providers(10), now=now)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        store.record(_providers(30), now=now + dt.timedelta(hours=1))

        samples = store.load()

        self.assertEqual([s.windows["primary"] for s in samples], [10.0, 30.0])

    def test_prune_drops_samples_older_than_days(self):
        store = HistoryStore(self.path)
        old = dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
        recent = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
        store.record(_providers(10), now=old)
        store.record(_providers(20), now=recent)

        store.prune(days=30, now=recent)

        samples = store.load()
        self.assertEqual([s.windows["primary"] for s in samples], [20.0])

    def test_daily_peaks_returns_max_percent_per_day(self):
        store = HistoryStore(self.path)
        day1 = dt.datetime(2026, 7, 3, 9, 0, 0, tzinfo=dt.timezone.utc)
        store.record(_providers(10), now=day1)
        store.record(_providers(40), now=day1 + dt.timedelta(hours=4))
        store.record(_providers(25), now=day1 + dt.timedelta(days=1))

        samples = store.load()
        series = daily_peaks(samples, provider="codex", window_key="primary", days=7,
                             now=dt.datetime(2026, 7, 4, 23, 0, 0, tzinfo=dt.timezone.utc))

        self.assertEqual(len(series), 7)
        self.assertEqual(series[-2].value, 40.0)
        self.assertEqual(series[-1].value, 25.0)
        self.assertEqual(series[0].value, 0.0)


class BurnDownTests(unittest.TestCase):
    def test_burn_down_series_returns_ideal_and_actual_remaining(self):
        base = dt.datetime(2026, 7, 4, 10, 0, 0, tzinfo=dt.timezone.utc)
        resets_at = base + dt.timedelta(hours=3)
        window_minutes = 300  # window started 2h before base
        samples = [
            (base, 40.0),
            (base + dt.timedelta(hours=1), 55.0),
            (base + dt.timedelta(hours=2), 70.0),
        ]

        result = burn_down_series(samples, window_minutes=window_minutes, resets_at=resets_at)

        self.assertEqual(result.window_start, resets_at - dt.timedelta(minutes=window_minutes))
        # actual remaining percent mirrors used percent
        self.assertEqual([round(p.value, 1) for p in result.actual], [60.0, 45.0, 30.0])
        # ideal starts at 100 at window start and hits 0 at reset
        self.assertAlmostEqual(result.ideal_remaining_at(result.window_start), 100.0)
        self.assertAlmostEqual(result.ideal_remaining_at(resets_at), 0.0)


if __name__ == "__main__":
    unittest.main()
