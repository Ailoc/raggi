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

import threading
import time
from collections import OrderedDict
from typing import Any, Callable


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

    def get(self, key) -> Any | None:
        """命中且未过期返回值，否则 None。

        注意：**不能用「返回 None」区分「没有」与「存了个 None」**。
        需要区分的地方用 `cached(key, loader)`。
        """
        now = time.monotonic()
        with self._lock:
            hit = self._live(key, now)
            if hit is not None:
                return hit[2]
            return None

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

    def cached(self, key, loader: Callable[[], Any],
               ttl: float | None = None) -> Any:
        """取值，未命中时用 loader 计算并写入。

        `loader` 在锁**外**执行：这些 loader 全是查库/查磁盘的慢操作，
        持锁跑会把所有并发请求串起来——那正是我们要治的病。
        代价是并发未命中时可能重复计算几次（可接受，无副作用）。

        与 `get` 不同，这里能区分「存了 None」：None 也是有效值，
        计算过就不会再算一遍（在 TTL 内）。
        """
        now = time.monotonic()
        with self._lock:
            hit = self._live(key, now)
            if hit is not None:
                return hit[2]
        value = loader()
        self.set(key, value, ttl=ttl)
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
