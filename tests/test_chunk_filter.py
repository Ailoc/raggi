"""结构化分块过滤：渲染、恒假语义、以及检索路径真的吃这个对象。

过滤原先只有 SQL 字符串一种形态，换向量引擎时只能解析 SQL 或另写一份
过滤语义。`ChunkFilter` 把条件变成数据；SQL 只是当前引擎的渲染结果。
"""
from __future__ import annotations

import uuid

import pytest

from rag.storage.filters import NEVER, ChunkFilter, in_clause, where_sql
from rag.storage.repos.chunks import (
    chunk_filter,
    chunk_prefilter,
    list_filter,
    search_chunks,
)
from rag.storage.tables import LanceStore


def test_empty_filter_renders_none():
    f = ChunkFilter()
    assert f.to_sql() is None
    assert f.is_empty() is True
    assert f.is_never() is False


def test_renders_each_condition():
    f = ChunkFilter(
        enabled_only=True, origin="parsed", kb_id="kb1",
        doc_ids=("d1", "d2"), only_standalone=True, text_contains="变频器")
    sql = f.to_sql()
    assert sql is not None
    assert "enabled = true" in sql
    assert "origin = 'parsed'" in sql
    assert "kb_id = 'kb1'" in sql
    assert "doc_id IN ('d1', 'd2')" in sql
    assert "doc_id = ''" in sql
    assert "text ILIKE '%变频器%'" in sql


def test_kb_ids_used_only_without_single_kb():
    sql = ChunkFilter(kb_ids=("a", "b")).to_sql()
    assert sql == "kb_id IN ('a', 'b')"
    sql2 = ChunkFilter(kb_id="a", kb_ids=("b",)).to_sql()
    assert sql2 == "kb_id = 'a'"


def test_text_contains_escapes_wildcards():
    sql = ChunkFilter(text_contains="100%_done").to_sql()
    assert sql is not None
    assert "100\\%\\_done" in sql, "通配符没转义：% / _ 会被当成通配"


def test_empty_doc_id_set_is_never_match_not_skip():
    f = ChunkFilter(doc_id_sets=((),))
    assert f.is_never() is True
    assert f.to_sql() == NEVER
    assert "IN ()" not in (f.to_sql() or "")


def test_in_clause_refuses_empty_set():
    assert in_clause("doc_id", ["a"]) == "doc_id IN ('a')"
    with pytest.raises(ValueError, match="恒假条件请用"):
        in_clause("doc_id", [])


def test_where_sql_normalizes_both_shapes():
    f = ChunkFilter(enabled_only=True)
    assert where_sql(f) == "enabled = true"
    assert where_sql("x = 1") == "x = 1"
    assert where_sql(None) is None


def test_chunk_prefilter_is_the_sql_rendering_of_chunk_filter(tmp_path):
    store = LanceStore.for_data_dir(tmp_path, 4)
    filters = {"origin": "parsed", "doc_ids": ["a", "b"]}
    assert chunk_prefilter(store, filters) == chunk_filter(store, filters).to_sql()
    # 读不了 documents 时：仍然恒假，不是「不加过滤」
    f = chunk_filter(None, {"mime": "application/pdf"})
    assert f.is_never() and "__no_match__" in (f.to_sql() or "")


def test_list_filter_makes_doc_id_win_over_kb():
    f = list_filter(doc_id="d1", kb_id="kb1", q=" 需要转义% ")
    assert f.doc_ids == ("d1",)
    assert f.kb_id == ""
    assert f.text_contains == "需要转义%"
    assert "kb_id" not in (f.to_sql() or "")


def _seed(store: LanceStore) -> None:
    from rag.storage.sql import chunk_defaults, fill_missing, table_columns

    rows = []
    for i in range(3):
        rows.append({
            "chunk_id": str(uuid.uuid4()), "doc_id": "d1", "kb_id": "kb1",
            "ordinal": i, "text": f"正文 {i}", "text_seg": f"正文 {i}",
            "vector": [1.0, 0.0, 0.0, 0.0], "enabled": i != 2,
            "fts_stale": False, "embed_model": "stub",
            "created_at": "2026-01-01", "updated_at": "2026-01-01"})
    store.chunks.add(fill_missing(
        rows, table_columns(store.chunks), chunk_defaults(4)))


def test_search_chunks_accepts_structured_filter(tmp_path):
    store = LanceStore.for_data_dir(tmp_path, 4)
    _seed(store)
    hits = search_chunks(
        store, mode="vector", vector=[1.0, 0.0, 0.0, 0.0],
        where=ChunkFilter(enabled_only=True), limit=10)
    assert len(hits) == 2, "停用的分块仍被检索到：结构化过滤没生效"
    assert all(bool(h["enabled"]) for h in hits)


def test_search_chunks_still_accepts_sql_string(tmp_path):
    store = LanceStore.for_data_dir(tmp_path, 4)
    _seed(store)
    hits = search_chunks(
        store, mode="vector", vector=[1.0, 0.0, 0.0, 0.0],
        where="enabled = true", limit=10)
    assert len(hits) == 2
