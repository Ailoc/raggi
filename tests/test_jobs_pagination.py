"""任务列表的分页与筛选：必须在服务端过滤/下推，不能“先取一页再筛”。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rag.api import Ctx, create_app
from rag.core.config import Settings
from rag.models.registry import ModelRegistry
from rag.storage.meta import MetaStore
from rag.storage.repos import jobs as jobs_repo
from rag.storage.tables import LanceStore


def _client(tmp_path, with_meta: bool):
    settings = Settings()
    settings.data_dir = tmp_path
    settings.embed.dim = 4
    store = LanceStore.for_data_dir(tmp_path, 4)
    meta = MetaStore(tmp_path / "raggi.db") if with_meta else None
    if meta is not None:
        store.meta = meta
    return TestClient(create_app(Ctx(settings, store, ModelRegistry(settings),
                                     meta=meta)))


def _job(store, job_id: str, stage: str, started: str) -> None:
    jobs_repo.add_job(store, {
        "job_id": job_id, "doc_id": "", "kb_id": "", "stage": stage,
        "progress": 1.0 if stage in ("done", "failed", "cancelled") else 0.0,
        "total": 0, "parser_engine": "native", "error": None,
        "started_at": started,
        "ended_at": started if stage in ("done", "failed", "cancelled") else None,
    })


@pytest.mark.parametrize("with_meta", [False, True])
def test_jobs_offset_and_status_filter_are_server_side(tmp_path, with_meta):
    client = _client(tmp_path, with_meta)
    store = client.app.state.ctx.store
    # 5 条较新的终态任务 + 1 条更早的活跃任务：活跃过滤必须在 LIMIT 之前，
    # 否则“活跃任务排在最近 50 条之后”时会从界面消失。
    for i in range(5):
        _job(store, f"d{i}", "done" if i != 2 else "failed",
             f"2026-10-20T10:0{i}:00+00:00")
    _job(store, "active-old", "queued", "2026-10-19T09:00:00+00:00")

    page1 = client.get("/api/jobs", params={"limit": 2, "offset": 0}).json()
    page2 = client.get("/api/jobs", params={"limit": 2, "offset": 2}).json()
    assert page1["total"] == page2["total"] == 6
    ids1 = [j["job_id"] for j in page1["items"]]
    ids2 = [j["job_id"] for j in page2["items"]]
    assert len(ids1) == len(ids2) == 2
    assert not set(ids1) & set(ids2), "第二页仍是第一页的数据，offset 没生效"

    failed = client.get("/api/jobs", params={"status": "failed"}).json()
    assert failed["total"] == 1
    assert [j["stage"] for j in failed["items"]] == ["failed"]

    active = client.get("/api/jobs", params={"status": "active",
                                             "limit": 2}).json()
    assert active["total"] == 1
    assert active["items"][0]["job_id"] == "active-old", (
        "活跃过滤发生在 LIMIT 之后：更早的活跃任务被最近 5 条终态任务挤掉了")

    legacy = client.get("/api/jobs", params={"active_only": "true",
                                             "limit": 2}).json()
    assert legacy["total"] == 1 and legacy["items"][0]["job_id"] == "active-old"


@pytest.mark.parametrize("with_meta", [False, True])
def test_jobs_cancelled_filter_and_offset_bounds(tmp_path, with_meta):
    client = _client(tmp_path, with_meta)
    store = client.app.state.ctx.store
    _job(store, "c1", "cancelled", "2026-10-20T10:00:00+00:00")
    _job(store, "c2", "cancelled", "2026-10-20T11:00:00+00:00")

    r = client.get("/api/jobs", params={"status": "cancelled",
                                        "offset": 1, "limit": 1}).json()
    assert r["total"] == 2
    assert [j["job_id"] for j in r["items"]] == ["c1"]
    # 越界 offset 返回空列表，但 total 仍是过滤后的总数
    empty = client.get("/api/jobs", params={"status": "cancelled",
                                            "offset": 99, "limit": 10}).json()
    assert empty["items"] == [] and empty["total"] == 2
