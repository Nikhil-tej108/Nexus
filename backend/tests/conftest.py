"""
Shared fixtures for the Nexus backend test suite.

Strategy
--------
* Use an **in-memory SQLite** database (aiosqlite) so tests are fully
  isolated from any real PostgreSQL instance.
* Override the FastAPI dependency-injection graph so every test uses
  the in-memory DB session and mocked external services.
* Provide helper factories that create DB rows directly (no HTTP round-
  trip) for use as test pre-conditions.
* Mock Redis and Qdrant at import-time using ``unittest.mock.patch`` so
  no real network calls are made.
"""
from __future__ import annotations

import uuid
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ---------------------------------------------------------------------------
# Environment bootstrap – must happen BEFORE any app imports so that
# get_settings() is populated with test-safe values.
# ---------------------------------------------------------------------------
import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/0")
os.environ.setdefault("QDRANT_URL", "http://localhost:6333")
os.environ.setdefault("JWT_SECRET", "test-secret-key-for-testing-only")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("ADMIN_BOOTSTRAP_EMAIL", "admin@nexus.local")
os.environ.setdefault("ADMIN_BOOTSTRAP_PASSWORD", "ChangeMeNow!")

# ---------------------------------------------------------------------------
# Patch external services BEFORE app modules are imported.
# ---------------------------------------------------------------------------

# --- Redis mock ---
_redis_mock = AsyncMock()
_redis_mock.incr = AsyncMock(return_value=0)
_redis_mock.expire = AsyncMock(return_value=True)
_redis_mock.lpush = AsyncMock(return_value=1)
_redis_mock.get = AsyncMock(return_value=None)
_redis_mock.set = AsyncMock(return_value=True)
_redis_mock.delete = AsyncMock(return_value=1)
_redis_mock.brpop = AsyncMock(return_value=None)

async def _scan_iter(*args, **kwargs):
    return
    yield  # make it an async generator

_redis_mock.scan_iter = _scan_iter

_get_redis_patch = patch(
    "app.services.redis_client.get_redis",
    return_value=_redis_mock,
)
_get_redis_patch.start()

# Patch queue module that imports get_redis at module level
_queue_redis_patch = patch(
    "app.services.queue.get_redis",
    return_value=_redis_mock,
)
_queue_redis_patch.start()

# --- Qdrant mock ---
_qdrant_mock = MagicMock()
_qdrant_mock.upsert = MagicMock()
_qdrant_mock.delete = MagicMock()
_qdrant_mock.search = MagicMock(return_value=[])

_qdrant_patch = patch(
    "app.services.qdrant_store.QdrantClient",
    return_value=_qdrant_mock,
)
_qdrant_patch.start()

# ---------------------------------------------------------------------------
# Now it is safe to import app modules.
# ---------------------------------------------------------------------------
from app.config import get_settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Role, User  # noqa: E402
from app.security import create_token, hash_password, new_csrf  # noqa: E402
from app.schemas import TokenUser  # noqa: E402

settings = get_settings()

# ---------------------------------------------------------------------------
# In-memory async engine (SQLite via aiosqlite).
# SQLite does not support PostgreSQL-specific types (UUID, JSONB, Enum with
# ``create_constraint``), so we need ``render_as_batch`` and some workarounds.
# ---------------------------------------------------------------------------
_TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

# We need to handle the PostgreSQL-specific Enum/UUID columns.
# Use a patched engine that maps them to SQLite-compatible equivalents.
from sqlalchemy import event  # noqa: E402
from sqlalchemy.dialects import sqlite  # noqa: E402

test_engine = create_async_engine(
    _TEST_DB_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)

