"""入库任务取消（协作式）。

对应「任务可管理」补齐：此前任务只能看不能管，卡住的长任务只能干等。

这里最关键的不是「能取消」，而是**如实说明取消到什么程度**：
排队中的任务是真取消，运行中的只能协作式取消（解析与向量化不响应中断）。
把两者混为一谈，会让用户以为已经停下、实际还在写库。
"""
from __future__ import annotations

import threading
import time

import pytest


class _GateEmbedder:
    """在 embed 阶段卡住，让取消请求打在「运行中」这个时机上。

    门控是**实例**状态：早前写成类属性时，前一个用例遗留的 release
    会让后一个用例的 embed 立刻返回，取消检查压根没被触发
    （表现为「任务竟然成功了」）。每个用例新建实例才隔离得住。
    """
    dim = 4
    model = "stub"

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def embed_one(self, text):  # noqa: ARG002
        return [1.0, 0.0, 0.0, 0.0]

    def embed(self, texts):
        self.entered.set()
        # 最多等 10s；测试失败时不要永久挂住
        self.release.wait(timeout=10)
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


@pytest.fixture()
def env(tmp_path):
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    class _Fast:
        dim = 4
        model = "stub"

        def embed_one(self, text):  # noqa: ARG002
            return [1.0, 0.0, 0.0, 0.0]

        def embed(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    store = LanceStore.for_data_dir(tmp_path, 4)
    settings = Settings()
    settings.data_dir = tmp_path
    settings.embed.dim = 4
    settings.ingest_workers = 1        # 单 worker：第二个任务必然排队
    registry = ModelRegistry(settings)
    registry.bundle.embedder = _Fast()
    ctx = Ctx(settings, store, registry)
    client = TestClient(create_app(ctx), raise_server_exceptions=False)
    client.store = store  # type: ignore[attr-defined]
    client.ctx = ctx      # type: ignore[attr-defined]
    return client


def _upload_doc(client, tmp_path, name: str = "doc.txt") -> str:
    """用**上传文件**建文档，返回 doc_id。

    只有上传的文档才有留档原文；粘贴文本入库的文档没有，
    重解析会直接 400——用粘贴文本测「重解析被取消」测不到东西。

    用 .txt 而非 .pdf：扩展名决定解析引擎，给 .txt 装纯文本才会走
    native 解析（给 .pdf 装文本会被 PyMuPDF 拒绝，报
    "Failed to open file ... as type pdf"）。
    """
    p = tmp_path / name
    p.write_bytes(("%s 的正文内容，需要保留。" % name).encode() * 300)
    r = client.post("/api/v1/documents",
                    files={"file": (name, p.read_bytes(), "text/plain")})
    assert r.status_code == 200, r.text
    return r.json()["doc_id"]


def _drain(client, job_id: str, timeout: float = 10.0) -> dict:
    """轮询到终态。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get(f"/api/v1/jobs/{job_id}").json()
        if st["terminal"]:
            return st
        time.sleep(0.03)
    st = client.get(f"/api/v1/jobs/{job_id}").json()
    raise AssertionError(f"任务未在 {timeout}s 内结束: {st}")


def _submit(client, title="t", kb_id: str = "") -> str:
    r = client.post("/api/v1/documents/text",
                    json={"text": f"{title} 的正文内容。" * 40,
                          "title": title, "kb_id": kb_id or None},
                    params={"wait": "false"})
    assert r.status_code == 200, r.text
    return r.json()["job_id"]


def _wait_stage(client, job_id: str, want: set[str], timeout: float = 10.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get(f"/api/v1/jobs/{job_id}").json()
        if st["stage"] in want:
            return st["stage"]
        time.sleep(0.02)
    st = client.get(f"/api/v1/jobs/{job_id}").json()
    raise AssertionError(f"任务未在 {timeout}s 内进入 {want}: {st}")


# ---- 排队中的任务：真取消 ----------------------------------------------


def test_cancel_queued_task(env):
    """排队中的任务被撤销后永不执行，任务行置为终态 cancelled。"""
    client = env
    blocker = _GateEmbedder()
    client.ctx.registry.bundle.embedder = blocker

    first = _submit(client, "第一个")
    assert blocker.entered.wait(timeout=10), "第一个任务未进入 embed 阶段"
    second = _submit(client, "第二个")   # 单 worker → 必然排队

    r = client.delete(f"/api/v1/jobs/{second}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "cancelled", body
    assert "取消" in body["message"]

    st = client.get(f"/api/v1/jobs/{second}").json()
    assert st["stage"] == "cancelled"
    assert st["terminal"] is True

    blocker.release.set()
    _wait_stage(client, first, {"done"})


def test_cancelled_task_produces_no_document(env):
    """被取消的任务不应留下文档（否则是「取消了个寂寞」）。"""
    client = env
    blocker = _GateEmbedder()
    client.ctx.registry.bundle.embedder = blocker

    first = _submit(client, "保留")
    assert blocker.entered.wait(timeout=10)
    second = _submit(client, "应消失")

    client.delete(f"/api/v1/jobs/{second}")
    blocker.release.set()
    _wait_stage(client, first, {"done"})

    titles = {d["title"] for d in client.get("/api/v1/documents").json()["items"]}
    assert "应消失" not in titles, "被取消的任务仍产出了文档"


# ---- 运行中的任务：协作式取消 ------------------------------------------


def test_cancel_running_task_is_cooperative(env):
    """运行中只能请求取消：如实回报 outcome=running，并在阶段边界退出。"""
    client = env
    blocker = _GateEmbedder()
    client.ctx.registry.bundle.embedder = blocker
    job = _submit(client, "运行中")
    assert blocker.entered.wait(timeout=10), "未进入 embed 阶段"

    r = client.delete(f"/api/v1/jobs/{job}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["outcome"] == "running", body
    # 措辞必须说明「当前阶段结束后才停」，不能让人以为已经停了
    assert "阶段" in body["message"]
    assert "无法中途打断" in body["message"]

    st = client.get(f"/api/v1/jobs/{job}").json()
    assert st["stage"] == "cancelling", st

    blocker.release.set()
    final = client.get(f"/api/v1/jobs/{job}")
    deadline = time.time() + 10
    while time.time() < deadline and not final.json()["terminal"]:
        time.sleep(0.05)
        final = client.get(f"/api/v1/jobs/{job}")
    assert final.json()["stage"] == "cancelled", final.json()
    assert final.json()["terminal"] is True


def test_cancelled_job_leaves_document_usable(env, tmp_path):
    """重解析被取消时，旧分块必须完好——取消不该损坏已有内容。"""
    client = env
    doc_id = _upload_doc(client, tmp_path)
    before = client.get(f"/api/v1/chunks?doc_id={doc_id}").json()["total"]
    assert before > 0

    blocker = _GateEmbedder()
    client.ctx.registry.bundle.embedder = blocker
    job = client.post(f"/api/v1/documents/{doc_id}/reparse?wait=false",
                      json={}).json()["job_id"]
    assert blocker.entered.wait(timeout=10), "未进入 embed 阶段"

    client.delete(f"/api/v1/jobs/{job}")
    blocker.release.set()
    _drain(client, job)

    after = client.get(f"/api/v1/chunks?doc_id={doc_id}").json()
    assert after["total"] == before, "取消后旧分块数量变了"
    # 注意：health 整体状态在此测试里**本来就是 degraded**（桩 embedder 的
    # model 名与配置里的不一致 → embedding_model_mismatch），与取消无关。
    # 因此只断言取消不该引入的那两类损坏：计数不一致与孤儿分块。
    h = client.get("/api/v1/health").json()
    assert h["count_mismatch"] == [], "取消后计数漂移"
    assert h["orphan_chunks"] == 0, "取消后出现孤儿分块"


def test_cancelled_document_not_marked_failed(env, tmp_path):
    """取消不是失败：文档状态不该变成 failed。"""
    client = env
    doc_id = _upload_doc(client, tmp_path)

    blocker = _GateEmbedder()
    client.ctx.registry.bundle.embedder = blocker
    job = client.post(f"/api/v1/documents/{doc_id}/reparse?wait=false",
                      json={}).json()["job_id"]
    assert blocker.entered.wait(timeout=10), "未进入 embed 阶段"
    client.delete(f"/api/v1/jobs/{job}")
    blocker.release.set()
    _drain(client, job)

    after = client.get(f"/api/v1/documents/{doc_id}").json()
    assert after["status"] != "failed", after["status"]


# ---- 边界情形 ----------------------------------------------------------


def test_cancel_finished_task(env):
    """已结束的任务：如实回报 finished，而不是谎称取消成功。"""
    client = env
    job = _submit(client, "已完成")
    _wait_stage(client, job, {"done"})
    r = client.delete(f"/api/v1/jobs/{job}")
    assert r.json()["outcome"] == "finished"
    assert "无需" in r.json()["message"]


def test_cancel_unknown_job_404(env):
    r = env.delete("/api/v1/jobs/does-not-exist")
    assert r.status_code == 404
    assert "detail" in r.json()


def test_cancelled_job_excluded_from_active_only(env):
    """cancelled 是终态：不再出现在「正在进行的任务」里。"""
    client = env
    blocker = _GateEmbedder()
    client.ctx.registry.bundle.embedder = blocker
    first = _submit(client, "跑着")
    assert blocker.entered.wait(timeout=10)
    second = _submit(client, "排着")
    client.delete(f"/api/v1/jobs/{second}")

    active = {j["job_id"] for j in
              client.get("/api/v1/jobs?active_only=true").json()["items"]}
    assert second not in active
    assert first in active
    blocker.release.set()
    _wait_stage(client, first, {"done"})


def test_job_records_kb_and_links_back(env, tmp_path):
    """任务行带 kb_id：任务页靠它把任务链回知识库。

    没有它，用户只能拿一串 UUID 猜那是哪篇内容。
    """
    client = env
    kb = client.post("/api/v1/kbs", json={"name": "归属库"}).json()["kb_id"]
    job = _submit(client, "带归属", kb_id=kb)
    _drain(client, job)
    row = client.get(f"/api/v1/jobs/{job}").json()
    assert row["kb_id"] == kb, f"任务未记录归属: {row}"
    assert row["doc_id"], "任务结束后应有产出的 doc_id"
    # 列表端点也要带这两个字段，否则任务页拿不到
    item = next(j for j in client.get("/api/v1/jobs").json()["items"]
                if j["job_id"] == job)
    assert item["kb_id"] == kb
    assert item["terminal"] is True


def test_list_items_carry_terminal_flag(env):
    """列表每行都带 terminal（与单条端点一致）。

    回归防护：只有单条端点带 terminal 时，前端列表页无法判断
    「停止」按钮该不该显示、要不要继续轮询。
    """
    client = env
    job = _submit(client, "终态字段")
    _drain(client, job)
    for item in client.get("/api/v1/jobs").json()["items"]:
        assert "terminal" in item, f"列表项缺 terminal: {item}"


def test_jobs_schema_migration_is_idempotent(tmp_path):
    """jobs 表补列可重复执行，且存量库不因此写入失败。

    回归防护：加列只在第一次生效，之后必须安静跳过；否则每次启动
    都白跑一次迁移，第二次还会因「列已存在」报错。
    """
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    assert "kb_id" in store.jobs.schema.names
    # 幂等：再跑一次迁移不应抛错
    store._migrate_columns()
    assert "kb_id" in store.jobs.schema.names
