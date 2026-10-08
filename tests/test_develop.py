from pathlib import Path
import tempfile
import unittest

from trailforge.core import ContractError
from trailforge.develop import CandidateWorkspace, Develop
from trailforge.ports import AdapterCapabilities, FunctionAdapter, VerifiedResult


class DevelopTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = CandidateWorkspace(self.root / 'candidate', allowed_paths=('answer.txt',))
        self.checks = self.root / 'checks'
        self.checks.mkdir()
        (self.checks / 'expected.txt').write_text('42', encoding='utf-8')
        self.builds = 0

    def build(self, request):
        self.builds += 1
        self.workspace.write('answer.txt', b'42')
        return {'written': 'answer.txt'}

    def verify(self, request):
        expected = (self.checks / 'expected.txt').read_bytes()
        verdict = 'PASS' if self.workspace.read('answer.txt') == expected else 'FAIL'
        return VerifiedResult(verdict, request.goal_digest, {'criterion': 'exact original data transformation'})

    def adapters(self):
        adapters = {phase: FunctionAdapter(AdapterCapabilities('example/' + phase.lower(), 'TOOL'),
                                           lambda request: {'phase': request.task_id}) for phase in ('PLAN', 'RESEARCH', 'DEBUG')}
        adapters['BUILD'] = FunctionAdapter(AdapterCapabilities('example/build', 'TOOL', 'CANDIDATE_WRITE'), self.build)
        adapters['VERIFY'] = FunctionAdapter(AdapterCapabilities('example/verify', 'VERIFY', 'SCOPED_READ'), self.verify)
        return adapters

    def profile(self, **options):
        return Develop(self.root / 'runs.sqlite3', tenant_id='tenant', resource_id='candidate',
                       objective='Create the exact answer data', workspace=self.workspace, checks=self.checks,
                       adapters=options.pop('adapters', self.adapters()), **options)

    def test_phase_resume_and_exact_terminal_handoff_without_repeat_build(self):
        profile = self.profile()
        run_id = profile.start()
        profile.advance(run_id, pause_after='1:BUILD')
        self.assertEqual(profile.continuity(run_id)['phases'][2]['status'], 'DONE')
        recovered = self.profile()
        completed = recovered.advance(run_id)
        self.assertEqual(completed['state'], 'COMPLETED')
        self.assertEqual(self.builds, 1)
        self.assertEqual(len(recovered.handoff(run_id)['phases']), 6)
        self.assertFalse(recovered.handoff(run_id)['linux_isolation_verified'])
        self.assertEqual(recovered.advance(run_id), completed)
        self.workspace.write('answer.txt', b'43')
        with self.assertRaises(ContractError):
            recovered.handoff(run_id)
        with self.assertRaises(ContractError):
            recovered.advance(run_id)

    def test_forged_verdict_never_completes(self):
        adapters = self.adapters()
        adapters['VERIFY'] = FunctionAdapter(AdapterCapabilities('example/verify', 'VERIFY'), lambda request: {'verdict': 'PASS'})
        profile = self.profile(adapters=adapters)
        record = profile.advance(profile.start())
        self.assertEqual(record['state'], 'WAITING_HUMAN')
        self.assertEqual(record['tasks']['1:VERIFY']['status'], 'UNKNOWN')
        with self.assertRaises(ContractError):
            profile.handoff(record['run_id'])

    def test_protected_check_mutation_and_ungranted_write_are_denied(self):
        adapters = self.adapters()
        def mutate(request):
            (self.checks / 'expected.txt').write_text('different', encoding='utf-8')
            return {}
        adapters['BUILD'] = FunctionAdapter(AdapterCapabilities('example/build', 'TOOL', 'CANDIDATE_WRITE'), mutate)
        profile = self.profile(adapters=adapters)
        record = profile.advance(profile.start())
        self.assertEqual(record['tasks']['1:BUILD']['status'], 'UNKNOWN')
        self.assertEqual(record['state'], 'WAITING_HUMAN')
        with self.assertRaises(ContractError):
            self.workspace.write('../checks/expected.txt', b'bad')
        with self.assertRaises(ContractError):
            self.workspace.write('not-granted.txt', b'bad')

    def test_nonbuild_callback_candidate_mutation_is_not_committed(self):
        adapters = self.adapters()
        adapters['PLAN'] = FunctionAdapter(AdapterCapabilities('example/plan', 'TOOL'), self.build)
        profile = self.profile(adapters=adapters)
        record = profile.advance(profile.start())
        self.assertEqual(record['tasks']['1:PLAN']['status'], 'UNKNOWN')
        self.assertEqual(self.builds, 1)

    def test_bounded_two_rounds_keep_failed_evidence_and_charge_both(self):
        adapters = self.adapters()
        def build(request):
            self.workspace.write('answer.txt', b'wrong' if request.input['round'] == 1 else b'42')
            return {}
        adapters['BUILD'] = FunctionAdapter(AdapterCapabilities('example/build', 'TOOL', 'CANDIDATE_WRITE'), build)
        profile = self.profile(rounds=2, adapters=adapters)
        record = profile.advance(profile.start())
        self.assertEqual(record['state'], 'COMPLETED')
        self.assertEqual(record['tasks']['1:VERIFY']['result']['verdict'], 'FAIL')
        self.assertEqual(record['tasks']['2:VERIFY']['result']['verdict'], 'PASS')
        self.assertEqual(record['units_reserved'], 12)
        with self.assertRaises(ContractError):
            self.profile(rounds=4)

    def test_resume_binds_protected_goal_checks_and_capabilities(self):
        profile = self.profile()
        run_id = profile.start()
        profile.advance(run_id, pause_after='1:PLAN')
        (self.checks / 'expected.txt').write_text('new', encoding='utf-8')
        with self.assertRaises(ContractError):
            self.profile().advance(run_id)
        with self.assertRaises(ContractError):
            CandidateWorkspace(self.root / 'candidate2', allowed_paths=('A.txt', 'a.txt'))


if __name__ == '__main__':
    unittest.main()
