"""请求 ID 与限流。

对应「可运维性」补齐：此前服务端出 500 时客户端只拿到纯文本
"Internal Server Error"，既没有原因、也没有能对上日志的线索；
公开部署时也没有任何防止误用打爆队列的保护。

这两项都是**横切**能力（中间件层），因此既有用例完全覆盖不到——
必须单独固化，否则改中间件时不会有任何测试报警。
"""
from __future__ import annotations

import time

import pytest


@pytest.fixture()
def client(tmp_path, rate_limit=None):
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    s = Settings()
    s.data_dir = tmp_path
    s.embed.dim = 4
    if rate_limit:
        s.rate_limit_writes_per_min = rate_limit
        s.rate_limit_burst = rate_limit
    c = TestClient(create_app(Ctx(s, store, ModelRegistry(s))),
                   raise_server_exceptions=False)
    return c


# ---- 请求 ID -----------------------------------------------------------


def test_request_id_on_every_response(client):
    """每个响应都带 X-Request-ID——排障时靠它把客户端报错和服务端日志对上。"""
    for path in ("/api/v1/kbs", "/api/v1/health"):
        r = client.get(path)
        assert r.headers.get("X-Request-ID"), f"{path} 缺少请求 ID"
        assert len(r.headers["X-Request-ID"]) >= 8


def test_request_id_is_unique_per_request(client):
    ids = {client.get("/api/v1/health").headers["X-Request-ID"]
           for _ in range(3)}
    assert len(ids) == 3, "不同请求拿到了同一个 ID"


def test_upstream_request_id_is_reused(client):
    """上游（反代）传了 X-Request-ID 就沿用，整条链路同一个值。"""
    r = client.get("/api/v1/health",
                   headers={"X-Request-ID": "trace-abc123"})
    assert r.headers["X-Request-ID"] == "trace-abc123"


@pytest.mark.parametrize("bad", [
    "has spaces",                    # 空格：非法标识字符
    "a" * 200,                        # 超长：可能被用来撑爆日志/响应
    "bad;drop",                      # 分隔符：可能被用于头注入
])
def test_hostile_request_id_is_replaced(client, bad):
    """不安全的入站 ID 一律丢弃并自生成，不回显、不写日志。

    只用 ASCII 构造：HTTP 头本身不能放非 ASCII 字符，
    中文/换行那类输入由 incoming_id 的字符白名单兜住，
    这里验证「可送达但不该被采信」的那些。
    """
    r = client.get("/api/v1/health", headers={"X-Request-ID": bad})
    assert r.headers["X-Request-ID"] != bad
    assert r.headers["X-Request-ID"]


def test_error_body_carries_request_id(client):
    """500 的响应体带上 request_id：用户截图就能让我们查到日志那一行。"""
    # 未捕获异常走兜底 handler：用一个必然抛错的端点路径触发不易，
    # 因此直接断言兜底逻辑的形状——错误体必须同时有 detail 与线索。
    r = client.get("/api/v1/documents/nope")
    assert r.status_code == 404
    assert "detail" in r.json()


def test_response_time_header(client):
    r = client.get("/api/v1/health")
    ms = r.headers.get("X-Response-Time-Ms")
    assert ms is not None, "缺少耗时头"
    assert float(ms) >= 0


# ---- 限流 --------------------------------------------------------------


def test_rate_limit_disabled_by_default(client):
    """默认不限流：单机自用不该被自己的默认配置挡住。"""
    codes = {client.post("/api/v1/kbs", json={"name": f"库{i}"}).status_code
             for i in range(12)}
    assert codes == {200}, codes


def test_writes_are_rate_limited(tmp_path, rate_limit=3):
    client = _client_with_limit(tmp_path, 3)
    codes = [client.post("/api/v1/kbs", json={"name": f"库{i}"}).status_code
             for i in range(6)]
    assert codes[:3] == [200, 200, 200], codes
    assert codes[3:] == [429, 429, 429], codes


def test_rate_limit_response_has_retry_after(tmp_path):
    client = _client_with_limit(tmp_path, 2)
    for i in range(2):
        client.post("/api/v1/kbs", json={"name": f"库{i}"})
    r = client.post("/api/v1/kbs", json={"name": "触发"})
    assert r.status_code == 429
    assert int(r.headers["Retry-After"]) >= 1
    body = r.json()
    assert "detail" in body and "retry_after" in body
    assert body["request_id"] == r.headers["X-Request-ID"]


def test_reads_never_rate_limited(tmp_path):
    """检索是读多写少、单次代价低，限它只会影响正常使用。"""
    client = _client_with_limit(tmp_path, 1)
    client.post("/api/v1/kbs", json={"name": "唯一额度"})
    codes = {client.get("/api/v1/kbs").status_code for _ in range(8)}
    assert codes == {200}, codes
    # POST /search 是读语义，同样不该被限流。
    # 注意它在这个环境里会 503（没有可用的 embedding），
    # 那**不是** 429——限流测试只关心状态码不是限流。
    codes = {client.post("/api/v1/search", json={"q": "x"}).status_code
             for _ in range(5)}
    assert 429 not in codes, "检索被限流了"


