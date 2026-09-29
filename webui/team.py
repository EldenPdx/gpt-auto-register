"""Team workspace protocol services.

External HTTP is deliberately exposed through injected sessions/transports so routes can
orchestrate the workflow without owning protocol details.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import quote

from auth_flow import AuthFlow
from config import Config
from http_client import USER_AGENT, create_http_session
from mail_providers import create_mail_provider
from webui import db


CHATGPT_BASE = "https://chatgpt.com"


class TeamServiceError(RuntimeError):
    """Structured error safe for route and batch boundaries."""

    def __init__(self, message: str, *, category: str = "team_error",
                 status_code: int | None = None, detail: Any = None):
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.detail = detail

    def to_dict(self) -> dict:
        return {
            "message": str(self),
            "category": self.category,
            "status_code": self.status_code,
            "detail": self.detail,
        }


def _response_detail(response) -> Any:
    try:
        return response.json()
    except Exception:
        return (getattr(response, "text", "") or "")[:2000]


def _http_category(status_code: int) -> str:
    return {
        401: "auth_error",
        403: "forbidden",
        409: "conflict",
        429: "rate_limited",
    }.get(status_code, "upstream_error")


class TeamClient:
    def __init__(self, workspace: dict, session=None, sleep_fn=time.sleep,
                 page_size: int = 100, clock_fn=time.time,
                 invite_retry_window: float = 300):
        self.workspace = dict(workspace or {})
        self.workspace_id = str(self.workspace.get("workspace_id") or "").strip()
        self.access_token = str(self.workspace.get("owner_access_token") or "").strip()
        if not self.workspace_id:
            raise ValueError("workspace_id is required")
        self.session = session or create_http_session(
            proxy=(self.workspace.get("proxy") or None), user_agent=USER_AGENT,
        )
        self.sleep_fn = sleep_fn
        self.clock_fn = clock_fn
        self.invite_retry_window = max(0.0, float(invite_retry_window))
        self.page_size = max(1, min(int(page_size), 100))
        self.session_id = str(self.workspace.get("session_id") or uuid.uuid4())
        self.timeout = int(self.workspace.get("team_timeout") or 30)

    def _headers(self, path: str, *, access_token: str | None = None,
                 referer: str = f"{CHATGPT_BASE}/admin/members") -> dict:
        token = self.access_token if access_token is None else str(access_token).strip()
        if "/invites" in path and referer == f"{CHATGPT_BASE}/admin/members":
            referer = f"{CHATGPT_BASE}/admin/members?tab=members"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Origin": CHATGPT_BASE,
            "Referer": referer,
            "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Dest": "empty",
            "Oai-Language": "en-US",
            "Priority": "u=1, i",
            "Oai-Session-Id": self.session_id,
            "X-Oai-Is-Client-Observation": "false",
            "X-Oai-Is-Pending-Updates": "false",
            "Chatgpt-Account-Id": self.workspace_id,
            "X-Openai-Target-Path": path,
            "X-Openai-Target-Route": path,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        device_id = str(self.workspace.get("owner_device_id") or "").strip()
        if device_id:
            headers["Oai-Device-Id"] = device_id
        if self.workspace.get("client_build_number"):
            headers["Oai-Client-Build-Number"] = str(self.workspace["client_build_number"])
        if self.workspace.get("client_version"):
            headers["Oai-Client-Version"] = str(self.workspace["client_version"])
        return headers

    def _request(self, method: str, path: str, *, access_token: str | None = None,
                 **kwargs):
        url = path if path.startswith("http") else f"{CHATGPT_BASE}{path}"
        headers = self._headers(path, access_token=access_token)
        for key, value in (kwargs.pop("headers", {}) or {}).items():
            if value is None:
                headers.pop(key, None)
            else:
                headers[key] = value
        kwargs.setdefault("timeout", self.timeout)
        request_fn = getattr(self.session, method.lower(), None)
        if not callable(request_fn):
            request_fn = lambda target, **options: self.session.request(method, target, **options)
        response = request_fn(url, headers=headers, **kwargs)
        status = int(getattr(response, "status_code", 0) or 0)
        detail = _response_detail(response)
        if not 200 <= status < 300:
            raise TeamServiceError(
                f"Team upstream HTTP {status}", category=_http_category(status),
                status_code=status, detail=detail,
            )
        return detail

    @staticmethod
    def _page(payload: Any) -> tuple[list[dict], int | None]:
        if isinstance(payload, list):
            return payload, None
        if not isinstance(payload, dict):
            return [], None
        body = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        for key in ("items", "users", "members", "account_invites", "invites", "requests"):
            if isinstance(body.get(key), list):
                items = body[key]
                break
        else:
            if isinstance(body.get("data"), list):
                items = body["data"]
            else:
                raise TeamServiceError(
                    "unrecognized paginated response envelope",
                    category="unsupported_schema", detail=payload,
                )
        total = next(
            (body[key] for key in ("total", "count", "total_count")
             if isinstance(body.get(key), int)),
            None,
        )
        return items, total

    def list_invites(self, include_requests: bool = False) -> dict:
        path = f"/backend-api/accounts/{self.workspace_id}/invites"
        items: list[dict] = []
        total = None
        offset = 0
        while True:
            params = {"offset": offset, "limit": self.page_size}
            if include_requests:
                params.update({"include_pending": "false", "include_requests": "true", "query": ""})
            page, reported_total = self._page(self._request("GET", path, params=params))
            items.extend(page)
            total = reported_total if reported_total is not None else total
            offset += len(page)
            if not page or (total is not None and offset >= total) or len(page) < self.page_size:
                break
        return {"items": items, "total": total if total is not None else len(items)}

    def list_members(self) -> dict:
        path = f"/backend-api/accounts/{self.workspace_id}/users"
        members: list[dict] = []
        seen: set[str] = set()
        total = None
        offset = 0
        while True:
            before_count = len(members)
            page, reported_total = self._page(self._request(
                "GET", path,
                params={"offset": offset, "limit": self.page_size, "query": ""},
            ))
            for member in page:
                key = str(
                    member.get("id") or member.get("user_id")
                    or member.get("email") or member.get("email_address") or member
                )
                if key not in seen:
                    seen.add(key)
                    members.append(member)
            if page and len(members) == before_count and len(page) >= self.page_size:
                raise TeamServiceError(
                    "member pagination made no progress",
                    category="verification_incomplete", detail={"offset": offset, "page": page},
                )
            total = reported_total if reported_total is not None else total
            offset += len(page)
            if not page or (total is not None and offset >= total) or len(page) < self.page_size:
                break
        known_total = total if total is not None else len(members)
        coverage = min(1.0, len(members) / known_total) if known_total else 1.0
        return {"members": members, "total": known_total, "coverage": coverage}

    def snapshot(self) -> dict:
        members = self.list_members()
        seat_payload = self._request(
            "GET", f"/backend-api/accounts/{self.workspace_id}/users/seat_type_counts",
        )
        seat_counts = (
            seat_payload.get("seat_type_counts", {})
            if isinstance(seat_payload, dict) else {}
        )
        subscription = self._request(
            "GET", "/backend-api/subscriptions",
            params={"account_id": self.workspace_id},
        )
        accounts = self._request(
            "GET", "/backend-api/accounts/check/v4-2023-04-27",
            params={"timezone_offset_min": -480},
        )
        return {
            "members": members,
            "seat_counts": seat_counts,
            "subscription": subscription,
            "accounts": accounts,
        }

    def send_invites(self, emails: list[str]) -> dict:
        cleaned = [str(email).strip().lower() for email in (emails or []) if str(email).strip()]
        if not 1 <= len(cleaned) <= 100:
            raise ValueError("send_invites requires 1..100 email addresses")
        path = f"/backend-api/accounts/{self.workspace_id}/invites"
        seat_payload = self._request(
            "GET", f"/backend-api/accounts/{self.workspace_id}/users/seat_type_counts",
        )
        seat_preflight = (
            seat_payload.get("seat_type_counts", {})
            if isinstance(seat_payload, dict) else {}
        )
        accepted, errored, raw = [], [], []
        for start in range(0, len(cleaned), 25):
            chunk = cleaned[start:start + 25]
            body = {
                "email_addresses": chunk,
                "flow_id": str(uuid.uuid4()),
                "role": "standard-user",
                "seat_type": "default",
                "resend_emails": True,
                "submission_id": str(uuid.uuid4()),
            }
            retry_started = None
            retry_count = 0
            while True:
                try:
                    result = self._request("POST", path, json=body)
                    break
                except Exception as exc:
                    retryable = not isinstance(exc, TeamServiceError) or (
                        exc.status_code == 429 or (exc.status_code or 0) >= 500
                    )
                    if not retryable:
                        raise
                    retry_started = self.clock_fn() if retry_started is None else retry_started
                    retry_count += 1
                    delay = (
                        min(30, 3 * (2 ** (retry_count - 1)))
                        if isinstance(exc, TeamServiceError) and exc.status_code == 429
                        else min(10, retry_count)
                    )
                    if self.clock_fn() + delay > retry_started + self.invite_retry_window:
                        raise
                    self.sleep_fn(delay)
            raw.append(result)
            if isinstance(result, dict):
                body_result = result.get("data") if isinstance(result.get("data"), dict) else result
                accepted.extend(body_result.get("account_invites") or [])
                errored.extend(body_result.get("errored_emails") or [])
        return {
            "account_invites": accepted,
            "errored_emails": errored,
            "seat_preflight": seat_preflight,
            "raw": raw,
        }

    def delete_invite(self, email: str) -> dict:
        email = str(email or "").strip().lower()
        if not email:
            raise ValueError("email is required")
        return self._request(
            "DELETE", f"/backend-api/accounts/{self.workspace_id}/invites",
            json={"email_address": email},
        )

    def accept_join_request(self, invite_id: str) -> dict:
        invite_id = str(invite_id or "").strip()
        if not invite_id:
            raise ValueError("invite_id is required")
        return self._request(
            "PATCH",
            f"/backend-api/accounts/{self.workspace_id}/invites/{quote(invite_id, safe='')}",
            json={"accept_request": True, "role": "standard-user", "seat_type": "default"},
        )

    def exchange_workspace_token(self) -> dict:
        session_data = self._request(
            "GET", "/api/auth/session",
            params={
                "exchange_workspace_token": "true",
                "workspace_id": self.workspace_id,
                "reason": "setCurrentAccountWithoutRedirect",
            },
            headers={
                "Authorization": None,
                "Chatgpt-Account-Id": None,
                "Referer": f"{CHATGPT_BASE}/",
            },
        )
        token = ""
        if isinstance(session_data, dict):
            token = str(session_data.get("accessToken") or session_data.get("access_token") or "").strip()
        if not token:
            raise TeamServiceError(
                "workspace token exchange returned no access token",
                category="invalid_response", detail=session_data,
            )
        return {"access_token": token, "session": session_data}

    def join_workspace(self) -> dict:
        request_path = f"/backend-api/accounts/{self.workspace_id}/invites/request"
        accept_path = f"/backend-api/accounts/{self.workspace_id}/invites/accept"
        requested = self._request("POST", request_path, json={})
        accepted = self._request("POST", accept_path, json={})
        accounts = self._request(
            "GET", "/backend-api/accounts/check/v4-2023-04-27",
            params={"timezone_offset_min": -480},
        )
        exchanged = self.exchange_workspace_token()
        return {
            **exchanged,
            "steps": {"request": requested, "accept": accepted, "accounts": accounts},
        }

    def accept_invite(self) -> dict:
        """Accept an owner invitation; cross-domain users cannot call invites/request."""
        accepted = self._request(
            "POST", f"/backend-api/accounts/{self.workspace_id}/invites/accept", json={},
        )
        accounts = self._request(
            "GET", "/backend-api/accounts/check/v4-2023-04-27",
            params={"timezone_offset_min": -480},
        )
        return {"accept": accepted, "accounts": accounts}

    def change_member_seat(self, user_id: str, seat_type: str,
                           expected_seat_type: str) -> dict:
        user_id = str(user_id or "").strip()
        if not user_id or not self.access_token:
            raise ValueError("user_id and owner access token are required")
        upstream_seat_type = {
            "Standard": "default", "Premium": "prolite",
            "default": "default", "prolite": "prolite",
            "usage_based": "usage_based",
        }.get(seat_type)
        if upstream_seat_type is None:
            raise ValueError("unsupported seat_type")
        before = self.list_members()
        if before["coverage"] < 1:
            raise TeamServiceError(
                "member seat change requires a complete snapshot",
                category="verification_incomplete",
            )
        member = next((row for row in before["members"]
                       if str(row.get("id") or row.get("user_id") or "") == user_id), None)
        if member is None:
            raise TeamServiceError("member not found", category="member_not_found")
        current = str(member.get("seat_type") or "")
        if current != expected_seat_type:
            raise TeamServiceError(
                "member seat changed since it was displayed",
                category="conflict", status_code=409,
                detail={"current_seat_type": current},
            )
        if current == upstream_seat_type:
            return {"changed": False, "member": member}
        path = f"/backend-api/accounts/{self.workspace_id}/users/{quote(user_id, safe='')}/seat/update"
        result = self._request("POST", path, json={
            "operation": "switch",
            "seat_type": upstream_seat_type,
            "flow_id": str(uuid.uuid4()),
            "mutation_attempt_id": str(uuid.uuid4()),
        })
        if isinstance(result, dict) and result.get("success") is False:
            raise TeamServiceError(
                "upstream rejected member seat change",
                category="upstream_error", detail=result,
            )
        after = self.list_members()
        verified = next((row for row in after["members"]
                         if str(row.get("id") or row.get("user_id") or "") == user_id), None)
        if after["coverage"] < 1 or verified is None or verified.get("seat_type") != upstream_seat_type:
            raise TeamServiceError(
                "seat change was sent but is not yet visible; check the remote workspace before retrying",
                category="verification_incomplete",
                detail={"requested_seat_type": upstream_seat_type,
                        "observed_seat_type": verified.get("seat_type") if verified else None},
            )
        return {"changed": True, "member": verified}

    def remove_member(self, user_id: str, access_token: str | None = None) -> dict:
        user_id = str(user_id or "").strip()
        token = str(access_token or self.access_token or "").strip()
        if not user_id:
            raise ValueError("user_id is required")
        if not token:
            raise ValueError("access_token is required")
        path = f"/backend-api/accounts/{self.workspace_id}/users/{quote(user_id, safe='')}"
        attempts = 0
        result = None
        last_conflict = None
        for retry in range(4):
            attempts += 1
            try:
                result = self._request("DELETE", path, access_token=token)
                break
            except TeamServiceError as exc:
                if exc.status_code != 409:
                    raise
                last_conflict = exc
                if retry == 3:
                    break
                self.sleep_fn((retry + 1) * 5)

        snapshot = self.list_members()
        if snapshot["coverage"] < 1:
            raise TeamServiceError(
                "member removal cannot be verified from a partial snapshot",
                category="verification_incomplete", detail=snapshot,
            )
        still_present = any(
            str(member.get("id") or member.get("user_id") or "") == user_id
            for member in snapshot["members"]
        )
        if still_present:
            if last_conflict is not None and result is None:
                raise last_conflict
            raise TeamServiceError(
                "member is still present after delete",
                category="verification_failed", detail=snapshot,
            )
        return {
            "ok": True,
            "attempts": attempts,
            "verified": True,
            "delete": result,
            "members": snapshot,
            "reconciled_after_conflict": result is None,
        }

    @staticmethod
    def _usage_window(window: Any) -> tuple[float | None, float | None, float | None]:
        if not isinstance(window, dict):
            return None, None, None
        pct = next((window.get(key) for key in (
            "used_percent", "used_percentage", "percentage", "pct",
        ) if isinstance(window.get(key), (int, float))), None)
        reset = next((window.get(key) for key in (
            "reset_after_seconds", "reset_seconds", "reset_after",
        ) if isinstance(window.get(key), (int, float))), None)
        duration = next((window.get(key) for key in (
            "limit_window_seconds", "window_seconds", "duration_seconds",
        ) if isinstance(window.get(key), (int, float))), None)
        return pct, reset, duration

    def probe_usage(self, access_token: str, policy: str = "inventory",
                    threshold: float = 100) -> dict:
        access_token = str(access_token or "").strip()
        if not access_token:
            raise ValueError("access_token is required")
        if policy not in ("inventory", "rotation"):
            raise ValueError("policy must be inventory or rotation")
        threshold = float(threshold)
        if not 0 < threshold <= 100:
            raise ValueError("threshold must be between 0 and 100")

        trace = self._request(
            "GET", f"{CHATGPT_BASE}/cdn-cgi/trace", access_token="",
            headers={
                "Chatgpt-Account-Id": None,
                "X-Openai-Target-Path": None,
                "X-Openai-Target-Route": None,
            },
        )
        trace_text = trace if isinstance(trace, str) else ""
        trace_fields = dict(
            line.split("=", 1) for line in trace_text.splitlines() if "=" in line
        )
        region = str(trace_fields.get("loc") or "").upper()
        if region in {"HK", "CN", "RU", "KP", "IR", "SY", "CU"}:
            return {
                "status": "region_blocked", "error_code": "region_blocked",
                "region": region, "trace": trace_fields, "identity": None,
                "agent_assertion": "unsupported",
            }

        whoami = None
        if access_token.startswith("at-"):
            try:
                whoami = self._request(
                    "GET", "https://auth.openai.com/api/accounts/v1/user-auth-credential/whoami",
                    access_token=access_token,
                    headers={
                        "Origin": None,
                        "Referer": None,
                        "Chatgpt-Account-Id": None,
                        "X-Openai-Target-Path": None,
                        "X-Openai-Target-Route": None,
                        "X-Oai-Is-Client-Observation": None,
                        "X-Oai-Is-Pending-Updates": None,
                    },
                )
            except TeamServiceError as exc:
                return {
                    "status": exc.category, "error_code": exc.category,
                    "detail": exc.detail, "region": region, "trace": trace_fields,
                    "identity": None, "agent_assertion": "unsupported",
                }

        try:
            raw = self._request(
                "GET", f"{CHATGPT_BASE}/backend-api/wham/usage",
                access_token=access_token,
                headers={
                    "Originator": "codex_cli_rs",
                    "User-Agent": "codex_cli_rs/0.146.0",
                    "Version": "0.146.0",
                    "X-Openai-Target-Path": None,
                    "X-Openai-Target-Route": None,
                },
            )
        except TeamServiceError as exc:
            html_forbidden = (
                exc.status_code == 403
                and isinstance(exc.detail, str)
                and ("<html" in exc.detail.lower() or "cloudflare" in exc.detail.lower())
            )
            error_code = {
                429: "rate_limited", 401: "auth_error", 403: "oauth_usage_forbidden",
            }.get(exc.status_code, exc.category)
            if html_forbidden:
                error_code = "cloudflare_forbidden"
            return {
                "status": "limit_reached" if exc.status_code == 429 else exc.category,
                "error_code": error_code, "detail": exc.detail,
                "region": region, "trace": trace_fields, "whoami": whoami,
                "identity": None, "agent_assertion": "unsupported",
            }

        rate = raw.get("rate_limit", {}) if isinstance(raw, dict) else {}
        primary = rate.get("primary_window", {}) if isinstance(rate, dict) else {}
        secondary = rate.get("secondary_window", {}) if isinstance(rate, dict) else {}
        pct_5h, reset_5h, primary_duration = self._usage_window(primary)
        pct_7d, reset_7d, secondary_duration = self._usage_window(secondary)
        if isinstance(raw, dict):
            pct_5h = raw.get("pct_5h", pct_5h)
            pct_7d = raw.get("pct_7d", pct_7d)
            reset_5h = raw.get("reset_5h", reset_5h)
            reset_7d = raw.get("reset_7d", reset_7d)

        pct_unknown = None
        for window in (rate.get("windows", []) if isinstance(rate, dict) else []):
            pct, _reset, duration = self._usage_window(window)
            if duration and duration <= 21_600 and pct_5h is None:
                pct_5h, reset_5h = pct, _reset
            elif duration and duration >= 86_400 and pct_7d is None:
                pct_7d, reset_7d = pct, _reset
            elif pct is not None:
                pct_unknown = max(pct_unknown or pct, pct)

        allowed = rate.get("allowed") if isinstance(rate, dict) else None
        limit_reached = bool(rate.get("limit_reached")) if isinstance(rate, dict) else False
        credits = raw.get("credits", {}) if isinstance(raw, dict) else {}
        spend = raw.get("spend_control", {}) if isinstance(raw, dict) else {}
        overage_reached = bool(credits.get("overage_limit_reached")) if isinstance(credits, dict) else False
        spend_reached = bool(spend.get("reached")) if isinstance(spend, dict) else False
        long_limited = isinstance(pct_7d, (int, float)) and pct_7d >= threshold
        short_limited = isinstance(pct_5h, (int, float)) and pct_5h >= threshold
        unknown_limited = isinstance(pct_unknown, (int, float)) and pct_unknown >= threshold
        exhausted = (
            long_limited or unknown_limited or (policy == "rotation" and short_limited)
            or limit_reached or allowed is False or overage_reached
        )
        quota_window = (
            "7d" if long_limited else "5h" if policy == "rotation" and short_limited
            else "unknown" if exhausted else None
        )
        reset_after = reset_7d if quota_window == "7d" else reset_5h if quota_window == "5h" else None
        identity = None
        identity_error = None
        try:
            identity = self._request(
                "GET", f"{CHATGPT_BASE}/backend-api/me", access_token=access_token,
            )
        except Exception as exc:
            identity_error = exc.to_dict() if isinstance(exc, TeamServiceError) else str(exc)
        return {
            "status": "limit_reached" if exhausted else "ok",
            "error_code": "usage_limit_reached" if exhausted else None,
            "reason": "quota_exhausted" if exhausted else None,
            "quota_window": quota_window,
            "reset_after_seconds": reset_after,
            "pct_5h": pct_5h,
            "pct_7d": pct_7d,
            "pct_unknown": pct_unknown,
            "reset_5h": reset_5h,
            "reset_7d": reset_7d,
            "limit_reached": limit_reached,
            "allowed": allowed,
            "overage_reached": overage_reached,
            "spend_reached": spend_reached,
            "short_window_limited": short_limited,
            "threshold": threshold,
            "window_seconds": {"primary": primary_duration, "secondary": secondary_duration},
            "region": region,
            "trace": trace_fields,
            "whoami": whoami,
            "identity": identity,
            "identity_error": identity_error,
            "agent_assertion": "unsupported",
            "raw": raw,
        }


class Sub2ApiClient:
    def __init__(self, workspace: dict, transport=None, refresh_fn=None):
        self.workspace = dict(workspace or {})
        base = str(self.workspace.get("sub2api_url") or "").strip().rstrip("/")
        if not base:
            raise ValueError("sub2api_url is required")
        self.base_url = base if base.endswith("/api/v1") else f"{base}/api/v1"
        self.api_key = str(self.workspace.get("sub2api_api_key") or "").strip()
        if not self.api_key:
            raise ValueError("sub2api_api_key is required")
        self.timeout = int(self.workspace.get("sub2api_timeout") or 30)
        self.transport = transport or create_http_session()
        self.refresh_fn = refresh_fn

    def _request(self, method: str, path: str, **kwargs) -> tuple[Any, Any]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-Api-Key": self.api_key,
        }
        headers.update(kwargs.pop("headers", {}) or {})
        response = self.transport.request(
            method, f"{self.base_url}{path}", headers=headers,
            timeout=self.timeout, **kwargs,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        raw = _response_detail(response)
        if not 200 <= status < 300:
            raise TeamServiceError(
                f"sub2api HTTP {status}",
                category="sub2api_auth" if status in (401, 403) else "sub2api_error",
                status_code=status, detail=raw,
            )
        if not isinstance(raw, dict) or "code" not in raw:
            raise TeamServiceError(
                "sub2api response envelope is missing code",
                category="unsupported_schema", detail=raw,
            )
        if raw.get("code") != 0:
            raise TeamServiceError(
                str(raw.get("msg") or "sub2api rejected request"),
                category="sub2api_error", detail=raw,
            )
        return raw.get("data"), raw

    def groups(self) -> dict:
        data, raw = self._request("GET", "/admin/groups")
        if isinstance(data, list):
            groups = data
        elif isinstance(data, dict):
            groups = data.get("groups") or data.get("items") or []
        else:
            groups = []
        return {"groups": groups, "raw": raw}

    def list_accounts(self) -> list[dict]:
        accounts = []
        for page in range(1, 101):
            data, _ = self._request("GET", "/admin/accounts", params={
                "page": page, "page_size": 100, "platform": "openai",
            })
            items = data.get("items") if isinstance(data, dict) else data
            if not isinstance(items, list):
                raise TeamServiceError("invalid sub2api account list", category="unsupported_schema")
            accounts.extend(item for item in items if isinstance(item, dict))
            if len(items) < 100:
                return accounts
        raise TeamServiceError("sub2api account pagination is incomplete", category="verification_incomplete")

    @staticmethod
    def member_account(accounts: list[dict], email: str, binding: dict | None = None) -> dict | None:
        email = email.strip().lower()
        binding = binding or {}
        remote_id = str(binding.get("remote_account_id") or "")
        name = f"rotation-{binding['binding_id']}" if binding.get("binding_id") else ""
        if remote_id:
            bound = [item for item in accounts if str(item.get("id") or item.get("account_id") or "") == remote_id]
            if len(bound) == 1:
                account = bound[0]
                remote_email = str((account.get("extra") or {}).get("email") or account.get("email") or "").lower()
                if email and remote_email and remote_email != email:
                    raise TeamServiceError("sub2api account email mismatch", category="identity_mismatch")
                return account
        matches = [item for item in accounts if (
            (name and item.get("name") == name)
            or (email and str((item.get("extra") or {}).get("email") or item.get("email") or "").lower() == email)
        )]
        if len(matches) > 1:
            raise TeamServiceError("multiple sub2api accounts match member", category="sub2api_reconcile_ambiguous")
        if not matches:
            return None
        account = matches[0]
        remote_email = str((account.get("extra") or {}).get("email") or account.get("email") or "").lower()
        if email and remote_email and remote_email != email:
            raise TeamServiceError("sub2api account email mismatch", category="identity_mismatch")
        return account

    def member_status(self, account: dict | None) -> dict:
        if account is None:
            return {"status": "missing"}
        groups = {int(value) for value in account.get("group_ids") or []}
        credentials = account.get("credentials_status") or {}
        expired = (
            account.get("auto_pause_on_expired") is True
            and account.get("expires_at") not in (None, "", 0, "0")
            and not self._future_timestamp(account["expires_at"])
        )
        cooling_down = any(self._future_timestamp(account.get(field)) for field in (
            "rate_limit_reset_at", "overload_until", "temp_unschedulable_until",
        ))
        usable = (
            set(self._group_ids()) <= groups
            and account.get("status") == "active"
            and account.get("schedulable") is True
            and not expired and not cooling_down
            and all(credentials.get(f"has_{key}") is True
                    for key in ("access_token", "refresh_token", "id_token"))
        )
        return {
            "status": "ready" if usable else "unavailable",
            "account_id": str(account.get("id") or account.get("account_id") or ""),
        }

    def remove_from_groups(self, account: dict) -> dict:
        account_id = account.get("id") or account.get("account_id")
        if account_id in (None, ""):
            raise TeamServiceError("sub2api account id is missing", category="unsupported_schema")
        path = f"/admin/accounts/{quote(str(account_id), safe='')}"
        before_data, _ = self._request("GET", path)
        before = self._account(before_data)
        listed_email = str((account.get("extra") or {}).get("email") or account.get("email") or "").lower()
        current_email = str((before.get("extra") or {}).get("email") or before.get("email") or "").lower()
        if (account.get("name") and account["name"] != before.get("name")) or (
            listed_email and current_email != listed_email
        ):
            raise TeamServiceError("sub2api account changed before removal", category="identity_mismatch")
        current = {int(value) for value in before.get("group_ids") or []}
        remaining = sorted(current - set(self._group_ids()))
        if remaining == sorted(current):
            return {"ok": True, "already_absent": True, "account_id": str(account_id)}
        if remaining:
            self._request("PUT", path, json={"group_ids": remaining})
            after_data, _ = self._request("GET", path)
            after = self._account(after_data)
            if {int(value) for value in after.get("group_ids") or []} != set(remaining):
                raise TeamServiceError("sub2api group removal was not verified", category="verification_failed")
            return {"ok": True, "account_id": str(account_id), "group_ids": remaining}
        self._request("DELETE", path)
        try:
            self._request("GET", path)
        except TeamServiceError as exc:
            if exc.status_code == 404:
                return {"ok": True, "deleted": True, "account_id": str(account_id)}
            raise
        raise TeamServiceError("sub2api account deletion was not verified", category="verification_failed")

    def _test_model_candidates(self, account_id: Any) -> list[str]:
        candidates = []
        selected_groups = set(self._group_ids())
        for group in self.groups()["groups"]:
            if not isinstance(group, dict) or group.get("id") not in selected_groups:
                continue
            allowlist = group.get("model_allowlist") or {}
            candidates.extend(allowlist.get("models") or [])
            candidates.append(group.get("default_mapped_model"))
            dispatch = group.get("messages_dispatch_model_config") or {}
            candidates.append(dispatch.get("sonnet_mapped_model"))
        if not any(isinstance(model, str) and model.startswith("gpt-") for model in candidates):
            data, _ = self._request("GET", f"/admin/accounts/{quote(str(account_id), safe='')}/models")
            models = data if isinstance(data, list) else (data.get("models") or data.get("items") or [])
            candidates.extend(model.get("id") if isinstance(model, dict) else model for model in models)
        return list(dict.fromkeys(
            model for model in candidates
            if isinstance(model, str) and model.startswith("gpt-") and "image" not in model
        ))[:3]

    def test_account(self, account_id: Any, model_id: str = "") -> dict:
        response = self.transport.request(
            "POST", f"{self.base_url}/admin/accounts/{quote(str(account_id), safe='')}/test",
            headers={"Accept": "text/event-stream", "Content-Type": "application/json", "X-Api-Key": self.api_key},
            json={"model_id": model_id} if model_id else {}, timeout=self.timeout,
        )
        if not 200 <= response.status_code < 300:
            raise TeamServiceError("sub2api account test failed", category="sub2api_test_error",
                                   status_code=response.status_code, detail=_response_detail(response))
        for line in (getattr(response, "text", "") or "").splitlines():
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue
            if event.get("type") == "error":
                raise TeamServiceError(str(event.get("error") or "sub2api account test failed"),
                                       category="sub2api_test_error")
            if event.get("type") == "test_complete":
                if event.get("success") is True:
                    return {"ok": True}
                raise TeamServiceError("sub2api account test was unsuccessful", category="sub2api_test_error")
        raise TeamServiceError("sub2api account test did not complete", category="verification_incomplete")

    def _group_ids(self) -> list[int]:
        raw = self.workspace.get("sub2api_group_ids")
        if isinstance(raw, list):
            values = raw
        else:
            values = str(raw or "").replace(";", ",").split(",")
        try:
            result = [int(value) for value in values if str(value).strip()]
        except (TypeError, ValueError) as exc:
            raise ValueError("sub2api_group_ids must contain integers") from exc
        if not result:
            raise ValueError("workspace-specific sub2api_group_ids are required")
        return result

    @staticmethod
    def _is_timeout_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return "curl: (28)" in message or "timed out" in message or "timeout" in message

    @staticmethod
    def _stage_error(stage: str, exc: Exception,
                     remote_account_id: Any = None) -> TeamServiceError:
        timeout = Sub2ApiClient._is_timeout_error(exc)
        detail = {
            "stage": stage,
            "cause": str(exc),
            "upstream_category": exc.category if isinstance(exc, TeamServiceError) else None,
        }
        if remote_account_id not in (None, ""):
            detail["remote_account_id"] = str(remote_account_id)
        return TeamServiceError(
            f"sub2api {stage} {'timed out' if timeout else 'failed'}: {exc}",
            category=(
                f"sub2api_{stage}_timeout" if timeout
                else exc.category if isinstance(exc, TeamServiceError)
                else f"sub2api_{stage}_error"
            ),
            status_code=exc.status_code if isinstance(exc, TeamServiceError) else None,
            detail=detail,
        )

    def _find_account_by_name(self, name: str) -> dict | None:
        data, _raw = self._request(
            "GET", "/admin/accounts",
            params={"search": name, "page": 1, "page_size": 100},
        )
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("items") or data.get("accounts") or data.get("list") or []
        else:
            items = []
        exact = [
            item for item in items
            if isinstance(item, dict) and str(item.get("name") or "") == name
        ]
        if len(exact) > 1:
            raise TeamServiceError(
                "multiple sub2api accounts match the rotation binding",
                category="sub2api_reconcile_ambiguous",
                detail={"stage": "create_reconcile", "name": name, "count": len(exact)},
            )
        return exact[0] if exact else None

    def _find_account_for_binding(self, name: str, email: str,
                                  binding_id: str) -> tuple[dict | None, str]:
        exact = self._find_account_by_name(name)
        if exact is not None:
            return exact, "name"

        binding_matches = []
        email_matches = []
        for page in range(1, 101):
            data, _raw = self._request(
                "GET", "/admin/accounts", params={"page": page, "page_size": 100},
            )
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items") or data.get("accounts") or data.get("list") or []
            else:
                items = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                extra = item.get("extra") if isinstance(item.get("extra"), dict) else {}
                creds = item.get("credentials") if isinstance(item.get("credentials"), dict) else {}
                if str(extra.get("binding_id") or "") == binding_id:
                    binding_matches.append(item)
                item_email = str(
                    item.get("email") or extra.get("email") or creds.get("email") or ""
                ).strip().lower()
                if item_email == email:
                    email_matches.append(item)
            if len(items) < 100:
                break

        for matches, kind in ((binding_matches, "binding"), (email_matches, "email")):
            unique = {
                str(item.get("id") or item.get("account_id") or ""): item
                for item in matches
                if item.get("id") not in (None, "") or item.get("account_id") not in (None, "")
            }
            if len(unique) > 1:
                raise TeamServiceError(
                    "multiple sub2api accounts match the rotation identity",
                    category="sub2api_reconcile_ambiguous",
                    detail={"stage": "create_reconcile", "match": kind, "count": len(unique)},
                )
            if unique:
                return next(iter(unique.values())), kind
        return None, ""

    @staticmethod
    def _account(data: Any) -> dict:
        if not isinstance(data, dict):
            return {}
        return data.get("account") if isinstance(data.get("account"), dict) else data

    @staticmethod
    def _generation(account: dict) -> Any:
        extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
        return account.get("generation", extra.get("generation"))

    def _check_local_binding(self, email: str, binding_id: str, generation: Any) -> None:
        local_id = self.workspace.get("id")
        if local_id is None:
            return
        binding = db.list_team_bindings(int(local_id)).get(email)
        if not binding or binding["binding_id"] != binding_id or str(binding["generation"]) != str(generation):
            raise TeamServiceError("local rotation binding changed", category="generation_mismatch")

    @staticmethod
    def _future_timestamp(value: Any) -> bool:
        if value in (None, "", 0, "0"):
            return False
        try:
            stamp = float(value)
            if stamp > 10_000_000_000:
                stamp /= 1000
        except (TypeError, ValueError):
            try:
                stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
            except ValueError as exc:
                raise TeamServiceError(
                    "unsupported sub2api scheduling timestamp",
                    category="unsupported_schema", detail=value,
                ) from exc
        return stamp > time.time()

    def _validate_remote_state(self, account: dict, generation: Any,
                               *, require_credentials: dict | None = None,
                               require_group_ids: list[int] | None = None,
                               require_schedulable: bool = True) -> None:
        remote_generation = self._generation(account)
        if str(remote_generation) != str(generation):
            raise TeamServiceError(
                "sub2api generation mismatch", category="generation_mismatch",
                detail={"expected": generation, "actual": remote_generation, "raw": account},
            )
        for field in ("rate_limit_reset_at", "overload_until", "temp_unschedulable_until"):
            if self._future_timestamp(account.get(field)):
                raise TeamServiceError(
                    f"sub2api account is blocked by {field}",
                    category="remote_rate_limited", detail=account,
                )
        schedulable = account.get("schedulable", account.get("is_schedulable"))
        if require_schedulable and schedulable is False:
            raise TeamServiceError(
                "sub2api account is not schedulable",
                category="remote_unschedulable", detail=account,
            )
        if require_group_ids is not None:
            remote_group_ids = account.get("group_ids")
            try:
                remote_groups = {int(value) for value in remote_group_ids}
            except (TypeError, ValueError):
                remote_groups = set()
            missing_groups = sorted(set(require_group_ids) - remote_groups)
            if missing_groups:
                raise TeamServiceError(
                    "sub2api readback omitted required groups",
                    category="invalid_readback",
                    detail={"missing_group_ids": missing_groups, "raw": account},
                )
        if require_credentials is None:
            return
        if account.get("status") != "active" or (
            account.get("auto_pause_on_expired") is True
            and account.get("expires_at") not in (None, "", 0, "0")
            and not self._future_timestamp(account["expires_at"])
        ):
            raise TeamServiceError("sub2api account is not active", category="remote_unschedulable")
        if schedulable is not True:
            raise TeamServiceError(
                "sub2api readback omitted schedulable state",
                category="unsupported_schema", detail=account,
            )
        credentials_status = account.get("credentials_status")
        if isinstance(credentials_status, dict):
            missing = [
                key for key in ("access_token", "refresh_token", "id_token")
                if credentials_status.get(f"has_{key}") is not True
            ]
        else:
            remote_credentials = account.get("credentials")
            if not isinstance(remote_credentials, dict):
                raise TeamServiceError(
                    "sub2api readback omitted credential status",
                    category="invalid_readback", detail=account,
                )
            missing = [
                key for key in ("access_token", "refresh_token", "id_token")
                if not remote_credentials.get(key)
                or remote_credentials.get(key) != require_credentials.get(key)
            ]
        if missing:
            raise TeamServiceError(
                "sub2api credential readback mismatch",
                category="invalid_readback", detail={"fields": missing, "raw": account},
            )

    def deliver(self, email: str, credentials: dict, request_id: str,
                binding_id: str, generation: Any, persist_local: bool = True) -> dict:
        from webui.exporter import (
            CODEX_CLIENT_ID,
            _decode_jwt_payload, _get_auth, _get_profile,
            build_sub2api_payload, refresh_codex_token,
        )

        email = str(email or "").strip().lower()
        request_id = str(request_id or "").strip()
        binding_id = str(binding_id or "").strip()
        if not email or not request_id or not binding_id or generation in (None, ""):
            raise ValueError("email, request_id, binding_id and generation are required")
        self._check_local_binding(email, binding_id, generation)
        credentials = dict(credentials or {})
        missing = [
            key for key in ("access_token", "refresh_token", "id_token")
            if not str(credentials.get(key) or "").strip()
        ]
        if missing:
            raise ValueError(f"missing OAuth credentials: {', '.join(missing)}")

        # The workspace Web token is only for membership APIs. Sub2API's Codex
        # Responses endpoint needs a fresh Codex OAuth token from the RT.
        refresh = self.refresh_fn or (
            lambda rt: refresh_codex_token(rt, proxy=self.workspace.get("proxy") or "")
        )
        old_refresh_token = credentials["refresh_token"]
        try:
            fresh = refresh(old_refresh_token)
        except Exception as exc:
            raise self._stage_error("refresh", exc) from exc
        credentials.update({
            "access_token": fresh["access_token"],
            "refresh_token": fresh.get("refresh_token") or credentials["refresh_token"],
            "id_token": fresh.get("id_token") or credentials["id_token"],
            "client_id": CODEX_CLIENT_ID,
        })
        access_claims = _decode_jwt_payload(credentials["access_token"])
        id_claims = _decode_jwt_payload(credentials["id_token"])
        token_emails = {
            str(value).strip().lower() for value in (
                access_claims.get("email"), _get_profile(access_claims).get("email"),
                id_claims.get("email"), _get_profile(id_claims).get("email"),
            ) if value
        }
        if not token_emails or token_emails != {email}:
            raise TeamServiceError("refreshed OAuth identity does not match member",
                                   category="credential_identity_mismatch")
        token_workspace = str(_get_auth(access_claims).get("chatgpt_account_id") or "")
        expected_workspace = str(self.workspace.get("workspace_id") or "")
        if expected_workspace and token_workspace != expected_workspace:
            raise TeamServiceError("refreshed OAuth token is not for this workspace",
                                   category="credential_workspace_mismatch")
        if persist_local and db.get_registered(email) and not db.rotate_registered_oauth_tokens(
            email, old_refresh_token, credentials["refresh_token"], credentials["id_token"],
        ):
            raise TeamServiceError("local refresh token changed during delivery",
                                   category="refresh_token_changed")

        group_ids = self._group_ids()
        payload = build_sub2api_payload({"email": email, **credentials}, group_ids)
        account_name = f"rotation-{binding_id}"
        payload["name"] = account_name
        payload["model_mapping"] = {}
        if isinstance(payload.get("credentials"), dict):
            payload["credentials"]["model_mapping"] = {}
        payload["extra"] = {"email": email, "binding_id": binding_id, "generation": generation}
        create_raw = None
        created_account = None
        create_attempts = 0
        create_reconciled = False
        create_reconciled_conflict = False
        adopt_existing = False
        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:16]
        create_key = f"rotation-create-{request_id}-{payload_hash}"
        last_timeout = None
        local_id = self.workspace.get("id")
        if local_id is not None:
            binding = db.list_team_bindings(int(local_id)).get(email) or {}
            remote_id = binding.get("remote_account_id")
            if remote_id:
                try:
                    existing_data, _ = self._request(
                        "GET", f"/admin/accounts/{quote(str(remote_id), safe='')}",
                    )
                except TeamServiceError as exc:
                    if exc.status_code != 404:
                        raise
                else:
                    candidate = self._account(existing_data)
                    extra = candidate.get("extra") if isinstance(candidate.get("extra"), dict) else {}
                    if candidate.get("name") != account_name or extra.get("binding_id") != binding_id:
                        raise TeamServiceError("bound sub2api account identity mismatch",
                                               category="identity_mismatch")
                    created_account = candidate
                    create_reconciled = True
            if created_account is None:
                created_account = self._find_account_by_name(account_name)
                create_reconciled = created_account is not None
        for _attempt in range(2 if created_account is None else 0):
            create_attempts += 1
            try:
                self._check_local_binding(email, binding_id, generation)
                created, create_raw = self._request(
                    "POST", "/admin/accounts", json=payload,
                    headers={"Idempotency-Key": create_key},
                )
                created_account = self._account(created)
                break
            except Exception as exc:
                conflict = isinstance(exc, TeamServiceError) and exc.status_code == 409
                if conflict:
                    try:
                        created_account, match_kind = self._find_account_for_binding(
                            account_name, email, binding_id,
                        )
                    except Exception as reconcile_exc:
                        raise self._stage_error("create_reconcile", reconcile_exc) from reconcile_exc
                    if created_account is None:
                        raise TeamServiceError(
                            "sub2api create conflict but no unique existing account was found",
                            category="sub2api_create_conflict",
                            status_code=409,
                            detail={"stage": "create_reconcile"},
                        ) from exc
                    create_reconciled_conflict = True
                    adopt_existing = match_kind != "name"
                    break
                if not self._is_timeout_error(exc):
                    raise self._stage_error("create", exc) from exc
                last_timeout = exc
                try:
                    created_account = self._find_account_by_name(account_name)
                except Exception as reconcile_exc:
                    raise self._stage_error("create_reconcile", reconcile_exc) from reconcile_exc
                if created_account is not None:
                    create_reconciled = True
                    break
        if created_account is None:
            raise TeamServiceError(
                "sub2api create timed out after 2 attempts; reconciliation found no account",
                category="sub2api_create_timeout",
                detail={"stage": "create", "attempts": create_attempts, "cause": str(last_timeout)},
            )
        account_id = created_account.get("id") or created_account.get("ID") or created_account.get("account_id")
        if account_id in (None, ""):
            raise TeamServiceError(
                "sub2api create response omitted account id",
                category="unsupported_schema", detail=create_raw,
            )
        account_path = f"/admin/accounts/{quote(str(account_id), safe='')}"
        if adopt_existing:
            try:
                existing_data, _existing_raw = self._request("GET", account_path)
                existing = self._account(existing_data)
                existing_generation = self._generation(existing)
                if str(existing_generation) != str(generation):
                    raise TeamServiceError("sub2api generation mismatch", category="generation_mismatch")
                existing_extra = (
                    dict(existing.get("extra"))
                    if isinstance(existing.get("extra"), dict) else {}
                )
                if existing_extra.get("binding_id") not in (None, "", binding_id):
                    raise TeamServiceError("sub2api binding mismatch", category="identity_mismatch")
                existing_extra.update({
                    "email": email,
                    "binding_id": binding_id,
                    "generation": generation,
                })
                self._request(
                    "PUT", account_path,
                    json={"group_ids": group_ids, "extra": existing_extra},
                )
            except Exception as exc:
                raise self._stage_error("adopt", exc, account_id) from exc
        try:
            before_data, before_raw = self._request("GET", account_path)
            before = self._account(before_data)
            self._validate_remote_state(before, generation, require_group_ids=group_ids,
                                        require_schedulable=False)
        except Exception as exc:
            raise self._stage_error("readback_before_apply", exc, account_id) from exc
        try:
            self._check_local_binding(email, binding_id, generation)
            applied, apply_raw = self._request(
                "POST", f"{account_path}/apply-oauth-credentials",
                json={
                    "type": "oauth",
                    "credentials": {**credentials, "model_mapping": {}},
                },
                headers={"Idempotency-Key": f"rotation-apply-{request_id}"},
            )
        except Exception as exc:
            raise self._stage_error("apply", exc, account_id) from exc
        try:
            after_data, after_raw = self._request("GET", account_path)
            after = self._account(after_data)
            self._validate_remote_state(
                after, generation,
                require_credentials=credentials,
                require_group_ids=group_ids,
            )
        except Exception as exc:
            raise self._stage_error("verify", exc, account_id) from exc
        tested = None
        try:
            for model_id in self._test_model_candidates(account_id):
                try:
                    tested = self.test_account(account_id, model_id)
                    break
                except TeamServiceError as exc:
                    if "not supported when using Codex" not in str(exc):
                        raise
            if tested is None:
                raise TeamServiceError("no supported Sub2API test model succeeded",
                                       category="sub2api_test_error")
        except Exception as exc:
            raise self._stage_error("test", exc, account_id) from exc
        quota = quota_raw = None
        for quota_attempt in range(2):
            try:
                quota, quota_raw = self._request(
                    "GET", f"/admin/openai/accounts/{quote(str(account_id), safe='')}/quota",
                )
                break
            except Exception as exc:
                retryable = self._is_timeout_error(exc) or (
                    isinstance(exc, TeamServiceError)
                    and isinstance(exc.status_code, int)
                    and exc.status_code >= 500
                )
                if quota_attempt == 0 and retryable:
                    continue
                raise self._stage_error("quota", exc, account_id) from exc
        return {
            "ok": True, "account": after,
            "raw": {
                "create": create_raw, "before": before_raw,
                "apply": apply_raw, "readback": after_raw, "quota": quota_raw,
            },
            "applied": applied,
            "tested": tested,
            "quota": quota,
            "create_attempts": create_attempts,
            "create_reconciled_after_timeout": create_reconciled,
            "create_reconciled_after_conflict": create_reconciled_conflict,
        }


def account_oauth(workspace: dict, email: str, *, _exchange: bool = True,
                  force_relogin: bool = False) -> dict:
    email = str(email or "").strip().lower()
    if not email:
        raise ValueError("email is required")
    registered = db.get_registered(email)
    account = db.get_account(email)
    if not registered and not account:
        raise TeamServiceError(
            f"local account not found: {email}", category="local_account_missing",
        )
    workspace_id = str((workspace or {}).get("workspace_id") or "").strip()
    if not workspace_id:
        raise ValueError("workspace_id is required")
    stored = dict(registered or {})
    stored_extra = stored.get("extra") if isinstance(stored.get("extra"), dict) else {}
    password = str(
        (stored.get("password") or "") if stored_extra.get("team_login_credentials")
        else stored.get("password") or (account or {}).get("password") or ""
    )

    def new_flow():
        env_overrides = {
            "TEAM_SKIP_ADD_PHONE": "1",
            "OAUTH_ALLOWED_WORKSPACE_ID": workspace_id if _exchange else "",
        }
        return AuthFlow(
            Config(proxy=(workspace or {}).get("proxy") or None),
            env_overrides=env_overrides,
            account_callback=lambda _email: stored,
        )

    def exchange(flow, credentials):
        client_workspace = {
            **dict(workspace or {}),
            "owner_access_token": credentials.get("access_token", ""),
            "owner_session_token": credentials.get("session_token", ""),
            "owner_device_id": credentials.get("device_id", ""),
        }
        return TeamClient(client_workspace, session=flow.session).exchange_workspace_token()

    flow = new_flow()
    result = None
    exchanged = None
    reuse_attempted = bool(stored.get("session_token") and not force_relogin)
    if reuse_attempted:
        try:
            result = flow.result
            for key, value in stored.items():
                if hasattr(result, key) and value not in (None, ""):
                    setattr(result, key, value)
            result.email = email
            result.password = password
            result.device_id = str(stored.get("device_id") or uuid.uuid4())
            flow.session.cookies.set("oai-did", result.device_id, domain=".chatgpt.com")
            flow.session.cookies.set(
                "__Secure-next-auth.session-token",
                str(stored["session_token"]),
                domain=".chatgpt.com",
            )
            if _exchange:
                exchanged = exchange(flow, stored)
            else:
                result = flow.from_existing_credentials(
                    str(stored["session_token"]), str(stored.get("access_token") or ""),
                    result.device_id,
                )
                if not result.access_token or result.email.strip().lower() != email:
                    raise TeamServiceError("saved login session is invalid", category="auth_error")
                result.password = password
        except Exception:
            result = None
            exchanged = None

    if result is None:
        if reuse_attempted:
            flow = new_flow()
        settings = db.get_mail_settings()
        extra = registered.get("extra", {}) if isinstance(registered, dict) else {}
        kind = str(
            (account or {}).get("kind") or extra.get("mail_source")
            or extra.get("kind") or settings.get("mail_source") or ""
        ).strip()
        if not kind:
            raise TeamServiceError(
                f"mail provider is not configured for {email}",
                category="mail_provider_missing",
            )
        try:
            provider = create_mail_provider(kind, settings, account)
        except Exception as exc:
            raise TeamServiceError(
                f"mail provider cannot serve {email}: {exc}",
                category="mail_provider_unavailable",
            ) from exc
        try:
            result = flow.run_protocol_login(provider, email, password)
        except RuntimeError as exc:
            retryable = (
                "invalid_state" in str(exc).lower()
                or "未拿到有效 session/access token" in str(exc)
            )
            if not retryable:
                raise
            # The old authorize state cannot be repaired inside this session.
            flow = new_flow()
            try:
                result = flow.run_protocol_login(provider, email, password)
            except RuntimeError as retry_exc:
                if "invalid_state" in str(retry_exc).lower() or "未拿到有效 session/access token" in str(retry_exc):
                    raise TeamServiceError(
                        "登录会话在新会话重试后仍不可用，请检查代理出口或稍后重试",
                        category="login_session_invalid", status_code=409,
                    ) from retry_exc
                raise

    for key, value in (
        ("email", email),
        ("password", password),
        ("device_id", stored.get("device_id")),
        ("refresh_token", stored.get("refresh_token")),
        ("id_token", stored.get("id_token")),
        ("totp_secret", stored.get("totp_secret")),
    ):
        if force_relogin and key in ("refresh_token", "id_token"):
            continue
        if value and not getattr(result, key, ""):
            setattr(result, key, value)
    credentials = {**stored, **result.to_dict()}
    if force_relogin:
        for key in ("refresh_token", "id_token"):
            credentials[key] = str(getattr(result, key, "") or "")
        if not all(credentials[key] for key in ("refresh_token", "id_token")):
            raise TeamServiceError("workspace OAuth did not return complete credentials",
                                   category="oauth_credentials_incomplete")
    saved_extra = credentials.pop("extra", {})
    if isinstance(saved_extra, dict):
        credentials = {**saved_extra, **credentials}
    credentials["email"] = email
    db.save_registered(credentials)

    output = {
        "ok": True,
        "email": email,
        "credentials": credentials,
        "workspace_access_token": "",
        "exchange": None,
        "_session": flow.session,
    }
    if _exchange:
        exchanged = exchanged or exchange(flow, credentials)
        output["workspace_access_token"] = exchanged["access_token"]
        output["exchange"] = exchanged["session"]
        session_data = exchanged.get("session") or {}
        refreshed_session = str(
            session_data.get("sessionToken") or session_data.get("session_token") or ""
        ).strip()
        if refreshed_session and refreshed_session != credentials.get("session_token"):
            credentials["session_token"] = refreshed_session
            flow.session.cookies.set(
                "__Secure-next-auth.session-token", refreshed_session,
                domain=".chatgpt.com",
            )
            db.save_registered(credentials)
    return output


def _account_spec(value: str | dict) -> dict:
    if isinstance(value, dict):
        spec = dict(value)
        spec["email"] = str(spec.get("email") or "").strip().lower()
        return spec
    return {"email": str(value or "").strip().lower()}


def _batch_failure(email: str, exc: Exception) -> dict:
    error = exc.to_dict() if isinstance(exc, TeamServiceError) else {
        "message": str(exc), "category": "error", "status_code": None, "detail": None,
    }
    return {"email": email, "ok": False, "error": error}


def _member_email(member: dict) -> str:
    user = member.get("user") if isinstance(member.get("user"), dict) else {}
    return str(member.get("email") or member.get("email_address") or user.get("email") or "").lower()


def _authorize_workspace_codex(workspace: dict, email: str, credentials: dict) -> dict:
    """Exchange Codex OAuth on the member's saved web session, without a new web login."""
    from webui.exporter import _decode_jwt_payload, _get_auth, _get_profile

    account = db.get_account(email)
    if not account:
        raise TeamServiceError("local mailbox credentials are missing", category="mail_provider_missing")
    provider = create_mail_provider(account["kind"], db.get_mail_settings(), account)
    credentials = dict(credentials)
    for _ in range(2):
        flow = AuthFlow(
            Config(proxy=workspace.get("proxy") or None),
            env_overrides={
                "OAUTH_ALLOWED_WORKSPACE_ID": str(workspace["workspace_id"]),
                "TEAM_SKIP_ADD_PHONE": "1",
            },
            account_callback=lambda _email: db.get_registered(email) or {},
        )
        restored = flow.from_existing_credentials(
            credentials.get("session_token", ""), credentials.get("access_token", ""),
            credentials.get("device_id", ""),
        )
        if not restored.access_token or restored.email.strip().lower() != email:
            break
        credentials["session_token"] = restored.session_token
        db.save_registered(credentials)
        if not flow.oauth_codex_rt_exchange(mail_provider=provider):
            continue
        claims = _decode_jwt_payload(flow.result.access_token)
        auth = _get_auth(claims)
        profile = _get_profile(claims)
        token_email = str(profile.get("email") or claims.get("email") or "").strip().lower()
        if token_email != email or str(auth.get("chatgpt_account_id") or "") != str(workspace["workspace_id"]):
            raise TeamServiceError("workspace Codex OAuth identity mismatch",
                                   category="credential_identity_mismatch")
        if flow.result.refresh_token and flow.result.id_token:
            credentials.update({
                "refresh_token": flow.result.refresh_token,
                "id_token": flow.result.id_token,
            })
            db.save_registered(credentials)
            return credentials
    raise TeamServiceError(
        "无法自动取得工作空间 Codex OAuth 凭据；请稍后重试或由账号本人完成授权",
        category="workspace_codex_oauth_required",
    )


