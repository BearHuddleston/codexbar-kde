import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QCoreApplication, QEvent
from PyQt6.QtWidgets import QApplication, QMessageBox

from codexbar_kde.app import DashboardWindow, RedeemWorker, UsageWorker
from codexbar_kde.history import HistoryStore
from codexbar_kde.model import normalize_payload


def _app():
    return QApplication.instance() or QApplication(["codexbar-kde-tests"])


def _wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        QApplication.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _process_alive(pid):
    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.exists():
        return False
    fields = stat_path.read_text().split()
    return len(fields) > 2 and fields[2] != "Z"


PAYLOAD = [
    {
        "provider": "codex",
        "source": "oauth",
        "version": "0.142.5",
        "usage": {
            "accountEmail": "person@example.com",
            "loginMethod": "pro",
            "primary": {"usedPercent": 36, "windowMinutes": 300, "resetDescription": "tomorrow, 1:03 AM"},
            "secondary": {"usedPercent": 7, "windowMinutes": 10080},
            "codexResetCredits": {
                "availableCount": 2,
                "credits": [
                    {"id": "RateLimitResetCredit_far", "status": "available",
                     "title": "Full reset (Weekly + 5 hr)", "expires_at": "2026-08-20T01:00:00Z"},
                    {"id": "RateLimitResetCredit_soon", "status": "available",
                     "title": "Full reset (Weekly + 5 hr)", "expires_at": "2026-07-12T02:39:09Z"},
                ],
            },
            "updatedAt": "2026-07-04T04:24:16Z",
        },
        "pace": {"primary": {"deltaPercent": -30, "expectedUsedPercent": 66, "willLastToReset": True}},
        "credits": {"remaining": 0},
    },
    {"provider": "claude", "source": "oauth", "usage": {"primary": {"usedPercent": 12}}},
    {"provider": "gemini", "source": "auto", "error": {"message": "rate limited"}},
]


