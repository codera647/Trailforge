"""Bounded Develop rounds on the shared kernel; host callbacks remain trusted.

Protected checks are outside the candidate and hash-bound. This is a development
controller, not an OS sandbox; use DockerSandbox for untrusted check execution.
"""
from __future__ import annotations

from dataclasses import asdict
import os
from pathlib import Path
import stat
from uuid import uuid4

from .adapters.sandbox import tree_digest
from .artifacts import no_links
from .contracts import integer
from .core import ContractError, digest, source_path
from .ports import AdapterCapabilities, FunctionAdapter, VerifiedResult, bounded_json
from .runtime import Goal, Harness, Task

PHASES = ('PLAN', 'RESEARCH', 'BUILD', 'DEBUG', 'VERIFY', 'HEALTH')


class CandidateWorkspace:
    """Explicit file tool for trusted local hosts; no shell or dynamic imports."""
    def __init__(self, directory, *, allowed_paths, max_file_bytes=262144):
        if type(allowed_paths) is not tuple or not 1 <= len(allowed_paths) <= 128:
            raise ContractError('explicit bounded candidate paths required')
        self.paths = tuple(source_path(path) for path in allowed_paths)
        if len(set(path.casefold() for path in self.paths)) != len(self.paths):
            raise ContractError('duplicate or case-aliased candidate path')
        integer(max_file_bytes, 1, 1048576)
        self.limit = max_file_bytes
        self.root = no_links(directory)
        self.root.mkdir(parents=True, exist_ok=True)
        tree_digest(self.root)

    def _path(self, relative):
        relative = source_path(relative)
        if relative not in self.paths:
            raise ContractError('candidate path not granted')
        path = no_links(self.root / relative)
        if path.exists():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ContractError('candidate must be a single regular file')
        return path

    def read(self, relative):
        path = self._path(relative)
        with path.open('rb') as stream:
            content = stream.read(self.limit + 1)
        if len(content) > self.limit:
            raise ContractError('candidate file exceeds read bound')
        return content

    def write(self, relative, content):
        if type(content) is not bytes or len(content) > self.limit:
            raise ContractError('candidate file exceeds write bound')
        path = self._path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = no_links(path.parent / ('.trailforge-' + uuid4().hex))
        try:
            with temporary.open('xb') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            self._path(relative)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        tree_digest(self.root)


