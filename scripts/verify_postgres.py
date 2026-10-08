"""Verify the exact passed wheel's optional PostgreSQL authority in isolation."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from verify_local import ROOT, manifest, run, candidate_wheel


def main():
    output = ROOT / '.local/verification'
    output.mkdir(parents=True, exist_ok=True)
    path = output / 'postgres-report.json'
    if path.exists():
        shutil.copy2(path, output / ('postgres-history-' + str(time.time_ns()) + '.json'))
    started = time.perf_counter()
    report = {'status': 'IN_PROGRESS', 'scope': 'INSTALLED_POSTGRES_REVIEW_AUTHORITY', 'stage': 'VALIDATE_BASE_CANDIDATE'}
    path.write_text(json.dumps(report), encoding='utf-8')
    def stage(name):
        report['stage'] = name
        path.write_text(json.dumps(report), encoding='utf-8')
        print(json.dumps({'stage': name}), flush=True)
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'TRAILFORGE_DATABASE_URL')}
    env['PYTHONUTF8'] = '1'
    try:
        dsn = os.environ['TRAILFORGE_DATABASE_URL']
        assert dsn, 'explicit disposable database required'
        base = json.loads((output / 'report.json').read_text(encoding='utf-8'))
        before = manifest()
        assert base['status'] == 'PASS' and base['candidate_files_sha256'] == before
        wheel = candidate_wheel(output)
        assert hashlib.sha256(wheel.read_bytes()).hexdigest() == base['wheel_sha256']
        with tempfile.TemporaryDirectory(prefix='tf-postgres-') as folder:
            temporary = Path(folder)
            tests = temporary / 'tests_postgres'
            shutil.copytree(ROOT / 'tests_postgres', tests, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            stage('CREATE_ENVIRONMENT')
            run([sys.executable, '-m', 'venv', str(temporary / 'venv')], cwd=temporary, env=env)
            py = temporary / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            stage('INSTALL_PINNED_POSTGRES_EXTRA')
            run([str(py), '-m', 'pip', 'install', '--disable-pip-version-check', '--timeout', '15', '--retries', '1',
                 '-r', str(ROOT / 'requirements-postgres.lock'), str(wheel) + '[postgres]'], cwd=temporary, env=env)
            stage('RUN_REAL_DATABASE_CONFORMANCE')
            test_env = {**env, 'TRAILFORGE_DATABASE_URL': dsn}
            receipt = output / 'postgres-suite.json'
            try:
                logs, errors = run([str(py), '-I', str(tests / 'run_suite.py'), '--receipt', str(receipt)], cwd=temporary, env=test_env)
            except RuntimeError:
                # Driver tracebacks can include SQL/connection details. Preserve the
                # structured suite result and expose no raw failure output.
                raise RuntimeError('installed PostgreSQL conformance failed; inspect structured suite receipt') from None
            (output / 'postgres-tests.log').write_text(logs + errors, encoding='utf-8')
            suite = json.loads(receipt.read_text(encoding='utf-8'))
            assert suite['status'] == 'PASS' and suite['installed_only'] and suite['owned_schema_cleanup'] == 'PASS'
            run([str(py), '-m', 'pip', 'check'], cwd=temporary, env=env)
            versions, _ = run([str(py), '-m', 'pip', 'freeze', '--all'], cwd=temporary, env=env)
        assert before == manifest(), 'candidate changed during verification'
        report.update(status='PASS', installed_only=True, wheel_sha256=base['wheel_sha256'],
                      candidate_files_sha256=before, suite=suite,
                      dependency_versions=[line for line in versions.splitlines() if '==' in line],
                      neutral_distributed_postgres=False)
    except Exception as error:
        report.update(status='FAIL', error_code=type(error).__name__)
    report['elapsed_seconds'] = round(time.perf_counter() - started, 3)
    path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k != 'candidate_files_sha256'}, indent=2), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
