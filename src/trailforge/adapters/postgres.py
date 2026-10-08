"""Postgres foundation authority, exercised with effect-free mock calls.

Caller scope is trusted host input, not authenticated customer identity. Durable
run-wakeup dispatch is supported; external effects and paid pricing are not.
"""

from __future__ import annotations

from contextlib import contextmanager
from importlib.resources import files
import json
import re
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from ..contracts import (PROTOCOL_VERSION, BudgetError, LeaseError, LeaseToken, integer,
                         validate_artifacts, validate_checkpoint, validate_roles, validate_transition)
from ..core import POLICY, ROLES, ConflictError, ContractError, canonical, digest, strict_json
from ..dispatch import DispatchToken
from .schema import validate_record


class PostgresRunStore:
    def __init__(self, dsn: str, tenant_id: str, resource_id: str, *, schema="trailforge",
                 worker_id: str | None = None, lease_seconds=30, tenant_limit=100, global_limit=1000):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema):
            raise ContractError("invalid database schema name")
        if type(tenant_id) is not str or not tenant_id or type(resource_id) is not str or not resource_id:
            raise ContractError("explicit tenant/resource scope required")
        if type(lease_seconds) not in (int, float) or not 0 < lease_seconds <= 300:
            raise ContractError("lease duration outside local supported bounds")
        integer(tenant_limit)
        integer(global_limit)
        self.dsn, self.tenant_id, self.resource_id, self.schema = dsn, tenant_id, resource_id, schema
        self.worker_id = worker_id or str(uuid4())
        self.lease_seconds = lease_seconds
        self.tenant_limit, self.global_limit = tenant_limit, global_limit

    @contextmanager
    def connect(self):
        # Context commits/rolls back and closes; no DSN logging.
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=5) as db:
            db.execute(sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(sql.Identifier(self.schema)))
            db.execute("SET LOCAL statement_timeout = '10s'")
            db.execute("SET LOCAL lock_timeout = '5s'")
            yield db

    def migrate(self):
        with self.connect() as db:
            db.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("trailforge-migration:" + self.schema,))
            db.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(self.schema)))
            db.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version text PRIMARY KEY, checksum text NOT NULL)")
            if any(row["version"] not in ("001", "002", "003", "004") for row in db.execute("SELECT version FROM schema_migrations")):
                raise ContractError("unsupported database migration version")
            for version, name in (("001", "001_foundation.sql"), ("002", "002_dispatch.sql"),
                                  ("003", "003_broker.sql"), ("004", "004_external_sessions.sql")):
                migration = files("trailforge").joinpath("migrations/" + name).read_text(encoding="utf-8")
                checksum = digest(migration)
                applied = db.execute("SELECT checksum FROM schema_migrations WHERE version=%s", (version,)).fetchone()
                if applied and applied["checksum"] != checksum:
                    raise ContractError("applied migration checksum changed")
                if not applied:
                    db.execute(migration)
                    if version == "002":
                        # A current event supplies a stable wakeup for runs
                        # admitted before dispatch existed. No terminal rerun.
                        rows = db.execute("SELECT * FROM runs WHERE payload::jsonb->>'stage' != 'DRAFTED' FOR UPDATE").fetchall()
                        for row in rows:
                            record = self._decode(db, row)
                            event = db.execute("SELECT kind FROM events WHERE run_id=%s AND sequence=%s",
                                               (row["run_id"], row["version"])).fetchone()["kind"]
                            self._enqueue_dispatch(db, record, event)
                    db.execute("INSERT INTO schema_migrations VALUES (%s, %s)", (version, checksum))

    def _row(self, db, run_id: str, *, lock=False):
        statement = "SELECT *, lease_until > clock_timestamp() AS lease_live FROM runs WHERE run_id=%s AND tenant_id=%s AND resource_id=%s"
        if lock:
            statement += " FOR UPDATE"
        row = db.execute(statement, (run_id, self.tenant_id, self.resource_id)).fetchone()
        if row is None:
            raise ContractError("run unavailable in caller scope")
        return row

    def _decode(self, db, row):
        record = strict_json(row["payload"])
        validate_record("RunCheckpoint", record)
        validate_artifacts(record)
        if (record["run_id"] != row["run_id"] or record["state_version"] != row["version"]
                or record["binding"]["tenant_id"] != row["tenant_id"]
                or record["binding"]["resource_id"] != row["resource_id"]
                or digest(record["binding"]) != row["binding_digest"] or digest(record) != row["checksum"]):
            raise ContractError("checkpoint identity/checksum mismatch")
        last = db.execute("SELECT sequence, state_digest FROM events WHERE run_id=%s ORDER BY sequence DESC LIMIT 1",
                          (row["run_id"],)).fetchone()
        if last is None or last["sequence"] != row["version"] or last["state_digest"] != row["checksum"]:
            raise ContractError("checkpoint journal mismatch")
        ledger = db.execute("SELECT charged FROM budgets WHERE scope_kind='RUN' AND scope_key=%s", (row["run_id"],)).fetchone()
        count = db.execute("SELECT count(*) AS calls FROM reservations WHERE run_id=%s", (row["run_id"],)).fetchone()["calls"]
        if ledger is None or ledger["charged"] != record["model_calls"] or count != record["model_calls"]:
            raise ContractError("checkpoint differs from authoritative reservations")
        return record

    def _scope(self, record):
        if (record["binding"]["tenant_id"] != self.tenant_id or record["binding"]["resource_id"] != self.resource_id):
            raise ContractError("checkpoint is outside caller scope")

    def _fence(self, row, token: LeaseToken):
        if (not isinstance(token, LeaseToken) or token.run_id != row["run_id"]
                or token.worker_id != row["lease_owner"] or token.epoch != row["lease_epoch"] or not row["lease_live"]):
            raise LeaseError("worker lease expired or epoch was replaced")

    def _commit(self, db, row, record, event, details=None, token=None):
        updated = {**record, "state_version": row["version"] + 1}
        checksum = digest(updated)
        statement = "UPDATE runs SET version=%s, payload=%s, checksum=%s WHERE run_id=%s"
        params = [updated["state_version"], canonical(updated), checksum, row["run_id"]]
        if token is not None:
            # Recheck database time at mutation, after all row/budget waits. A
            # previously observed live lease cannot authorize a late commit.
            statement += " AND lease_owner=%s AND lease_epoch=%s AND lease_until > clock_timestamp()"
            params.extend([token.worker_id, token.epoch])
        if db.execute(statement, params).rowcount != 1:
            raise LeaseError("worker lease expired at commit")
        db.execute("INSERT INTO events (run_id, sequence, kind, state_digest, details) VALUES (%s,%s,%s,%s,%s)",
                   (row["run_id"], updated["state_version"], event, checksum, Jsonb(details or {})))
        if event in ("SNAPSHOT_CAPTURED", "MOCK_INVOCATION_ADMITTED", "MOCK_REVIEW_RECORDED", "EVIDENCE_VALIDATED"):
            self._enqueue_dispatch(db, updated, event)
        return updated

    def _enqueue_dispatch(self, db, record, event):
        message = {"schema_version": PROTOCOL_VERSION, "kind": "RUN_WAKEUP",
                   "message_id": f"{record['run_id']}:wake:{record['state_version']}",
                   "run_id": record["run_id"], "binding_digest": digest(record["binding"]),
                   "event_sequence": record["state_version"], "event_kind": event}
        validate_record("DispatchMessage", message)
        db.execute("INSERT INTO dispatch_outbox (message_id,run_id,event_sequence,payload,checksum) VALUES (%s,%s,%s,%s,%s)",
                   (message["message_id"], message["run_id"], message["event_sequence"], Jsonb(message), digest(message)))

    def _budget(self, db, kind, key, limit):
        db.execute("INSERT INTO budgets (scope_kind,scope_key,unit_limit) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                   (kind, key, limit))
        existing = db.execute("SELECT unit_limit FROM budgets WHERE scope_kind=%s AND scope_key=%s", (kind, key)).fetchone()
        if existing["unit_limit"] != limit:
            raise ContractError("configured budget differs from authoritative ledger")

    def create(self, record):
        try:
            with self.connect() as db:
                self._create(db, record)
        except psycopg.errors.UniqueViolation as exc:
            raise ConflictError("run already exists") from exc

    def _create(self, db, record):
        validate_record("RunCheckpoint", record)
        validate_artifacts(record)
        self._scope(record)
        if record["stage"] != "ADMITTED" or record["state_version"] != 0 or record["model_calls"] != 0:
            raise ContractError("invalid initial checkpoint")
        db.execute("INSERT INTO runs (run_id,tenant_id,resource_id,binding_digest,version,payload,checksum) VALUES (%s,%s,%s,%s,0,%s,%s)",
                   (record["run_id"], self.tenant_id, self.resource_id, digest(record["binding"]), canonical(record), digest(record)))
        db.execute("INSERT INTO events (run_id,sequence,kind,state_digest) VALUES (%s,0,'ADMITTED',%s)",
                   (record["run_id"], digest(record)))
        self._budget(db, "GLOBAL", "mock", self.global_limit)
        self._budget(db, "TENANT", self.tenant_id, self.tenant_limit)
        self._budget(db, "RUN", record["run_id"], POLICY["max_model_calls"])
        for role in ROLES:
            task = {"schema_version": PROTOCOL_VERSION, "task_id": record["run_id"] + ":" + role,
                    "run_id": record["run_id"], "role": role, "attempt": 0, "dependencies": [],
                    "status": "PENDING", "binding_digest": digest(record["binding"])}
            db.execute("INSERT INTO tasks VALUES (%s,%s,%s,%s)",
                       (task["task_id"], record["run_id"], role, Jsonb(task)))
        self._enqueue_dispatch(db, record, "ADMITTED")

    def load(self, run_id):
        with self.connect() as db:
            # Same transaction/row lock ensures checkpoint and journal are read consistently.
            row = self._row(db, run_id, lock=True)
            return self._decode(db, row)

    def acquire(self, run_id):
        with self.connect() as db:
            row = self._row(db, run_id, lock=True)
            record = self._decode(db, row)
            if record["stage"] == "DRAFTED":
                raise LeaseError("terminal draft has no mutable worker lease")
            if row["lease_live"]:
                raise LeaseError("run already has a live worker")
            epoch = row["lease_epoch"] + 1
            db.execute("UPDATE runs SET lease_owner=%s, lease_epoch=%s, lease_until=clock_timestamp()+(%s * interval '1 second') WHERE run_id=%s",
                       (self.worker_id, epoch, self.lease_seconds, run_id))
            self._commit(db, row, record, "LEASE_ACQUIRED", {"worker_id": self.worker_id, "epoch": epoch})
            return LeaseToken(run_id, self.worker_id, epoch)

    def release(self, token):
        with self.connect() as db:
            row = self._row(db, token.run_id, lock=True)
            self._fence(row, token)
            record = self._decode(db, row)
            self._commit(db, row, record, "LEASE_RELEASED", {"epoch": token.epoch}, token=token)
            db.execute("UPDATE runs SET lease_owner=NULL, lease_until=NULL WHERE run_id=%s", (token.run_id,))

    def renew(self, token):
        with self.connect() as db:
            row = self._row(db, token.run_id, lock=True)
            self._fence(row, token)
            record = self._decode(db, row)
            if db.execute("UPDATE runs SET lease_until=clock_timestamp()+(%s * interval '1 second') WHERE run_id=%s AND lease_until > clock_timestamp()",
                          (self.lease_seconds, token.run_id)).rowcount != 1:
                raise LeaseError("cannot renew an expired lease")
            return self._commit(db, row, record, "LEASE_RENEWED", {"epoch": token.epoch}, token=token)

    @contextmanager
    def worker(self, run_id):
        token = self.acquire(run_id)
        try:
            yield LeasedRunStore(self, token)
        finally:
            try:
                self.release(token)
            except LeaseError:
                # Expiry/replacement must never release somebody else's lease.
                pass

    def save(self, record, expected_version, event, *, token=None):
        validate_record("RunCheckpoint", record)
        validate_artifacts(record)
        integer(expected_version)
        with self.connect() as db:
            row = self._row(db, record["run_id"], lock=True)
            self._fence(row, token)
            previous = self._decode(db, row)
            if row["version"] != expected_version:
                raise ConflictError("stale state version")
            validate_transition(previous, record, event)
            if event not in ("SNAPSHOT_CAPTURED", "MOCK_REVIEW_RECORDED", "EVIDENCE_VALIDATED", "DRAFT_SAVED") or record["stage"] == previous["stage"]:
                raise ContractError("save requires a declared advancing workflow event")
            if record["model_calls"] != previous["model_calls"]:
                raise ContractError("use atomic model admission, not save, to consume units")
            if event == "MOCK_REVIEW_RECORDED":
                validate_roles(record["review"]["output"]["roles"])
                operation_key = f"{record['run_id']}:mock:{record['model_calls']}"
                reservation = db.execute("SELECT reservation_id,payload FROM reservations WHERE run_id=%s AND operation_key=%s FOR UPDATE",
                                         (record["run_id"], operation_key)).fetchone()
                if reservation is None:
                    raise ContractError("review output lacks reservation")
                settled = {**reservation["payload"], "state": "SETTLED"}
                db.execute("UPDATE reservations SET payload=%s WHERE reservation_id=%s", (Jsonb(settled), reservation["reservation_id"]))
                for role, status in record["review"]["output"]["roles"].items():
                    task = db.execute("SELECT payload FROM tasks WHERE run_id=%s AND role=%s", (record["run_id"], role)).fetchone()["payload"]
                    task.update(status="DONE" if status == "COMPLETE" else status, attempt=record["model_calls"])
                    db.execute("UPDATE tasks SET payload=%s WHERE task_id=%s", (Jsonb(task), task["task_id"]))
            return self._commit(db, row, record, event, token=token)

    def admit_model(self, record, token):
        with self.connect() as db:
            return self._admit_model(db, record, token)

    def _admit_model(self, db, record, token):
        row = self._row(db, record["run_id"], lock=True)
        self._fence(row, token)
        previous = self._decode(db, row)
        if previous != record:
            raise ConflictError("model admission uses stale checkpoint")
        if record["stage"] != "SNAPSHOT" or record["model_calls"] >= POLICY["max_model_calls"]:
            raise BudgetError("run mock invocation allowance exhausted")
        scopes = sorted([("RUN", record["run_id"]), ("TENANT", self.tenant_id), ("GLOBAL", "mock")])
        for kind, key in scopes:
            budget = db.execute("SELECT unit_limit,charged FROM budgets WHERE scope_kind=%s AND scope_key=%s FOR UPDATE", (kind, key)).fetchone()
            if budget is None or budget["charged"] >= budget["unit_limit"]:
                raise BudgetError("authoritative shared mock unit allowance exhausted")
        for kind, key in scopes:
            db.execute("UPDATE budgets SET charged=charged+1 WHERE scope_kind=%s AND scope_key=%s", (kind, key))
        candidate = {**record, "model_calls": record["model_calls"] + 1}
        validate_transition(previous, candidate, "MOCK_INVOCATION_ADMITTED")
        reservation = {"schema_version": PROTOCOL_VERSION, "reservation_id": str(uuid4()), "run_id": record["run_id"],
                       "operation_key": f"{record['run_id']}:mock:{candidate['model_calls']}",
                       "binding_digest": row["binding_digest"], "unit": "MOCK_CALL", "units": 1,
                       "lease_epoch": token.epoch, "state": "CHARGED",
                       "scope_keys": [kind + ":" + key for kind, key in scopes]}
        db.execute("INSERT INTO reservations VALUES (%s,%s,%s,%s)",
                   (reservation["reservation_id"], record["run_id"], reservation["operation_key"], Jsonb(reservation)))
        return self._commit(db, row, candidate, "MOCK_INVOCATION_ADMITTED", {"reservation_id": reservation["reservation_id"]}, token=token)

    def events(self, run_id):
        with self.connect() as db:
            self._decode(db, self._row(db, run_id, lock=True))
            return db.execute("SELECT sequence,kind,state_digest,details FROM events WHERE run_id=%s ORDER BY sequence", (run_id,)).fetchall()

    def reservations(self, run_id):
        with self.connect() as db:
            self._row(db, run_id, lock=True)
            return [r["payload"] for r in db.execute("SELECT payload FROM reservations WHERE run_id=%s ORDER BY operation_key", (run_id,))]

    def tasks(self, run_id):
        with self.connect() as db:
            self._row(db, run_id, lock=True)
            return [r["payload"] for r in db.execute("SELECT payload FROM tasks WHERE run_id=%s ORDER BY role", (run_id,))]

    def budget_usage(self, run_id):
        with self.connect() as db:
            self._row(db, run_id, lock=True)
            return db.execute("SELECT scope_kind,scope_key,unit_limit,charged FROM budgets WHERE (scope_kind='RUN' AND scope_key=%s) OR (scope_kind='TENANT' AND scope_key=%s) OR scope_kind='GLOBAL' ORDER BY scope_kind,scope_key",
                              (run_id, self.tenant_id)).fetchall()

    def _dispatch_message(self, db, row):
        message = row["payload"]
        validate_record("DispatchMessage", message)
        run = self._row(db, row["run_id"])
        event = db.execute("SELECT kind FROM events WHERE run_id=%s AND sequence=%s",
                           (row["run_id"], row["event_sequence"])).fetchone()
        if (digest(message) != row["checksum"] or message["message_id"] != row["message_id"]
                or message["message_id"] != f"{row['run_id']}:wake:{row['event_sequence']}"
                or message["run_id"] != row["run_id"] or message["event_sequence"] != row["event_sequence"]
                or message["binding_digest"] != run["binding_digest"] or event is None
                or message["event_kind"] != event["kind"]):
            raise ContractError("dispatch identity/binding/journal mismatch")
        return message

    def claim_dispatch(self):
        with self.connect() as db:
            # Run scope is host-owned. A dispatcher never claims another tenant
            # or repository, and SKIP LOCKED permits independent consumers.
            # Retire a bounded batch of expired final attempts without letting
            # one exhausted row hide other ready work from this poll.
            db.execute("""WITH expired AS (
                SELECT o.message_id FROM dispatch_outbox o JOIN runs r USING(run_id)
                WHERE r.tenant_id=%s AND r.resource_id=%s AND o.status='CLAIMED'
                AND o.attempts=5 AND o.claim_until <= clock_timestamp()
                LIMIT 32 FOR UPDATE OF o SKIP LOCKED)
                UPDATE dispatch_outbox SET status='EXHAUSTED',error_code='CLAIM_EXPIRED',
                claim_owner=NULL,claim_until=NULL WHERE message_id IN (SELECT message_id FROM expired)""",
                (self.tenant_id, self.resource_id))
            row = db.execute("""SELECT o.* FROM dispatch_outbox o JOIN runs r USING (run_id)
                WHERE r.tenant_id=%s AND r.resource_id=%s AND o.attempts < 5 AND
                ((o.status='PENDING' AND o.available_at <= clock_timestamp()) OR
                 (o.status='CLAIMED' AND o.claim_until <= clock_timestamp()))
                ORDER BY o.available_at, o.created_at, o.message_id
                LIMIT 1 FOR UPDATE OF o SKIP LOCKED""", (self.tenant_id, self.resource_id)).fetchone()
            if row is None:
                return None
            message = self._dispatch_message(db, row)
            epoch = row["claim_epoch"] + 1
            db.execute("""UPDATE dispatch_outbox SET status='CLAIMED', attempts=attempts+1,
                claim_owner=%s,claim_epoch=%s,claim_until=clock_timestamp()+(%s * interval '1 second')
                WHERE message_id=%s""", (self.worker_id, epoch, self.lease_seconds, row["message_id"]))
            return DispatchToken(row["message_id"], self.worker_id, epoch), message

    def _dispatch_claim(self, db, token):
        if (not isinstance(token, DispatchToken) or type(token.epoch) is not int or token.epoch < 1):
            raise LeaseError("dispatch claim required")
        row = db.execute("""SELECT o.*, o.claim_until > clock_timestamp() AS claim_live
            FROM dispatch_outbox o JOIN runs r USING(run_id)
            WHERE o.message_id=%s AND r.tenant_id=%s AND r.resource_id=%s FOR UPDATE OF o""",
            (token.message_id, self.tenant_id, self.resource_id)).fetchone()
        if row is None:
            raise ContractError("dispatch unavailable in caller scope")
        self._dispatch_message(db, row)
        if (row["status"] != "CLAIMED" or row["claim_owner"] != token.worker_id
                or row["claim_epoch"] != token.epoch or not row["claim_live"]):
            raise LeaseError("dispatch claim expired or replaced")
        return row

    def _finish_dispatch(self, db, row, token, status, delay=0, error=None):
        changed = db.execute("""UPDATE dispatch_outbox SET status=%s, claim_owner=NULL,claim_until=NULL,
            available_at=clock_timestamp()+(%s * interval '1 second'),error_code=%s,
            delivered_at=CASE WHEN %s='DELIVERED' THEN clock_timestamp() ELSE NULL END
            WHERE message_id=%s AND status='CLAIMED' AND claim_owner=%s AND claim_epoch=%s
            AND claim_until > clock_timestamp()""",
            (status, delay, error, status, row["message_id"], token.worker_id, token.epoch))
        if changed.rowcount != 1:
            raise LeaseError("dispatch claim expired at commit")

    def acknowledge_dispatch(self, token):
        with self.connect() as db:
            row = self._dispatch_claim(db, token)
            self._finish_dispatch(db, row, token, "DELIVERED")

    def retry_dispatch(self, token):
        with self.connect() as db:
            row = self._dispatch_claim(db, token)
            exhausted = row["attempts"] >= 5
            self._finish_dispatch(db, row, token, "EXHAUSTED" if exhausted else "PENDING",
                                  min(30, 2 ** (row["attempts"] - 1)), "TRANSPORT_FAILURE")

    def dispatch_messages(self, run_id):
        with self.connect() as db:
            self._decode(db, self._row(db, run_id, lock=True))
            rows = db.execute("SELECT * FROM dispatch_outbox WHERE run_id=%s ORDER BY event_sequence", (run_id,)).fetchall()
            return [{"message": self._dispatch_message(db, row), "status": row["status"],
                     "attempts": row["attempts"], "claim_epoch": row["claim_epoch"],
                     "error_code": row["error_code"]} for row in rows]


class LeasedRunStore:
    """Scoped facade keeps lease authority outside model inputs."""

    def __init__(self, parent, token):
        self.parent, self.token = parent, token

    def load(self, run_id):
        if run_id != self.token.run_id:
            raise LeaseError("lease cannot access another run")
        return self.parent.load(run_id)

    def save(self, record, expected_version, event):
        return self.parent.save(record, expected_version, event, token=self.token)

    def admit_model(self, record):
        return self.parent.admit_model(record, self.token)
