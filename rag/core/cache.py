"""进程内 TTL 缓存：给「读多写少 + 能容忍 N 秒陈旧」的热路径用。

为什么手写而不是引依赖：本项目零外部服务依赖是产品定位的一部分
（DESIGN §2「单进程、单端口、单数据目录」），为一个 30 行的 LRU
引 cachetools 不值得，而且这里要的行为很具体：

- **线程安全**：热路径全在 `asyncio.to_thread` 的工作线程里跑，
  裸 dict 在并发下会丢更新、`len()` 上界也会被突破（本仓库原来就有一份
  `Embedder._one_cache` 是裸 dict——见 docs/PERF-CONCURRENCY-2026-10-05.md R9）；
- **过期时间存在值里**，不做后台清理（条目有上界，满了丢最旧的）；
- **写路径能主动失效**：缓存只省去「查」，绝不省去「判定」。

关于一致性的诚实说明：这是**每进程**一份缓存。多 worker 部署下，
一次写操作只能失效本进程；其它进程要等 TTL 到点。因此
只把这里用在「观测数据」与「可容忍秒级陈旧的鉴权读」上，
并且相应地在文档里写明边界（见 PERF-FINAL-DECISION §5）。
"""
from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import orjson

from rag.core.ports import CacheBackend

logger = logging.getLogger("raggi.cache")


class _KeyedLocks:
    """按 key 的单飞锁，**用完即回收**。

    改前是一个只增不删的 dict：`embed_one` 以查询文本为键，长跑进程里
    每来一个新查询就永久留一把锁（审计里记过这条泄漏）。这里给每个 key
    记引用计数，最后一个使用者离开时把锁删掉——没有持有者时就不可能有
    同一 key 的并发加载，下次重建锁是安全的。
    """

    def __init__(self) -> None:
        self._locks: dict[Any, list] = {}
        self._guard = threading.Lock()

    @contextmanager
    def hold(self, key) -> Iterator[None]:
        with self._guard:
            entry = self._locks.get(key)
            if entry is None:
                entry = [threading.Lock(), 0]
                self._locks[key] = entry
            entry[1] += 1
        lock = entry[0]
        with lock:
            try:
                yield
            finally:
                with self._guard:
                    entry[1] -= 1
                    if entry[1] <= 0:
                        self._locks.pop(key, None)

    def __len__(self) -> int:  # pragma: no cover - 只给测试与排障看
        with self._guard:
            return len(self._locks)


class TTLCache:
    """带上界的线程安全 TTL 缓存。

    条目存的是 `(写入时刻, 该项自己的 ttl, 值)` 而不是 `截止时刻`：
    后者让 `age_ms()` 只能用**缓存的默认 ttl** 反推年龄，于是任何
    `set(..., ttl=自定义)` 的项都会报出错误的年龄（`/api/health` 的
    「这是 N 秒前的数据」正是拿这个给用户的，报错方向还不安全）。
    """

    def __init__(self, max_items: int = 512, ttl_seconds: float = 5.0):
        self.max_items = max(1, int(max_items))
        self.ttl = float(ttl_seconds)
        self._data: OrderedDict[Any, tuple[float, float, Any]] = OrderedDict()
        self._lock = threading.Lock()
        # 单飞锁：同一 key 的并发 miss 只让一个线程算，其余等待后直接
        # 命中缓存。健康/统计这类「一次计算几秒」的快照，没有它时
        # N 个并发冷请求会同时跑 N 次全表扫描（docstring 早就说
        # 「并发合流」，实现却只是 get→compute→set）。
        self._load_locks = _KeyedLocks()

    def get_entry(self, key) -> tuple[Any, float] | None:
        """命中时返回 (值, 年龄毫秒)，否则 None。"""
        now = time.monotonic()
        with self._lock:
            hit = self._live(key, now)
            if hit is None:
                return None
            return hit[2], (now - hit[0]) * 1000.0

    def get(self, key) -> Any | None:
        """命中且未过期返回值，否则 None。

        注意：**不能用「返回 None」区分「没有」与「存了个 None」**。
        需要区分的地方用 `cached(key, loader)`。
        """
        entry = self.get_entry(key)
        return entry[0] if entry is not None else None

    def age_ms(self, key) -> float | None:
        """命中项已经存在多久（毫秒）；未命中或已过期返回 None。

        给「快照类」端点用：响应里必须能诚实说明「这是几分钟前的数据」，
        否则调用方会把它当实时值判断（`/api/health` 就靠这个决定要不要
        告诉用户「正在重新对账」）。
        """
        now = time.monotonic()
        with self._lock:
            hit = self._live(key, now)
            if hit is None:
                return None
            return (now - hit[0]) * 1000

    def set(self, key, value, ttl: float | None = None) -> None:
        created = time.monotonic()
        with self._lock:
            self._data[key] = (
                created, self.ttl if ttl is None else float(ttl), value)
            self._data.move_to_end(key)
            while len(self._data) > self.max_items:
                self._data.popitem(last=False)

    def invalidate(self, key) -> None:
        with self._lock:
            self._data.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def get_or_load(self, key, loader: Callable[[], Any],
                    ttl: float | None = None) -> tuple[Any, float, bool]:
        """单飞加载：返回 ``(值, 年龄毫秒, 是否本次计算)``。

        同一 key 的并发未命中只有一个线程执行 loader，其余线程在
        per-key 锁上等待，随后直接读到刚写入的值。不同 key 互不阻塞。

        `loader` 在主锁外执行（慢操作不能串行化全局）；单飞锁只负责
        「同一个 key 不重复算」。loader 抛异常时锁自然释放，等待者会
        依次重试——错误不会被缓存成「健康」。
        """
        entry = self.get_entry(key)
        if entry is not None:
            return entry[0], entry[1], False

        with self._load_locks.hold(key):
            # 双重检查：等锁期间别的线程可能已经算完
            entry = self.get_entry(key)
            if entry is not None:
                return entry[0], entry[1], False
            value = loader()
            self.set(key, value, ttl=ttl)
            return value, 0.0, True

    def cached(self, key, loader: Callable[[], Any],
               ttl: float | None = None) -> Any:
        """取值，未命中时用 loader 计算并写入（并发合流见 get_or_load）。"""
        value, _age, _loaded = self.get_or_load(key, loader, ttl=ttl)
        return value

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def _live(self, key, now: float):
        """取未过期项，过期就地删。调用方必须已持锁。"""
        item = self._data.get(key)
        if item is None:
            return None
        if item[0] + item[1] <= now:
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return item


