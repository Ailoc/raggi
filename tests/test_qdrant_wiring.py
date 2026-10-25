"""Qdrant 向量后端的端到端接线（阶段 3c）。

需要真实 Qdrant：`RAG_TEST_QDRANT_URL`（例如 http://127.0.0.1:6333）。
验证的是**应用真的走 Qdrant**：入库 → 检索 → 分块列表 → 编辑 → 停用 →
删除文档 → 健康检查，全部经 `chunk_backend` 分派，不碰 Lance 的 chunks 表。
"""
from __future__ import annotations

import os
import uuid

import pytest

URL = os.environ.get("RAG_TEST_QDRANT_URL", "")
pytestmark = pytest.mark.skipif(
    not URL, reason="未设置 RAG_TEST_QDRANT_URL，跳过 Qdrant 接线测试")


class _E:
    dim = 4
    model = "stub"

    def embed_one(self, text):  # noqa: ARG002
        return [1.0, 0.0, 0.0, 0.0]

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


@pytest.fixture()
def env(tmp_path):
    from rag.api import Ctx
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.qdrant_store import QdrantChunkStore
    from rag.storage.tables import LanceStore

    settings = Settings()
    settings.data_dir = tmp_path
    settings.token = ""
    settings.embed.dim = 4
    settings.embed.model = "stub"
    store = LanceStore.for_data_dir(tmp_path, 4)
    store.qdrant = QdrantChunkStore(
        URL, collection=f"w_{uuid.uuid4().hex[:8]}", dim=4)
    store.qdrant.ensure_collection()
    store.vector_engine = "qdrant"
    registry = ModelRegistry(settings)
    registry.bundle = type(registry.bundle)(
        embedder=_E(), chat_factory=lambda: None, reranker=None,
        embed_cfg=settings.embed, llm_cfg=settings.llm,
        rerank_cfg=settings.rerank)
    ctx = Ctx(settings, store, registry)
    yield ctx
    store.qdrant.drop()


def test_ingest_search_page_edit_disable_delete(env):
    from rag.chunk_edit import (
        edit_chunk,
        remove_chunk,
        set_chunk_enabled,
    )
    from rag.ingest import pipeline
    from rag.retrieval.search import search
    from rag.storage.health import health
    from rag.storage.repos.chunks import chunks_page, get_chunk
    from rag.storage.repos.documents import delete_document

    ctx = env
    out = pipeline.ingest_text(
        ctx.store, ctx.registry, "变频器额定电压 380V。" * 30, "接线验证",
        ctx.settings.parser, ctx.settings)
    doc_id = out["doc_id"]
    assert out["chunk_count"] > 0

    # 1) 检索走 Qdrant（dense + sparse + RRF）
    res = search(ctx.store, ctx.registry.embedder, None, "变频器",
                 ctx.settings.retrieve, None, 5)
    assert res["results"], "Qdrant 检索没有结果"
    hit = res["results"][0]
    assert hit["snippet"]["text"], "检索命中没有片段"

    # 2) 分块列表/详情走 Qdrant
    page, total = chunks_page(ctx.store, doc_id=doc_id, limit=10)
    assert total > 0 and len(page) == total
    assert all(r["doc_id"] == doc_id for r in page)
    chunk = get_chunk(ctx.store, page[0]["chunk_id"])
    assert chunk["text"]

    # 3) 编辑：正文与向量一起更新
    cid = page[0]["chunk_id"]
    edit_chunk(ctx.store, ctx.registry.embedder, cid,
               "改写成龙脑香热带雨林", force=True)
    assert "龙脑香" in (get_chunk(ctx.store, cid) or {}).get("text", "")
    fts = search(ctx.store, ctx.registry.embedder, None, "龙脑香",
                 ctx.settings.retrieve, None, 5)
    assert any(r["chunk_id"] == cid for r in fts["results"]), "编辑后全文检索没命中"

    # 4) 停用：检索不再返回
    set_chunk_enabled(ctx.store, cid, False)
    res2 = search(ctx.store, ctx.registry.embedder, None, "龙脑香",
                  ctx.settings.retrieve, None, 5)
    assert all(r["chunk_id"] != cid for r in res2["results"]), "停用块仍被检索到"

    # 5) 删除单块：计数减少
    before = chunks_page(ctx.store, doc_id=doc_id, limit=100)[1]
    remove_chunk(ctx.store, cid)
    after = chunks_page(ctx.store, doc_id=doc_id, limit=100)[1]
    assert after == before - 1

    # 6) 健康检查：计数与维度来自 Qdrant
    h = health(ctx.store, 4, "stub")
    assert h["chunk_count"] == ctx.store.qdrant.count()
    assert h["stored_embedding_dim"] == 4
    assert h["count_mismatch"] == [], "文档 chunk_count 没跟上 Qdrant"

    # 7) 删除文档：它下面的分块全部清掉
    delete_document(ctx.store, doc_id, backend=None)
    assert chunks_page(ctx.store, doc_id=doc_id, limit=10)[1] == 0
