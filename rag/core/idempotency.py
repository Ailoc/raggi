"""幂等键：重试同一个请求不会产生第二份数据。

**为什么需要**：网络超时后客户端无法区分「服务端没收到」与「收到了但
响应丢了」，最常见的应对是重试——而对入库这类非幂等的写操作，重试就会
产生第二份文档。带 `Idempotency-Key` 的重试会返回**第一次的结果**。

**边界（要如实说明）**：
- 内容哈希去重已经挡住了「同一内容重复入库」，但那是**事后**发现并返回
  `status=duplicate`，调用方拿不到「这是我第一次提交的结果」这个确定性；
- 幂等键解决的是**过程**幂等：同一个键的第二次请求根本不进流水线。
- 记录有 TTL（默认 24 小时），过期后同一键会被当成新请求——这是有意
  的取舍：永久保留会让键空间无界增长，而实际重试都发生在分钟级。

**进程内实现**：单机单进程，符合本项目的部署形态。多进程部署时每个
worker 各有一份，跨进程的重复提交防不住——真需要时换成共享存储。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

HEADER = "Idempotency-Key"
DEFAULT_TTL_SECONDS = 24 * 3600
# 键长度上限：键会落进日志，限制长度避免被用来撑爆内存/日志
MAX_KEY_LEN = 200
# 同一键最多记多少条结果（同一键不该产生多次成功，防的是键被复用）
MAX_ENTRIES = 1000

# 与 requestid 同样的理由：只收短且安全的标识
_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


@dataclass
class _Entry:
    result: dict
    created: float


@dataclass
class IdempotencyStore:
    ttl: float = DEFAULT_TTL_SECONDS
    _entries: dict[str, _Entry] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, key: str, path: str) -> dict | None:
        """取出该键此前的结果；没有或已过期则返回 None。

        键按 (path, key) 作用域：同一个键用在不同端点上互不干扰，
        否则「先建库再入库」共用一个键会拿到完全无关的结果。
        """
        now = time.time()
        k = f"{path}\n{key}"
        with self._lock:
            e = self._entries.get(k)
            if e is None:
                return None
            if now - e.created > self.ttl:
                del self._entries[k]
                return None
            return dict(e.result)

    def put(self, key: str, path: str, result: dict) -> None:
        """记录该键的结果，供后续同键重试返回。"""
        k = f"{path}\n{key}"
        with self._lock:
            self._gc(time.time())
            self._entries[k] = _Entry(result=dict(result),
                                     created=time.time())

    def _gc(self, now: float) -> None:
        """清掉过期条目，并防止键空间无界增长。"""
        dead = [k for k, e in self._entries.items()
                if now - e.created > self.ttl]
        for k in dead:
            del self._entries[k]
        # 全部未过期但数量超上限时，丢最早的一半——
        # 重试窗口本来就很短，丢掉老条目不影响正确性
        if len(self._entries) > MAX_ENTRIES:
            ordered = sorted(self._entries.items(), key=lambda kv: kv[1].created)
            for k, _ in ordered[: len(ordered) // 2]:
                del self._entries[k]

    def reset(self) -> None:
        with self._lock:
            self._entries.clear()


def normalize_key(request) -> str | None:
    """取入站的 Idempotency-Key；缺失或不安全则返回 None（不启用幂等）。"""
    raw = request.headers.get(HEADER) or request.headers.get(
        "idempotency-key")
    if not raw:
        return None
    key = raw.strip()
    if not key or len(key) > MAX_KEY_LEN or not set(key) <= _ALLOWED:
        return None
    return key