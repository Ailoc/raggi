"""入库队列的回归测试。

覆盖：
- 异步提交立即返回，不等解析与向量化完成（这正是「上传卡住」的根因）；
- 阶段流转与终态可查，异步调用方靠它显示进度；
- 产出的 doc_id 回写任务行（否则异步调用方找不到生成的文档）；
- 队列有界，满了快速失败而不是无限堆积；
- 同步模式行为不变（向后兼容）。
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

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


# ---- 阶段可观测（不赌时序的版本）---------------------------------------
# 这里原来是一条 `test_stage_progress_observed`：靠「桩睡 0.12s vs 每 20ms 轮询」
# 制造观测窗口。它在全量套件里红过一次（`seen == ['done']`）而单跑 5/5 绿 ——
# 负载相关的偶发红。同一个断言改成下面这条事件闸门版本，语义没削弱，
# 只是不再依赖谁先跑完。


class _GatedEmbedder:
    """provider 调用卡在一个事件上，直到测试确认「已经看到了中间阶段」。"""
    dim = 4
    model = "stub"

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def _gate(self):
        self.entered.set()
        # 测试必须放行，否则这条 job 永不终态 —— 用超时而不是无限等
        if not self.release.wait(timeout=20.0):
            raise RuntimeError("测试未放行 embedding 闸门")
        return [1.0, 0.0, 0.0, 0.0]

    def embed_one(self, text):  # noqa: ARG002
        return self._gate()

    def embed(self, texts):
        vec = self._gate()
        return [vec for _ in texts]


def test_stage_is_observable_without_racing_the_clock(tmp_path):
    """阶段可观测，且**不依赖**「谁先跑完」：把 provider 卡住再轮询。"""
    gated = _GatedEmbedder()
    c = _client_with(gated, tmp_path)
    job_id = c.post(
        "/api/documents/text",
        json={"text": "确定性阶段 " * 30, "title": "t"},
        params={"wait": "false"}).json()["job_id"]
    seen: list[str] = []
    try:
        assert gated.entered.wait(timeout=20.0), "job 没走到 embedding"
        # 此刻 provider 被卡住 ⇒ 任务必然还停在向量化那一步，与机器快慢无关。
        # 断言具体阶段名而不是「非终态就行」：后者在「整条流水线只在结束时写一次
        # 任务行」的回归下仍然绿（它会看到创建时的 queued），那就等于没测进度。
        st = c.get(f"/api/jobs/{job_id}").json()
        seen.append(st["stage"])
        assert st["stage"] == "embed", (
            f"provider 还卡着，阶段却不是 embed：{st}（前端进度条会停在错误处）")
        assert not st.get("terminal"), f"provider 还卡着就已经终态：{st}"
    finally:
        gated.release.set()
    final = _drain(c, job_id)
    seen.append(final["stage"])
    assert final["stage"] == "done"
    assert len(set(seen)) >= 2, f"阶段过于笼统，无法显示进度: {seen}"


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

# ---- embedding 并发闸 ---------------------------------------------------
# 闸门存在的理由（pipeline.py 上方注释原话）：外层任务数 × 内层分片数会
# 相乘，把远端 embedding 打到限流，然后每个分片各自退避重试 3 次 ——
# 级联重试风暴。这个乘积只有在闸门是**进程级**时才会被封顶。


class _CountingEmbedder:
    """记录同时在途的请求数峰值与每次条数；每个请求睡 50ms 以保证重叠。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._inflight = 0
        self.peak = 0
        self.calls = 0
        self.sizes: list[int] = []

    def embed(self, texts):
        with self._lock:
            self._inflight += 1
            self.calls += 1
            self.sizes.append(len(texts))
            self.peak = max(self.peak, self._inflight)
        time.sleep(0.05)
        with self._lock:
            self._inflight -= 1
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]


@pytest.fixture
def fresh_gates():
    """进程级单例必须能被隔离，否则一条测试的闸会漏进下一条。"""
    from rag.ingest import pipeline

    pipeline._GATES.clear()
    yield pipeline._GATES
    pipeline._GATES.clear()


