"""SQLite development store: atomic checkpoints/events and optimistic writes.

No distributed leases, shared reservations, outbox or database-admin tamper defense.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .core import ConflictError, ContractError, VERSION, canonical, digest, strict_json


class SQLiteRunStore:
    def __init__(self, path: str | Path, *, protocol=VERSION):
        if protocol not in (VERSION, 'trailforge/execution/0.1'):
            raise ContractError('unsupported local store protocol')
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.protocol = protocol
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, binding_digest TEXT NOT NULL,
                    version INTEGER NOT NULL, payload TEXT NOT NULL, checksum TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL, sequence INTEGER NOT NULL,
                    kind TEXT NOT NULL, state_digest TEXT NOT NULL,
                    PRIMARY KEY (run_id, sequence)
                );
            """)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, record: dict) -> None:
        if record["schema_version"] != self.protocol or record["state_version"] != 0:
            raise ContractError("invalid initial record")
        try:
            with self.connect() as db:
                db.execute("INSERT INTO runs VALUES (?, ?, ?, ?, ?)",
                           (record["run_id"], digest(record["binding"]), 0,
                            canonical(record), digest(record)))
                db.execute("INSERT INTO events VALUES (?, 0, 'ADMITTED', ?)",
                           (record["run_id"], digest(record)))
        except sqlite3.IntegrityError as exc:
            raise ConflictError("run already exists") from exc

    def load(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT binding_digest, version, payload, checksum FROM runs WHERE run_id=?",
                             (run_id,)).fetchone()
            if row is None:
                raise ContractError("unknown run")
            record = strict_json(row[2])
            if not isinstance(record, dict) or digest(record) != row[3]:
                raise ContractError("checkpoint checksum mismatch")
            if (record.get("schema_version") != self.protocol or record.get("run_id") != run_id
                    or record.get("state_version") != row[1]
                    or digest(record.get("binding")) != row[0]):
                raise ContractError("checkpoint identity/version mismatch")
            last = db.execute("SELECT sequence, state_digest FROM events WHERE run_id=? ORDER BY sequence DESC LIMIT 1",
                              (run_id,)).fetchone()
            if last != (row[1], row[3]):
                raise ContractError("checkpoint/event mismatch")
            return record

    def save(self, record: dict, expected_version: int, event: str) -> dict:
        with self.connect() as db:
            return self.save_in_transaction(db, record, expected_version, event)

    def save_in_transaction(self, db, record: dict, expected_version: int, event: str) -> dict:
        """Trusted adapter hook for atomic admission plus other local ledgers."""
        if record.get('schema_version') != self.protocol:
            raise ContractError('checkpoint/store protocol mismatch')
        updated = {**record, "state_version": expected_version + 1}
        checksum = digest(updated)
        cursor = db.execute("""UPDATE runs SET version=?, payload=?, checksum=?
                WHERE run_id=? AND version=? AND binding_digest=?""",
                                (updated["state_version"], canonical(updated), checksum,
                                 updated["run_id"], expected_version, digest(updated["binding"])))
        if cursor.rowcount != 1:
            raise ConflictError("stale version or changed binding")
        db.execute("INSERT INTO events VALUES (?, ?, ?, ?)",
                       (updated["run_id"], updated["state_version"], event, checksum))
        return updated

    def events(self, run_id: str) -> list[dict]:
        self.load(run_id)
        with self.connect() as db:
            return [{"sequence": row[0], "kind": row[1], "state_digest": row[2]}
                    for row in db.execute("SELECT sequence, kind, state_digest FROM events WHERE run_id=? ORDER BY sequence",
                                          (run_id,))]
