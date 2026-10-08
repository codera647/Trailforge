"""Run wakeups request recovery; they never authorize model or external effects."""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class DispatchToken:
    message_id: str
    worker_id: str
    epoch: int


class DispatchStore(Protocol):
    def claim_dispatch(self) -> tuple[DispatchToken, dict] | None: ...
    def acknowledge_dispatch(self, token: DispatchToken) -> None: ...
    def retry_dispatch(self, token: DispatchToken) -> None: ...


class DispatchDeliveryError(RuntimeError):
    """Stable transport failure; raw provider exception text is not exposed."""


class WakeupTransport(Protocol):
    def send(self, message: dict) -> None:
        """Return only after transport accepts the stable message identity.

        Lost acknowledgement can cause a duplicate wakeup. Consumers must load
        current scoped authority and resume; message text grants no permission.
        This port must never be used for COMMENT or other non-idempotent effects.
        """
        ...


class Dispatcher:
    def __init__(self, store: DispatchStore, transport: WakeupTransport):
        self.store, self.transport = store, transport

    def dispatch_one(self) -> bool:
        claimed = self.store.claim_dispatch()
        if claimed is None:
            return False
        token, message = claimed
        try:
            self.transport.send(message)
        except Exception:
            # Store only a stable code; arbitrary transport errors may be secret.
            # Exceptions include unknown queue acceptance; duplicates are safe
            # only because this adapter sends recovery hints, never effects.
            try:
                self.store.retry_dispatch(token)
            except Exception:
                raise DispatchDeliveryError("wakeup delivery failed; durable claim requires recovery") from None
            raise DispatchDeliveryError("wakeup delivery failed; durable retry recorded") from None
        self.store.acknowledge_dispatch(token)
        return True