def _retry_delivery_with_workspace_login(workspace: dict, email: str, spec: dict,
                                         request_id: str) -> tuple[dict, dict]:
    oauth = account_oauth(workspace, email)
    credentials = _authorize_workspace_codex(workspace, email, oauth["credentials"])
    workspace_token = str(oauth.get("workspace_access_token") or "")
    member_workspace = {
        **workspace, "owner_access_token": workspace_token,
        "owner_session_token": credentials.get("session_token", ""),
        "owner_device_id": credentials.get("device_id", ""),
    }
    usage = TeamClient(member_workspace, session=oauth["_session"]).probe_usage(workspace_token)
    if usage.get("status") != "ok":
        raise TeamServiceError("account usage is not eligible for delivery",
                               category="usage_not_deliverable", detail=usage)
    delivery = Sub2ApiClient(workspace).deliver(
        email, {**credentials, "access_token": workspace_token},
        request_id=request_id,
        binding_id=str(spec.get("binding_id") or email),
        generation=spec.get("generation", workspace.get("generation", 1)),
    )
    return usage, delivery


def auto_join_accounts(workspace: dict, accounts: list[str | dict]) -> dict:
    results = []
    batch_request_id = str(uuid.uuid4())
    for index, value in enumerate(accounts or []):
        spec = _account_spec(value)
        email = spec["email"]
        try:
            if not email:
                raise ValueError("email is required")
            owner_client = TeamClient(workspace)
            member_snapshot = owner_client.list_members()
            if member_snapshot["coverage"] < 1:
                raise TeamServiceError(
                    "member cannot be verified from a partial snapshot",
                    category="verification_incomplete", detail=member_snapshot,
                )
            matched = next(
                (member for member in member_snapshot["members"] if _member_email(member) == email),
                None,
            )
            was_member = matched is not None
            oauth = account_oauth(workspace, email, _exchange=matched is not None)
            credentials = dict(oauth["credentials"])
            member_workspace = {
                **dict(workspace or {}),
                "owner_access_token": credentials.get("access_token", ""),
                "owner_session_token": credentials.get("session_token", ""),
                "owner_device_id": credentials.get("device_id", ""),
            }
            member_client = TeamClient(member_workspace, session=oauth["_session"])
            if matched is not None:
                workspace_access_token = str(oauth.get("workspace_access_token") or "")
                if not workspace_access_token:
                    raise TeamServiceError(
                        "workspace access token is missing",
                        category="workspace_token_missing",
                    )
            else:
                invites = owner_client.list_invites()["items"]
                if not any(_member_email(row) == email for row in invites):
                    sent = owner_client.send_invites([email])
                    if not sent["account_invites"] or sent["errored_emails"]:
                        raise TeamServiceError(
                            "workspace invitation was not accepted by upstream",
                            category="invite_failed", detail=sent["errored_emails"],
                        )
                member_client.accept_invite()
                workspace_access_token = ""
                member_snapshot = TeamClient(workspace).list_members()
                if member_snapshot["coverage"] < 1:
                    raise TeamServiceError(
                        "joined member cannot be verified from a partial snapshot",
                        category="verification_incomplete", detail=member_snapshot,
                    )
                matched = next(
                    (member for member in member_snapshot["members"] if _member_email(member) == email),
                    None,
                )
            if matched is None:
                raise TeamServiceError(
                    "joined account is absent from member snapshot",
                    category="verification_failed", detail=member_snapshot,
                )
            if not was_member:
                oauth = account_oauth(workspace, email)
                credentials = dict(oauth["credentials"])
                workspace_access_token = str(oauth.get("workspace_access_token") or "")
                member_client = TeamClient({
                    **dict(workspace or {}),
                    "owner_access_token": workspace_access_token,
                    "owner_session_token": credentials.get("session_token", ""),
                    "owner_device_id": credentials.get("device_id", ""),
                }, session=oauth["_session"])

            usage = member_client.probe_usage(workspace_access_token)
            if usage.get("status") != "ok":
                raise TeamServiceError(
                    "account usage is not eligible for delivery",
                    category="usage_not_deliverable", detail=usage,
                )
            delivered_credentials = {**credentials, "access_token": workspace_access_token}
            request_id = str(spec.get("request_id") or f"{batch_request_id}-{index}")
            try:
                delivery = Sub2ApiClient(workspace).deliver(
                    email, delivered_credentials, request_id=request_id,
                    binding_id=str(spec.get("binding_id") or email),
                    generation=spec.get("generation", workspace.get("generation", 1)),
                )
            except TeamServiceError as exc:
                if exc.category not in (
                    "credential_workspace_mismatch", "credential_identity_mismatch",
                ):
                    raise
                usage, delivery = _retry_delivery_with_workspace_login(workspace, email, spec, request_id)
            results.append({
                "email": email, "ok": True, "member": matched,
                "usage": usage, "delivery": delivery,
            })
        except Exception as exc:
            results.append(_batch_failure(email, exc))
    return {"results": results}


