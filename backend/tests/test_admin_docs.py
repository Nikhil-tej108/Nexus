"""
Integration tests for the admin document management API
(app/routers/admin_docs.py).

Covers:
* GET    /admin/documents          – list documents (admin only)
* POST   /admin/documents          – upload a document (admin only)
* PATCH  /admin/documents/{id}/visibility – override visibility (admin only)
* DELETE /admin/documents/{id}    – delete a document (admin only)

External services mocked:
* Redis  – enqueue_ingest / invalidate_cache (via conftest.py)
* Qdrant – delete_document (via conftest.py)
* File I/O – storage writes and os.remove are patched per test
"""
from __future__ import annotations

import io
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DocStatus, Document, Role, User, Visibility
from tests.conftest import admin_auth, chat_auth, _make_user

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Shared document factory
# ---------------------------------------------------------------------------

async def _make_document(db: AsyncSession, admin: User) -> Document:
    doc = Document(
        filename="test.pdf",
        content_type="application/pdf",
        storage_path="/tmp/test.pdf",
        visibility=Visibility.internal,
        visibility_override=False,
        status=DocStatus.ready,
        progress_step="ready",
        progress_pct=100,
        uploaded_by=admin.id,
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    return doc


# ---------------------------------------------------------------------------
# GET /admin/documents
# ---------------------------------------------------------------------------

class TestListDocuments:
    async def test_admin_can_list_documents(
        self, client: AsyncClient, admin_user: User, db_session: AsyncSession
    ):
        doc = await _make_document(db_session, admin_user)
        auth = admin_auth(admin_user)
        resp = await client.get(
            "/admin/documents",
            cookies=auth["cookies"],
            headers=auth["headers"],
        )
        assert resp.status_code == 200
        ids = [d["id"] for d in resp.json()]
        assert str(doc.id) in ids

    async def test_internal_user_cannot_list_documents(
        self, client: AsyncClient, internal_user: User
    ):
        auth = chat_auth(internal_user)
        resp = await client.get(
            "/admin/documents",
            cookies=auth["cookies"],
            headers=auth["headers"],
        )
        assert resp.status_code in (401, 403)

    async def test_unauthenticated_cannot_list_documents(
        self, client: AsyncClient
    ):
        resp = await client.get("/admin/documents")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /admin/documents  (upload)
# ---------------------------------------------------------------------------

class TestUploadDocument:
    @pytest.fixture(autouse=True)
    def _patch_storage(self, tmp_path):
        """Redirect file writes to a temp directory."""
        with patch("app.routers.admin_docs.settings") as mock_settings:
            mock_settings.max_upload_mb = 50
            mock_settings.upload_dir = str(tmp_path)
            mock_settings.admin_cookie = "nexus_admin"
            mock_settings.csrf_cookie = "nexus_csrf"
            yield mock_settings

    async def test_admin_can_upload_pdf(
        self, client: AsyncClient, admin_user: User
    ):
        auth = admin_auth(admin_user)
        with patch("app.routers.admin_docs.enqueue_ingest", new_callable=AsyncMock):
            with patch("app.routers.admin_docs.audit", new_callable=AsyncMock):
                resp = await client.post(
                    "/admin/documents",
                    files={"file": ("report.pdf", io.BytesIO(b"%PDF-1.4 test"), "application/pdf")},
                    cookies=auth["cookies"],
                    headers=auth["headers"],
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["filename"] == "report.pdf"
        assert body["status"] == "queued"

    async def test_unsupported_file_type_returns_400(
        self, client: AsyncClient, admin_user: User
    ):
        auth = admin_auth(admin_user)
        with patch("app.routers.admin_docs.enqueue_ingest", new_callable=AsyncMock):
            with patch("app.routers.admin_docs.audit", new_callable=AsyncMock):
                resp = await client.post(
                    "/admin/documents",
                    files={"file": ("script.exe", io.BytesIO(b"MZ"), "application/octet-stream")},
                    cookies=auth["cookies"],
                    headers=auth["headers"],
                )
        assert resp.status_code == 400

    async def test_file_too_large_returns_413(
        self, client: AsyncClient, admin_user: User
    ):
        auth = admin_auth(admin_user)
        # 1 byte more than 50 MB limit
        large_data = b"x" * (50 * 1024 * 1024 + 1)
        with patch("app.routers.admin_docs.enqueue_ingest", new_callable=AsyncMock):
            with patch("app.routers.admin_docs.audit", new_callable=AsyncMock):
                resp = await client.post(
                    "/admin/documents",
                    files={"file": ("big.pdf", io.BytesIO(large_data), "application/pdf")},
                    cookies=auth["cookies"],
                    headers=auth["headers"],
                )
        assert resp.status_code == 413

    async def test_non_admin_cannot_upload(
        self, client: AsyncClient, internal_user: User
    ):
        auth = chat_auth(internal_user)
        resp = await client.post(
            "/admin/documents",
            files={"file": ("test.pdf", io.BytesIO(b"%PDF"), "application/pdf")},
            cookies=auth["cookies"],
            headers=auth["headers"],
        )
        assert resp.status_code in (401, 403)

    async def test_upload_with_visibility_override(
        self, client: AsyncClient, admin_user: User
    ):
        auth = admin_auth(admin_user)
        with patch("app.routers.admin_docs.enqueue_ingest", new_callable=AsyncMock):
            with patch("app.routers.admin_docs.audit", new_callable=AsyncMock):
                resp = await client.post(
                    "/admin/documents",
                    data={"visibility": "generic"},
                    files={"file": ("doc.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
                    cookies=auth["cookies"],
                    headers=auth["headers"],
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["visibility"] == "generic"
        assert body["visibility_override"] is True


# ---------------------------------------------------------------------------
# PATCH /admin/documents/{id}/visibility
# ---------------------------------------------------------------------------

class TestOverrideVisibility:
    async def test_admin_can_override_visibility(
        self, client: AsyncClient, admin_user: User, db_session: AsyncSession
    ):
        doc = await _make_document(db_session, admin_user)
        auth = admin_auth(admin_user)
        with patch("app.routers.admin_docs.enqueue_ingest", new_callable=AsyncMock):
            with patch("app.routers.admin_docs.invalidate_cache", new_callable=AsyncMock):
                with patch("app.routers.admin_docs.audit", new_callable=AsyncMock):
                    resp = await client.patch(
                        f"/admin/documents/{doc.id}/visibility",
                        json={"visibility": "generic"},
                        cookies=auth["cookies"],
                        headers={**auth["headers"], "Content-Type": "application/json"},
                    )
        assert resp.status_code == 200
        assert resp.json()["visibility"] == "generic"

    async def test_override_nonexistent_doc_returns_404(
        self, client: AsyncClient, admin_user: User
    ):
        auth = admin_auth(admin_user)
        resp = await client.patch(
            f"/admin/documents/{uuid.uuid4()}/visibility",
            json={"visibility": "generic"},
            cookies=auth["cookies"],
            headers={**auth["headers"], "Content-Type": "application/json"},
        )
        assert resp.status_code == 404

    async def test_non_admin_cannot_override_visibility(
        self, client: AsyncClient, internal_user: User, admin_user: User, db_session: AsyncSession
    ):
        doc = await _make_document(db_session, admin_user)
        auth = chat_auth(internal_user)
        resp = await client.patch(
            f"/admin/documents/{doc.id}/visibility",
            json={"visibility": "generic"},
            cookies=auth["cookies"],
            headers={**auth["headers"], "Content-Type": "application/json"},
        )
        assert resp.status_code in (401, 403)

    async def test_invalid_visibility_value_returns_422(
        self, client: AsyncClient, admin_user: User, db_session: AsyncSession
    ):
        doc = await _make_document(db_session, admin_user)
        auth = admin_auth(admin_user)
        resp = await client.patch(
            f"/admin/documents/{doc.id}/visibility",
            json={"visibility": "INVALID_VALUE"},
            cookies=auth["cookies"],
            headers={**auth["headers"], "Content-Type": "application/json"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# DELETE /admin/documents/{id}
# ---------------------------------------------------------------------------

class TestDeleteDocument:
    async def test_admin_can_delete_document(
        self, client: AsyncClient, admin_user: User, db_session: AsyncSession
    ):
        doc = await _make_document(db_session, admin_user)
        auth = admin_auth(admin_user)
        with patch("app.routers.admin_docs.delete_document", new_callable=MagicMock):
            with patch("os.remove"):
                with patch("app.routers.admin_docs.invalidate_cache", new_callable=AsyncMock):
                    with patch("app.routers.admin_docs.audit", new_callable=AsyncMock):
                        resp = await client.delete(
                            f"/admin/documents/{doc.id}",
                            cookies=auth["cookies"],
                            headers=auth["headers"],
                        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

    async def test_delete_nonexistent_doc_returns_404(
        self, client: AsyncClient, admin_user: User
    ):
        auth = admin_auth(admin_user)
        resp = await client.delete(
            f"/admin/documents/{uuid.uuid4()}",
            cookies=auth["cookies"],
            headers=auth["headers"],
        )
        assert resp.status_code == 404

    async def test_non_admin_cannot_delete_document(
        self, client: AsyncClient, internal_user: User, admin_user: User, db_session: AsyncSession
    ):
        doc = await _make_document(db_session, admin_user)
        auth = chat_auth(internal_user)
        resp = await client.delete(
            f"/admin/documents/{doc.id}",
            cookies=auth["cookies"],
            headers=auth["headers"],
        )
        assert resp.status_code in (401, 403)

    async def test_unauthenticated_cannot_delete_document(
        self, client: AsyncClient, admin_user: User, db_session: AsyncSession
    ):
        doc = await _make_document(db_session, admin_user)
        resp = await client.delete(f"/admin/documents/{doc.id}")
        assert resp.status_code in (401, 403)
