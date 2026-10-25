"""在飞请求准入控制。

应用层的排队没有上限：上游重试风暴打进来时，所有请求都被接住，最后
一起超时。准入控制把「无限排队」换成「快速拒绝」——超限 503 +
Retry-After；健康探针永远放行（否则编排会把实例判死）。
"""
from __future__ import annotations

import threading
import time

import pytest

from rag.core.admission import InflightGate
from rag.core.config import Settings


def test_gate_counts_and_rejects():
    gate = InflightGate(2)
    assert gate.enter() and gate.enter()
    assert gate.enter() is False
    gate.leave()
    assert gate.enter() is True
    assert gate.snapshot() == {"inflight": 2, "max_inflight": 2,
                               "peak_inflight": 2}


def test_gate_disabled_by_default():
    assert InflightGate(0).enabled is False
    assert InflightGate(0).enter() is True      # 不计数、也不拒绝
    assert InflightGate(0).snapshot()["inflight"] == 0
    assert Settings().api_max_inflight == 0


@pytest.fixture()
def app_client(tmp_path):
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    settings = Settings()
    settings.data_dir = tmp_path
    settings.token = ""
    settings.embed.dim = 4
    settings.api_max_inflight = 1
    store = LanceStore.for_data_dir(tmp_path, 4)
    registry = ModelRegistry(settings)
    app = create_app(Ctx(settings, store, registry))
    return app, TestClient(app)


def test_second_request_gets_503_while_first_in_flight(app_client, monkeypatch):
    """max_inflight=1：第一个请求占着槽位时，第二个必须快速 503。"""
    from fastapi.testclient import TestClient

    import rag.api.search as search_api

    app, client = app_client
    entered = threading.Event()
    release = threading.Event()

    def slow_search(*_a, **_k):
        entered.set()
        release.wait(5)
        return {"query": "x", "took_ms": 1.0, "mode": "hybrid",
                "score_kind": "none", "total_candidates": 0, "offset": 0,
                "top_k": 3, "has_more": False, "results": []}

    monkeypatch.setattr(search_api, "search", slow_search)

    first: dict = {}

    def hold():
        first["r"] = client.post("/api/search", json={"q": "x", "top_k": 3})

    t = threading.Thread(target=hold)
    t.start()
    assert entered.wait(5), "第一个请求没有进入 handler"

    second = TestClient(app).post("/api/search", json={"q": "y", "top_k": 3})
    assert second.status_code == 503
    assert second.headers.get("retry-after") == "1"
    assert second.headers.get("x-request-id")
    assert "在飞请求已达上限" in second.text

    # 健康探针不受准入控制影响
    assert TestClient(app).get("/api/health").status_code == 200

    release.set()
    t.join(timeout=5)
    assert first["r"].status_code == 200
    # 槽位已经归还，下一请求正常
    assert TestClient(app).post("/api/search", json={"q": "z"}).status_code == 200


def test_stats_exposes_inflight(app_client):
    _app, client = app_client
    body = client.get("/api/stats").json()
    assert body["max_inflight"] == 1
    assert body["inflight"] == 0
    assert body["peak_inflight"] >= 0


def test_slow_streaming_holds_the_slot_until_done(app_client, monkeypatch):
    """SSE 长连接同样占槽位：它消费的是连接与内存，不是只有 CPU。"""
    import rag.api.search as search_api

    _app, client = app_client
    entered = threading.Event()
    release = threading.Event()

    def slow(*_a, **_k):
        entered.set()
        release.wait(5)
        return {"query": "x", "took_ms": 1.0, "mode": "hybrid",
                "score_kind": "none", "total_candidates": 0, "offset": 0,
                "top_k": 3, "has_more": False, "results": []}

    monkeypatch.setattr(search_api, "search", slow)
    t = threading.Thread(target=lambda: client.post("/api/search", json={"q": "x"}))
    t.start()
    assert entered.wait(5)
    time.sleep(0.05)
    from fastapi.testclient import TestClient
    assert TestClient(_app).post("/api/search", json={"q": "y"}).status_code == 503
    release.set()
    t.join(timeout=5)
