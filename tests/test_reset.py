import datetime as dt
import http.client
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

from codexbar_kde.reset import (
    CodexAuthError,
    CodexResetError,
    _request,
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
NOW = dt.datetime(2026, 7, 9, tzinfo=dt.timezone.utc)


class PickNextExpiringTests(unittest.TestCase):
    def test_picks_available_credit_with_earliest_expiry(self):
        credit = pick_next_expiring(CREDITS, now=NOW)
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
        ], now=NOW)
        self.assertEqual(credit["id"], "dated")

    def test_invalid_expiry_is_not_treated_as_nonexpiring(self):
        credit = pick_next_expiring([
            {"id": "invalid", "status": "available", "expires_at": "not-a-date"},
            {"id": "nodate", "status": "available"},
        ], now=NOW)

        self.assertIsNotNone(credit)
        self.assertEqual(credit.get("id") if credit else None, "nodate")

    def test_empty_list_returns_none(self):
        self.assertIsNone(pick_next_expiring([]))

    def test_expired_available_credit_is_ignored(self):
        credit = pick_next_expiring([
            {"id": "expired", "status": "available", "expires_at": "2026-07-08T00:00:00Z"},
            {"id": "future", "status": "available", "expires_at": "2026-08-01T00:00:00Z"},
        ], now=NOW)

        self.assertIsNotNone(credit)
        self.assertEqual(credit["id"], "future")

    def test_expiries_are_ordered_as_utc_instants(self):
        credit = pick_next_expiring([
            {"id": "later", "status": "available", "expires_at": "2026-07-10T00:00:00-10:00"},
            {"id": "earlier", "status": "available", "expires_at": "2026-07-10T05:00:00+00:00"},
        ], now=NOW)

        self.assertIsNotNone(credit)
        self.assertEqual(credit["id"], "earlier")


class LoadAuthTests(unittest.TestCase):
    def test_loads_token_and_account_id_from_codex_home(self):
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
        with tempfile.TemporaryDirectory() as tmp:
            auth = Path(tmp) / "auth.json"
            auth.write_text(json.dumps({"tokens": {}}))
            with self.assertRaises(CodexAuthError):
                load_codex_auth(auth)

    def test_non_object_auth_file_raises_auth_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "auth.json"
            path.write_text("[]")

            with self.assertRaises(CodexAuthError):
                load_codex_auth(path)

    def test_non_string_credentials_fall_back_to_valid_nested_values(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "auth.json"
            path.write_text(json.dumps({
                "access_token": 123,
                "account_id": True,
                "tokens": {
                    "access_token": " nested-token ",
                    "account_id": " nested-account ",
                },
            }))

            self.assertEqual(
                load_codex_auth(path),
                ("nested-token", "nested-account"),
            )

    def test_malformed_or_blank_credentials_raise_auth_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "auth.json"
            path.write_text(json.dumps({
                "access_token": ["not", "a", "token"],
                "account_id": "   ",
                "tokens": {"access_token": False, "account_id": {}},
            }))

            with self.assertRaises(CodexAuthError):
                load_codex_auth(path)


class RequestSecurityTests(unittest.TestCase):
    def test_cross_origin_redirect_is_rejected_before_credentials_are_forwarded(self):
        seen_headers = []

        class TargetHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen_headers.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, format, *args):
                del format, args

        target = HTTPServer(("127.0.0.1", 0), TargetHandler)

        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://localhost:{target.server_port}/capture")
                self.end_headers()

            def log_message(self, format, *args):
                del format, args

        redirect = HTTPServer(("127.0.0.1", 0), RedirectHandler)
        threads = [
            threading.Thread(target=server.serve_forever, daemon=True)
            for server in (target, redirect)
        ]
        for thread in threads:
            thread.start()
        try:
            with self.assertRaises(CodexResetError):
                _request(
                    "GET",
                    f"http://127.0.0.1:{redirect.server_port}/start",
                    token="REVIEW_TOKEN_123456",
                    account_id="REVIEW_ACCOUNT_123456",
                )
        finally:
            for server in (redirect, target):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=2)

        self.assertEqual(seen_headers, [])

    def test_oversized_response_is_rejected(self):
        body = b'{"data":"' + (b"x" * 1_048_576) + b'"}'

        class FakeResponse:
            status = 200

            def read(self, size=-1):
                return body if size < 0 else body[:size]

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        with patch("codexbar_kde.reset._open_request", return_value=FakeResponse()):
            with self.assertRaises(CodexResetError):
                _request(
                    "GET",
                    "https://chatgpt.com/backend-api/example",
                    token="REVIEW_TOKEN_123456",
                    account_id="REVIEW_ACCOUNT_123456",
                )

    def test_response_read_failures_are_normalized(self):
        failures = (
            TimeoutError("timed out while reading"),
            OSError("connection reset while reading"),
            http.client.IncompleteRead(b"partial", 100),
        )

        class FakeResponse:
            status = 200

            def __init__(self, failure):
                self.failure = failure

            def read(self, _size=-1):
                raise self.failure

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with (
                    patch(
                        "codexbar_kde.reset._open_request",
                        return_value=FakeResponse(failure),
                    ),
                    self.assertRaisesRegex(CodexResetError, "network error"),
                ):
                    _request(
                        "POST",
                        "https://chatgpt.com/backend-api/example",
                        token="REVIEW_TOKEN_123456",
                        account_id="REVIEW_ACCOUNT_123456",
                    )


