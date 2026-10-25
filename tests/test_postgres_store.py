"""Postgres 元数据引擎与 SQLite 的行为对齐（集成测试）。

需要真实 Postgres：设置 `RAG_TEST_PG_URL`
（例如 `postgresql://postgres:raggi@127.0.0.1:55432/raggi`）后才会跑。
默认套件跳过——单机形态不依赖任何外部服务。

这里不是重复 SQLite 的单元测试，而是**同一串仓储调用在两个引擎上
给出相同结果**：SQL 文本只有一份，方言差异全部收敛在 pg_meta 里。
"""
from __future__ import annotations

import os
import uuid

import pytest

URL = os.environ.get("RAG_TEST_PG_URL", "")
pytestmark = pytest.mark.skipif(
    not URL, reason="未设置 RAG_TEST_PG_URL，跳过 Postgres 集成测试")


@pytest.fixture()
def stores(tmp_path):
    """(SQLite store, Postgres store)，都已挂好 meta。"""
    from rag.storage.meta import MetaStore
    from rag.storage.pg_meta import PostgresMetaStore
    from rag.storage.tables import LanceStore

    lance_dir = tmp_path / "lance"

    sqlite_meta = MetaStore(tmp_path / "meta.db")
    sqlite_store = LanceStore.for_data_dir(lance_dir, 4)
    sqlite_store.meta = sqlite_meta

    pg_meta = PostgresMetaStore(URL)
    # 每个测试用独立 schema 太慢；这里清空四张真源表
    for table in ("documents", "jobs", "kbs", "apikeys"):
        pg_meta.execute(f"DELETE FROM {table}")
    pg_store = LanceStore.for_data_dir(lance_dir, 4)
    pg_store.meta = pg_meta
    return sqlite_store, pg_store, sqlite_meta, pg_meta


def _doc(i: int, **over) -> dict:
    base = {
        "doc_id": f"d{i}", "title": f"Document {i}", "source_uri": None,
        "mime": "text/plain", "parser_engine": "native",
        "content_hash": f"h{i}", "char_count": 10, "chunk_count": 2,
        "text": f"正文 {i}", "status": "ready", "error": None,
        "kb_id": "kb1", "stored_file": None, "meta": "{}",
        "created_at": f"2026-01-0{i}", "updated_at": "2026-01-01",
    }
    base.update(over)
    return base


def test_schema_reflection_matches(stores):
    _, _, sqlite_meta, pg_meta = stores
    assert pg_meta.columns("documents") == sqlite_meta.columns("documents")
    s_def = sqlite_meta.not_null_defaults("documents")
    p_def = pg_meta.not_null_defaults("documents")
    # PG 的 REAL 改成 DOUBLE PRECISION 不影响默认值本身
    assert p_def == s_def


def test_documents_crud_and_filters_match(stores):
    from rag.storage.repos import documents as repo

    sqlite_store, pg_store, _, _ = stores
    rows = [_doc(i) for i in range(1, 6)]
    repo.upsert_documents(sqlite_store, [dict(r) for r in rows])
    repo.upsert_documents(pg_store, [dict(r) for r in rows])

    a, total_a = repo.list_documents(sqlite_store, kb_id="kb1", limit=2, offset=1)
    b, total_b = repo.list_documents(pg_store, kb_id="kb1", limit=2, offset=1)
    assert total_a == total_b == 5
    assert [r["doc_id"] for r in a] == [r["doc_id"] for r in b]

    # LIKE 大小写不敏感：SQLite 默认如此，PG 必须 ILIKE 才能对齐
    qa, qta = repo.list_documents(sqlite_store, q="DOCUMENT 3")
    qb, qtb = repo.list_documents(pg_store, q="DOCUMENT 3")
    assert qta == qtb == 1
    assert [r["title"] for r in qa] == [r["title"] for r in qb]


