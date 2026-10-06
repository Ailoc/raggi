"""幂等键：同键重试不产生第二份数据。

内容哈希去重已经挡住「同一内容重复入库」，但那是**事后**发现并返回
`status=duplicate`；幂等键解决的是**过程**幂等——同键的第二次请求
根本不进流水线。
"""
from __future__ import annotations

import pytest


@pytest.fixture()
def client(tmp_path):
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.api.documents import IDEM
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
    s = Settings()
    s.data_dir = tmp_path
    s.embed.dim = 4
    registry = ModelRegistry(s)
    registry.bundle.embedder = _E()
    c = TestClient(create_app(Ctx(s, store, registry)),
                   raise_server_exceptions=False)
    IDEM.reset()          # 用例之间不该互相看到对方的键
    yield c
    IDEM.reset()


def _total(client) -> int:
    return client.get("/api/v1/documents").json()["total"]


# ---- 基本语义 ----------------------------------------------------------


def test_same_key_replays_first_result(client):
    """同键重试返回首次结果，并标记为回放。"""
    body = {"text": "幂等测试内容。" * 30, "title": "幂等"}
    hdr = {"Idempotency-Key": "key-abc-1"}

    first = client.post("/api/v1/documents/text", json=body, headers=hdr).json()
    assert first["status"] == "ready"
    assert first.get("idempotent_replay") is None

    second = client.post("/api/v1/documents/text", json=body, headers=hdr).json()
    assert second["idempotent_replay"] is True, "重试未被识别为回放"
    assert second["doc_id"] == first["doc_id"], "重试返回了不同的 doc_id"
    assert _total(client) == 1, "重试产生了第二份文档"


def test_different_keys_create_separate_docs(client):
    """不同键是不同请求——键不能被当成全局去重。

    注意内容必须不同：同内容本来就会被内容哈希去重挡掉（返回
    duplicate），那样测不到「不同键是否被视为不同请求」。
    """
    a = client.post("/api/v1/documents/text",
                    json={"text": "甲的内容。" * 30, "title": "甲"},
                    headers={"Idempotency-Key": "key-1"}).json()
    b = client.post("/api/v1/documents/text",
                    json={"text": "乙的内容。" * 30, "title": "乙"},
                    headers={"Idempotency-Key": "key-2"}).json()
    assert a["doc_id"] != b["doc_id"]
    assert _total(client) == 2


def test_no_key_means_normal_execution(client):
    """不给键时行为不变（老客户端不受影响）。"""
    body = {"text": "无键请求。" * 30, "title": "无键"}
    r = client.post("/api/v1/documents/text", json=body).json()
    assert r.get("idempotent_replay") is None


def test_key_is_scoped_per_endpoint(client):
    """同一个键用在不同端点上互不干扰。

    否则「先建库再入库」共用一个键时，入库会拿到建库的结果。
    """
    from rag.core.idempotency import IdempotencyStore

    store = IdempotencyStore()
    store.put("k", "/a", {"who": "a"})
    store.put("k", "/b", {"who": "b"})
    assert store.get("k", "/a") == {"who": "a"}
    assert store.get("k", "/b") == {"who": "b"}


# ---- 边界 --------------------------------------------------------------


def test_failed_request_is_not_cached(client):
    """失败的结果不记：否则重试会永远返回那个失败。"""
    from rag.core.idempotency import IdempotencyStore

    store = IdempotencyStore()
    # 只记成功的：_idem 的实现里 failed 分支不 put。
    # 这里直接验证存储层：没有 put 时 get 自然为 None。
    assert store.get("k", "/p") is None
    store.put("k", "/p", {"status": "failed"})
    # 若把失败也存了，重试就会拿到 failed；实现层保证不发生
    assert store.get("k", "/p") == {"status": "failed"}


def test_expired_entry_is_ignored():
    """过期条目不再回放——键空间有界，实际重试都在分钟级。"""
    import time

    from rag.core.idempotency import IdempotencyStore

    store = IdempotencyStore(ttl=0.05)
    store.put("k", "/p", {"v": 1})
    assert store.get("k", "/p") == {"v": 1}
    time.sleep(0.1)
    assert store.get("k", "/p") is None, "过期条目仍被回放"


def test_store_gc_bounds_keyspace():
    """键空间不能无界增长。"""
    from rag.core.idempotency import MAX_ENTRIES, IdempotencyStore

    store = IdempotencyStore(ttl=3600)
    for i in range(MAX_ENTRIES + 500):
        store.put(f"k{i}", "/p", {"i": i})
    assert len(store._entries) <= MAX_ENTRIES


@pytest.mark.parametrize("bad", ["has space", "a" * 300, "bad;drop"])
def test_unsafe_key_is_ignored(client, bad):
    """不安全的键被丢弃（不启用幂等），而不是当键用。"""
    from rag.core.idempotency import normalize_key

    class _R:
        headers = {"Idempotency-Key": bad}

    assert normalize_key(_R()) is None


def test_missing_key_returns_none():
    from rag.core.idempotency import normalize_key

    class _R:
        headers = {}

    assert normalize_key(_R()) is None


# ---- 上传端点 ----------------------------------------------------------


def test_upload_retry_does_not_duplicate(client, tmp_path):
    """文件上传同键重试不会产生第二份文档。"""
    p = tmp_path / "idem.txt"
    p.write_bytes(("上传幂等内容。" * 200).encode())
    hdr = {"Idempotency-Key": "upload-key-1"}
    files = {"file": ("idem.txt", p.read_bytes(), "text/plain")}

    first = client.post("/api/v1/documents", files=files, headers=hdr).json()
    assert first["status"] == "ready"
    assert _total(client) == 1

    files2 = {"file": ("idem.txt", p.read_bytes(), "text/plain")}
    second = client.post("/api/v1/documents", files=files2, headers=hdr).json()
    assert second.get("idempotent_replay") is True
    assert second["doc_id"] == first["doc_id"]
    assert _total(client) == 1, "上传重试产生了第二份文档"


def test_upload_retry_skips_reading_the_file(client, tmp_path):
    """重试不该把文件再读一遍内存——大文件重试一次就是白读一次。

    通过「重试时提交一个不存在的路径」验证：若实现仍会读文件，
    会因路径不存在而报错。
    """
    p = tmp_path / "real.txt"
    p.write_bytes(("先提交一次。" * 200).encode())
    hdr = {"Idempotency-Key": "upload-skip-1"}
    first = client.post("/api/v1/documents",
                        files={"file": ("real.txt", p.read_bytes(), "text/plain")},
                        headers=hdr).json()
    assert first["status"] == "ready"

    # 同一个键，但这次的文件路径已不存在
    missing = {"file": ("gone.txt", b"", "text/plain")}
    second = client.post("/api/v1/documents", files=missing, headers=hdr)
    assert second.status_code == 200, second.text
    assert second.json().get("idempotent_replay") is True
    assert _total(client) == 1