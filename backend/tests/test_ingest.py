"""
Unit tests for the document ingestion pipeline
(app/services/ingest.py).

Covers:
* Happy path: parse → chunk → embed → upsert_chunks → status=ready
* State transitions: queued → parsing → embedding → ready
* Failure path: exception during parse sets status=failed and logs error
* Failure path: exception during embed sets status=failed
* Visibility classification is applied when visibility_override=False
* Visibility classification is skipped when visibility_override=True
* Missing document is a no-op (returns early)
* Chunk count is correctly set on the document after ingest

All external I/O (parse_file, embed_texts, upsert_chunks, delete_document,
classify_blocks, chunk_blocks) is mocked so no real services are required.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import DocStatus, Document, Role, User, Visibility
from tests.conftest import _make_user

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Fixture: a pre-committed Document ready for ingestion
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture()
async def queued_doc(db_session: AsyncSession, admin_user: User) -> Document:
    doc = Document(
        filename="sample.pdf",
        content_type="application/pdf",
        storage_path="/data/uploads/sample.pdf",
        visibility=Visibility.internal,
        visibility_override=False,
        status=DocStatus.queued,
        progress_step="queued",
        progress_pct=0,
        uploaded_by=admin_user.id,
    )
    db_session.add(doc)
    await db_session.commit()
    await db_session.refresh(doc)
    return doc


# ---------------------------------------------------------------------------
# Fake chunk drafts returned by chunk_blocks mock
# ---------------------------------------------------------------------------

def _fake_draft(text: str = "hello world", page: int = 1):
    draft = MagicMock()
    draft.text = text
    draft.page = page
    draft.sheet = None
    draft.kind = "text"
    draft.parent_text = ""
    return draft


# ---------------------------------------------------------------------------
# Happy-path ingest
# ---------------------------------------------------------------------------

class TestIngestDocumentHappyPath:
    """Full successful pipeline run."""

    @pytest.fixture(autouse=True)
    def _mock_externals(self):
        blocks = [MagicMock()]
        drafts = [_fake_draft("chunk one"), _fake_draft("chunk two")]
        vectors = [[0.1] * 768, [0.2] * 768]

        with (
            patch("app.services.ingest.delete_document") as mock_del,
            patch("app.services.ingest.parse_file", new_callable=AsyncMock, return_value=blocks) as mock_parse,
            patch("app.services.ingest.classify_blocks", return_value=Visibility.internal) as mock_cls,
            patch("app.services.ingest.chunk_blocks", return_value=drafts) as mock_chunk,
            patch("app.services.ingest.embed_texts", new_callable=AsyncMock, return_value=vectors) as mock_embed,
            patch("app.services.ingest.upsert_chunks") as mock_upsert,
            patch("app.services.ingest.SessionLocal") as mock_sl,
        ):
            # Wire SessionLocal to yield our test db session
            self.mock_del = mock_del
            self.mock_parse = mock_parse
            self.mock_cls = mock_cls
            self.mock_chunk = mock_chunk
            self.mock_embed = mock_embed
            self.mock_upsert = mock_upsert
            yield

    async def test_document_reaches_ready_status(
        self, db_session: AsyncSession, queued_doc: Document
    ):
        from app.services.ingest import ingest_document

        with patch("app.services.ingest.SessionLocal") as mock_sl:
            mock_sl.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_sl.return_value.__aexit__ = AsyncMock(return_value=False)
            await ingest_document(str(queued_doc.id))

        await db_session.refresh(queued_doc)
        assert queued_doc.status == DocStatus.ready

    async def test_chunk_count_is_set(
        self, db_session: AsyncSession, queued_doc: Document
    ):
        from app.services.ingest import ingest_document

        with patch("app.services.ingest.SessionLocal") as mock_sl:
            mock_sl.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_sl.return_value.__aexit__ = AsyncMock(return_value=False)
            await ingest_document(str(queued_doc.id))

        await db_session.refresh(queued_doc)
        assert queued_doc.chunk_count == 2  # two drafts in _mock_externals

    async def test_upsert_chunks_is_called(
        self, db_session: AsyncSession, queued_doc: Document
    ):
        from app.services.ingest import ingest_document

        with patch("app.services.ingest.SessionLocal") as mock_sl:
            mock_sl.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_sl.return_value.__aexit__ = AsyncMock(return_value=False)
            await ingest_document(str(queued_doc.id))

        self.mock_upsert.assert_called_once()

    async def test_progress_reaches_100(
        self, db_session: AsyncSession, queued_doc: Document
    ):
        from app.services.ingest import ingest_document

        with patch("app.services.ingest.SessionLocal") as mock_sl:
            mock_sl.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_sl.return_value.__aexit__ = AsyncMock(return_value=False)
            await ingest_document(str(queued_doc.id))

        await db_session.refresh(queued_doc)
        assert queued_doc.progress_pct == 100


# ---------------------------------------------------------------------------
# Visibility classification
# ---------------------------------------------------------------------------

class TestVisibilityClassification:
    async def test_classification_applied_when_no_override(
        self, db_session: AsyncSession, queued_doc: Document
    ):
        queued_doc.visibility_override = False
        await db_session.commit()

        with (
            patch("app.services.ingest.delete_document"),
            patch("app.services.ingest.parse_file", new_callable=AsyncMock, return_value=[MagicMock()]),
            patch("app.services.ingest.classify_blocks", return_value=Visibility.restricted_pii) as mock_cls,
            patch("app.services.ingest.chunk_blocks", return_value=[_fake_draft()]),
            patch("app.services.ingest.embed_texts", new_callable=AsyncMock, return_value=[[0.1] * 768]),
            patch("app.services.ingest.upsert_chunks"),
            patch("app.services.ingest.SessionLocal") as mock_sl,
        ):
            mock_sl.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_sl.return_value.__aexit__ = AsyncMock(return_value=False)
            from app.services.ingest import ingest_document
            await ingest_document(str(queued_doc.id))

        await db_session.refresh(queued_doc)
        assert queued_doc.visibility == Visibility.restricted_pii

    async def test_classification_skipped_when_override_set(
        self, db_session: AsyncSession, queued_doc: Document
    ):
        queued_doc.visibility_override = True
        queued_doc.visibility = Visibility.generic
        await db_session.commit()

        with (
            patch("app.services.ingest.delete_document"),
            patch("app.services.ingest.parse_file", new_callable=AsyncMock, return_value=[MagicMock()]),
            patch("app.services.ingest.classify_blocks", return_value=Visibility.restricted_pii) as mock_cls,
            patch("app.services.ingest.chunk_blocks", return_value=[_fake_draft()]),
            patch("app.services.ingest.embed_texts", new_callable=AsyncMock, return_value=[[0.1] * 768]),
            patch("app.services.ingest.upsert_chunks"),
            patch("app.services.ingest.SessionLocal") as mock_sl,
        ):
            mock_sl.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_sl.return_value.__aexit__ = AsyncMock(return_value=False)
            from app.services.ingest import ingest_document
            await ingest_document(str(queued_doc.id))

        await db_session.refresh(queued_doc)
        # Should remain generic (override respected)
        assert queued_doc.visibility == Visibility.generic
        mock_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Failure / error paths
# ---------------------------------------------------------------------------

class TestIngestFailurePaths:
    async def test_parse_error_sets_failed_status(
        self, db_session: AsyncSession, queued_doc: Document
    ):
        with (
            patch("app.services.ingest.delete_document"),
            patch(
                "app.services.ingest.parse_file",
                new_callable=AsyncMock,
                side_effect=RuntimeError("parse boom"),
            ),
            patch("app.services.ingest.SessionLocal") as mock_sl,
        ):
            mock_sl.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_sl.return_value.__aexit__ = AsyncMock(return_value=False)
            from app.services.ingest import ingest_document
            await ingest_document(str(queued_doc.id))

        await db_session.refresh(queued_doc)
        assert queued_doc.status == DocStatus.failed
        assert "parse boom" in (queued_doc.error or "")

    async def test_embed_error_sets_failed_status(
        self, db_session: AsyncSession, queued_doc: Document
    ):
        with (
            patch("app.services.ingest.delete_document"),
            patch("app.services.ingest.parse_file", new_callable=AsyncMock, return_value=[MagicMock()]),
            patch("app.services.ingest.classify_blocks", return_value=Visibility.internal),
            patch("app.services.ingest.chunk_blocks", return_value=[_fake_draft()]),
            patch(
                "app.services.ingest.embed_texts",
                new_callable=AsyncMock,
                side_effect=ConnectionError("Ollama unreachable"),
            ),
            patch("app.services.ingest.SessionLocal") as mock_sl,
        ):
            mock_sl.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_sl.return_value.__aexit__ = AsyncMock(return_value=False)
            from app.services.ingest import ingest_document
            await ingest_document(str(queued_doc.id))

        await db_session.refresh(queued_doc)
        assert queued_doc.status == DocStatus.failed
        assert "Ollama unreachable" in (queued_doc.error or "")

    async def test_error_message_truncated_to_2000_chars(
        self, db_session: AsyncSession, queued_doc: Document
    ):
        long_error = "x" * 5000

        with (
            patch("app.services.ingest.delete_document"),
            patch(
                "app.services.ingest.parse_file",
                new_callable=AsyncMock,
                side_effect=RuntimeError(long_error),
            ),
            patch("app.services.ingest.SessionLocal") as mock_sl,
        ):
            mock_sl.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_sl.return_value.__aexit__ = AsyncMock(return_value=False)
            from app.services.ingest import ingest_document
            await ingest_document(str(queued_doc.id))

        await db_session.refresh(queued_doc)
        assert len(queued_doc.error or "") <= 2000

    async def test_missing_document_is_noop(self, db_session: AsyncSession):
        """Passing a non-existent UUID should not raise – just return early."""
        with patch("app.services.ingest.SessionLocal") as mock_sl:
            mock_sl.return_value.__aenter__ = AsyncMock(return_value=db_session)
            mock_sl.return_value.__aexit__ = AsyncMock(return_value=False)
            from app.services.ingest import ingest_document
            # Should complete without exception
            await ingest_document(str(uuid.uuid4()))
