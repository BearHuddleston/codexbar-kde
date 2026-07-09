import json
import subprocess
import unittest
from unittest.mock import patch

from codexbar_kde.app import (
    build_codexbar_command,
    build_tray_tooltip,
    color_for_percent,
    format_updated_age,
    load_usage_from_json_text,
    load_usage_payload_from_command,
    progress_style,
    provider_accent_color,
    redact_text,
    RedeemWorker,
)


class AppTests(unittest.TestCase):
    def test_version_flag_reports_package_version(self):
        import codexbar_kde
        from codexbar_kde.app import parse_args

        with self.assertRaises(SystemExit) as ctx:
            parse_args(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertRegex(codexbar_kde.__version__, r"^\d+\.\d+\.\d+$")

    def test_build_codexbar_command_uses_absolute_binary_and_json_only(self):
        self.assertEqual(
            build_codexbar_command("/usr/bin/codexbar"),
            ["/usr/bin/codexbar", "usage", "--format", "json", "--json-only", "--pretty"],
        )

    def test_modern_ui_helpers_use_provider_accents_and_thin_meters(self):
        self.assertEqual(provider_accent_color("codex"), "#7170ff")
        self.assertEqual(provider_accent_color("claude"), "#d97757")
        self.assertEqual(color_for_percent(95), "#ff6b6b")

        style = progress_style(42, "#7170ff")

        self.assertIn("#7170ff", style)
        self.assertIn("border-radius: 3px", style)
        self.assertIn("max-height: 6px", style)

    def test_load_usage_from_json_text_returns_normalized_providers(self):
        text = json.dumps([
            {"provider": "codex", "source": "oauth", "usage": {"primary": {"usedPercent": 33}}},
            {"provider": "claude", "source": "oauth", "usage": {"secondary": {"usedPercent": 22}}},
        ])

        providers = load_usage_from_json_text(text)

        self.assertEqual([p.display_name for p in providers], ["Codex", "Claude"])
        self.assertEqual(providers[0].windows[0].used_percent, 33.0)
        self.assertEqual(providers[1].windows[0].label, "weekly")

    def test_load_usage_payload_from_command_accepts_json_payload_even_when_codexbar_exits_nonzero(self):
        stdout = json.dumps([
            {"provider": "codex", "usage": {"primary": {"usedPercent": 1}}},
            {"provider": "claude", "source": "auto", "error": {"message": "rate limited"}},
        ])
        completed = subprocess.CompletedProcess(["/usr/bin/codexbar"], 1, stdout=stdout, stderr="")
        with patch("codexbar_kde.app.subprocess.run", return_value=completed):
            payload = load_usage_payload_from_command("/usr/bin/codexbar")

        self.assertEqual(payload[1]["error"]["message"], "rate limited")

    def test_command_failure_redacts_json_credentials(self):
        secret = "REVIEW_STDERR_SECRET_123456"
        completed = subprocess.CompletedProcess(
            ["/usr/bin/codexbar"],
            2,
            stdout="",
            stderr=f'{{"access_token": "{secret}"}}',
        )
        with patch("codexbar_kde.app.subprocess.run", return_value=completed):
            with self.assertRaises(RuntimeError) as raised:
                load_usage_payload_from_command("/usr/bin/codexbar")

        self.assertIn("[REDACTED]", str(raised.exception))
        self.assertNotIn(secret, str(raised.exception))

    def test_redeem_worker_revalidates_and_reports_reconciled_success(self):
        worker = RedeemWorker("RateLimitResetCredit_a")
        emitted = []
        worker.finished_with_result.connect(lambda ok, message: emitted.append((ok, message)))
        result = {
            "code": "reset",
            "windows_reset": None,
            "credit": {"id": "RateLimitResetCredit_a", "status": "redeemed"},
            "reconciled": True,
        }
        with (
            patch("codexbar_kde.app.load_codex_auth", return_value=("tok", "acct")),
            patch("codexbar_kde.app.redeem_reset_credit", return_value=result) as redeem,
        ):
            worker.run()

        redeem.assert_called_once_with("tok", "acct", "RateLimitResetCredit_a")
        self.assertEqual(emitted, [(True, "Redeemed — confirmed after status refresh")])

    def test_build_tray_tooltip_shows_expanded_provider_details_without_identity(self):
        text = json.dumps([
            {
                "provider": "codex",
                "source": "oauth",
                "version": "0.142.5",
                "usage": {
                    "accountEmail": "person@example.com",
                    "primary": {"usedPercent": 33, "resetsAt": "2026-07-04T01:00:00Z"},
                    "secondary": {"usedPercent": 12},
                },
                "credits": {"remaining": 4},
            },
            {
                "provider": "claude",
                "source": "oauth",
                "usage": {"primary": {"usedPercent": 0}, "secondary": {"usedPercent": 22}},
            },
        ])
        providers = load_usage_from_json_text(text)

        tooltip = build_tray_tooltip(providers)

        self.assertIn("CodexBar KDE", tooltip)
        self.assertIn("Codex (oauth, v0.142.5)", tooltip)
        self.assertIn("  5h/session: 33% used", tooltip)
        self.assertIn("resets", tooltip)
        self.assertIn("  weekly: 12% used", tooltip)
        self.assertIn("  credits remaining: 4", tooltip)
        self.assertIn("Claude (oauth)", tooltip)
        self.assertIn("  5h/session: 0% used", tooltip)
        self.assertIn("  weekly: 22% used", tooltip)
        self.assertNotIn("person@example.com", tooltip)

    def test_build_tray_tooltip_redacts_errors(self):
        tooltip = build_tray_tooltip([], "token=abcdef person@example.com")

        self.assertIn("Refresh failed", tooltip)
        self.assertIn("token=[REDACTED]", tooltip)
        self.assertNotIn("abcdef", tooltip)
        self.assertNotIn("person@example.com", tooltip)

    def test_redact_text_handles_json_credentials(self):
        secret = "REVIEW_SECRET_123456"

        redacted = redact_text(f'{{"access_token": "{secret}"}}')

        self.assertIn("[REDACTED]", redacted)
        self.assertNotIn(secret, redacted)

    def test_redact_text_handles_common_unstructured_credentials(self):
        cases = {
            "token = REVIEW_TOKEN_123456": "REVIEW_TOKEN_123456",
            "'api_key': 'REVIEW_KEY_123456'": "REVIEW_KEY_123456",
            "Authorization: Basic REVIEW_BASIC_123456": "REVIEW_BASIC_123456",
            "request failed for https://person:REVIEW_PASS_123456@example.com/path": "REVIEW_PASS_123456",
            "eyJhbGciOiJIUzI1NiJ9.REVIEWPAYLOAD123456.REVIEW_SIGNATURE_123456": "REVIEW_SIGNATURE_123456",
            "OpenAI key sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456": "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
            "Anthropic key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456": "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ123456",
            "Gemini key AIzaABCDEFGHIJKLMNOPQRSTUVWXYZ123456789": "AIzaABCDEFGHIJKLMNOPQRSTUVWXYZ123456789",
        }
        for text, secret in cases.items():
            with self.subTest(text=text):
                redacted = redact_text(text)
                self.assertIn("[REDACTED]", redacted)
                self.assertNotIn(secret, redacted)

    def test_redact_text_handles_quoted_values_with_spaces(self):
        secret = "two words REVIEW_SPACE_SECRET_123456"

        redacted = redact_text(f'{{"password": "{secret}"}}')

        self.assertIn("[REDACTED]", redacted)
        self.assertNotIn(secret, redacted)
        self.assertNotIn("REVIEW_SPACE_SECRET_123456", redacted)

        escaped = r'{"password": "two words \"REVIEW_ESCAPED_SECRET_123456\" tail"}'
        escaped_redacted = redact_text(escaped)
        self.assertEqual(escaped_redacted, '{"password": "[REDACTED]"}')
        self.assertEqual(redact_text(escaped_redacted), escaped_redacted)

    def test_redact_text_handles_quoted_authorization_values(self):
        for scheme in ("Bearer", "Basic"):
            secret = f"REVIEW_{scheme.upper()}_SECRET_123456"
            with self.subTest(scheme=scheme):
                redacted = redact_text(
                    f'{{"authorization": "{scheme} {secret}"}}'
                )

                self.assertIn("[REDACTED]", redacted)
                self.assertNotIn(secret, redacted)

    def test_redact_text_does_not_hide_plain_basic_language(self):
        self.assertEqual(
            redact_text("basic authentication failed"),
            "basic authentication failed",
        )

    def test_build_tray_tooltip_compacts_provider_error_from_raw_payload(self):
        payload = [{"provider": "claude", "source": "auto", "error": {"message": "Could not parse Claude usage: rate limited"}}]
        providers = load_usage_from_json_text(json.dumps(payload))

        tooltip = build_tray_tooltip(providers, raw_payload=payload)

        self.assertIn("Claude (auto)", tooltip)
        self.assertIn("Error: Could not parse Claude usage:", tooltip)
        # long errors word-wrap into indented continuation lines
        self.assertIn("\n    limited", tooltip)

    def test_build_tray_tooltip_uses_nerd_font_icons_status_and_usage_bars(self):
        payload = [
            {
                "provider": "codex",
                "source": "oauth",
                "version": "0.142.5",
                "usage": {
                    "primary": {"resetDescription": "tomorrow, 1:03 AM", "usedPercent": 36},
                    "secondary": {"resetDescription": "Jul 6 at 10:01 PM", "usedPercent": 7},
                    "updatedAt": "2026-07-04T04:24:16Z",
                },
                "pace": {"primary": {"deltaPercent": -30, "expectedUsedPercent": 66, "willLastToReset": True}},
                "credits": {"remaining": 0},
            },
            {
                "provider": "claude",
                "source": "claude",
                "usage": {"primary": {"resetDescription": "Resets1am(America/Chicago)", "usedPercent": 0}},
            },
            {"provider": "gemini", "source": "auto", "error": {"message": "rate limited"}},
        ]
        providers = load_usage_from_json_text(json.dumps(payload))

        tooltip = build_tray_tooltip(providers, raw_payload=payload)

        self.assertIn("\uf0e4   CodexBar KDE • 3 providers", tooltip)
        self.assertIn("\uf058   Codex (oauth, v0.142.5)", tooltip)
        self.assertIn("\uf017   5h/session: 36% used", tooltip)
        self.assertIn("36% peak", tooltip)
        self.assertIn("▰▰▰▱▱▱▱▱▱▱", tooltip)
        self.assertIn("\uf0e7   30% reserve · expected 66% ✓", tooltip)
        self.assertIn("\uf09d   Credits: 0 remaining", tooltip)
        self.assertIn("↳ resets 1 am", tooltip)
        self.assertNotIn("America/Chicago", tooltip)
        self.assertIn("\uf071   Gemini (auto)", tooltip)
        self.assertIn("Error: rate limited", tooltip)

    def test_build_tray_tooltip_lines_fit_kde_wrap_budget(self):
        payload = [
            {
                "provider": "codex",
                "source": "oauth",
                "version": "0.142.5",
                "usage": {
                    "accountEmail": "person@example.com",
                    "loginMethod": "pro",
                    "dataConfidence": "exact",
                    "primary": {"usedPercent": 0, "resetDescription": "tomorrow, 2:47 PM", "windowMinutes": 300},
                    "secondary": {"usedPercent": 8, "resetDescription": "Jul 6 at 10:01 PM", "windowMinutes": 10080},
                    "extraRateWindows": [
                        {"id": "codex-spark", "title": "Codex Spark Weekly",
                         "window": {"usedPercent": 0, "resetDescription": "Jul 11 at 2:00 PM"}},
                    ],
                    "codexResetCredits": {
                        "availableCount": 4,
                        "credits": [
                            {"title": "Full reset (Weekly + 5 hr)", "status": "available", "expires_at": "2026-07-12T02:39:09Z"},
                            {"title": "Full reset (Weekly + 5 hr)", "status": "available", "expires_at": "2026-07-18T01:00:00Z"},
                        ],
                    },
                    "updatedAt": "2026-07-04T04:24:16Z",
                },
                "pace": {
                    "primary": {"deltaPercent": -84, "expectedUsedPercent": 84, "willLastToReset": True},
                    "secondary": {"deltaPercent": 12, "expectedUsedPercent": 67, "willLastToReset": False},
                },
                "credits": {"remaining": 0},
            },
            {
                "provider": "claude",
                "source": "claude",
                "version": "2.1.201",
                "usage": {"primary": {"usedPercent": 0, "resetDescription": "Resets4pm(America/Chicago)"}},
            },
            {
                "provider": "gemini",
                "source": "auto",
                "error": {"message": "Could not parse Gemini usage: the quota endpoint is rate limited right now. Please try again later."},
            },
        ]
        providers = load_usage_from_json_text(json.dumps(payload))

        tooltip = build_tray_tooltip(providers, raw_payload=payload)

        for line in tooltip.splitlines():
            self.assertLessEqual(len(line), 46, f"line too wide for KDE tooltip wrap: {line!r}")

    def test_format_updated_age_returns_relative_time(self):
        import datetime as dt
        now = dt.datetime(2026, 7, 4, 6, 30, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(format_updated_age("2026-07-04T06:29:40Z", now=now), "just now")
        self.assertEqual(format_updated_age("2026-07-04T06:05:00Z", now=now), "25m ago")
        self.assertEqual(format_updated_age("2026-07-04T02:30:00Z", now=now), "4h ago")
        self.assertEqual(format_updated_age("2026-07-01T06:30:00Z", now=now), "3d ago")
        self.assertEqual(format_updated_age("not-a-date", now=now), "not-a-date")

    def test_build_tray_tooltip_groups_duplicate_reset_credits(self):
        payload = [
            {
                "provider": "codex",
                "usage": {
                    "primary": {"usedPercent": 1},
                    "codexResetCredits": {
                        "availableCount": 4,
                        "credits": [
                            {"title": "Full reset (Weekly + 5 hr)", "status": "available", "expires_at": "2026-07-12T02:39:09Z"},
                            {"title": "Full reset (Weekly + 5 hr)", "status": "available", "expires_at": "2026-07-18T01:00:00Z"},
                            {"title": "Full reset (Weekly + 5 hr)", "status": "available", "expires_at": "2026-07-27T01:00:00Z"},
                            {"title": "Full reset (Weekly + 5 hr)", "status": "available", "expires_at": "2026-07-31T01:00:00Z"},
                        ],
                    },
                },
            }
        ]
        providers = load_usage_from_json_text(json.dumps(payload))

        tooltip = build_tray_tooltip(providers, raw_payload=payload)

        self.assertIn("Reset credits: 4 available", tooltip)
        self.assertIn("Full reset (Weekly + 5 hr) ×4", tooltip)
        self.assertIn("↳ next expires 2026-07-12", tooltip)
        self.assertEqual(tooltip.count("Full reset (Weekly + 5 hr)"), 1)

    def test_build_tray_tooltip_compacts_raw_codexbar_payload_to_meaningful_lines(self):
        payload = [
            {
                "credits": {"events": [], "remaining": 0, "updatedAt": "2026-07-04T04:24:16Z"},
                "pace": {
                    "primary": {
                        "deltaPercent": -66,
                        "expectedUsedPercent": 67,
                        "stage": "farBehind",
                        "summary": "66% in reserve | Expected 67% used | Lasts until reset",
                        "willLastToReset": True,
                    }
                },
                "provider": "codex",
                "source": "oauth",
                "usage": {
                    "accountEmail": "person@example.com",
                    "codexResetCredits": {
                        "availableCount": 1,
                        "credits": [
                            {
                                "description": "Thanks for using Codex! You've been granted one free rate limit reset.",
                                "expires_at": "2026-07-12T02:39:09Z",
                                "granted_at": "2026-06-12T02:39:09Z",
                                "id": "RateLimitResetCredit_example",
                                "reset_type": "codex_rate_limits",
                                "status": "available",
                                "title": "Full reset (Weekly + 5 hr)",
                            }
                        ],
                        "updatedAt": "2026-07-04T04:24:16Z",
                    },
                    "dataConfidence": "exact",
                    "extraRateWindows": [
                        {
                            "id": "codex-spark",
                            "title": "Codex Spark 5-hour",
                            "window": {
                                "resetDescription": "tomorrow, 4:24 AM",
                                "resetsAt": "2026-07-04T09:24:16Z",
                                "usedPercent": 0,
                                "windowMinutes": 300,
                            },
                        }
                    ],
                    "identity": {"accountEmail": "person@example.com", "loginMethod": "pro", "providerID": "codex"},
                    "loginMethod": "pro",
                    "primary": {
                        "resetDescription": "tomorrow, 1:03 AM",
                        "resetsAt": "2026-07-04T06:03:19Z",
                        "usedPercent": 1,
                        "windowMinutes": 300,
                    },
                    "secondary": {"resetDescription": "Jul 6 at 10:01 PM", "usedPercent": 7, "windowMinutes": 10080},
                    "tertiary": None,
                    "updatedAt": "2026-07-04T04:24:16Z",
                },
                "version": "0.142.5",
            }
        ]
        providers = load_usage_from_json_text(json.dumps(payload))

        tooltip = build_tray_tooltip(providers, raw_payload=payload)

        self.assertIn("Codex (oauth, v0.142.5)", tooltip)
        self.assertIn("Account: person@example.com", tooltip)
        self.assertIn("Plan: pro · confidence: exact", tooltip)
        self.assertIn("5h/session: 1% used", tooltip)
        self.assertIn("↳ resets tomorrow, 1:03 AM", tooltip)
        self.assertIn("weekly: 7% used", tooltip)
        self.assertIn("↳ resets Jul 6 at 10:01 PM", tooltip)
        self.assertIn("Codex Spark 5-hour: 0% used", tooltip)
        self.assertIn("↳ resets tomorrow, 4:24 AM", tooltip)
        self.assertIn("66% reserve · expected 67% ✓", tooltip)
        self.assertIn("Credits: 0 remaining", tooltip)
        self.assertIn("Reset credits: 1 available", tooltip)
        self.assertIn("Full reset (Weekly + 5 hr)", tooltip)
        self.assertIn("↳ expires 2026-07-12", tooltip)
        self.assertIn("Updated: ", tooltip)
        self.assertNotIn("Updated: 2026-07-04T04:24:16Z", tooltip)
        self.assertNotIn("events", tooltip)
        self.assertNotIn("RateLimitResetCredit_example", tooltip)
        self.assertNotIn("Thanks for using Codex", tooltip)
        self.assertNotIn("granted_at", tooltip)
        self.assertNotIn("reset_type", tooltip)
        self.assertNotIn("providerID", tooltip)
        self.assertNotIn("tertiary", tooltip)
        self.assertNotIn("windowMinutes", tooltip)


if __name__ == "__main__":
    unittest.main()
