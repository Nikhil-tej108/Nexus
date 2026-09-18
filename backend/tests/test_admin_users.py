"""
Integration tests for admin user management APIs
(app/routers/admin_users.py).

Covers:
* GET  /admin/users/internal  – list internal users (admin only)
* GET  /admin/users/external  – list external users (admin only)
* POST /admin/users/internal  – create internal user (admin only)
* POST /admin/users/{id}/disable – disable a user (admin only)
* POST /admin/users/{id}/enable  – re-enable a user (admin only)
* POST /admin/users/{id}/reset-password – reset password (admin only)

Also verifies that non-admin users receive 401/403 on all protected routes.
"""
from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Role, User
from tests.conftest import _make_user, admin_auth, chat_auth

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# GET /admin/users/internal
# ---------------------------------------------------------------------------

class TestListInternalUsers:
    async def test_admin_can_list_internal_users(
        self, client: AsyncClient, admin_user: User, internal_user: User
    ):
        auth = admin_auth(admin_user)
        resp = await client.get(
            "/admin/users/internal",
            cookies=auth["cookies"],
            headers=auth["headers"],
        )
        assert resp.status_code == 200
        emails = [u["email"] for u in resp.json()]
        assert internal_user.email in emails

    async def test_internal_user_cannot_list_internal_users(
        self, client: AsyncClient, internal_user: User
    ):
        auth = chat_auth(internal_user)
        resp = await client.get(
            "/admin/users/internal",
            cookies=auth["cookies"],
            headers=auth["headers"],
        )
        assert resp.status_code in (401, 403)

    async def test_unauthenticated_returns_401(self, client: AsyncClient):
        resp = await client.get("/admin/users/internal")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# GET /admin/users/external
# ---------------------------------------------------------------------------

class TestListExternalUsers:
    async def test_admin_can_list_external_users(
        self, client: AsyncClient, admin_user: User, external_user: User
    ):
        auth = admin_auth(admin_user)
        resp = await client.get(
            "/admin/users/external",
            cookies=auth["cookies"],
            headers=auth["headers"],
        )
        assert resp.status_code == 200
        emails = [u["email"] for u in resp.json()]
        assert external_user.email in emails

    async def test_external_user_forbidden(
        self, client: AsyncClient, external_user: User
    ):
        auth = chat_auth(external_user)
        resp = await client.get(
            "/admin/users/external",
            cookies=auth["cookies"],
            headers=auth["headers"],
        )
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /admin/users/internal  (create)
# ---------------------------------------------------------------------------

