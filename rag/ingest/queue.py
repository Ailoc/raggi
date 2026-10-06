"""入库任务队列：有界并发 + 状态可查。

**为什么需要队列**

入库是「解析 → 切分 → 向量化 → 写库」的复合操作，CPU 与 IO 都重。
在此之前每个 HTTP 请求各自跑完整流程，带来两个问题：

1. **并发无上限**。多个上传同时涌入时，全部挤在 LanceDB 的写锁和
   embedding 服务上互相排队，整体吞吐反而更低，且一个失败会拖慢其它。
2. **请求被长时间挂住**。上传 50MB 的 PDF 要让 HTTP 连接一直等到
   解析与向量化结束，客户端容易超时，用户也看不到任何进度。

**设计**

- 进程内的有界线程池（默认 2 个 worker）：入库是重操作，并发过高
  只会互相争抢，2 个足以让「解析」与「向量化」的 IO 互相重叠。
- 队列有长度上限：满了直接拒绝（503 + `Retry-After`），快速失败好过无限堆积。
- 任务状态落在 jobs 表，前端轮询即可显示进度。
- 同步调用方 `wait=True` 阻塞等结果，行为与队列引入前一致；
  异步调用方拿 job_id 自行轮询。
"""
from __future__ import annotations

import datetime
import logging
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor

from rag.core.errors import QueueFull
from rag.storage.repos import jobs as jobs_repo

logger = logging.getLogger("raggi.queue")

# 终态：不再变化，前端可以停止轮询
# cancelled 也算终态：排队中被撤销，或运行中在阶段边界退出
TERMINAL_STAGES = jobs_repo.TERMINAL_STAGES

# 并发 worker 数。入库的重活是解析与 embedding（都是 IO 等待型），
# 2 个足以重叠，再多只会加剧 LanceDB 写锁争抢。
DEFAULT_WORKERS = 2
# 队列长度上限：超出直接拒绝，避免请求无限堆积把内存吃光
DEFAULT_MAX_PENDING = 32
# 任务记录保留天数的**默认值**：`settings.job_retention_days` 会覆盖它
# （装配时在 `api/__init__.py` 传进来）。留在这里只作为不传参数时的行为。
JOB_RETENTION_DAYS = 7


