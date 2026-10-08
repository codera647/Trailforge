from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from trailforge.adapters.langgraph import LangGraphWorkflow
from trailforge.core import ContractError
from trailforge.ports import AdapterCapabilities, FunctionAdapter, VerifiedResult
from trailforge.runtime import Goal, Harness, Task


class WorkflowTests(unittest.TestCase):
    def test_real_graph_restart_and_replay_use_kernel_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            calls = [0]
            def tool(request):
                from langsmith.run_helpers import get_tracing_context
                self.assertIs(get_tracing_context()['enabled'], False)
                calls[0] += 1
                return {'value': 6}
            adapters = [FunctionAdapter(AdapterCapabilities('tool/1', 'TOOL'), tool),
                        FunctionAdapter(AdapterCapabilities('check/1', 'VERIFY'),
                                        lambda request: VerifiedResult('PASS', request.goal_digest, request.dependencies))]
            goal = Goal('tenant', 'resource', 'Workflow contract',
                        (Task('tool', 'tool/1', {}), Task('check', 'check/1', {}, ('tool',))), ('check',))
            def host():
                return Harness(Path(temporary) / 'runs.sqlite3', tenant_id='tenant', resource_id='resource', adapters=adapters)
            harness = host()
            run_id = harness.start(goal)
            self.assertEqual(LangGraphWorkflow(harness, goal, pause_after='tool').advance(run_id)['state'], 'CHECKING')
            recovered = LangGraphWorkflow(host(), goal)
            record = recovered.advance(run_id)
            self.assertEqual(record['state'], 'COMPLETED')
            self.assertEqual(recovered.advance(run_id), record)
            self.assertEqual(calls[0], 1)
            with self.assertRaises(ContractError):
                LangGraphWorkflow(host(), replace(goal, objective='changed')).advance(run_id)

    def test_real_graph_cannot_retry_unknown_or_approve_external_effects(self):
        with tempfile.TemporaryDirectory() as temporary:
            calls = [0]
            def failure(request):
                calls[0] += 1
                raise OSError('unavailable')
            adapters = [FunctionAdapter(AdapterCapabilities('tool/1', 'TOOL'), failure),
                        FunctionAdapter(AdapterCapabilities('check/1', 'VERIFY'),
                                        lambda request: VerifiedResult('PASS', request.goal_digest, {}))]
            host = Harness(Path(temporary) / 'runs.sqlite3', tenant_id='tenant', resource_id='resource', adapters=adapters)
            goal = Goal('tenant', 'resource', 'Workflow contract',
                        (Task('tool', 'tool/1', {}), Task('check', 'check/1', {}, ('tool',))), ('check',))
            workflow = LangGraphWorkflow(host, goal)
            run_id = host.start(goal)
            uncertain = workflow.advance(run_id)
            self.assertEqual(uncertain['state'], 'WAITING_HUMAN')
            self.assertEqual(workflow.advance(run_id), uncertain)
            self.assertEqual(calls[0], 1)
            with self.assertRaises(ContractError):
                host.start(replace(goal, allowed_effects=('EXTERNAL_WRITE',)))
