"""Test fixtures for pytest — function-scoped DB isolation."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Must be set before importing app modules
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["TESTING"] = "true"
os.environ["DEBUG"] = "true"

# Keep the Tkinter overlay windows off the visible desktop during tests. The
# app still maps/deiconifies them (needed for real geometry assertions), but
# positions them far off-screen so running the suite never covers the
# developer's display. See StealthOverlayApp._init_window.
os.environ.setdefault("KIYRA_OVERLAY_OFFSCREEN", "1")

# Point the vector store at a throwaway dir. Without this, tests share the real
# ./data/chroma: an in-memory DB whose document ids collide with production, so
# `delete_document_chunks()` in a test wipes the candidate's real embeddings.
import atexit

_TEST_CHROMA_DIR = tempfile.mkdtemp(prefix="kiyra_test_chroma_")
os.environ["CHROMA_DIR"] = _TEST_CHROMA_DIR
atexit.register(shutil.rmtree, _TEST_CHROMA_DIR, ignore_errors=True)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from db.database import Base, get_db
from main import app
from db.models import User, ModelConfiguration


def _make_engine():
    return create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )


def _seed_db(session):
    """Insert the minimum required rows for a clean test DB."""
    if not session.query(User).filter_by(username="local").first():
        session.add(User(username="local"))
    if not session.query(ModelConfiguration).filter_by(is_active=True).first():
        session.add(ModelConfiguration())
    session.commit()


@pytest.fixture
def client():
    """
    Function-scoped test client with a fresh in-memory database per test.
    Each test gets a completely isolated database — no state leaks.
    """
    engine = _make_engine()
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

    # Seed the minimum required rows
    seed_session = TestingSessionLocal()
    try:
        _seed_db(seed_session)
    finally:
        seed_session.close()

    def override_get_db():
        db = TestingSessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()

    Base.metadata.drop_all(bind=engine)
    engine.dispose()


@pytest.fixture
def db_session(client):
    """
    Direct DB session access for tests that need to inspect or write DB state.
    Uses the same engine as the client fixture (shares dependency override).
    """
    # Get the session factory from the current override
    _db_gen = app.dependency_overrides[get_db]()
    db = next(_db_gen)
    try:
        yield db
    finally:
        try:
            next(_db_gen)
        except StopIteration:
            pass


@pytest.fixture(scope="session")
def tk_root():
    """The single Tk root shared by every Tkinter-driven test module.

    Creating a second Tk interpreter in the same process after a previous one
    has been destroyed intermittently fails on CPython 3.14 / Windows with
    ``_tkinter.TclError: invalid command name "tcl_findLibrary"``. Each module
    that needs a window therefore reuses this one root and destroys only its
    own child widgets, never the root itself.
    """
    tk = pytest.importorskip("tkinter")
    root = tk.Tk()
    root.withdraw()
    yield root
    try:
        root.destroy()
    except Exception:
        pass
    tk._default_root = None
