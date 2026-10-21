"""SSE 出口成帧：业务帧单调 id（seq）+ 空闲心跳；心跳不占序号、DONE 不占序号。"""
from __future__ import annotations

import asyncio

from app.api.sse import to_sse
from app.core.models import FinishReason, TokenUsage
from app.core.streaming import StreamEvent


def _frame_id(frame: str) -> int | None:
    for line in frame.splitlines():
        if line.startswith("id:"):
            return int(line[3:].strip())
    return None


def _is_heartbeat(frame: str) -> bool:
    return frame == ": heartbeat\n\n"


def _is_done(frame: str) -> bool:
    return any(ln.strip() == "data: [DONE]" for ln in frame.splitlines())


async def _slow_events():
    yield StreamEvent.start("standard-chat", resolved_model="m", endpoint_id="e")
    await asyncio.sleep(0.12)  # 空闲间隔，期间应下发心跳
    yield StreamEvent.delta("hi")
    yield StreamEvent.finish(
        FinishReason.STOP, resolved_model="m", endpoint_id="e",
        usage=TokenUsage(input_tokens=1, output_tokens=1),
    )


async def test_business_frames_have_monotonic_seq_and_done_unnumbered():
    frames = [f async for f in to_sse(_slow_events(), heartbeat_seconds=None)]

    numbered = [_frame_id(f) for f in frames if _frame_id(f) is not None]
    # start / delta / finish 三帧（无 usage 快照时 finish 不补发 usage 帧）
    assert numbered == list(range(len(numbered)))
    assert len(numbered) >= 3
    # 终止哨兵 [DONE] 不分配 id
    done_frames = [f for f in frames if _is_done(f)]
    assert len(done_frames) == 1
    assert _frame_id(done_frames[0]) is None


async def test_heartbeat_emitted_when_idle_and_does_not_consume_seq():
    frames = [f async for f in to_sse(_slow_events(), heartbeat_seconds=0.04)]

    heartbeats = [f for f in frames if _is_heartbeat(f)]
    assert len(heartbeats) >= 1  # 0.12s 的空闲间隔内至少一次心跳

    # 心跳帧不携带 id，业务帧序号仍然从 0 连续递增、不被心跳打断
    assert all(_frame_id(f) is None for f in heartbeats)
    numbered = [_frame_id(f) for f in frames if _frame_id(f) is not None]
    assert numbered == list(range(len(numbered)))