class LayeredCache:
    """L1（进程内，单飞）+ 可选 L2（Redis）缓存。

    没有配置 `redis.url` 时 `l2 is None`，行为与 `TTLCache` 完全一致。
    L2 的读失败按 miss 处理、写失败只记一次日志：共享缓存是加速件，
    不能变成可用性依赖——Redis 挂了，检索要退化到"慢但正确"，
    而不是直接 500。
    """

    def __init__(self, l1: TTLCache, l2: CacheBackend | None = None):
        self._l1 = l1
        self._l2 = l2
        self._locks = _KeyedLocks()
        self._l2_warned = False

    @property
    def has_l2(self) -> bool:
        return self._l2 is not None

    def get_entry(self, key) -> tuple[Any, float] | None:
        return self._l1.get_entry(key)

    def get(self, key) -> Any | None:
        entry = self.get_entry(key)
        return entry[0] if entry is not None else None

    def set(self, key, value, ttl: float | None = None) -> None:
        self._l1.set(key, value, ttl=ttl)
        self._l2_set(key, value, ttl)

    def invalidate(self, key) -> None:
        self._l1.invalidate(key)
        if self._l2 is not None:
            try:
                self._l2.delete(_l2_key(key))
            except Exception as e:  # noqa: BLE001
                self._warn_l2("删除", e)

    def get_or_load(self, key, loader: Callable[[], Any],
                    ttl: float | None = None) -> tuple[Any, float, bool]:
        entry = self._l1.get_entry(key)
        if entry is not None:
            return entry[0], entry[1], False
        with self._locks.hold(key):
            entry = self._l1.get_entry(key)
            if entry is not None:
                return entry[0], entry[1], False
            cached = self._l2_get(key)
            if cached is not None:
                self._l1.set(key, cached, ttl=ttl)
                return cached, 0.0, False
            value = loader()
            self._l1.set(key, value, ttl=ttl)
            self._l2_set(key, value, ttl)
            return value, 0.0, True

    # ---- L2 ----------------------------------------------------------

    def _l2_get(self, key) -> Any | None:
        if self._l2 is None:
            return None
        try:
            raw = self._l2.get(_l2_key(key))
        except Exception as e:  # noqa: BLE001
            self._warn_l2("读取", e)
            return None
        if raw is None:
            return None
        try:
            return orjson.loads(raw)
        except Exception as e:  # noqa: BLE001
            # 脏值/版本不兼容：当作未命中，下一次会覆盖写
            self._warn_l2("反序列化", e)
            return None

    def _l2_set(self, key, value, ttl: float | None) -> None:
        if self._l2 is None:
            return
        try:
            self._l2.set(_l2_key(key), orjson.dumps(value),
                         self._l1.ttl if ttl is None else float(ttl))
        except Exception as e:  # noqa: BLE001
            self._warn_l2("写入", e)

    def _warn_l2(self, action: str, exc: Exception) -> None:
        if self._l2_warned:
            return
        self._l2_warned = True
        logger.warning("共享缓存%s失败，本进程退化为只用本地缓存: %s", action, exc)


def _l2_key(key) -> str:
    """L2 的键：哈希后定长，避免把整段查询文本塞进 Redis 键。"""
    import hashlib

    return hashlib.sha256(
        str(key).encode("utf-8", "replace")).hexdigest()


_SHARED_CACHES: dict[tuple, LayeredCache] = {}
_SHARED_GUARD = threading.Lock()


def shared_cache(settings) -> LayeredCache:
    """按配置取**进程级**共享缓存（L2 可选）。

    模型热切换会重建 Embedder，缓存实例必须跨重建复用，否则每次改配置
    都会把热度清零。键里带上 url / 容量 / TTL，测试与多套配置互不串。
    """
    redis_cfg = settings.redis
    cache_cfg = settings.cache
    sig = (redis_cfg.url, redis_cfg.namespace,
           int(cache_cfg.max_items), float(cache_cfg.ttl_seconds))
    with _SHARED_GUARD:
        hit = _SHARED_CACHES.get(sig)
        if hit is not None:
            return hit
        l1 = TTLCache(max_items=cache_cfg.max_items,
                      ttl_seconds=cache_cfg.ttl_seconds)
        l2: CacheBackend | None = None
        if redis_cfg.url:
            from rag.core.redis import RedisCache

            l2 = RedisCache(
                redis_cfg.url,
                namespace=f"{redis_cfg.namespace}:cache",
                timeout_ms=redis_cfg.timeout_ms,
                max_connections=redis_cfg.max_connections)
        cache = LayeredCache(l1, l2)
        _SHARED_CACHES[sig] = cache
        return cache


def reset_shared_caches() -> None:
    """测试用：清掉进程级单例（真实运行不需要）。"""
    with _SHARED_GUARD:
        _SHARED_CACHES.clear()
