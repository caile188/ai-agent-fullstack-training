"""总超时预算：重试由"次数 + 总截止时间 + 错误类型"共同约束。"""
from __future__ import annotations

import time


class TimeoutBudget:
    """以单调时钟计的剩余时间预算，贯穿整条 fallback 链。"""

    def __init__(self, total_ms: int, connect_ms: int, ttft_ms: int) -> None:
        self.total_ms = total_ms
        self.connect_ms = connect_ms
        self.ttft_ms = ttft_ms
        self._start = time.monotonic()

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._start) * 1000.0

    def remaining_ms(self) -> float:
        return max(0.0, self.total_ms - self.elapsed_ms())

    def expired(self) -> bool:
        return self.remaining_ms() <= 0

    def request_timeout(self, stream: bool) -> float:
        """本次 HTTP 调用允许的总时长（受剩余预算与连接/TTFT 边界约束）。"""
        if stream:
            # 流式：连接 + TTFT 是强边界；之后生成期不做整体硬超时（交给读超时/取消）
            cap = self.connect_ms + self.ttft_ms
        else:
            cap = self.total_ms
        return min(cap / 1000.0, max(0.0, self.remaining_ms() / 1000.0))