def test_upsert_updates_and_keeps_pk(stores):
    from rag.storage.repos import documents as repo

    sqlite_store, pg_store, _, _ = stores
    for st in (sqlite_store, pg_store):
        repo.upsert_documents(st, [_doc(1)])
        repo.upsert_documents(st, [_doc(1, title="改名", chunk_count=9)])

    a = repo.get_document(sqlite_store, "d1")
    b = repo.get_document(pg_store, "d1")
    for key in ("doc_id", "title", "chunk_count", "text"):
        assert a[key] == b[key]


def test_jobs_and_kbs_match(stores):
    from rag.storage.repos import jobs as jobs_repo
    from rag.storage.repos import kbs as kb_repo

    sqlite_store, pg_store, _, _ = stores
    for st in (sqlite_store, pg_store):
        kb = kb_repo.create_kb(st, "知识库")
        jobs_repo.add_job(st, {
            "job_id": "j1", "doc_id": "d1", "kb_id": kb["kb_id"],
            "stage": "queued", "progress": 0.0, "total": 0,
            "parser_engine": "", "error": None,
            "started_at": "2026-01-01", "ended_at": None})
        jobs_repo.set_job(st, "j1", stage="done")

    a = jobs_repo.get_job(sqlite_store, "j1")
    b = jobs_repo.get_job(pg_store, "j1")
    assert a["stage"] == b["stage"] == "done"
    assert [k["name"] for k in kb_repo.list_kbs(sqlite_store)] == \
        [k["name"] for k in kb_repo.list_kbs(pg_store)]


def test_transaction_rolls_back(stores):
    _, _, sqlite_meta, pg_meta = stores
    for meta in (sqlite_meta, pg_meta):
        try:
            with meta.tx() as conn:
                conn.execute(
                    "INSERT INTO kbs(kb_id, name) VALUES(?,?)", ("tx1", "回滚"))
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert meta.query("SELECT kb_id FROM kbs WHERE kb_id=?", ("tx1",)) == []


def test_meta_state_and_epoch(stores):
    _, _, sqlite_meta, pg_meta = stores
    for meta in (sqlite_meta, pg_meta):
        meta.set_state("probe", "1")
        assert meta.get_state("probe") == "1"
        assert meta.bump_state("epoch") >= 1
        assert meta.state_int("epoch") >= 1


def test_shared_rate_limiter_and_idempotency(stores):
    """共用 SQLite 的那两套状态机在 PG 上必须原样工作。"""
    from rag.core.idempotency import IdempotencyStore
    from rag.core.ratelimit import RateLimiter

    _, _, _, pg_meta = stores
    limiter = RateLimiter(limit=60, burst=2, meta=pg_meta)
    assert [limiter.allow("c")[0] for _ in range(2)] == [True, True]
    assert limiter.allow("c")[0] is False

    idem = IdempotencyStore(meta=pg_meta)
    key = str(uuid.uuid4())
    claim = idem.claim(key, "/documents/text")
    assert claim.kind == "owned"
    idem.finish(key, "/documents/text", claim.token, {"doc_id": "d1"})
    replay = idem.claim(key, "/documents/text")
    assert replay.kind == "replay"
    assert replay.result == {"doc_id": "d1"}


def test_copy_from_sqlite(tmp_path):
    from rag.storage.meta import MetaStore
    from rag.storage.pg_meta import PostgresMetaStore, copy_from_sqlite

    sqlite_meta = MetaStore(tmp_path / "src.db")
    from rag.storage.repos import documents as repo
    from rag.storage.tables import LanceStore

    src_store = LanceStore.for_data_dir(tmp_path / "lance", 4)
    src_store.meta = sqlite_meta
    repo.upsert_documents(src_store, [_doc(1), _doc(2)])

    pg_meta = PostgresMetaStore(URL)
    for table in ("documents", "jobs", "kbs", "apikeys"):
        pg_meta.execute(f"DELETE FROM {table}")
    report = copy_from_sqlite(sqlite_meta, pg_meta)

    assert report["documents"] == 2
    assert pg_meta.count("documents") == 2
    assert pg_meta.get_state("schema_version") != ""
