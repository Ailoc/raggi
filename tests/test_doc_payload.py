"""文档详情按需带正文。

`text` 可能有 20MB：打开文档工作台并不需要它（原文从留档文件按需取），
却让每次详情请求都拖着整篇正文走一遍 SQLite 溢出页、JSON 序列化与
浏览器主线程解析。`include_text=false` 时既不投影该列也不返回它。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rag.api import Ctx, create_app
from rag.core.config import Settings
from rag.models.registry import ModelRegistry
from rag.storage.backend import LocalBackend
from rag.storage.meta import MetaStore
from rag.storage.repos.documents import get_document
from rag.storage.tables import LanceStore

LONG = "变频器额定电压 380V，允许波动 ±10%。" * 200


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
    c = TestClient(create_app(Ctx(settings, store, registry, meta=meta)))
    c.store = store  # type: ignore[attr-defined]
    return c


def _ingest(client) -> str:
    r = client.post("/api/documents/text",
                    json={"text": LONG, "title": "按需正文"})
    assert r.status_code == 200, r.text
    return r.json()["doc_id"]


def test_detail_can_skip_text(client):
    doc_id = _ingest(client)

    slim = client.get(f"/api/documents/{doc_id}",
                      params={"include_chunks": "false",
                              "include_text": "false"})
    assert slim.status_code == 200
    body = slim.json()
    assert body["has_text"] is True
    assert not body.get("text"), "include_text=false 仍带回了正文"
    assert len(slim.content) < 2000, "详情响应仍然过大"

    full = client.get(f"/api/documents/{doc_id}",
                      params={"include_text": "true"})
    assert full.json()["text"] == LONG


def test_repo_does_not_project_text_column(client):
    doc_id = _ingest(client)
    row = get_document(client.store, doc_id, include_text=False)
    assert row is not None
    assert "text" not in row, "没投影正文列，却仍然出现在返回行里"
    assert row["char_count"] > 0
