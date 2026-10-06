"""入库队列的回归测试。

覆盖：
- 异步提交立即返回，不等解析与向量化完成（这正是「上传卡住」的根因）；
- 阶段流转与终态可查，异步调用方靠它显示进度；
- 产出的 doc_id 回写任务行（否则异步调用方找不到生成的文档）；
- 队列有界，满了快速失败而不是无限堆积；
- 同步模式行为不变（向后兼容）。
"""
from __future__ import annotations

import time

import pytest


class _SlowEmbedder:
    """带延迟的桩：让阶段推进可观测，也用来验证「立即返回」。"""
    dim = 4
    model = "stub"
    delay = 0.12

    def embed_one(self, text):  # noqa: ARG002
        time.sleep(self.delay)
        return [1.0, 0.0, 0.0, 0.0]

    def embed(self, texts):
        time.sleep(self.delay)
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class _VerySlowEmbedder(_SlowEmbedder):
    """用于「是否立刻返回」的断言：延迟放大到 800ms。

    为什么不用绝对墙钟阈值（比如 100ms）：那个数字会被**与流水线无关**
    的一次性成本击穿——Pydantic 校验器构建、LanceDB 首次写入、模块
    首次导入，实测同一份代码在 89~300ms 之间抖动，阈值一压就变成随机
    失败（曾因机器负载不同连续三次红）。改成「延迟 800ms 的流水线是否
    没被等完」：判据仍是同一件事（有没有阻塞在流水线里），但余量足够大，
    与环境噪声无关。
    """
    delay = 0.8


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    settings = Settings()
    settings.data_dir = tmp_path
    settings.embed.dim = 4
    registry = ModelRegistry(settings)
    registry.bundle.embedder = _SlowEmbedder()
    ctx = Ctx(settings, store, registry)
    c = TestClient(create_app(ctx), raise_server_exceptions=False)
    c.ctx = ctx  # type: ignore[attr-defined]
    return c


def _client_with(embedder, tmp_path):
    """用指定 embedder 构造客户端（隔离「立即返回」那类时延断言）。"""
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    settings = Settings()
    settings.data_dir = tmp_path
    settings.embed.dim = 4
    registry = ModelRegistry(settings)
    registry.bundle.embedder = embedder
    c = TestClient(create_app(Ctx(settings, store, registry)),
                   raise_server_exceptions=False)
    c.store = store  # type: ignore[attr-defined]
    return c


