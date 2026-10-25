"""入库写锁只包真正需要互斥的写。

原文留档（本地 rename / s3 上传）是纯 I/O，原先在写锁里完成：
一次上传会把跨进程单写者按住几十毫秒到几秒，所有入库与编辑排队。
这里用一把"探针锁"证明留档发生时并不持锁。
"""
from __future__ import annotations

import shutil
from contextlib import contextmanager

from rag.core.config import Settings
from rag.ingest import pipeline
from rag.models.registry import ModelRegistry
from rag.storage.tables import LanceStore


class _StubEmbedder:
    dim = 4
    model = "stub"

    def embed_one(self, text):  # noqa: ARG002
        return [1.0, 0.0, 0.0, 0.0]

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def test_archive_runs_outside_write_lock(tmp_path, monkeypatch):
    settings = Settings()
    settings.data_dir = tmp_path
    settings.embed.dim = 4
    settings.embed.model = "stub"
    store = LanceStore.for_data_dir(tmp_path, 4)
    registry = ModelRegistry(settings)
    registry.bundle = type(registry.bundle)(
        embedder=_StubEmbedder(), chat_factory=lambda: None,
        reranker=None, embed_cfg=settings.embed, llm_cfg=settings.llm,
        rerank_cfg=settings.rerank)

    state = {"locked": False}

    @contextmanager
    def probe_lock():
        state["locked"] = True
        try:
            yield
        finally:
            state["locked"] = False

    monkeypatch.setattr(store, "write_lock", probe_lock)

    class Backend:
        locked_during_move: bool | None = None

        def move_in(self, src, name):
            Backend.locked_during_move = state["locked"]
            shutil.move(str(src), str(tmp_path / name))

    path = tmp_path / "manual.md"
    path.write_text("# 标题\n\n变频器额定电压 380V。\n", encoding="utf-8")

    pipeline.ingest_file(
        store, registry, path, "锁范围测试",
        settings.parser, settings, backend=Backend())

    assert Backend.locked_during_move is False, \
        "原文留档在写锁内完成：一次上传会把整个写入串行化"
