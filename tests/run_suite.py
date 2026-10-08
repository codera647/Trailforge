"""Trusted installed-wheel test runner: empty or skipped suites never pass."""
import argparse
import json
from pathlib import Path
import platform
import sys
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    args.receipt.parent.mkdir(parents=True, exist_ok=True)
    report = {'status': 'IN_PROGRESS', 'python': platform.python_version(), 'platform': platform.platform()}
    args.receipt.write_text(json.dumps(report), encoding='utf-8')
    suite = unittest.defaultTestLoader.discover(str(args.suite))
    count = suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    passed = count > 0 and result.testsRun == count and result.wasSuccessful() and not result.skipped and not result.expectedFailures
    report.update(status='PASS' if passed else 'FAIL', discovered=count, run=result.testsRun,
                  failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
                  expected_failures=len(result.expectedFailures))
    args.receipt.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report), flush=True)
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
