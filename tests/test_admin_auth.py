"""Administrator authentication at the HTTP boundary."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from webui import db
from webui.app import app


class AdminAuthApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.old_path = db.DB_PATH
        self.tmp = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.tmp.name) / "auth.db"
        db.init_db()
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        db.DB_PATH = self.old_path
        self.tmp.cleanup()

    def test_admin_api_requires_login(self) -> None:
        response = self.client.get("/api/stats")
        self.assertEqual(response.status_code, 401)

    def test_default_admin_can_login_and_read_protected_data(self) -> None:
        self.assertEqual(self.client.post("/api/auth/login", json={
            "username": "admin", "password": "incorrect",
        }).status_code, 401)
        response = self.client.post("/api/auth/login", json={
            "username": "admin", "password": "xvanai666",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.cookies.get("admin_session"))
        self.assertEqual(self.client.get("/api/stats").status_code, 200)

    def test_write_requires_csrf_token(self) -> None:
        login = self.client.post("/api/auth/login", json={
            "username": "admin", "password": "xvanai666",
        })
        self.assertEqual(login.status_code, 200)
        response = self.client.post("/api/accounts/reset_failed")
        self.assertEqual(response.status_code, 403)

    def test_session_can_be_restored_after_page_reload(self) -> None:
        login = self.client.post("/api/auth/login", json={
            "username": "admin", "password": "xvanai666",
        })
        response = self.client.get("/api/auth/session")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["csrf_token"], login.json()["csrf_token"])

    def test_password_change_invalidates_old_session(self) -> None:
        login = self.client.post("/api/auth/login", json={
            "username": "admin", "password": "xvanai666",
        })
        other_client = TestClient(app)
        other_client.post("/api/auth/login", json={
            "username": "admin", "password": "xvanai666",
        })
        csrf = login.json()["csrf_token"]
        rejected = self.client.post("/api/auth/password", json={
            "current_password": "incorrect", "new_password": "new-secret-123",
        }, headers={"x-csrf-token": csrf})
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(self.client.get("/api/stats").status_code, 200)
        response = self.client.post("/api/auth/password", json={
            "current_password": "xvanai666", "new_password": "new-secret-123",
        }, headers={"x-csrf-token": csrf})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/stats").status_code, 401)
        self.assertEqual(other_client.get("/api/stats").status_code, 401)
        other_client.close()
        self.assertEqual(self.client.post("/api/auth/login", json={
            "username": "admin", "password": "xvanai666",
        }).status_code, 401)
        self.assertEqual(self.client.post("/api/auth/login", json={
            "username": "admin", "password": "new-secret-123",
        }).status_code, 200)

    def test_logout_revokes_session(self) -> None:
        login = self.client.post("/api/auth/login", json={
            "username": "admin", "password": "xvanai666",
        })
        response = self.client.post("/api/auth/logout", headers={
            "x-csrf-token": login.json()["csrf_token"],
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.client.get("/api/stats").status_code, 401)
