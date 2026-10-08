"""Original owned test worker: actual process death after durable admission."""
import json
import os
from pathlib import Path
import sys

from trailforge.ports import AdapterCapabilities, FunctionAdapter, VerifiedResult
from trailforge.runtime import Goal, Harness, Task

phase, directory = sys.argv[1], Path(sys.argv[2])


def transform(request):
    with (directory / 'calls.txt').open('a', encoding='utf-8') as stream:
        stream.write('called\n')
        stream.flush()
        os.fsync(stream.fileno())
    marker = directory / 'crash-once'
    if marker.exists():
        marker.unlink()
        os._exit(17)
    return {'total': sum(request.input['values'])}


def verify(request):
    result = request.dependencies['transform']['total']
    return VerifiedResult('PASS' if result == 6 else 'FAIL', request.goal_digest, {'total': result})


adapters = [FunctionAdapter(AdapterCapabilities('original/transform/1', 'TOOL'), transform),
            FunctionAdapter(AdapterCapabilities('original/verify/1', 'VERIFY'), verify)]
goal = Goal('tenant', 'data', 'Original neutral transformation',
            (Task('transform', 'original/transform/1', {'values': [1, 2, 3]}),
             Task('verify', 'original/verify/1', {}, ('transform',))), ('verify',), budget_units=3)
harness = Harness(directory / 'runs.sqlite3', tenant_id='tenant', resource_id='data', adapters=adapters,
                  clock=lambda: 1000 if phase in ('start', 'crash') else 1031)
if phase in ('start', 'crash'):
    run_id = harness.start(goal)
    (directory / 'run-id.json').write_text(json.dumps({'run_id': run_id}), encoding='utf-8')
    if phase == 'crash':
        (directory / 'crash-once').write_text('original fault marker', encoding='utf-8')
    record = harness.advance(run_id, goal, pause_after='transform')
else:
    run_id = json.loads((directory / 'run-id.json').read_text(encoding='utf-8'))['run_id']
    if phase == 'retry':
        harness.resolve_unknown(run_id, goal, 'transform', disposition='RETRY')
    record = harness.advance(run_id, goal)
print(json.dumps(record))
