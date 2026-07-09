import datetime as dt
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from codexbar_kde.history import (
    HistoryStore,
    burn_down_series,
    daily_peaks,
)
from codexbar_kde.model import normalize_payload


def _providers(percent_primary: float, percent_secondary: float = 0.0):
    return normalize_payload(
        {
            "provider": "codex",
            "usage": {
                "primary": {"usedPercent": percent_primary, "windowMinutes": 300},
                "secondary": {"usedPercent": percent_secondary, "windowMinutes": 10080},
            },
        }
    )


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

    def test_record_separates_new_sample_from_torn_final_line(self):
        store = HistoryStore(self.path)
        self.path.write_text('{"incomplete": true')
        now = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)

        store.record(_providers(30), now=now)

        samples = store.load()
        self.assertEqual([sample.windows["primary"] for sample in samples], [30.0])

    def test_load_skips_invalid_utf8_lines(self):
        store = HistoryStore(self.path)
        now = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
        store.record(_providers(10), now=now)
        with self.path.open("ab") as fh:
            fh.write(
                b'{"provider":"cod\xffex","windows":{"primary":20},"ts":"2026-07-04T12:30:00+00:00"}\n'
            )
        store.record(_providers(30), now=now + dt.timedelta(hours=1))

        samples = store.load()

        self.assertEqual(
            [sample.windows["primary"] for sample in samples], [10.0, 30.0]
        )

    def test_load_skips_timestamps_that_overflow_utc_conversion(self):
        valid = {
            "ts": "2026-07-04T12:00:00+00:00",
            "provider": "codex",
            "windows": {"primary": 25},
        }
        overflowing = {
            "ts": "0001-01-01T00:00:00+23:59",
            "provider": "codex",
            "windows": {"primary": 50},
        }
        self.path.write_text(
            "\n".join(json.dumps(item) for item in [overflowing, valid]) + "\n"
        )

        samples = HistoryStore(self.path).load()

        self.assertEqual([sample.windows["primary"] for sample in samples], [25.0])

    def test_load_skips_json_numbers_rejected_by_python(self):
        valid = json.dumps(
            {
                "ts": "2026-07-04T12:00:00+00:00",
                "provider": "codex",
                "windows": {"primary": 25},
            }
        )
        oversized_integer = (
            '{"ts":"2026-07-04T12:00:00+00:00","provider":"codex",'
            '"windows":{"primary":' + ("9" * 5_000) + "}}"
        )
        self.path.write_text(f"{oversized_integer}\n{valid}\n")

        samples = HistoryStore(self.path).load()

        self.assertEqual([sample.windows["primary"] for sample in samples], [25.0])

    def test_load_skips_excessively_nested_json(self):
        valid = json.dumps(
            {
                "ts": "2026-07-04T12:00:00+00:00",
                "provider": "codex",
                "windows": {"primary": 25},
            }
        )
        depth = 100_000
        nested = ("[" * depth) + "0" + ("]" * depth)
        self.path.write_text(f"{nested}\n{valid}\n")

        samples = HistoryStore(self.path).load()

        self.assertEqual([sample.windows["primary"] for sample in samples], [25.0])

    def test_load_rejects_non_object_and_malformed_nested_records(self):
        valid = {
            "ts": "2026-07-04T12:00:00+00:00",
            "provider": "codex",
            "windows": {"primary": 25},
        }
        malformed = [
            None,
            [],
            42,
            {"ts": valid["ts"], "provider": "codex", "windows": None},
            {"ts": valid["ts"], "provider": [], "windows": {"primary": 1}},
            {"ts": valid["ts"], "provider": "codex", "windows": {"primary": True}},
            {"ts": valid["ts"], "provider": "codex", "windows": {"primary": "NaN"}},
            {"ts": valid["ts"], "provider": "codex", "windows": {"primary": 101}},
        ]
        self.path.write_text(
            "\n".join(json.dumps(item) for item in [*malformed, valid]) + "\n"
        )

        samples = HistoryStore(self.path).load()

        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0].provider, "codex")
        self.assertEqual(samples[0].windows, {"primary": 25.0})

    def test_history_files_are_private(self):
        nested_path = Path(self.tmp.name) / "private" / "history.jsonl"
        store = HistoryStore(nested_path)

        store.record(_providers(10))

        self.assertEqual(os.stat(nested_path.parent).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(nested_path).st_mode & 0o777, 0o600)

    def test_existing_history_file_permissions_are_tightened_on_load(self):
        self.path.write_text(
            '{"ts":"2026-07-04T12:00:00+00:00","provider":"codex",'
            '"windows":{"primary":10}}\n'
        )
        self.path.chmod(0o644)

        HistoryStore(self.path).load()

        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_existing_custom_parent_permissions_are_preserved(self):
        shared = Path(self.tmp.name) / "shared"
        shared.mkdir(mode=0o755)
        shared.chmod(0o755)
        path = shared / "history.jsonl"

        HistoryStore(path).record(_providers(10))

        self.assertEqual(os.stat(shared).st_mode & 0o777, 0o755)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_prune_drops_samples_older_than_days(self):
        store = HistoryStore(self.path)
        old = dt.datetime(2026, 6, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
        recent = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
        store.record(_providers(10), now=old)
        store.record(_providers(20), now=recent)

        store.prune(days=30, now=recent)

        samples = store.load()
        self.assertEqual([s.windows["primary"] for s in samples], [20.0])

    def test_record_syncs_new_file_and_parent_directory(self):
        store = HistoryStore(self.path)
        now = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)

        with patch("codexbar_kde.history.os.fsync", wraps=os.fsync) as fsync:
            store.record(_providers(10), now=now)

        self.assertEqual(fsync.call_count, 2)

    def test_prune_syncs_replacement_file_and_parent_directory(self):
        store = HistoryStore(self.path)
        now = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
        store.record(_providers(10), now=now)

        with patch("codexbar_kde.history.os.fsync", wraps=os.fsync) as fsync:
            store.prune(days=60, now=now)

        self.assertEqual(fsync.call_count, 2)

    def test_concurrent_record_and_prune_preserve_all_recent_samples(self):
        now = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
        errors = []
        barrier = threading.Barrier(5)

        def writer(index):
            try:
                store = HistoryStore(self.path)
                barrier.wait()
                for offset in range(20):
                    store.record(
                        _providers(index * 20 + offset),
                        now=now + dt.timedelta(seconds=index * 20 + offset),
                    )
            except Exception as exc:
                errors.append(exc)

        def pruner():
            try:
                store = HistoryStore(self.path)
                barrier.wait()
                for _ in range(10):
                    store.prune(days=365, now=now)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(index,)) for index in range(4)]
        threads.append(threading.Thread(target=pruner))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)

        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(HistoryStore(self.path).load()), 80)

    def test_prune_if_due_runs_at_most_once_per_interval(self):
        store = HistoryStore(self.path)
        now = dt.datetime(2026, 7, 4, 12, 0, 0, tzinfo=dt.timezone.utc)
        store.record(_providers(10), now=now)

        first = store.prune_if_due(days=60, now=now)
        store.record(_providers(20), now=now + dt.timedelta(minutes=5))
        second = store.prune_if_due(days=60, now=now + dt.timedelta(hours=1))

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(store.load()), 2)

    def test_daily_peaks_returns_max_percent_per_day(self):
        store = HistoryStore(self.path)
        day1 = dt.datetime(2026, 7, 3, 9, 0, 0, tzinfo=dt.timezone.utc)
        store.record(_providers(10), now=day1)
        store.record(_providers(40), now=day1 + dt.timedelta(hours=4))
        store.record(_providers(25), now=day1 + dt.timedelta(days=1))

        samples = store.load()
        series = daily_peaks(
            samples,
            provider="codex",
            window_key="primary",
            days=7,
            now=dt.datetime(2026, 7, 4, 23, 0, 0, tzinfo=dt.timezone.utc),
        )

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

        result = burn_down_series(
            samples, window_minutes=window_minutes, resets_at=resets_at
        )

        self.assertEqual(
            result.window_start, resets_at - dt.timedelta(minutes=window_minutes)
        )
        # actual remaining percent mirrors used percent
        self.assertEqual([round(p.value, 1) for p in result.actual], [60.0, 45.0, 30.0])
        # ideal starts at 100 at window start and hits 0 at reset
        self.assertAlmostEqual(result.ideal_remaining_at(result.window_start), 100.0)
        self.assertAlmostEqual(result.ideal_remaining_at(resets_at), 0.0)


if __name__ == "__main__":
    unittest.main()
