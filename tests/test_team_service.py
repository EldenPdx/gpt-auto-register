from __future__ import annotations

import unittest
from unittest.mock import Mock, patch


class Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class ScriptedSession:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.cookies = Mock()

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class TeamClientInvitesTest(unittest.TestCase):
    def test_list_invites_paginates_on_one_fixed_session(self) -> None:
        from webui.team import TeamClient

        session = ScriptedSession(
            Response(payload={"items": [{"id": "i1"}], "total": 2}),
            Response(payload={"items": [{"id": "i2"}], "total": 2}),
        )
        client = TeamClient({
            "workspace_id": "ws-1",
            "owner_access_token": "owner-at",
            "owner_device_id": "device-1",
        }, session=session, page_size=1)

        result = client.list_invites()

        self.assertEqual([item["id"] for item in result["items"]], ["i1", "i2"])
        self.assertEqual(result["total"], 2)
        self.assertEqual([call[2]["params"]["offset"] for call in session.calls], [0, 1])
        headers = session.calls[0][2]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer owner-at")
        self.assertEqual(headers["Chatgpt-Account-Id"], "ws-1")
        self.assertEqual(headers["Oai-Device-Id"], "device-1")
        self.assertEqual(headers["Origin"], "https://chatgpt.com")
        self.assertEqual(headers["Sec-Fetch-Mode"], "cors")
        self.assertTrue(headers["Oai-Session-Id"])
        self.assertEqual(headers["X-Oai-Is-Client-Observation"], "false")
        self.assertEqual(session.calls[0][2]["timeout"], 30)


class TeamClientMembersTest(unittest.TestCase):
    def test_list_members_deduplicates_and_reports_partial_coverage(self) -> None:
        from webui.team import TeamClient

        session = ScriptedSession(
            Response(payload={"users": [{"id": "u1"}, {"id": "u2"}], "total": 4}),
            Response(payload={"users": [{"id": "u2"}, {"id": "u3"}], "total": 4}),
        )
        result = TeamClient({
            "workspace_id": "ws-1", "owner_access_token": "at",
        }, session=session, page_size=2).list_members()

        self.assertEqual([member["id"] for member in result["members"]], ["u1", "u2", "u3"])
        self.assertEqual(result["total"], 4)
        self.assertEqual(result["coverage"], 0.75)

    def test_unknown_member_envelope_cannot_claim_full_coverage(self) -> None:
        from webui.team import TeamClient, TeamServiceError

        client = TeamClient({
            "workspace_id": "ws-1", "owner_access_token": "at",
        }, session=ScriptedSession(Response(payload={"unexpected": []})))

        with self.assertRaises(TeamServiceError) as caught:
            client.list_members()
        self.assertEqual(caught.exception.category, "unsupported_schema")

    def test_snapshot_keeps_member_and_billing_sources_separate(self) -> None:
        from webui.team import TeamClient

        session = ScriptedSession(
            Response(payload={"users": [{"id": "u1"}], "total": 1}),
            Response(payload={"seat_type_counts": {"default": 3}}),
            Response(payload={"id": "sub-1", "seats_in_use": 2}),
            Response(payload={"accounts": {"ws-1": {"account": {"is_deactivated": False}}}}),
        )

        result = TeamClient({
            "workspace_id": "ws-1", "owner_access_token": "at",
        }, session=session).snapshot()

        self.assertEqual(result["members"]["total"], 1)
        self.assertEqual(result["seat_counts"], {"default": 3})
        self.assertEqual(result["subscription"]["id"], "sub-1")
        self.assertIn("ws-1", result["accounts"]["accounts"])


