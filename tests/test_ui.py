import json
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from codexbar_kde.app import DashboardWindow
from codexbar_kde.model import normalize_payload


def _app():
    return QApplication.instance() or QApplication(["codexbar-kde-tests"])


PAYLOAD = [
    {
        "provider": "codex",
        "source": "oauth",
        "version": "0.142.5",
        "usage": {
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

    def _window(self):
        window = DashboardWindow(codexbar_bin="/usr/bin/codexbar", refresh_seconds=3600)
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

    def test_reset_panel_hidden_without_codex_credits(self):
        window = DashboardWindow(codexbar_bin="/usr/bin/codexbar", refresh_seconds=3600)
        payload = [{"provider": "claude", "source": "oauth", "usage": {"primary": {"usedPercent": 12}}}]
        window.set_providers(normalize_payload(payload), raw_payload=payload)

        self.assertFalse(window.view_overview.reset_panel.isVisibleTo(window.view_overview))


if __name__ == "__main__":
    unittest.main()
