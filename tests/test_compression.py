"""响应压缩：大响应必须压，小响应不压，SSE 绝不能压。

首屏 JS 634KB + CSS 87KB 原先原样上线（实测无 `content-encoding`）。
反过来，几百字节的 JSON 压了只会更慢——`minimum_size` 就是这条线。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rag.api import Ctx, create_app
from rag.core.config import Settings
from rag.models.registry import ModelRegistry
from rag.storage.backend import LocalBackend
from rag.storage.meta import MetaStore
from rag.storage.tables import LanceStore


class _StubEmbedder:
    dim = 4
    model = "stub"

    def embed_one(self, text):  # noqa: ARG002
        return [1.0, 0.0, 0.0, 0.0]

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


@pytest.fixture()
def client(tmp_path):
    settings = Settings()
    settings.data_dir = tmp_path
    settings.token = ""
    settings.embed.dim = 4
    settings.embed.model = "stub"
    store = LanceStore(LocalBackend(tmp_path), 4)
    meta = MetaStore(tmp_path / "raggi.db")
    store.meta = meta
    registry = ModelRegistry(settings)
    registry.bundle = type(registry.bundle)(
        embedder=_StubEmbedder(), chat_factory=lambda: None,
        reranker=None, embed_cfg=settings.embed, llm_cfg=settings.llm,
        rerank_cfg=settings.rerank)
    return TestClient(create_app(Ctx(settings, store, registry, meta=meta)))


def test_large_json_is_gzipped(client):
    r = client.post("/api/documents/text", json={
        "text": "变频器额定电压 380V，允许波动 ±10%。" * 400,
        "title": "压缩测试文档",
    })
    assert r.status_code == 200, r.text
    doc_id = r.json()["doc_id"]

    plain = client.get(f"/api/documents/{doc_id}",
                       headers={"Accept-Encoding": "identity"})
    gzipped = client.get(f"/api/documents/{doc_id}",
                         headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in plain.headers
    assert gzipped.headers.get("content-encoding") == "gzip"
    # httpx 的 TestClient 会自动解压，所以这里只钉协议头；
    # 真正省了多少字节由浏览器/网关决定（正文重复度高，压缩比很大）。


def test_small_json_is_left_alone(client):
    """几百字节的 JSON 不值得压：省不下字节，还多一次 CPU 与解压。"""
    r = client.get("/api/health", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    assert "content-encoding" not in r.headers
