"""Exact host approvals and conservative single-host publication recovery.

The host owns identity, policy and current revision checks. This module does not
authenticate a person or guarantee exactly-once delivery from arbitrary providers.
Persisted SENDING/UNKNOWN operations can only be inspected, never blindly resent.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import sqlite3
import time
from uuid import uuid4

from .contracts import hash_value, integer
from .core import ContractError, canonical, digest, fields, nonempty, strict_json
from .ports import bounded_json

PROTOCOL = 'trailforge/publication/0.1'


@dataclass(frozen=True)
class Intent:
    tenant_id: str
    resource_id: str
    goal_digest: str
    policy_digest: str
    revision: str
    action: str
    payload: object


class GuardedPublisher:
    """Trusted host composition; SQLite files and callbacks are host authority."""
    def __init__(self, path, *, tenant_id, resource_id, adapter, live_guard, clock=time.time):
        self.scope = (nonempty(tenant_id), nonempty(resource_id))
        self.adapter, self.guard, self.clock = adapter, live_guard, clock
        self.adapter_id = nonempty(adapter.adapter_id)
        self.mode = nonempty(adapter.mode)
        if not callable(live_guard) or self.mode not in ('LOCAL_SIMULATION', 'HOST_EXTERNAL'):
            raise ContractError('explicit publisher mode and live host guard required')
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS publication_approvals (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, checksum TEXT NOT NULL,
                    revoked INTEGER NOT NULL DEFAULT 0, consumed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS publication_operations (
                    id TEXT PRIMARY KEY, intent_digest TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL, checksum TEXT NOT NULL);
            ''')

    def _db(self):
        # Explicit closing is necessary: sqlite's own context manager only commits.
        from contextlib import contextmanager
        @contextmanager
        def connection():
            db = sqlite3.connect(self.path, timeout=5)
            try:
                with db:
                    yield db
            finally:
                db.close()
        return connection()

    def _intent(self, intent):
        if type(intent) is not Intent or (intent.tenant_id, intent.resource_id) != self.scope:
            raise ContractError('publication scope denied')
        hash_value(intent.goal_digest)
        hash_value(intent.policy_digest)
        nonempty(intent.revision)
        nonempty(intent.action)
        if self.adapter.adapter_id != self.adapter_id or self.adapter.mode != self.mode:
            raise ContractError('publisher capabilities changed')
        content = bounded_json(asdict(intent), 900000)
        return {'schema_version': PROTOCOL, 'intent': content, 'payload_digest': digest(content['payload']),
                'adapter_id': self.adapter_id, 'mode': self.mode}

    def _guard(self, binding, actor, action):
        # Copy prevents a guard changing the exact binding it is authorizing.
        try:
            allowed = self.guard(bounded_json(binding, 1048576), actor, action)
        except Exception:
            raise ContractError('live publisher authority denied') from None
        if allowed is not True:
            raise ContractError('live publisher authority denied')

    @staticmethod
    def _decode(row):
        if row is None:
            raise ContractError('unknown publication authority record')
        value = strict_json(row[0])
        if digest(value) != row[1]:
            raise ContractError('publication authority checksum mismatch')
        return value

    def approve(self, intent, *, actor, ttl_seconds=300):
        binding = self._intent(intent)
        actor = nonempty(actor)
        if len(actor) > 256:
            raise ContractError('actor label exceeds bound')
        integer(ttl_seconds, 1, 3600)
        self._guard(binding, actor, 'APPROVE')
        approval = {'approval_id': str(uuid4()), 'binding': binding, 'actor': actor,
                    'issued_at': self.clock(), 'expires_at': self.clock() + ttl_seconds,
                    'identity_origin': 'HOST_AUTHORITY' if self.mode == 'HOST_EXTERNAL' else 'LOCAL_OPERATOR_LABEL'}
        with self._db() as db:
            db.execute('INSERT INTO publication_approvals VALUES (?,?,?,0,0)',
                       (approval['approval_id'], canonical(approval), digest(approval)))
        return approval

    def revoke(self, approval_id, intent):
        binding = self._intent(intent)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            approval = self._decode(db.execute('SELECT payload,checksum FROM publication_approvals WHERE id=?', (approval_id,)).fetchone())
            if approval['binding'] != binding:
                raise ContractError('revocation binding mismatch')
            self._guard(binding, approval['actor'], 'REVOKE')
            db.execute('UPDATE publication_approvals SET revoked=1 WHERE id=?', (approval_id,))

    def inspect_operation(self, operation_id, intent):
        binding = self._intent(intent)
        with self._db() as db:
            record = self._decode(db.execute('SELECT payload,checksum FROM publication_operations WHERE id=?', (operation_id,)).fetchone())
        if record['binding'] != binding:
            raise ContractError('operation binding mismatch')
        return record

    @staticmethod
    def _save(db, record):
        db.execute('UPDATE publication_operations SET payload=?,checksum=? WHERE id=?',
                   (canonical(record), digest(record), record['operation_id']))

    def _validate_receipt(self, raw, operation):
        raw = bounded_json(raw, 65536)
        fields(raw, {'status', 'operation_id', 'binding_digest', 'remote_id'})
        if (raw['status'] != 'ACCEPTED' or raw['operation_id'] != operation['operation_id']
                or raw['binding_digest'] != digest(operation['binding'])):
            raise ContractError('publisher receipt binding mismatch')
        nonempty(raw['remote_id'])
        return raw

    def publish(self, intent, approval_id):
        binding = self._intent(intent)
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload,checksum,revoked,consumed FROM publication_approvals WHERE id=?', (approval_id,)).fetchone()
            approval = self._decode(row[:2] if row else None)
            if approval['binding'] != binding:
                raise ContractError('exact approval binding mismatch')
            self._guard(binding, approval['actor'], 'EXECUTE')
            existing = db.execute('SELECT payload,checksum FROM publication_operations WHERE intent_digest=?', (digest(binding),)).fetchone()
            # Terminal and uncertain replay never calls execute again, even with a new approval.
            if existing is not None:
                return self._decode(existing)
            if row[2] or row[3] or approval['expires_at'] <= self.clock():
                raise ContractError('expired, revoked or consumed approval')
            operation = {'operation_id': str(uuid4()), 'binding': binding, 'actor': approval['actor'],
                         'approval_id': approval_id, 'state': 'SENDING', 'receipt': None, 'error_code': None}
            db.execute('UPDATE publication_approvals SET consumed=1 WHERE id=? AND consumed=0', (approval_id,))
            db.execute('INSERT INTO publication_operations VALUES (?,?,?,?)',
                       (operation['operation_id'], digest(binding), canonical(operation), digest(operation)))
        # Once admission is durable, an interrupted process is uncertain, never PENDING.
        try:
            self._guard(binding, approval['actor'], 'EXECUTE')
            # Fresh revocation/expiry check immediately before dispatch. Hosts must also
            # enforce live authority inside their provider adapter at the write boundary.
            with self._db() as db:
                revoked = db.execute('SELECT revoked FROM publication_approvals WHERE id=?', (approval_id,)).fetchone()[0]
            if revoked or approval['expires_at'] <= self.clock():
                raise ContractError('approval no longer live')
            receipt = self._validate_receipt(self.adapter.execute(bounded_json(operation, 1048576)), operation)
        except Exception:
            return self._record_outcome(operation, None, 'DISPATCH_OUTCOME_UNKNOWN')
        return self._record_outcome(operation, receipt, None)

    def _record_outcome(self, operation, receipt, error):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            current = self._decode(db.execute('SELECT payload,checksum FROM publication_operations WHERE id=?', (operation['operation_id'],)).fetchone())
            if current['state'] == 'ACCEPTED':
                return current
            current.update(state='ACCEPTED' if receipt is not None else 'UNKNOWN', receipt=receipt, error_code=error)
            self._save(db, current)
            return current

    def reconcile(self, operation_id, intent):
        operation = self.inspect_operation(operation_id, intent)
        self._guard(operation['binding'], operation['actor'], 'RECONCILE')
        if operation['state'] == 'ACCEPTED':
            return operation
        try:
            raw = self.adapter.inspect(bounded_json(operation, 1048576))
            receipt = self._validate_receipt(raw, operation)
        except Exception:
            return self._record_outcome(operation, None, 'RECONCILIATION_UNKNOWN')
        return self._record_outcome(operation, receipt, None)


class DurableSimulationPublisher:
    """Independent mock remote database; no provider credentials or external writes."""
    adapter_id = 'trailforge.durable_simulation/0.1'
    mode = 'LOCAL_SIMULATION'

    def __init__(self, path, *, lose_response=False):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if type(lose_response) is not bool:
            raise ContractError('explicit simulation fault required')
        self.lose_response = lose_response
        db = sqlite3.connect(self.path)
        try:
            with db:
                db.execute('CREATE TABLE IF NOT EXISTS accepted (id TEXT PRIMARY KEY, binding_digest TEXT NOT NULL, receipt TEXT NOT NULL)')
        finally:
            db.close()

    def execute(self, operation):
        receipt = {'status': 'ACCEPTED', 'operation_id': operation['operation_id'],
                   'binding_digest': digest(operation['binding']), 'remote_id': 'simulation:' + str(uuid4())}
        db = sqlite3.connect(self.path)
        try:
            with db:
                db.execute('INSERT OR IGNORE INTO accepted VALUES (?,?,?)',
                           (operation['operation_id'], receipt['binding_digest'], canonical(receipt)))
                row = db.execute('SELECT binding_digest,receipt FROM accepted WHERE id=?', (operation['operation_id'],)).fetchone()
                if row[0] != receipt['binding_digest']:
                    raise ContractError('simulation operation collision')
                receipt = strict_json(row[1])
        finally:
            db.close()
        if self.lose_response:
            raise OSError('simulated response lost after durable acceptance')
        return receipt

    def inspect(self, operation):
        db = sqlite3.connect(self.path)
        try:
            row = db.execute('SELECT receipt FROM accepted WHERE id=?', (operation['operation_id'],)).fetchone()
            return strict_json(row[0]) if row else {'status': 'UNKNOWN'}
        finally:
            db.close()
