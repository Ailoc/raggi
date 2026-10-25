"""Redis / Valkey 接入（共享缓存；限流脚本在 ratelimit 里）。

`redis` 是**可选依赖**：只有配置了 `redis.url` 才会 import 它，
单机形态不需要装、也不会因为没装而起不来。连接是惰性的，
连不上时抛 `RedisUnavailable`，由调用方决定降级方式。

为什么用同步客户端：仓库里的读路径几乎都在 `asyncio.to_thread` 的
工作线程里跑（查库、检索、embedding），共享缓存在这些线程里同步调用
最省事；没有引入 async 客户端，避免同一份缓存出现两套调用约定。
"""
from __future__ import annotations

import logging

from rag.core.ports import CacheBackend

logger = logging.getLogger("raggi.redis")


class RedisUnavailable(RuntimeError):
    """Redis 不可用（没装依赖、连不上、超时、命令失败）。"""


class RedisClient:
    """最小 Redis 客户端：命名空间 + 超时 + 统一异常。"""

    def __init__(self, url: str, *, namespace: str = "raggi",
                 timeout_ms: int = 300, max_connections: int = 64):
        if not url:
            raise RedisUnavailable("redis.url 为空")
        try:
            import redis  # noqa: F401  可选依赖
        except ImportError as e:  # pragma: no cover - 取决于安装形态
            raise RedisUnavailable(
                "未安装 redis 依赖（pip install 'raggi[scale]'）") from e
        self._lib = redis
        self.namespace = namespace
        timeout = max(0.05, timeout_ms / 1000.0)
        self._pool = redis.ConnectionPool.from_url(
            url,
            socket_timeout=timeout,
            socket_connect_timeout=timeout,
            health_check_interval=30,
            max_connections=max(1, int(max_connections)),
            decode_responses=False,
        )
        self._client = redis.Redis(connection_pool=self._pool)

    def key(self, name: str) -> str:
        return f"{self.namespace}:{name}"

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except self._lib.RedisError as e:
            raise RedisUnavailable(str(e)) from e

    def get(self, key: str) -> bytes | None:
        return self._call(self._client.get, self.key(key))

    def psetex(self, key: str, ttl_seconds: float, value: bytes) -> None:
        ttl_ms = max(1, int(float(ttl_seconds) * 1000))
        self._call(self._client.psetex, self.key(key), ttl_ms, value)

    def delete(self, key: str) -> None:
        self._call(self._client.delete, self.key(key))

    def eval(self, script: str, keys: list[str], args: list):
        full = [self.key(k) for k in keys]
        return self._call(self._client.eval, script, len(full), *full, *args)

    def exists(self, key: str) -> bool:
        return bool(self._call(self._client.exists, self.key(key)))

    def psetex_prefixed(self, key: str, ttl_ms: int, value: bytes) -> None:
        """按毫秒 TTL 写一个已经带命名空间的键（取消标记用）。"""
        self._call(self._client.psetex, key, max(1, int(ttl_ms)), value)

    # ---- Streams ------------------------------------------------------
    #
    # 不用「通用命令名」那一层：redis-py 的方法签名与 Redis 命令名并不
    # 一一对应（XGROUP 是 xgroup_create、XREADGROUP 参数顺序也不同），
    # 泛型转发在第一次调用时才炸。这里逐个显式包一层，返回结构与
    # redis-py 保持一致，队里只处理列表/字典。

    def xadd(self, stream: str, fields: dict):
        return self._call(self._client.xadd, self.key(stream), fields)

    def xlen(self, stream: str) -> int:
        return int(self._call(self._client.xlen, self.key(stream)))

    def xgroup_create(self, stream: str, group: str) -> None:
        try:
            self._call(self._client.xgroup_create,
                       self.key(stream), group, id="0", mkstream=True)
        except Exception as e:  # noqa: BLE001
            if "BUSYGROUP" not in str(e):
                raise

    def xreadgroup(self, stream: str, group: str, consumer: str,
                   *, count: int = 1, block_ms: int = 2000):
        return self._call(
            self._client.xreadgroup, group, consumer,
            {self.key(stream): ">"}, count=count, block=block_ms)

    def xack(self, stream: str, group: str, entry_id) -> None:
        self._call(self._client.xack, self.key(stream), group, entry_id)

    def xpending(self, stream: str, group: str):
        return self._call(self._client.xpending, self.key(stream), group)

    def xinfo_groups(self, stream: str):
        return self._call(self._client.xinfo_groups, self.key(stream))

    def xtrim(self, stream: str, maxlen: int) -> int:
        return int(self._call(
            self._client.xtrim, self.key(stream), maxlen=maxlen,
            approximate=True))

    def xautoclaim(self, stream: str, group: str, consumer: str,
                   min_idle_ms: int, start: str = "0-0", count: int = 5):
        return self._call(
            self._client.xautoclaim, self.key(stream), group, consumer,
            min_idle_ms, start, count=count)

    def ping(self) -> bool:
        return bool(self._call(self._client.ping))

    def close(self) -> None:  # pragma: no cover - 进程退出/测试清理用
        try:
            self._pool.disconnect()
        except Exception:  # noqa: BLE001
            pass


class RedisCache(CacheBackend):
    """共享缓存后端：字节值 + TTL。失败一律抛 `RedisUnavailable`。"""

    def __init__(self, url: str, *, namespace: str = "raggi:cache",
                 timeout_ms: int = 300, max_connections: int = 64):
        self._client = RedisClient(
            url, namespace=namespace, timeout_ms=timeout_ms,
            max_connections=max_connections)

    def get(self, key: str) -> bytes | None:
        return self._client.get(key)

    def set(self, key: str, value: bytes, ttl_seconds: float) -> None:
        self._client.psetex(key, ttl_seconds, value)

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def ping(self) -> bool:
        return self._client.ping()
