"""Exercise the pinned optional workflow on the exact passed base wheel."""
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
    started = time.perf_counter()
    report = {'status': 'IN_PROGRESS', 'scope': 'LANGGRAPH_OPTIONAL'}
    path = output / 'optional-report.json'
    if path.exists():
        shutil.copy2(path, output / ('optional-history-' + str(time.time_ns()) + '.json'))
    path.write_text(json.dumps(report), encoding='utf-8')
    def stage(name):
        report['stage'] = name
        path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    env = {key: value for key, value in os.environ.items()
           if key not in ('PYTHONPATH', 'TRAILFORGE_DATABASE_URL') and not key.startswith(('LANGSMITH_', 'LANGCHAIN_'))}
    env['PYTHONUTF8'] = '1'
    env['LANGSMITH_TRACING'] = 'false'
    env['LANGCHAIN_TRACING_V2'] = 'false'
    try:
        base = json.loads((output / 'report.json').read_text(encoding='utf-8'))
        before = manifest()
        assert base['status'] == 'PASS' and base['candidate_files_sha256'] == before, 'exact base candidate must pass first'
        wheel = candidate_wheel(output)
        assert hashlib.sha256(wheel.read_bytes()).hexdigest() == base['wheel_sha256']
        # A short system-temp path avoids Windows package paths exceeding platform limits.
        # Failure stages are durable before each subprocess, independent of cleanup.
        with tempfile.TemporaryDirectory(prefix='tf-workflow-') as temporary:
            staging = Path(temporary)
            stage('CREATE_ENVIRONMENT')
            run([sys.executable, '-m', 'venv', str(staging / 'venv')], cwd=staging, env=env)
            py = staging / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            stage('INSTALL_OPTIONAL_DEPENDENCIES')
            install_out, install_err = run([str(py), '-m', 'pip', 'install', '--disable-pip-version-check',
                                           '--timeout', '15', '--retries', '1', str(wheel) + '[langgraph]'], cwd=staging, env=env)
            (output / 'optional-install.log').write_text(install_out + install_err, encoding='utf-8')
            stage('CHECK_PINNED_VERSION')
            run([str(py), '-I', '-c', "from importlib.metadata import version; assert version('langgraph')=='1.2.14'"], cwd=staging, env=env)
            stage('RUN_OPTIONAL_CONFORMANCE')
            receipt = output / 'optional-suite.json'
            logs, errors = run([str(py), '-I', str(ROOT / 'tests/run_suite.py'), '--suite', str(ROOT / 'tests_optional'), '--receipt', str(receipt)], cwd=staging, env=env)
            (output / 'optional-tests.log').write_text(logs + errors, encoding='utf-8')
            suite = json.loads(receipt.read_text(encoding='utf-8'))
            assert suite['status'] == 'PASS'
            run([str(py), '-m', 'pip', 'check'], cwd=staging, env=env)
            dependencies, _ = run([str(py), '-m', 'pip', 'freeze', '--all'], cwd=staging, env=env)
            # Wheel direct-file lines are local paths; retain versions only as receipt metadata.
            packages = [line for line in dependencies.splitlines() if '==' in line]
        assert before == manifest(), 'candidate changed during optional verification'
        report = {'status': 'PASS', 'scope': 'LANGGRAPH_OPTIONAL', 'installed_only': True,
                  'langgraph': '1.2.14', 'kernel_authority_replay': True, 'optional_cases': suite['run'], 'suite': suite,
                  'wheel_sha256': base['wheel_sha256'], 'dependency_versions': packages,
                  'candidate_files_sha256': before, 'elapsed_seconds': round(time.perf_counter() - started, 3),
                  'linux_isolation_run': False, 'postgres_run': False}
    except Exception as error:
        report = {'status': 'FAIL', 'scope': 'LANGGRAPH_OPTIONAL', 'error': str(error),
                  'stage': report.get('stage', 'VALIDATE_BASE_CANDIDATE'),
                  'elapsed_seconds': round(time.perf_counter() - started, 3)}
    output.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    except OSError:
        print(json.dumps(report, indent=2))
        raise
    print(json.dumps({key: value for key, value in report.items() if key != 'candidate_files_sha256'}, indent=2))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
