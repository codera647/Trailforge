"""Installed-only original Develop example; no untrusted code or shell execution."""
from pathlib import Path
import json
import tempfile

from trailforge.develop import CandidateWorkspace, Develop
from trailforge.ports import AdapterCapabilities, FunctionAdapter, VerifiedResult


def run():
    with tempfile.TemporaryDirectory(prefix='trailforge-develop-') as temporary:
        root = Path(temporary)
        candidate = CandidateWorkspace(root / 'candidate', allowed_paths=('result.json',))
        checks = root / 'protected-checks'
        checks.mkdir()
        (checks / 'expected.json').write_text('[1,2,3]', encoding='utf-8')

        def build(request):
            candidate.write('result.json', json.dumps(sorted([3, 1, 2])).encode())
            return {'written': 'result.json'}

        def verify(request):
            expected = json.loads((checks / 'expected.json').read_text(encoding='utf-8'))
            observed = json.loads(candidate.read('result.json'))
            return VerifiedResult('PASS' if observed == expected else 'FAIL', request.goal_digest,
                                  {'expected': expected, 'observed': observed})

        adapters = {phase: FunctionAdapter(AdapterCapabilities('example/' + phase.lower(), 'TOOL'),
                                           lambda request: {'task': request.task_id}) for phase in ('PLAN', 'RESEARCH', 'DEBUG')}
        adapters['BUILD'] = FunctionAdapter(AdapterCapabilities('example/build', 'TOOL', 'CANDIDATE_WRITE'), build)
        adapters['VERIFY'] = FunctionAdapter(AdapterCapabilities('example/verify', 'VERIFY', 'SCOPED_READ'), verify)
        def profile():
            return Develop(root / 'runs.sqlite3', tenant_id='example', resource_id='data', objective='Write sorted data',
                           workspace=candidate, checks=checks, adapters=adapters)
        first = profile()
        run_id = first.start()
        first.advance(run_id, pause_after='1:BUILD')
        recovered = profile()
        recovered.advance(run_id)
        print(json.dumps(recovered.handoff(run_id), indent=2))


if __name__ == '__main__':
    run()
