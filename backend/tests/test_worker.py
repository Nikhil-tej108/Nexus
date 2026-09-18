"""
Unit tests for the background ingestion worker (app/worker.py).

Covers:
* Worker dequeues a document ID from Redis and calls ingest_document
* Worker handles an empty queue gracefully (timeout returns None)
* Worker logs and continues when ingest_document raises an exception
* Worker does not crash on repeated failures (resilience loop behaviour)
* Redis brpop is awaited with the correct queue key and timeout

The actual asyncio event loop is used (pytest-asyncio) but all I/O
(Redis, ingest_document, qdrant ensure_collection, DB engine) is mocked.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helper: run the worker main loop for N iterations then stop
# ---------------------------------------------------------------------------

async def _run_worker_iterations(
    n: int,
    brpop_side_effects: list,
    ingest_side_effect=None,
) -> None:
    """
    Drive the worker main() loop for exactly *n* iterations then cancel it.

    brpop_side_effects: list of return values for successive r.brpop calls.
    ingest_side_effect: optional exception to raise from ingest_document.
    """
    call_count = 0

    async def _brpop(key, timeout=5):
        nonlocal call_count
        if call_count < len(brpop_side_effects):
            result = brpop_side_effects[call_count]
        else:
            result = None
        call_count += 1
        if call_count >= n:
            raise asyncio.CancelledError()
        return result

    redis_mock = AsyncMock()
    redis_mock.brpop = _brpop

    ingest_mock = AsyncMock(side_effect=ingest_side_effect)

    with (
        patch("app.worker.get_redis", return_value=redis_mock),
        patch("app.worker.ingest_document", ingest_mock),
        patch("app.worker.ensure_collection"),
        patch("app.worker.engine") as mock_engine,
    ):
        mock_engine.begin.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
        mock_engine.begin.return_value.__aexit__ = AsyncMock(return_value=False)

        from app.worker import main

        try:
            await main()
        except asyncio.CancelledError:
            pass

    return ingest_mock


class TestWorkerLoop:
    async def test_worker_dequeues_and_calls_ingest(self):
        doc_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        ingest_mock = await _run_worker_iterations(
            n=2,
            brpop_side_effects=[(b"nexus:ingest", doc_id)],
        )
        ingest_mock.assert_awaited_once_with(doc_id)

    async def test_worker_skips_empty_queue(self):
        """When brpop returns None (timeout), ingest should NOT be called."""
        ingest_mock = await _run_worker_iterations(
            n=2,
            brpop_side_effects=[None],
        )
        ingest_mock.assert_not_awaited()

    async def test_worker_continues_after_ingest_exception(self):
        """An ingest failure must not crash the worker loop."""
        doc_id = "11111111-2222-3333-4444-555555555555"
        ingest_mock = await _run_worker_iterations(
            n=3,
            brpop_side_effects=[
                (b"nexus:ingest", doc_id),
                (b"nexus:ingest", doc_id),
            ],
            ingest_side_effect=RuntimeError("ingest kaboom"),
        )
        # ingest was attempted twice despite the first failure
        assert ingest_mock.await_count == 2

    async def test_worker_processes_multiple_documents(self):
        doc_ids = [
            "aaaaaaaa-0000-0000-0000-000000000001",
            "aaaaaaaa-0000-0000-0000-000000000002",
            "aaaaaaaa-0000-0000-0000-000000000003",
        ]
        ingest_mock = await _run_worker_iterations(
            n=4,
            brpop_side_effects=[
                (b"nexus:ingest", doc_ids[0]),
                (b"nexus:ingest", doc_ids[1]),
                (b"nexus:ingest", doc_ids[2]),
            ],
        )
        assert ingest_mock.await_count == 3
        actual_ids = [c.args[0] for c in ingest_mock.await_args_list]
        assert actual_ids == doc_ids

    async def test_worker_uses_correct_queue_key(self):
        """Verify brpop is called with QUEUE_KEY, not an arbitrary string."""
        from app.services.queue import QUEUE_KEY

        brpop_calls: list = []

        async def _tracking_brpop(key, timeout=5):
            brpop_calls.append(key)
            raise asyncio.CancelledError()

        redis_mock = AsyncMock()
        redis_mock.brpop = _tracking_brpop

        with (
            patch("app.worker.get_redis", return_value=redis_mock),
            patch("app.worker.ingest_document", new_callable=AsyncMock),
            patch("app.worker.ensure_collection"),
            patch("app.worker.engine") as mock_engine,
        ):
            mock_engine.begin.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            mock_engine.begin.return_value.__aexit__ = AsyncMock(return_value=False)

            from app.worker import main

            try:
                await main()
            except asyncio.CancelledError:
                pass

        assert brpop_calls and brpop_calls[0] == QUEUE_KEY


# ---------------------------------------------------------------------------
# Redis brpop failure simulation
# ---------------------------------------------------------------------------

class TestWorkerRedisFailure:
    async def test_redis_connection_error_propagates(self):
        """
        If the Redis connection drops completely the worker should propagate
        the exception (it is not silenced by the ingest try/except block).
        """

        async def _boom(key, timeout=5):
            raise ConnectionError("Redis gone")

        redis_mock = AsyncMock()
        redis_mock.brpop = _boom

        with (
            patch("app.worker.get_redis", return_value=redis_mock),
            patch("app.worker.ingest_document", new_callable=AsyncMock),
            patch("app.worker.ensure_collection"),
            patch("app.worker.engine") as mock_engine,
        ):
            mock_engine.begin.return_value.__aenter__ = AsyncMock(return_value=MagicMock())
            mock_engine.begin.return_value.__aexit__ = AsyncMock(return_value=False)

            from app.worker import main

            with pytest.raises(ConnectionError, match="Redis gone"):
                await main()
