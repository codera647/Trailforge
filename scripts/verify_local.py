"""Verify a standalone wheel and adoption outside the source checkout."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tarfile
import time
import tomllib
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def candidate_wheel(output):
    return output / ("trailforge-" + VERSION + "-py3-none-any.whl")


def manifest():
    result = {}
    def excluded(name):
        return name in ('.local', '.venv', '__pycache__', 'build', 'dist', '.git') or name.endswith('.egg-info')
    def fail(error):
        raise error
    # Prune generated environments before stat/read, including on Windows.
    for directory, dirs, files in os.walk(ROOT, onerror=fail):
        dirs[:] = sorted(name for name in dirs if not excluded(name))
        for name in sorted(files):
            if excluded(name):
                continue
            path = Path(directory) / name
            result[path.relative_to(ROOT).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return dict(sorted(result.items()))


def run(args, *, cwd, env, log_path=None):
    # Each owned subprocess is bounded; reap its entire Windows launcher tree.
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
    process = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding='utf-8', creationflags=flags,
                               start_new_session=os.name != 'nt')
    try:
        output, error = process.communicate(timeout=240)
    except subprocess.TimeoutExpired:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True, check=False)
        else:
            import signal
            os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        raise RuntimeError('owned verification command timed out') from None
    if log_path is not None:
        Path(log_path).write_text(output + error, encoding='utf-8')
    if process.returncode:
        raise RuntimeError('standalone command failed: ' + output[-2000:] + error[-2000:])
    return output, error


def main():
    output = ROOT / '.local/verification'
    output.mkdir(parents=True, exist_ok=True)
    before = manifest()
    started = time.perf_counter()
    scope = 'TF_PORTABLE_KERNEL_DEVELOP_HOST_PORTS_PUBLICATION_SIMULATION'
    report = {'status': 'IN_PROGRESS', 'scope': scope, 'postgres_run': False}
    report_path = output / 'report.json'
    if report_path.exists():
        archive = output / ('history-' + str(time.time_ns()))
        archive.mkdir()
        for previous in (report_path, output / 'standalone-tests.log'):
            if previous.exists():
                shutil.copy2(previous, archive / previous.name)
    report_path.write_text(json.dumps(report) + '\n', encoding='utf-8')
    current_stage = 'PREPARE'
    def stage(name):
        nonlocal current_stage
        current_stage = name
        report['stage'] = name
        report_path.write_text(json.dumps(report) + '\n', encoding='utf-8')
        print(json.dumps({'stage': name}), flush=True)
    env = {key: value for key, value in os.environ.items() if key not in ('PYTHONPATH', 'TRAILFORGE_DATABASE_URL')}
    env['PYTHONUTF8'] = '1'
    try:
        with tempfile.TemporaryDirectory(prefix='adoption-', dir=output) as temporary:
            staging = Path(temporary)
            package = staging / 'package'
            shutil.copytree(ROOT, package, ignore=shutil.ignore_patterns('.local', '.venv', '__pycache__', 'build', 'dist', '*.egg-info', '.git'))
            stage('BUILD_DISTRIBUTIONS')
            run([sys.executable, '-m', 'build', '--wheel', '--sdist', '--no-isolation', '--outdir', str(staging / 'dist'), str(package)], cwd=staging, env=env)
            distributions = list((staging / 'dist').glob('*.tar.gz'))
            assert len(distributions) == 1
            with tarfile.open(distributions[0]) as archive:
                members = archive.getnames()
                assert not any('/.local/' in name or name.endswith(('.pdf', '.sqlite3', '.pyc')) for name in members)
                for required in ('examples/develop_data.py', 'docs/publication.md', 'tests_optional/test_langgraph.py', 'tests_postgres/test_postgres.py', 'requirements-postgres.lock', '.gitattributes', 'LICENSE', 'THIRD_PARTY_NOTICES.md'):
                    assert any(name.endswith('/' + required) for name in members)
            wheels = list((staging / 'dist').glob('*.whl'))
            assert len(wheels) == 1
            with zipfile.ZipFile(wheels[0]) as archive:
                members = archive.namelist()
                assert not any('diffwise' in name or name.endswith(('.pdf', '.sqlite3', '.pyc')) for name in members)
                assert 'trailforge/data/examples/suite.json' in members
                assert 'trailforge/data/examples/unsupported/main.rs' in members
                assert any(name.endswith('/licenses/LICENSE') for name in members)
            venv = staging / 'installed'
            stage('CREATE_ENVIRONMENT')
            run([sys.executable, '-m', 'venv', str(venv)], cwd=staging, env=env)
            py = venv / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            cli = venv / ('Scripts/trailforge.exe' if os.name == 'nt' else 'bin/trailforge')
            stage('INSTALL_BASE')
            run([str(py), '-m', 'pip', 'install', '--no-index', '--no-deps', str(wheels[0])], cwd=staging, env=env)
            run([str(py), '-I', '-c',
                 f"import importlib.util,trailforge; assert all(importlib.util.find_spec(n) is None for n in ('diffwise','psycopg','fastapi','cryptography','langgraph')); assert trailforge.__version__ == '{VERSION}'; print(trailforge.__version__)"], cwd=staging, env=env)
            stage('RUN_CONFORMANCE')
            tests_output, tests_error = run([str(py), '-I', str(package / 'tests/run_suite.py'), '--suite', str(package / 'tests'), '--receipt', str(output / 'standalone-suite.json')], cwd=staging, env=env, log_path=output / 'standalone-tests.log')
            (output / 'standalone-tests.log').write_text(tests_output + tests_error, encoding='utf-8')
            suite = json.loads((output / 'standalone-suite.json').read_text(encoding='utf-8'))
            assert suite['status'] == 'PASS'
            stage('CLI_ADOPTION')
            run([str(cli), 'init', 'example'], cwd=staging, env=env)
            smoke, _ = run([str(cli), 'evaluate', '--suite', 'example'], cwd=staging, env=env)
            smoke = json.loads(smoke)
            assert smoke['status'] == 'PASS' and len(smoke['cases']) == 10
            paused, _ = run([str(cli), 'run', '--fixture', 'example/buggy-python', '--store', 'runs.sqlite3', '--pause-after', 'snapshot'], cwd=staging, env=env)
            run_id = json.loads(paused)['run_id']
            command = [str(cli), 'resume', '--fixture', 'example/buggy-python', '--store', 'runs.sqlite3', '--run-id', run_id]
            resumed, _ = run(command, cwd=staging, env=env)
            repeated, _ = run(command, cwd=staging, env=env)
            assert json.loads(resumed) == json.loads(repeated)
            draft = json.loads(resumed)
            assert draft['workflow_outcome'] == 'COMPLETE' and len(draft['findings']) == 1
            inspected, _ = run([str(cli), 'inspect', '--store', 'runs.sqlite3', '--run-id', run_id], cwd=staging, env=env)
            assert json.loads(inspected)['model_calls'] == 1
            run([str(py), '-I', '-m', 'trailforge', '--version'], cwd=staging, env=env)
            run([str(cli), 'export', '--store', 'runs.sqlite3', '--run-id', run_id, '--output', 'review.json'], cwd=staging, env=env)
            assert json.loads((staging / 'review.json').read_text())['run_id'] == run_id
            stage('RECOVERY_EXAMPLES')
            for example in ('data_pipeline.py', 'develop_data.py'):
                result, _ = run([str(py), '-I', str(package / 'examples' / example)], cwd=staging, env=env)
                assert json.loads(result)['state'] == 'COMPLETED'
            stage('PUBLICATION_RECOVERY')
            uncertain, _ = run([str(cli), 'approve', '--store', 'runs.sqlite3', '--run-id', run_id,
                                '--actor', 'local-operator', '--simulate', '--lose-response'], cwd=staging, env=env)
            uncertain = json.loads(uncertain)
            assert uncertain['state'] == 'UNKNOWN' and uncertain['external_effect'] == 'NONE'
            reconciled, _ = run([str(cli), 'reconcile', '--store', 'runs.sqlite3', '--run-id', run_id,
                                 '--operation-id', uncertain['operation_id'], '--simulate'], cwd=staging, env=env)
            assert json.loads(reconciled)['state'] == 'ACCEPTED'
            stage('FINAL_INSPECTION')
            run([str(py), '-m', 'pip', 'check'], cwd=staging, env=env)
            wheel_hash = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
            shutil.copy2(wheels[0], output / wheels[0].name)
            shutil.copy2(distributions[0], output / distributions[0].name)
        after = manifest()
        assert before == after, 'candidate changed during verification'
        report = {'status': 'PASS', 'scope': scope,
                  'installed_only': True, 'optional_dependencies_absent': True,
                  'suite': suite,
                  'separate_process_resume': True, 'terminal_resume_idempotent': True,
                  'cli_eight_commands': True, 'non_pr_example': True, 'develop_trusted_host_example': True,
                  'separate_process_publication_reconciliation': True, 'synthetic_cases': 10,
                  'model_quality_claim': False, 'postgres_run': False, 'linux_isolation_run': False,
                  'wheel_sha256': wheel_hash, 'sdist_sha256': hashlib.sha256((output / distributions[0].name).read_bytes()).hexdigest(),
                  'candidate_files_sha256': before,
                  'elapsed_seconds': round(time.perf_counter() - started, 3),
                  'limits': ['local platform only', 'portable subset, not full harness release', 'LangGraph optional gate separate',
                             'same-workspace criteria', 'Postgres installed-extra gate separate']}
    except Exception as error:
        report = {'status': 'FAIL', 'scope': scope,
                  'elapsed_seconds': round(time.perf_counter() - started, 3), 'stage': current_stage, 'error': str(error)}
    report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'candidate_files_sha256'}, indent=2))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
