"""L'empreinte des facteurs de double authentification, sur SQL réel : rien de posé se lit
None, poser remplace (`db/empreintes_2fa.py`). Patron de base éphémère :
`test_account_suspension_db_live.py::live`."""
from __future__ import annotations

import os
import uuid

import pytest


@pytest.fixture(scope="module")
def live(pg_module_dsn):
    avant = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = pg_module_dsn
    try:
        from oto_mcp.db import init_db
        init_db()
        yield
    finally:
        if avant is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = avant


def test_rien_de_pose_puis_poser_remplace(live):
    from oto_mcp.db import empreintes_2fa
    sub = f"u-{uuid.uuid4().hex[:8]}"
    assert empreintes_2fa.lire(sub) is None
    empreintes_2fa.poser(sub, "a" * 64)
    assert empreintes_2fa.lire(sub) == "a" * 64
    empreintes_2fa.poser(sub, "b" * 64)
    assert empreintes_2fa.lire(sub) == "b" * 64
