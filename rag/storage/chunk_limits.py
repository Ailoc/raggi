"""分块尺寸的限幅与派生的重叠字符数 —— **一个不 import 任何东西的叶子**。

为什么单独一个模块（而不是留在 `plan.py` 里）：`kbs.py` 写知识库方案时要限幅，
`plan.py` 解析生效方案时也要限幅，而 `plan.py` 反过来依赖 `repos`（含 `kbs`）。
两边都从 `plan` 拿就把 `repos/kbs → plan → repos` 变成真环 ——
原先靠 `kbs.py` 里两处函数内 `from ..plan import clamp_size` 掩盖着。
纯数值约束没有资格决定依赖方向，所以它下沉成叶子，两个方向都可以指它。

这里刻意**不**做「`plan` 再导出一遍」：同一件事有两个导入路径，
就又回到本仓反复在防的那种状态（形状一致但没有强制，改一处漏一处）。
"""
from __future__ import annotations

MAX_OVERLAP_RATIO = 50.0
MIN_CHUNK_SIZE = 64
MAX_CHUNK_SIZE = 8192


def clamp_size(value) -> int:
    """分块大小限幅（0 = 继承）。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    if n <= 0:
        return 0
    return max(MIN_CHUNK_SIZE, min(MAX_CHUNK_SIZE, n))


def clamp_ratio(value) -> float:
    """重叠比例限幅 0-50%（超过 50% 收益递减且块数翻倍）。"""
    try:
        r = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(MAX_OVERLAP_RATIO, r))


def overlap_chars(chunk_size: int, ratio: float) -> int:
    """百分比 → 实际重叠字符数，且恒小于 chunk_size（RCT 要求）。"""
    size = max(1, int(chunk_size))
    return max(0, min(size // 2, round(size * clamp_ratio(ratio) / 100.0)))
