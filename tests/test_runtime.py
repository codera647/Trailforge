"""Neutral workflow authority/recovery tests, independent of PR business logic."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest

from trailforge.artifacts import LocalArtifactStore
from trailforge.core import ConflictError, ContractError
from trailforge.ports import AdapterCapabilities, FunctionAdapter, VerifiedResult
from trailforge.runtime import Goal, Harness, Task


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='trailforge-runtime-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / 'runs.sqlite3'
        self.now = 1000.0
        self.calls = 0
        self.adapters = [FunctionAdapter(AdapterCapabilities('sum/1', 'TOOL'), self.sum),
                         FunctionAdapter(AdapterCapabilities('verify/1', 'VERIFY'), self.verify)]
        self.goal = Goal('tenant', 'resource', 'Verify a data transformation',
                         (Task('sum', 'sum/1', {'values': [1, 2, 3]}),
                          Task('check', 'verify/1', {'expected': 6}, ('sum',))), ('check',))

    def sum(self, request):
        self.calls += 1
        return {'total': sum(request.input['values'])}

    def test_unknown_verdict_waits_without_journal_churn_then_explicit_retry(self):
        verdict = ['UNKNOWN']
        self.adapters[1] = FunctionAdapter(AdapterCapabilities('verify/1', 'VERIFY'),
                                          lambda request: VerifiedResult(verdict[0], request.goal_digest, {}))
        harness = self.harness()
        run_id = harness.start(self.goal)
        waiting = harness.advance(run_id, self.goal)
        self.assertEqual(waiting['state'], 'WAITING_HUMAN')
        self.assertEqual(harness.advance(run_id, self.goal), waiting)
        harness.resolve_unknown(run_id, self.goal, 'check', disposition='RETRY')
        verdict[0] = 'PASS'
        completed = harness.advance(run_id, self.goal)
        self.assertEqual(completed['state'], 'COMPLETED')
        self.assertEqual(completed['units_reserved'], 3)
        self.assertEqual(completed['tasks']['check']['attempts'], 2)

    def test_unknown_cancellation_cannot_be_reopened(self):
        self.adapters[0] = FunctionAdapter(AdapterCapabilities('sum/1', 'TOOL'),
                                          lambda request: (_ for _ in ()).throw(OSError('unknown')))
        harness = self.harness()
        run_id = harness.start(self.goal)
        harness.advance(run_id, self.goal)
        cancelled = harness.resolve_unknown(run_id, self.goal, 'sum', disposition='CANCEL')
        self.assertEqual(cancelled['state'], 'CANCELLED')
        with self.assertRaises(ContractError):
            harness.resolve_unknown(run_id, self.goal, 'sum', disposition='RETRY')

    def verify(self, request):
        verdict = 'PASS' if request.dependencies['sum']['total'] == request.input['expected'] else 'FAIL'
        return VerifiedResult(verdict, request.goal_digest, {'observed': request.dependencies['sum']})

    def harness(self, **kwargs):
        return Harness(self.path, tenant_id='tenant', resource_id='resource', adapters=kwargs.pop('adapters', self.adapters),
                       clock=lambda: self.now, **kwargs)

    def test_non_pr_resume_terminal_idempotence_and_runner_receipts(self):
        first = self.harness()
        run_id = first.start(self.goal)
        first.advance(run_id, self.goal, pause_after='sum')
        recovered = self.harness()
        record = recovered.advance(run_id, self.goal)
        self.assertEqual(record['state'], 'COMPLETED')
        self.assertEqual(record['units_reserved'], 2)
        self.assertEqual(record['tasks']['sum']['receipt']['issuer'], 'trailforge.execution_runner/0.1')
        self.assertEqual(recovered.advance(run_id, self.goal), record)
        self.assertEqual(self.calls, 1)

    def test_cross_scope_goal_change_and_adapter_change_fail_closed(self):
        harness = self.harness()
        run_id = harness.start(self.goal)
        harness.advance(run_id, self.goal, pause_after='sum')
        with self.assertRaises(ContractError):
            harness.advance(run_id, replace(self.goal, objective='new'), pause_after='sum')
        other = Harness(self.path, tenant_id='other', resource_id='resource', adapters=self.adapters)
        with self.assertRaises(ContractError):
            other.inspect(run_id)
        self.adapters[0].capabilities = replace(self.adapters[0].capabilities, reservation_units=9)
        with self.assertRaises(ContractError):
            harness.advance(run_id, self.goal)

    def test_unregistered_external_effect_cycles_and_model_verifier_denied(self):
        for goal in (replace(self.goal, allowed_effects=('EXTERNAL_WRITE',)),
                     replace(self.goal, required_checks=('sum',)),
                     replace(self.goal, tasks=(Task('sum', 'sum/1', {}, ('check',)), *self.goal.tasks[1:])),
                     replace(self.goal, tasks=(Task('sum', 'unregistered', {}), *self.goal.tasks[1:]))):
            with self.subTest(goal=goal), self.assertRaises(ContractError):
                self.harness().start(goal)

    def test_budget_denial_does_not_call_verifier_or_reset_units(self):
        goal = replace(self.goal, budget_units=1)
        harness = self.harness()
        run_id = harness.start(goal)
        record = harness.advance(run_id, goal)
        self.assertEqual(record['state'], 'WAITING_HUMAN')
        self.assertEqual(record['units_reserved'], 1)
        self.assertEqual(record['tasks']['check']['attempts'], 0)
        repeated = harness.advance(run_id, goal)
        self.assertEqual(repeated['units_reserved'], 1)
        self.assertEqual(self.calls, 1)

    def test_exception_and_forged_verdict_retain_unknown_until_host_disposition(self):
        def broken(request):
            raise RuntimeError('PRIVATE PROVIDER TOKEN')
        self.adapters[0] = FunctionAdapter(AdapterCapabilities('sum/1', 'MODEL'), broken)
        harness = self.harness()
        run_id = harness.start(self.goal)
        unknown = harness.advance(run_id, self.goal)
        self.assertEqual(unknown['state'], 'WAITING_HUMAN')
        self.assertEqual(unknown['units_reserved'], 1)
        self.assertNotIn('PRIVATE', str(unknown))
        self.assertEqual(harness.advance(run_id, self.goal), unknown)
        harness.resolve_unknown(run_id, self.goal, 'sum', disposition='RETRY')
        for attempt in range(2):
            harness.advance(run_id, self.goal)
            harness.resolve_unknown(run_id, self.goal, 'sum', disposition='RETRY')
        exhausted = harness.advance(run_id, self.goal)
        self.assertEqual(exhausted['units_reserved'], 3)
        self.assertEqual(exhausted['tasks']['sum']['attempts'], 3)
        self.assertEqual(exhausted['state'], 'WAITING_HUMAN')

    def test_verifier_json_cannot_mint_a_host_verdict_and_unknown_is_not_complete(self):
        self.adapters[1] = FunctionAdapter(AdapterCapabilities('verify/1', 'VERIFY'),
                                          lambda request: {'verdict': 'PASS', 'goal_digest': request.goal_digest, 'evidence': {}})
        harness = self.harness()
        run_id = harness.start(self.goal)
        self.assertEqual(harness.advance(run_id, self.goal)['state'], 'WAITING_HUMAN')
        self.adapters[1] = FunctionAdapter(AdapterCapabilities('verify/1', 'VERIFY'),
                                          lambda request: VerifiedResult('UNKNOWN', request.goal_digest, {}))
        harness = self.harness()
        run_id = harness.start(self.goal)
        self.assertEqual(harness.advance(run_id, self.goal)['state'], 'WAITING_HUMAN')

    def test_expired_worker_cannot_commit_or_reissue_without_resolution(self):
        def late(request):
            self.now += 31
            return {'total': 6}
        self.adapters[0] = FunctionAdapter(AdapterCapabilities('sum/1', 'TOOL'), late)
        harness = self.harness()
        run_id = harness.start(self.goal)
        with self.assertRaises(ConflictError):
            harness.advance(run_id, self.goal)
        record = self.harness().advance(run_id, self.goal)
        self.assertEqual(record['tasks']['sum']['status'], 'UNKNOWN')
        self.assertEqual(record['units_reserved'], 1)
        self.assertEqual(record['state'], 'WAITING_HUMAN')

    def test_live_concurrent_worker_cannot_reenter_the_run(self):
        run_id = None
        def nested(request):
            with self.assertRaises(ConflictError):
                self.harness().advance(run_id, self.goal)
            return {'total': 6}
        self.adapters[0] = FunctionAdapter(AdapterCapabilities('sum/1', 'TOOL'), nested)
        harness = self.harness()
        run_id = harness.start(self.goal)
        self.assertEqual(harness.advance(run_id, self.goal)['state'], 'COMPLETED')

    def test_admitted_input_is_detached_from_handler_mutation(self):
        def mutate(request):
            request.input['values'].append(100)
            return {'total': 6}
        self.adapters[0] = FunctionAdapter(AdapterCapabilities('sum/1', 'TOOL'), mutate)
        harness = self.harness()
        run_id = harness.start(self.goal)
        record = harness.advance(run_id, self.goal)
        self.assertEqual(record['goal']['tasks'][0]['input']['values'], [1, 2, 3])
        self.assertEqual(self.goal.tasks[0].input['values'], [1, 2, 3])

    def test_artifact_dedup_scope_bounds_and_corruption(self):
        store = LocalArtifactStore(self.root / 'artifacts', tenant_id='tenant', resource_id='resource', max_bytes=32)
        scope = ('tenant', 'resource')
        receipt = store.put(scope, b'original evidence', 'text/plain')
        self.assertEqual(store.put(scope, b'original evidence', 'text/plain'), receipt)
        self.assertEqual(store.get(scope, receipt), b'original evidence')
        with self.assertRaises(ContractError):
            store.get(('other', 'resource'), receipt)
        with self.assertRaises(ContractError):
            store.put(scope, b'x' * 33, 'text/plain')
        path = store.directory / receipt['content_digest'].removeprefix('sha256:')
        path.write_bytes(b'changed')
        with self.assertRaises(ContractError):
            store.get(scope, receipt)

    def test_shared_tenant_global_limits_do_not_reset_across_runs(self):
        harness = self.harness(tenant_limit=2, global_limit=3)
        first = harness.start(self.goal)
        self.assertEqual(harness.advance(first, self.goal)['state'], 'COMPLETED')
        second = harness.start(self.goal)
        denied = harness.advance(second, self.goal)
        self.assertEqual(denied['units_reserved'], 0)
        self.assertEqual(denied['tasks']['sum']['attempts'], 0)
        resources = harness.resource_status()
        self.assertEqual(resources['scopes']['global']['reserved'], 2)
        self.assertEqual(resources['scopes']['tenant']['reserved'], 2)
        with self.assertRaises(ContractError):
            self.harness(tenant_limit=3, global_limit=3)

    def test_last_shared_unit_is_atomic_under_concurrent_admission(self):
        harness = self.harness(tenant_limit=10, global_limit=1)
        first, second = harness.start(self.goal), harness.start(self.goal)
        def advance(run_id):
            return self.harness(tenant_limit=10, global_limit=1).advance(run_id, self.goal)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(advance, (first, second)))
        self.assertEqual(sum(record['units_reserved'] for record in results), 1)
        self.assertEqual(harness.resource_status()['scopes']['global']['reserved'], 1)
        self.assertEqual(self.calls, 1)

    def test_cancel_retains_admitted_units_and_fences_late_result(self):
        harness, run_id = None, None
        def cancelled(request):
            harness.cancel(run_id, self.goal)
            return {'total': 6}
        self.adapters[0] = FunctionAdapter(AdapterCapabilities('sum/1', 'TOOL'), cancelled)
        harness = self.harness()
        run_id = harness.start(self.goal)
        with self.assertRaises(ConflictError):
            harness.advance(run_id, self.goal)
        record = harness.inspect(run_id)
        self.assertEqual(record['state'], 'CANCELLED')
        self.assertEqual(record['units_reserved'], 1)
        self.assertEqual(record['tasks']['sum']['status'], 'UNKNOWN')
        self.assertEqual(harness.resource_status()['scopes']['global']['reserved'], 1)


if __name__ == '__main__':
    unittest.main()
