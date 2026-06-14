import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.api.deps import get_db
from app.db.base import Base
from app.db.init_db import seed_companies, seed_peers
from app.main import app


@pytest.fixture()
def db_session(tmp_path_factory):
    db_dir = tmp_path_factory.mktemp("sqlite-db")
    db_path = db_dir / "test_runtime.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    with TestingSession() as session:
        seed_companies(session)
        seed_peers(session)
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()
    if db_path.exists():
        db_path.unlink()


@pytest.fixture()
def client(db_session):
    def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
