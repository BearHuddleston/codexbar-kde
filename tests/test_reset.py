import datetime as dt
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from codexbar_kde.reset import (
    CodexAuthError,
    consume_reset_credit,
    load_codex_auth,
    pick_next_expiring,
)


CREDITS = [
    {"id": "RateLimitResetCredit_b", "status": "available", "expires_at": "2026-07-18T01:00:00Z",
     "title": "Full reset (Weekly + 5 hr)", "reset_type": "codex_rate_limits"},
    {"id": "RateLimitResetCredit_a", "status": "available", "expires_at": "2026-07-12T02:39:09Z",
     "title": "Full reset (Weekly + 5 hr)", "reset_type": "codex_rate_limits"},
    {"id": "RateLimitResetCredit_used", "status": "redeemed", "expires_at": "2026-07-01T00:00:00Z"},
    {"id": "RateLimitResetCredit_nodate", "status": "available"},
]


class PickNextExpiringTests(unittest.TestCase):
    def test_picks_available_credit_with_earliest_expiry(self):
        credit = pick_next_expiring(CREDITS)
        self.assertIsNotNone(credit)
        self.assertEqual(credit["id"], "RateLimitResetCredit_a")

    def test_ignores_non_available_credits(self):
        credit = pick_next_expiring([
            {"id": "x", "status": "redeemed", "expires_at": "2026-07-01T00:00:00Z"},
        ])
        self.assertIsNone(credit)

    def test_credits_without_expiry_sort_last(self):
        credit = pick_next_expiring([
            {"id": "nodate", "status": "available"},
            {"id": "dated", "status": "available", "expires_at": "2026-08-01T00:00:00Z"},
        ])
        self.assertEqual(credit["id"], "dated")

    def test_empty_list_returns_none(self):
        self.assertIsNone(pick_next_expiring([]))


class LoadAuthTests(unittest.TestCase):
    def test_loads_token_and_account_id_from_codex_home(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp) / "auth.json"
            auth.write_text(json.dumps({
                "tokens": {"access_token": "tok123", "account_id": "acc456"},
            }))
            token, account_id = load_codex_auth(auth)
        self.assertEqual(token, "tok123")
        self.assertEqual(account_id, "acc456")

    def test_missing_file_raises_auth_error(self):
        with self.assertRaises(CodexAuthError):
            load_codex_auth(Path("/nonexistent/auth.json"))

    def test_missing_fields_raise_auth_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp) / "auth.json"
            auth.write_text(json.dumps({"tokens": {}}))
            with self.assertRaises(CodexAuthError):
                load_codex_auth(auth)


class ConsumeTests(unittest.TestCase):
    def test_consume_posts_credit_id_with_uuid_request_id(self):
        captured = {}

        class FakeResponse:
            status = 200

            def read(self):
                return json.dumps({
                    "code": "reset", "windows_reset": 1,
                    "credit": {"id": "RateLimitResetCredit_a", "status": "redeemed",
                               "redeemed_at": "2026-07-04T12:00:00Z"},
                }).encode()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = json.loads(req.data.decode())
            captured["auth"] = req.get_header("Authorization")
            captured["account"] = req.get_header("Chatgpt-account-id")
            return FakeResponse()

        with patch("codexbar_kde.reset.urllib.request.urlopen", side_effect=fake_urlopen):
            result = consume_reset_credit("tok123", "acc456", "RateLimitResetCredit_a")

        self.assertIn("/wham/rate-limit-reset-credits/consume", captured["url"])
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["body"]["credit_id"], "RateLimitResetCredit_a")
        self.assertEqual(len(captured["body"]["redeem_request_id"]), 36)  # uuid4
        self.assertEqual(captured["auth"], "Bearer tok123")
        self.assertEqual(captured["account"], "acc456")
        self.assertEqual(result["windows_reset"], 1)
        self.assertEqual(result["code"], "reset")


if __name__ == "__main__":
    unittest.main()
