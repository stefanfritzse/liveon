"""Storage for the admin console password.

The password used to come only from ``LIVEON_ADMIN_PASSWORD``, which meant changing
it required editing a Kubernetes secret and restarting the pod. A password set from
the console is stored here instead — hashed, never in plain text — and takes
precedence over the environment variable, which remains the bootstrap for a fresh
deployment.

Hashing uses :func:`hashlib.scrypt` from the standard library: memory-hard, no extra
dependency, and fast enough that verifying on each request is unnoticeable.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

LOGGER = logging.getLogger(__name__)

#: scrypt work factors. n=2**14 verifies in roughly 50ms on a laptop, which is a
#: sensible trade for a console whose pages each trigger one verification.
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DERIVED_KEY_BYTES = 32
_SALT_BYTES = 16

#: Short enough not to obstruct a local deployment, long enough to be worth having.
MIN_PASSWORD_LENGTH = 12


class AdminCredentialStore:
    """Persist a single hashed admin password."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA journal_mode = WAL;")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS admin_credential (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    salt BLOB NOT NULL,
                    password_hash BLOB NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    # ------------------------------------------------------------------
    # Hashing
    # ------------------------------------------------------------------
    @staticmethod
    def _derive(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=_SCRYPT_N,
            r=_SCRYPT_R,
            p=_SCRYPT_P,
            dklen=_DERIVED_KEY_BYTES,
        )

    # ------------------------------------------------------------------
    # Reading and writing
    # ------------------------------------------------------------------
    def is_configured(self) -> bool:
        """Return ``True`` when a password has been set from the console."""

        row = self._conn.execute("SELECT 1 FROM admin_credential WHERE id = 1;").fetchone()
        return row is not None

    def set_password(self, password: str) -> None:
        """Replace the stored password. The caller is responsible for authorising this."""

        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
            )

        salt = secrets.token_bytes(_SALT_BYTES)
        digest = self._derive(password, salt)
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO admin_credential(id, salt, password_hash, updated_at)
                VALUES (1, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    salt = excluded.salt,
                    password_hash = excluded.password_hash,
                    updated_at = excluded.updated_at;
                """,
                (salt, digest, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
        LOGGER.info("Admin password updated", extra={"event": "admin.password_changed"})

    def verify(self, password: str) -> bool:
        """Return ``True`` when ``password`` matches the stored one."""

        row = self._conn.execute(
            "SELECT salt, password_hash FROM admin_credential WHERE id = 1;"
        ).fetchone()
        if row is None:
            return False
        candidate = self._derive(password, bytes(row["salt"]))
        return hmac.compare_digest(candidate, bytes(row["password_hash"]))

    def clear(self) -> None:
        """Forget the stored password so the environment variable applies again.

        This is the recovery path for a forgotten console password.
        """

        with self._conn:
            self._conn.execute("DELETE FROM admin_credential WHERE id = 1;")
        LOGGER.warning("Admin password cleared", extra={"event": "admin.password_cleared"})

    def updated_at(self) -> str | None:
        row = self._conn.execute(
            "SELECT updated_at FROM admin_credential WHERE id = 1;"
        ).fetchone()
        return row["updated_at"] if row else None

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:  # pragma: no cover - shutdown must not raise
            pass


def resolve_credential_db_path() -> Path:
    """Return the database file the credential store should use."""

    raw = (os.getenv("LIVEON_DB_PATH") or "").strip()
    if raw:
        return Path(raw)
    return Path.home() / "liveon" / "data" / "content.db"


def create_admin_credential_store() -> AdminCredentialStore:
    """Build a credential store against the configured database."""

    return AdminCredentialStore(resolve_credential_db_path())