def board_accounts(workspace: dict, accounts: list[str | dict]) -> dict:
    return auto_join_accounts(workspace, accounts)


def push_members_to_sub2api(workspace: dict, accounts: list[str | dict]) -> dict:
    try:
        snapshot = TeamClient(workspace).list_members()
        if snapshot["coverage"] < 1:
            raise TeamServiceError(
                "members cannot be delivered from a partial snapshot",
                category="verification_incomplete", detail=snapshot,
            )
    except Exception as exc:
        return {
            "results": [
                _batch_failure(_account_spec(value)["email"], exc)
                for value in (accounts or [])
            ],
        }

    results = []
    batch_request_id = str(uuid.uuid4())
    for index, value in enumerate(accounts or []):
        spec = _account_spec(value)
        email = spec["email"]
        try:
            if not email:
                raise ValueError("email is required")
            member = next(
                (row for row in snapshot["members"] if _member_email(row) == email),
                None,
            )
            if member is None:
                raise TeamServiceError(
                    "member not found in current workspace",
                    category="member_not_found", detail={"email": email},
                )

            supplied = {
                key: str((spec.get("credentials") or {}).get(key) or "").strip()
                for key in ("access_token", "refresh_token", "id_token")
            }
            if any(supplied.values()):
                missing = [key for key, value in supplied.items() if not value]
                if missing:
                    raise TeamServiceError(
                        f"incomplete OAuth credentials: {', '.join(missing)}",
                        category="oauth_credentials_incomplete",
                    )
                credentials = supplied
                workspace_access_token = supplied["access_token"]
                session = None
            else:
                try:
                    oauth = account_oauth(workspace, email)
                except TeamServiceError as exc:
                    if exc.category != "local_account_missing":
                        raise
                    raise TeamServiceError(
                        "remote-only member requires OAuth credentials",
                        category="oauth_credentials_required",
                        detail={"email": email},
                    ) from exc
                credentials = dict(oauth["credentials"])
                workspace_access_token = str(oauth.get("workspace_access_token") or "")
                session = oauth.get("_session")
            if not workspace_access_token:
                raise TeamServiceError(
                    "workspace access token is missing",
                    category="workspace_token_missing",
                )
            member_workspace = {
                **dict(workspace or {}),
                "owner_access_token": workspace_access_token,
                "owner_session_token": credentials.get("session_token", ""),
                "owner_device_id": credentials.get("device_id", ""),
            }
            usage = TeamClient(
                member_workspace, session=session,
            ).probe_usage(workspace_access_token)
            if usage.get("status") != "ok":
                raise TeamServiceError(
                    "account usage is not eligible for delivery",
                    category="usage_not_deliverable", detail=usage,
                )
            if any(supplied.values()):
                identity = usage.get("identity") if isinstance(usage.get("identity"), dict) else {}
                identity_user = identity.get("user") if isinstance(identity.get("user"), dict) else {}
                identity_email = str(
                    identity.get("email") or identity_user.get("email") or ""
                ).strip().lower()
                identity_id = str(
                    identity.get("id") or identity.get("user_id")
                    or identity_user.get("id") or ""
                ).strip()
                member_id = str(
                    member.get("account_user_id") or member.get("user_id")
                    or member.get("id") or ""
                ).strip()
                if not identity_email and not identity_id:
                    raise TeamServiceError(
                        "OAuth credential identity cannot be verified",
                        category="credential_identity_unverified",
                    )
                if (
                    (identity_email and identity_email != email)
                    or (identity_id and member_id and identity_id != member_id)
                ):
                    raise TeamServiceError(
                        "OAuth credentials do not belong to the selected member",
                        category="credential_identity_mismatch",
                        detail={"email": email},
                    )
            request_id = str(spec.get("request_id") or f"{batch_request_id}-{index}")
            try:
                delivery = Sub2ApiClient(workspace).deliver(
                    email, {**credentials, "access_token": workspace_access_token},
                    request_id=request_id,
                    binding_id=str(spec.get("binding_id") or email),
                    generation=spec.get("generation", workspace.get("generation", 1)),
                    **({"persist_local": False} if any(supplied.values()) else {}),
                )
            except TeamServiceError as exc:
                if any(supplied.values()) or exc.category not in (
                    "credential_workspace_mismatch", "credential_identity_mismatch",
                ):
                    raise
                usage, delivery = _retry_delivery_with_workspace_login(workspace, email, spec, request_id)
            results.append({
                "email": email, "ok": True, "member": member,
                "usage": usage, "delivery": delivery,
            })
        except Exception as exc:
            results.append(_batch_failure(email, exc))
    return {"results": results}


