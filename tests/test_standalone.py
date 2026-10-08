"""Adopter-facing regression checks; no Diffwise package or source fixtures."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest

from trailforge.adapters.mock import FixtureModel
from trailforge.cli import evaluate_suite, inspect_run, main
from trailforge.controller import OfflineController
from trailforge.core import ContractError
from trailforge.examples import initialize
from trailforge.fixtures import load_fixture
from trailforge.store import SQLiteRunStore


class StandaloneTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='trailforge-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.examples = self.root / 'examples'
        initialize(self.examples)
        self.store = SQLiteRunStore(self.root / 'runs.sqlite3')

    def cli(self, *args):
        output, error = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(error):
            code = main(list(args))
        return code, json.loads(output.getvalue()) if output.getvalue() else None, error.getvalue()

    def test_installed_examples_cover_supported_and_partial_cases(self):
        report = evaluate_suite(self.examples)
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(len(report['cases']), 10)
        self.assertFalse(report['model_quality_claim'])
        self.assertEqual(sum(case['outcome'] == 'PARTIAL' for case in report['cases']), 3)
        self.assertFalse(list(self.examples.rglob('*.pyc')))

    def test_resume_recovers_and_finished_run_spends_no_extra_call(self):
        fixture = load_fixture(self.examples / 'buggy-python')
        controller = OfflineController(self.store, FixtureModel())
        run_id = controller.start(fixture)
        controller.advance(run_id, fixture, 'snapshot')
        recovered = OfflineController(SQLiteRunStore(self.store.path), FixtureModel())
        record = recovered.advance(run_id, fixture)
        self.assertEqual(record['outcome'], 'COMPLETE')
        self.assertEqual(record['model_calls'], 1)
        self.assertEqual(recovered.advance(run_id, fixture), record)

    def test_changed_input_and_cross_resource_cannot_resume(self):
        fixture = load_fixture(self.examples / 'buggy-python')
        controller = OfflineController(self.store, FixtureModel())
        run_id = controller.start(fixture)
        controller.advance(run_id, fixture, 'snapshot')
        other = replace(fixture, binding=replace(fixture.binding, resource_id='other'))
        with self.assertRaises(ContractError):
            controller.advance(run_id, other)
        (self.examples / 'buggy-python/stats.py').write_text('changed = True\n', encoding='utf-8')
        with self.assertRaises(ContractError):
            controller.advance(run_id, load_fixture(self.examples / 'buggy-python'))

    def test_init_never_overwrites_an_existing_directory(self):
        sentinel = self.examples / 'keep.txt'
        sentinel.write_text('private', encoding='utf-8')
        code, result, error = self.cli('init', str(self.examples))
        self.assertEqual(code, 2)
        self.assertIsNone(result)
        self.assertNotIn('private', error)
        self.assertEqual(sentinel.read_text(), 'private')

    def test_cli_pause_resume_inspect_and_exclusive_export(self):
        fixture = str(self.examples / 'buggy-python')
        path = str(self.store.path)
        code, paused, _ = self.cli('run', '--fixture', fixture, '--store', path, '--pause-after', 'snapshot')
        self.assertEqual(code, 0)
        self.assertTrue(paused['paused'])
        run_id = paused['run_id']
        code, draft, _ = self.cli('resume', '--fixture', fixture, '--store', path, '--run-id', run_id)
        self.assertEqual(code, 0)
        self.assertEqual(draft['workflow_outcome'], 'COMPLETE')
        visible = inspect_run(self.store, run_id)
        self.assertNotIn('snapshot', visible)
        self.assertNotIn('review', visible)
        self.assertIn('snapshot', inspect_run(self.store, run_id, include_source=True))
        output = self.root / 'export.json'
        code, result, _ = self.cli('export', '--store', path, '--run-id', run_id, '--output', str(output))
        self.assertEqual(code, 0)
        first_bytes = output.read_bytes()
        code, result, _ = self.cli('export', '--store', path, '--run-id', run_id, '--output', str(output))
        self.assertEqual(code, 2)
        self.assertEqual(output.read_bytes(), first_bytes)

    def test_suite_rejects_escape_duplicate_case_and_boolean_count(self):
        suite_path = self.examples / 'suite.json'
        original = json.loads(suite_path.read_text())
        for variant in ('path', 'duplicate', 'boolean', 'version'):
            suite = json.loads(json.dumps(original))
            if variant == 'path':
                suite['cases'][0]['fixture'] = '../private'
            elif variant == 'duplicate':
                suite['cases'][1]['id'] = suite['cases'][0]['id']
            elif variant == 'boolean':
                suite['cases'][0]['expected_findings'] = True
            else:
                suite['schema_version'] = 'future'
            suite_path.write_text(json.dumps(suite), encoding='utf-8')
            with self.subTest(variant=variant), self.assertRaises(ContractError):
                evaluate_suite(self.examples)

    def test_wrong_expectation_is_failed_report_not_a_quality_claim(self):
        suite_path = self.examples / 'suite.json'
        suite = json.loads(suite_path.read_text())
        suite['cases'][0]['expected_findings'] = 0
        suite_path.write_text(json.dumps(suite), encoding='utf-8')
        code, result, _ = self.cli('evaluate', '--suite', str(self.examples))
        self.assertEqual(code, 1)
        self.assertEqual(result['status'], 'FAIL')
        self.assertFalse(result['model_quality_claim'])

    def test_inspect_detects_corrupt_checkpoint_and_missing_store_creates_nothing(self):
        fixture = load_fixture(self.examples / 'buggy-python')
        controller = OfflineController(self.store, FixtureModel())
        run_id = controller.start(fixture)
        controller.advance(run_id, fixture)
        with self.store.connect() as db:
            db.execute('UPDATE runs SET checksum=? WHERE run_id=?', ('invalid', run_id))
        with self.assertRaises(ContractError):
            inspect_run(self.store, run_id)
        missing = self.root / 'missing.sqlite3'
        code, _, error = self.cli('inspect', '--store', str(missing), '--run-id', run_id)
        self.assertEqual(code, 2)
        self.assertFalse(missing.exists())
        self.assertNotIn(str(missing), error)


if __name__ == '__main__':
    unittest.main()
