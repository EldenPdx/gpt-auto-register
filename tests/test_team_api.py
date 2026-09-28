from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from webui import db
from webui.app import app


def _workspace_token(workspace_id: str) -> str:
    payload = {
        "https://api.openai.com/auth": {"chatgpt_account_id": workspace_id},
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"header.{encoded}.signature"


class TeamWorkspaceApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_path = db.DB_PATH
        self._tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self._tmp.name) / "team-api.db"
        db.init_db()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        db.DB_PATH = self._old_path
        self._tmp.cleanup()

    def test_workspace_can_be_created_and_read_without_exposing_secrets(self) -> None:
        response = self.client.post("/api/team/workspaces", json={
            "name": "Alpha",
            "workspace_id": "ws-alpha",
            "owner_email": "owner@example.com",
            "owner_access_token": "owner-secret",
            "proxy": "http://proxy-user:proxy-password@example.test:8080",
            "sub2api_url": "https://sub2.example/api/v1",
            "sub2api_api_key": "sub2-secret",
            "sub2api_group_ids": "12",
        })

        self.assertEqual(response.status_code, 200, response.text)
        created = response.json()["data"]
        self.assertEqual(created["owner_access_token"], "***")
        self.assertEqual(created["proxy"], "***")
        self.assertEqual(created["sub2api_api_key"], "***")

        listed = self.client.get("/api/team/workspaces").json()["items"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["workspace_id"], "ws-alpha")
        self.assertEqual(listed[0]["sub2api_group_ids"], "12")
        self.assertNotIn("owner-secret", response.text)
        self.assertNotIn("sub2-secret", response.text)
        self.assertNotIn("proxy-password", response.text)

    def test_registered_account_can_seed_workspace_owner_credentials(self) -> None:
        db.save_registered({
            "email": "owner@example.com",
            "access_token": _workspace_token("ws-imported"),
            "session_token": "registered-st",
            "device_id": "registered-device",
        })

        response = self.client.post("/api/team/workspaces", json={
            "name": "Imported",
            "workspace_id": "ws-imported",
            "owner_email": "owner@example.com",
            "import_owner_credentials": True,
        })

        self.assertEqual(response.status_code, 200, response.text)
        stored = db.get_team_workspace(response.json()["data"]["id"])
        self.assertEqual(stored["owner_access_token"], _workspace_token("ws-imported"))
        self.assertEqual(stored["owner_session_token"], "registered-st")
        self.assertEqual(stored["owner_device_id"], "registered-device")

    def test_import_does_not_treat_a_personal_or_other_workspace_token_as_owner_token(self) -> None:
        db.save_registered({
            "email": "owner@example.com",
            "access_token": _workspace_token("ws-other"),
            "session_token": "registered-st",
        })

        response = self.client.post("/api/team/workspaces", json={
            "name": "Imported",
            "workspace_id": "ws-target",
            "owner_email": "owner@example.com",
            "import_owner_credentials": True,
        })

        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(db.list_team_workspaces(), [])

    def test_duplicate_workspace_id_is_rejected_without_overwriting_secrets(self) -> None:
        original = db.save_team_workspace({
            "name": "Original",
            "workspace_id": "ws-duplicate",
            "owner_access_token": "keep-owner-token",
            "sub2api_api_key": "keep-sub2-key",
        })

        response = self.client.post("/api/team/workspaces", json={
            "name": "Accidental duplicate",
            "workspace_id": "ws-duplicate",
            "owner_access_token": "",
            "sub2api_api_key": "",
        })

        self.assertEqual(response.status_code, 409, response.text)
        stored = db.get_team_workspace(original["id"])
        self.assertEqual(stored["name"], "Original")
        self.assertEqual(stored["owner_access_token"], "keep-owner-token")
        self.assertEqual(stored["sub2api_api_key"], "keep-sub2-key")

    def test_members_route_exposes_normalized_items(self) -> None:
        workspace = db.save_team_workspace({
            "name": "Alpha", "workspace_id": "ws-alpha",
            "owner_access_token": "owner-token",
        })
        remote = Mock()
        remote.list_members.return_value = {
            "members": [{"id": "u1", "email": "member@example.com", "role": "member"}],
            "total": 1, "coverage": 1.0,
        }

        with patch("webui.app.team.TeamClient", return_value=remote):
            response = self.client.get(f"/api/team/workspaces/{workspace['id']}/members")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["items"][0]["email"], "member@example.com")
        self.assertEqual(response.json()["coverage"], 1.0)

    def test_board_route_never_serializes_tokens_from_service_results(self) -> None:
        workspace = db.save_team_workspace({
            "name": "Alpha", "workspace_id": "ws-alpha",
            "owner_access_token": "owner-token",
            "sub2api_url": "https://sub2.example", "sub2api_api_key": "key",
            "sub2api_group_ids": "7",
        })
        result = {
            "results": [{
                "email": "member@example.com", "ok": True,
                "delivery": {"credentials": {"access_token": "secret-at"}},
            }],
        }

        with patch("webui.app.team.board_accounts", return_value=result) as board:
            response = self.client.post(
                f"/api/team/workspaces/{workspace['id']}/board",
                json={"emails": ["member@example.com"]},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("secret-at", response.text)
        self.assertEqual(response.json()["results"][0]["email"], "member@example.com")
        spec = board.call_args.args[1][0]
        self.assertEqual(spec["email"], "member@example.com")
        self.assertTrue(spec["binding_id"])
        self.assertTrue(spec["request_id"])
        self.assertEqual(spec["generation"], 1)
        self.assertEqual(
            db.get_or_create_team_binding(workspace["id"], "member@example.com")["phase"],
            "complete",
        )

    def test_push_members_route_reuses_workspace_bindings_and_hides_tokens(self) -> None:
        workspace = db.save_team_workspace({
            "name": "Alpha", "workspace_id": "ws-alpha",
            "owner_access_token": "owner-token",
            "sub2api_url": "https://sub2.example", "sub2api_api_key": "key",
            "sub2api_group_ids": "7",
        })
        result = {
            "results": [{
                "email": "member@example.com", "ok": True,
                "delivery": {
                    "account": {"id": 42},
                    "credentials": {"access_token": "secret-at"},
                },
            }],
        }

        with patch("webui.app.team.push_members_to_sub2api", return_value=result) as push:
            response = self.client.post(
                f"/api/team/workspaces/{workspace['id']}/members/push-sub2api",
                json={"emails": ["member@example.com"]},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("secret-at", response.text)
        spec = push.call_args.args[1][0]
        self.assertEqual(spec["email"], "member@example.com")
        self.assertTrue(spec["binding_id"])
        self.assertTrue(spec["request_id"])
        self.assertEqual(spec["generation"], 1)
        binding = db.get_or_create_team_binding(workspace["id"], "member@example.com")
        self.assertEqual(binding["phase"], "complete")
        self.assertEqual(binding["remote_account_id"], "42")

    def test_push_members_route_accepts_ephemeral_remote_oauth_credentials(self) -> None:
        workspace = db.save_team_workspace({
            "name": "Alpha", "workspace_id": "ws-alpha",
            "owner_access_token": "owner-token",
            "sub2api_url": "https://sub2.example", "sub2api_api_key": "key",
            "sub2api_group_ids": "7",
        })
        result = {"results": [{"email": "remote@example.com", "ok": True}]}
        credentials = {
            "access_token": "workspace-at", "refresh_token": "rt", "id_token": "id",
        }

        with patch("webui.app.team.push_members_to_sub2api", return_value=result) as push:
            response = self.client.post(
                f"/api/team/workspaces/{workspace['id']}/members/push-sub2api",
                json={"accounts": [{"email": "remote@example.com", **credentials}]},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("workspace-at", response.text)
        self.assertNotIn("\"rt\"", response.text)
        spec = push.call_args.args[1][0]
        self.assertEqual(spec["credentials"], credentials)
        self.assertEqual(spec["email"], "remote@example.com")

    def test_failed_delivery_still_checkpoints_remote_account_id(self) -> None:
        workspace = db.save_team_workspace({
            "name": "Alpha", "workspace_id": "ws-alpha",
            "owner_access_token": "owner-token",
        })
        result = {"results": [{
            "email": "member@example.com", "ok": False,
            "error": {
                "message": "sub2api verify failed",
                "category": "invalid_readback",
                "detail": {"stage": "verify", "remote_account_id": "42"},
            },
        }]}

        with patch("webui.app.team.push_members_to_sub2api", return_value=result):
            response = self.client.post(
                f"/api/team/workspaces/{workspace['id']}/members/push-sub2api",
                json={"emails": ["member@example.com"]},
            )

        self.assertEqual(response.status_code, 200, response.text)
        binding = db.get_or_create_team_binding(workspace["id"], "member@example.com")
        self.assertEqual(binding["phase"], "failed")
        self.assertEqual(binding["remote_account_id"], "42")

    def test_owner_oauth_refreshes_workspace_token_without_returning_session(self) -> None:
        workspace = db.save_team_workspace({
            "name": "Alpha", "workspace_id": "ws-alpha",
            "owner_email": "owner@example.com", "owner_access_token": "old-token",
        })
        oauth_result = {
            "ok": True,
            "email": "owner@example.com",
            "credentials": {"session_token": "session-secret", "device_id": "device-1"},
            "workspace_access_token": "new-workspace-token",
            "exchange": {"accessToken": "new-workspace-token"},
            "_session": object(),
        }

        with patch("webui.app.team.account_oauth", return_value=oauth_result):
            response = self.client.post(
                f"/api/team/workspaces/{workspace['id']}/accounts/owner%40example.com/oauth",
                json={},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn("new-workspace-token", response.text)
        self.assertNotIn("session-secret", response.text)
        self.assertEqual(
            db.get_team_workspace(workspace["id"])["owner_access_token"],
            "new-workspace-token",
        )

    def test_reuse_export_sub2api_keeps_workspace_group_selection(self) -> None:
        workspace = db.save_team_workspace({
            "name": "Alpha",
            "workspace_id": "ws-alpha",
            "sub2api_group_ids": "7,8",
        })
        db.save_export_config({
            "sub2api_url": "https://shared-sub2.example/api/v1",
            "sub2api_api_key": "shared-secret",
            "sub2api_group_ids": "99",
        })

        response = self.client.post(
            f"/api/team/workspaces/{workspace['id']}/sub2api/reuse-export-config",
            json={},
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["data"]["sub2api_api_key"], "***")
        stored = db.get_team_workspace(workspace["id"])
        self.assertEqual(stored["sub2api_url"], "https://shared-sub2.example/api/v1")
        self.assertEqual(stored["sub2api_api_key"], "shared-secret")
        self.assertEqual(stored["sub2api_group_ids"], "7,8")


if __name__ == "__main__":
    unittest.main()
