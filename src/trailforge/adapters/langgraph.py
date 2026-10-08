"""Optional scheduling facade. Kernel SQLite, not graph state, owns authority."""
from typing import TypedDict

from ..core import ContractError, nonempty


class _State(TypedDict):
    run_id: str
    result: dict


class LangGraphWorkflow:
    def __init__(self, harness, goal, *, pause_after=None):
        try:
            from langgraph.graph import END, START, StateGraph
        except ImportError:
            raise ContractError('optional langgraph extra is not installed') from None
        self.harness, self.goal, self.pause_after = harness, goal, pause_after
        # No graph checkpointer or provider; kernel run ID is the resume authority.
        graph = StateGraph(_State)
        graph.add_node('kernel', self._step)
        graph.add_edge(START, 'kernel')
        graph.add_edge('kernel', END)
        self._graph = graph.compile()

    def _step(self, state):
        return {'result': self.harness.advance(nonempty(state['run_id']), self.goal, pause_after=self.pause_after)}

    def advance(self, run_id):
        # No arbitrary graph update/Command input or human approval resume value.
        from langsmith import tracing_context
        # Do not inherit automatic cloud tracing from a caller's environment.
        # Hosts can export allowlisted kernel events through their telemetry port.
        with tracing_context(enabled=False, parent=False):
            return self._graph.invoke({'run_id': nonempty(run_id)}, config={'recursion_limit': 4})['result']
