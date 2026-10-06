"""冒烟测试：建表 / 写入 / 向量检索 / 健康。无需 embedding 服务（使用桩 embedder）。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from rag.storage.tables import LanceStore
from rag.storage.repos import upsert_chunks, upsert_documents
from rag.retrieval.search import search
from rag.storage.health import health


class _StubEmbedder:
    dim = 4
    model = "stub"

    def embed_one(self, text):  # noqa: ARG002
        return [1.0, 0.0, 0.0, 0.0]

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def test_store_ingest_search():
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(d, 4)
        upsert_documents(store, [{
            "doc_id": "d1", "title": "t", "source_uri": None, "mime": "text/plain",
            "parser_engine": "native", "content_hash": "h1", "char_count": 10, "chunk_count": 1,
            "text": "hello world", "status": "ready", "error": None, "meta": "{}",
            "created_at": "", "updated_at": "",
        }])
        upsert_chunks(store, [{
            "chunk_id": "c1", "doc_id": "d1", "ordinal": 0, "text": "hello world",
            "text_seg": "hello world", "heading_path": "", "page": None, "char_start": 0,
            "char_end": 11, "token_count": 2, "origin": "manual", "edited": False,
            "original_text": None, "offset_valid": True, "embed_model": "stub",
            "vector": [1.0, 0.0, 0.0, 0.0], "created_at": "", "updated_at": "",
        }])
        store.ensure_vector_index()
        store.ensure_fts_index()
        res = search(store, _StubEmbedder(), None, "hello", __import__("rag.core.config", fromlist=["Settings"]).Settings().retrieve)
        assert res["results"], "应有命中"
        assert res["results"][0]["chunk_id"] == "c1"

        h = health(store, 4, "stub")
        assert h["chunk_count"] == 1
        assert h["status"] == "ok"
