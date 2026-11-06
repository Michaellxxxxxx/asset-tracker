"""p30: an RQ work-horse is a fork of the worker process. It must never reuse a
pooled DB connection inherited from the parent (that is what produced
"can't change 'autocommit' now: INTRANS" in refresh_address_labels_job and
"server closed the connection" in the parent's poll thread)."""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.pool import QueuePool

from app import db as appdb


def _conn_id(engine) -> int:
    with engine.connect() as c:
        c.execute(text("select 1"))
        return id(c.connection.dbapi_connection)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs fork")
def test_forked_child_does_not_reuse_parent_pool_connection(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'p30.db'}", poolclass=QueuePool)
    appdb.install_fork_safety(engine)
    parent_id = _conn_id(engine)
    assert _conn_id(engine) == parent_id  # pooled reuse in the same process
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # child
        try:
            os.write(w, b"1" if _conn_id(engine) == parent_id else b"0")
        finally:
            os._exit(0)
    os.close(w)
    reused = os.read(r, 1)
    os.waitpid(pid, 0)
    assert reused == b"0", "child reused the parent's pooled connection"
    # parent pool untouched and still usable
    assert _conn_id(engine) == parent_id


def test_app_engine_is_fork_safe():
    assert getattr(appdb.engine, "_p30_fork_safe", False)
