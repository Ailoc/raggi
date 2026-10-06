"""知识库端点回归测试：CRUD / 入库归属 / 列表过滤 /
库内检索 / 级联删除 / 重解析保留归属。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from rag.api import Ctx, create_app
from rag.core.config import Settings
from rag.models.registry import ModelRegistry
from rag.storage.repos import kbs
from rag.storage.tables import LanceStore


class _StubEmbedder:
    dim = 4
    model = "stub"

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def embed_one(self, text):
        return [1.0, 0.0, 0.0, 0.0]


def _client(tmp_path: Path) -> tuple[TestClient, LanceStore]:
    s = Settings()
    s.data_dir = tmp_path
    s.token = ""
    store = LanceStore.for_data_dir(tmp_path, 4)
    reg = ModelRegistry(s)
    reg.bundle.embedder = _StubEmbedder()
    return TestClient(create_app(Ctx(s, store, reg))), store


def test_kb_crud(tmp_path):
    """创建 / 列表 / 更新 / 删除知识库。"""
    c, store = _client(tmp_path)
    kb = c.post("/api/kbs", json={
        "name": "产品手册", "description": "v1"}).json()
    assert kb["name"] == "产品手册"
    assert kb["kb_id"]
    # 列表
    lst = c.get("/api/kbs").json()
    assert len(lst) == 1
    assert lst[0]["doc_count"] == 0
    # 更新
    upd = c.put("/api/kbs/" + kb["kb_id"],
                json={"name": "产品手册 v2",
                      "description": "更新"}).json()
    assert upd["name"] == "产品手册 v2"
    # 详情
    detail = c.get("/api/kbs/" + kb["kb_id"]).json()
    assert detail["name"] == "产品手册 v2"
    # 删除
    r = c.delete("/api/kbs/" + kb["kb_id"])
    assert r.status_code == 200
    assert c.get("/api/kbs/" + kb["kb_id"]).status_code == 404
    # 重复删除 → 404
    assert c.delete("/api/kbs/" + kb["kb_id"]).status_code == 404
    # 空名称 → 400
    assert c.post("/api/kbs", json={"name": " "}).status_code == 400


def test_ingest_into_kb(tmp_path):
    """入库归属知识库：文本 / 批量 / 列表过滤 / 库内检索。"""
    c, store = _client(tmp_path)
    kb = c.post("/api/kbs", json={"name": "手册"}).json()
    # 文本入库到 KB
    r = c.post("/api/documents/text", json={
        "text": "苹果香蕉葡萄是水果", "title": "水果一",
        "kb_id": kb["kb_id"]})
    assert r.json()["status"] == "ready"
    doc_id = r.json()["doc_id"]
    # 列表过滤
    in_kb = c.get(
        f"/api/documents?kb_id={kb['kb_id']}").json()
    assert in_kb["total"] == 1
    assert in_kb["items"][0]["kb_id"] == kb["kb_id"]
    # kb_id 为空 = 不过滤（全部文档）
    none = c.get("/api/documents?kb_id=").json()
    assert none["total"] == 1
    # KB 统计
    detail = c.get("/api/kbs/" + kb["kb_id"]).json()
    assert detail["doc_count"] == 1
    assert detail["chunk_count"] >= 1
    # 库内检索命中
    res = c.post("/api/search", json={
        "q": "苹果", "kb_id": kb["kb_id"]}).json()
    assert res["results"], "库内检索应命中"
    assert res["results"][0]["doc_id"] == doc_id
    # 库内检索：不命中库外文档（stub 向量下库内文档
    # 经 vector 通道恒命中，故断言「只返回库内文档」）
    c.post("/api/documents/text", json={
        "text": "苹果汁是饮料", "title": "库外"})
    outside = c.get(
        "/api/documents?limit=10").json()
    outside_id = [d["doc_id"] for d in outside["items"]
                  if d["title"] == "库外"][0]
    res2 = c.post("/api/search", json={
        "q": "苹果汁", "kb_id": kb["kb_id"]}).json()
    assert all(r["doc_id"] == doc_id
               for r in res2["results"]), \
        "库内检索不应返回库外文档"
    res3 = c.post("/api/search", json={"q": "苹果汁"}).json()
    assert any(r["doc_id"] == outside_id
               for r in res3["results"]), \
        "全局检索应命中库外文档"
    # GET 便捷检索带 kb_id
    res4 = c.get(
        "/api/search", params={
            "q": "苹果", "kb_id": kb["kb_id"]}).json()
    assert res4["results"]


def test_batch_ingest_kb(tmp_path):
    """批量入库可指定 kb_id（逐项独立）。"""
    c, _ = _client(tmp_path)
    kb = c.post("/api/kbs", json={"name": "批量"}).json()
    r = c.post("/api/documents/batch", json={
        "items": [
            {"text": "批量一", "title": "t1",
             "kb_id": kb["kb_id"]},
            {"text": "批量二", "title": "t2"},
        ]}).json()
    assert r["total"] == 2
    assert all(x["status"] == "ready" for x in r["results"])
    in_kb = c.get(
        f"/api/documents?kb_id={kb['kb_id']}").json()
    assert in_kb["total"] == 1
    assert in_kb["items"][0]["title"] == "t1"


def test_delete_kb_cascades(tmp_path):
    """删除知识库级联删除其文档与分块。"""
    c, store = _client(tmp_path)
    kb = c.post("/api/kbs", json={"name": "待删"}).json()
    c.post("/api/documents/text", json={
        "text": "将被级联删除的文本", "title": "x",
        "kb_id": kb["kb_id"]})
    assert store.documents.count_rows() == 1
    assert store.chunks.count_rows() >= 1
    r = c.delete("/api/kbs/" + kb["kb_id"])
    assert r.status_code == 200
    assert store.documents.count_rows() == 0
    assert store.chunks.count_rows() == 0
    assert c.get("/api/kbs").json() == []


def test_reparse_preserves_kb(tmp_path):
    """重解析保留知识库归属（meta 合并）。"""
    c, _ = _client(tmp_path)
    kb = c.post("/api/kbs", json={"name": "重解析"}).json()
    # 上传文件以产生留档原文
    content = "重解析文本：芒果榴莲是热带水果".encode()
    r = c.post("/api/documents",
               files={"file": ("doc.txt", content,
                               "text/plain")},
               data={"kb_id": kb["kb_id"]})
    assert r.json()["status"] == "ready"
    doc_id = r.json()["doc_id"]
    # 重解析
    r2 = c.post(f"/api/documents/{doc_id}/reparse", json={})
    assert r2.status_code == 200
    assert r2.json()["status"] == "ready"
    # 归属保留
    in_kb = c.get(
        f"/api/documents?kb_id={kb['kb_id']}").json()
    assert in_kb["total"] == 1
    assert in_kb["items"][0]["doc_id"] == doc_id


def test_kb_store_direct(tmp_path):
    """存储层 KB 函数直接测试（计数聚合）。"""
    with tempfile.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        kb = kbs.create_kb(store, "直接测试", "描述")
        assert kb["name"] == "直接测试"
        # 无文档时计数为 0
        lst = kbs.list_kbs(store)
        assert lst[0]["doc_count"] == 0
        # 更新
        upd = kbs.update_kb(store, kb["kb_id"],
                            name="改名")
        assert upd["name"] == "改名"
        # doc_ids_in_kb 空
        assert kbs.doc_ids_in_kb(store, kb["kb_id"]) == []
        # 删除空 KB
        assert kbs.delete_kb(store, kb["kb_id"]) is True
        assert kbs.get_kb(store, kb["kb_id"]) is None
        # 删除不存在的 KB
        assert kbs.delete_kb(store, "nope") is False