class TeamClientInviteMutationTest(unittest.TestCase):
    def test_send_invites_enforces_batch_bounds_and_preserves_remote_detail(self) -> None:
        from webui.team import TeamClient

        session = ScriptedSession(
            Response(payload={"seat_type_counts": {"default": 1}}),
            Response(payload={
                "account_invites": [{"email_address": "a@example.com"}],
                "errored_emails": [{"email": "b@example.com", "error": "blocked"}],
            }),
        )
        client = TeamClient({
            "workspace_id": "ws-1", "owner_access_token": "at",
        }, session=session)

        result = client.send_invites(["A@example.com", "b@example.com"])

        body = session.calls[1][2]["json"]
        self.assertEqual(body["email_addresses"], ["a@example.com", "b@example.com"])
        self.assertEqual(body["role"], "standard-user")
        self.assertEqual(result["seat_preflight"], {"default": 1})
        self.assertEqual(result["errored_emails"][0]["error"], "blocked")
        with self.assertRaisesRegex(ValueError, "1..100"):
            client.send_invites([])

    def test_send_invites_chunks_twenty_five_and_retries_429(self) -> None:
        from webui.team import TeamClient

        session = ScriptedSession(
            Response(payload={"seat_type_counts": {"default": 10}}),
            Response(status_code=429, payload={"error": "slow"}),
            Response(payload={"account_invites": [{"id": "first"}], "errored_emails": []}),
            Response(payload={"account_invites": [{"id": "second"}], "errored_emails": []}),
        )
        sleeps = []
        client = TeamClient({
            "workspace_id": "ws-1", "owner_access_token": "at",
        }, session=session, sleep_fn=sleeps.append, clock_fn=lambda: 0)

        result = client.send_invites([f"u{i}@example.com" for i in range(26)])

        post_calls = [call for call in session.calls if call[0] == "POST"]
        self.assertEqual([len(call[2]["json"]["email_addresses"]) for call in post_calls], [25, 25, 1])
        self.assertEqual(sleeps, [3])
        self.assertEqual([row["id"] for row in result["account_invites"]], ["first", "second"])

    def test_delete_invite_uses_email_not_invite_id(self) -> None:
        from webui.team import TeamClient

        session = ScriptedSession(Response(payload={"deleted": True}))
        client = TeamClient({
            "workspace_id": "ws-1", "owner_access_token": "at",
        }, session=session)

        self.assertTrue(client.delete_invite(" Dead@Example.com ")["deleted"])
        self.assertEqual(session.calls[0][2]["json"], {"email_address": "dead@example.com"})

    def test_accept_join_request_sets_standard_default_seat(self) -> None:
        from webui.team import TeamClient

        session = ScriptedSession(Response(payload={"accepted": True}))
        client = TeamClient({
            "workspace_id": "ws-1", "owner_access_token": "at",
        }, session=session)

        result = client.accept_join_request("invite-7")

        self.assertTrue(result["accepted"])
        self.assertTrue(session.calls[0][1].endswith("/invites/invite-7"))
        self.assertEqual(session.calls[0][2]["json"], {
            "accept_request": True, "role": "standard-user", "seat_type": "default",
        })


class TeamClientJoinTest(unittest.TestCase):
    def test_exchange_workspace_token_uses_cookie_session_without_stale_account_headers(self) -> None:
        from webui.team import TeamClient

        session = ScriptedSession(Response(payload={"accessToken": "workspace-at"}))
        result = TeamClient({
            "workspace_id": "ws-1", "owner_access_token": "stale-personal-at",
        }, session=session).exchange_workspace_token()

        self.assertEqual(result["access_token"], "workspace-at")
        headers = session.calls[0][2]["headers"]
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Chatgpt-Account-Id", headers)
        self.assertEqual(headers["Referer"], "https://chatgpt.com/")
        self.assertEqual(headers["X-Openai-Target-Path"], "/api/auth/session")
        self.assertEqual(headers["X-Openai-Target-Route"], "/api/auth/session")

    def test_join_workspace_runs_strict_four_step_protocol(self) -> None:
        from webui.team import TeamClient

        session = ScriptedSession(
            Response(payload={"requested": True}),
            Response(payload={"accepted": True}),
            Response(payload={"accounts": {"ws-1": {}}}),
            Response(payload={"accessToken": "workspace-at", "user": {"email": "a@example.com"}}),
        )
        result = TeamClient({
            "workspace_id": "ws-1", "owner_access_token": "personal-at",
        }, session=session).join_workspace()

        self.assertEqual(result["access_token"], "workspace-at")
        self.assertEqual([call[0] for call in session.calls], ["POST", "POST", "GET", "GET"])
        self.assertEqual(
            [call[1].split("chatgpt.com", 1)[1] for call in session.calls],
            [
                "/backend-api/accounts/ws-1/invites/request",
                "/backend-api/accounts/ws-1/invites/accept",
                "/backend-api/accounts/check/v4-2023-04-27",
                "/api/auth/session",
            ],
        )
        self.assertEqual(session.calls[3][2]["params"]["workspace_id"], "ws-1")


class TeamClientRemoveMemberTest(unittest.TestCase):
    def test_remove_member_retries_409_then_verifies_authoritative_snapshot(self) -> None:
        from webui.team import TeamClient

        session = ScriptedSession(
            Response(status_code=409, payload={"error": "busy-1"}),
            Response(status_code=409, payload={"error": "busy-2"}),
            Response(payload={"deleted": True}),
            Response(payload={"users": [], "total": 0}),
        )
        sleeps = []
        client = TeamClient({
            "workspace_id": "ws-1", "owner_access_token": "owner-at",
        }, session=session, sleep_fn=sleeps.append)

        result = client.remove_member("user-1", access_token="member-workspace-at")

        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(sleeps, [5, 10])
        self.assertEqual(
            session.calls[0][2]["headers"]["Authorization"],
            "Bearer member-workspace-at",
        )