def test_embed_gate_is_reused_within_the_process(fresh_gates):
    from rag.core.config import EmbedConfig
    from rag.ingest import pipeline

    cfg = EmbedConfig(concurrency=3)
    g1 = pipeline._embed_gate(cfg)
    g2 = pipeline._embed_gate(cfg)
    assert g1 is g2, "每次新建一把 = 每个文档各一张独立门票，乘积没被封顶"
    assert g1.permits == 6
    # 不同尺寸各算各的闸（配置在测试里会变，不该互相踩）
    assert pipeline._embed_gate(EmbedConfig(concurrency=3)) is g1
    assert pipeline._embed_gate(EmbedConfig(concurrency=5)).permits == 10
    assert len(fresh_gates) == 2


def test_concurrent_ingests_share_one_gate(fresh_gates):
    """4 个文档 × 每个 4 分片：峰值在途必须 <= permits，而不是 16。"""
    from rag.core.config import EmbedConfig
    from rag.ingest import pipeline

    cfg = EmbedConfig(concurrency=2, batch=8)   # permits = 4
    emb = _CountingEmbedder()

    def one(i):
        return pipeline._embed_concurrently(
            emb, [f"t{i}-{j}" for j in range(32)], cfg)

    with ThreadPoolExecutor(max_workers=4) as ex:
        outs = list(ex.map(one, range(4)))
    assert all(len(o) == 32 for o in outs), "分片结果必须按原顺序拼回"
    assert emb.calls == 16
    # 闸真的在放行并发（否则 peak==1，这条测试就退化成「串行也算过」）
    assert emb.peak > 1, "峰值 1 说明根本没并发，测不到闸门"
    assert emb.peak <= min(cfg.concurrency * 2, 64), (
        f"峰值在途 {emb.peak} > 闸门 {cfg.concurrency * 2}："
        "每个文档各自建闸，「任务数 × 分片数」没有被封顶")


def test_embed_batches_by_configured_size(fresh_gates):
    """`embed.batch` 只有一个读取点，所以它必须有直接断言。

    B6 的前提（「并发入库时每个分片各打一条 HTTP」）就是被这条事实否掉的：
    200 chunks / batch=64 → 4 次请求 [64,64,64,8]，不是 200 次。
    哪天合批被改坏（退化成 per-chunk），没有任何测试会红 —— 不可接受。
    """
    from rag.core.config import EmbedConfig
    from rag.ingest import pipeline

    cfg = EmbedConfig(batch=64, concurrency=4)
    emb = _CountingEmbedder()
    out = pipeline._embed_concurrently(emb, [f"t{i}" for i in range(200)], cfg)
    assert len(out) == 200
    # 分片是并发发出的，到达顺序不确定 —— 比形状，不比顺序
    # （返回值的顺序由 `ex.map` 保证，上面 `len(out)` 那条只验证没丢）
    assert sorted(emb.sizes, reverse=True) == [64, 64, 64, 8], \
        f"合批形状变了：{emb.sizes}"
    # 短文档必须一次打完（合批的收益就在这里，别退回逐条）
    emb2 = _CountingEmbedder()
    pipeline._embed_concurrently(emb2, [f"t{i}" for i in range(15)], cfg)
    assert emb2.sizes == [15]


def test_gate_actually_throttles_when_shared(fresh_gates, monkeypatch):
    """变异检查：把闸门退回「每次新建」，上面的封顶断言必须失效。

    这条不是冗余——它证明前一条测试测的是闸门本身，而不是恰好线程数少。
    """
    from rag.core.config import EmbedConfig
    from rag.ingest import pipeline
    from rag.models.http import ScaledSemaphore

    monkeypatch.setattr(pipeline, "_embed_gate",
                        lambda cfg: ScaledSemaphore(4))
    emb = _CountingEmbedder()
    cfg = EmbedConfig(concurrency=2, batch=8)

    def one(i):
        return pipeline._embed_concurrently(
            emb, [f"t{i}-{j}" for j in range(32)], cfg)

    with ThreadPoolExecutor(max_workers=4) as ex:
        list(ex.map(one, range(4)))
    # 每文档一把闸 → 4 文档 × 2 worker = 8 在途，远超 permits=4
    assert emb.peak > min(cfg.concurrency * 2, 64), (
        "退回 per-call 建闸后峰值仍被封顶，说明并发度不够，前一条测试是假的")
