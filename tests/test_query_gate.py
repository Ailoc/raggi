"""查询侧 embedding 闸门：容量背压与分段计时。

检索路径原先完全没有并发上限：几百个不同查询会同时打到 provider，
429 → 退避重试 → 所有人的 p95 一起崩。闸门把「排队」换成「快速失败」，
并把耗时拆成「模型往返」与「检索+组装」两段，慢在哪一眼能看出来。
"""
from __future__ import annotations

import threading
import time

import pytest

from rag.core.config import Settings
from rag.core.errors import GateBusy
from rag.models.embeddings import Embedder, build_embedder, query_gate


class _SlowLC:
    def __init__(self, delay: float):
        self.delay = delay
        self.calls = 0
        self._lock = threading.Lock()

    def embed_documents(self, texts):
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def _embedder(permits: int, delay: float, wait_ms: int = 50) -> Embedder:
    return Embedder(_SlowLC(delay), 4, "stub", False,
                    query_gate=query_gate(permits), gate_wait_ms=wait_ms)


def test_default_config_has_no_gate():
    """单机默认不限流：行为和改造前一致。"""
    settings = Settings()
    assert settings.embed.query_concurrency == 0
    assert build_embedder(settings.embed)._query_gate is None


def test_gate_rejects_different_query_when_full():
    emb = _embedder(permits=1, delay=0.4)
    errors: list[Exception] = []

    def hold():
        emb.embed_one("占用槽位的查询")

    t = threading.Thread(target=hold)
    t.start()
    time.sleep(0.05)  # 让占用者拿到槽位
    try:
        with pytest.raises(GateBusy):
            emb.embed_one("另一个查询")
    finally:
        t.join(timeout=3)
    assert errors == []


def test_same_query_collapses_before_the_gate():
    """相同查询在单飞锁上合流，不重复占槽位，也不会被自己的并发挤掉。"""
    emb = _embedder(permits=1, delay=0.15, wait_ms=200)
    results: list[list[float]] = []
    errors: list[Exception] = []

    def run():
        try:
            results.append(emb.embed_one("同一个热点查询"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert errors == [], f"相同查询被闸门拒了: {errors[:1]}"
    assert results == [[1.0, 0.0, 0.0, 0.0]] * 8
    assert emb.lc.calls == 1, "单飞合并失效"


def test_gate_busy_maps_to_503_with_retry_after(monkeypatch, tmp_path):
    """容量背压必须是 503 + Retry-After，而不是 500。"""
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.api import search as search_api
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    settings = Settings()
    settings.data_dir = tmp_path
    settings.token = ""
    settings.embed.dim = 4
    store = LanceStore.for_data_dir(tmp_path, 4)
    registry = ModelRegistry(settings)
    registry.bundle.embedder = _embedder(1, 0.0)
    client = TestClient(create_app(Ctx(settings, store, registry)))

    monkeypatch.setattr(search_api, "search",
                        lambda *a, **k: (_ for _ in ()).throw(
                            GateBusy("检索并发已满")))
    r = client.post("/api/search", json={"q": "x", "top_k": 3})
    assert r.status_code == 503
    assert r.headers.get("retry-after") == "1"
    assert "并发已满" in r.text


def test_search_reports_split_timings(monkeypatch):
    """took_ms 必须包含 embedding；embed_ms/search_ms 加起来对得上。"""
    from rag.retrieval import search as svc

    monkeypatch.setattr(svc, "chunk_prefilter", lambda store, filters: None)
    monkeypatch.setattr(svc, "search_chunks", lambda *a, **k: [])
    monkeypatch.setattr(svc, "_doc_meta", lambda store, ids: {})
    monkeypatch.setattr(svc, "_contexts", lambda store, rows, win: {})

    emb = _embedder(permits=2, delay=0.08)
    out = svc.search(object(), emb, None, "计时探针", Settings().retrieve, None, 3)

    assert out["embed_ms"] >= 70, f"embedding 没被计入: {out['embed_ms']}"
    assert out["search_ms"] >= 0
    assert out["took_ms"] >= out["embed_ms"], "took_ms 仍是「embed 之后」的旧口径"
    assert abs(out["took_ms"] - (out["embed_ms"] + out["search_ms"])) < 1.0
