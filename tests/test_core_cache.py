"""进程内缓存的并发语义。"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

from rag.core.cache import TTLCache


def test_get_or_load_single_flights_concurrent_misses():
    """同一 key 的并发 miss 只能真正计算一次。

    回归防护：健康/统计快照是「一次计算数秒」的重活，原来的
    get→compute→set 让 N 个并发冷请求同时跑 N 次全表扫描；
    docstring 写着「并发合流」，实现却没有。
    """
    cache = TTLCache(max_items=8, ttl_seconds=60)
    calls = 0
    calls_lock = threading.Lock()
    ready = threading.Barrier(8)

    def loader():
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.05)
        return "v"

    def worker(_):
        ready.wait()
        return cache.get_or_load("k", loader)[0]

    with ThreadPoolExecutor(max_workers=8) as ex:
        values = list(ex.map(worker, range(8)))
    assert values == ["v"] * 8
    assert calls == 1, f"并发 miss 计算了 {calls} 次"


def test_get_or_load_reports_age_and_hit():
    cache = TTLCache(max_items=8, ttl_seconds=60)
    value, age, loaded = cache.get_or_load("k", lambda: 1)
    assert (value, age, loaded) == (1, 0.0, True)
    value, age, loaded = cache.get_or_load("k", lambda: 2)
    assert value == 1 and age >= 0 and loaded is False


def test_different_keys_do_not_block_each_other():
    cache = TTLCache(max_items=8, ttl_seconds=60)
    started = threading.Event()

    def slow():
        started.set()
        time.sleep(0.2)
        return "slow"

    with ThreadPoolExecutor(max_workers=2) as ex:
        f = ex.submit(cache.get_or_load, "slow", slow)
        started.wait(1)
        t0 = time.monotonic()
        assert cache.get_or_load("fast", lambda: "fast")[0] == "fast"
        assert time.monotonic() - t0 < 0.1
        f.result()
