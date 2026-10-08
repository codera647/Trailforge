"""Run the original Linux boundary suite against the exact passed base wheel."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from verify_local import ROOT, manifest, run, candidate_wheel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    args = parser.parse_args()
    output = ROOT / '.local/verification'
    path = output / 'sandbox-report.json'
    output.mkdir(parents=True, exist_ok=True)
    report = {'status': 'IN_PROGRESS', 'scope': 'INSTALLED_REAL_LINUX_BOUNDARIES'}
    path.write_text(json.dumps(report), encoding='utf-8')
    env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'TRAILFORGE_DATABASE_URL')}
    try:
        before = manifest()
        base = json.loads((output / 'report.json').read_text(encoding='utf-8'))
        assert base['status'] == 'PASS' and base['candidate_files_sha256'] == before
        wheel = candidate_wheel(output)
        assert hashlib.sha256(wheel.read_bytes()).hexdigest() == base['wheel_sha256']
        with tempfile.TemporaryDirectory(prefix='tf-boundary-') as folder:
            temporary = Path(folder)
            shutil.copy2(ROOT / 'scripts/verify_sandbox.py', temporary / 'verify_sandbox.py')
            run([sys.executable, '-m', 'venv', str(temporary / 'venv')], cwd=temporary, env=env)
            py = temporary / 'venv' / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
            run([str(py), '-m', 'pip', 'install', '--no-index', '--no-deps', str(wheel)], cwd=temporary, env=env)
            run([str(py), '-I', '-c', 'import trailforge,sys; from pathlib import Path; assert Path(trailforge.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())'], cwd=temporary, env=env)
            logs, errors = run([str(py), '-I', str(temporary / 'verify_sandbox.py'), '--image', args.image,
                                '--output', str(path)], cwd=temporary, env=env)
            (output / 'sandbox-tests.log').write_text(logs + errors, encoding='utf-8')
            report = json.loads(path.read_text(encoding='utf-8'))
            assert report['status'] == 'PASS' and report['cases'] == 5
        assert before == manifest()
        report.update(installed_only=True, wheel_sha256=base['wheel_sha256'], candidate_files_sha256=before)
    except Exception as error:
        report.update(status='FAIL', error_code=type(error).__name__)
    path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k not in ('candidate_files_sha256', 'receipts')}, indent=2), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
