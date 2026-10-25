"""Redis Streams 入库队列：任务规格跨进程传递。

与 `IngestQueue`（进程内线程池）共用同一个入口 `submit_job(job)`，
API 侧看不出差别；区别是执行者可以部署在别的容器/机器上：

    API ──XADD──▶ raggi:ingest ──XREADGROUP──▶ rag worker ──▶ pipeline

语义（必须如实写清楚）：
- **至少一次**：worker 崩溃时任务会留在 pending，超过 `claim_idle_ms`
  后被其它 worker 用 XAUTOCLAIM 接管重跑。任务本身按内容哈希幂等，
  但重跑的分块编辑类任务会覆盖同一批行。
- **取消**：排队中的任务无法从 Stream 中间删除，取消标记写在 Redis，
  worker 取到时立刻标 cancelled 并 ACK（不执行）。
- **wait=true**：远程队列没有本地 Future，API 层改为轮询任务行直到终态。
"""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
import uuid

from rag.core.errors import QueueFull
from rag.ingest.jobspec import IngestJob
from rag.storage.repos import jobs as jobs_repo

logger = logging.getLogger("raggi.queue.redis")

TERMINAL_STAGES = jobs_repo.TERMINAL_STAGES


def _now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class RedisStreamQueue:
    """把入库任务写进 Redis Stream 的队列后端。"""

    def __init__(self, store, runner, *, url: str, namespace: str = "raggi",
                 stream: str = "raggi:ingest", group: str = "raggi-workers",
                 consumer: str = "", max_pending: int = 32,
                 block_ms: int = 2000, claim_idle_ms: int = 60_000,
                 timeout_ms: int = 300):
        from rag.core.redis import RedisClient

        self.store = store
        self._runner = runner
        self.stream = stream
        self.group = group
        self.consumer = consumer or f"{socket.gethostname()}-{os.getpid()}"
        self.max_pending = max(1, int(max_pending))
        self.block_ms = max(100, int(block_ms))
        self.claim_idle_ms = max(1000, int(claim_idle_ms))
        self.namespace = namespace
        self._client = RedisClient(
            url, namespace=namespace, timeout_ms=timeout_ms,
            max_connections=16)
        # 阻塞读必须用单独的连接：socket_timeout 短于 BLOCK 时间时，
        # 客户端会先超时抛错。这里按 block + 2s 配。
        self._consume_client = RedisClient(
            url, namespace=namespace, timeout_ms=self.block_ms + 2000,
            max_connections=2)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._group_ready = False

    # ---- 提交 ----------------------------------------------------------

    def submit_job(self, job: IngestJob):
        """写任务行 + XADD。返回 (job_id, None)（没有本地 Future）。"""
        with self._lock:
            pending = self._backlog()
            if pending is not None and pending >= self.max_pending:
                raise QueueFull(
                    f"入库队列已满（{pending}/{self.max_pending}）。"
                    "请稍后重试，或等当前任务完成")

        job_id = str(uuid.uuid4())
        try:
            jobs_repo.add_job(self.store, {
                "job_id": job_id, "doc_id": job.doc_id, "kb_id": job.kb_id,
                "stage": "queued", "progress": 0.0, "total": 0,
                "parser_engine": job.engine, "error": None,
                "started_at": _now(), "ended_at": None,
            })
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"写入任务记录失败: {e}") from e

        try:
            self._client.xadd(
                self.stream, {"job_id": job_id, "payload": job.to_payload()})
            # 已 ACK 的消息仍留在 Stream 里，不裁剪会无限增长
            self._client.xtrim(
                self.stream, max(1000, self.max_pending * 50))
        except Exception as e:  # noqa: BLE001
            jobs_repo.set_job(self.store, job_id, stage="failed",
                              error=f"入队失败: {e}"[:500], ended_at=_now())
            raise RuntimeError(f"任务入队失败: {e}") from e
        return job_id, None

    # ---- 取消 / 状态 ----------------------------------------------------

    def cancel(self, job_id: str) -> str:
        row = jobs_repo.get_job(self.store, job_id)
        if row is None:
            return "unknown"
        stage = str(row.get("stage") or "")
        if stage in TERMINAL_STAGES:
            return "finished"
        try:
            self._client.psetex_prefixed(
                self._client.key(f"cancel:{job_id}"), 3_600_000, b"1")
        except Exception as e:  # noqa: BLE001
            logger.warning("写取消标记失败，改为只依赖任务行状态: %s", e)
        if stage == "queued":
            jobs_repo.set_job(self.store, job_id, stage="cancelled",
                              error="已取消（尚未开始执行）", ended_at=_now())
            return "cancelled"
        jobs_repo.set_job(self.store, job_id, stage="cancelling")
        return "running"

    def is_cancelled(self, job_id: str) -> bool:
        if self._stop.is_set():
            return True
        try:
            if self._client.exists(f"cancel:{job_id}"):
                return True
        except Exception:  # noqa: BLE001
            # Redis 抖动时以任务行为准（它由 cancel() 一并写入）
            pass
        row = jobs_repo.get_job(self.store, job_id, cols=["job_id", "stage"])
        return bool(row) and str(row.get("stage")) == "cancelling"

    def _backlog(self) -> int | None:
        """积压 = 未投递(lag) + 已投递未 ACK(pending)。

        不能用 XLEN：已 ACK 的消息仍留在 Stream 里（历史），XLEN 只增不减，
        拿它做队列上限会让队列在跑过 max_pending 个任务后永久"已满"。
        """
        try:
            for g in self._client.xinfo_groups(self.stream) or []:
                name = _decode(_field(g, "name"))
                if name != self.group:
                    continue
                lag = int(_field(g, "lag") or 0)
                pend = int(_field(g, "pending") or 0)
                return lag + pend
            return 0
        except Exception:  # noqa: BLE001
            try:
                return self._client.xlen(self.stream)
            except Exception:  # noqa: BLE001
                return None

    def stats(self) -> dict:
        pending = self._backlog()
        in_flight = -1
        try:
            summary = self._client.xpending(self.stream, self.group) or []
            in_flight = int(summary[0]) if summary else 0
        except Exception:  # noqa: BLE001
            pass
        return {"workers": -1,
                "pending": -1 if pending is None else pending,
                "max_pending": self.max_pending, "in_flight": in_flight,
                "backend": "redis"}

    def shutdown(self, wait: bool = False) -> None:  # noqa: ARG002
        self._stop.set()

    # ---- worker 侧 ------------------------------------------------------

    def ensure_group(self) -> None:
        if self._group_ready:
            return
        self._client.xgroup_create(self.stream, self.group)
        self._group_ready = True

    def consume_forever(self) -> None:
        """阻塞消费，直到 shutdown()。任务体异常已由 pipeline 写终态。"""
        self.ensure_group()
        logger.info("worker 已启动 consumer=%s stream=%s group=%s",
                    self.consumer, self.stream, self.group)
        last_claim = 0.0
        while not self._stop.is_set():
            if time.time() - last_claim > 30:
                last_claim = time.time()
                self._reclaim()
            try:
                resp = self._consume_client.xreadgroup(
                    self.stream, self.group, self.consumer,
                    count=1, block_ms=self.block_ms)
            except Exception as e:  # noqa: BLE001
                logger.warning("读取队列失败，1 秒后重试: %s", e)
                self._stop.wait(1.0)
                continue
            if not resp:
                continue
            # redis-py 的 xreadgroup 返回 [[stream, [[id, fields], …]], …]
            for _stream, entries in resp:
                for entry_id, fields in entries:
                    self._handle(entry_id, fields)

    def _handle(self, entry_id, fields: dict) -> None:
        fields = {_decode(k): v for k, v in fields.items()}
        job_id = _decode(fields.get("job_id"))
        raw = fields.get("payload")
        try:
            job = IngestJob.from_payload(_decode(raw) or "")
        except Exception as e:  # noqa: BLE001
            logger.error("任务反序列化失败 entry=%s: %s", entry_id, e)
            self._ack(entry_id)
            return
        if self.is_cancelled(job_id):
            jobs_repo.set_job(self.store, job_id, stage="cancelled",
                              error="已取消（尚未开始执行）", ended_at=_now())
            self._ack(entry_id)
            return
        try:
            self._runner(job, job_id=job_id)
        except Exception as e:  # noqa: BLE001
            # pipeline 自己会写 failed；这里兜底，防止裸任务体漏写终态
            row = jobs_repo.get_job(self.store, job_id)
            if row and str(row.get("stage")) not in TERMINAL_STAGES:
                jobs_repo.set_job(self.store, job_id, stage="failed",
                                  error=str(e)[:500], ended_at=_now())
            logger.warning("任务执行失败 job=%s: %s", job_id, e)
        finally:
            self._ack(entry_id)

    def _ack(self, entry_id) -> None:
        try:
            self._client.xack(self.stream, self.group, entry_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("ACK 失败（消息会被重投）: %s", e)

    def _reclaim(self) -> None:
        """接管崩溃 worker 留下的 pending（至少一次语义）。"""
        try:
            out = self._consume_client.xautoclaim(
                self.stream, self.group, self.consumer,
                self.claim_idle_ms, "0-0", count=5)
        except Exception:  # noqa: BLE001
            return
        if not out or len(out) < 2:
            return
        for entry_id, fields in out[1]:
            logger.info("接管超时任务 entry=%s", entry_id)
            self._handle(entry_id, fields)


def _field(row, name: str):
    """redis-py 的字典键可能是 bytes 也可能是 str（版本差异）。"""
    if name in row:
        return row[name]
    return row.get(name.encode())


def _decode(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)
