from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from webui import db


class TeamWorkspaceDbTest(unittest.TestCase):
    def setUp(self) -> None:
        self._old_path = db.DB_PATH
        self._tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self._tmp.name) / "team-test.db"
        db.init_db()

    def tearDown(self) -> None:
        db.DB_PATH = self._old_path
        self._tmp.cleanup()

    def test_multiple_workspaces_keep_independent_sub2api_config(self) -> None:
        first = db.save_team_workspace({
            "name": "Alpha",
            "workspace_id": "ws-alpha",
            "owner_email": "owner-a@example.com",
            "owner_access_token": "at-alpha",
            "client_build_number": "2026092401",
            "client_version": "release-2026-09-24",
            "sub2api_url": "https://alpha.example",
            "sub2api_api_key": "key-alpha",
            "sub2api_group_ids": "1,2",
        })
        second = db.save_team_workspace({
            "name": "Beta",
            "workspace_id": "ws-beta",
            "owner_email": "owner-b@example.com",
            "owner_access_token": "at-beta",
            "sub2api_url": "https://beta.example/api/v1",
            "sub2api_api_key": "key-beta",
            "sub2api_group_ids": "7",
        })

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(
            [item["workspace_id"] for item in db.list_team_workspaces()],
            ["ws-beta", "ws-alpha"],
        )
        self.assertEqual(db.get_team_workspace(first["id"])["sub2api_group_ids"], "1,2")
        self.assertEqual(db.get_team_workspace(first["id"])["client_build_number"], "2026092401")
        self.assertEqual(db.get_team_workspace(second["id"])["sub2api_group_ids"], "7")

    def test_workspace_update_preserves_omitted_secrets(self) -> None:
        saved = db.save_team_workspace({
            "name": "Alpha",
            "workspace_id": "ws-alpha",
            "owner_access_token": "owner-token",
            "sub2api_api_key": "sub2-key",
        })

        db.save_team_workspace({
            "id": saved["id"],
            "name": "Alpha renamed",
            "workspace_id": "ws-alpha",
        })

        updated = db.get_team_workspace(saved["id"])
        self.assertEqual(updated["name"], "Alpha renamed")
        self.assertEqual(updated["owner_access_token"], "owner-token")
        self.assertEqual(updated["sub2api_api_key"], "sub2-key")

    def test_usage_is_kept_per_workspace_without_losing_other_metadata(self) -> None:
        db.save_registered({
            "email": "member@example.com",
            "access_token": "at",
            "plus_check": {"status": "free"},
        })

        db.update_team_usage("member@example.com", "ws-a", {"status": "ok", "pct_7d": 20})
        db.update_team_usage("member@example.com", "ws-b", {"status": "limit_reached", "pct_7d": 100})

        stored = db.get_registered("member@example.com")["extra"]
        self.assertEqual(stored["plus_check"]["status"], "free")
        self.assertEqual(stored["team_usage"]["ws-a"]["pct_7d"], 20)
        self.assertEqual(stored["team_usage"]["ws-b"]["pct_7d"], 100)

    def test_boarding_binding_reuses_identity_and_request_across_retry(self) -> None:
        workspace = db.save_team_workspace({"name": "Alpha", "workspace_id": "ws-a"})

        first = db.get_or_create_team_binding(workspace["id"], "member@example.com")
        db.update_team_binding(
            workspace["id"], "member@example.com", phase="failed", last_error="network",
        )
        retry = db.get_or_create_team_binding(workspace["id"], "member@example.com")

        self.assertEqual(retry["binding_id"], first["binding_id"])
        self.assertEqual(retry["request_id"], first["request_id"])
        self.assertEqual(retry["generation"], 1)
        self.assertEqual(retry["phase"], "failed")

    def test_refresh_token_rotation_preserves_saved_web_credentials(self) -> None:
        db.save_registered({
            "email": "member@example.com", "access_token": "web-at",
            "refresh_token": "old-rt", "id_token": "old-id", "password": "pw",
        })
        self.assertTrue(db.rotate_registered_oauth_tokens(
            "member@example.com", "old-rt", "new-rt", "new-id",
        ))
        self.assertFalse(db.rotate_registered_oauth_tokens(
            "member@example.com", "old-rt", "stale-rt", "stale-id",
        ))
        stored = db.get_registered("member@example.com")
        self.assertEqual((stored["access_token"], stored["refresh_token"], stored["id_token"]),
                         ("web-at", "new-rt", "new-id"))
        self.assertEqual(stored["password"], "pw")

    def test_remote_member_login_credentials_preserve_oauth_tokens_on_update(self) -> None:
        db.save_team_member_login_credentials(
            "Member@Example.com", openai_password="openai-pw",
            totp_secret="JBSWY3DPEHPK3PXP", mailbox_password="mail-pw",
            mail_client_id="ms-client", mail_refresh_token="m" * 30,
        )
        mailbox = db.get_account("member@example.com")
        self.assertEqual((mailbox["kind"], mailbox["client_id"], mailbox["refresh_token"]),
                         ("outlook", "ms-client", "m" * 30))
        registered = db.get_registered("member@example.com")
        self.assertEqual(registered["password"], "openai-pw")
        self.assertEqual(registered["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertIn("member@example.com", db.list_team_supplemented_emails())

        db.save_registered({**registered, "access_token": "web-at", "refresh_token": "codex-rt"})
        db.save_team_member_login_credentials(
            "member@example.com", openai_password="corrected-pw",
        )
        updated = db.get_registered("member@example.com")
        self.assertEqual((updated["password"], updated["access_token"], updated["refresh_token"]),
                         ("corrected-pw", "web-at", "codex-rt"))
        self.assertEqual(db.get_account("member@example.com")["refresh_token"], "m" * 30)

    def test_remote_member_login_rejects_missing_mailbox_before_writing(self) -> None:
        with self.assertRaisesRegex(ValueError, "Client ID"):
            db.save_team_member_login_credentials("member@example.com", openai_password="pw")
        self.assertIsNone(db.get_registered("member@example.com"))
        self.assertIsNone(db.get_account("member@example.com"))


if __name__ == "__main__":
    unittest.main()
