"""P7a migration: old SQLite file DBs gain created_at / next_attempt_at and
lose the NOT NULL on alert_events.sent_at without losing rows."""

from __future__ import annotations

from sqlalchemy import create_engine, inspect, text

from app.migrate import apply_phase2_migrations


def _old_engine(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'old.db'}", future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY, username VARCHAR(64))"))
        conn.execute(text("INSERT INTO users (id, username) VALUES (1, 'u1')"))
        conn.execute(
            text(
                "CREATE TABLE alert_events ("
                "id INTEGER PRIMARY KEY, "
                "user_id INTEGER NOT NULL REFERENCES users(id), "
                "rule_id INTEGER, "
                "position_id INTEGER, "
                "level VARCHAR(16) NOT NULL, "
                "hf NUMERIC(38, 8), "
                "sent_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                "channel_result VARCHAR(255)"
                ")"
            )
        )
        conn.execute(
            text(
                "INSERT INTO alert_events "
                "(id, user_id, level, hf, sent_at, channel_result) "
                "VALUES (7, 1, 'danger', 1.05, '2026-10-01 10:00:00', 'sent')"
            )
        )
        conn.execute(
            text(
                "CREATE TABLE wechat_push_queue ("
                "id INTEGER PRIMARY KEY, "
                "pending_id INTEGER, "
                "alert_event_id INTEGER, "
                "user_id INTEGER NOT NULL, "
                "text TEXT NOT NULL, "
                "status VARCHAR(16) NOT NULL DEFAULT 'queued', "
                "attempts INTEGER NOT NULL DEFAULT 0, "
                "last_error VARCHAR(255), "
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
                "sent_at TIMESTAMP"
                ")"
            )
        )
    return engine


def test_sqlite_migration_is_additive_and_idempotent(tmp_path):
    engine = _old_engine(tmp_path)
    apply_phase2_migrations(engine)
    apply_phase2_migrations(engine)  # second run must be a no-op

    inspector = inspect(engine)
    event_cols = {col["name"]: col for col in inspector.get_columns("alert_events")}
    assert event_cols["created_at"] is not None
    assert event_cols["sent_at"]["nullable"] is True
    queue_cols = {col["name"] for col in inspector.get_columns("wechat_push_queue")}
    assert "next_attempt_at" in queue_cols

    with engine.begin() as conn:
        created_at = conn.execute(
            text("SELECT created_at FROM alert_events WHERE id = 7")
        ).scalar_one()
        # Backfilled from the old sent_at, not "now".
        assert str(created_at).startswith("2026-10-01 10:00:00")
        # New queued rows may leave sent_at NULL.
        conn.execute(
            text(
                "INSERT INTO alert_events (id, user_id, level, hf, sent_at, channel_result) "
                "VALUES (8, 1, 'warn', 1.4, NULL, 'queued:no_context_token')"
            )
        )
        assert (
            conn.execute(text("SELECT sent_at FROM alert_events WHERE id = 8")).scalar_one()
            is None
        )
    engine.dispose()