class TeamClientUsageTest(unittest.TestCase):
    def test_inventory_policy_keeps_five_hour_and_long_windows_separate(self) -> None:
        from webui.team import TeamClient

        session = ScriptedSession(
            Response(payload="loc=US\ncolo=SJC"),
            Response(payload={
                "rate_limit": {
                    "allowed": True,
                    "limit_reached": False,
                    "primary_window": {
                        "used_percent": 100,
                        "reset_after_seconds": 60,
                        "limit_window_seconds": 18_000,
                    },
                    "secondary_window": {
                        "used_percent": 90,
                        "reset_after_seconds": 600,
                        "limit_window_seconds": 604_800,
                    },
                },
            }),
            Response(payload={"plan_type": "team"}),
        )
        result = TeamClient({
            "workspace_id": "ws-1", "owner_access_token": "owner",
        }, session=session).probe_usage("workspace-at")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["pct_5h"], 100)
        self.assertEqual(result["pct_7d"], 90)
        self.assertTrue(result["short_window_limited"])
        self.assertEqual(
            session.calls[1][2]["headers"]["Authorization"], "Bearer workspace-at",
        )
        self.assertNotIn("X-Openai-Target-Path", session.calls[1][2]["headers"])
        self.assertNotIn("X-Openai-Target-Route", session.calls[1][2]["headers"])

    def test_usage_429_and_long_window_have_distinct_limit_detail(self) -> None:
        from webui.team import TeamClient

        limited = TeamClient({
            "workspace_id": "ws-1", "owner_access_token": "owner",
        }, session=ScriptedSession(
            Response(payload="loc=US"),
            Response(status_code=429, payload={"error": "slow down"}),
        ))
        self.assertEqual(limited.probe_usage("at")["error_code"], "rate_limited")

        long_window = TeamClient({
            "workspace_id": "ws-1", "owner_access_token": "owner",
        }, session=ScriptedSession(
            Response(payload="loc=US"),
            Response(payload={
                "rate_limit": {
                    "allowed": True,
                    "secondary_window": {
                        "used_percent": 100,
                        "reset_after_seconds": 700,
                        "limit_window_seconds": 604_800,
                    },
                },
            }),
            Response(payload={"plan_type": "team"}),
        )).probe_usage("at")
        self.assertEqual(long_window["status"], "limit_reached")
        self.assertEqual(long_window["quota_window"], "7d")
        self.assertEqual(long_window["reset_after_seconds"], 700)

    def test_inventory_policy_temporarily_exhausts_unknown_full_window(self) -> None:
        from webui.team import TeamClient

        result = TeamClient({
            "workspace_id": "ws-1", "owner_access_token": "owner",
        }, session=ScriptedSession(
            Response(payload="loc=US"),
            Response(payload={
                "rate_limit": {
                    "allowed": True,
                    "windows": [{"used_percent": 100}],
                },
            }),
            Response(payload={"plan_type": "team"}),
        )).probe_usage("at", policy="inventory")

        self.assertEqual(result["status"], "limit_reached")
        self.assertEqual(result["quota_window"], "unknown")

    def test_usage_html_403_is_cloudflare_not_oauth_failure(self) -> None:
        from webui.team import TeamClient

        result = TeamClient({
            "workspace_id": "ws-1", "owner_access_token": "owner",
        }, session=ScriptedSession(
            Response(payload="loc=US"),
            Response(status_code=403, payload=ValueError("not json"), text="<html>challenge</html>"),
        )).probe_usage("at")

        self.assertEqual(result["status"], "forbidden")
        self.assertEqual(result["error_code"], "cloudflare_forbidden")


