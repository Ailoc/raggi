"""元数据引擎（SQLite/WAL）的行为与对齐测试。

为什么必须有这一组：`storage.meta_engine` 默认是 `auto`，也就是说
**SQLite 现在是生产路径**，测试却全都构造 `Ctx(..., meta=None)` 走
LanceDB 回退分支。默认路径没人跑，等于把最容易被改坏的那条路留白。

所以这里做两件事：
1. **对齐测试**：同一串操作分别在「有 meta / 无 meta」两条路径上跑一遍，
   断言 API 响应一致 —— 分派写成分支形式，最大的风险就是两支悄悄分叉；
2. **写放大上界**：入库 N 次后 LanceDB 的 `jobs` 表版本数不许增长
   （改前一次入库 ≈5 个版本，48 次后 226 个版本 / 224 个碎片文件，
   同期 `/api/documents` 的 p50 从 13.4ms 涨到 30.7ms）。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from rag.api import Ctx, create_app
from rag.core.config import Settings
from rag.models.registry import ModelRegistry
from rag.storage.backend import LocalBackend
from rag.storage.meta import MetaStore
from rag.storage.tables import LanceStore


class _StubEmbedder:
    """确定性向量：入库不该依赖外部模型服务。"""

    dim = 4
    model = "stub"

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    def embed_one(self, text):
        return [1.0, 0.0, 0.0, 0.0]


def _env(tmp_path, *, with_meta: bool):
    settings = Settings()
    settings.data_dir = tmp_path
    settings.token = ""
    settings.embed.dim = 4
    settings.embed.model = "stub"
    store = LanceStore(LocalBackend(tmp_path), 4)
    meta = MetaStore(tmp_path / "raggi.db") if with_meta else None
    if meta is not None:
        store.meta = meta
    registry = ModelRegistry(settings)
    registry.bundle = type(registry.bundle)(
        embedder=_StubEmbedder(), chat_factory=lambda: None,
        reranker=None, embed_cfg=settings.embed, llm_cfg=settings.llm,
        rerank_cfg=settings.rerank)
    client = TestClient(create_app(Ctx(settings, store, registry,
                                       meta=meta)))
    return client, store, meta


def _seed_sequence(client):
    """跑一串真实操作，返回「外部可见结果」的列表。"""
    out = []
    kb = client.post("/api/kbs", json={"name": "对齐测试库"}).json()
    out.append(("kb_created", kb["name"]))
    r = client.post("/api/documents/text",
                    json={"text": "第一段 内容 对齐", "title": "甲",
                          "kb_id": kb["kb_id"]})
    doc = r.json()
    out.append(("doc_status", doc["status"], doc["chunk_count"] > 0))
    out.append(("doc_get", client.get(f"/api/documents/{doc['doc_id']}")
                .json()["title"]))
    out.append(("list", [d["title"] for d in client.get(
        "/api/documents", params={"kb_id": kb["kb_id"]}).json()["items"]]))
    out.append(("list_total", client.get(
        "/api/documents", params={"kb_id": kb["kb_id"]}).json()["total"]))
    chunks = client.get("/api/chunks", params={"doc_id": doc["doc_id"]}).json()
    out.append(("chunks_total", chunks["total"]))
    cid = chunks["items"][0]["chunk_id"]
    out.append(("edit", client.patch(f"/api/chunks/{cid}", json={
        "text": "改写后的 内容 与 关键词"}).status_code))
    out.append(("toggle", client.patch(
        f"/api/chunks/{cid}/enabled", json={"enabled": False}).status_code))
    out.append(("search", [h["chunk_id"] == cid for h in client.post(
        "/api/search", json={"q": "关键词", "kb_id": kb["kb_id"],
                             "include_disabled": True}).json()["results"]]))
    out.append(("jobs", [(j["stage"], j["doc_id"] == doc["doc_id"])
                         for j in client.get("/api/jobs").json()["items"]]))
    key = client.post("/api/keys", json={"name": "只读", "scope": "read"})
    out.append(("key_created", key.status_code))
    secret = key.json().get("api_key", "")
    wkey = client.post("/api/keys", json={"name": "读写", "scope": "write"})
    wsecret = wkey.json().get("api_key", "")
    out.append(("key_created_write", wkey.status_code))
    # 签发第一把密钥之后，密钥管理接口本身就要带凭据了
    # （「零密钥时放行」只是引导例外）。这里用刚拿到的那把读密钥列，
    # 顺带把「read 作用域能不能列密钥」这条语义钉住。
    kl = client.get("/api/keys", headers={"X-API-Key": secret})
    out.append(("key_list", kl.status_code,
                [k["name"] for k in kl.json().get("items", [])]))
    ok = client.get("/api/documents", headers={"X-API-Key": secret})
    out.append(("key_auth", ok.status_code))
    bad = client.post("/api/documents/text", headers={"X-API-Key": secret},
                      json={"text": "不该成功", "title": "越权"})
    out.append(("read_scope_blocks_write", bad.status_code))
    # 对账与删除要在**吊销密钥之前**做：库里只要还留着密钥行（哪怕已吊销），
    # `has_keys()` 就仍为真、无凭据请求就仍被拒 —— 那是设计如此，
    # 而吊销之后的行为在下面单独两条里断言。
    hp = client.get("/api/health", params={"fresh": True},
                    headers={"X-API-Key": wsecret})
    out.append(("health", hp.status_code, hp.json().get("status")))
    out.append(("delete_doc", client.delete(
        f"/api/documents/{doc['doc_id']}", headers={"X-API-Key": secret})
        .status_code))
    out.append(("kb_doc_count_after_del", client.get(
        f"/api/kbs/{kb['kb_id']}", headers={"X-API-Key": wsecret}
    ).json().get("doc_count")))
    out.append(("revoke", client.delete(
        f"/api/keys/{key.json()['key_id']}",
        headers={"X-API-Key": secret}).status_code))
    out.append(("after_revoke", client.get(
        "/api/documents", headers={"X-API-Key": secret}).status_code))
    out.append(("no_credential_after_keys_exist", client.get(
        "/api/documents").status_code))
    return out


@pytest.mark.parametrize("with_meta", [False, True], ids=["lancedb", "sqlite"])
def test_environment_builds(tmp_path, with_meta):
    client, store, meta = _env(tmp_path, with_meta=with_meta)
    assert client.get("/api/health").status_code == 200
    assert (meta is not None) == with_meta


def test_two_engines_agree_on_every_observed_result(tmp_path):
    """同一串操作在两条路径上必须给出**完全一致**的外部可见结果。

    这是这组测试的核心。分派写成「if meta: … else: …」的形式，
    最大的风险不是崩，而是两支悄悄分叉 —— 比如 SQLite 分支忘了
    `ORDER BY doc_id` 兜底，翻页就会在并列处重漏。
    """
    a_dir = tmp_path / "lance"
    b_dir = tmp_path / "sqlite"
    a_dir.mkdir()
    b_dir.mkdir()
    ca, _sa, _ma = _env(a_dir, with_meta=False)
    cb, _sb, _mb = _env(b_dir, with_meta=True)
    ra = _seed_sequence(ca)
    rb = _seed_sequence(cb)
    # chunk_id / doc_id / 时间戳是随机或单调的，比较时把它们归一掉
    def norm(rows):
        return [(r[0], json.dumps(r[1:], sort_keys=True, default=str))
                for r in rows]

    for x, y in zip(norm(ra), norm(rb)):
        assert x[0] == y[0]
        assert x[1] == y[1], f"端点 {x[0]} 两条引擎结果不一致：\n" \
                             f"  lancedb={x[1]}\n  sqlite ={y[1]}"
    assert len(ra) == len(rb), (
        "两条路径的操作步数不同：某支中途抛异常或提前返回，"
        "对齐断言就只比了前缀")


def test_sqlite_path_writes_metadata_to_sqlite_only(tmp_path):
    """jobs / apikeys / documents 的行落在 SQLite，LanceDB 侧不再增长。

    改前一次入库会在 `jobs` 上留下 ≈5 个版本；写放大是
    「用得越久越慢」的直接来源（诊断报告 §2.6）。
    """
    client, store, meta = _env(tmp_path, with_meta=True)

    def lance_versions(table):
        return len(store._table(table).list_versions())

    v_jobs_before = lance_versions("jobs")
    for i in range(6):
        r = client.post("/api/documents/text",
                        json={"text": f"写放大测试 内容 {i}", "title": f"w{i}"})
        assert r.status_code == 200, r.text

    v_jobs_after = lance_versions("jobs")
    assert v_jobs_after == v_jobs_before, (
        f"入库把 LanceDB 的 jobs 版本从 {v_jobs_before} 推到 {v_jobs_after}"
        "：SQLite 没接住写")
    assert meta.count("jobs") >= 6, "任务行没写进 SQLite"
    assert meta.count("documents") == 6
    assert store.documents.count_rows() == 0, (
        "documents 不该再往 LanceDB 写（两份真源会分叉）")


def test_migration_preserves_document_text(tmp_path):
    """自动迁移**不许丢正文**（这条对应一次真实事故）。

    `meta.DOC_COLS` 曾漏掉 `text`：迁移后文档还在、`char_count` 还在，
    但打开是空白，重新切分会报「该文档无正文」。而当时的校验只比行数，
    于是全绿通过。所以这里既断言正文完好，也断言校验本身有牙。
    """
    from rag.storage.meta import import_from_lance

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    store = LanceStore(LocalBackend(src_dir), 4)
    store.documents.add([{
        "doc_id": "d-text", "title": "有正文的文档", "source_uri": None,
        "mime": "text/markdown", "parser_engine": "native",
        "content_hash": "h-text", "char_count": 12, "chunk_count": 0,
        "text": "这段正文必须活着到达 SQLite", "status": "ready",
        "error": None, "kb_id": "", "stored_file": None, "meta": "{}",
        "created_at": "2026-10-01T00:00:00+00:00",
        "updated_at": "2026-10-01T00:00:00+00:00"}])
    meta = MetaStore(tmp_path / "raggi.db")
    import_from_lance(meta, store)
    row = meta.query_one("SELECT text, char_count FROM documents WHERE doc_id=?",
                         ("d-text",))
    assert row["text"] == "这段正文必须活着到达 SQLite", (
        f"迁移丢了正文：{row['text']!r}")


def test_observation_counts_follow_the_same_source_as_the_list(tmp_path):
    """列表、/health、/stats 必须数同一个真源。

    元数据搬到 SQLite 后，可观测性路径若还读 Lance 旧表，就会出现
    「列表说 8 篇、health 说 7 篇并且永久 degraded」—— 这是分派式改造
    最容易漏的一类问题，因为每一条路径单独看都「没错」。
    """
    client, store, meta = _env(tmp_path, with_meta=True)
    kb = client.post("/api/kbs", json={"name": "计数一致性"}).json()
    for i in range(3):
        client.post("/api/documents/text", json={
            "text": f"计数一致性内容 {i} 关键词", "title": f"c{i}",
            "kb_id": kb["kb_id"]})

    listed = client.get("/api/documents").json()["total"]
    health = client.get("/api/health", params={"fresh": True}).json()
    stats = client.get("/api/stats").json()
    assert listed == health["doc_count"] == stats["doc_count"], (
        f"三处文档数不一致：列表 {listed} / health {health['doc_count']}"
        f" / stats {stats['doc_count']}")
    assert health["status"] == "ok", (
        f"刚入库完就 degraded：{health['count_mismatch']} "
        f"orphan={health['orphan_chunks']}")
    assert health["chunk_count"] == stats["chunk_count"]
    assert health["orphan_chunks"] == 0


def test_delete_kb_leaves_no_orphan_documents(tmp_path):
    """删库要按**当前真源**收集文档再级联，否则文档行会留在原地。

    `delete_kb` 曾经自己拼一次 Lance 查询取文档列表：元数据搬到 SQLite 后
    它查到空列表，于是库被删掉、文档还挂着已不存在的 kb_id，
    既不出现在任何列表里、也删不掉。
    """
    client, store, meta = _env(tmp_path, with_meta=True)
    kb = client.post("/api/kbs", json={"name": "待删库"}).json()
    for i in range(2):
        client.post("/api/documents/text", json={
            "text": f"级联删除内容 {i}", "title": f"k{i}",
            "kb_id": kb["kb_id"]})
    assert client.get("/api/documents", params={"kb_id": kb["kb_id"]
                                                }).json()["total"] == 2
    assert client.delete(f"/api/kbs/{kb['kb_id']}").status_code == 200
    assert meta.count("documents") == 0, "删库后仍有文档行（孤儿）"
    assert meta.count("kbs") == 0
    assert store.chunks.count_rows() == 0, "删库后仍有分块（可被检索命中）"


def test_search_titles_and_filters_follow_the_source(tmp_path):
    """检索结果的标题与 mime 过滤读的是当前真源，不是旧表。"""
    client, store, meta = _env(tmp_path, with_meta=True)
    kb = client.post("/api/kbs", json={"name": "检索归属"}).json()
    doc = client.post("/api/documents/text", json={
        "text": "检索标题验证 关键词 唯一标记 kv1", "title": "带标题的文档",
        "kb_id": kb["kb_id"]}).json()
    res = client.post("/api/search", json={"q": "kv1", "mode": "vector",
                                          "kb_id": kb["kb_id"]}).json()
    assert res["results"], "刚入库的内容按 kb 检索不到"
    assert res["results"][0]["title"] == "带标题的文档", (
        "标题为空说明检索层还在读旧引擎的 documents")
    by_mime = client.post("/api/search", json={
        "q": "kv1", "mode": "vector",
        "filters": {"mime": "text/plain"}}).json()
    assert by_mime["results"], "mime 过滤恒空：过滤条件读错引擎"


def test_sqlite_path_keeps_chunks_in_lancedb(tmp_path):
    """分块与向量**留在 LanceDB**：那是列存 + ANN + tantivy 值钱的地方。

    换引擎的边界必须钉住，否则下一步就会有人顺手把 chunks 也搬走，
    然后发现中文高频词的全文检索慢了 6–8 倍（见方案 §8.3 的实测）。
    """
    client, store, meta = _env(tmp_path, with_meta=True)
    client.post("/api/documents/text",
                json={"text": "分块仍然在 Lance 里 测试样例", "title": "块"})
    assert store.chunks.count_rows() > 0
    assert meta.count("documents") == 1


def test_auto_mode_imports_existing_lancedb_rows_once(tmp_path):
    """存量升级：SQLite 为空时从 LanceDB 旧表导入，且导入后行数一致。

    单机工具的升级路径必须是「换个版本重启就能用」，
    而不是「先照 README 手工跑一次迁移」。
    """
    from rag.storage.meta import import_from_lance, needs_import

    src = LanceStore(LocalBackend(tmp_path), 4)
    src.documents.add([{"doc_id": "d1", "title": "旧文档", "kb_id": "",
                        "content_hash": "h1", "chunk_count": 0,
                        "char_count": 3, "text": "abc", "status": "ready",
                        "error": None, "stored_file": None, "meta": "{}",
                        "parser_engine": "native", "mime": "text/plain",
                        "source_uri": None,
                        "created_at": "2026-10-01T00:00:00+00:00",
                        "updated_at": "2026-10-01T00:00:00+00:00"}])
    meta = MetaStore(tmp_path / "raggi.db")
    assert needs_import(meta)
    report = import_from_lance(meta, src)
    assert report["documents"]["ok"] is True
    assert report["documents"]["imported"] == 1
    assert not needs_import(meta), "第二次启动不该再导入一遍"
    assert meta.query_one("SELECT title FROM documents WHERE doc_id='d1'")[
        "title"] == "旧文档"


def test_import_failure_does_not_start_half_migrated(tmp_path):
    """导入失败要**喊出来**，不能留下「一半 SQLite 一半 Lance」的状态。

    那种分裂比拒绝启动更难查：读一支、写另一支，且不会报错。
    """
    from unittest import mock

    from rag.storage.meta import import_from_lance

    meta = MetaStore(tmp_path / "raggi.db")
    src = mock.MagicMock()
    # scalar_rows 是导入路径唯一的读入口；它在 import_from_lance 内部
    # 局部导入，所以要 patch 定义它的那个模块。
    with mock.patch("rag.storage.sql.scalar_rows",
                    side_effect=RuntimeError("表损坏")):
        with pytest.raises(Exception) as e:
            import_from_lance(meta, src)
    assert "导入" in str(e.value) or "损坏" in str(e.value)


def test_fallback_engine_flag_is_honoured(tmp_path):
    """`meta_engine=lancedb` 必须完全退回旧路径 —— 那是唯一的应急出口。"""
    from rag.storage.meta import prepare_meta_store

    settings = Settings()
    settings.data_dir = tmp_path
    settings.storage.meta_engine = "lancedb"
    store = LanceStore(LocalBackend(tmp_path), 4)
    assert prepare_meta_store(settings, store) is None
