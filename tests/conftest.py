"""Wspólna baza in-memory dla testów webappki.

`DATABASE_URL` musi być ustawiony **przed** importem `webapp.db` (engine
tworzy się przy imporcie), więc robimy to tutaj, na samej górze.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="transcripts-tests-"))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP / 'test.db'}")
os.environ.setdefault("RECALL_DIR", str(_TMP / "recall"))
os.environ.setdefault("TRANSCRIPTS_DIR", str(_TMP / "transcripts"))
os.environ.setdefault("AUTH_DISABLED", "1")

import pytest  # noqa: E402
from webapp.db import SessionLocal, engine  # noqa: E402
from webapp.models import Base  # noqa: E402


@pytest.fixture()
def session():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()