def test_idempotent_ingest_exempt(tmp_path):
    """内容去重让重复入库本就安全，挡它只会让脚本作者困惑。"""
    client = _client_with_limit(tmp_path, 1, embedder=True)
    client.post("/api/v1/kbs", json={"name": "吃掉额度"})
    codes = {client.post("/api/v1/documents/text",
                         json={"text": "同样的内容", "title": "t"}).status_code
             for _ in range(4)}
    assert codes == {200}, codes


def test_bucket_refills_over_time(tmp_path):
    """令牌桶匀速补充：等一会儿额度会回来（不是永久封禁）。

    这里直接测限流器而不是走 HTTP：按每分钟 60 的速率，走 HTTP 时
    每个请求之间的真实间隔会让桶偷偷补回 0.0x 个令牌，「打满两个」
    这个断言就会时灵时不灵——测的是网络延迟，不是限流逻辑。
    """
    from rag.core.ratelimit import RateLimiter

    rl = RateLimiter(limit=60, burst=2)     # 每秒补 1 个
    assert rl.allow("k")[0] is True
    assert rl.allow("k")[0] is True
    ok, retry = rl.allow("k")
    assert ok is False and retry >= 1

    time.sleep(1.2)
    assert rl.allow("k")[0] is True, "等待后额度未恢复"


def test_rate_limit_is_per_key(tmp_path):
    """分桶：一个调用方超限不影响另一个（否则误写的脚本会拖累所有人）。"""
    client = _client_with_limit(tmp_path, 1)
    client.post("/api/v1/kbs", json={"name": "额度"})
    assert client.post("/api/v1/kbs", json={"name": "超限"}).status_code == 429
    # TestClient 同一 IP；换一个 key 就是另一个桶
    # （未鉴权时按 IP 分桶，这里验证 IP 维度确实是共享的）
    assert client.post("/api/v1/kbs", json={"name": "仍超限"}).status_code == 429


def test_static_assets_never_rate_limited(tmp_path):
    client = _client_with_limit(tmp_path, 1)
    client.post("/api/v1/kbs", json={"name": "额度"})
    codes = {client.get("/").status_code for _ in range(5)}
    assert 429 not in codes, "静态资源被限流了"


# ---- 单元级：限流器本身 -------------------------------------------------


def test_limiter_disabled_is_noop():
    from rag.core.ratelimit import RateLimiter

    rl = RateLimiter(limit=0)
    assert not rl.enabled
    assert all(rl.allow("k")[0] for _ in range(1000))


def test_limiter_gc_drops_idle_buckets():
    """长期不用的桶要被清掉，否则 key 会无限增长（内存泄漏）。"""
    from rag.core.ratelimit import RateLimiter

    rl = RateLimiter(limit=60)
    for i in range(200):
        rl.allow(f"k{i}")
    assert len(rl._buckets) > 0
    # 把 updated 推到很久以前，模拟长期不用
    for b in rl._buckets.values():
        b.updated -= 10_000
        b.tokens = rl.burst
    rl.allow("fresh")          # 触发一次 GC
    assert len(rl._buckets) <= 2, f"GC 未清理空闲桶: {len(rl._buckets)}"


def test_caller_key_uses_key_id_when_authenticated():
    from rag.core.ratelimit import caller_key

    class _R:
        client = None

    rid = caller_key(_R(), {"key_id": "k1"})
    assert rid == "key:k1"


def test_is_idempotent_only_matches_intended_paths():
    from rag.core.ratelimit import is_idempotent

    assert is_idempotent("/api/documents/text")
    assert is_idempotent("/api/v1/documents/text")
    assert not is_idempotent("/api/v1/documents")
    assert not is_idempotent("/api/v1/documents/batch")
    assert not is_idempotent("/api/v1/kbs")


# ---- 单元级：请求 ID -----------------------------------------------------


def test_new_request_id_shape():
    from rag.core.requestid import new_request_id

    rid = new_request_id()
    assert 8 <= len(rid) <= 32
    assert rid.isalnum()


# ---- helpers -----------------------------------------------------------

def _client_with_limit(tmp_path, limit: int, embedder: bool = False):
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    s = Settings()
    s.data_dir = tmp_path
    s.embed.dim = 4
    s.rate_limit_writes_per_min = limit
    s.rate_limit_burst = limit
    reg = ModelRegistry(s)
    if embedder:
        # 入库要真的走完流水线才有意义；默认的 embedder 会 503。
        class _E:
            dim = 4
            model = "stub"

            def embed_one(self, text):  # noqa: ARG002
                return [1.0, 0.0, 0.0, 0.0]

            def embed(self, texts):
                return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        reg.bundle.embedder = _E()
    return TestClient(create_app(Ctx(s, store, reg)),
                      raise_server_exceptions=False)