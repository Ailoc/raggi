"""API 共享工具：上下文取值 / 标量转换 / 脱敏 / 配置合并 / 检索配置覆盖。"""
from __future__ import annotations

from fastapi import HTTPException

from rag.storage.sql import scalar


def get_ctx(request):
    """从请求取应用上下文（Ctx）。"""
    return request.app.state.ctx


def enqueue(ctx, job, *, wait: bool = True, timeout: float = 600.0) -> dict:
    """把一个**可序列化的入库任务**交给队列。

    `job` 是 `rag.ingest.jobspec.IngestJob`：本地队列进程内执行，
    Redis Stream 队列交给独立的 worker 进程。API 层只依赖
    `queue.submit_job(job)` 这一个入口。

    wait=True（默认）等到任务终态再返回：
    - 本地队列有 Future，直接取结果（异常原样重抛）；
    - 外部队列没有 Future，轮询任务行；结果里的 chunk_count / engine
      从文档行回读（**"duplicate" 这类细分状态由本地队列保留**，
      远程队列只能给出 ready/failed/cancelled 三态）。

    队列满（`QueueFull`）在这里拦下来翻译成 503，状态码与 Retry-After
    取自异常本身：端点外围普遍有 `except Exception → 500` 的兜底。
    """

    from fastapi import HTTPException

    from rag.core.errors import QueueFull
    from rag.ingest.jobspec import IngestJob  # noqa: F401  仅用于类型可读性
    from rag.ingest.queue import JobCancelled

    try:
        job_id, fut = ctx.queue.submit_job(job)
    except QueueFull as e:
        raise HTTPException(e.status_code, str(e), headers=dict(e.headers))
    if not wait:
        return {"job_id": job_id, "status": "queued", "doc_id": job.doc_id}
    try:
        result = fut.result() if fut is not None else _wait_remote(
            ctx, job_id, timeout)
    except JobCancelled:
        # 等待中被取消：这是**用户的动作**，不是服务端故障，
        # 不该变成 500。409 让调用方能明确区分「被取消」与「出错了」。
        raise HTTPException(409, "任务已取消")
    result.setdefault("job_id", job_id)
    return result


def _wait_remote(ctx, job_id: str, timeout: float) -> dict:
    """外部队列：轮询任务行直到终态（本地队列走 Future，不用这里）。"""
    import time

    from fastapi import HTTPException

    from rag.ingest.queue import JobCancelled
    from rag.storage.repos import documents as docs_repo
    from rag.storage.repos import jobs as jobs_repo

    terminal = {"done", "failed", "cancelled"}
    deadline = time.monotonic() + max(1.0, timeout)
    delay = 0.05
    last = None
    while time.monotonic() < deadline:
        row = jobs_repo.get_job(ctx.store, job_id)
        last = row
        stage = str((row or {}).get("stage") or "")
        if stage in terminal:
            if stage == "cancelled":
                raise JobCancelled()
            if stage == "failed":
                raise RuntimeError(str((row or {}).get("error") or "任务失败"))
            doc_id = str((row or {}).get("doc_id") or "")
            doc = (docs_repo.get_document(ctx.store, doc_id, include_text=False)
                   if doc_id else None)
            return {
                "doc_id": doc_id,
                "job_id": job_id,
                "status": "ready",
                "chunk_count": int((doc or {}).get("chunk_count") or 0),
                "engine": str((doc or {}).get("parser_engine") or ""),
            }
        time.sleep(delay)
        delay = min(1.0, delay * 1.5)
    raise HTTPException(
        504, f"等待任务超时（{timeout:.0f}s，任务仍在后台继续；"
             f"job_id={job_id}，最后阶段={str((last or {}).get('stage'))}）")


def scalar_dict(d: dict) -> dict:
    """整行做标量转换（numpy/pyarrow → Python 原生）。

    实现收敛到 storage.sql.scalar：早前 _common / storage.plan /
    chunk_edit 各有一份同名 _scalar，改一处漏两处就会让某条路径
    悄悄返回 numpy 类型（JSON 序列化时才炸）。
    """
    return {k: scalar(v) for k, v in d.items()}


def mask(d: dict) -> dict:
    """api_key 脱敏（回显掩码，回传时忽略）。"""
    if "api_key" in d:
        d = dict(d)
        d["api_key"] = "***" if d["api_key"] else ""
    return d


def merge_sub(settings, key: str, patch: dict):
    """把前端/API 提交的部分配置合并进 settings 的某个子模型。

    **整体替换子模型，不就地改字段**。原来这里是 `setattr(sub, k, v)`
    逐个改：并发请求正读着同一个 settings 时，会看到「一半新一半旧」的
    配置（比如新模型名 + 旧维度），而这类状态既没有报错也难以复现。
    换成 `model_copy(update=…)` 后替换的是**引用**，读者要么拿到旧对象、
    要么拿到新对象，永远是自洽的一组配置。
    """
    sub = getattr(settings, key)
    # 从类上取 model_fields：在实例上访问已废弃（Pydantic 2.11+）
    fields = type(sub).model_fields
    updates = {}
    for k, v in patch.items():
        if k in fields:
            # 前端拿到的是掩码 "***"，原样回传时忽略，避免覆盖真实密钥
            if k == "api_key" and v == "***":
                continue
            updates[k] = v
    if not updates:
        return sub
    new_sub = sub.model_copy(update=updates)
    setattr(settings, key, new_sub)
    return new_sub


def cfg_with_overrides(settings, mode: str | None, window: int | None):
    """检索配置的请求级覆盖（mode / window）。"""
    cfg = settings.retrieve
    updates = {}
    if mode in ("hybrid", "vector", "fts"):
        updates["mode"] = mode
    if window is not None:
        updates["window"] = max(0, int(window))
    return cfg.model_copy(update=updates) if updates else cfg


async def safe(fn):
    """执行协程，失败时返回结构化错误而非 500。"""
    try:
        return await fn
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}


# ---- 错误分类 --------------------------------------------------------
# 旧实现在每个端点都写 `except Exception -> 503 "（…embedding 服务不可用？）"`，
# 把 SQL 语法错误、Arrow 维度失配一并伪装成模型服务故障，排障被带偏（B2）。
# 这里按异常类型分类：只有 EmbedUnavailable 是 503，其余保留原始信息。


def _short(exc: Exception, limit: int = 400) -> str:
    """异常的可读摘要：取 message，多行取首行，避免把整段栈回传给客户端。"""
    msg = str(exc).strip() or exc.__class__.__name__
    return msg.splitlines()[0][:limit]


def raise_operation_error(exc: Exception, action: str) -> None:
    """把底层异常映射为合适的 HTTP 错误。

    EmbedUnavailable → 503（模型服务不可用，已有全局 handler 兜底）
    ValueError / FileNotFoundError / KeyError → 400/404（调用方问题）
    其它 → 500 + 原始摘要，不假装是 embedding 的问题。

    action 是已完成时（"重切分失败"），动词短语；未完成时传
    "向量化"（得到 "向量化失败"）。
    """
    from rag.models.embeddings import EmbedUnavailable

    if isinstance(exc, EmbedUnavailable):
        raise HTTPException(503, f"{action}：{_short(exc)}")
    if isinstance(exc, KeyError):
        raise HTTPException(404, _short(exc))
    if isinstance(exc, (ValueError, FileNotFoundError, PermissionError)):
        raise HTTPException(400, f"{action}：{_short(exc)}")
    msg = action if action.endswith(("失败", "错误")) else f"{action}失败"
    raise HTTPException(500, f"{msg}：{_short(exc)}")
