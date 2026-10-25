"""入库任务的**可序列化**规格。

为什么需要它：`IngestQueue.submit(fn, ...)` 传的是可调用对象，只能在
本进程执行。多副本/独立 worker 形态下，API 只能把"要做什么"写进队列，
由另一个进程反序列化后执行——所以任务必须是一个可 JSON 的数据结构。

文件任务的 `path` 是上传中转副本的路径，要求 API 与 worker 共享同一个
数据卷（`deploy/docker-compose.yml` 里的 `/data`）。这是"文件先落到
共享存储"的第一步；对象存储形态下应改为 S3 key（阶段 3）。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

JobKind = Literal["file", "text", "url", "reparse", "resplit"]


@dataclass(frozen=True)
class IngestJob:
    """一次入库/重解析的描述（不含 store/registry 等运行时对象）。"""

    kind: JobKind
    title: str = ""
    kb_id: str = ""
    # kind=text
    text: str = ""
    # kind=url
    url: str = ""
    # kind=file：共享卷上的工作副本 + 原始文件名（决定解析引擎）
    path: str = ""
    filename: str = ""
    # kind=reparse：目标文档 + 可选的解析引擎覆盖（空串 = 沿用当前配置）
    doc_id: str = ""
    engine: str = ""
    # 幂等键只用于排障关联（真正的幂等判定在 API 层，入队前完成）
    idempotency_key: str = field(default="", repr=False)

    def to_payload(self) -> bytes:
        return json.dumps(asdict(self), ensure_ascii=False).encode("utf-8")

    @classmethod
    def from_payload(cls, raw: bytes | str) -> "IngestJob":
        data = json.loads(raw)
        kind = data.get("kind")
        if kind not in ("file", "text", "url", "reparse", "resplit"):
            raise ValueError(f"未知的入库任务类型: {kind!r}")
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in data.items() if k in allowed})


def run_ingest_job(ctx, job: IngestJob, job_id: str) -> dict[str, Any]:
    """在**当前进程**执行一个入库任务（本地队列与外部 worker 共用）。

    `ctx` 需要提供 store / registry / settings / backend / queue —— 与
    `rag.api.Ctx` 一致；worker 进程用它自己装配的那一份。
    """
    from rag.ingest import pipeline
    from rag.ingest.queue import JobCancelled
    from rag.parsing.router import ALLOWED_ENGINES

    # 文件/文本/URL 的引擎覆盖在这里折算进 parser 配置；重解析由
    # pipeline.reparse_doc 自己处理（它还要求 engine=None 表示沿用）。
    parser_cfg = ctx.settings.parser
    if (job.kind in ("file", "text", "url") and job.engine
            and job.engine != "auto" and job.engine in ALLOWED_ENGINES):
        parser_cfg = parser_cfg.model_copy(update={"default": job.engine})
    path = Path(job.path) if job.kind == "file" else None
    try:
        if job.kind == "file":
            return pipeline.ingest_file(
                ctx.store, ctx.registry, path, job.title or None,
                parser_cfg, ctx.settings,
                backend=ctx.backend, source_uri=job.filename,
                kb_id=job.kb_id, job_id=job_id, queue=ctx.queue)
        if job.kind == "text":
            return pipeline.ingest_text(
                ctx.store, ctx.registry, job.text, job.title, parser_cfg,
                ctx.settings, kb_id=job.kb_id, job_id=job_id, queue=ctx.queue)
        if job.kind == "url":
            return pipeline.ingest_url(
                ctx.store, ctx.registry, job.url, parser_cfg, ctx.settings,
                kb_id=job.kb_id, job_id=job_id, queue=ctx.queue)
        if job.kind == "resplit":
            return pipeline.resplit_doc(
                ctx.store, ctx.registry, job.doc_id, parser_cfg, ctx.settings,
                job_id=job_id, queue=ctx.queue)
        if job.kind == "reparse":
            return pipeline.reparse_doc(
                ctx.store, ctx.registry, job.doc_id, parser_cfg, ctx.settings,
                ctx.backend, engine=job.engine or None,
                job_id=job_id, queue=ctx.queue)
    except JobCancelled:
        _cleanup(path)
        raise
    except Exception:
        _cleanup(path)
        raise
    raise ValueError(f"未知的入库任务类型: {job.kind!r}")


def _cleanup(path: Path | None) -> None:
    """失败/取消时删掉上传的工作副本；成功时 pipeline 已把它归档搬走。"""
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
