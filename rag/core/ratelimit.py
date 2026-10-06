"""限流：令牌桶（按调用方维度）。

**为什么限写不限读**：检索是读多写少、单次代价低，挡它只会影响正常使用；
而入库/编辑/删除才是真正吃资源、可能被滥用打爆入库队列的路径。

**为什么按调用方而不是全局**：单机自用时全局桶会让一个误写的脚本把
所有人的配额吃掉；按 key_id 分桶则互不影响。

**为什么令牌桶而不是固定窗口**：固定窗口在窗口边界会放行 2 倍流量
（打满当前窗口、立刻再打满下一个），令牌桶用「匀速补充 + 允许突发」
平滑掉这个尖峰，也天然给出 Retry-After。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

# 幂等与无害的写：不消耗配额。
# 停用/启用分块同理——它只是翻一个布尔列，不触发重新向量化。
# 检索也是：POST /search 是**读**语义（只是不接受 body 而已），限它会
# 直接影响正常使用。
# 注意写成裸路径（不含版本前缀）：调用方传入的 path 含 /api/v1，
# 这里统一按「去掉前缀后的路径」比对。
_IDEMPOTENT_PATHS = (
    "/documents/text",
    "/documents/url",
    "/search",
)


@dataclass
class _Bucket:
    tokens: float
    updated: float


@dataclass
class RateLimiter:
    """按调用方的令牌桶。

    limit<=0 表示关闭（单机自用的默认），此时所有方法都是空操作——
    不给默认配置强加一个「某天突然开始报 429」的惊喜。
    """

    limit: int = 0
    burst: int = 0
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if self.burst <= 0:
            self.burst = max(1, self.limit)

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def allow(self, key: str) -> tuple[bool, int]:
        """取一个令牌。返回 (是否放行, 建议等待秒数)。

        Retry-After 用「补一个令牌要多久」计算，这是保证速率的最小等待
        时间——比它短就一定会再被拒。
        """
        if not self.enabled:
            return True, 0
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = _Bucket(tokens=float(self.burst), updated=now)
                self._buckets[key] = b
                self._gc(now)
            else:
                # 按经过时间补充，上限为 burst
                b.tokens = min(float(self.burst),
                                b.tokens + (now - b.updated) * self.limit / 60.0)
                b.updated = now
            if b.tokens >= 1.0:
                b.tokens -= 1.0
                return True, 0
            # 还差多少令牌，以及补一个令牌要多久
            need = 1.0 - b.tokens
            retry = int(need * 60.0 / self.limit) + 1
            return False, max(1, retry)

    def _gc(self, now: float) -> None:
        """清掉长期不用的桶，避免 key 无限增长。

        以「桶已补满且久未使用」为标准：这样的桶与新建的等价，
        留着没有意义；正在被限制的桶一定更近，不会被误删。
        """
        stale = self.burst / max(1, self.limit / 60.0) * 2 + 60
        cutoff = now - stale
        dead = [k for k, b in self._buckets.items()
                if b.updated < cutoff and b.tokens >= self.burst]
        for k in dead:
            del self._buckets[k]

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


def caller_key(request, key_row: dict | None) -> str:
    """限流维度：按有效密钥分，未鉴权时按客户端 IP。

    未鉴权场景（本机自用）按 IP 足够；而把所有未鉴权请求归到一个桶会
    让并发页面加载互相影响。
    """
    if key_row and key_row.get("key_id"):
        return f"key:{key_row['key_id']}"
    client = request.client
    return f"ip:{client.host}" if client else "ip:unknown"


def normalize_path(path: str) -> str:
    """去掉 /api 或 /api/v1 前缀，统一带上开头的斜杠。

    豁免名单因此不必关心版本：加了 /api/v2 也不会让豁免失效。
    """
    for prefix in ("/api/v1/", "/api/"):
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    return "/" + path.lstrip("/")


def is_limited_method(request) -> bool:
    """只有写方法参与限流。"""
    return request.method in ("POST", "PUT", "PATCH", "DELETE")


def is_idempotent(path: str) -> bool:
    """路径是否在幂等豁免名单里。

    内容哈希去重让「同一段文本重复入库」本来就安全，挡它只会让
    脚本作者困惑：明明没入库成功，却被告知「太频繁」。
    """
    rel = normalize_path(path)
    return any(rel == p or rel.startswith(p + "?") for p in _IDEMPOTENT_PATHS)