class TestCreateInternalUser:
    async def test_admin_creates_internal_user(
        self, client: AsyncClient, admin_user: User
    ):
        auth = admin_auth(admin_user)
        resp = await client.post(
            "/admin/users/internal",
            json={"email": "newinternal@test.local", "password": "InitPassword123!"},
            cookies=auth["cookies"],
            headers={**auth["headers"], "Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["email"] == "newinternal@test.local"
        assert body["role"] == "internal"

    async def test_duplicate_email_returns_409(
        self, client: AsyncClient, admin_user: User, internal_user: User
    ):
        auth = admin_auth(admin_user)
        resp = await client.post(
            "/admin/users/internal",
            json={"email": internal_user.email, "password": "InitPassword123!"},
            cookies=auth["cookies"],
            headers={**auth["headers"], "Content-Type": "application/json"},
        )
        assert resp.status_code == 409

    async def test_non_admin_cannot_create_internal_user(
        self, client: AsyncClient, internal_user: User
    ):
        auth = chat_auth(internal_user)
        resp = await client.post(
            "/admin/users/internal",
            json={"email": "other@test.local", "password": "InitPassword123!"},
            cookies=auth["cookies"],
            headers={**auth["headers"], "Content-Type": "application/json"},
        )
        assert resp.status_code in (401, 403)

    async def test_short_password_rejected(
        self, client: AsyncClient, admin_user: User
    ):
        auth = admin_auth(admin_user)
        resp = await client.post(
            "/admin/users/internal",
            json={"email": "shortpw@test.local", "password": "short"},
            cookies=auth["cookies"],
            headers={**auth["headers"], "Content-Type": "application/json"},
        )
        assert resp.status_code == 422  # Pydantic validation error


# ---------------------------------------------------------------------------
# POST /admin/users/{id}/disable
# ---------------------------------------------------------------------------

class TestDisableUser:
    async def test_admin_can_disable_internal_user(
        self,
        client: AsyncClient,
        admin_user: User,
        internal_user: User,
        db_session: AsyncSession,
    ):
        auth = admin_auth(admin_user)
        resp = await client.post(
            f"/admin/users/{internal_user.id}/disable",
            cookies=auth["cookies"],
            headers=auth["headers"],
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        # Verify the DB state
        await db_session.refresh(internal_user)
        assert internal_user.is_active is False

    async def test_disabling_nonexistent_user_returns_404(
        self, client: AsyncClient, admin_user: User
    ):
        auth = admin_auth(admin_user)
        resp = await client.post(
            f"/admin/users/{uuid.uuid4()}/disable",
            cookies=auth["cookies"],
            headers=auth["headers"],
        )
        assert resp.status_code == 404

    async def test_admin_cannot_be_disabled(
        self, client: AsyncClient, admin_user: User, db_session: AsyncSession
    ):
        """Admins are protected – trying to disable an admin returns 404."""
        second_admin = await _make_user(
            db_session, "admin2@test.local", Role.admin
        )
        auth = admin_auth(admin_user)
        resp = await client.post(
            f"/admin/users/{second_admin.id}/disable",
            cookies=auth["cookies"],
            headers=auth["headers"],
        )
        assert resp.status_code == 404

    async def test_non_admin_cannot_disable_user(
        self, client: AsyncClient, internal_user: User, db_session: AsyncSession
    ):
        target = await _make_user(db_session, "target@test.local", Role.internal)
        auth = chat_auth(internal_user)
        resp = await client.post(
            f"/admin/users/{target.id}/disable",
            cookies=auth["cookies"],
            headers=auth["headers"],
        )
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /admin/users/{id}/enable
# ---------------------------------------------------------------------------

class TestEnableUser:
    async def test_admin_can_enable_disabled_user(
        self,
        client: AsyncClient,
        admin_user: User,
        db_session: AsyncSession,
    ):
        user = await _make_user(
            db_session, "reenable@test.local", Role.internal, is_active=False
        )
        auth = admin_auth(admin_user)
        resp = await client.post(
            f"/admin/users/{user.id}/enable",
            cookies=auth["cookies"],
            headers=auth["headers"],
        )
        assert resp.status_code == 200

        await db_session.refresh(user)
        assert user.is_active is True

    async def test_enable_nonexistent_user_returns_404(
        self, client: AsyncClient, admin_user: User
    ):
        auth = admin_auth(admin_user)
        resp = await client.post(
            f"/admin/users/{uuid.uuid4()}/enable",
            cookies=auth["cookies"],
            headers=auth["headers"],
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# POST /admin/users/{id}/reset-password
# ---------------------------------------------------------------------------

class TestResetPassword:
    async def test_admin_can_reset_internal_user_password(
        self,
        client: AsyncClient,
        admin_user: User,
        internal_user: User,
        db_session: AsyncSession,
    ):
        auth = admin_auth(admin_user)
        resp = await client.post(
            f"/admin/users/{internal_user.id}/reset-password",
            json={"new_password": "FreshNewPassword123!"},
            cookies=auth["cookies"],
            headers={**auth["headers"], "Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        # must_change_password flag should be set
        await db_session.refresh(internal_user)
        assert internal_user.must_change_password is True

    async def test_reset_external_user_password_returns_404(
        self, client: AsyncClient, admin_user: User, external_user: User
    ):
        """Only internal user passwords can be reset via this endpoint."""
        auth = admin_auth(admin_user)
        resp = await client.post(
            f"/admin/users/{external_user.id}/reset-password",
            json={"new_password": "FreshNewPassword123!"},
            cookies=auth["cookies"],
            headers={**auth["headers"], "Content-Type": "application/json"},
        )
        assert resp.status_code == 404

    async def test_non_admin_cannot_reset_password(
        self, client: AsyncClient, internal_user: User
    ):
        auth = chat_auth(internal_user)
        resp = await client.post(
            f"/admin/users/{internal_user.id}/reset-password",
            json={"new_password": "FreshNewPassword123!"},
            cookies=auth["cookies"],
            headers={**auth["headers"], "Content-Type": "application/json"},
        )
        assert resp.status_code in (401, 403)

    async def test_short_new_password_rejected(
        self, client: AsyncClient, admin_user: User, internal_user: User
    ):
        auth = admin_auth(admin_user)
        resp = await client.post(
            f"/admin/users/{internal_user.id}/reset-password",
            json={"new_password": "short"},
            cookies=auth["cookies"],
            headers={**auth["headers"], "Content-Type": "application/json"},
        )
        assert resp.status_code == 422
