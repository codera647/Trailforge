"""Real database preflight and strict installed-only optional conformance."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import unittest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--receipt', type=Path, required=True)
    args = parser.parse_args()
    path = args.receipt
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        archive = path.parent / 'attempts'
        archive.mkdir(exist_ok=True)
        index = 1
        while (archive / f'{index}-report.json').exists():
            index += 1
        shutil.copy2(path, archive / f'{index}-report.json')
    report = {'status': 'IN_PROGRESS', 'stage': 'DATABASE_PREFLIGHT',
              'python': platform.python_version(), 'platform': platform.platform()}
    path.write_text(json.dumps(report), encoding='utf-8')
    try:
        import psycopg
        import trailforge
        from importlib.metadata import version
        assert Path(trailforge.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
        assert importlib.util.find_spec('diffwise') is None
        assert version('psycopg') == '3.3.6' and version('jsonschema') == '4.26.0'
        dsn = os.environ['TRAILFORGE_DATABASE_URL']
        with psycopg.connect(dsn, connect_timeout=5, options='-c statement_timeout=10000 -c lock_timeout=5000') as db:
            server = db.execute('SELECT version()').fetchone()[0]
        report.update(postgres_version=server, psycopg=version('psycopg'), jsonschema=version('jsonschema'), installed_only=True)
        report['stage'] = 'RUN_CONFORMANCE'
        path.write_text(json.dumps(report), encoding='utf-8')
        suite = unittest.defaultTestLoader.discover(str(Path(__file__).resolve().parent))
        count = suite.countTestCases()
        result = unittest.TextTestRunner(verbosity=2).run(suite)
        # Discovery imports support from the test directory, never package source.
        import support
        with psycopg.connect(dsn, connect_timeout=5, options='-c statement_timeout=10000 -c lock_timeout=5000') as db:
            remaining = [name for name in sorted(support.OWNED_SCHEMAS)
                         if db.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s', (name,)).fetchone()]
        passed = count > 0 and count == result.testsRun and result.wasSuccessful() and not result.skipped and not result.expectedFailures and not remaining
        report.update(status='PASS' if passed else 'FAIL', discovered=count, run=result.testsRun,
                      failures=len(result.failures), errors=len(result.errors), skipped=len(result.skipped),
                      expected_failures=len(result.expectedFailures), owned_schemas=len(support.OWNED_SCHEMAS),
                      owned_schema_cleanup='PASS' if not remaining else 'FAIL')
    except Exception as error:
        # Never serialize a DSN, SQL parameters or driver error text.
        report.update(status='FAIL', error_code=type(error).__name__)
    path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
