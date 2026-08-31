"""Test fixtures for pytest — function-scoped DB isolation."""
from __future__ import annotations

import os
import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Must be set before importing app modules
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["TESTING"] = "true"
os.environ["DEBUG"] = "true"

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
