"""Independent-process recovery, bounded burst admission and store compatibility."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from trailforge.adapters.mock import FixtureModel
from trailforge.controller import OfflineController
from trailforge.core import ContractError
from trailforge.examples import initialize
from trailforge.fixtures import load_fixture
from trailforge.ports import AdapterCapabilities, FunctionAdapter, VerifiedResult
from trailforge.runtime import Goal, Harness, Task
from trailforge.store import SQLiteRunStore

WORKER = Path(__file__).with_name('workers') / 'neutral_worker.py'


class ReleaseConformanceTests(unittest.TestCase):
    def worker(self, phase, directory, expected=0):
        result = subprocess.run([sys.executable, '-I', str(WORKER), phase, str(directory)],
                                cwd=directory, capture_output=True, encoding='utf-8', timeout=30)
        self.assertEqual(result.returncode, expected, result.stderr)
        return json.loads(result.stdout) if expected == 0 else None

    def test_neutral_separate_process_resume_preserves_exact_terminal_state(self):
        with tempfile.TemporaryDirectory() as directory:
            first = self.worker('start', directory)
            self.assertEqual(first['units_reserved'], 1)
            resumed = self.worker('resume', directory)
            self.assertEqual(resumed['state'], 'COMPLETED')
            self.assertEqual(resumed['units_reserved'], 2)
            self.assertEqual(self.worker('resume', directory), resumed)
            self.assertEqual((Path(directory) / 'calls.txt').read_text(encoding='utf-8').splitlines(), ['called'])

    def test_actual_worker_death_retains_unknown_until_explicit_host_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            self.worker('crash', directory, expected=17)
            uncertain = self.worker('resume', directory)
            self.assertEqual(uncertain['state'], 'WAITING_HUMAN')
            self.assertEqual(uncertain['tasks']['transform']['status'], 'UNKNOWN')
            self.assertEqual(uncertain['units_reserved'], 1)
            self.assertEqual(self.worker('resume', directory), uncertain)
            completed = self.worker('retry', directory)
            self.assertEqual(completed['state'], 'COMPLETED')
            self.assertEqual(completed['units_reserved'], 3)
            self.assertEqual(completed['tasks']['transform']['attempts'], 2)
            self.assertEqual(len((Path(directory) / 'calls.txt').read_text(encoding='utf-8').splitlines()), 2)

    def test_burst_of_sixteen_runs_conserves_shared_admission_under_four_workers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'authority.sqlite3'
            calls, lock = [0], threading.Lock()
            def transform(request):
                with lock:
                    calls[0] += 1
                return {'total': 6}
            def verify(request):
                with lock:
                    calls[0] += 1
                return VerifiedResult('PASS', request.goal_digest, {'total': request.dependencies['transform']['total']})
            adapters = [FunctionAdapter(AdapterCapabilities('tool/1', 'TOOL'), transform),
                        FunctionAdapter(AdapterCapabilities('check/1', 'VERIFY'), verify)]
            jobs = []
            for index in range(16):
                tenant = 'tenant-' + str(index % 2)
                harness = Harness(path, tenant_id=tenant, resource_id='data', adapters=adapters, tenant_limit=8, global_limit=12)
                goal = Goal(tenant, 'data', 'Bounded burst', (Task('transform', 'tool/1', {}),
                            Task('check', 'check/1', {}, ('transform',))), ('check',), budget_units=2)
                jobs.append((harness, goal, harness.start(goal)))
            def advance(job):
                harness, goal, run_id = job
                return harness.advance(run_id, goal)
            with ThreadPoolExecutor(max_workers=4) as workers:
                records = list(workers.map(advance, jobs))
            self.assertEqual(calls[0], 12)
            self.assertEqual(sum(record['units_reserved'] for record in records), 12)
            self.assertEqual(jobs[0][0].resource_status()['scopes']['global']['reserved'], 12)
            for tenant_index in range(2):
                self.assertLessEqual(sum(record['units_reserved'] for record in records
                                         if record['binding']['tenant_id'] == 'tenant-' + str(tenant_index)), 8)
            self.assertTrue(all(record['state'] in ('COMPLETED', 'WAITING_HUMAN') for record in records))
            for job, record in zip(jobs, records):
                if record['state'] == 'COMPLETED':
                    self.assertEqual(advance(job), record)
            self.assertEqual(calls[0], 12)

    def test_original_review_and_neutral_records_coexist_without_protocol_coercion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            initialize(root / 'fixtures')
            review_input = load_fixture(root / 'fixtures/buggy-python')
            legacy = SQLiteRunStore(root / 'runs.sqlite3')
            controller = OfflineController(legacy, FixtureModel())
            legacy_id = controller.start(review_input)
            original = controller.advance(legacy_id, review_input)
            adapter = FunctionAdapter(AdapterCapabilities('check/1', 'VERIFY'),
                                      lambda request: VerifiedResult('PASS', request.goal_digest, {}))
            neutral = Harness(root / 'runs.sqlite3', tenant_id='tenant', resource_id='data', adapters=[adapter])
            goal = Goal('tenant', 'data', 'Neutral protocol', (Task('check', 'check/1', {}),), ('check',))
            neutral_id = neutral.start(goal)
            self.assertEqual(neutral.advance(neutral_id, goal)['state'], 'COMPLETED')
            self.assertEqual(controller.advance(legacy_id, review_input), original)
            with self.assertRaises(ContractError):
                legacy.load(neutral_id)
            with self.assertRaises(ContractError):
                neutral.inspect(legacy_id)
            with self.assertRaises(ContractError):
                SQLiteRunStore(root / 'runs.sqlite3', protocol='trailforge/execution/999')