class Sub2ApiClientTest(unittest.TestCase):
    def test_groups_normalizes_base_once_and_requires_code_zero(self) -> None:
        from webui.team import Sub2ApiClient, TeamServiceError

        session = ScriptedSession(Response(payload={
            "code": 0, "msg": "", "data": [{"id": 7, "name": "default"}],
        }))
        client = Sub2ApiClient({
            "sub2api_url": "https://sub.example/api/v1/",
            "sub2api_api_key": "secret",
        }, transport=session)

        result = client.groups()

        self.assertEqual(result["groups"][0]["id"], 7)
        self.assertEqual(session.calls[0][1], "https://sub.example/api/v1/admin/groups")
        self.assertEqual(session.calls[0][2]["headers"]["X-Api-Key"], "secret")

        failing = Sub2ApiClient({
            "sub2api_url": "https://sub.example", "sub2api_api_key": "secret",
        }, transport=ScriptedSession(Response(payload={"code": 9, "msg": "denied"})))
        with self.assertRaises(TeamServiceError) as caught:
            failing.groups()
        self.assertEqual(caught.exception.detail["code"], 9)

        missing_envelope = Sub2ApiClient({
            "sub2api_url": "https://sub.example", "sub2api_api_key": "secret",
        }, transport=ScriptedSession(Response(payload={"groups": []})))
        with self.assertRaisesRegex(TeamServiceError, "response envelope"):
            missing_envelope.groups()

    def test_deliver_enforces_managed_invariants_and_readback(self) -> None:
        from webui.team import Sub2ApiClient

        session = ScriptedSession(
            Response(payload={"code": 0, "data": {"id": 42}}),
            Response(payload={"code": 0, "data": {
                "id": 42, "generation": 3, "schedulable": True, "group_ids": [7, 8],
            }}),
            Response(payload={"code": 0, "data": {"applied": True}}),
            Response(payload={"code": 0, "data": {
                "id": 42, "generation": 3, "schedulable": True, "group_ids": [7, 8],
                "credentials": {"model_mapping": {}},
                "credentials_status": {
                    "has_access_token": True,
                    "has_refresh_token": True,
                    "has_id_token": True,
                },
            }}),
            Response(status_code=502, payload={"message": "temporary upstream failure"}),
            Response(payload={"code": 0, "data": {"eligible": True}}),
        )
        client = Sub2ApiClient({
            "sub2api_url": "https://sub.example",
            "sub2api_api_key": "key",
            "sub2api_group_ids": "7,8",
        }, transport=session)

        result = client.deliver(
            "a@example.com",
            {"access_token": "at", "refresh_token": "rt", "id_token": "id"},
            request_id="req-1", binding_id="bind-1", generation=3,
        )

        create = session.calls[0][2]
        self.assertEqual(create["headers"]["Idempotency-Key"], "rotation-create-req-1")
        self.assertEqual(create["json"]["name"], "rotation-bind-1")
        self.assertEqual(create["json"]["model_mapping"], {})
        self.assertEqual(create["json"]["group_ids"], [7, 8])
        self.assertEqual([call[0] for call in session.calls], ["POST", "GET", "POST", "GET", "GET", "GET"])
        self.assertEqual(result["account"]["id"], 42)
        self.assertEqual(result["quota"], {"eligible": True})

    def test_deliver_recovers_create_timeout_by_exact_rotation_name(self) -> None:
        from webui.team import Sub2ApiClient

        timeout = RuntimeError("Failed to perform, curl: (28) Operation timed out")
        session = ScriptedSession(
            timeout,
            Response(payload={"code": 0, "data": {"items": [{
                "id": 42, "name": "rotation-bind-1", "generation": 3,
            }]}}),
            Response(payload={"code": 0, "data": {
                "id": 42, "generation": 3, "schedulable": True, "group_ids": [7],
            }}),
            Response(payload={"code": 0, "data": {"applied": True}}),
            Response(payload={"code": 0, "data": {
                "id": 42, "generation": 3, "schedulable": True, "group_ids": [7],
                "credentials": {
                    "access_token": "at", "refresh_token": "rt", "id_token": "id",
                },
            }}),
            Response(payload={"code": 0, "data": {"eligible": True}}),
        )
        client = Sub2ApiClient({
            "sub2api_url": "https://sub.example", "sub2api_api_key": "key",
            "sub2api_group_ids": "7", "sub2api_timeout": 120,
        }, transport=session)

        result = client.deliver(
            "a@example.com",
            {"access_token": "at", "refresh_token": "rt", "id_token": "id"},
            request_id="req-1", binding_id="bind-1", generation=3,
        )

        self.assertTrue(result["create_reconciled_after_timeout"])
        self.assertEqual(result["create_attempts"], 1)
        self.assertEqual([call[0] for call in session.calls], ["POST", "GET", "GET", "POST", "GET", "GET"])
        self.assertEqual(session.calls[1][2]["params"]["search"], "rotation-bind-1")

    def test_deliver_retries_create_timeout_with_same_idempotency_key(self) -> None:
        from webui.team import Sub2ApiClient

        timeout = RuntimeError("curl: (28) Operation timed out after 120002 milliseconds")
        session = ScriptedSession(
            timeout,
            Response(payload={"code": 0, "data": {"items": []}}),
            Response(payload={"code": 0, "data": {"id": 42}}),
            Response(payload={"code": 0, "data": {
                "id": 42, "generation": 3, "schedulable": True, "group_ids": [7],
            }}),
            Response(payload={"code": 0, "data": {"applied": True}}),
            Response(payload={"code": 0, "data": {
                "id": 42, "generation": 3, "schedulable": True, "group_ids": [7],
                "credentials": {
                    "access_token": "at", "refresh_token": "rt", "id_token": "id",
                },
            }}),
            Response(payload={"code": 0, "data": {"eligible": True}}),
        )
        client = Sub2ApiClient({
            "sub2api_url": "https://sub.example", "sub2api_api_key": "key",
            "sub2api_group_ids": "7", "sub2api_timeout": 120,
        }, transport=session)

        result = client.deliver(
            "a@example.com",
            {"access_token": "at", "refresh_token": "rt", "id_token": "id"},
            request_id="req-1", binding_id="bind-1", generation=3,
        )

        posts = [call for call in session.calls if call[0] == "POST" and call[1].endswith("/admin/accounts")]
        self.assertEqual(result["create_attempts"], 2)
        self.assertEqual(len(posts), 2)
        self.assertEqual(
            [call[2]["headers"]["Idempotency-Key"] for call in posts],
            ["rotation-create-req-1", "rotation-create-req-1"],
        )

    def test_deliver_reconciles_409_by_unique_email_and_adopts_groups(self) -> None:
        from webui.team import Sub2ApiClient

        legacy = {
            "id": 42, "name": "legacy-account",
            "credentials": {"email": "a@example.com"},
            "extra": {"kept": "yes"},
        }
        session = ScriptedSession(
            Response(status_code=409, payload={"message": "account already exists"}),
            Response(payload={"code": 0, "data": {"items": []}}),
            Response(payload={"code": 0, "data": {"items": [legacy]}}),
            Response(payload={"code": 0, "data": legacy}),
            Response(payload={"code": 0, "data": {"id": 42}}),
            Response(payload={"code": 0, "data": {
                "id": 42, "generation": 3, "schedulable": True, "group_ids": [7],
            }}),
            Response(payload={"code": 0, "data": {"applied": True}}),
            Response(payload={"code": 0, "data": {
                "id": 42, "generation": 3, "schedulable": True, "group_ids": [7],
                "credentials_status": {
                    "has_access_token": True,
                    "has_refresh_token": True,
                    "has_id_token": True,
                },
            }}),
            Response(payload={"code": 0, "data": {"eligible": True}}),
        )
        client = Sub2ApiClient({
            "sub2api_url": "https://sub.example", "sub2api_api_key": "key",
            "sub2api_group_ids": "7",
        }, transport=session)

        result = client.deliver(
            "a@example.com",
            {"access_token": "at", "refresh_token": "rt", "id_token": "id"},
            request_id="req-1", binding_id="bind-1", generation=3,
        )

        self.assertTrue(result["create_reconciled_after_conflict"])
        self.assertEqual(result["account"]["id"], 42)
        update = next(call for call in session.calls if call[0] == "PUT")
        self.assertEqual(update[2]["json"]["group_ids"], [7])
        self.assertEqual(update[2]["json"]["extra"], {
            "kept": "yes", "email": "a@example.com",
            "binding_id": "bind-1", "generation": 3,
        })

    def test_deliver_labels_apply_stage_failures(self) -> None:
        from webui.team import Sub2ApiClient, TeamServiceError

        session = ScriptedSession(
            Response(payload={"code": 0, "data": {"id": 42}}),
            Response(payload={"code": 0, "data": {
                "id": 42, "generation": 3, "schedulable": True, "group_ids": [7],
            }}),
            RuntimeError("apply connection failed"),
        )
        client = Sub2ApiClient({
            "sub2api_url": "https://sub.example", "sub2api_api_key": "key",
            "sub2api_group_ids": "7",
        }, transport=session)

        with self.assertRaises(TeamServiceError) as caught:
            client.deliver(
                "a@example.com",
                {"access_token": "at", "refresh_token": "rt", "id_token": "id"},
                request_id="req-1", binding_id="bind-1", generation=3,
            )

        self.assertEqual(caught.exception.category, "sub2api_apply_error")
        self.assertEqual(caught.exception.detail["stage"], "apply")
        self.assertEqual(caught.exception.detail["remote_account_id"], "42")

    def test_deliver_requires_workspace_specific_group_ids(self) -> None:
        from webui.team import Sub2ApiClient

        client = Sub2ApiClient({
            "sub2api_url": "https://sub.example", "sub2api_api_key": "key",
            "sub2api_group_ids": "",
        }, transport=ScriptedSession())
        with self.assertRaisesRegex(ValueError, "group"):
            client.deliver(
                "a@example.com",
                {"access_token": "at", "refresh_token": "rt", "id_token": "id"},
                request_id="req", binding_id="binding", generation=1,
            )


