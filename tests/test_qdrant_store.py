"""Qdrant 分块后端的集成测试（阶段 3b）。

需要真实 Qdrant：设置 `RAG_TEST_QDRANT_URL`（例如 http://127.0.0.1:6333）
后才会跑。默认套件跳过——单机形态不依赖任何外部服务。

这里验证的是**语义对齐**：结构化过滤（含恒假）、vector / sparse 全文 /
RRF 融合、删除与计数。分数绝对值与 Lance 不可比，比较的是排序与过滤。
"""
from __future__ import annotations

import os
import uuid

import pytest

from rag.storage.filters import ChunkFilter

URL = os.environ.get("RAG_TEST_QDRANT_URL", "")
pytestmark = pytest.mark.skipif(
    not URL, reason="未设置 RAG_TEST_QDRANT_URL，跳过 Qdrant 集成测试")


@pytest.fixture()
def store():
    from rag.storage.qdrant_store import QdrantChunkStore

    st = QdrantChunkStore(URL, collection=f"t_{uuid.uuid4().hex[:8]}", dim=4)
    st.ensure_collection()
    yield st
    st.drop()


def _row(text: str, **over):
    base = {
        "chunk_id": str(uuid.uuid4()), "doc_id": "d1", "kb_id": "kb1",
        "ordinal": 0, "heading_path": "h", "page": None, "text": text,
        "char_start": 0, "char_end": len(text), "offset_valid": True,
        "edited": False, "enabled": True, "origin": "parsed",
        "embed_model": "stub", "created_at": "2026-01-01",
        "updated_at": "2026-01-01", "vector": [1.0, 0.0, 0.0, 0.0],
    }
    base.update(over)
    return base


def test_upsert_count_get_delete(store):
    a = _row("变频器 额定电压 380V")
    b = _row("龙脑香 热带雨林", doc_id="d2", kb_id="kb2",
             vector=[0.0, 1.0, 0.0, 0.0])
    store.upsert_chunks([a, b])

    assert store.count() == 2
    got = store.get_chunk(a["chunk_id"])
    assert got["text"] == a["text"] and got["doc_id"] == "d1"

    store.delete_chunks(ChunkFilter(doc_ids=("d2",)))
    assert store.count() == 1


def test_vector_search_and_enabled_filter(store):
    on = _row("enabled chunk", ordinal=0)
    off = _row("disabled chunk", ordinal=1, enabled=False)
    store.upsert_chunks([on, off])

    hits = store.search(mode="vector", vector=[1.0, 0.0, 0.0, 0.0],
                        where=ChunkFilter(enabled_only=True), limit=10)
    ids = [h["chunk_id"] for h in hits]
    assert ids == [on["chunk_id"]], "停用分块仍被检索到"
    assert hits[0]["_distance"] == pytest.approx(0.0, abs=1e-6)


def test_filters_map_kb_doc_and_never(store):
    a = _row("甲", kb_id="kb1", doc_id="d1")
    b = _row("乙", kb_id="kb2", doc_id="d2")
    store.upsert_chunks([a, b])

    def ids(where):
        return {h["chunk_id"] for h in store.search(
            mode="vector", vector=[1.0, 0.0, 0.0, 0.0], where=where, limit=10)}

    assert ids(ChunkFilter(kb_id="kb2")) == {b["chunk_id"]}
    assert ids(ChunkFilter(kb_ids=("kb1", "kb2"))) == {a["chunk_id"], b["chunk_id"]}
    assert ids(ChunkFilter(doc_ids=("d1",))) == {a["chunk_id"]}
    # 空白名单 = 恒假，绝不能退化成"不过滤"
    assert ids(ChunkFilter(doc_id_sets=((),))) == set()


def test_sparse_fulltext_search(store):
    """jieba 分词 + IDF：中文关键词要能命中并按相关度排序。"""
    hit = _row("变频器过流报警先检查电机电缆绝缘与负载", ordinal=0)
    other = _row("龙脑香科植物分布于热带雨林", ordinal=1,
                 vector=[0.0, 1.0, 0.0, 0.0])
    store.upsert_chunks([hit, other])

    rows = store.search(mode="fts", text="变频器 电机", limit=5)
    assert rows and rows[0]["chunk_id"] == hit["chunk_id"]
    assert rows[0].get("_score") is not None


