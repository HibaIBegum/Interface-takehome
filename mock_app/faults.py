"""Injectable faults, armed via POST /__faults and consumed by content-frame requests."""

from __future__ import annotations

import threading
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Fault(str, Enum):
    SESSION_TIMEOUT = "session_timeout"
    SERVER_ERROR = "server_error"
    INTERSTITIAL = "interstitial"
    SLOW_LOAD = "slow_load"


class FaultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fault: Fault
    count: int = Field(default=1, ge=0, le=100, description="Number of requests to affect; 0 disarms.")
    delay_ms: int = Field(default=3000, ge=0, le=30000, description="slow_load only.")


class FaultInjector:
    """Thread-safe countdown per fault. At most one fault fires per request, in enum order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._armed: dict[Fault, int] = {}
        self.delay_s = 3.0

    def arm(self, req: FaultRequest) -> None:
        with self._lock:
            if req.count == 0:
                self._armed.pop(req.fault, None)
            else:
                self._armed[req.fault] = req.count
            if req.fault is Fault.SLOW_LOAD:
                self.delay_s = req.delay_ms / 1000

    def clear(self) -> None:
        with self._lock:
            self._armed.clear()

    def snapshot(self) -> dict[str, int]:
        with self._lock:
            return {f.value: n for f, n in self._armed.items()}

    def consume(self, method: str) -> Fault | None:
        with self._lock:
            for fault in Fault:
                if self._armed.get(fault, 0) <= 0:
                    continue
                # A modal only makes sense on a rendered page; POSTs here redirect.
                if fault is Fault.INTERSTITIAL and method != "GET":
                    continue
                self._armed[fault] -= 1
                if self._armed[fault] == 0:
                    del self._armed[fault]
                return fault
        return None