def offboard_accounts(workspace: dict, accounts: list[str | dict]) -> dict:
    client = TeamClient(workspace)
    sub2api = None
    remote_accounts = None
    try:
        snapshot = client.list_members()
        if snapshot["coverage"] < 1:
            raise TeamServiceError(
                "members cannot be offboarded from a partial snapshot",
                category="verification_incomplete", detail=snapshot,
            )
    except Exception as exc:
        return {
            "results": [
                _batch_failure(_account_spec(value)["email"], exc)
                for value in (accounts or [])
            ],
        }

    results = []
    for value in accounts or []:
        spec = _account_spec(value)
        email = spec["email"]
        try:
            user_id = str(spec.get("user_id") or "").strip()
            member = next((row for row in snapshot["members"] if (
                user_id and str(row.get("id") or row.get("user_id") or "") == user_id
            ) or (not user_id and _member_email(row) == email)), None)
            if member is None:
                raise TeamServiceError(
                    "member not found", category="member_not_found",
                    detail={"email": email, "user_id": user_id},
                )
            member_user = member.get("user") if isinstance(member.get("user"), dict) else {}
            member_account = member.get("account") if isinstance(member.get("account"), dict) else {}
            actual_email = _member_email(member)
            if email and actual_email and email != actual_email:
                raise TeamServiceError(
                    "member email and user_id identify different accounts",
                    category="identity_mismatch", detail=member,
                )
            role = str(
                member.get("role") or member.get("account_user_role")
                or member_user.get("role") or member_user.get("account_user_role")
                or member_account.get("role") or member_account.get("account_user_role")
                or ""
            ).lower()
            if "owner" in role:
                raise TeamServiceError(
                    "workspace owner cannot be offboarded",
                    category="owner_protected", detail=member,
                )
            owner_email = str(workspace.get("owner_email") or "").strip().lower()
            if owner_email and actual_email == owner_email:
                raise TeamServiceError(
                    "configured workspace owner cannot be offboarded",
                    category="owner_protected", detail=member,
                )
            if not role or not any(marker in role for marker in ("member", "user", "admin")):
                raise TeamServiceError(
                    "member role is missing or unrecognized",
                    category="role_unverified", detail=member,
                )
            user_id = user_id or str(member.get("id") or member.get("user_id") or "").strip()
            if not user_id:
                raise TeamServiceError(
                    "member record omitted user id", category="unsupported_schema", detail=spec,
                )
            if sub2api is None:
                sub2api = Sub2ApiClient(workspace)
                remote_accounts = sub2api.list_accounts()
            remote = sub2api.member_account(remote_accounts, actual_email or email, spec.get("binding"))
            remote_removal = (
                sub2api.remove_from_groups(remote) if remote is not None
                else {"ok": True, "already_absent": True}
            )
            member_token = str(
                spec.get("workspace_access_token") or spec.get("access_token") or ""
            ).strip()
            if not member_token:
                try:
                    member_token = str(
                        account_oauth(workspace, actual_email or email).get("workspace_access_token")
                        or ""
                    ).strip()
                except Exception:
                    member_token = ""
            member_token = member_token or str(workspace.get("owner_access_token") or "").strip()
            try:
                removed = client.remove_member(user_id, access_token=member_token)
            except TeamServiceError as exc:
                owner_token = str(workspace.get("owner_access_token") or "").strip()
                if exc.status_code not in (401, 403) or not owner_token or member_token == owner_token:
                    raise
                detail_text = str(exc.detail or "").lower()
                if exc.status_code == 403 and ("<html" in detail_text or "cloudflare" in detail_text):
                    raise
                refreshed_token = ""
                try:
                    refreshed_token = str(
                        account_oauth(workspace, actual_email or email).get("workspace_access_token")
                        or ""
                    ).strip()
                except Exception:
                    pass
                if refreshed_token and refreshed_token != member_token:
                    try:
                        removed = client.remove_member(user_id, access_token=refreshed_token)
                    except TeamServiceError:
                        removed = client.remove_member(user_id, access_token=owner_token)
                else:
                    removed = client.remove_member(user_id, access_token=owner_token)
            results.append({
                "email": email, "user_id": user_id, "ok": True,
                "removal": removed, "sub2api": remote_removal,
            })
        except Exception as exc:
            results.append(_batch_failure(email, exc))
    return {"results": results}
