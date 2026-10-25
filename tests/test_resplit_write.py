"""重切分不重写整篇正文。

documents 一行带着整篇正文（上限 20MB）。重切分 / 重解析如果产出同一份
正文，整行 upsert 会把这段正文连同 SQLite 溢出页或 Lance 数据页重写一遍，
而内容逐字节相同。`content_hash` 相同就只更新小字段。
"""
from __future__ import annotations

import pytest

from rag.core.config import Settings
from rag.ingest import pipeline
from rag.models.registry import ModelRegistry
from rag.storage.repos.documents import get_document
from rag.storage.tables import LanceStore

TEXT = "# 标题\n\n变频器额定电压 380V，允许波动 ±10%。\n" * 40


class _StubEmbedder:
    dim = 4
    model = "stub"

    def embed_one(self, text):  # noqa: ARG002
        return [1.0, 0.0, 0.0, 0.0]

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


@pytest.fixture()
def env(tmp_path):
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
    out = pipeline.ingest_text(store, registry, TEXT, "重切分", settings.parser, settings)
    return store, registry, settings, out["doc_id"]


def test_resplit_updates_only_small_fields(env, monkeypatch):
    store, registry, settings, doc_id = env
    calls = {"upsert": 0, "fields": 0}
    real_upsert = pipeline.upsert_documents

    def spy_upsert(*a, **kw):
        calls["upsert"] += 1
        return real_upsert(*a, **kw)

    real_fields = pipeline.set_doc_fields

    def spy_fields(*a, **kw):
        calls["fields"] += 1
        return real_fields(*a, **kw)

    monkeypatch.setattr(pipeline, "upsert_documents", spy_upsert)
    monkeypatch.setattr(pipeline, "set_doc_fields", spy_fields)

    before = get_document(store, doc_id)
    out = pipeline.resplit_doc(store, registry, doc_id, settings.parser, settings)

    assert out["chunk_count"] == before["chunk_count"] or out["chunk_count"] > 0
    assert calls["upsert"] == 0, "正文没变，却整行重写了 documents（含 20MB 正文）"
    assert calls["fields"] >= 1, "小字段也没有更新"
    after = get_document(store, doc_id)
    assert after["text"] == before["text"], "正文被改坏了"
    assert after["created_at"] == before["created_at"], "入库时间不该被重切覆盖"


def test_changed_text_still_takes_the_full_upsert(env, monkeypatch):
    """正文真的变了（重解析拿到新内容）时必须整行写回，不能走快捷路径。"""
    store, registry, settings, doc_id = env
    calls = {"upsert": 0}
    real_upsert = pipeline.upsert_documents

    def spy_upsert(*a, **kw):
        calls["upsert"] += 1
        return real_upsert(*a, **kw)

    monkeypatch.setattr(pipeline, "upsert_documents", spy_upsert)

    from rag.ingest.pipeline import _ensure_job
    from rag.parsing.loaders import load_text

    engine, documents = load_text(TEXT + "\n新增段落。", "重切分")
    pipeline._run(
        store, registry, engine, documents, "重切分", "text:重切分",
        _ensure_job(store, None), settings.parser,
        split_cfg=settings.split, doc_id=doc_id,
        plan=None)

    assert calls["upsert"] >= 1, "正文变了却只更新了小字段"