class Develop:
    """One to three explicit rounds, with persisted phase receipts and handoff.

    Fixed rounds are intentional: hosts choose the bounded plan before admission.
    Each phase is a trusted adapter. Model JSON cannot supply a VerifiedResult.
    """
    def __init__(self, store, *, tenant_id, resource_id, objective, workspace, checks,
                 adapters, rounds=1, budget_units=32, **harness_options):
        integer(rounds, 1, 3)
        if type(workspace) is not CandidateWorkspace or type(adapters) is not dict or set(adapters) != set(PHASES) - {'HEALTH'}:
            raise ContractError('workspace and five explicit phase adapters required')
        self.workspace, self.checks = workspace, no_links(checks)
        if self.workspace.root.is_relative_to(self.checks) or self.checks.is_relative_to(self.workspace.root):
            raise ContractError('protected checks must be outside candidate')
        self.checks_digest = tree_digest(self.checks)
        tasks, registered = [], []
        for phase, adapter in adapters.items():
            adapter.capabilities.validate()
            required_effect = 'CANDIDATE_WRITE' if phase == 'BUILD' else None
            if required_effect and adapter.capabilities.effect != required_effect:
                raise ContractError('BUILD requires candidate-write capability')
            if phase != 'BUILD' and adapter.capabilities.effect not in ('PURE', 'SCOPED_READ'):
                raise ContractError('non-BUILD phase cannot write candidates')
            if phase == 'VERIFY' and adapter.capabilities.kind != 'VERIFY':
                raise ContractError('VERIFY requires a trusted host verifier')
            if phase != 'VERIFY' and adapter.capabilities.kind == 'VERIFY':
                raise ContractError('only VERIFY can mint a phase verdict')
            wrapped = AdapterCapabilities('trailforge.develop/' + phase.lower() + '/' + adapter.capabilities.adapter_id,
                                          adapter.capabilities.kind, adapter.capabilities.effect,
                                          max_input_bytes=adapter.capabilities.max_input_bytes,
                                          max_output_bytes=adapter.capabilities.max_output_bytes,
                                          reservation_units=adapter.capabilities.reservation_units)
            registered.append(FunctionAdapter(wrapped, self._wrap(phase, adapter)))
        registered.append(FunctionAdapter(AdapterCapabilities('trailforge.develop/health/0.1', 'VERIFY', 'SCOPED_READ'), self._health))
        adapter_ids = {adapter.capabilities.adapter_id.split('/')[1].upper(): adapter.capabilities.adapter_id for adapter in registered}
        contract = {'profile': 'trailforge/develop/0.1', 'checks_digest': self.checks_digest,
                    'candidate_root_digest': digest(str(workspace.root)), 'allowed_paths': list(workspace.paths),
                    'file_bytes': workspace.limit, 'rounds': rounds}
        for iteration in range(1, rounds + 1):
            for phase in PHASES:
                task_id = str(iteration) + ':' + phase
                dependencies = tuple(task.task_id for task in tasks)
                tasks.append(Task(task_id, adapter_ids[phase], {**contract, 'round': iteration}, dependencies))
        last = str(rounds)
        self.goal = Goal(tenant_id, resource_id, objective, tuple(tasks), (last + ':VERIFY', last + ':HEALTH'),
                         ('PURE', 'SCOPED_READ', 'CANDIDATE_WRITE'), budget_units=budget_units)
        self.harness = Harness(store, tenant_id=tenant_id, resource_id=resource_id, adapters=registered, **harness_options)

    def _bound(self, request):
        if request.input['checks_digest'] != self.checks_digest or tree_digest(self.checks) != self.checks_digest:
            raise ContractError('protected checks changed')
        tree_digest(self.workspace.root)

    def _wrap(self, phase, adapter):
        capabilities = asdict(adapter.capabilities)
        def execute(request):
            self._bound(request)
            if asdict(adapter.capabilities) != capabilities:
                raise ContractError('underlying phase capabilities changed')
            before = tree_digest(self.workspace.root)
            result = adapter.execute(request)
            self._bound(request)
            after = tree_digest(self.workspace.root)
            if phase != 'BUILD' and before != after:
                raise ContractError('phase changed candidate without write capability')
            binding = {'candidate_digest': after, 'checks_digest': self.checks_digest,
                       'profile': 'trailforge/develop/0.1', 'round': request.input['round']}
            if phase == 'VERIFY':
                if type(result) is not VerifiedResult or result.goal_digest != request.goal_digest:
                    raise ContractError('model data cannot establish verification')
                return VerifiedResult(result.verdict, result.goal_digest, {'host_evidence': result.evidence, 'workspace': binding})
            return {'phase_result': bounded_json(result, adapter.capabilities.max_output_bytes), 'workspace': binding}
        return execute

    def _health(self, request):
        self._bound(request)
        verification = request.dependencies[str(request.input['round']) + ':VERIFY']
        current = tree_digest(self.workspace.root)
        verdict = 'PASS' if verification['evidence']['workspace']['candidate_digest'] == current else 'UNKNOWN'
        return VerifiedResult(verdict, request.goal_digest,
                              {'candidate_digest': current, 'checks_digest': self.checks_digest,
                               'phase': 'HEALTH', 'criterion_authority': 'HOST_VERIFIER'})

    def start(self):
        return self.harness.start(self.goal)

    def advance(self, run_id, *, pause_after=None):
        record = self.harness.inspect(run_id)
        # Even terminal resume must check the exact candidate before issuing a handoff.
        if record['state'] == 'COMPLETED':
            self.handoff(run_id)
        return self.harness.advance(run_id, self.goal, pause_after=pause_after)

    def continuity(self, run_id):
        record = self.harness.inspect(run_id)
        if record['goal'] != self.harness._goal(self.goal):
            raise ContractError('Develop goal binding changed')
        return {'run_id': run_id, 'goal_digest': record['binding']['goal_digest'], 'state': record['state'],
                'checks_digest': self.checks_digest, 'units_reserved': record['units_reserved'],
                'phases': [{'task_id': task.task_id, 'status': record['tasks'][task.task_id]['status'],
                            'attempts': record['tasks'][task.task_id]['attempts'],
                            'receipt_id': record['tasks'][task.task_id]['receipt']['receipt_id']
                            if record['tasks'][task.task_id]['receipt'] else None}
                           for task in self.goal.tasks],
                'context_authority': 'HOST_CHECKPOINT', 'external_effect': 'NONE'}

    def handoff(self, run_id):
        record = self.harness.inspect(run_id)
        if record['goal'] != self.harness._goal(self.goal) or record['state'] != 'COMPLETED':
            raise ContractError('verified Develop completion required for handoff')
        health = record['tasks'][self.goal.required_checks[-1]]['result']['evidence']
        if tree_digest(self.checks) != self.checks_digest or tree_digest(self.workspace.root) != health['candidate_digest']:
            raise ContractError('candidate/checks changed after verification')
        body = {**self.continuity(run_id), 'candidate_digest': health['candidate_digest'],
                'issuer': 'trailforge.develop_handoff/0.1', 'execution_mode': 'TRUSTED_HOST_CALLBACKS',
                'linux_isolation_verified': False}
        return {**body, 'handoff_id': digest(body)}
