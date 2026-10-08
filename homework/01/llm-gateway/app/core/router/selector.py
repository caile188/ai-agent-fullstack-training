"""同优先级候选内的平滑加权轮询（nginx SWRR 思路）。

每次选择：各候选 current += weight，选 current 最大者，再把其 current -= 总权重。
长期看选中比例正比于 weight，且相邻选择尽量分散（平滑），无随机、确定性可回放。
状态按 logical_model 隔离，仅保活在进程内，不落库。
"""
from __future__ import annotations


class SmoothWeightedRR:
    def __init__(self) -> None:
        # key -> {id: [weight, current]}
        self._state: dict[str, dict[str, list[int]]] = {}

    def pick(self, key: str, weights: dict[str, int]) -> str:
        ids = sorted(weights)  # 确定性遍历顺序
        pool = self._state.setdefault(key, {})
        for i in ids:
            if i not in pool:
                pool[i] = [max(1, weights[i]), 0]
            # 权重配置可能变更，同步有效权重
            pool[i][0] = max(1, weights[i])

        total = sum(pool[i][0] for i in ids)
        chosen = ids[0]
        for i in ids:
            pool[i][1] += pool[i][0]
            if pool[i][1] > pool[chosen][1]:
                chosen = i
        pool[chosen][1] -= total
        # 清理已不在候选中的端点
        for stale in [i for i in pool if i not in ids]:
            del pool[stale]
        return chosen
