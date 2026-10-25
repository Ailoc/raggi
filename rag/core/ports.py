"""可插拔后端的协议定义（缓存 / 限流 / 队列）。

这些协议只描述**跨进程**需要的能力，不绑定具体实现：单机默认仍是
进程内实现，配置了 Redis/Postgres 后再替换。把接口放在这里而不是
各实现模块里，是为了让「哪些能力可以被替换」有一份可读的清单——
10k 在飞请求的改造顺序就是按这份清单逐项替换。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CacheBackend(Protocol):
    """跨进程共享缓存的最小面（字节值 + TTL）。

    只放三个操作：读取、带 TTL 写入、删除。单飞、序列化、降级策略都在
    调用方（`LayeredCache`）里，后端实现不需要知道缓存里存的是什么。
    """

    def get(self, key: str) -> bytes | None:
        """命中返回值，未命中返回 None；后端故障时抛异常。"""
        ...

    def set(self, key: str, value: bytes, ttl_seconds: float) -> None:
        """带过期时间写入；失败时抛异常，由调用方决定是否吞掉。"""
        ...

    def delete(self, key: str) -> None:
        """删除一个键（不存在不算错误）。"""
        ...


@runtime_checkable
class LimiterBackend(Protocol):
    """跨进程限流的最小面：按调用方键取一个令牌。"""

    @property
    def enabled(self) -> bool: ...

    def allow(self, key: str) -> tuple[bool, int]:
        """返回 (是否放行, 建议等待秒数)。"""
        ...
