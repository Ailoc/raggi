"""模型服务的共享 HTTP 客户端与并发治理。

为什么要有这一层
----------------
改造前每个出网调用都自己开连接：

- `models/rerank.py` 用 `urllib.request.urlopen` —— **每次请求新建一条 TCP
  连接**（HTTPS 还要多付一次 TLS 握手），没有 keep-alive、没有连接上限；
- `registry.chat()` 每个请求 `ChatOpenAI(...)` 新建一次，连带新建它内部的
  httpx 连接池；
- embedding 走 LangChain 的客户端，实例本身是复用的，但并发闸门
  （`_EMBED_SEMAPHORE`）是硬编码的 8。

在并发下这三件事合起来的效果是：**每个请求都要走一遍握手**，
远端服务看到的是 N 倍于实际请求数的连接数，本地则在线程里等握手。

这里给出两样东西
----------------
1. 一个进程级共享的 `httpx.Client`（连接池 + keep-alive），线程安全；
2. 一个可配置的并发闸门与最小熔断，让「远端限流 → 重试 → 更限流」的
   级联风暴有出口。

诚实说明：`httpx.Client` 是**同步**客户端，因为这些调用发生在
`asyncio.to_thread` 的工作线程里（与改造前一致）。把它们改成 async
需要把 embedding/rerank 的全链路改成协程，那是另一次改造，
而且不会改变「连接复用」这个本质收益。
"""
from __future__ import annotations

import logging
import threading
import time

import httpx

logger = logging.getLogger("raggi.http")

# 连接池额度。max_connections 是「同时在飞」的上限，
# max_keepalive_connections 是空闲时留着的连接数（后者决定 keep-alive 命中率）。
DEFAULT_MAX_CONNECTIONS = 100
DEFAULT_MAX_KEEPALIVE = 20

_clients: dict[tuple, httpx.Client] = {}
_clients_lock = threading.Lock()


def get_client(*, timeout: float = 30.0, verify: bool = True) -> httpx.Client:
    """按 (timeout, verify) 取共享客户端。

    key 里带 timeout 是因为 httpx 的超时挂在客户端默认值上；
    不同 provider 的合理超时不同（embedding 60s、rerank 30s），
    为它们各留一个池，也比「一个池迁就最慢的那个」好。
    """
    key = (round(float(timeout), 3), bool(verify))
    with _clients_lock:
        c = _clients.get(key)
        if c is None:
            c = httpx.Client(
                timeout=httpx.Timeout(float(timeout), connect=min(
                    10.0, float(timeout))),
                verify=verify,
                limits=httpx.Limits(
                    max_connections=DEFAULT_MAX_CONNECTIONS,
                    max_keepalive_connections=DEFAULT_MAX_KEEPALIVE),
            )
            _clients[key] = c
        return c


def post_json(url: str, payload: dict, *, headers: dict | None = None,
              timeout: float = 30.0) -> dict:
    """POST JSON 并解析 JSON。异常直接抛给调用方（由各自的降级策略处理）。"""
    client = get_client(timeout=timeout)
    r = client.post(url, json=payload, headers=headers or {})
    r.raise_for_status()
    return r.json()


class CircuitBreaker:
    """连续失败 N 次后短路一段时间。

    只用于**可选增强**的调用（精排）：远端挂了就该停止打它，
    而不是每个请求都再撞一次超时。`_apply_rerank` 早就有「失败即降级」，
    但没有「因此不再尝试」——缺的是这后半句。
    """

    def __init__(self, threshold: int = 3, cooldown_seconds: float = 30.0):
        self.threshold = max(1, int(threshold))
        self.cooldown = float(cooldown_seconds)
        self._fails = 0
        self._open_until = 0.0
        self._lock = threading.Lock()

    def allow(self) -> bool:
        with self._lock:
            return not (self._fails >= self.threshold
                        and time.monotonic() < self._open_until)

    def record_success(self) -> None:
        with self._lock:
            self._fails = 0
            self._open_until = 0.0

    def record_failure(self) -> None:
        with self._lock:
            self._fails += 1
            if self._fails >= self.threshold:
                self._open_until = time.monotonic() + self.cooldown
                logger.warning("连续 %d 次失败，%.0fs 内不再尝试（短路降级）",
                               self._fails, self.cooldown)


class ScaledSemaphore:
    """按配置定尺寸的并发闸门（进程级），**只作上下文管理器使用**。

    取代原来硬编码的 `threading.Semaphore(8)`：闸门的意义是
    「别让 N 个入库任务 × M 个分片把远端打到限流」，而合理的 N/M 组合
    取决于 provider 的配额，必须可配。

    刻意不提供裸 `acquire()/release()`：那对方法允许调用点写出
    「异常路径忘记 release」的永久漏额度，而 `with` 在结构上不可能漏。

    **必须如实说明边界**：这是每进程一把闸。多进程形态下总并发是
    进程数 × permits，所以配置值应按「单个 API/worker 进程」理解。
    """

    def __init__(self, permits: int):
        self.permits = max(1, int(permits))
        self._sem = threading.Semaphore(self.permits)

    def __enter__(self):
        self._sem.acquire()
        return self

    def __exit__(self, *exc):
        self._sem.release()
        return False
