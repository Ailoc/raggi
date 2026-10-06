"""API 共享工具：上下文取值 / 标量转换 / 脱敏 / 配置合并 / 检索配置覆盖。"""
from __future__ import annotations

from fastapi import HTTPException

from rag.storage.sql import scalar


def get_ctx(request):
    """从请求取应用上下文（Ctx）。"""
    return request.app.state.ctx


def _bind(ctx, fn):
    """把 pipeline 入口绑定到 (store, registry, queue)，返回只吃数据参数的调用对象。

    集中在这里做，是为了让 `enqueue` 的契约保持简单：
    它只调度，不关心被调对象的签名长什么样。

    绑定 queue 是取消能力的前提：阶段边界的取消检查要问队列
    「这个任务被请求取消了吗」，而队列实例只有 ctx 上有。
    """
    from functools import partial

    return partial(fn, ctx.store, ctx.registry, queue=ctx.queue)


def enqueue(ctx, fn, *args, wait: bool = True,
            doc_id: str = "", **kwargs) -> dict:
    """把入库任务交给队列。

    `fn` 必须是**已绑定好基础设施**的可调用对象（store / registry / ctx），
    队列只负责把它连同数据参数一起调度。这样队列不必知道
    「pipeline 函数的前两个参数是 store 和 registry」这种约定——
    早前在这里自动 partial 绑定，结果把 `_ingest_upload(ctx, ...)`
    也当成 pipeline 签名去绑，直接参数错位报 500。

    wait=True（默认）阻塞等结果，响应与队列引入前完全一致；
    wait=false 立即返回 {job_id, status: "queued"}，客户端轮询
    /api/jobs/{job_id} 获取进度——上传大文件时界面不必卡住。

    队列满（`QueueFull`）在这里拦下来翻译成 503，**但状态码与
    `Retry-After` 取自异常本身**（`rag.core.errors.QueueFull`）：端点外围
    普遍有 `except Exception → 500` 的兜底，领域异常不在这里拦就会被
    兜成 500；而在这里硬写 503 又会让同一个契约有两处定义。

    kwargs 里若带 kb_id，它要同时到两处：submit 的任务行（供任务页回链
    知识库）与 pipeline（决定文档归属）。submit 从 kwargs 里取它，
    因此这里**原样透传**即可，不需要拆成两份——拆开反而会因同名
    撞出 "multiple values"。
    """
    from rag.core.errors import QueueFull
    from rag.ingest.queue import JobCancelled

    try:
        job_id, fut = ctx.queue.submit(fn, *args, doc_id=doc_id, **kwargs)
    except QueueFull as e:
        raise HTTPException(e.status_code, str(e), headers=dict(e.headers))
    if not wait:
        return {"job_id": job_id, "status": "queued", "doc_id": doc_id}
    try:
        result = fut.result()      # 任务体的异常在这里原样重抛
    except JobCancelled:
        # 等待中被取消：这是**用户的动作**，不是服务端故障，
        # 不该变成 500。409 让调用方能明确区分「被取消」与「出错了」。
        raise HTTPException(409, "任务已取消")
    result.setdefault("job_id", job_id)
    return result


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
