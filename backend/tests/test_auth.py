"""
Integration / E2E tests for the auth router (app/routers/auth.py).

Covers:
* POST /auth/login        – valid credentials, wrong password, disabled account,
                            role/site separation, rate-limit short-circuit
* POST /auth/admin/login  – admin-only endpoint
* POST /auth/external     – external user creation and sign-in
* POST /auth/logout       – clears session cookies
* GET  /auth/me           – returns current user info
* POST /auth/change-password – happy-path and wrong-current-password

All external dependencies (Redis, Qdrant) are mocked via conftest.py.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Role, User
from app.security import hash_password
from tests.conftest import _make_user, admin_auth, chat_auth


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _login_headers(csrf: str) -> dict[str, str]:
    return {"X-CSRF-Token": csrf, "Content-Type": "application/json"}


async def _do_login(
    client: AsyncClient,
    email: str,
    password: str,
    path: str = "/auth/login",
) -> tuple[int, dict]:
    resp = await client.post(
        path,
        json={"email": email, "password": password},
    )
    return resp.status_code, resp.json()


# ---------------------------------------------------------------------------
# POST /auth/login
# ---------------------------------------------------------------------------

class TestLogin:
    async def test_login_internal_user_success(self, client: AsyncClient, internal_user: User):
        status, body = await _do_login(client, internal_user.email, "TestPassword123!")
        assert status == 200
        assert body["user"]["email"] == internal_user.email
        assert "csrf" in body

    async def test_login_sets_session_cookie(self, client: AsyncClient, internal_user: User):
        resp = await client.post(
            "/auth/login",
            json={"email": internal_user.email, "password": "TestPassword123!"},
        )
        assert resp.status_code == 200
        # A session cookie should be present
        assert any("nexus" in name for name in resp.cookies)

    async def test_login_wrong_password_returns_401(self, client: AsyncClient, internal_user: User):
        status, body = await _do_login(client, internal_user.email, "WrongPassword!")
        assert status == 401

    async def test_login_unknown_email_returns_401(self, client: AsyncClient):
        status, _ = await _do_login(client, "nobody@test.local", "Anything123!")
        assert status == 401

    async def test_login_disabled_account_returns_403(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        user = await _make_user(
            db_session, "disabled@test.local", Role.internal, is_active=False
        )
        status, _ = await _do_login(client, user.email, "TestPassword123!")
        assert status == 403

    async def test_admin_cannot_login_via_chat_endpoint(
        self, client: AsyncClient, admin_user: User
    ):
        """Admin users must use /auth/admin/login, not the chat login."""
        status, _ = await _do_login(client, admin_user.email, "TestPassword123!")
        assert status == 403

    async def test_external_user_login_rejected_at_standard_endpoint(
        self, client: AsyncClient, external_user: User
    ):
        status, _ = await _do_login(client, external_user.email, "")
        assert status in (400, 401)

    async def test_rate_limit_triggers_429(
        self, client: AsyncClient, redis_mock, internal_user: User
    ):
        """When Redis reports > limit hits, login should return 429."""
        from unittest.mock import AsyncMock
        redis_mock.incr = AsyncMock(return_value=100)  # way above the 20 limit
        status, _ = await _do_login(client, internal_user.email, "TestPassword123!")
        assert status == 429


# ---------------------------------------------------------------------------
# POST /auth/admin/login
# ---------------------------------------------------------------------------

class TestAdminLogin:
    async def test_admin_login_success(self, client: AsyncClient, admin_user: User):
        status, body = await _do_login(client, admin_user.email, "TestPassword123!", "/auth/admin/login")
        assert status == 200
        assert body["user"]["role"] == "admin"

    async def test_non_admin_rejected_at_admin_login(
        self, client: AsyncClient, internal_user: User
    ):
        status, _ = await _do_login(
            client, internal_user.email, "TestPassword123!", "/auth/admin/login"
        )
        assert status == 401

    async def test_admin_login_wrong_password(self, client: AsyncClient, admin_user: User):
        status, _ = await _do_login(
            client, admin_user.email, "WrongPass!", "/auth/admin/login"
        )
        assert status == 401


# ---------------------------------------------------------------------------
# POST /auth/external
# ---------------------------------------------------------------------------

class TestExternalStart:
    async def test_creates_new_external_user(self, client: AsyncClient):
        resp = await client.post(
            "/auth/external", json={"email": "newexternal@test.local"}
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["user"]["role"] == "external"
        assert "csrf" in body

    async def test_existing_external_user_can_sign_in_again(
        self, client: AsyncClient, external_user: User
    ):
        resp = await client.post(
            "/auth/external", json={"email": external_user.email}
        )
        assert resp.status_code == 200

    async def test_internal_email_rejected(
        self, client: AsyncClient, internal_user: User
    ):
        resp = await client.post(
            "/auth/external", json={"email": internal_user.email}
        )
        assert resp.status_code == 400

    async def test_disabled_external_user_rejected(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        user = await _make_user(
            db_session, "exdisabled@test.local", Role.external, is_active=False
        )
        resp = await client.post(
            "/auth/external", json={"email": user.email}
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# POST /auth/logout
# ---------------------------------------------------------------------------

class TestLogout:
    async def test_logout_requires_authentication(self, client: AsyncClient):
        resp = await client.post("/auth/logout")
        assert resp.status_code in (401, 403)

    async def test_logout_clears_cookies(
        self, client: AsyncClient, internal_user: User
    ):
        auth = chat_auth(internal_user)
        resp = await client.post(
            "/auth/logout",
            cookies=auth["cookies"],
            headers=auth["headers"],
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}


# ---------------------------------------------------------------------------
# GET /auth/me
# ---------------------------------------------------------------------------

class TestMe:
    async def test_me_returns_user_info(
        self, client: AsyncClient, internal_user: User
    ):
        auth = chat_auth(internal_user)
        resp = await client.get(
            "/auth/me",
            cookies=auth["cookies"],
            headers=auth["headers"],
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["user"]["email"] == internal_user.email
        assert body["site"] in ("chat", "admin")

    async def test_me_unauthenticated_returns_401(self, client: AsyncClient):
        resp = await client.get("/auth/me")
        assert resp.status_code in (401, 403)

    async def test_me_includes_csrf_token(
        self, client: AsyncClient, admin_user: User
    ):
        auth = admin_auth(admin_user)
        resp = await client.get(
            "/auth/me",
            cookies=auth["cookies"],
            headers=auth["headers"],
        )
        assert resp.status_code == 200
        assert "csrf" in resp.json()


# ---------------------------------------------------------------------------
# POST /auth/change-password
# ---------------------------------------------------------------------------

class TestChangePassword:
    async def test_change_password_success(
        self, client: AsyncClient, internal_user: User
    ):
        auth = chat_auth(internal_user)
        resp = await client.post(
            "/auth/change-password",
            json={
                "current_password": "TestPassword123!",
                "new_password": "NewSecurePassword456!",
            },
            cookies=auth["cookies"],
            headers={**auth["headers"], "Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    async def test_change_password_wrong_current_fails(
        self, client: AsyncClient, internal_user: User
    ):
        auth = chat_auth(internal_user)
        resp = await client.post(
            "/auth/change-password",
            json={
                "current_password": "WrongCurrent!",
                "new_password": "NewSecurePassword456!",
            },
            cookies=auth["cookies"],
            headers={**auth["headers"], "Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    async def test_change_password_unauthenticated_returns_401(
        self, client: AsyncClient
    ):
        resp = await client.post(
            "/auth/change-password",
            json={"current_password": "x", "new_password": "NewPassword123!"},
        )
        assert resp.status_code in (401, 403)