class AccountOAuthTest(unittest.TestCase):
    def test_account_oauth_reuses_a_verified_saved_session(self) -> None:
        from auth_flow import AuthResult
        from webui.team import account_oauth

        stored = {
            "email": "a@example.com", "password": "pw", "totp_secret": "totp",
            "access_token": "saved-at", "session_token": "saved-st",
            "refresh_token": "saved-rt", "id_token": "saved-id", "device_id": "did",
        }
        flow = Mock()
        flow.result = AuthResult()
        flow.session = ScriptedSession(Response(payload={"accessToken": "workspace-at"}))
        flow.run_protocol_login.side_effect = AssertionError("must reuse saved session")

        with (
            patch("webui.team.db.get_registered", return_value=stored),
            patch("webui.team.db.get_account", return_value=None),
            patch("webui.team.db.save_registered") as save_registered,
            patch("webui.team.create_mail_provider") as create_provider,
            patch("webui.team.AuthFlow", return_value=flow),
        ):
            result = account_oauth(
                {"workspace_id": "ws-1", "proxy": "socks5://p"},
                "A@example.com",
            )

        flow.from_existing_credentials.assert_not_called()
        flow.run_protocol_login.assert_not_called()
        create_provider.assert_not_called()
        self.assertEqual(result["workspace_access_token"], "workspace-at")
        self.assertEqual(result["credentials"]["access_token"], "saved-at")
        self.assertEqual(result["credentials"]["session_token"], "saved-st")
        self.assertEqual(result["credentials"]["refresh_token"], "saved-rt")
        self.assertEqual(result["credentials"]["id_token"], "saved-id")
        self.assertEqual(save_registered.call_args.args[0]["totp_secret"], "totp")

    def test_account_oauth_falls_back_when_saved_session_cannot_refresh_access(self) -> None:
        from webui.team import account_oauth

        stored = {
            "email": "a@example.com", "password": "pw",
            "access_token": "stale-at", "session_token": "stale-st", "device_id": "did",
        }
        restore_flow = Mock(session=ScriptedSession(Response(payload={"WARNING_BANNER": {}})))
        login_flow = Mock(session=ScriptedSession(Response(payload={"accessToken": "workspace-at"})))
        from auth_flow import AuthResult
        logged_in = AuthResult()
        logged_in.email = "a@example.com"
        logged_in.password = "pw"
        logged_in.access_token = "new-at"
        logged_in.session_token = "new-st"
        login_flow.run_protocol_login.return_value = logged_in

        with (
            patch("webui.team.db.get_registered", return_value=stored),
            patch("webui.team.db.get_account", return_value={"email": "a@example.com", "kind": "outlook"}),
            patch("webui.team.db.get_mail_settings", return_value={"mail_source": "outlook"}),
            patch("webui.team.db.save_registered"),
            patch("webui.team.create_mail_provider", return_value=object()),
            patch("webui.team.AuthFlow", side_effect=[restore_flow, login_flow]),
        ):
            result = account_oauth(
                {"workspace_id": "ws-1"}, "a@example.com",
            )

        login_flow.run_protocol_login.assert_called_once()
        self.assertEqual(result["workspace_access_token"], "workspace-at")
        self.assertEqual(result["credentials"]["access_token"], "new-at")
        self.assertEqual(result["credentials"]["session_token"], "new-st")

    def test_account_oauth_reuses_login_session_and_keeps_personal_token_in_db(self) -> None:
        from webui.team import account_oauth

        flow = Mock()
        flow.session = ScriptedSession(Response(payload={"accessToken": "workspace-at"}))
        auth_result = Mock()
        auth_result.to_dict.return_value = {
            "email": "a@example.com", "password": "pw",
            "access_token": "personal-at", "refresh_token": "rt", "id_token": "id",
            "session_token": "st", "device_id": "did",
        }
        flow.run_protocol_login.return_value = auth_result
        auth_flow_cls = Mock(return_value=flow)

        with (
            patch("webui.team.db.get_registered", return_value={
                "email": "a@example.com", "password": "pw",
                "extra": {"plus_check": {"status": "free"}},
            }),
            patch("webui.team.db.get_account", return_value={"email": "a@example.com", "kind": "outlook"}),
            patch("webui.team.db.get_mail_settings", return_value={"mail_source": "outlook"}),
            patch("webui.team.db.save_registered") as save_registered,
            patch("webui.team.create_mail_provider", return_value=object()),
            patch("webui.team.AuthFlow", auth_flow_cls),
        ):
            result = account_oauth({"workspace_id": "ws-1", "proxy": "socks5://p"}, "A@example.com")

        self.assertEqual(result["workspace_access_token"], "workspace-at")
        self.assertEqual(save_registered.call_args.args[0]["access_token"], "personal-at")
        self.assertNotIn("extra", save_registered.call_args.args[0])
        self.assertEqual(
            save_registered.call_args.args[0]["plus_check"]["status"], "free",
        )
        self.assertEqual(
            auth_flow_cls.call_args.kwargs["env_overrides"]["OAUTH_ALLOWED_WORKSPACE_ID"],
            "ws-1",
        )
        self.assertEqual(
            auth_flow_cls.call_args.kwargs["env_overrides"]["TEAM_SKIP_ADD_PHONE"],
            "1",
        )
        self.assertEqual(flow.session.calls[0][1], "https://chatgpt.com/api/auth/session")


