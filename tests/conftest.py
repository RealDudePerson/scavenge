import asyncio
import os
import sys

import pytest
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool
from starlette.testclient import TestClient

# Make the repo root importable as ``app`` regardless of how pytest is invoked.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import app.main as main  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture
def db_engine(tmp_path):
    # NullPool: connections are never shared across the (several) event loops
    # that pytest, TestClient and asyncio.run each create.
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'test.db'}", poolclass=NullPool
    )
    yield engine
    asyncio.run(engine.dispose())


@pytest.fixture
def session_factory(db_engine):
    return sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def client(db_engine, session_factory, monkeypatch, tmp_path):
    """A TestClient wired to a fresh temporary SQLite database.

    The app's module-level ``async_session`` / ``init_db`` are swapped for the
    test engine so no real ``scavenge.db`` is touched, and the upload dirs are
    redirected into ``tmp_path`` so tests never write into ``uploads/``.
    """

    async def _init_db():
        async with db_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    monkeypatch.setattr(main, "async_session", session_factory)
    monkeypatch.setattr(main, "init_db", _init_db)

    upload_root = tmp_path / "uploads"
    for name, attr in (("originals", "ORIGINALS_DIR"), ("thumb", "THUMB_DIR"), ("display", "DISPLAY_DIR")):
        path = upload_root / name
        path.mkdir(parents=True)
        monkeypatch.setattr(main, attr, str(path))

    # Reset module-level caches / limiters that would otherwise leak between tests.
    main._login_attempts.clear()
    main._position_attempts.clear()
    main._ACTIVE_HUNT_ID = None
    main._ACTIVE_THEME = main.DEFAULT_THEME.copy()

    with TestClient(main.app) as test_client:
        yield test_client
