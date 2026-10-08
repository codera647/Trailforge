"""Actual durable response-loss recovery, exact approval and no resend tests."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import sqlite3
import tempfile
import unittest

from trailforge.core import ContractError, digest
from trailforge.publication import DurableSimulationPublisher, GuardedPublisher, Intent


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now, self.allowed = 1000, True
        self.intent = Intent('tenant', 'resource', digest('goal'), digest('policy'), 'revision-1', 'DRAFT', {'text': 'reviewed'})
        self.remote = DurableSimulationPublisher(self.root / 'remote.sqlite3')
        self.calls = 0
        self.execute = self.remote.execute
        def count(operation):
            self.calls += 1
            return self.execute(operation)
        self.remote.execute = count

    def host(self, remote=None, tenant='tenant'):
        return GuardedPublisher(self.root / 'authority.sqlite3', tenant_id=tenant, resource_id='resource',
                                adapter=remote or self.remote, live_guard=lambda *args: self.allowed,
                                clock=lambda: self.now)

    def test_lost_response_fresh_host_and_remote_reconcile_without_resend(self):
        host = self.host()
        approval = host.approve(self.intent, actor='operator')
        self.remote.lose_response = True
        uncertain = host.publish(self.intent, approval['approval_id'])
        self.assertEqual(uncertain['state'], 'UNKNOWN')
        self.assertEqual(self.calls, 1)
        self.assertEqual(host.publish(self.intent, approval['approval_id']), uncertain)
        fresh_remote = DurableSimulationPublisher(self.root / 'remote.sqlite3')
        fresh_remote.execute = lambda op: self.fail('reconciliation must never dispatch')
        recovered = self.host(fresh_remote).reconcile(uncertain['operation_id'], self.intent)
        self.assertEqual(recovered['state'], 'ACCEPTED')
        self.assertEqual(recovered['receipt']['binding_digest'], digest(recovered['binding']))
        self.assertEqual(self.host(fresh_remote).reconcile(recovered['operation_id'], self.intent), recovered)
        self.assertEqual(self.calls, 1)

    def test_payload_goal_revision_policy_scope_action_are_exact(self):
        host = self.host()
        approval = host.approve(self.intent, actor='operator')
        for changed in (replace(self.intent, payload={'text': 'different'}), replace(self.intent, goal_digest=digest('other')),
                        replace(self.intent, policy_digest=digest('other')), replace(self.intent, revision='new'),
                        replace(self.intent, tenant_id='other'), replace(self.intent, action='MERGE')):
            with self.subTest(changed=changed), self.assertRaises(ContractError):
                host.publish(changed, approval['approval_id'])
        self.assertEqual(self.calls, 0)

    def test_expiry_revocation_and_live_guard_deny_before_dispatch(self):
        host = self.host()
        approval = host.approve(self.intent, actor='operator', ttl_seconds=1)
        self.now += 1
        with self.assertRaises(ContractError):
            host.publish(self.intent, approval['approval_id'])
        revoked = host.approve(self.intent, actor='operator')
        host.revoke(revoked['approval_id'], self.intent)
        with self.assertRaises(ContractError):
            host.publish(self.intent, revoked['approval_id'])
        live = host.approve(self.intent, actor='operator')
        self.allowed = False
        with self.assertRaises(ContractError):
            host.publish(self.intent, live['approval_id'])
        with self.assertRaises(ContractError):
            host.approve(self.intent, actor='operator')
        self.assertEqual(self.calls, 0)

    def test_guard_revocation_between_admission_and_dispatch_never_calls_provider(self):
        host = self.host()
        approval = host.approve(self.intent, actor='operator')
        count = [0]
        def guard(*args):
            count[0] += 1
            return count[0] == 1
        host.guard = guard
        operation = host.publish(self.intent, approval['approval_id'])
        self.assertEqual(operation['state'], 'UNKNOWN')
        self.assertEqual(self.calls, 0)
        host.guard = lambda *args: True
        self.assertEqual(host.reconcile(operation['operation_id'], self.intent)['state'], 'UNKNOWN')
        self.assertEqual(host.publish(self.intent, approval['approval_id'])['state'], 'UNKNOWN')
        self.assertEqual(self.calls, 0)

    def test_concurrent_publish_and_new_approval_cannot_resend_same_intent(self):
        host = self.host()
        approval = host.approve(self.intent, actor='operator')
        with ThreadPoolExecutor(max_workers=2) as workers:
            operations = list(workers.map(lambda _: self.host().publish(self.intent, approval['approval_id']), range(2)))
        self.assertEqual(len({op['operation_id'] for op in operations}), 1)
        self.assertEqual(self.calls, 1)
        another = host.approve(self.intent, actor='another operator')
        self.assertEqual(host.publish(self.intent, another['approval_id'])['state'], 'ACCEPTED')
        self.assertEqual(self.calls, 1)

    def test_crash_after_admission_and_absent_remote_remain_unknown(self):
        host = self.host()
        approval = host.approve(self.intent, actor='operator')
        def crash(operation):
            raise SystemExit('process terminated after durable admission')
        self.remote.execute = crash
        with self.assertRaises(SystemExit):
            host.publish(self.intent, approval['approval_id'])
        replay = self.host().publish(self.intent, approval['approval_id'])
        self.assertEqual(replay['state'], 'SENDING')
        uncertain = self.host().reconcile(replay['operation_id'], self.intent)
        self.assertEqual(uncertain['state'], 'UNKNOWN')
        self.assertEqual(self.calls, 0)

    def test_forged_provider_receipt_and_corrupt_authority_fail_closed(self):
        host = self.host()
        approval = host.approve(self.intent, actor='operator')
        self.remote.execute = lambda op: {'status': 'ACCEPTED', 'operation_id': op['operation_id'],
                                         'binding_digest': digest('wrong'), 'remote_id': 'fake'}
        operation = host.publish(self.intent, approval['approval_id'])
        self.assertEqual(operation['state'], 'UNKNOWN')
        db = sqlite3.connect(host.path)
        try:
            with db:
                db.execute("UPDATE publication_operations SET checksum='corrupt'")
        finally:
            db.close()
        with self.assertRaises(ContractError):
            host.reconcile(operation['operation_id'], self.intent)

    def test_guard_receives_detached_data_and_operator_label_is_truthful(self):
        host = self.host()
        def guard(binding, actor, action):
            binding['intent']['payload']['text'] = 'attempted change'
            return True
        host.guard = guard
        approval = host.approve(self.intent, actor='local label')
        self.assertEqual(approval['binding']['intent']['payload']['text'], 'reviewed')
        self.assertEqual(approval['identity_origin'], 'LOCAL_OPERATOR_LABEL')
        with self.assertRaises(ContractError):
            host.approve(self.intent, actor='operator', ttl_seconds=True)


if __name__ == '__main__':
    unittest.main()
