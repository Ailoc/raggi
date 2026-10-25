"""Redis / Valkey 集成测试（需要真实实例）。

默认套件不跑：设置 `RAG_TEST_REDIS_URL`（例如
`redis://127.0.0.1:63790/0`）后才会执行。这里验证两件在单进程里
测不出来的事：
1. 共享缓存的读写与 TTL；
2. **两个副本共享同一份额度**——同样是 3 个令牌，一个副本花完，
   另一个副本必须拒绝（进程内实现做不到这一点）。
"""
from __future__ import annotations

import os
import time
import uuid

import pytest

URL = os.environ.get("RAG_TEST_REDIS_URL", "")
pytestmark = pytest.mark.skipif(
    not URL, reason="未设置 RAG_TEST_REDIS_URL，跳过 Redis 集成测试")


def _ns() -> str:
    return f"raggi-test:{uuid.uuid4().hex}"


def test_shared_cache_roundtrip_and_ttl():
    from rag.core.redis import RedisCache

    cache = RedisCache(URL, namespace=_ns(), timeout_ms=500)
    assert cache.ping() is True
    cache.set("k", b"value", 30)
    assert cache.get("k") == b"value"

    cache.set("short", b"x", 0.15)
    time.sleep(0.4)
    assert cache.get("short") is None, "TTL 没生效，共享缓存会存成永久数据"

    cache.delete("k")
    assert cache.get("k") is None


def test_rate_limiter_shares_quota_across_replicas():
    from rag.core.ratelimit import RedisRateLimiter

    ns = _ns()
    a = RedisRateLimiter(URL, namespace=ns, limit=60, burst=3, timeout_ms=500)
    b = RedisRateLimiter(URL, namespace=ns, limit=60, burst=3, timeout_ms=500)
    assert a.enabled and b.enabled

    allowed = [a.allow("caller-1")[0] for _ in range(3)]
    assert allowed == [True, True, True]
    ok, retry = b.allow("caller-1")
    assert ok is False, "另一个副本拿到了独立额度：限流没有共享"
    assert retry >= 1
    # 不同调用方互不影响
    assert b.allow("caller-2")[0] is True


def test_layered_cache_shared_between_two_instances():
    """两个进程各自 L1 独立，但 L2 命中后不必回源。"""
    from rag.core.cache import LayeredCache, TTLCache
    from rag.core.redis import RedisCache

    ns = _ns()
    l2 = RedisCache(URL, namespace=ns, timeout_ms=500)
    a = LayeredCache(TTLCache(max_items=8, ttl_seconds=60), l2)
    b = LayeredCache(TTLCache(max_items=8, ttl_seconds=60), l2)

    a.get_or_load("q", lambda: [0.1, 0.2], ttl=60)
    value, _age, loaded = b.get_or_load(
        "q", lambda: (_ for _ in ()).throw(AssertionError("不该回源")))
    assert value == [0.1, 0.2] and loaded is False


# ---- Redis Streams 队列 -----------------------------------------------------


def _wait_until(pred, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return False


def _queue(store, runner, ns):
    from rag.ingest.redis_queue import RedisStreamQueue

    q = RedisStreamQueue(
        store, runner, url=URL, namespace=ns, stream="ingest", group="g",
        consumer="c1", max_pending=8, block_ms=200, claim_idle_ms=60_000,
        timeout_ms=500)
    q.ensure_group()
    return q


def test_stream_queue_submit_consume_ack(tmp_path):
    import threading

    from rag.ingest.jobspec import IngestJob
    from rag.storage.repos import jobs as jobs_repo
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    seen = []

    def runner(job, job_id=None, **_kw):
        seen.append((job, job_id))
        jobs_repo.set_job(store, job_id, stage="done", ended_at="now")
        return {"doc_id": "d"}

    q = _queue(store, runner, _ns())
    job_id, fut = q.submit_job(IngestJob(kind="text", text="hello", kb_id="kb"))
    assert fut is None, "外部队列不该返回本地 Future"
    assert jobs_repo.get_job(store, job_id)["stage"] == "queued"
    assert jobs_repo.get_job(store, job_id)["kb_id"] == "kb"

    t = threading.Thread(target=q.consume_forever, daemon=True)
    t.start()
    assert _wait_until(lambda: seen), "worker 没取到任务"
    q.shutdown()
    t.join(timeout=5)

    assert seen[0][0].text == "hello" and seen[0][1] == job_id
    assert jobs_repo.get_job(store, job_id)["stage"] == "done"
    assert _wait_until(lambda: q.stats()["pending"] == 0)


def test_stream_queue_cancel_skips_execution(tmp_path):
    import threading

    from rag.ingest.jobspec import IngestJob
    from rag.storage.repos import jobs as jobs_repo
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    called = []
    q = _queue(store, lambda *a, **k: called.append(1), _ns())
    job_id, _ = q.submit_job(IngestJob(kind="text", text="x"))

    assert q.cancel(job_id) == "cancelled"
    assert jobs_repo.get_job(store, job_id)["stage"] == "cancelled"

    t = threading.Thread(target=q.consume_forever, daemon=True)
    t.start()
    assert _wait_until(lambda: q.stats()["pending"] == 0), "被取消的消息没有被 ACK"
    q.shutdown()
    t.join(timeout=5)
    assert called == [], "取消的任务仍被执行了"


def test_stream_queue_runs_the_real_pipeline(tmp_path):
    """端到端：API 侧只入队，worker 侧跑完入库并写终态。"""
    import threading

    from rag.api import Ctx
    from rag.core.config import Settings
    from rag.ingest.jobspec import IngestJob, run_ingest_job
    from rag.models.registry import ModelRegistry
    from rag.storage.backend import LocalBackend
    from rag.storage.meta import MetaStore
    from rag.storage.repos import jobs as jobs_repo
    from rag.storage.tables import LanceStore

    class _E:
        dim = 4
        model = "stub"

        def embed_one(self, text):  # noqa: ARG002
            return [1.0, 0.0, 0.0, 0.0]

        def embed(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

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
        embedder=_E(), chat_factory=lambda: None, reranker=None,
        embed_cfg=settings.embed, llm_cfg=settings.llm,
        rerank_cfg=settings.rerank)
    ctx = Ctx(settings, store, registry, meta=meta)

    q = _queue(store, lambda job, job_id=None, **kw: run_ingest_job(
        ctx, job, job_id), _ns())
    ctx.queue = q

    job_id, _ = q.submit_job(IngestJob(
        kind="text", text="变频器额定电压 380V。" * 30, title="远程入库"))

    t = threading.Thread(target=q.consume_forever, daemon=True)
    t.start()
    assert _wait_until(
        lambda: (jobs_repo.get_job(store, job_id) or {}).get("stage") == "done",
        timeout=30), f"任务没有完成: {jobs_repo.get_job(store, job_id)}"
    q.shutdown()
    t.join(timeout=5)

    from rag.storage.repos.documents import docs_query

    docs = docs_query(store, ["title", "chunk_count"])
    assert [d["title"] for d in docs] == ["远程入库"]
    assert int(docs[0]["chunk_count"]) > 0