TestSessionLocal = async_sessionmaker(
    test_engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


async def _create_tables() -> None:
    """Create all tables in the test SQLite database."""
    # We need to adapt PostgreSQL-specific columns.
    # Patch UUID columns to use String(36) for SQLite compatibility.
    from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB
    from sqlalchemy import String, Text

    @event.listens_for(test_engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_conn, connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with test_engine.begin() as conn:
        # Use a custom metadata render that maps PG types to SQLite types.
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                checkfirst=True,
            )
        )


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _setup_db():
    """Create all ORM tables once per test session."""
    # Monkey-patch SQLAlchemy type mappings for SQLite compatibility.
    # This must happen before create_all is called.
    from sqlalchemy.dialects.postgresql import UUID as PG_UUID, JSONB
    from sqlalchemy import String, Text, TypeDecorator

    class _UUIDString(TypeDecorator):
        impl = String(36)
        cache_ok = True

        def process_bind_param(self, value, dialect):
            if value is None:
                return None
            return str(value)

        def process_result_value(self, value, dialect):
            if value is None:
                return None
            import uuid
            return uuid.UUID(value)

    # Override the PostgreSQL UUID type used in column definitions
    import sqlalchemy.dialects.postgresql as pg_dialect
    pg_dialect.UUID = _UUIDString  # type: ignore[attr-defined]

    # Override JSONB → Text for SQLite
    class _JSONBText(TypeDecorator):
        impl = Text
        cache_ok = True

        def process_bind_param(self, value, dialect):
            if value is None:
                return None
            import json
            return json.dumps(value)

        def process_result_value(self, value, dialect):
            if value is None:
                return None
            import json
            return json.loads(value)

    pg_dialect.JSONB = _JSONBText  # type: ignore[attr-defined]

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest_asyncio.fixture()
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Provide a fresh, auto-rolling-back DB session for each test."""
    async with TestSessionLocal() as session:
        yield session
        await session.rollback()


@pytest.fixture(autouse=True)
def _override_get_db(db_session: AsyncSession):
    """Override FastAPI's get_db dependency to use the test session."""
    from app.db import get_db

    async def _test_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = _test_get_db
    yield
    app.dependency_overrides.pop(get_db, None)


@pytest_asyncio.fixture()
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Async HTTP client wired to the test FastAPI app."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://testserver",
    ) as ac:
        yield ac


# ---------------------------------------------------------------------------
# User factory helpers
# ---------------------------------------------------------------------------

async def _make_user(
    db: AsyncSession,
    email: str,
    role: Role,
    password: str = "TestPassword123!",
    is_active: bool = True,
    must_change_password: bool = False,
) -> User:
    user = User(
        email=email,
        password_hash=hash_password(password) if role != Role.external else None,
        role=role,
        is_active=is_active,
        must_change_password=must_change_password,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest_asyncio.fixture()
async def admin_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, "admin@test.local", Role.admin)


@pytest_asyncio.fixture()
async def internal_user(db_session: AsyncSession) -> User:
    return await _make_user(db_session, "internal@test.local", Role.internal)


@pytest_asyncio.fixture()
async def external_user(db_session: AsyncSession) -> User:
    user = User(
        email="external@test.local",
        password_hash=None,
        role=Role.external,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


# ---------------------------------------------------------------------------
# Auth cookie helpers
# ---------------------------------------------------------------------------

def _auth_cookies(user: User, site: str = "chat") -> dict[str, str]:
    """Return cookie dict (session + CSRF) that the test client can use."""
    token_user = TokenUser(
        id=user.id,
        email=user.email,
        role=user.role,
        must_change_password=user.must_change_password,
    )
    token = create_token(token_user, site)
    csrf = new_csrf()
    cookie_name = settings.admin_cookie if site == "admin" else settings.chat_cookie
    return {
        "cookies": {cookie_name: token, settings.csrf_cookie: csrf},
        "headers": {"X-CSRF-Token": csrf},
    }


def admin_auth(user: User) -> dict[str, Any]:
    return _auth_cookies(user, site="admin")


def chat_auth(user: User) -> dict[str, Any]:
    return _auth_cookies(user, site="chat")


# ---------------------------------------------------------------------------
# Global redis mock fixture so individual tests can introspect calls
# ---------------------------------------------------------------------------

@pytest.fixture()
def redis_mock():
    """Return the shared redis AsyncMock for assertion in tests."""
    _redis_mock.reset_mock()
    _redis_mock.incr = AsyncMock(return_value=0)
    _redis_mock.expire = AsyncMock(return_value=True)
    _redis_mock.lpush = AsyncMock(return_value=1)
    _redis_mock.get = AsyncMock(return_value=None)
    _redis_mock.set = AsyncMock(return_value=True)
    _redis_mock.delete = AsyncMock(return_value=1)
    _redis_mock.brpop = AsyncMock(return_value=None)
    return _redis_mock