class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qapp = _app()

    def setUp(self):
        self.history_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.history_tmp.cleanup)
        self.history = HistoryStore(Path(self.history_tmp.name) / "history.jsonl")

    def _window(self):
        window = DashboardWindow(
            codexbar_bin="/usr/bin/codexbar",
            refresh_seconds=3600,
            history_store=self.history,
        )
        providers = normalize_payload(PAYLOAD)
        window.set_providers(providers, raw_payload=PAYLOAD)
        return window

    def test_window_offers_multiple_statistic_views(self):
        window = self._window()

        names = window.view_names()

        self.assertIn("Overview", names)
        self.assertIn("History", names)
        self.assertIn("Burn-down", names)
        self.assertIn("Details", names)

    def test_switching_views_changes_current_view(self):
        window = self._window()

        window.show_view("History")
        self.assertEqual(window.current_view_name(), "History")
        window.show_view("Overview")
        self.assertEqual(window.current_view_name(), "Overview")

    def test_overview_lists_all_providers_including_errors(self):
        window = self._window()

        text = window.overview_summary_text()

        self.assertIn("Codex", text)
        self.assertIn("Claude", text)
        self.assertIn("Gemini", text)
        self.assertIn("rate limited", text)

    def test_details_view_shows_compact_provider_fields(self):
        window = self._window()

        window.show_view("Details")
        text = window.details_text()

        self.assertIn("Codex (oauth, v0.142.5)", text)
        self.assertIn("5h/session", text)
        self.assertIn("Plan: pro", text)

    def test_details_privacy_mode_masks_identity_and_can_be_disabled(self):
        window = self._window()

        self.assertTrue(window.view_details.privacy_toggle.isChecked())
        self.assertNotIn("person@example.com", window.details_text())
        self.assertIn("Account: [REDACTED]", window.details_text())

        window.set_privacy_mode(False)

        self.assertFalse(window.view_details.privacy_toggle.isChecked())
        self.assertIn("person@example.com", window.details_text())

    def test_refresh_timer_interval_stays_within_qt_integer_range(self):
        maximum = DashboardWindow(
            refresh_seconds=2_147_484,
            history_store=self.history,
        )
        minimum = DashboardWindow(
            refresh_seconds=1,
            history_store=self.history,
        )
        self.addCleanup(maximum.close)
        self.addCleanup(minimum.close)

        self.assertEqual(maximum.refresh_seconds, 2_147_483)
        self.assertEqual(maximum.timer.interval(), 2_147_483_000)
        self.assertEqual(minimum.timer.interval(), 30_000)

    def test_overview_shows_reset_credit_panel_with_redeem_button(self):
        window = self._window()

        panel = window.view_overview.reset_panel
        self.assertTrue(panel.isVisibleTo(window.view_overview))
        self.assertEqual(panel.credit_count(), 2)
        # button targets the credit that expires first
        self.assertEqual(panel.target_credit_id(), "RateLimitResetCredit_soon")
        label = panel.redeem_button.text()
        self.assertIn("Redeem", label)
        self.assertIn("expires", label.lower())

    def test_reset_panel_hides_when_all_available_credits_are_expired(self):
        window = self._window()
        panel = window.view_overview.reset_panel

        panel.set_credits([
            {
                "id": "expired",
                "status": "available",
                "expires_at": "2020-01-01T00:00:00Z",
            }
        ])

        self.assertFalse(panel.isVisibleTo(window.view_overview))
        self.assertEqual(panel.credit_count(), 0)
        self.assertEqual(panel.target_credit_id(), "")
        self.assertFalse(panel.redeem_button.isEnabled())

    def test_reset_panel_hidden_without_codex_credits(self):
        window = DashboardWindow(
            codexbar_bin="/usr/bin/codexbar",
            refresh_seconds=3600,
            history_store=self.history,
        )
        payload = [{"provider": "claude", "source": "oauth", "usage": {"primary": {"usedPercent": 12}}}]
        window.set_providers(normalize_payload(payload), raw_payload=payload)

        self.assertFalse(window.view_overview.reset_panel.isVisibleTo(window.view_overview))

    def test_usage_success_is_persisted_before_result_signal(self):
        events = []
        worker = UsageWorker(
            "/usr/bin/codexbar",
            history_store=self.history,
        )
        worker.finished_with_result.connect(
            lambda *_: events.append("result")
        )

        def record(*args, **kwargs):
            events.append("record")
            return []

        with (
            patch("codexbar_kde.app.load_usage_payload_from_command", return_value=PAYLOAD),
            patch.object(self.history, "record", side_effect=record),
        ):
            worker.run()

        self.assertEqual(events[:2], ["record", "result"])

    def test_refresh_updates_cached_history_off_the_gui_thread(self):
        window = self._window()
        main_thread = threading.get_ident()
        record_threads = []
        original_record = self.history.record

        def recording(*args, **kwargs):
            record_threads.append(threading.get_ident())
            return original_record(*args, **kwargs)

        with (
            patch("codexbar_kde.app.load_usage_payload_from_command", return_value=PAYLOAD),
            patch.object(self.history, "record", side_effect=recording),
            patch.object(self.history, "prune", wraps=self.history.prune) as prune,
            patch.object(self.history, "load", wraps=self.history.load) as load,
        ):
            window.refresh_now()
            self.assertTrue(_wait_until(lambda: window.worker is None))
            first_count = len(window._history_samples)
            window.refresh_now()
            self.assertTrue(_wait_until(lambda: window.worker is None))

        self.assertGreater(first_count, 0)
        self.assertEqual(len(window._history_samples), first_count * 2)
        self.assertEqual(prune.call_count, 1)
        self.assertEqual(load.call_count, 0)
        self.assertTrue(record_threads)
        self.assertTrue(all(thread_id != main_thread for thread_id in record_threads))

    def test_finished_usage_worker_is_released(self):
        window = self._window()
        with patch("codexbar_kde.app.load_usage_payload_from_command", return_value=PAYLOAD):
            window.refresh_now()
            self.assertTrue(_wait_until(lambda: window.worker is None))

        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()
        self.assertEqual(window.findChildren(UsageWorker), [])

    def test_finished_redeem_worker_is_released(self):
        window = self._window()
        result = {
            "code": "reset",
            "windows_reset": 1,
            "credit": {"id": "RateLimitResetCredit_soon", "status": "redeemed"},
        }
        with (
            patch.object(
                QMessageBox,
                "question",
                return_value=QMessageBox.StandardButton.Yes,
            ),
            patch("codexbar_kde.app.load_codex_auth", return_value=("tok", "acct")),
            patch("codexbar_kde.app.redeem_reset_credit", return_value=result),
            patch.object(window, "refresh_now"),
        ):
            window._redeem_reset_credit("RateLimitResetCredit_soon")
            self.assertTrue(_wait_until(lambda: window.redeem_worker is None))

        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()
        self.assertEqual(window.findChildren(RedeemWorker), [])

    def test_shutdown_waits_for_active_redeem_worker(self):
        window = self._window()
        result = {
            "code": "reset",
            "windows_reset": 1,
            "credit": {"id": "credit", "status": "redeemed"},
        }

        def slow_redeem(*_args):
            time.sleep(0.1)
            return result

        with (
            patch("codexbar_kde.app.load_codex_auth", return_value=("tok", "acct")),
            patch("codexbar_kde.app.redeem_reset_credit", side_effect=slow_redeem),
        ):
            worker = RedeemWorker("credit", window)
            window.redeem_worker = worker
            worker.start()
            try:
                window.shutdown_workers()
            finally:
                worker.wait(2_000)

        self.assertFalse(worker.isRunning())

    def test_usage_worker_cancel_terminates_spawned_process_group(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pid_path = root / "pids"
            script = root / "codexbar"
            script.write_text(
                "#!/usr/bin/python3\n"
                "import os, pathlib, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])\n"
                f"pathlib.Path({str(pid_path)!r}).write_text(f'{{os.getpid()}} {{child.pid}}')\n"
                "time.sleep(30)\n"
            )
            script.chmod(0o755)
            worker = UsageWorker(str(script))
            pids = []
            worker.start()
            try:
                self.assertTrue(_wait_until(pid_path.exists))
                pids = [int(value) for value in pid_path.read_text().split()]
                worker.cancel()
                self.assertTrue(worker.wait(3_000))
                self.assertTrue(
                    _wait_until(lambda: not any(_process_alive(pid) for pid in pids))
                )
            finally:
                if worker.isRunning():
                    worker.terminate()
                    worker.wait(2_000)
                for pid in pids:
                    if _process_alive(pid):
                        os.kill(pid, 9)

    def test_application_quit_with_active_worker_exits_cleanly(self):
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "codexbar"
            script.write_text(
                "#!/usr/bin/python3\n"
                "import subprocess, time\n"
                "subprocess.Popen(['sleep', '30'])\n"
                "time.sleep(30)\n"
            )
            script.chmod(0o755)
            program = f"""
import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication
from codexbar_kde.app import DashboardWindow
app = QApplication(['lifecycle-test'])
window = DashboardWindow(codexbar_bin={str(script)!r}, refresh_seconds=3600)
app.aboutToQuit.connect(window.shutdown_workers)
window.refresh_now()
QTimer.singleShot(200, app.quit)
raise SystemExit(app.exec())
"""
            env = os.environ.copy()
            env["PYTHONPATH"] = str(Path(__file__).parents[1] / "src")
            completed = subprocess.run(
                [sys.executable, "-c", program],
                env=env,
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("QThread: Destroyed", completed.stderr)


if __name__ == "__main__":
    unittest.main()
