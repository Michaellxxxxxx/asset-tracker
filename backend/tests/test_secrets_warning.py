"""Default-secret startup warnings and MASTER_KEY rotation."""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy.orm import Session

from app.config import (
    DEV_JWT_SECRET_DEFAULT,
    DEV_MASTER_KEY_DEFAULT,
    settings,
    warn_if_default_secrets,
)
from app.crypto import decrypt_with_key, encrypt_with_key
from app.models import ApiCredential, User
from scripts.rotate_master_key import RotateError, rotate_credentials

OLD_KEY = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
NEW_KEY = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
OTHER_KEY = "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"


def test_warns_when_secrets_are_dev_defaults(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        "app.config.settings",
        replace(
            settings,
            jwt_secret=DEV_JWT_SECRET_DEFAULT,
            master_key=DEV_MASTER_KEY_DEFAULT,
        ),
    )
    caplog.set_level(logging.WARNING)
    hits = warn_if_default_secrets(logging.getLogger("test.secrets.warning"))
    assert hits == ["JWT_SECRET", "MASTER_KEY"]
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    text = caplog.text
    assert "JWT_SECRET 仍为开发默认，请轮换后再用于生产" in text
    assert "MASTER_KEY 仍为开发默认，请轮换后再用于生产" in text
    assert DEV_JWT_SECRET_DEFAULT not in text
    assert DEV_MASTER_KEY_DEFAULT not in text


def test_no_warning_when_secrets_rotated(monkeypatch, caplog) -> None:
    monkeypatch.setattr(
        "app.config.settings",
        replace(settings, jwt_secret="rotated-jwt-secret-value", master_key=NEW_KEY),
    )
    caplog.set_level(logging.WARNING)
    assert warn_if_default_secrets(logging.getLogger("test.secrets.warning")) == []
    assert "仍为开发默认" not in caplog.text


def _user(db: Session) -> User:
    user = User(username="rotate-user", hashed_password="x", is_admin=False)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _add_credential(db: Session, user_id: int, name: str, plaintext: str, key: str) -> ApiCredential:
    row = ApiCredential(
        user_id=user_id,
        exchange_name=name,
        encrypted_payload=encrypt_with_key(key, plaintext),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def test_rotate_migrates_then_skips(db: Session) -> None:
    user = _user(db)
    plaintexts = {"binance": "k1:s1", "gate": "k2:s2"}
    for name, text in plaintexts.items():
        _add_credential(db, user.id, name, text, OLD_KEY)

    first = rotate_credentials(db, OLD_KEY, NEW_KEY, dry_run=False)
    db.commit()
    assert (first.migrated, first.skipped, first.total) == (2, 0, 2)

    rows = db.query(ApiCredential).order_by(ApiCredential.id).all()
    for row in rows:
        opened = decrypt_with_key(NEW_KEY, bytes(row.encrypted_payload))
        assert opened == plaintexts[row.exchange_name]
        with pytest.raises(InvalidTag):
            decrypt_with_key(OLD_KEY, bytes(row.encrypted_payload))

    second = rotate_credentials(db, OLD_KEY, NEW_KEY, dry_run=False)
    db.commit()
    assert (second.migrated, second.skipped, second.total) == (0, 2, 2)
    again = db.query(ApiCredential).order_by(ApiCredential.id).all()
    for row in again:
        assert decrypt_with_key(NEW_KEY, bytes(row.encrypted_payload)) == plaintexts[row.exchange_name]


def test_rotate_wrong_key_rolls_back(db: Session) -> None:
    user = _user(db)
    row = _add_credential(db, user.id, "binance", "keep-me", OLD_KEY)
    original = bytes(row.encrypted_payload)

    with pytest.raises(RotateError, match="cannot be decrypted"):
        rotate_credentials(db, OTHER_KEY, NEW_KEY, dry_run=False)
    db.rollback()

    fresh = db.get(ApiCredential, row.id)
    assert fresh is not None
    assert bytes(fresh.encrypted_payload) == original
    assert decrypt_with_key(OLD_KEY, bytes(fresh.encrypted_payload)) == "keep-me"


def test_rotate_dry_run_does_not_write(db: Session) -> None:
    user = _user(db)
    row = _add_credential(db, user.id, "gate", "untouched", OLD_KEY)
    original = bytes(row.encrypted_payload)

    result = rotate_credentials(db, OLD_KEY, NEW_KEY, dry_run=True)
    db.rollback()
    assert (result.migrated, result.skipped, result.total) == (1, 0, 1)

    fresh = db.get(ApiCredential, row.id)
    assert fresh is not None
    assert bytes(fresh.encrypted_payload) == original
    assert decrypt_with_key(OLD_KEY, bytes(fresh.encrypted_payload)) == "untouched"


def test_cli_dry_run_and_migrate(capsys) -> None:
    import tempfile
    from pathlib import Path

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as OrmSession

    from app.db import Base
    from scripts.rotate_master_key import main

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rotate.sqlite"
        file_url = f"sqlite+pysqlite:///{path}"
        file_engine = create_engine(file_url, future=True)
        Base.metadata.create_all(file_engine)
        with OrmSession(file_engine) as session:
            owner = User(username="cli-user", hashed_password="x", is_admin=False)
            session.add(owner)
            session.flush()
            session.add(
                ApiCredential(
                    user_id=owner.id,
                    exchange_name="binance",
                    encrypted_payload=encrypt_with_key(OLD_KEY, "cli-secret"),
                )
            )
            session.commit()
        file_engine.dispose()

        code = main(
            [
                "--old-key",
                OLD_KEY,
                "--new-key",
                NEW_KEY,
                "--database-url",
                file_url,
                "--dry-run",
            ]
        )
        assert code == 0
        assert capsys.readouterr().out.strip() == "would_migrate=1 skipped=0 total=1"

        code = main(
            [
                "--old-key",
                OLD_KEY,
                "--new-key",
                NEW_KEY,
                "--database-url",
                file_url,
                "--yes",
            ]
        )
        assert code == 0
        assert capsys.readouterr().out.strip() == "migrated=1 skipped=0 total=1"

        check = create_engine(file_url, future=True)
        with OrmSession(check) as session:
            stored = session.query(ApiCredential).one()
            assert decrypt_with_key(NEW_KEY, bytes(stored.encrypted_payload)) == "cli-secret"
            assert OLD_KEY not in capsys.readouterr().out
            assert NEW_KEY not in capsys.readouterr().err
        check.dispose()