class JobCancelled(Exception):
    """协作式取消：在阶段边界主动退出流水线。

    定义在队列模块（而非 pipeline）：API 层要捕获它把同步请求转成 409，
    而 pipeline 依赖队列、API 依赖两者——放在队列里才不会形成
    「api → pipeline → api」的循环导入。

    为什么是异常而不是静默返回：流水线有多个提前 return 的分支，
    静默返回会让调用方以为成功（拿到一个没有 doc_id 的结果），
    而任务行其实已被标记为取消。
    """


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class IngestQueue:
    """有界并发的入库队列。"""

    def __init__(self, store, workers: int = DEFAULT_WORKERS,
                 max_pending: int = DEFAULT_MAX_PENDING,
                 retention_days: int = JOB_RETENTION_DAYS):
        self.store = store
        self.workers = max(1, int(workers))
        self.max_pending = max(1, int(max_pending))
        # 来自 `settings.job_retention_days`。曾经这个配置只写进 config.toml
        # 就没人读了：用户改成 1 天，记录照样留 7 天 —— 配置在骗人。
        self.retention_days = max(1, int(retention_days))
        self._pool = ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix="raggi-ingest")
        self._pending = 0
        self._lock = threading.Lock()
        self._last_prune = 0.0
        # job_id → Future，用于取消排队中的任务；
        # job_id 集合，记录已被请求取消但仍在运行的任务
        self._futures: dict[str, Future] = {}
        self._cancelled: set[str] = set()
        # 已终止（_wrapped 的 finally 已执行）的 job_id。
        # 有了它，cancel() 才能区分「正在运行」与「早就跑完了但任务行
        # 还停在非终态」—— 否则会对一个已完成任务写 stage=cancelling，
        # 而那个任务的 finally 已经过去，不会再有任何人修正它。
        self._finished: set[str] = set()

    # ---- 提交 ----------------------------------------------------------

    def submit(self, fn, *args, doc_id: str = "", engine: str = "",
               **kwargs) -> tuple[str, Future]:
        """把一次入库排进队列，返回 (job_id, future)。

        任务行在**入队时**就写入 jobs 表（stage=queued），
        这样前端一拿到 job_id 就能查到状态，不必等 worker 起来。

        kb_id 从 kwargs 里取（不设成独立形参）：它同时也是**要传给
        pipeline 的数据参数**，做成形参会与调用方的 kwargs 撞车
        （Python 在调用时绑定关键字，报 "multiple values"）。
        它必须在入队时就写进任务行——那是 doc_id 生成之前唯一已知的
        归属线索，任务页靠它把任务链回知识库。
        """
        job_kb = str(kwargs.get("kb_id") or "")
        with self._lock:
            if self._pending >= self.max_pending:
                raise QueueFull(
                    f"入库队列已满（{self._pending}/{self.max_pending}）。"
                    "请稍后重试，或等当前任务完成")
            self._pending += 1

        job_id = str(uuid.uuid4())
        try:
            jobs_repo.add_job(self.store, {
                "job_id": job_id, "doc_id": doc_id, "kb_id": job_kb,
                "stage": "queued",
                "progress": 0.0, "total": 0, "parser_engine": engine,
                "error": None, "started_at": _now(), "ended_at": None,
            })
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._pending -= 1
            raise RuntimeError(f"写入任务记录失败: {e}") from e

        self._maybe_prune()

        def _wrapped():
            try:
                return fn(*args, job_id=job_id, **kwargs)
            finally:
                with self._lock:
                    self._pending -= 1
                    self._futures.pop(job_id, None)
                    self._cancelled.discard(job_id)
                    self._finished.add(job_id)

        try:
            fut = self._pool.submit(_wrapped)
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._pending -= 1
            self._fail(job_id, str(e))
            raise RuntimeError(f"任务入队失败: {e}") from e
        with self._lock:
            # 竞态守卫：worker 可能在 submit 返回前就跑完 _wrapped，
            # 其 finally 已经 pop 过 job_id。此时再登记会把一个**已终止**
            # 的 Future 塞回表里，cancel() 拿不到它就退化成「请求协作式
            # 取消」——对一个已完成任务写 stage=cancelling，终态被改写成
            # 永不修复的非终态（finally 已执行完，不会再有人改回来）。
            if job_id in self._cancelled or fut.done():
                self._futures.pop(job_id, None)
                self._finished.add(job_id)
            else:
                self._futures[job_id] = fut
        return job_id, fut

    # ---- 取消 ----------------------------------------------------------

    def cancel(self, job_id: str) -> str:
        """请求取消一个任务，返回实际处置方式。

        诚实的能力边界（PyMuPDF / embedding 都不响应中断）：
        - **排队中**：真正取消——从池里撤掉，永不执行；
        - **运行中**：协作式取消——置取消标记，任务在**下一个阶段边界**
          自行退出。已经发出的 HTTP 请求会跑完（无法打断），因此不是
          立即停止，这点必须如实告诉调用方，不能让用户以为已经停了。

        返回 "cancelled" / "running" / "finished" / "unknown"，
        让 API 层据此给出不同的说明。
        """
        row = get_job(self.store, job_id)
        if row is None:
            return "unknown"
        if row.get("stage") in TERMINAL_STAGES:
            return "finished"

        with self._lock:
            fut = self._futures.get(job_id)
            done = job_id in self._finished
        if done and fut is None:
            # 任务体早已执行完（finally 已跑过），但任务行因裸任务体
            # 没自己写终态而仍停在 queued。此时再写 cancelling 等于把
            # 一个终止的任务永久标成「正在停止」，前端会一直轮询下去。
            return "finished"
        # 还没被 worker 取走的：直接撤销，并把任务行置为终态
        if fut is not None and fut.cancel():
            with self._lock:
                self._pending -= 1
                self._finished.discard(job_id)
            self._cancel(job_id)
            return "cancelled"

        # 已在运行：只能请求协作式取消
        with self._lock:
            self._cancelled.add(job_id)
        self._mark_cancelling(job_id)
        return "running"

    def is_cancelled(self, job_id: str) -> bool:
        """这个任务被请求取消了吗。

        **必须同时看任务行**：多进程形态下「点停止的那个请求」和
        「正在跑这个任务的进程」通常不是同一个进程，内存里的
        `_cancelled` 集合互相看不见。`cancel()` 已经把行写成
        `stage='cancelling'`，这里以行为准，内存只是本进程的快路径。

        行为查一次 ~3-5ms（Lance 点查），阶段边界每任务最多 2 次，
        代价可控；元数据搬到 SQLite 之后（S3）这里会降到微秒级。
        """
        with self._lock:
            if job_id in self._cancelled:
                return True
        try:
            row = get_job(self.store, job_id, cols=["job_id", "stage"])
        except Exception:  # noqa: BLE001
            return False
        return bool(row) and str(row.get("stage")) == "cancelling"

    # ---- 状态 ----------------------------------------------------------

    def stats(self) -> dict:
        with self._lock:
            pending = self._pending
        return {"workers": self.workers, "pending": pending,
                "max_pending": self.max_pending}

    def shutdown(self, wait: bool = False) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=False)
        with self._lock:
            self._finished.clear()

    # ---- 内部 ----------------------------------------------------------

    def _fail(self, job_id: str, msg: str) -> None:
        if not jobs_repo.set_job(self.store, job_id, stage="failed",
                                 error=msg[:500], ended_at=_now()):
            logger.debug("标记任务失败出错（写入未生效）")

    def _cancel(self, job_id: str) -> None:
        """把已撤销的任务行置为终态（排队中即被真正取消）。"""
        if not jobs_repo.set_job(self.store, job_id, stage="cancelled",
                                 error="已取消（尚未开始执行）",
                                 ended_at=_now()):
            logger.debug("标记任务已取消出错（写入未生效）")

    def _mark_cancelling(self, job_id: str) -> None:
        """运行中的任务：先记为 cancelling，让界面立刻反映「正在停止」。

        真正的终态由流水线在阶段边界自己写（它知道在哪退出最安全）。
        """
        if not jobs_repo.set_job(self.store, job_id, stage="cancelling"):
            logger.debug("标记任务取消中出错（写入未生效）")

    def _maybe_prune(self) -> None:
        """顺带清理过期任务记录与内存中的已终止集合。

        没有独立定时器：提交时做一次即可——没有新任务就不会产生新记录，
        也就没有增长压力。每小时最多跑一次，避免每次提交都全表扫。
        """
        import time

        now = time.time()
        with self._lock:
            if now - self._last_prune < 3600:
                return
            self._last_prune = now
        # _finished 只为 cancel() 的判定服务；任务行被清理后它就再也
        # 用不上了，必须一起裁掉，否则长跑进程会一直累积。
        try:
            cutoff = (datetime.datetime.now(datetime.timezone.utc)
                      - datetime.timedelta(days=self.retention_days)
                      ).isoformat()
            deleted = jobs_repo.prune_jobs(self.store, cutoff)
            if deleted:
                logger.info("已清理 %d 条过期任务记录", deleted)
        except Exception as e:  # noqa: BLE001
            logger.debug("清理任务记录失败: %s", e)
        # 裁剪必须在锁**外**做逐个查询（get_job 会读 LanceDB），
        # 否则长时间持锁会把 submit/cancel 一起卡住。
        with self._lock:
            stale = [j for j in self._finished
                     if j not in self._futures and j not in self._cancelled]
        keep = {j for j in stale if self._is_unfinished(j)}
        with self._lock:
            self._finished -= (set(stale) - keep)

    def _is_unfinished(self, job_id: str) -> bool:
        """任务行还没到终态就保留判定记录（cancel() 还要靠它）。"""
        row = get_job(self.store, job_id)
        return row is not None and row.get("stage") not in TERMINAL_STAGES


def list_jobs(store, limit: int = 50, kb_id: str = "") -> list[dict]:
    """最近的任务列表（按开始时间倒序），兼容旧签名（只返回列表）。

    任务记录是纯观测数据：查询失败不该让接口 500，返回空列表即可。
    """
    try:
        rows, _total = jobs_repo.list_jobs(store, limit=limit, kb_id=kb_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("读取任务列表失败: %s", e)
        return []
    return rows


def get_job(store, job_id: str, cols: list[str] | None = None) -> dict | None:
    """单个任务状态。"""
    try:
        return jobs_repo.get_job(store, job_id, cols=cols)
    except Exception as e:  # noqa: BLE001
        logger.debug("读取任务失败 %s: %s", job_id, e)
        return None