class BatchOrchestrationTest(unittest.TestCase):
    def test_auto_join_accounts_isolates_each_account_failure(self) -> None:
        from webui.team import auto_join_accounts

        class FakeTeamClient:
            def __init__(self, workspace, session=None):
                self.workspace = workspace

            def join_workspace(self):
                return {"access_token": "workspace-at"}

            def exchange_workspace_token(self):
                return {"access_token": "workspace-at"}

            def list_members(self):
                return {
                    "members": [{"id": "u1", "email": "good@example.com"}],
                    "total": 1, "coverage": 1.0,
                }

            def probe_usage(self, access_token):
                return {"status": "ok", "pct_5h": 10, "pct_7d": 20}

        deliver = Mock(return_value={"ok": True, "account": {"id": 9}})
        fake_sub2 = Mock()
        fake_sub2.deliver = deliver
        oauth_ok = {
            "credentials": {
                "access_token": "personal-at", "refresh_token": "rt", "id_token": "id",
                "device_id": "did",
            },
            "workspace_access_token": "workspace-at",
            "_session": object(),
        }

        with (
            patch("webui.team.account_oauth", side_effect=[oauth_ok, RuntimeError("login failed")]),
            patch("webui.team.TeamClient", FakeTeamClient),
            patch("webui.team.Sub2ApiClient", return_value=fake_sub2),
        ):
            result = auto_join_accounts(
                {"workspace_id": "ws-1", "owner_access_token": "owner"},
                ["good@example.com", "bad@example.com"],
            )

        self.assertEqual([row["ok"] for row in result["results"]], [True, False])
        self.assertEqual(result["results"][1]["email"], "bad@example.com")
        self.assertEqual(deliver.call_args.args[1]["access_token"], "workspace-at")

    def test_auto_join_does_not_deliver_an_unusable_account(self) -> None:
        from webui.team import auto_join_accounts

        class FakeTeamClient:
            def __init__(self, workspace, session=None):
                pass

            def join_workspace(self):
                return {"access_token": "workspace-at"}

            def exchange_workspace_token(self):
                return {"access_token": "workspace-at"}

            def list_members(self):
                return {
                    "members": [{"id": "u1", "email": "full@example.com"}],
                    "total": 1, "coverage": 1.0,
                }

            def probe_usage(self, access_token):
                return {"status": "limit_reached", "quota_window": "7d"}

        oauth = {
            "credentials": {
                "access_token": "personal-at", "refresh_token": "rt", "id_token": "id",
            },
            "workspace_access_token": "workspace-at",
            "_session": object(),
        }
        deliver = Mock()
        fake_sub2 = Mock(deliver=deliver)
        with (
            patch("webui.team.account_oauth", return_value=oauth),
            patch("webui.team.TeamClient", FakeTeamClient),
            patch("webui.team.Sub2ApiClient", return_value=fake_sub2),
        ):
            result = auto_join_accounts(
                {"workspace_id": "ws-1", "owner_access_token": "owner"},
                ["full@example.com"],
            )

        self.assertFalse(result["results"][0]["ok"])
        self.assertEqual(result["results"][0]["error"]["category"], "usage_not_deliverable")
        deliver.assert_not_called()

    def test_auto_join_existing_member_skips_join_request(self) -> None:
        from webui.team import auto_join_accounts

        class FakeTeamClient:
            def __init__(self, workspace, session=None):
                pass

            def list_members(self):
                return {
                    "members": [{"id": "u1", "email": "member@example.com"}],
                    "total": 1, "coverage": 1.0,
                }

            def join_workspace(self):
                raise AssertionError("existing member must not request access again")

            def exchange_workspace_token(self):
                return {"access_token": "workspace-at"}

            def probe_usage(self, access_token):
                return {"status": "ok", "pct_5h": 10, "pct_7d": 20}

        oauth = {
            "credentials": {
                "access_token": "personal-at", "refresh_token": "rt", "id_token": "id",
            },
            "workspace_access_token": "workspace-at",
            "_session": object(),
        }
        deliver = Mock(return_value={"ok": True, "account": {"id": 9}})
        with (
            patch("webui.team.account_oauth", return_value=oauth),
            patch("webui.team.TeamClient", FakeTeamClient),
            patch("webui.team.Sub2ApiClient", return_value=Mock(deliver=deliver)),
        ):
            result = auto_join_accounts(
                {"workspace_id": "ws-1", "owner_access_token": "owner"},
                ["member@example.com"],
            )

        self.assertTrue(result["results"][0]["ok"])
        self.assertEqual(deliver.call_args.args[1]["access_token"], "workspace-at")

    def test_push_members_skips_join_and_delivers_workspace_credentials(self) -> None:
        from webui.team import push_members_to_sub2api

        class FakeTeamClient:
            def __init__(self, workspace, session=None):
                self.workspace = workspace

            def list_members(self):
                return {
                    "members": [{"id": "u1", "email": "member@example.com"}],
                    "total": 1, "coverage": 1.0,
                }

            def probe_usage(self, access_token):
                if access_token != "workspace-at":
                    raise AssertionError(f"unexpected access token: {access_token!r}")
                return {"status": "ok", "pct_5h": 10, "pct_7d": 20}

        deliver = Mock(return_value={"ok": True, "account": {"id": 42}})
        oauth = {
            "credentials": {
                "access_token": "personal-at", "refresh_token": "rt", "id_token": "id",
            },
            "workspace_access_token": "workspace-at",
            "_session": object(),
        }
        with (
            patch("webui.team.account_oauth", return_value=oauth),
            patch("webui.team.TeamClient", FakeTeamClient),
            patch("webui.team.Sub2ApiClient", return_value=Mock(deliver=deliver)),
        ):
            result = push_members_to_sub2api(
                {"workspace_id": "ws-1", "owner_access_token": "owner"},
                [{
                    "email": "member@example.com", "binding_id": "binding-1",
                    "request_id": "request-1", "generation": 3,
                }],
            )

        self.assertTrue(result["results"][0]["ok"])
        self.assertEqual(deliver.call_args.args[1]["access_token"], "workspace-at")
        self.assertEqual(deliver.call_args.kwargs, {
            "request_id": "request-1", "binding_id": "binding-1", "generation": 3,
        })

    def test_push_members_rejects_account_missing_from_current_snapshot(self) -> None:
        from webui.team import push_members_to_sub2api

        remote = Mock()
        remote.list_members.return_value = {
            "members": [], "total": 0, "coverage": 1.0,
        }
        with (
            patch("webui.team.TeamClient", return_value=remote),
            patch("webui.team.account_oauth") as oauth,
        ):
            result = push_members_to_sub2api(
                {"workspace_id": "ws-1", "owner_access_token": "owner"},
                ["missing@example.com"],
            )

        self.assertFalse(result["results"][0]["ok"])
        self.assertEqual(result["results"][0]["error"]["category"], "member_not_found")
        oauth.assert_not_called()

    def test_push_remote_only_member_accepts_explicit_oauth_credentials(self) -> None:
        from webui.team import push_members_to_sub2api

        class FakeTeamClient:
            def __init__(self, workspace, session=None):
                pass

            def list_members(self):
                return {
                    "members": [{
                        "id": "u1", "account_user_id": "au1",
                        "email": "remote@example.com",
                    }],
                    "total": 1, "coverage": 1.0,
                }

            def probe_usage(self, access_token):
                return {
                    "status": "ok", "pct_5h": 10, "pct_7d": 20,
                    "identity": {"id": "au1", "email": "remote@example.com"},
                }

        deliver = Mock(return_value={"ok": True, "account": {"id": 43}})
        with (
            patch("webui.team.TeamClient", FakeTeamClient),
            patch("webui.team.account_oauth", side_effect=AssertionError("must not log in")),
            patch("webui.team.Sub2ApiClient", return_value=Mock(deliver=deliver)),
        ):
            result = push_members_to_sub2api(
                {"workspace_id": "ws-1", "owner_access_token": "owner"},
                [{
                    "email": "remote@example.com", "binding_id": "binding-2",
                    "request_id": "request-2", "generation": 1,
                    "credentials": {
                        "access_token": "workspace-at", "refresh_token": "rt",
                        "id_token": "id",
                    },
                }],
            )

        self.assertTrue(result["results"][0]["ok"])
        self.assertEqual(deliver.call_args.args[1], {
            "access_token": "workspace-at", "refresh_token": "rt", "id_token": "id",
        })

    def test_push_remote_only_member_rejects_wrong_explicit_identity(self) -> None:
        from webui.team import push_members_to_sub2api

        remote = Mock()
        remote.list_members.return_value = {
            "members": [{"id": "u1", "email": "remote@example.com"}],
            "total": 1, "coverage": 1.0,
        }
        remote.probe_usage.return_value = {
            "status": "ok",
            "identity": {"id": "other", "email": "other@example.com"},
        }
        with (
            patch("webui.team.TeamClient", return_value=remote),
            patch("webui.team.Sub2ApiClient") as sub2api,
        ):
            result = push_members_to_sub2api(
                {"workspace_id": "ws-1", "owner_access_token": "owner"},
                [{
                    "email": "remote@example.com",
                    "credentials": {
                        "access_token": "wrong-at", "refresh_token": "rt", "id_token": "id",
                    },
                }],
            )

        self.assertFalse(result["results"][0]["ok"])
        self.assertEqual(
            result["results"][0]["error"]["category"],
            "credential_identity_mismatch",
        )
        sub2api.assert_not_called()

    def test_offboard_prefers_member_workspace_token_and_isolates_failure(self) -> None:
        from webui.team import offboard_accounts

        calls = []

        class FakeTeamClient:
            def __init__(self, workspace):
                pass

            def list_members(self):
                return {
                    "members": [
                        {"id": "u1", "email": "a@example.com", "role": "member"},
                        {"id": "u2", "email": "b@example.com", "role": "member"},
                    ],
                    "total": 2, "coverage": 1.0,
                }

            def remove_member(self, user_id, access_token=None):
                calls.append((user_id, access_token))
                if user_id == "u2":
                    raise RuntimeError("delete failed")
                return {"ok": True, "verified": True}

        with (
            patch("webui.team.TeamClient", FakeTeamClient),
            patch("webui.team.account_oauth", return_value={
                "workspace_access_token": "fresh-member-at",
            }),
        ):
            result = offboard_accounts(
                {"workspace_id": "ws-1", "owner_access_token": "owner-at"},
                [
                    {"email": "a@example.com", "user_id": "u1", "workspace_access_token": "member-at"},
                    {"email": "b@example.com", "user_id": "u2"},
                ],
            )

        self.assertEqual(calls, [("u1", "member-at"), ("u2", "fresh-member-at")])
        self.assertEqual([row["ok"] for row in result["results"]], [True, False])

    def test_offboard_refuses_owner_even_when_user_id_is_supplied(self) -> None:
        from webui.team import offboard_accounts

        class FakeTeamClient:
            def __init__(self, workspace):
                pass

            def list_members(self):
                return {
                    "members": [{
                        "id": "owner-1", "email": "owner@example.com",
                        "role": "account-owner",
                    }],
                    "total": 1, "coverage": 1.0,
                }

            def remove_member(self, user_id, access_token=None):
                raise AssertionError("owner must not be removed")

        with patch("webui.team.TeamClient", FakeTeamClient):
            result = offboard_accounts(
                {"workspace_id": "ws-1", "owner_access_token": "owner-at"},
                [{"email": "owner@example.com", "user_id": "owner-1"}],
            )

        self.assertFalse(result["results"][0]["ok"])
        self.assertEqual(result["results"][0]["error"]["category"], "owner_protected")


if __name__ == "__main__":
    unittest.main()
