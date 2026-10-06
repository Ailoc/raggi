"""批量操作端点：分块批量启停 / 文档批量删除。

对应「批量操作」补齐：前端原先在客户端循环调单条端点——200 条分块就是
200 个请求且无原子性，中途失败后「哪些块被停用了」无法回答。
这些测试锁住「一次写入」与「如实回报」两个关键语义。
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    class _E:
        dim = 4
        model = "stub"

        def embed_one(self, text):  # noqa: ARG002
            return [1.0, 0.0, 0.0, 0.0]

        def embed(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    store = LanceStore.for_data_dir(tmp_path, 4)
    settings = Settings()
    settings.data_dir = tmp_path
    settings.embed.dim = 4
    registry = ModelRegistry(settings)
    registry.bundle.embedder = _E()
    return TestClient(create_app(Ctx(settings, store, registry)),
                      raise_server_exceptions=False)


def _mkdoc(client, title: str, chunk_size: int = 64) -> str:
    """建一篇文档，返回 doc_id（分块数 ≥1）。"""
    return client.post("/api/v1/documents/text", json={
        "text": f"{title}的内容。" * chunk_size,
        "title": title,
    }).json()["doc_id"]


def _chunk_ids(client, doc_id: str) -> list[str]:
    r = client.get(f"/api/v1/chunks?doc_id={doc_id}&limit=500")
    return [c["chunk_id"] for c in r.json()["items"]]


# ---- 分块批量启停 ------------------------------------------------------


def test_batch_disable_then_enable(client):
    """批量启停一次完成，且状态真的落库。"""
    doc = _mkdoc(client, "批量启停")
    ids = _chunk_ids(client, doc)
    assert ids

    r = client.patch("/api/v1/chunks/batch-enabled",
                     json={"chunk_ids": ids, "enabled": False})
    assert r.status_code == 200, r.text
    assert r.json() == {"updated": len(ids), "enabled": False}

    after = client.get(
        f"/api/v1/chunks?doc_id={doc}&include_disabled=true").json()["items"]
    assert [c["enabled"] for c in after] == [False] * len(ids)

    client.patch("/api/v1/chunks/batch-enabled",
                 json={"chunk_ids": ids, "enabled": True})
    after = client.get(
        f"/api/v1/chunks?doc_id={doc}&include_disabled=true").json()["items"]
    assert [c["enabled"] for c in after] == [True] * len(ids)


def test_batch_enabled_excludes_from_search(client):
    """批量停用后该块不再参与检索——这是停用的实际语义。"""
    doc = _mkdoc(client, "检索验证")
    ids = _chunk_ids(client, doc)
    client.patch("/api/v1/chunks/batch-enabled",
                 json={"chunk_ids": ids, "enabled": False})
    res = client.post("/api/v1/search", json={"q": "检索验证", "top_k": 20})
    assert res.status_code == 200, res.text
    assert all(h["chunk_id"] not in ids for h in res.json()["results"])


def test_batch_enabled_is_all_or_nothing(client):
    """任一 ID 不存在即整体 404，不做部分成功。

    回归防护：部分成功最糟——用户以为全停用了，实际有一半还在检索里。
    """
    doc = _mkdoc(client, "原子性")
    ids = _chunk_ids(client, doc)
    r = client.patch("/api/v1/chunks/batch-enabled",
                     json={"chunk_ids": ids + ["ghost"], "enabled": False})
    assert r.status_code == 404, r.text
    # 未发生任何改动
    after = client.get(
        f"/api/v1/chunks?doc_id={doc}&include_disabled=true").json()["items"]
    assert all(c["enabled"] for c in after)


def test_batch_enabled_requires_ids(client):
    """空列表 422：批量端点不接受「什么都不做」。"""
    r = client.patch("/api/v1/chunks/batch-enabled",
                     json={"chunk_ids": [], "enabled": False})
    assert r.status_code == 422


def test_batch_enabled_deduplicates(client):
    """重复 ID 只算一次——否则 updated 会虚高。"""
    doc = _mkdoc(client, "去重")
    ids = _chunk_ids(client, doc)
    dup = ids + ids
    r = client.patch("/api/v1/chunks/batch-enabled",
                     json={"chunk_ids": dup, "enabled": False})
    assert r.json()["updated"] == len(ids)


# ---- 文档批量删除 ------------------------------------------------------


def test_batch_delete_documents(client):
    """批量删除级联分块，且健康检查不留孤儿。"""
    ids = [_mkdoc(client, f"批量{i}") for i in range(3)]
    before = client.get("/api/v1/health").json()
    assert before["chunk_count"] >= 3

    r = client.request("DELETE", "/api/v1/documents/batch",
                       json={"doc_ids": ids[:2]})
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": 2, "skipped": 0}

    left = {d["doc_id"] for d in client.get("/api/v1/documents").json()["items"]}
    assert left == set(ids[2:])
    health = client.get("/api/v1/health").json()
    assert health["orphan_chunks"] == 0, "批量删除留下了孤儿分块"
    assert health["count_mismatch"] == []


def test_batch_delete_reports_skipped(client):
    """已不存在的 ID 被跳过并如实回报，而不是假装全删成功。"""
    doc = _mkdoc(client, "存在的")
    r = client.request("DELETE", "/api/v1/documents/batch",
                       json={"doc_ids": [doc, "ghost-1", "ghost-2"]})
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": 1, "skipped": 2}


def test_batch_delete_all_ghost(client):
    """全部不存在 → deleted=0 且不报错（幂等）。"""
    r = client.request("DELETE", "/api/v1/documents/batch",
                       json={"doc_ids": ["x", "y"]})
    assert r.status_code == 200
    assert r.json() == {"deleted": 0, "skipped": 2}


def test_batch_delete_requires_ids(client):
    r = client.request("DELETE", "/api/v1/documents/batch",
                       json={"doc_ids": []})
    assert r.status_code == 422


def test_batch_delete_updates_kb_counts(client):
    """文档删掉后知识库的 doc_count 要同步。"""
    kb = client.post("/api/v1/kbs", json={"name": "计数库"}).json()
    kb_id = kb["kb_id"]
    doc = client.post("/api/v1/documents/text", json={
        "text": "计数用正文。" * 40, "title": "计数", "kb_id": kb_id,
    }).json()["doc_id"]

    before = client.get(f"/api/v1/kbs/{kb_id}").json()["doc_count"]
    assert before == 1
    client.request("DELETE", "/api/v1/documents/batch", json={"doc_ids": [doc]})
    after = client.get(f"/api/v1/kbs/{kb_id}").json()["doc_count"]
    assert after == 0, "批量删除后知识库文档数未回写"


def test_single_delete_endpoint_works(client):
    """单条删除端点自身可用。

    回归防护：加批量端点时曾把单条用的导入误删，NameError 只在**运行时**
    触发——批量删除把数据清空后没人再调单条路径，测试因此全绿，
    直到实测逐条删除才暴出 500。
    """
    doc = _mkdoc(client, "单条删除")
    r = client.delete(f"/api/v1/documents/{doc}")
    assert r.status_code == 200, r.text
    assert client.get(f"/api/v1/documents/{doc}").status_code == 404
