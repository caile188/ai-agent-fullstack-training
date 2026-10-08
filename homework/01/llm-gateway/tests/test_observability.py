"""脱敏与成本计算。"""
from __future__ import annotations

from app.core.models import TokenUsage
from app.services.observability.cost import price_usage
from app.services.observability.redaction import fingerprint, redact, text_meta


def test_redact_secrets():
    assert "sk-abcdefgh123456" not in redact("token sk-abcdefgh123456 here")
    assert "[REDACTED]" in redact("Authorization: Bearer sk-abcdefgh123456")
    # 正文不入库：只留长度与指纹
    meta = text_meta("hello world")
    assert set(meta) == {"chars", "sha256"}
    assert meta["chars"] == 11
    assert fingerprint("hello world") == fingerprint("hello world")


async def test_cost_ledger_pricing(harness):
    svc, _ = harness
    # pro: input 4.50/1m, output 13.50/1m（价格以 gateway.yaml 为准）
    usage = TokenUsage(input_tokens=1_000_000, output_tokens=1_000_000, cached_tokens=0)
    cost = price_usage(svc.registry, "ds-v4-pro", usage)
    assert round(cost.input_cost, 4) == 4.50
    assert round(cost.output_cost, 4) == 13.50
    assert round(cost.total, 4) == 18.00
