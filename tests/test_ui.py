import datetime as dt
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
from PyQt6.QtWidgets import QApplication, QLabel, QMessageBox

from codexbar_kde.app import (
    _CommandCancelled,
    _run_codexbar_command,
    DashboardWindow,
    RedeemWorker,
    UsageWorker,
)
from codexbar_kde.history import HistoryStore, Sample
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
    try:
        fields = stat_path.read_text().split()
    except OSError:
        return False
    return len(fields) > 2 and fields[2] != "Z"


PAYLOAD = [
    {
        "provider": "codex",
        "source": "oauth",
        "version": "0.142.5",
        "usage": {
            "accountEmail": "person@example.com",
            "loginMethod": "pro",
            "primary": {
                "usedPercent": 36,
                "windowMinutes": 300,
                "resetDescription": "tomorrow, 1:03 AM",
            },
            "secondary": {"usedPercent": 7, "windowMinutes": 10080},
            "codexResetCredits": {
                "availableCount": 2,
                "credits": [
                    {
                        "id": "RateLimitResetCredit_far",
                        "status": "available",
                        "title": "Full reset (Weekly + 5 hr)",
                        "expires_at": "2099-08-20T01:00:00Z",
                    },
                    {
                        "id": "RateLimitResetCredit_soon",
                        "status": "available",
                        "title": "Full reset (Weekly + 5 hr)",
                        "expires_at": "2099-07-12T02:39:09Z",
                    },
                ],
            },
            "updatedAt": "2026-07-04T04:24:16Z",
        },
        "pace": {
            "primary": {
                "deltaPercent": -30,
                "expectedUsedPercent": 66,
                "willLastToReset": True,
            }
        },
        "credits": {"remaining": 0},
    },
    {
        "provider": "claude",
        "source": "oauth",
        "usage": {"primary": {"usedPercent": 12}},
    },
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

    def test_sidebar_toggle_collapses_to_glyph_rail(self):
        window = self._window()
        window.show()
        self.assertTrue(window._sidebar_expanded)
        self.assertEqual(window.sidebar.width(), window.SIDEBAR_WIDTH)
        self.assertEqual(window.nav_buttons["Overview"].text(), "Overview")
        # the toggle lives on the sidebar itself, not in the content header
        self.assertIs(window.nav_toggle.parentWidget(), window.sidebar)

        window.toggle_sidebar()
        # collapsed: still visible, narrow, icon-only buttons + name tooltips
        self.assertTrue(window.sidebar.isVisible())
        self.assertEqual(window.sidebar.width(), window.RAIL_WIDTH)
        for name, button in window.nav_buttons.items():
            self.assertEqual(button.text(), "")
            self.assertFalse(button.icon().isNull())
            self.assertEqual(button.toolTip(), name)
        self.assertEqual(window.refresh_button.text(), "\u21bb")
        # status reduces to its dot, full text moves to tooltip
        self.assertEqual(window.status_label.text(), "●")
        self.assertEqual(window.status_label.toolTip(), window._status_text)

        window.toggle_sidebar()
        self.assertEqual(window.sidebar.width(), window.SIDEBAR_WIDTH)
        self.assertEqual(window.nav_buttons["History"].text(), "History")
        # icons stay on the buttons in both states (shared icon column)
        self.assertFalse(window.nav_buttons["History"].icon().isNull())
        self.assertEqual(window.refresh_button.text(), "Refresh")
        self.assertNotEqual(window.status_label.text(), "●")
        window.close()

    def test_rail_nav_buttons_align_with_expanded_positions(self):
        window = self._window()
        window.show()
        QCoreApplication.processEvents()
        expanded = {n: b.geometry().top() for n, b in window.nav_buttons.items()}
        window.toggle_sidebar()
        QCoreApplication.processEvents()
        for name, button in window.nav_buttons.items():
            self.assertEqual(
                button.geometry().top(),
                expanded[name],
                f"{name} shifted vertically on collapse",
            )
        window.toggle_sidebar()  # restore persisted state for other tests
        window.close()

    def test_expanding_sidebar_raises_window_minimum_to_avoid_clipping(self):
        window = self._window()
        window.show()
        QCoreApplication.processEvents()
        window.set_sidebar_visible(False)
        QCoreApplication.processEvents()
        min_rail = window.minimumWidth()
        window.resize(min_rail, 700)
        window.set_sidebar_visible(True)
        QCoreApplication.processEvents()
        # minimum tracks the layout: expanded sidebar needs a wider floor,
        # and the window is pushed up to it so content can't be clipped
        self.assertGreater(window.minimumWidth(), min_rail)
        self.assertGreaterEqual(window.width(), window.minimumWidth())
        window.close()

    def test_sidebar_visibility_persists_via_settings(self):
        window = self._window()
        window.set_sidebar_visible(False)
        self.assertFalse(window._settings.value("sidebar_visible", True, type=bool))
        window.set_sidebar_visible(True)
        self.assertTrue(window._settings.value("sidebar_visible", True, type=bool))
        window.close()

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

    def test_overview_redacts_credentials_and_respects_identity_privacy(self):
        payload = [
            {
                "provider": "codex",
                "source": "token=SOURCE_SECRET_123456 source@example.com",
                "usage": {
                    "primary": {
                        "label": "token=LABEL_SECRET_123456 selector@example.com",
                        "usedPercent": 12,
                        "resetDescription": (
                            "token=RESET_SECRET_123456 reset@example.com"
                        ),
                    },
                    "codexResetCredits": {
                        "availableCount": 1,
                        "credits": [
                            {
                                "id": "RateLimitResetCredit_private",
                                "status": "available",
                                "title": (
                                    "token=CREDIT_SECRET_123456 credit@example.com"
                                ),
                                "expires_at": "2026-08-01T00:00:00Z",
                            }
                        ],
                    },
                },
            }
        ]
        window = DashboardWindow(refresh_seconds=3600, history_store=self.history)
        self.addCleanup(window.close)
        window.set_providers(
            normalize_payload(payload),
            error="token=ERROR_SECRET_123456 error@example.com",
            raw_payload=payload,
        )

        def visible_text():
            overview = "\n".join(
                label.text() for label in window.view_overview.findChildren(QLabel)
            )
            selectors = "\n".join(
                (
                    window.view_history.selector.itemText(0),
                    window.view_burndown.selector.itemText(0),
                )
            )
            return "\n".join((overview, selectors, window.error_label.text()))

        private_text = visible_text()
        for secret in (
            "SOURCE_SECRET_123456",
            "LABEL_SECRET_123456",
            "RESET_SECRET_123456",
            "CREDIT_SECRET_123456",
            "ERROR_SECRET_123456",
        ):
            self.assertNotIn(secret, private_text)
        for email in (
            "source@example.com",
            "selector@example.com",
            "reset@example.com",
            "credit@example.com",
            "error@example.com",
        ):
            self.assertNotIn(email, private_text)

        panel = window.view_overview.reset_panel
        panel.set_busy(True, "Redeeming…")
        self.assertFalse(panel.redeem_button.isEnabled())
        self.assertFalse(panel.status_line.isHidden())

        window.set_privacy_mode(False)

        self.assertFalse(panel.redeem_button.isEnabled())
        self.assertFalse(panel.status_line.isHidden())
        self.assertEqual(panel.status_line.text(), "Redeeming…")
        panel.show_result(
            "token=RESULT_SECRET_123456 result@example.com",
            ok=False,
        )
        revealed_text = visible_text()
        for email in (
            "source@example.com",
            "selector@example.com",
            "reset@example.com",
            "credit@example.com",
            "error@example.com",
            "result@example.com",
        ):
            self.assertIn(email, revealed_text)
        for secret in (
            "SOURCE_SECRET_123456",
            "LABEL_SECRET_123456",
            "RESET_SECRET_123456",
            "CREDIT_SECRET_123456",
            "ERROR_SECRET_123456",
            "RESULT_SECRET_123456",
        ):
            self.assertNotIn(secret, revealed_text)

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

    def test_reset_panel_groups_duplicate_credit_titles(self):
        window = self._window()
        panel = window.view_overview.reset_panel

        panel.set_credits(
            [
                {
                    "id": f"credit_{i}",
                    "status": "available",
                    "title": "Full reset (Weekly + 5 hr)",
                    "expires_at": f"2099-07-{12 + i * 7:02d}T01:00:00Z",
                }
                for i in range(3)
            ]
        )
        text = panel.credit_lines.text()
        lines = text.splitlines()
        # one line, bare duration run — count lives in the header, the
        # "expires in" phrase lives on the button, so neither repeats here
        self.assertEqual(len(lines), 1)
        self.assertIn("Full reset (Weekly + 5 hr) — ", lines[0])
        self.assertNotIn("×", text)
        self.assertNotIn("expires in", text)
        self.assertEqual(lines[0].count("·"), 2)
        # durations are NBSP-joined so a wrap moves the whole run to the
        # next line instead of orphaning the last duration
        run = lines[0].split("— ")[1]
        self.assertNotIn(" ", run)
        self.assertIn("\u00a0·\u00a0", run)
        # button still carries the phrase + target expiry exactly once
        self.assertIn("expires in", panel.redeem_button.text())
        # ungrouped single credit keeps the plain form
        panel.set_credits(
            [
                {
                    "id": "solo",
                    "status": "available",
                    "title": "Full reset (Weekly + 5 hr)",
                    "expires_at": "2099-07-12T01:00:00Z",
                }
            ]
        )
        self.assertNotIn("×", panel.credit_lines.text())
        # credits without expiry fall back to ×N so the size stays visible
        panel.set_credits(
            [
                {"id": "a", "status": "available", "title": "Full reset"},
                {"id": "b", "status": "available", "title": "Full reset"},
            ]
        )
        self.assertIn("×2", panel.credit_lines.text())
        window.close()

    def test_reset_panel_hides_when_all_available_credits_are_expired(self):
        window = self._window()
        panel = window.view_overview.reset_panel

        panel.set_credits(
            [
                {
                    "id": "expired",
                    "status": "available",
                    "expires_at": "2020-01-01T00:00:00Z",
                }
            ]
        )

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
        payload = [
            {
                "provider": "claude",
                "source": "oauth",
                "usage": {"primary": {"usedPercent": 12}},
            }
        ]
        window.set_providers(normalize_payload(payload), raw_payload=payload)

        self.assertFalse(
            window.view_overview.reset_panel.isVisibleTo(window.view_overview)
        )

    def test_reset_panel_shown_only_while_codex_is_selected(self):
        window = self._window()
        overview = window.view_overview

        self.assertEqual(overview.selected_provider(), "codex")
        self.assertTrue(overview.reset_panel.isVisibleTo(overview))

        overview._select("claude")
        self.assertFalse(overview.reset_panel.isVisibleTo(overview))

        overview._select("codex")
        self.assertTrue(overview.reset_panel.isVisibleTo(overview))

    def test_switching_providers_leaves_no_orphaned_stage_widgets(self):
        window = self._window()
        overview = window.view_overview

        def stage_labels():
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            QApplication.processEvents()
            return [
                label.text()
                for label in overview._stage_host.findChildren(QLabel)
                if label.text()
            ]

        overview._select("claude")
        labels = stage_labels()
        self.assertNotIn(
            "Codex", labels, f"codex ghosts after switching away: {labels}"
        )
        # exactly one stage header and one primary window row — no stacked ghosts
        self.assertEqual(labels.count("Claude"), 1)
        self.assertEqual(labels.count("5h/session"), 1)

        overview._select("codex")
        labels = stage_labels()
        self.assertNotIn("Claude", labels)
        self.assertEqual(labels.count("5h/session"), 1)

        for _ in range(2):
            overview._select("claude")
            overview._select("codex")
        self.assertEqual(len(stage_labels()), len(labels))

    def test_usage_success_is_persisted_before_result_signal(self):
        events = []
        worker = UsageWorker(
            "/usr/bin/codexbar",
            history_store=self.history,
        )
        worker.finished_with_result.connect(lambda *_: events.append("result"))

        def record(*args, **kwargs):
            events.append("record")
            return []

        with (
            patch(
                "codexbar_kde.app.load_usage_payload_from_command", return_value=PAYLOAD
            ),
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
            patch(
                "codexbar_kde.app.load_usage_payload_from_command", return_value=PAYLOAD
            ),
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
        with patch(
            "codexbar_kde.app.load_usage_payload_from_command", return_value=PAYLOAD
        ):
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

    def test_stale_worker_cleanup_cannot_clear_a_new_worker(self):
        window = self._window()
        old_usage = UsageWorker("/old", window)
        new_usage = UsageWorker("/new", window)
        window.worker = new_usage

        window._usage_worker_stopped(old_usage)

        self.assertIs(window.worker, new_usage)

        old_redeem = RedeemWorker("old", window)
        new_redeem = RedeemWorker("new", window)
        window.redeem_worker = new_redeem

        window._redeem_worker_stopped(old_redeem)

        self.assertIs(window.redeem_worker, new_redeem)

    def test_finished_worker_reference_remains_reserved_until_cleanup(self):
        window = self._window()
        old_worker = UsageWorker("/old", window)
        window.worker = old_worker

        with patch.object(UsageWorker, "start") as start:
            window.refresh_now()

        start.assert_not_called()
        self.assertIs(window.worker, old_worker)

    def test_preloaded_history_samples_populate_charts_without_refresh(self):
        sample = Sample(
            ts=dt.datetime.now(dt.timezone.utc),
            provider="codex",
            windows={"primary": 36.0},
        )
        window = DashboardWindow(
            refresh_seconds=3600,
            history_store=self.history,
            history_samples=[sample],
        )
        window.set_providers(normalize_payload(PAYLOAD), raw_payload=PAYLOAD)

        self.assertEqual(window._history_samples, [sample])
        self.assertNotEqual(window.view_history.stats._cells["today"].text(), "—")

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

    def test_timeout_kills_descendant_after_process_group_leader_exits(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pid_path = root / "child.pid"
            script = root / "codexbar"
            script.write_text(
                "#!/usr/bin/python3\n"
                "import pathlib, signal, subprocess, sys\n"
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])\n"
                f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid))\n"
            )
            script.chmod(0o755)
            child_pid = None
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    _run_codexbar_command([str(script)], timeout=0.2)
                child_pid = int(pid_path.read_text())
                self.assertTrue(_wait_until(lambda: not _process_alive(child_pid)))
            finally:
                if child_pid is not None and _process_alive(child_pid):
                    os.kill(child_pid, 9)

    def test_cancel_kills_descendant_after_process_group_leader_exits(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pid_path = root / "child.pid"
            script = root / "codexbar"
            script.write_text(
                "#!/usr/bin/python3\n"
                "import pathlib, signal, subprocess, sys\n"
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'])\n"
                f"pathlib.Path({str(pid_path)!r}).write_text(str(child.pid))\n"
            )
            script.chmod(0o755)
            child_pid = None
            try:
                with self.assertRaises(_CommandCancelled):
                    _run_codexbar_command(
                        [str(script)],
                        timeout=30,
                        cancel_requested=pid_path.exists,
                    )
                child_pid = int(pid_path.read_text())
                self.assertTrue(_wait_until(lambda: not _process_alive(child_pid)))
            finally:
                if child_pid is not None and _process_alive(child_pid):
                    os.kill(child_pid, 9)

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
from PyQt6.QtCore import QSettings, QTimer
from PyQt6.QtWidgets import QApplication
for fmt in (QSettings.Format.NativeFormat, QSettings.Format.IniFormat):
    QSettings.setPath(fmt, QSettings.Scope.UserScope, {td!r})
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
