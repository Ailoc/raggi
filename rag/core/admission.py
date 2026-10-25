"""在飞请求准入控制（进程级）。

为什么需要：应用层的排队没有上限。上游一个重试风暴打进来，事件循环
会把它们全接住，每个都占着连接、线程池额度与内存，最后**一起超时**；
p95 与错误率同时崩，而且看不出是谁的锅。

准入控制把「无限排队」换成「快速拒绝」：超过 `api_max_inflight` 时
直接 503 + Retry-After，让客户端错峰。它保护的是进程本身，不替代
具体的资源闸门（embedding 并发、入库队列）。

配置 `api_max_inflight = 0`（默认）＝ 关闭，单机自用行为不变。
"""
from __future__ import annotations

import threading


class InflightGate:
    """线程安全的在飞计数器；limit<=0 表示不限制。"""

    def __init__(self, limit: int = 0):
        self.limit = max(0, int(limit))
        self._count = 0
        self._lock = threading.Lock()
        self._peak = 0

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def enter(self) -> bool:
        """尝试占一个槽位；超限返回 False（调用方负责回 503）。"""
        if self.limit <= 0:
            return True
        with self._lock:
            if self._count >= self.limit:
                return False
            self._count += 1
            self._peak = max(self._peak, self._count)
            return True

    def leave(self) -> None:
        if self.limit <= 0:
            return
        with self._lock:
            if self._count > 0:
                self._count -= 1

    def snapshot(self) -> dict:
        with self._lock:
            return {"inflight": self._count, "max_inflight": self.limit,
                    "peak_inflight": self._peak}
