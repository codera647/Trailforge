from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest


class PublicationCLITests(unittest.TestCase):
    def test_separate_process_approval_response_loss_and_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory:
            def cli(*args, code=0):
                result = subprocess.run([sys.executable, '-m', 'trailforge', *args], cwd=directory,
                                        capture_output=True, text=True, encoding='utf-8', timeout=30)
                self.assertEqual(result.returncode, code, result.stderr)
                return json.loads(result.stdout) if code == 0 else None
            cli('init', 'examples')
            run = cli('run', '--fixture', 'examples/buggy-python')
            # Draft public output contains run_id; recovery resolves its exact binding.
            run_id = run['run_id']
            uncertain = cli('approve', '--run-id', run_id, '--actor', 'operator', '--simulate', '--lose-response')
            self.assertEqual(uncertain['state'], 'UNKNOWN')
            self.assertEqual(uncertain['external_effect'], 'NONE')
            recovered = cli('reconcile', '--run-id', run_id, '--operation-id', uncertain['operation_id'], '--simulate')
            self.assertEqual(recovered['state'], 'ACCEPTED')
            self.assertEqual(recovered['identity_origin'], 'LOCAL_OPERATOR_LABEL')
            self.assertEqual(cli('approve', '--run-id', run_id, '--actor', 'second', '--simulate')['operation_id'], uncertain['operation_id'])
            cli('approve', '--run-id', run_id, '--actor', 'operator', code=2)
            partial = cli('run', '--fixture', 'examples/unsupported')
            cli('approve', '--run-id', partial['run_id'], '--actor', 'operator', '--simulate', code=2)
            self.assertTrue(Path(directory, 'trailforge.sqlite3.simulation.sqlite3').is_file())
