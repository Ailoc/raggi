"""分层缓存：L1 进程内 + L2 共享（可选），单飞与降级。

目标形态是多副本共享同一份查询向量缓存；默认（没有 redis.url）必须
与改造前的进程内缓存**行为一致**——这是"默认行为不变"的硬约束。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from rag.core.cache import LayeredCache, TTLCache, reset_shared_caches, shared_cache
from rag.core.config import Settings


class FakeL2:
    """最小共享缓存实现（同时也用来模拟故障）。"""

    def __init__(self, fail: bool = False):
        self.data: dict[str, bytes] = {}
        self.fail = fail
        self.gets = 0
        self.sets = 0

    def get(self, key):
        self.gets += 1
        if self.fail:
            raise RuntimeError("redis down")
        return self.data.get(key)

    def set(self, key, value, ttl_seconds):
        if self.fail:
            raise RuntimeError("redis down")
        self.sets += 1
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


def test_l1_hit_does_not_touch_l2_or_loader():
    l2 = FakeL2()
    cache = LayeredCache(TTLCache(max_items=8, ttl_seconds=60), l2)
    calls = []

    def loader():
        calls.append(1)
        return [1.0, 2.0]

    assert cache.get_or_load("k", loader) == ([1.0, 2.0], 0.0, True)
    value, age_ms, loaded = cache.get_or_load("k", loader)
    assert value == [1.0, 2.0] and loaded is False and age_ms >= 0
    assert len(calls) == 1
    # 首次冷启动查过一次 L2（miss），第二次是纯 L1 命中，不该再查
    assert l2.gets == 1, "L1 已命中还去问 L2"
    assert l2.sets == 1, "写入时应回填 L2"


def test_l2_hit_populates_l1_other_process_shape():
    """模拟另一个副本算过：新进程 L1 空，但 L2 有值。"""
    l2 = FakeL2()
    a = LayeredCache(TTLCache(max_items=8, ttl_seconds=60), l2)
    a.get_or_load("q", lambda: [0.5, 0.25], ttl=60)

    b = LayeredCache(TTLCache(max_items=8, ttl_seconds=60), l2)
    loaded = b.get_or_load("q", lambda: (_ for _ in ()).throw(AssertionError("不该回源")))
    assert loaded[0] == [0.5, 0.25]
    assert loaded[2] is False
    assert b.get("q") == [0.5, 0.25], "L2 命中后应回填 L1"


def test_l2_failure_degrades_to_loader():
    l2 = FakeL2(fail=True)
    cache = LayeredCache(TTLCache(max_items=8, ttl_seconds=60), l2)
    value, _age, loaded = cache.get_or_load("q", lambda: [1.0])
    assert value == [1.0] and loaded is True
    # 第二次仍可从 L1 命中，不受 L2 故障影响
    assert cache.get_or_load("q", lambda: [9.9])[0] == [1.0]


def test_missing_l2_is_exactly_the_old_behavior():
    cache = LayeredCache(TTLCache(max_items=8, ttl_seconds=60))
    assert cache.has_l2 is False
    assert cache.get_or_load("q", lambda: {"v": 1})[0] == {"v": 1}
    assert cache.get("q") == {"v": 1}


def test_single_flight_collapses_concurrent_misses():
    cache = LayeredCache(TTLCache(max_items=32, ttl_seconds=60), FakeL2())
    calls = []
    gate = threading.Event()

    def loader():
        calls.append(1)
        time.sleep(0.05)
        gate.set()
        return [42.0]

    with ThreadPoolExecutor(max_workers=16) as ex:
        results = list(ex.map(lambda _: cache.get_or_load("same", loader)[0], range(16)))
    assert results == [[42.0]] * 16
    assert len(calls) == 1, f"同一 key 回源了 {len(calls)} 次"


def test_per_key_locks_are_released():
    """单飞锁用完即回收：改前每个新查询永久留一把锁。"""
    cache = LayeredCache(TTLCache(max_items=8, ttl_seconds=60), FakeL2())
    for i in range(200):
        cache.get_or_load(f"q{i}", lambda i=i: [float(i)])

    assert len(cache._locks) == 0, "分层缓存的单飞锁泄漏了"
    l1 = TTLCache(max_items=8, ttl_seconds=60)
    for i in range(200):
        l1.get_or_load(f"k{i}", lambda i=i: i)
    assert len(l1._load_locks) == 0, "TTLCache 的单飞锁泄漏了"


def test_shared_cache_without_redis_has_no_l2():
    reset_shared_caches()
    settings = Settings()
    settings.redis.url = ""
    cache = shared_cache(settings)
    assert cache.has_l2 is False
    # 同一个进程、同一份配置只建一次（模型热切换不能把缓存清零）
    assert shared_cache(settings) is cache
    reset_shared_caches()


def test_default_settings_keep_redis_off():
    settings = Settings()
    assert settings.redis.url == ""
    assert settings.cache.max_items == 512
    assert settings.rate_limit_backend == "auto"
