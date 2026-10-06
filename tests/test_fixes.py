"""回归测试：本次审计修复项的针对性验证。可直接 python 运行或 pytest 运行。"""
from __future__ import annotations

import tempfile
from pathlib import Path

from rag.core.config import RetrieveConfig, Settings
from rag.retrieval.search import search
from rag.storage.health import health
from rag.storage.tables import LanceStore
from rag.storage.repos import upsert_chunks, upsert_documents


class _StubEmbedder:
    dim = 4
    model = "stub"

    def embed_one(self, text):  # noqa: ARG002
        return [1.0, 0.0, 0.0, 0.0]

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def _seed(store: LanceStore, n: int = 1, embed_model: str = "stub") -> None:
    vec = [1.0] + [0.0] * (store.dim - 1)
    upsert_documents(store, [{
        "doc_id": "d1", "title": "t", "source_uri": None, "mime": "text/plain",
        "parser_engine": "native", "content_hash": "h1", "char_count": 10, "chunk_count": n,
        "text": "hello world", "status": "ready", "error": None, "meta": "{}",
        "created_at": "", "updated_at": "",
    }])
    upsert_chunks(store, [{
        "chunk_id": f"c{i}", "doc_id": "d1", "ordinal": i, "text": f"hello world {i}",
        "text_seg": "hello world", "heading_path": "", "page": None, "char_start": 0,
        "char_end": 11, "token_count": 2, "origin": "manual", "edited": False,
        "original_text": None, "offset_valid": True, "embed_model": embed_model,
        "vector": vec, "created_at": "", "updated_at": "",
    } for i in range(n)])


def test_optimize_no_error():
    """修复：optimize 曾因 timedelta 未导入而静默失败。"""
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        _seed(store)
        store.optimize()  # 不应抛异常
        assert store.chunks.count_rows() == 1


def test_vector_index_incremental():
    """修复：索引按需/增量重建，少量新增不触发全量重建。"""
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 64)
        _seed(store, n=300)
        store.ensure_vector_index()
        exists, unindexed = store._index_state("vector")
        assert exists, "向量索引应已建立"
        assert store._vector_needs_rebuild(305) is False


def test_auth_and_key_masking():
    """修复：RAG_TOKEN 真正生效；三类模型 api_key 均已脱敏。"""
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.models.registry import ModelRegistry

    with tempfile.TemporaryDirectory() as d:
        s = Settings()
        s.data_dir = Path(d)
        store = LanceStore.for_data_dir(d, s.embed.dim)
        reg = ModelRegistry(s)

        c = TestClient(create_app(Ctx(s, store, reg)))
        assert c.get("/api/health").status_code == 200
        # 掩码只对非空密钥生效；先设一个再验证
        c.put("/api/models?persist=false", json={
            "embed": {"api_key": "sk-secret"},
            "llm": {"api_key": "sk-llm"},
            # 回归防护：rerank 补上 api_key 字段后曾漏过脱敏，
            # 真实密钥经 GET /api/models 明文回显
            "rerank": {"api_key": "sk-rerank"},
        })
        models = c.get("/api/models").json()
        assert models["embed"]["api_key"] == "***"
        assert models["llm"]["api_key"] == "***"
        assert models["rerank"]["api_key"] == "***"
        # 原样回传掩码不得覆盖真实密钥
        c.put("/api/models?persist=false", json={
            "rerank": {"api_key": "***"}})
        assert s.rerank.api_key == "sk-rerank", "掩码回传覆盖了真实密钥"

        s2 = Settings()
        s2.data_dir = Path(d)
        s2.token = "secret"
        c2 = TestClient(create_app(Ctx(s2, store, reg)))
        assert c2.get("/api/health").status_code == 401
        assert c2.get("/api/health", headers={"Authorization": "Bearer secret"}).status_code == 200


def test_search_mode_override():
    """修复：检索模式可被请求覆盖。"""
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(d, 4)
        _seed(store)
        store.ensure_vector_index()
        store.ensure_fts_index()
        for m in ("vector", "fts", "hybrid"):
            res = search(store, _StubEmbedder(), None, "hello", RetrieveConfig(mode=m))
            assert res["mode"] == m
            assert res["degraded_reason"] is None, f"{m} 不应降级: {res['degraded_reason']}"


def test_health_embedding_mismatch():
    """修复：嵌入模型一致性检测不再是死代码。"""
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        _seed(store, embed_model="old-model")
        h = health(store, 4, "stub")
        assert h["embedding_model_mismatch"] is True
        assert h["status"] == "degraded"


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"FAIL {name}: {e!r}")
    raise SystemExit(1 if fails else 0)
