"""独立入库 worker：消费 Redis Stream 里的任务。

用法（`queue.backend=redis` + `redis.url`）：

    rag worker

它复用 `server.build_ctx()` 的装配（同一份数据目录、同一套模型配置），
只是不启动 HTTP。任务体仍然是 `rag.ingest.jobspec.run_ingest_job`，
与进程内队列完全同一条代码路径。
"""
from __future__ import annotations

import logging
import signal
import sys

log = logging.getLogger("raggi.worker")


def run_worker() -> None:
    from rag.server import build_ctx

    ctx = build_ctx()
    queue = ctx.queue
    if not hasattr(queue, "consume_forever"):
        raise SystemExit(
            "当前 queue.backend 不是 redis（见 [queue] backend）；"
            "`rag worker` 只消费外部队列")

    def _stop(signum, _frame):
        log.info("收到信号 %s，正在退出（当前任务会跑完并 ACK）", signum)
        queue.shutdown()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    log.info("worker 启动：stream=%s group=%s consumer=%s",
             queue.stream, queue.group, queue.consumer)
    try:
        queue.consume_forever()
    except KeyboardInterrupt:  # pragma: no cover - 交互式退出
        queue.shutdown()
    sys.stdout.flush()
