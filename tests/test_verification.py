"""Acceptance cannot succeed with empty, skipped or expected-failing tests."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile
import unittest


class VerificationTests(unittest.TestCase):
    def test_empty_suite_is_rejected(self):
        self.check_denied('', 0, 0)

    def test_skipped_suite_is_rejected(self):
        self.check_denied('import unittest\nclass T(unittest.TestCase):\n @unittest.skip("unsupported")\n def test_case(self): pass\n', 1, 1)

    def test_expected_failure_is_rejected(self):
        self.check_denied('import unittest\nclass T(unittest.TestCase):\n @unittest.expectedFailure\n def test_case(self): self.fail("expected")\n', 1, 0)

    def check_denied(self, source, count, skipped):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tests = root / 'tests'
            tests.mkdir()
            if source:
                (tests / 'test_case.py').write_text(source, encoding='utf-8')
            report = root / 'receipt.json'
            runner = Path(__file__).resolve().parent / 'run_suite.py'
            process = subprocess.run([sys.executable, '-I', str(runner), '--suite', str(tests), '--receipt', str(report)],
                                     cwd=root, capture_output=True, text=True, timeout=20)
            self.assertEqual(process.returncode, 1)
            actual = json.loads(report.read_text(encoding='utf-8'))
            self.assertEqual((actual['status'], actual['run'], actual['skipped']), ('FAIL', count, skipped))
