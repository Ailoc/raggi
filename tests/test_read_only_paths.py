"""读语义的 POST 不得被当成写。

`POST /api/search`、`/api/answer`、`/api/embed` 都不改数据，但中间件只看
方法名：每次检索都会递增 SQLite 的 `write_epoch` 并把 health/stats 快照
清空。后果有两层——读请求占了跨进程写锁；观测端点每次检索后都要冷算
一遍 O(N) 对账（实测 health 热 3.3ms → 检索后 20.1ms）。

这里用两条互相独立的判据钉住：写代际不前进，且健康快照的年龄继续增长
（`X-Snapshot-Age-Ms` 不是「刚被重建」的 0）。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from rag.api import Ctx, _is_read_only_post, create_app
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


def _epoch(client) -> int:
    return client.app.state.ctx.meta.state_int("write_epoch")


def _snapshot_age(client) -> float:
    r = client.get("/api/health")
    assert r.status_code == 200
    return float(r.headers["X-Snapshot-Age-Ms"])


def test_search_neither_bumps_epoch_nor_cools_health(client):
    assert _snapshot_age(client) < 50          # 先算一份热快照
    before = _epoch(client)
    time.sleep(0.06)

    r = client.post("/api/search", json={"q": "苹果", "top_k": 3})
    assert r.status_code == 200

    assert _epoch(client) == before, "检索不该递增写代际"
    assert _snapshot_age(client) > 30, "检索把健康快照打冷了：观测端点又要全表对账"


def test_versioned_read_paths_behave_the_same(client):
    """`/api/v1/...` 是规范前缀，豁免不能只写在 `/api` 上。"""
    before = _epoch(client)
    r = client.post("/api/v1/search", json={"q": "苹果", "top_k": 3})
    assert r.status_code == 200
    assert _epoch(client) == before


def test_real_write_still_invalidates(client):
    """豁免只给读路径：真正写一次，写代际与快照都必须更新。"""
    assert _snapshot_age(client) < 50
    before = _epoch(client)
    r = client.post("/api/kbs", json={"name": "写路径回归库"})
    assert r.status_code == 200
    assert _epoch(client) == before + 1
    assert _snapshot_age(client) < 30, "写入后仍要重算快照，否则界面会显示旧账"


@pytest.mark.parametrize("path", [
    "/api/search", "/api/v1/search",
    "/api/search/report", "/api/v1/search/report",
    "/api/answer", "/api/v1/answer",
    "/api/answer/stream", "/api/v1/answer/stream",
    "/api/embed", "/api/v1/embed",
])
def test_read_only_predicate_accepts(path):
    assert _is_read_only_post(path)


@pytest.mark.parametrize("path", [
    "/api/kbs", "/api/v1/kbs",
    "/api/documents/text", "/api/chunks",
    "/api/v1/models", "/api/models/test",
    "/assets/index.js", "/",
])
def test_read_only_predicate_rejects(path):
    assert not _is_read_only_post(path)
