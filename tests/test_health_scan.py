"""health 的 chunks 汇总只扫一遍（Arrow 聚合），失败再退回逐项查询。

2 万块实测：改前 cold health p50 ≈ 40ms（三趟扫描 + 每行变成 dict），
改后 8–17ms。这里钉住「口径一致」与「扫描失败仍能如实降级」。
"""
from __future__ import annotations

import uuid

import pytest

from rag.storage import health as health_mod
from rag.storage.health import _chunk_facts, _unique_models, health
from rag.storage.repos.chunks import chunk_counts_by_doc
from rag.storage.repos.documents import upsert_documents
from rag.storage.sql import chunk_defaults, fill_missing, table_columns
from rag.storage.tables import LanceStore


@pytest.fixture()
def store(tmp_path):
    st = LanceStore.for_data_dir(tmp_path, 4)
    docs = []
    rows = []
    for d in range(2):
        did = str(uuid.uuid4())
        docs.append({"doc_id": did, "title": f"d{d}", "content_hash": did,
                     "char_count": 10, "chunk_count": 3, "text": "正文",
                     "status": "ready", "kb_id": "", "meta": "{}",
                     "created_at": "2026-01-01", "updated_at": "2026-01-01"})
        for i in range(3):
            rows.append({
                "chunk_id": str(uuid.uuid4()), "doc_id": did, "kb_id": "",
                "ordinal": i, "text": "t", "text_seg": "t",
                "vector": [0.1, 0.2, 0.3, 0.4], "enabled": True,
                # 第二篇的一条标脏：待重建计数与模型集合都要能测出来
                "fts_stale": d == 1 and i == 0,
                "embed_model": "other" if d == 1 else "stub",
                "created_at": "2026-01-01", "updated_at": "2026-01-01"})
    upsert_documents(st, docs)
    st.chunks.add(fill_missing(rows, table_columns(st.chunks), chunk_defaults(4)))
    return st


def test_single_scan_matches_the_individual_queries(store):
    counts, stale, models = _chunk_facts(store)
    assert counts == chunk_counts_by_doc(store)
    assert stale == store.chunks.count_rows(filter="fts_stale = true") == 1
    assert models == _unique_models(store) == {"stub", "other"}


def test_health_does_not_fall_back_when_the_scan_works(store, monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(health_mod, "chunk_counts_by_doc",
                        lambda s: calls.append("counts") or {})
    monkeypatch.setattr(health_mod, "_unique_models",
                        lambda s: calls.append("models") or set())
    res = health(store, 4, "stub")
    assert calls == [], "汇总扫描成功时不该再逐项扫"
    assert res["chunk_count"] == 6
    assert res["fts_stale_count"] == 1
    assert res["embedding_model_mismatch"] is True


def test_health_degrades_honestly_when_the_scan_fails(store, monkeypatch):
    def boom(_store):
        raise RuntimeError("scanner 挂了")

    monkeypatch.setattr(health_mod, "_chunk_facts", boom)
    res = health(store, 4, "stub")
    # 退回逐项查询仍然能给出正确结果，且不虚报「检查失败」
    assert res["chunk_count"] == 6
    assert res["fts_stale_count"] == 1
    assert res["checks_failed"] == []
