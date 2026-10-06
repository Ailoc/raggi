"""闭环压测客户端。

**为什么每个线程各自建 client**：`httpx.Client` 不是线程安全的。
本仓库曾把「并发吞吐上不去」归因于 GIL、又归因于 LanceDB 的事件循环，
两次都错——真正原因是压测工具本身多线程共享一个 client
（DESIGN §12.1 有记录：换成每线程独立 client 后 5.4 → 9.5 req/s）。
这条纪律写进代码，别再靠口口相传。
"""
from __future__ import annotations

import statistics
import threading
import time
from dataclasses import dataclass, field

import httpx


@dataclass
class Sample:
    """一次并发档位的结果。"""
    path: str
    concurrency: int
    n: int = 0
    rps: float = 0.0
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    max_ms: float = 0.0
    errors: dict = field(default_factory=dict)

    def line(self) -> str:
        return (f"{self.path[:44]:46s} c={self.concurrency:3d} n={self.n:6d} "
                f"rps={self.rps:7.1f} p50={self.p50_ms:7.1f}ms "
                f"p95={self.p95_ms:7.1f}ms err={sum(self.errors.values())}")

    def as_dict(self) -> dict:
        return {"path": self.path, "concurrency": self.concurrency,
                "n": self.n, "rps": round(self.rps, 2),
                "p50_ms": round(self.p50_ms, 2),
                "p95_ms": round(self.p95_ms, 2),
                "errors": self.errors}


def closed_loop(base: str, path: str, *, concurrency: int = 1,
                duration: float = 5.0, method: str = "GET",
                json_body: dict | None = None,
                unique_each: bool = False,
                timeout: float = 120.0) -> Sample:
    """固定并发数闭环压测：每个线程串行发请求，测的是「服务端能吃下多少」。

    `unique_each=True` 时给每请求的 body 追加唯一串，避免内容哈希去重
    把并发入库压测变成「同一篇文档入库 N 次」——那测不出真实吞吐。
    """
    lat: list[float] = []
    errors: dict[str, int] = {}
    lock = threading.Lock()
    stop_at = time.time() + duration
    counter = itertools_count()

    def worker() -> None:
        client = httpx.Client(base_url=base, timeout=timeout)
        mine: list[float] = []
        while time.time() < stop_at:
            body = json_body
            if unique_each and isinstance(body, dict):
                body = dict(body)
                tag = f"uniq{next(counter)}"
                for k in ("text", "title", "q"):
                    if k in body:
                        body[k] = f"{body[k]} {tag}"
                        break
            t0 = time.perf_counter()
            try:
                r = (client.get(path) if method == "GET"
                     else client.request(method, path, json=body))
                dt = (time.perf_counter() - t0) * 1000
                if r.status_code >= 400:
                    key = f"{r.status_code}:{r.text[:60]}"
                    with lock:
                        errors[key] = errors.get(key, 0) + 1
                else:
                    mine.append(dt)
            except Exception as e:  # noqa: BLE001
                with lock:
                    k = type(e).__name__
                    errors[k] = errors.get(k, 0) + 1
        client.close()
        with lock:
            lat.extend(mine)

    ths = [threading.Thread(target=worker) for _ in range(concurrency)]
    started = time.time()
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    wall = max(0.001, time.time() - started)
    lat.sort()
    return Sample(
        path=path, concurrency=concurrency, n=len(lat),
        rps=len(lat) / wall,
        p50_ms=lat[len(lat) // 2] if lat else 0.0,
        p95_ms=lat[min(len(lat) - 1, int(len(lat) * 0.95))] if lat else 0.0,
        max_ms=lat[-1] if lat else 0.0,
        errors=dict(errors))


def itertools_count():
    """单调递增计数器（线程安全足够：只在持锁的 worker 内使用）。"""
    import itertools

    return itertools.count(1)


def median(samples: list[Sample]) -> float:
    vals = [s.p50_ms for s in samples if s.p50_ms]
    return statistics.median(vals) if vals else 0.0
