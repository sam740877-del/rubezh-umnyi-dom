import os
import tempfile
from pathlib import Path

import pytest

# Тесты работают на отдельной временной базе, а не на рабочей ski.db
_TMP_DIR = tempfile.mkdtemp(prefix="ski-tests-")
os.environ["SKI_DATABASE_URL"] = f"sqlite:///{Path(_TMP_DIR) / 'test.db'}"

from app.database import SessionLocal, engine, init_db  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture()
def session():
    Base.metadata.drop_all(engine)
    init_db()
    db = SessionLocal()
    try:
        yield db
    finally:
        db.rollback()
        db.close()
