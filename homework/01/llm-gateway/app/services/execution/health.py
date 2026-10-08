"""熔断器：closed / open / half-open。

- 连续失败达阈值 -> open（路由直接拒绝该端点，记 circuit_open）
- 冷却结束 -> half-open，仅放行有限探测请求
- 探测成功 -> closed；探测失败 -> 重新 open
"""
from __future__ import annotations

import enum
import threading
import time

from app.config import CircuitConfig


class BreakerState(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(self, config: CircuitConfig) -> None:
        self.config = config
        self._lock = threading.Lock()
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._state: dict[str, BreakerState] = {}
        self._probes_in_flight = 0

    def _state_of(self, endpoint_id: str) -> BreakerState:
        return self._state.get(endpoint_id, BreakerState.CLOSED)

    def is_open(self, endpoint_id: str) -> bool:
        """路由时判定：open 拒绝；half-open 仅在探测名额满时拒绝。"""
        with self._lock:
            st = self._state_of(endpoint_id)
            if st == BreakerState.CLOSED:
                return False
            if st == BreakerState.OPEN:
                opened = self._opened_at.get(endpoint_id)
                if opened is not None and time.monotonic() - opened >= self.config.cooldown_seconds:
                    # 进入半开
                    self._state[endpoint_id] = BreakerState.HALF_OPEN
                    self._probes_in_flight = 0
                    return not self._try_probe(endpoint_id)
                return True
            # half-open：只允许 half_open_probe 个探测
            return not self._try_probe(endpoint_id)

    def _try_probe(self, endpoint_id: str) -> bool:
        if self._probes_in_flight < self.config.half_open_probe:
            self._probes_in_flight += 1
            return True
        return False

    def record_success(self, endpoint_id: str) -> None:
        with self._lock:
            self._failures[endpoint_id] = 0
            self._opened_at.pop(endpoint_id, None)
            self._state[endpoint_id] = BreakerState.CLOSED
            self._probes_in_flight = 0

    def record_failure(self, endpoint_id: str) -> None:
        with self._lock:
            st = self._state_of(endpoint_id)
            if st == BreakerState.HALF_OPEN:
                # 探测失败：立即重新打开
                self._state[endpoint_id] = BreakerState.OPEN
                self._opened_at[endpoint_id] = time.monotonic()
                self._probes_in_flight = 0
                return
            failures = self._failures.get(endpoint_id, 0) + 1
            self._failures[endpoint_id] = failures
            if failures >= self.config.failure_threshold:
                self._state[endpoint_id] = BreakerState.OPEN
                self._opened_at[endpoint_id] = time.monotonic()

    def health_score(self, endpoint_id: str) -> float:
        """粗略健康分：closed=1，half-open=0.5，open=0。"""
        with self._lock:
            return {
                BreakerState.CLOSED: 1.0,
                BreakerState.HALF_OPEN: 0.5,
                BreakerState.OPEN: 0.0,
            }[self._state_of(endpoint_id)]

    def status(self) -> dict[str, dict[str, object]]:
        with self._lock:
            return {
                eid: {
                    "state": self._state_of(eid).value,
                    "failures": self._failures.get(eid, 0),
                }
                for eid in list(self._state)
            }
