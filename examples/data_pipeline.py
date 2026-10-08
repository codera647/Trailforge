"""A non-PR host using only the public SDK-free Trailforge API."""
import json

from trailforge.ports import AdapterCapabilities, FunctionAdapter, VerifiedResult
from trailforge.runtime import Goal, Harness, Task


def total(request):
    return {'total': sum(request.input['values'])}


def verify(request):
    result = request.dependencies['sum']['total']
    return VerifiedResult('PASS' if result == request.input['expected'] else 'FAIL',
                          request.goal_digest, {'observed': result})


def main():
    adapters = [FunctionAdapter(AdapterCapabilities('example.sum/1', 'TOOL'), total),
                FunctionAdapter(AdapterCapabilities('example.verify/1', 'VERIFY'), verify)]
    goal = Goal('example-tenant', 'data-pipeline', 'Sum a bounded original dataset and verify its total',
                (Task('sum', 'example.sum/1', {'values': [1, 2, 3]}),
                 Task('check', 'example.verify/1', {'expected': 6}, ('sum',))), ('check',))
    harness = Harness('data-pipeline.sqlite3', tenant_id=goal.tenant_id, resource_id=goal.resource_id, adapters=adapters)
    run_id = harness.start(goal)
    harness.advance(run_id, goal, pause_after='sum')
    recovered = Harness('data-pipeline.sqlite3', tenant_id=goal.tenant_id, resource_id=goal.resource_id, adapters=adapters)
    record = recovered.advance(run_id, goal)
    print(json.dumps({'run_id': run_id, 'state': record['state'], 'units_reserved': record['units_reserved'],
                      'result': record['tasks']['sum']['result']}, indent=2))


if __name__ == '__main__':
    main()
