"""价格版本目录：把当前配置的端点单价固化为内容寻址的版本。

成本必须用"调用发生那一刻的价格表"计算，不能用今天的价格回算几个月前的调用。
启动时从配置派生一份确定性快照并算出 sha256 版本号；cost_ledger 绑定该版本，
价格快照落 price_versions 表，保证历史成本可按当时单价复核。
"""
from __future__ import annotations

import hashlib
import json

from app.config import GatewayConfig
from app.core.capabilities import Price


class PriceCatalog:
    def __init__(self, config: GatewayConfig) -> None:
        snapshot: dict[str, dict[str, float]] = {}
        self._prices: dict[str, Price] = {}
        for eid in sorted(config.endpoints):
            ep = config.endpoints[eid]
            p = ep.price_model()
            self._prices[eid] = p
            snapshot[eid] = {
                "input_per_1m": p.input_per_1m,
                "output_per_1m": p.output_per_1m,
                "cached_per_1m": p.cached_per_1m,
            }
        self.snapshot = snapshot
        canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.version = f"cfg_{digest[:12]}"

    def price(self, endpoint_id: str) -> Price:
        return self._prices[endpoint_id]

    def snapshot_json(self) -> str:
        return json.dumps(self.snapshot, sort_keys=True, ensure_ascii=False)