def _drain(client, job_id: str, timeout: float = 20.0) -> dict:
    """轮询到终态，返回最终任务状态。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get(f"/api/jobs/{job_id}").json()
        if st.get("terminal"):
            return st
        time.sleep(0.05)
    raise AssertionError(f"任务未在 {timeout}s 内结束: {st}")


# ---- 异步语义 ----------------------------------------------------------


def test_async_submit_returns_immediately(tmp_path):
    """wait=false 必须立刻返回，不能等解析与向量化跑完。

    回归防护：这正是「上传大文件时请求一直挂着」的根因——
    同步模式下 HTTP 连接要等到整条流水线结束。

    判据用「延迟 800ms 的流水线是否被等完」而不是绝对毫秒阈值：
    绝对阈值会被与流水线无关的一次性成本（校验器构建、LanceDB 首次
    写入）击穿，测的是环境噪声而不是回归。
    """
    c = _client_with(_VerySlowEmbedder(), tmp_path)
    t0 = time.perf_counter()
    r = c.post("/api/documents/text",
                json={"text": "异步入库内容 " * 20, "title": "async"},
                params={"wait": "false"})
    elapsed = (time.perf_counter() - t0) * 1000
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "queued"
    assert body["job_id"]
    # 同步跑完至少要过 embedder 的 800ms；留出充足余量仍能区分
    assert elapsed < 800 * 0.5, f"疑似等完了整条流水线（{elapsed:.0f}ms）"


def test_job_reaches_terminal_state(client):
    job_id = client.post(
        "/api/documents/text",
        json={"text": "任务终态测试 " * 20, "title": "t"},
        params={"wait": "false"}).json()["job_id"]
    final = _drain(client, job_id)
    assert final["stage"] == "done", final
    assert final["terminal"] is True


def test_job_records_produced_doc_id(client):
    """任务行必须回写产出的 doc_id。

    回归防护：异步调用方只拿得到 job_id，不回写就只能去文档列表里
    靠标题猜哪篇是刚入库的。
    """
    job_id = client.post(
        "/api/documents/text",
        json={"text": "回写 doc_id 测试 " * 20, "title": "t"},
        params={"wait": "false"}).json()["job_id"]
    final = _drain(client, job_id)
    assert final["doc_id"], "任务未回写 doc_id"
    # 该 doc_id 必须真实存在
    assert client.get(f"/api/documents/{final['doc_id']}").status_code == 200


def test_duplicate_ingest_records_hit_doc_id(client):
    """重复入库也要回写 doc_id——调用方据此知道命中了哪一篇。"""
    payload = {"text": "重复内容测试 " * 20, "title": "a"}
    first = _drain(client, client.post(
        "/api/documents/text", json=payload,
        params={"wait": "false"}).json()["job_id"])
    second = _drain(client, client.post(
        "/api/documents/text", json={**payload, "title": "b"},
        params={"wait": "false"}).json()["job_id"])
    assert second["doc_id"] == first["doc_id"], "重复入库未指向既有文档"


def test_stage_progress_observed(client):
    """阶段要可观测——前端靠它显示「解析中 / 向量化」而不是干等。"""
    job_id = client.post(
        "/api/documents/text",
        json={"text": "阶段观测 " * 30, "title": "t"},
        params={"wait": "false"}).json()["job_id"]
    seen = []
    deadline = time.time() + 20
    while time.time() < deadline:
        st = client.get(f"/api/jobs/{job_id}").json()
        if st["stage"] not in seen:
            seen.append(st["stage"])
        if st.get("terminal"):
            break
        time.sleep(0.02)
    assert "done" in seen
    assert len(seen) >= 2, f"阶段过于笼统，无法显示进度: {seen}"


def test_failed_job_reports_error(client, tmp_path):
    """失败任务要落终态并带错误信息，否则前端会一直轮询。"""
    class _Boom(_SlowEmbedder):
        def embed(self, texts):
            raise RuntimeError("embedding 服务挂了")

    client.ctx.registry.bundle.embedder = _Boom()
    job_id = client.post(
        "/api/documents/text",
        json={"text": "失败测试 " * 10, "title": "t"},
        params={"wait": "false"}).json()["job_id"]
    final = _drain(client, job_id)
    assert final["stage"] == "failed"
    assert "embedding" in (final["error"] or "")


# ---- 队列边界 ----------------------------------------------------------


def test_queue_rejects_when_full(tmp_path):
    """队列满时快速失败（503），不无限堆积。"""
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    settings = Settings()
    settings.data_dir = tmp_path
    settings.embed.dim = 4
    registry = ModelRegistry(settings)
    registry.bundle.embedder = _SlowEmbedder()
    ctx = Ctx(settings, store, registry)
    ctx.queue.max_pending = 1          # 压到最小便于触发
    ctx.queue.workers = 1
    c = TestClient(create_app(ctx), raise_server_exceptions=False)

    codes = []
    for i in range(4):
        r = c.post("/api/documents/text",
                   json={"text": f"满载测试 {i} " * 30, "title": f"t{i}"},
                   params={"wait": "false"})
        codes.append(r.status_code)
    assert 503 in codes, f"队列满时未拒绝: {codes}"


def test_unknown_job_is_404(client):
    assert client.get("/api/jobs/nope").status_code == 404


def test_job_list_and_active_filter(client):
    """任务列表可用，active_only 只返回未结束的任务。"""
    client.post("/api/documents/text",
                json={"text": "列表测试 " * 10, "title": "t"},
                params={"wait": "false"})
    lst = client.get("/api/jobs").json()
    assert lst["total"] >= 1
    assert "queue" in lst, "缺少队列状态"
    assert lst["queue"]["workers"] >= 1
    # 全部排空后 active_only 应为空
    time.sleep(1.2)
    act = client.get("/api/jobs", params={"active_only": "true"}).json()
    assert act["total"] == 0, f"仍有未结束任务: {act['items']}"


# ---- 向后兼容 ----------------------------------------------------------


def test_sync_mode_unchanged(client):
    """wait=true（默认）行为与引入队列前一致：直接拿结果。"""
    r = client.post("/api/documents/text",
                    json={"text": "同步模式测试 " * 10, "title": "t"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["doc_id"]
    assert body["chunk_count"] >= 1


def test_sync_failure_still_raises(client):
    """同步模式下失败要抛成 HTTP 错误，而不是静默返回。"""
    class _Boom(_SlowEmbedder):
        def embed(self, texts):
            raise RuntimeError("boom")

    client.ctx.registry.bundle.embedder = _Boom()
    r = client.post("/api/documents/text",
                    json={"text": "失败 " * 10, "title": "t"})
    assert r.status_code >= 400
    assert "boom" in r.text


def test_direct_pipeline_call_still_works(tmp_path):
    """不经队列直接调 pipeline 也要能用（测试与脚本的既有用法）。"""
    from rag.core.config import Settings
    from rag.ingest import pipeline
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    settings = Settings()
    settings.data_dir = tmp_path
    settings.embed.dim = 4
    store = LanceStore.for_data_dir(settings.data_dir, 4)
    registry = ModelRegistry(settings)
    registry.bundle.embedder = _SlowEmbedder()

    res = pipeline.ingest_text(store, registry, "直调测试内容 " * 5,
                               "direct", settings.parser, settings)
    assert res["status"] == "ready"
    # 自行建了任务行并走到终态
    jobs = store.jobs.search().select(["stage"]).to_list()
    assert any(j["stage"] == "done" for j in jobs)