def test_hybrid_returns_rrf_scores(store):
    hit = _row("变频器 额定电压 380V", ordinal=0)
    store.upsert_chunks([hit])
    rows = store.search(mode="hybrid", vector=[1.0, 0.0, 0.0, 0.0],
                        text="变频器", limit=5)
    assert rows and rows[0]["chunk_id"] == hit["chunk_id"]
    assert rows[0].get("_relevance_score") is not None


def test_update_text_refreshes_sparse_and_payload(store):
    row = _row("旧内容", ordinal=0)
    store.upsert_chunks([row])
    store.update_text(row["chunk_id"], text="新内容 变频器",
                      vector=[1.0, 0.0, 0.0, 0.0], updated_at="2026-02-02")

    got = store.get_chunk(row["chunk_id"])
    assert got["text"] == "新内容 变频器"
    assert got["updated_at"] == "2026-02-02"
    rows = store.search(mode="fts", text="变频器", limit=5)
    assert rows and rows[0]["chunk_id"] == row["chunk_id"]


def test_vector_topk_matches_lance(tmp_path, store):
    """同一批向量在两个引擎上 top-k 顺序一致（余弦距离口径对齐）。"""
    from rag.storage.repos.chunks import search_chunks
    from rag.storage.sql import chunk_defaults, fill_missing, table_columns
    from rag.storage.tables import LanceStore

    rows = [
        _row("近", ordinal=0, vector=[1.0, 0.0, 0.0, 0.0]),
        _row("中", ordinal=1, vector=[0.8, 0.6, 0.0, 0.0]),
        _row("远", ordinal=2, vector=[0.0, 0.0, 1.0, 0.0]),
    ]
    store.upsert_chunks([dict(r) for r in rows])

    lance = LanceStore.for_data_dir(tmp_path, 4)
    lance.chunks.add(fill_missing(
        [dict(r) for r in rows], table_columns(lance.chunks), chunk_defaults(4)))

    query = [1.0, 0.0, 0.0, 0.0]
    from rag.storage.filters import ChunkFilter
    a = search_chunks(lance, mode="vector", vector=query,
                      where=ChunkFilter(enabled_only=True), limit=3)
    b = store.search(mode="vector", vector=query,
                     where=ChunkFilter(enabled_only=True), limit=3)
    assert [h["chunk_id"] for h in a] == [h["chunk_id"] for h in b]


def test_page_is_stable_on_ordinal_ties(store):
    """ordinal 只在单文档内唯一：分页必须有次级键，否则并列处重复/漏项。"""
    ids = [str(uuid.UUID(int=i + 1)) for i in range(4)]
    rows = [
        _row("a", ordinal=0, chunk_id=ids[0]),
        _row("b", ordinal=1, chunk_id=ids[1]),
        _row("c", ordinal=1, chunk_id=ids[2]),
        _row("d", ordinal=2, chunk_id=ids[3]),
    ]
    store.upsert_chunks(rows)

    page1, total = store.page(limit=2, offset=0)
    page2, _ = store.page(limit=2, offset=2)

    assert total == 4
    got = [r["chunk_id"] for r in page1 + page2]
    assert len(got) == len(set(got)) == 4, "分页出现重复或漏项"
    assert set(got) == set(ids)
    ordinals = [r["ordinal"] for r in page1 + page2]
    assert ordinals == sorted(ordinals), "跨页顺序不是 ordinal 升序"


def test_page_honours_filters(store):
    a = _row("甲", doc_id="d1", ordinal=0)
    b = _row("乙", doc_id="d2", ordinal=0)
    store.upsert_chunks([a, b])
    rows, total = store.page(where=ChunkFilter(doc_ids=("d2",)), limit=10)
    assert total == 1 and [r["chunk_id"] for r in rows] == [b["chunk_id"]]


def test_facts_and_vector_dim(store):
    store.upsert_chunks([
        _row("甲", doc_id="d1", embed_model="m1"),
        _row("乙", doc_id="d1", embed_model="m1"),
        _row("丙", doc_id="d2", embed_model="m2"),
    ])
    counts, stale, models = store.facts()
    assert counts == {"d1": 2, "d2": 1}
    assert stale == 0, "Qdrant 的 sparse 索引随点更新，没有待重建"
    assert models == {"m1", "m2"}
    assert store.vector_dim() == 4