class ConsumeTests(unittest.TestCase):
    def test_consume_posts_credit_id_with_uuid_request_id(self):
        captured = {}

        class FakeResponse:
            status = 200

            def read(self, size=-1):
                payload = json.dumps({
                    "code": "reset", "windows_reset": 1,
                    "credit": {"id": "RateLimitResetCredit_a", "status": "redeemed",
                               "redeemed_at": "2026-07-04T12:00:00Z"},
                }).encode()
                return payload if size < 0 else payload[:size]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_open_request(req, timeout=0):
            captured["url"] = req.full_url
            captured["method"] = req.get_method()
            captured["body"] = json.loads(req.data.decode())
            captured["auth"] = req.get_header("Authorization")
            captured["account"] = req.get_header("Chatgpt-account-id")
            return FakeResponse()

        with patch("codexbar_kde.reset._open_request", side_effect=fake_open_request):
            result = consume_reset_credit("tok123", "acc456", "RateLimitResetCredit_a")

        self.assertIn("/wham/rate-limit-reset-credits/consume", captured["url"])
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["body"]["credit_id"], "RateLimitResetCredit_a")
        self.assertEqual(len(captured["body"]["redeem_request_id"]), 36)  # uuid4
        self.assertEqual(captured["auth"], "Bearer tok123")
        self.assertEqual(captured["account"], "acc456")
        self.assertEqual(result["windows_reset"], 1)
        self.assertEqual(result["code"], "reset")

    def test_consume_rejects_empty_success_payload(self):
        with patch("codexbar_kde.reset._request", return_value=(200, {})):
            with self.assertRaisesRegex(CodexResetError, "invalid consume response"):
                consume_reset_credit("tok123", "acc456", "RateLimitResetCredit_a")

    def test_consume_rejects_response_for_a_different_credit(self):
        payload = {
            "code": "reset",
            "windows_reset": 1,
            "credit": {"id": "other", "status": "redeemed"},
        }
        with patch("codexbar_kde.reset._request", return_value=(200, payload)):
            with self.assertRaisesRegex(CodexResetError, "invalid consume response"):
                consume_reset_credit("tok123", "acc456", "RateLimitResetCredit_a")

    def test_consume_requires_a_positive_window_count(self):
        payload = {
            "code": "reset",
            "windows_reset": None,
            "credit": {"id": "RateLimitResetCredit_a", "status": "redeemed"},
        }
        with patch("codexbar_kde.reset._request", return_value=(200, payload)):
            with self.assertRaisesRegex(CodexResetError, "invalid consume response"):
                consume_reset_credit("tok123", "acc456", "RateLimitResetCredit_a")

    def test_redeem_revalidates_credit_before_consuming(self):
        from codexbar_kde import reset

        available = {
            "id": "RateLimitResetCredit_a",
            "status": "available",
            "expires_at": "2026-08-01T00:00:00Z",
        }
        consumed = {
            "code": "reset",
            "windows_reset": 1,
            "credit": {"id": "RateLimitResetCredit_a", "status": "redeemed"},
        }
        with (
            patch.object(reset, "list_reset_credits", return_value={"credits": [available]}) as listed,
            patch.object(reset, "consume_reset_credit", return_value=consumed) as consumed_call,
        ):
            result = reset.redeem_reset_credit(
                "tok123",
                "acc456",
                "RateLimitResetCredit_a",
                now=NOW,
            )

        listed.assert_called_once()
        consumed_call.assert_called_once()
        self.assertEqual(result, consumed)

    def test_redeem_aborts_if_a_different_credit_now_expires_first(self):
        from codexbar_kde import reset

        originally_selected = {
            "id": "RateLimitResetCredit_a",
            "status": "available",
            "expires_at": "2026-08-01T00:00:00Z",
        }
        newly_sooner = {
            "id": "RateLimitResetCredit_new",
            "status": "available",
            "expires_at": "2026-07-20T00:00:00Z",
        }
        with (
            patch.object(
                reset,
                "list_reset_credits",
                return_value={"credits": [originally_selected, newly_sooner]},
            ),
            patch.object(reset, "consume_reset_credit") as consumed_call,
        ):
            with self.assertRaisesRegex(CodexResetError, "available credits changed"):
                reset.redeem_reset_credit(
                    "tok123",
                    "acc456",
                    "RateLimitResetCredit_a",
                    now=NOW,
                )

        consumed_call.assert_not_called()

    def test_redeem_reconciles_after_an_ambiguous_consume_failure(self):
        from codexbar_kde import reset

        available = {
            "id": "RateLimitResetCredit_a",
            "status": "available",
            "expires_at": "2026-08-01T00:00:00Z",
        }
        redeemed = {"id": "RateLimitResetCredit_a", "status": "redeemed"}
        with (
            patch.object(
                reset,
                "list_reset_credits",
                side_effect=[{"credits": [available]}, {"credits": [redeemed]}],
            ) as listed,
            patch.object(
                reset,
                "consume_reset_credit",
                side_effect=CodexResetError("network error after POST"),
            ),
        ):
            result = reset.redeem_reset_credit(
                "tok123",
                "acc456",
                "RateLimitResetCredit_a",
                now=NOW,
            )

        self.assertEqual(listed.call_count, 2)
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["credit"], redeemed)

    def test_post_read_timeout_reconciles_without_retrying_post(self):
        from codexbar_kde import reset

        credit_id = "RateLimitResetCredit_a"
        available = {
            "id": credit_id,
            "status": "available",
            "expires_at": "2026-08-01T00:00:00Z",
        }
        redeemed = {"id": credit_id, "status": "redeemed"}

        class FakeResponse:
            status = 200

            def __init__(self, payload=None, failure=None):
                self.payload = payload
                self.failure = failure

            def read(self, size=-1):
                if self.failure is not None:
                    raise self.failure
                encoded = json.dumps(self.payload).encode()
                return encoded if size < 0 else encoded[:size]

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        responses = iter((
            FakeResponse({"credits": [available]}),
            FakeResponse(failure=TimeoutError("timed out after POST")),
            FakeResponse({"credits": [redeemed]}),
        ))
        methods = []

        def fake_open(req, *, timeout):
            del timeout
            methods.append(req.get_method())
            return next(responses)

        with patch.object(reset, "_open_request", side_effect=fake_open):
            result = reset.redeem_reset_credit(
                "tok123",
                "acc456",
                credit_id,
                now=NOW,
            )

        self.assertEqual(methods, ["GET", "POST", "GET"])
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["credit"], redeemed)

    def test_redeem_preserves_failure_if_credit_remains_available(self):
        from codexbar_kde import reset

        available = {
            "id": "RateLimitResetCredit_a",
            "status": "available",
            "expires_at": "2026-08-01T00:00:00Z",
        }
        with (
            patch.object(
                reset,
                "list_reset_credits",
                side_effect=[{"credits": [available]}, {"credits": [available]}],
            ) as listed,
            patch.object(
                reset,
                "consume_reset_credit",
                side_effect=CodexResetError("network error after POST"),
            ) as consumed,
        ):
            with self.assertRaisesRegex(CodexResetError, "network error after POST"):
                reset.redeem_reset_credit(
                    "tok123",
                    "acc456",
                    "RateLimitResetCredit_a",
                    now=NOW,
                )

        self.assertEqual(listed.call_count, 2)
        consumed.assert_called_once()

    def test_redeem_reconciles_if_credit_disappears_after_consume_failure(self):
        from codexbar_kde import reset

        available = {
            "id": "RateLimitResetCredit_a",
            "status": "available",
            "expires_at": "2026-08-01T00:00:00Z",
        }
        with (
            patch.object(
                reset,
                "list_reset_credits",
                side_effect=[{"credits": [available]}, {"credits": []}],
            ) as listed,
            patch.object(
                reset,
                "consume_reset_credit",
                side_effect=CodexResetError("network error after POST"),
            ),
        ):
            result = reset.redeem_reset_credit(
                "tok123",
                "acc456",
                "RateLimitResetCredit_a",
                now=NOW,
            )

        self.assertEqual(listed.call_count, 2)
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["credit"]["id"], "RateLimitResetCredit_a")
        self.assertEqual(result["credit"]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
