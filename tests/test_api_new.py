"""新端点回归测试：search GET/report、batch、embed、answer 开关、
versions/rollback、stats、chunk 详情/批量编辑、documents 过滤、reparse。
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from rag.api import Ctx, create_app
from rag.core.config import (EmbedConfig, LLMConfig, ParserConfig,
                         RerankConfig, Settings, SplitConfig)
from rag.ingest import pipeline
from rag.models.registry import ModelRegistry
from rag.storage.health import health
from rag.storage.tables import LanceStore
from rag.storage.repos import upsert_chunks, upsert_documents


class _StubEmbedder:
    dim = 4
    model = "stub"

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def embed_one(self, text):
        return [1.0, 0.0, 0.0, 0.0]


class _FakeChat:
    async def ainvoke(self, prompt):
        return SimpleNamespace(content="答案依据[1]")


class _FakeBundle:
    def __init__(self):
        self.embed_cfg = EmbedConfig()
        self.llm_cfg = LLMConfig()
        self.rerank_cfg = RerankConfig()


class _FakeRegistry:
    """无需真实模型服务的注册表桩（仅测试用）。"""

    def __init__(self):
        self.embedder = _StubEmbedder()
        self.reranker = None
        self.bundle = _FakeBundle()

    def chat(self):
        return _FakeChat()

    def reload(self):
        return {"ok": True, "warnings": []}


def _client(tmp_path: Path) -> TestClient:
    s = Settings()
    s.data_dir = tmp_path
    s.token = ""
    store = LanceStore.for_data_dir(tmp_path, 4)
    reg = _FakeRegistry()
    return TestClient(create_app(Ctx(s, store, reg)))


def _seed(store: LanceStore, n: int = 3) -> None:
    vec = [1.0, 0.0, 0.0, 0.0]
    upsert_documents(store, [{
        "doc_id": "d1", "title": "测试文档", "source_uri": None,
        "mime": "text/plain", "parser_engine": "native",
        "content_hash": "h1", "char_count": 30, "chunk_count": n,
        "text": "你好世界\n" * n, "status": "ready", "error": None,
        "meta": "{}", "created_at": "", "updated_at": "",
    }])
    upsert_chunks(store, [{
        "chunk_id": f"c{i}", "doc_id": "d1", "ordinal": i,
        "text": f"你好世界 {i}", "text_seg": "你好 世界",
        "heading_path": "", "page": None, "char_start": 0,
        "char_end": 10, "token_count": 2, "origin": "manual",
        "edited": False, "original_text": None, "offset_valid": True,
        "embed_model": "stub", "vector": vec,
        "created_at": "", "updated_at": "",
    } for i in range(n)])


def test_search_get_and_report(tmp_path):
    """GET /api/search（query 参数）与 Markdown 报告导出。"""
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        _seed(store)
        s = Settings()
        s.data_dir = Path(d)
        c = TestClient(create_app(Ctx(s, store, _FakeRegistry())))
        r = c.get("/api/search", params={"q": "你好", "top_k": 2})
        assert r.status_code == 200
        body = r.json()
        assert body["mode"] == "hybrid"
        assert len(body["results"]) >= 1
        assert body["results"][0]["chunk_id"] == "c0"
        # GET 带过滤
        r2 = c.get("/api/search", params={
            "q": "你好", "doc_ids": "d1", "origin": "manual"})
        assert r2.status_code == 200
        assert len(r2.json()["results"]) == 3
        # 报告导出
        r3 = c.post("/api/search/report", json={"q": "你好"})
        assert r3.status_code == 200
        assert "检索报告" in r3.text
        assert "你好" in r3.text


def test_batch_ingest(tmp_path):
    """POST /api/documents/batch 批量入库。"""
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        s = Settings()
        s.data_dir = Path(d)
        reg = ModelRegistry(s)
        reg.bundle.embedder = _StubEmbedder()
        c = TestClient(create_app(Ctx(s, store, reg)))
        r = c.post("/api/documents/batch", json={
            "items": [
                {"text": "批量文本一：苹果香蕉", "title": "t1"},
                {"text": "批量文本二：葡萄西瓜", "title": "t2"},
            ]})
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        assert all(x["status"] == "ready" for x in body["results"])
        assert store.documents.count_rows() == 2


def test_url_ingest_endpoint_shape(tmp_path):
    """POST /api/documents/url 端点存在且参数类型化（缺 url → 422）。"""
    c = _client(tmp_path)
    assert c.post("/api/documents/url", json={}).status_code == 422


def test_embed_endpoint(tmp_path):
    """POST /api/embed 文本向量化。"""
    c = _client(tmp_path)
    r = c.post("/api/embed", json={"texts": ["你好", "世界"]})
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 2
    assert body["dim"] == 4
    assert len(body["vectors"]) == 2
    assert c.post("/api/embed", json={"texts": []}).status_code == 400


def test_answer_disabled_by_default(tmp_path):
    """POST /api/answer 默认关闭（503）。"""
    c = _client(tmp_path)
    assert c.post("/api/answer", json={"q": "你好"}).status_code == 503


def test_answer_enabled(tmp_path):
    """features.answer=true 时 /api/answer 返回带引用的答案。"""
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        _seed(store)
        s = Settings()
        s.data_dir = Path(d)
        s.features.answer = True
        c = TestClient(create_app(Ctx(s, store, _FakeRegistry())))
        r = c.post("/api/answer", json={"q": "你好"})
        assert r.status_code == 200
        body = r.json()
        assert "答案" in body["answer"]
        assert body["citations"], "应有引用"
        assert body["citations"][0]["chunk_id"] == "c0"


def test_versions_and_rollback(tmp_path):
    """GET /api/versions 与 POST /api/rollback。"""
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        _seed(store)
        s = Settings()
        s.data_dir = Path(d)
        c = TestClient(create_app(Ctx(s, store, _FakeRegistry())))
        r = c.get("/api/versions")
        assert r.status_code == 200
        versions = r.json()["versions"]
        assert versions, "应有版本"
        v0 = versions[0]["version"]
        r2 = c.post("/api/rollback", json={"version": v0})
        assert r2.status_code == 200
        assert r2.json()["rolled_back_to"] == v0


def test_stats(tmp_path):
    """GET /api/stats 容量统计。"""
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        _seed(store)
        s = Settings()
        s.data_dir = Path(d)
        c = TestClient(create_app(Ctx(s, store, _FakeRegistry())))
        r = c.get("/api/stats")
        assert r.status_code == 200
        body = r.json()
        assert body["doc_count"] == 1
        assert body["chunk_count"] == 3
        assert "indexes" in body
        assert "lancedb_dir_bytes" in body


def test_chunk_get_and_batch_edit(tmp_path):
    """GET /api/chunks/{id} 与 PATCH /api/chunks 批量编辑。"""
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        _seed(store)
        s = Settings()
        s.data_dir = Path(d)
        reg = _FakeRegistry()
        c = TestClient(create_app(Ctx(s, store, reg)))
        r = c.get("/api/chunks/c0")
        assert r.status_code == 200
        assert r.json()["text"] == "你好世界 0"
        assert c.get("/api/chunks/nope").status_code == 404
        # 批量编辑（manual 分块无需 force）
        r2 = c.patch("/api/chunks", json={"edits": [
            {"chunk_id": "c0", "text": "改后的文本 零"},
            {"chunk_id": "c1", "text": "改后的文本 一"},
        ]})
        assert r2.status_code == 200
        assert r2.json()["edited"] == 2
        row = store.chunks.search().where(
            "chunk_id = 'c0'").to_list()[0]
        assert row["text"] == "改后的文本 零"
        assert row["edited"] is True


def test_documents_list_filters(tmp_path):
    """GET /api/documents 支持 status/parser_engine/mime/q 过滤。"""
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        _seed(store)
        upsert_documents(store, [{
            "doc_id": "d2", "title": "另一篇", "source_uri": None,
            "mime": "application/pdf", "parser_engine": "pymupdf4llm",
            "content_hash": "h2", "char_count": 5, "chunk_count": 0,
            "text": "pdf 内容", "status": "failed", "error": "x",
            "meta": "{}", "created_at": "", "updated_at": "",
        }])
        s = Settings()
        s.data_dir = Path(d)
        c = TestClient(create_app(Ctx(s, store, _FakeRegistry())))
        assert c.get("/api/documents").json()["total"] == 2
        assert c.get(
            "/api/documents", params={"status": "failed"}
        ).json()["total"] == 1
        assert c.get(
            "/api/documents",
            params={"parser_engine": "pymupdf4llm"}).json()["total"] == 1
        assert c.get(
            "/api/documents", params={"mime": "text/plain"}).json()[
                "total"] == 1
        assert c.get(
            "/api/documents", params={"q": "另一篇"}).json()["total"] == 1
        assert c.get(
            "/api/documents", params={"q": "不存在的标题"}).json()[
                "total"] == 0


def test_chunks_server_side_q_filter_pages_after_filtering():
    """GET /api/chunks 的 q 必须在**分页前**过滤，total 是过滤后的块数。

    这条是前端"分块页只能在已加载的窗口里搜"的根因：过滤放在分页之后时，
    界面只能写「在已加载的 N 块中匹配 M 块」，而命中在窗口外的块永远取不到。
    所以关键断言不是"能不能筛"，而是**第三个匹配项能不能靠 offset 拿到**。
    """
    def chunk(i: int, text: str, kb: str = "k1", doc: str = "d1") -> dict:
        return {
            "chunk_id": f"c{i}", "doc_id": doc, "kb_id": kb, "ordinal": i,
            "text": text, "text_seg": text, "heading_path": "",
            "page": None, "char_start": 0, "char_end": len(text),
            "token_count": 2, "origin": "parser", "edited": False,
            "original_text": None, "offset_valid": True,
            "embed_model": "stub", "vector": [1.0, 0.0, 0.0, 0.0],
            "created_at": "", "updated_at": "",
        }

    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        # 命中项散落在 ordinal 1 / 3 / 4：limit=2 时第三个命中必然落在第一页之外
        upsert_chunks(store, [
            chunk(0, "开篇闲聊"),
            chunk(1, "布偶猫 的 毛发 护理"),
            chunk(2, "中段无关内容"),
            chunk(3, "猫咪 喂食 时间表"),
            chunk(4, "流浪猫 领养记录 Kitten"),
        ])
        s = Settings()
        s.data_dir = Path(d)
        c = TestClient(create_app(Ctx(s, store, _FakeRegistry())))
        base = {"doc_id": "d1"}

        def get(**params):
            r = c.get("/api/chunks", params={**base, **params})
            assert r.status_code == 200
            return r.json()

        assert get()["total"] == 5, "不带 q 时应返回全部"
        page1 = get(q="猫", limit=2)
        assert page1["total"] == 3, "total 必须是过滤后的块数，而不是全库块数"
        assert [x["chunk_id"] for x in page1["items"]] == ["c1", "c3"], \
            "第一页应给出前两个命中（按 ordinal 稳定排序）"
        page2 = get(q="猫", limit=2, offset=2)
        assert [x["chunk_id"] for x in page2["items"]] == ["c4"], \
            "第三个命中必须靠 offset 取得到——这才证明过滤发生在分页之前"
        assert page2["total"] == 3, "翻页时 total 要保持同一次过滤的口径"

        # 大小写不敏感（英文混排是常见库内容）
        assert [x["chunk_id"] for x in get(q="kitten", limit=10)["items"]] == ["c4"]
        # 无命中时是 0，而不是"没取到"
        assert get(q="完全不存在的词")["total"] == 0
        # 与 kb / only_standalone 组合时语义不变
        upsert_chunks(store, [chunk(9, "独立猫咪便签", kb="k1", doc="")])
        only = c.get("/api/chunks", params={"kb_id": "k1", "only_standalone": "true",
                                            "q": "猫咪"}).json()
        assert [x["chunk_id"] for x in only["items"]] == ["c9"], \
            "独立分块范围与 q 要能同时生效"
        # 列表列不能因为多了一条取数路径而缩水：前端高亮依赖这些字段
        assert "offset_valid" in page1["items"][0] and "char_start" in page1["items"][0], \
            "q 路径少返回了定位字段，前端的高亮会静默不生效"


def test_reparse_no_stored_file(tmp_path):
    """POST /api/documents/{id}/reparse：无留档原文 → 400。"""
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        _seed(store)
        s = Settings()
        s.data_dir = Path(d)
        reg = ModelRegistry(s)
        reg.bundle.embedder = _StubEmbedder()
        c = TestClient(create_app(Ctx(s, store, reg)))
        r = c.post("/api/documents/d1/reparse", json={})
        assert r.status_code == 400
        assert "留档" in r.json()["detail"]
        assert c.post(
            "/api/documents/nope/reparse", json={}).status_code == 404


def test_chunk_ordinal_no_collision_after_delete(tmp_path):
    """删除分块后新增分块，ordinal 不与现存冲突（回归修复）。"""
    from rag.chunk_edit import add_manual_chunk, remove_chunk

    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        _seed(store, n=3)
        # 删除中间块 c1（ordinal=1）：剩余 ordinal 为 [0, 2]
        remove_chunk(store, "c1")
        # 新增块：max(ordinal)+1 = 3；
        # 旧实现 count_rows=2 会与 c2 的 ordinal=2 冲突
        new_id = add_manual_chunk(store, _StubEmbedder(), "新增块",
                                  doc_id="d1")
        rows = store.chunks.search().where(
            "doc_id = 'd1'").select(["ordinal"]).to_list()
        ordinals = sorted(int(r["ordinal"]) for r in rows)
        assert ordinals == [0, 2, 3], f"ordinal 冲突: {ordinals}"
        assert len(set(ordinals)) == len(ordinals), "ordinal 不应重复"
        # 新块可检索
        from rag.core.config import RetrieveConfig
        from rag.retrieval.search import search

        res = search(store, _StubEmbedder(), None, "新增",
                     RetrieveConfig(mode="fts"))
        assert res["results"], "FTS 应命中新块"


def test_dim_mismatch_detection(tmp_path):
    """维度守护：chunks 表实际维度 vs 配置 dim 不匹配 → degraded。"""
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        _seed(store)
        assert store.vector_dim() == 4
        # 配置 dim=8 与表实际 dim=4 不匹配
        h = health(store, 8, "stub")
        assert h["stored_embedding_dim"] == 4
        assert h["dim_mismatch"] is True
        assert h["status"] == "degraded"
        # 匹配时正常
        h2 = health(store, 4, "stub")
        assert h2["dim_mismatch"] is False
        assert h2["status"] == "ok"
