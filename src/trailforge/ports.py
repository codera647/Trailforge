"""Versioned application-neutral ports. Plugin implementations are trusted host code."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .core import ContractError, canonical, nonempty
from .contracts import integer

ADAPTER_PROTOCOL = 'trailforge/adapter/0.1'
EFFECTS = frozenset({'PURE', 'SCOPED_READ', 'CANDIDATE_WRITE', 'EXTERNAL_WRITE'})


@dataclass(frozen=True)
class AdapterCapabilities:
    adapter_id: str
    kind: str
    effect: str = 'PURE'
    protocol: str = ADAPTER_PROTOCOL
    max_input_bytes: int = 65536
    max_output_bytes: int = 65536
    reservation_units: int = 1

    def validate(self):
        nonempty(self.adapter_id)
        if self.protocol != ADAPTER_PROTOCOL or self.kind not in ('MODEL', 'TOOL', 'VERIFY') or self.effect not in EFFECTS:
            raise ContractError('unsupported adapter capability')
        integer(self.max_input_bytes, 1, 1048576)
        integer(self.max_output_bytes, 1, 1048576)
        integer(self.reservation_units, 1, 1000000)


@dataclass(frozen=True)
class TaskRequest:
    run_id: str
    task_id: str
    scope: tuple[str, str]
    goal_digest: str
    input: Any
    dependencies: dict[str, Any]


@dataclass(frozen=True)
class VerifiedResult:
    """Host verifier result; JSON from a model is not this authority type."""
    verdict: str
    goal_digest: str
    evidence: Any


class TaskAdapter(Protocol):
    capabilities: AdapterCapabilities

    def execute(self, request: TaskRequest) -> Any: ...


class FunctionAdapter:
    """Explicit host composition; never dynamically import a module named by model data."""
    def __init__(self, capabilities, handler):
        capabilities.validate()
        if not callable(handler):
            raise ContractError('explicit adapter handler required')
        self.capabilities = capabilities
        self._handler = handler

    def execute(self, request):
        return self._handler(request)


class ArtifactPort(Protocol):
    def put(self, scope: tuple[str, str], content: bytes, media_type: str) -> dict: ...
    def get(self, scope: tuple[str, str], receipt: dict) -> bytes: ...


class WorkflowPort(Protocol):
    """Schedules trusted controller steps; cannot grant permissions or mint verdicts."""
    def advance(self, run_id: str) -> dict: ...


class RetrievalPort(Protocol):
    def retrieve(self, scope: tuple[str, str], revision: str, query: str, limit: int) -> list[dict]: ...


class PublisherPort(Protocol):
    def prepare(self, intent: dict) -> dict: ...
    def execute(self, operation: dict) -> dict: ...
    def inspect(self, operation: dict) -> dict: ...


class TelemetryPort(Protocol):
    def emit(self, event: dict) -> None: ...


def bounded_json(value, limit):
    """Detach mutable plugin data and reject oversized/nonfinite/non-JSON output."""
    from .core import strict_json
    try:
        raw = canonical(value)
        if len(raw.encode('utf-8')) > limit:
            raise ContractError('adapter data exceeds bound')
        return strict_json(raw)
    except (TypeError, ValueError, RecursionError):
        raise ContractError('invalid or oversized adapter JSON') from None
