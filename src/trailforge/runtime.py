"""Application-neutral, trusted single-host execution with durable bounded attempts.

Callbacks are trusted host code, not sandboxed model code. Leases fence commits;
they do not preempt Python or guarantee provider monetary usage. External writes
are denied until a dedicated guarded publisher is configured in a later module.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import time
from uuid import uuid4

from .contracts import BudgetError, integer
from .core import ConflictError, ContractError, digest, nonempty
from .ports import TaskRequest, VerifiedResult, bounded_json
from .store import SQLiteRunStore

EXECUTION_PROTOCOL = 'trailforge/execution/0.1'


@dataclass(frozen=True)
class Task:
    task_id: str
    adapter_id: str
    input: object
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class Goal:
    tenant_id: str
    resource_id: str
    objective: str
    tasks: tuple[Task, ...]
    required_checks: tuple[str, ...]
    allowed_effects: tuple[str, ...] = ('PURE', 'SCOPED_READ')
    budget_units: int = 32
    max_task_attempts: int = 3


class Harness:
    def __init__(self, path, *, tenant_id, resource_id, adapters, lease_seconds=30, clock=time.time,
                 tenant_limit=1000, global_limit=10000):
        self.scope = (nonempty(tenant_id), nonempty(resource_id))
        self.store = SQLiteRunStore(path, protocol=EXECUTION_PROTOCOL)
        if type(lease_seconds) not in (int, float) or not 0 < lease_seconds <= 300:
            raise ContractError('lease duration outside local bounds')
        self.lease_seconds, self.clock = lease_seconds, clock
        self.adapters = {}
        for adapter in adapters:
            adapter.capabilities.validate()
            identifier = adapter.capabilities.adapter_id
            if identifier in self.adapters:
                raise ContractError('duplicate adapter ID')
            self.adapters[identifier] = adapter
        self.adapter_digest = digest({name: asdict(adapter.capabilities) for name, adapter in self.adapters.items()})
        integer(tenant_limit, 1, 1000000000)
        integer(global_limit, 1, 1000000000)
        self.ledger_scopes = (('tenant:' + digest(self.scope[0]), tenant_limit), ('global', global_limit))
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS execution_limits (scope_key TEXT PRIMARY KEY, cap INTEGER NOT NULL, reserved INTEGER NOT NULL CHECK (reserved >= 0 AND reserved <= cap))')
            for key, cap in self.ledger_scopes:
                db.execute('INSERT OR IGNORE INTO execution_limits VALUES (?, ?, 0)', (key, cap))
                if db.execute('SELECT cap FROM execution_limits WHERE scope_key=?', (key,)).fetchone()[0] != cap:
                    raise ContractError('existing authority limit differs; no implicit cap reset')

    def _goal(self, goal):
        if digest({name: asdict(adapter.capabilities) for name, adapter in self.adapters.items()}) != self.adapter_digest:
            raise ContractError('registered adapter capabilities changed')
        if type(goal) is not Goal or (goal.tenant_id, goal.resource_id) != self.scope:
            raise ContractError('host goal scope mismatch')
        integer(goal.budget_units, 1, 1000000)
        integer(goal.max_task_attempts, 1, 3)
        nonempty(goal.objective)
        if (type(goal.tasks) is not tuple or type(goal.required_checks) is not tuple
                or type(goal.allowed_effects) is not tuple
                or not 1 <= len(goal.tasks) <= 128 or not goal.required_checks):
            raise ContractError('bounded tasks and explicit checks required')
        if (len(set(goal.allowed_effects)) != len(goal.allowed_effects)
                or not set(goal.allowed_effects) <= {'PURE', 'SCOPED_READ', 'CANDIDATE_WRITE'}):
            raise ContractError('effect denied; external writes need a guarded publisher')
        seen = set()
        for task in goal.tasks:
            if type(task) is not Task:
                raise ContractError('typed tasks required')
            nonempty(task.task_id)
            if (type(task.dependencies) is not tuple or task.task_id in seen
                    or not set(task.dependencies) <= seen or len(set(task.dependencies)) != len(task.dependencies)):
                raise ContractError('tasks must be unique and topologically ordered')
            seen.add(task.task_id)
            adapter = self.adapters.get(task.adapter_id)
            if adapter is None or adapter.capabilities.effect not in goal.allowed_effects:
                raise ContractError('unregistered adapter or effect denied')
            if adapter.capabilities.kind == 'VERIFY' and adapter.capabilities.effect not in ('PURE', 'SCOPED_READ'):
                raise ContractError('verifier cannot write candidates')
            bounded_json(task.input, adapter.capabilities.max_input_bytes)
        checks = set(goal.required_checks)
        if len(checks) != len(goal.required_checks) or not checks <= seen:
            raise ContractError('invalid required checks')
        for task in goal.tasks:
            if task.task_id in checks and self.adapters[task.adapter_id].capabilities.kind != 'VERIFY':
                raise ContractError('required checks need a host verifier adapter')
        return bounded_json(asdict(goal), 1048576)

    def start(self, goal):
        contract = self._goal(goal)
        goal_digest = digest(contract)
        record = {'schema_version': EXECUTION_PROTOCOL, 'run_id': str(uuid4()), 'state_version': 0,
                  'binding': {'tenant_id': self.scope[0], 'resource_id': self.scope[1],
                              'goal_digest': goal_digest, 'adapter_digest': self.adapter_digest},
                  'goal': contract, 'state': 'READY', 'units_reserved': 0,
                  'tasks': {task.task_id: {'status': 'PENDING', 'attempts': 0, 'result': None,
                                          'receipt': None, 'error_code': None} for task in goal.tasks},
                  'active': None, 'outcome': None}
        self.store.create(record)
        return record['run_id']

    def inspect(self, run_id):
        record = self.store.load(run_id)
        if (record['binding'].get('tenant_id'), record['binding'].get('resource_id')) != self.scope:
            raise ContractError('run scope denied')
        if record['binding'].get('goal_digest') != digest(record['goal']):
            raise ContractError('goal binding changed')
        return record

    def _save(self, record, event):
        return self.store.save(record, record['state_version'], event)

    def _reserve_and_save(self, record, units):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for key, _ in self.ledger_scopes:
                changed = db.execute('UPDATE execution_limits SET reserved=reserved+? WHERE scope_key=? AND reserved+? <= cap',
                                     (units, key, units))
                if changed.rowcount != 1:
                    raise BudgetError('shared authority allowance exhausted')
            return self.store.save_in_transaction(db, record, record['state_version'], 'TASK_ADMITTED')

    def resource_status(self):
        with self.store.connect() as db:
            result = {}
            for key, _ in self.ledger_scopes:
                cap, reserved = db.execute('SELECT cap,reserved FROM execution_limits WHERE scope_key=?', (key,)).fetchone()
                result['tenant' if key.startswith('tenant:') else 'global'] = {'cap': cap, 'reserved': reserved}
            return {'unit': 'DECLARED_ADAPTER_UNITS', 'scopes': result, 'monetary_guarantee': False}

    def advance(self, run_id, goal, *, pause_after=None):
        contract = self._goal(goal)
        record = self.inspect(run_id)
        if record['goal'] != contract or record['binding']['adapter_digest'] != self.adapter_digest:
            raise ContractError('resume goal or adapter binding changed')
        if record['state'] in ('COMPLETED', 'FAILED', 'CANCELLED'):
            return record
        if record['active'] is not None:
            if record['active']['expires_at'] > self.clock():
                raise ConflictError('a live local worker owns the run')
            task = record['tasks'][record['active']['task_id']]
            task.update(status='UNKNOWN', error_code='EXPIRED_ATTEMPT')
            record.update(state='WAITING_HUMAN', active=None)
            return self._save(record, 'EXPIRED_ATTEMPT_RETAINED')
        # UNKNOWN is not silently released/reissued, even for a nominally pure adapter.
        if any(task['status'] == 'UNKNOWN' for task in record['tasks'].values()):
            return record
        if record['state'] == 'WAITING_HUMAN' and any(
                record['tasks'][name]['status'] == 'DONE' and record['tasks'][name]['result']['verdict'] == 'UNKNOWN'
                for name in goal.required_checks):
            return record
        for index, specification in enumerate(goal.tasks):
            if self._goal(goal) != contract:
                raise ContractError('goal changed during execution')
            state = record['tasks'][specification.task_id]
            if state['status'] == 'DONE':
                continue
            if state['attempts'] >= goal.max_task_attempts:
                record.update(state='WAITING_HUMAN', outcome=None)
                return self._save(record, 'ATTEMPTS_EXHAUSTED')
            if any(record['tasks'][name]['status'] != 'DONE' for name in specification.dependencies):
                raise ContractError('dependency result missing')
            adapter = self.adapters[specification.adapter_id]
            capabilities = adapter.capabilities
            request = TaskRequest(run_id, specification.task_id, self.scope, record['binding']['goal_digest'],
                                  bounded_json(contract['tasks'][index]['input'], capabilities.max_input_bytes),
                                  bounded_json({name: record['tasks'][name]['result'] for name in specification.dependencies}, capabilities.max_input_bytes))
            units = capabilities.reservation_units
            if record['units_reserved'] + units > goal.budget_units:
                record.update(state='WAITING_HUMAN', outcome=None)
                return self._save(record, 'BUDGET_DENIED')
            owner = str(uuid4())
            before_admission = bounded_json(record, 16777216)
            state.update(status='RUNNING', attempts=state['attempts'] + 1, error_code=None)
            record.update(state='RUNNING', units_reserved=record['units_reserved'] + units,
                          active={'task_id': specification.task_id, 'owner': owner,
                                  'expires_at': self.clock() + self.lease_seconds})
            try:
                record = self._reserve_and_save(record, units)
            except BudgetError:
                record = before_admission
                record.update(state='WAITING_HUMAN', outcome=None)
                return self._save(record, 'SHARED_BUDGET_DENIED')
            try:
                raw = adapter.execute(request)
                if capabilities.kind == 'VERIFY':
                    if (type(raw) is not VerifiedResult or raw.verdict not in ('PASS', 'FAIL', 'UNKNOWN')
                            or raw.goal_digest != request.goal_digest):
                        raise ContractError('invalid host verifier result')
                    result = bounded_json(asdict(raw), capabilities.max_output_bytes)
                else:
                    result = bounded_json(raw, capabilities.max_output_bytes)
                if self._goal(goal) != contract:
                    raise ContractError('goal changed during execution')
            except Exception:
                current = self.inspect(run_id)
                if current['active'] is None or current['active']['owner'] != owner:
                    raise ConflictError('worker ownership replaced') from None
                current['tasks'][specification.task_id].update(status='UNKNOWN', error_code='ADAPTER_OUTCOME_UNKNOWN')
                current.update(active=None, state='WAITING_HUMAN')
                return self._save(current, 'ADAPTER_OUTCOME_UNKNOWN')
            current = self.inspect(run_id)
            if (current['active'] is None or current['active']['owner'] != owner
                    or current['active']['expires_at'] <= self.clock()):
                raise ConflictError('expired or replaced worker cannot commit')
            receipt = {'issuer': 'trailforge.execution_runner/0.1', 'run_id': run_id,
                       'task_id': specification.task_id, 'goal_digest': request.goal_digest,
                       'adapter_id': specification.adapter_id, 'output_digest': digest(result),
                       'reservation_units': units, 'attempt': current['tasks'][specification.task_id]['attempts']}
            current['tasks'][specification.task_id].update(status='DONE', result=result,
                                                          receipt={**receipt, 'receipt_id': digest(receipt)})
            current.update(active=None, state='CHECKING')
            record = self._save(current, 'TASK_RESULT_RECORDED')
            if pause_after == specification.task_id:
                return record
        verdicts = [record['tasks'][name]['result']['verdict'] for name in goal.required_checks]
        if 'FAIL' in verdicts:
            record.update(state='FAILED', outcome='FAILED')
        elif 'UNKNOWN' in verdicts:
            record.update(state='WAITING_HUMAN', outcome=None)
        else:
            record.update(state='COMPLETED', outcome='COMPLETE')
        return self._save(record, 'GOAL_CHECKS_EVALUATED')

    def resolve_unknown(self, run_id, goal, task_id, *, disposition):
        """Trusted host decision, not a model action. Reservations/attempts never reset."""
        contract = self._goal(goal)
        record = self.inspect(run_id)
        if record['goal'] != contract or record['binding']['adapter_digest'] != self.adapter_digest:
            raise ContractError('goal/adapter binding changed')
        if record['state'] in ('COMPLETED', 'FAILED', 'CANCELLED'):
            raise ContractError('terminal run cannot be reopened')
        task = record['tasks'].get(task_id)
        uncertain_verdict = (task is not None and task['status'] == 'DONE' and task['result'] is not None
                             and task['result'].get('verdict') == 'UNKNOWN'
                             and self.adapters[next(item.adapter_id for item in goal.tasks if item.task_id == task_id)].capabilities.kind == 'VERIFY')
        if record['active'] is not None or task is None or (task['status'] != 'UNKNOWN' and not uncertain_verdict):
            raise ContractError('no resolvable unknown attempt')
        if disposition not in ('RETRY', 'CANCEL'):
            raise ContractError('explicit retry or cancel disposition required')
        if disposition == 'CANCEL':
            record.update(state='CANCELLED', outcome='CANCELLED')
        else:
            record['tasks'][task_id].update(status='PENDING', result=None, receipt=None)
            record.update(state='READY', outcome=None)
        return self._save(record, 'HOST_UNKNOWN_' + disposition)

    def cancel(self, run_id, goal):
        contract = self._goal(goal)
        record = self.inspect(run_id)
        if record['goal'] != contract or record['binding']['adapter_digest'] != self.adapter_digest:
            raise ContractError('goal/adapter binding changed')
        if record['state'] in ('COMPLETED', 'FAILED', 'CANCELLED'):
            return record
        if record['active'] is not None:
            record['tasks'][record['active']['task_id']].update(status='UNKNOWN', error_code='CANCELLED_AFTER_ADMISSION')
        record.update(state='CANCELLED', outcome='CANCELLED', active=None)
        # Already reserved units remain. Cancel cannot undo a running callback.
        return self._save(record, 'HOST_CANCELLED')
