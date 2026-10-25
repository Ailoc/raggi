"""入库任务规格与执行分发。

队列能跨进程的前提是任务可序列化；这里钉住规格的 JSON 往返、分发到
正确的 pipeline 入口、引擎覆盖的折算，以及失败时上传副本的清理。
"""
from __future__ import annotations

import json
import uuid

import pytest

from rag.core.config import Settings
from rag.ingest.jobspec import IngestJob, run_ingest_job


class _Ctx:
    def __init__(self):
        self.settings = Settings()
        self.store = object()
        self.registry = object()
        self.backend = object()
        self.queue = object()


def test_payload_roundtrip():
    job = IngestJob(kind="text", text="正文", title="标题", kb_id="kb-1")
    back = IngestJob.from_payload(job.to_payload())
    assert back == job
    # 也接受字符串（Redis 客户端可能已解码）
    assert IngestJob.from_payload(job.to_payload().decode()) == job


def test_payload_rejects_unknown_kind():
    with pytest.raises(ValueError, match="未知的入库任务类型"):
        IngestJob.from_payload(json.dumps({"kind": "sql"}))


def test_dispatch_text_and_engine_override(monkeypatch):
    seen = {}

    def fake_ingest_text(store, registry, text, title, parser_cfg, settings,
                         *, kb_id="", job_id=None, queue=None):
        seen.update(text=text, title=title, engine=parser_cfg.default,
                    kb_id=kb_id, job_id=job_id, queue=queue)
        return {"doc_id": "d1"}

    from rag.ingest import pipeline

    monkeypatch.setattr(pipeline, "ingest_text", fake_ingest_text)
    ctx = _Ctx()
    out = run_ingest_job(ctx, IngestJob(
        kind="text", text="正文", title="T", kb_id="kb", engine="native"),
        "job-1")

    assert out == {"doc_id": "d1"}
    assert seen == {"text": "正文", "title": "T", "engine": "native",
                    "kb_id": "kb", "job_id": "job-1", "queue": ctx.queue}


def test_dispatch_reparse_keeps_engine_for_pipeline(monkeypatch):
    seen = {}

    def fake_reparse(store, registry, doc_id, parser_cfg, settings, backend,
                     *, engine=None, job_id=None, queue=None):
        seen.update(doc_id=doc_id, engine=engine, parser=parser_cfg.default)
        return {"doc_id": doc_id}

    from rag.ingest import pipeline

    monkeypatch.setattr(pipeline, "reparse_doc", fake_reparse)
    ctx = _Ctx()
    run_ingest_job(ctx, IngestJob(kind="reparse", doc_id="d9", engine="docling"),
                   "job-2")
    assert seen == {"doc_id": "d9", "engine": "docling", "parser": "auto"}


def test_file_job_cleans_up_on_failure(tmp_path, monkeypatch):
    from rag.ingest import pipeline

    src = tmp_path / f"{uuid.uuid4().hex}.md"
    src.write_text("x", encoding="utf-8")

    def boom(*_a, **_kw):
        raise RuntimeError("parse failed")

    monkeypatch.setattr(pipeline, "ingest_file", boom)
    with pytest.raises(RuntimeError):
        run_ingest_job(_Ctx(), IngestJob(kind="file", path=str(src)), "job-3")
    assert not src.exists(), "失败后上传副本没清理，磁盘会泄漏"


def test_file_job_success_is_not_deleted(tmp_path, monkeypatch):
    """成功时文件已被 pipeline 归档搬走；执行器不应再碰它。"""
    from rag.ingest import pipeline

    src = tmp_path / "kept.md"
    src.write_text("x", encoding="utf-8")
    monkeypatch.setattr(pipeline, "ingest_file", lambda *_a, **_kw: {"doc_id": "d"})
    run_ingest_job(_Ctx(), IngestJob(kind="file", path=str(src)), "job-4")
    assert src.exists()
