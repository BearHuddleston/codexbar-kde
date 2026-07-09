import datetime as dt
import unittest

from codexbar_kde.model import (
    format_reset_countdown,
    normalize_payload,
    parse_iso_datetime,
    sanitize_for_debug,
    severity_for_percent,
)


class ModelTests(unittest.TestCase):
    def test_normalize_payload_extracts_provider_windows_and_omits_identity(self):
        payload = [
            {
                "provider": "codex",
                "source": "oauth",
                "version": "0.38.0",
                "usage": {
                    "accountEmail": "person@example.com",
                    "identity": {"email": "person@example.com", "token": "secret"},
                    "updatedAt": "2026-07-04T00:00:00Z",
                    "primary": {"usedPercent": 11, "resetsAt": "2026-07-04T01:02:50Z"},
                    "secondary": {"usedPercent": 6, "resetsAt": "2026-07-07T03:01:46Z"},
                },
                "credits": {"remaining": 4, "events": [{"ignored": True}]},
            },
            {
                "provider": "claude",
                "source": "oauth",
                "usage": {
                    "primary": {"percentLeft": 100, "resetsAt": "2026-07-04T01:00:00Z"},
                    "secondary": {"usedPercent": 21, "resetsAt": "2026-07-07T09:00:00Z"},
                },
            },
        ]

        providers = normalize_payload(payload)

        self.assertEqual([p.provider for p in providers], ["codex", "claude"])
        self.assertEqual(providers[0].display_name, "Codex")
        self.assertEqual([(w.key, w.label, w.used_percent) for w in providers[0].windows], [
            ("primary", "5h/session", 11.0),
            ("secondary", "weekly", 6.0),
        ])
        self.assertEqual(providers[0].credits_remaining, 4)
        self.assertEqual(providers[1].windows[0].used_percent, 0.0)
        self.assertEqual(providers[1].windows[1].used_percent, 21.0)
        self.assertNotIn("person@example.com", repr(providers))
        self.assertNotIn("secret", repr(providers))

    def test_normalize_payload_extracts_nested_extra_rate_windows(self):
        payload = {
            "provider": "codex",
            "usage": {
                "primary": {"usedPercent": 5},
                "extraRateWindows": [
                    {
                        "id": "codex-spark",
                        "title": "Codex Spark 5-hour",
                        "window": {"usedPercent": 40, "resetsAt": "2026-07-04T09:24:16Z"},
                    }
                ],
            },
        }

        providers = normalize_payload(payload)

        labels = [(w.label, w.used_percent) for w in providers[0].windows]
        self.assertIn(("Codex Spark 5-hour", 40.0), labels)

    def test_normalize_payload_attaches_pace_notes_to_windows(self):
        payload = {
            "provider": "codex",
            "usage": {
                "primary": {"usedPercent": 36},
                "secondary": {"usedPercent": 7},
            },
            "pace": {
                "primary": {"deltaPercent": -30, "expectedUsedPercent": 66, "willLastToReset": True},
                "secondary": {"deltaPercent": 12, "expectedUsedPercent": 40, "willLastToReset": False},
            },
        }

        providers = normalize_payload(payload)

        notes = {w.key: w.pace_note for w in providers[0].windows}
        self.assertEqual(notes["primary"], "30% reserve · expected 66% · lasts to reset")
        self.assertEqual(notes["secondary"], "12% over pace · expected 40% · may run out before reset")

    def test_normalize_payload_preserves_human_reset_descriptions(self):
        payload = {
            "provider": "claude",
            "usage": {
                "primary": {
                    "usedPercent": 12,
                    "resetDescription": "Resets4pm(America/Chicago)",
                }
            },
        }

        window = normalize_payload(payload)[0].windows[0]

        self.assertIsNone(window.resets_at)
        self.assertEqual(window.reset_countdown, "")
        self.assertEqual(window.reset_description, "Resets4pm(America/Chicago)")

    def test_normalize_payload_rejects_nonfinite_and_overflowing_percentages(self):
        payload = [
            {"provider": "nan", "usage": {"primary": {"usedPercent": "NaN"}}},
            {"provider": "huge", "usage": {"primary": {"usedPercent": 10 ** 5000}}},
        ]

        providers = normalize_payload(payload)

        self.assertEqual(providers[0].windows, [])
        self.assertEqual(providers[1].windows, [])

    def test_parse_iso_datetime_rejects_overflowing_utc_conversion(self):
        self.assertIsNone(parse_iso_datetime("0001-01-01T00:00:00+23:59"))

    def test_normalize_payload_keeps_window_minutes(self):
        payload = {
            "provider": "codex",
            "usage": {
                "primary": {"usedPercent": 5, "windowMinutes": 300},
                "secondary": {"usedPercent": 7, "windowMinutes": 10080},
            },
        }

        providers = normalize_payload(payload)

        minutes = {w.key: w.window_minutes for w in providers[0].windows}
        self.assertEqual(minutes["primary"], 300)
        self.assertEqual(minutes["secondary"], 10080)

    def test_normalize_payload_keeps_provider_errors_as_cards(self):
        providers = normalize_payload({
            "provider": "gemini",
            "source": "api",
            "error": {"message": "not configured"},
        })

        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0].display_name, "Gemini")
        self.assertEqual(providers[0].error, "not configured")
        self.assertEqual(providers[0].windows, [])

    def test_sanitize_for_debug_preserves_sensitive_container_redaction(self):
        sanitized = sanitize_for_debug({
            "identity": {"display_name": "Ada"},
            "organization": "Example Corp",
            "message": "safe",
        })

        self.assertEqual(sanitized["identity"], "[REDACTED]")
        self.assertEqual(sanitized["organization"], "[REDACTED]")
        self.assertEqual(sanitized["message"], "safe")

    def test_normalize_payload_redacts_provider_errors(self):
        secret = "REVIEW_PROVIDER_SECRET_123456"

        providers = normalize_payload({
            "provider": "gemini",
            "error": {"message": f'failed: {{"access_token": "{secret}"}}'},
        })

        self.assertIn("[REDACTED]", providers[0].error)
        self.assertNotIn(secret, providers[0].error)

    def test_reset_countdown_and_severity(self):
        now = dt.datetime(2026, 7, 4, 0, 0, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(format_reset_countdown("2026-07-04T01:02:50Z", now=now), "1h 3m")
        self.assertEqual(format_reset_countdown("2026-07-07T03:01:46Z", now=now), "3d 3h")
        self.assertEqual(format_reset_countdown(None, now=now), "")
        self.assertEqual(severity_for_percent(5), "ok")
        self.assertEqual(severity_for_percent(75), "warn")
        self.assertEqual(severity_for_percent(95), "critical")


if __name__ == "__main__":
    unittest.main()
