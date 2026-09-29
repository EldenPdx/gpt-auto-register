from __future__ import annotations

import unittest
from unittest.mock import Mock

from auth_flow import AuthFlow, AuthResult


class _Response:
    def __init__(self, status_code: int, data: dict | None = None):
        self.status_code = status_code
        self.text = ""
        self._data = data or {}

    def json(self) -> dict:
        return self._data


class _Session:
    def __init__(self):
        self.calls = []
        self.responses = [_Response(401), _Response(200, {"continue_url": "/next"})]

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class TeamOAuthContractTest(unittest.TestCase):
    def test_invalid_login_state_does_not_retry_signup_in_same_session(self) -> None:
        flow = AuthFlow.__new__(AuthFlow)
        flow.result = AuthResult()
        flow.check_proxy = lambda: True
        flow.warmup = lambda: True
        flow.get_csrf_token = lambda: "csrf"
        flow.get_auth_url = lambda *_args, **_kwargs: "https://auth.openai.com/oauth/authorize"
        flow.auth_oauth_init = lambda _url: "device"
        flow.get_sentinel_token = lambda _device: "sentinel"
        flow._get_env = lambda _key, default="": default
        flow.authorize_continue = Mock(side_effect=lambda **kwargs: (_ for _ in ()).throw(
            RuntimeError(f"authorize/continue 失败(screen_hint={kwargs['screen_hint']}): HTTP 409 invalid_state")
        ))

        with self.assertRaisesRegex(RuntimeError, "screen_hint=login"):
            flow.run_protocol_login(object(), "member@example.com", "password")
        self.assertEqual(flow.authorize_continue.call_count, 1)

    def test_existing_session_refresh_uses_single_response_and_json_session_token(self) -> None:
        class Cookies:
            def __init__(self):
                self.values = {}

            def set(self, name, value, **_kwargs):
                self.values[name] = value

            def get(self, name, default=""):
                return self.values.get(name, default)

        class Session:
            def __init__(self):
                self.cookies = Cookies()
                self.calls = 0

            def get(self, *_args, **_kwargs):
                self.calls += 1
                return _Response(200, {
                    "accessToken": "fresh-at",
                    "sessionToken": "fresh-st",
                    "user": {"email": "a@example.com"},
                })

        flow = AuthFlow.__new__(AuthFlow)
        flow.result = AuthResult()
        flow.session = Session()
        flow._common_headers = lambda _referer: {}
        flow._build_chatgpt_cookie_header = lambda: ""

        result = flow.from_existing_credentials("saved-st", "saved-at", "did")

        self.assertTrue(result.is_valid())
        self.assertEqual(result.access_token, "fresh-at")
        self.assertEqual(result.session_token, "fresh-st")
        self.assertEqual(result.email, "a@example.com")
        self.assertEqual(flow.session.calls, 1)

    def test_existing_session_is_invalid_when_refresh_returns_no_access_token(self) -> None:
        class Cookies:
            def set(self, *_args, **_kwargs):
                pass

            def get(self, _name, default=""):
                return default

        class Session:
            cookies = Cookies()

            def get(self, *_args, **_kwargs):
                return _Response(200, {})

        flow = AuthFlow.__new__(AuthFlow)
        flow.result = AuthResult()
        flow.session = Session()
        flow._common_headers = lambda _referer: {}
        flow._build_chatgpt_cookie_header = lambda: ""

        result = flow.from_existing_credentials("stale-st", "stale-at", "did")

        self.assertFalse(result.is_valid())

    def test_password_login_matches_browser_sentinel_then_real_password(self) -> None:
        flow = AuthFlow.__new__(AuthFlow)
        flow.session = _Session()
        flow._last_sentinel_token = ""
        flow._last_sentinel_so_token = ""
        flow._common_headers = lambda _referer: {}
        flow._trace_http = lambda *_args, **_kwargs: None

        result = flow.login_password_verify("real-password")

        self.assertEqual(result, {"continue_url": "/next"})
        self.assertEqual(
            [call[1]["json"]["password"] for call in flow.session.calls],
            ["sentinel", "real-password"],
        )

    def test_password_is_not_sent_when_sentinel_preflight_is_rate_limited(self) -> None:
        flow = AuthFlow.__new__(AuthFlow)
        flow.session = _Session()
        flow.session.responses = [_Response(429)]
        flow._last_sentinel_token = ""
        flow._last_sentinel_so_token = ""
        flow._common_headers = lambda _referer: {}
        flow._trace_http = lambda *_args, **_kwargs: None

        with self.assertRaisesRegex(RuntimeError, "实际 HTTP 429"):
            flow.login_password_verify("must-not-be-sent")

        self.assertEqual(len(flow.session.calls), 1)
        self.assertEqual(flow.session.calls[0][1]["json"]["password"], "sentinel")


if __name__ == "__main__":
    unittest.main()
