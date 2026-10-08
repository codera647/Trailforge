"""Real Linux Docker conformance. Missing engine/image is BLOCKED, never skipped PASS."""
import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import tempfile
import time

from trailforge.adapters.sandbox import DockerSandbox, SandboxDenied, SandboxLimits, tree_digest
from trailforge.core import digest

BOUNDARY = '''import json, os, socket
assert os.getuid() == 65534
assert os.environ.get('TRAILFORGE_TEST_HOST_CANARY') is None
assert not os.path.exists('/var/run/docker.sock')
assert not os.path.exists('/host-canary.txt')
denied = 0
for path in ('/candidate/input.txt', '/checks/boundary.py', '/etc/trailforge-write'):
    try:
        with open(path, 'w') as stream: stream.write('denied')
    except OSError:
        denied += 1
assert denied == 3
connection = socket.socket()
connection.settimeout(2)
network_denied = False
try:
    connection.connect(('203.0.113.1', 443))
except OSError:
    network_denied = True
finally:
    connection.close()
assert network_denied
with open('/proc/self/status') as stream:
    capabilities = next(line for line in stream if line.startswith('CapEff:')).split()[1]
assert int(capabilities, 16) == 0
from pathlib import Path
memory = next(path for path in (Path('/sys/fs/cgroup/memory.max'), Path('/sys/fs/cgroup/memory/memory.limit_in_bytes')) if path.is_file())
assert int(memory.read_text()) == 268435456
print(json.dumps({'uid': 65534, 'write_denials': denied, 'network_denied': network_denied, 'capabilities': 0, 'memory_bytes': 268435456}))
'''
PIDS = '''import json, os, signal, time
children = []
limited = False
try:
    for index in range(64):
        try: child = os.fork()
        except OSError:
            limited = True
            break
        if child == 0:
            while True: time.sleep(1)
        children.append(child)
finally:
    for child in children: os.kill(child, signal.SIGKILL)
    for child in children: os.waitpid(child, 0)
assert limited
print(json.dumps({'pids_limited': limited, 'children': len(children)}))
'''


class ObservedSandbox(DockerSandbox):
    def command(self, *args, **kwargs):
        self.owned_name = kwargs['name']
        return super().command(*args, **kwargs)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True, help='Preloaded digest-pinned Linux image providing python3 >=3.11')
    parser.add_argument('--output', default='.local/verification/linux-sandbox.json')
    args = parser.parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    report = {'status': 'IN_PROGRESS', 'scope': 'REAL_LINUX_DOCKER_BOUNDARIES', 'image': args.image}
    receipts = []
    try:
        probe = DockerSandbox(args.image)
        probe.preflight()
        with tempfile.TemporaryDirectory(prefix='trailforge-linux-') as temporary:
            root = Path(temporary)
            candidate, checks = root / 'candidate', root / 'checks'
            candidate.mkdir(mode=0o755)
            checks.mkdir(mode=0o755)
            root.chmod(0o755)
            (root / 'host-canary.txt').write_text('ORIGINAL_TEST_CANARY', encoding='utf-8')
            (candidate / 'input.txt').write_text('original input', encoding='utf-8')
            scripts = {'boundary.py': BOUNDARY, 'pids.py': PIDS,
                       'output.py': "import sys; sys.stdout.write('x' * 200000); sys.stdout.flush()\n",
                       'timeout.py': 'import time; time.sleep(60)\n'}
            for name, content in scripts.items():
                (checks / name).write_text(content, encoding='utf-8')
                (checks / name).chmod(0o644)
            (candidate / 'input.txt').chmod(0o644)
            goal = digest({'criteria': 'original Linux conformance', 'image': args.image, 'checks': tree_digest(checks)})
            previous = os.environ.get('TRAILFORGE_TEST_HOST_CANARY')
            os.environ['TRAILFORGE_TEST_HOST_CANARY'] = 'ORIGINAL_TEST_CANARY'
            try:
                for name, limits, expected in (
                    ('boundary.py', SandboxLimits(), 'EXECUTED'),
                    ('pids.py', SandboxLimits(pids=16), 'EXECUTED'),
                    ('output.py', SandboxLimits(output_bytes=1024), 'OUTPUT_LIMIT'),
                    ('timeout.py', SandboxLimits(seconds=2), 'TIMEOUT')):
                    sandbox = ObservedSandbox(args.image, limits=limits)
                    receipt = sandbox.run(candidate, checks, ('python3', '-I', '/checks/' + name),
                                          goal_digest=goal, candidate_digest=tree_digest(candidate), checks_digest=tree_digest(checks))
                    assert receipt['status'] == expected, (name, receipt['status'])
                    assert receipt['criterion_verdict'] == 'UNKNOWN', 'execution cannot self-grade semantics'
                    assert receipt['output_bytes'] <= limits.output_bytes
                    if expected == 'EXECUTED':
                        assert receipt['exit_code'] == 0
                        observed = json.loads(receipt['output'])
                        if name == 'boundary.py':
                            assert observed == {'uid': 65534, 'write_denials': 3, 'network_denied': True,
                                                'capabilities': 0, 'memory_bytes': 268435456}
                        else:
                            assert observed['pids_limited'] is True and observed['children'] < 16
                    # Inspect only our owned name, never enumerate/delete unrelated containers.
                    assert sandbox.confirm_absent(sandbox.owned_name), 'owned container remains'
                    receipts.append({'case': name, 'limits': asdict(limits),
                                     'receipt': {key: value for key,value in receipt.items() if key != 'output'}})
                before = tree_digest(candidate)
                (candidate / 'input.txt').write_text('changed candidate', encoding='utf-8')
                try:
                    probe.run(candidate, checks, ('python3', '-I', '/checks/boundary.py'),
                              goal_digest=goal, candidate_digest=before, checks_digest=tree_digest(checks))
                except SandboxDenied as error:
                    assert str(error) == 'WORKSPACE_CHANGED'
                else:
                    raise AssertionError('stale candidate admitted')
            finally:
                if previous is None:
                    os.environ.pop('TRAILFORGE_TEST_HOST_CANARY', None)
                else:
                    os.environ['TRAILFORGE_TEST_HOST_CANARY'] = previous
        report.update(status='PASS', cases=5, linux_isolation_run=True, criteria_origin='ORIGINAL_MAKER_OWNED_TESTS', receipts=receipts)
    except SandboxDenied as error:
        code = str(error)
        report.update(status='BLOCKED' if code in ('SANDBOX_UNAVAILABLE', 'PINNED_IMAGE_UNAVAILABLE', 'LINUX_ENGINE_REQUIRED') else 'FAIL',
                      error_code=code, linux_isolation_run=False, receipts=receipts)
    except Exception:
        report.update(status='FAIL', error_code='CONFORMANCE_FAILED', receipts=receipts)
    report['elapsed_seconds'] = round(time.perf_counter() - started, 3)
    output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key:value for key,value in report.items() if key != 'receipts'}, indent=2))
    return 0 if report['status'] == 'PASS' else 2 if report['status'] == 'BLOCKED' else 1


if __name__ == '__main__':
    raise SystemExit(main())
