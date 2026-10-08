"""Allowlisted structured events; no source, prompt, actor or provider payload fields."""
import threading
from uuid import UUID

from .contracts import hash_value, integer
from .core import ContractError, canonical, fields

KINDS = frozenset({'ADMITTED', 'TASK_ADMITTED', 'TASK_RESULT_RECORDED', 'GOAL_CHECKS_EVALUATED',
                   'WAITING_HUMAN', 'CANCELLED', 'PUBLICATION_UNKNOWN', 'PUBLICATION_ACCEPTED'})
STATES = frozenset({'READY', 'RUNNING', 'CHECKING', 'COMPLETED', 'FAILED', 'CANCELLED',
                    'WAITING_HUMAN', 'SENDING', 'UNKNOWN', 'ACCEPTED'})


class JsonEventSink:
    """Host-owned stream; lock coordinates this instance only, not processes."""
    def __init__(self, stream):
        self.stream, self.lock = stream, threading.Lock()

    def emit(self, event):
        fields(event, {'schema_version', 'run_id', 'scope_digest', 'kind', 'state', 'units_reserved'})
        if event['schema_version'] != 'trailforge/telemetry/0.1' or event['kind'] not in KINDS or event['state'] not in STATES:
            raise ContractError('unsupported telemetry event')
        try:
            if str(UUID(event['run_id'])) != event['run_id']:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise ContractError('canonical telemetry run ID required') from None
        hash_value(event['scope_digest'])
        integer(event['units_reserved'])
        raw = canonical(event) + '\n'
        with self.lock:
            self.stream.write(raw)
            self.stream.flush()
