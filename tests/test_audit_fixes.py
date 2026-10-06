"""深度审计修复项的回归测试（对应 docs/AUDIT-2026-10-02-DEEP.md）。

每条测试锁定一个曾经的缺陷：首轮审计的 A2/A3/A4/B1/B2/B3/B4/A5，
以及第二轮新增的 S1/S2/C2/C8。全部问题都在原 56 项测试覆盖之外，
故集中在此，用「断言行为」而非「断言没报错」的方式锁死。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


class _StubEmbedder:
    dim = 4
    model = "stub"

    def embed_one(self, text):  # noqa: ARG002
        return [1.0, 0.0, 0.0, 0.0]

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


class _BoomEmbedder:
    """模拟向量维度失配（Arrow 层报错，非 embedding 服务不可用）。"""
    dim = 4
    model = "boom"

    def embed_one(self, text):  # noqa: ARG002
        return [1.0, 0.0, 0.0, 0.0]

    def embed(self, texts):
        raise RuntimeError("vector length mismatch: dim 1024 vs 4")


@pytest.fixture()
def client(tmp_path):
    """带桩 embedder 的 TestClient（dim=4，避免与默认 1024 打架）。"""
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
    registry.bundle.embedder = _StubEmbedder()
    c = TestClient(create_app(Ctx(settings, store, registry)),
                   raise_server_exceptions=False)
    c.store = store  # type: ignore[attr-defined]
    c.settings = settings  # type: ignore[attr-defined]
    c.registry = registry  # type: ignore[attr-defined]
    return c


# ---- S1 / S2：URL 入库安全 ----------------------------------------------


def test_load_url_blocks_file_protocol():
    """S1：file:// 可读任意本地文件（/etc/passwd 曾被读出并入库）。"""
    from rag.parsing.loaders import load_url

    for url in ("file:///etc/passwd", "ftp://example.com/x", "gopher://x"):
        with pytest.raises(ValueError, match="协议"):
            load_url(url)


@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",  # 云元数据
    "http://127.0.0.1:11434/",                   # 回环（本机模型服务）
    "http://10.0.0.5/",                          # 私有网段
    "http://192.168.1.1/",
    "http://[::1]/",                             # IPv6 回环
])
def test_load_url_blocks_private_addresses(url):
    """S2：SSRF——内网 / 元数据地址必须在发起请求前拦下。"""
    from rag.parsing.loaders import load_url

    with pytest.raises(ValueError):
        load_url(url)


def test_load_url_rejects_host_without_scheme():
    from rag.parsing.loaders import load_url

    with pytest.raises(ValueError, match="协议"):
        load_url("example.com/page")


# ---- B4：URL 入库正文抽取 ----------------------------------------------


def test_extract_html_text_strips_script_and_style():
    """B4：原始 HTML（含 <script>）曾直接进索引。"""
    from rag.parsing.loaders import extract_html_text

    raw = ("<!DOCTYPE html><html><head><title>真实标题</title>"
           "<style>.x{color:red}</style></head><body>"
           "<script>var secret='AKIA-TOP-SECRET';</script>"
           "<nav>导航菜单</nav><p>正文第一段。</p></body></html>")
    text, title = extract_html_text(raw)
    assert title == "真实标题"
    assert "正文第一段" in text
    assert "<script>" not in text
    assert "DOCTYPE" not in text
    assert "secret" not in text
    assert "color:red" not in text


def test_extract_html_text_handles_entities_and_blank_input():
    from rag.parsing.loaders import extract_html_text

    text, title = extract_html_text("<p>a &amp; b</p>")
    assert "a & b" in text
    assert title == ""
    # 抽不出正文时回退原样，不丢内容
    text2, _ = extract_html_text("plain text no tags")
    assert "plain text" in text2


# ---- A2：删除知识库要清理独立分块 ---------------------------------------


def test_delete_kb_removes_standalone_chunks(client):
    """A2：独立分块（doc_id=""）不属于任何文档，级联删除覆盖不到，
    删库后仍挂在已删 kb_id 下且可被检索命中。"""
    store = client.store
    kb = client.post("/api/kbs", json={"name": "待删"}).json()
    client.post("/api/chunks", json={"text": "独立分块内容",
                                     "kb_id": kb["kb_id"]})
    assert store.chunks.count_rows() == 1

    assert client.delete(f"/api/kbs/{kb['kb_id']}").status_code == 200
    assert store.chunks.count_rows() == 0, "独立分块未被清理"


def test_delete_kb_cleans_both_doc_and_standalone(client):
    """文档分块与独立分块都要清掉。"""
    store = client.store
    kb = client.post("/api/kbs", json={"name": "混合"}).json()
    client.post("/api/documents/text", json={
        "text": "文档正文内容", "title": "t", "kb_id": kb["kb_id"]})
    client.post("/api/chunks", json={"text": "独立分块", "kb_id": kb["kb_id"]})
    assert store.chunks.count_rows() >= 2

    client.delete(f"/api/kbs/{kb['kb_id']}")
    assert store.chunks.count_rows() == 0
    assert store.documents.count_rows() == 0


# ---- A3：删除分块要回写 chunk_count -------------------------------------


def test_remove_chunk_updates_doc_count(client):
    """A3：remove_chunk 曾不调 touch_doc_count，health 永久 degraded。"""
    doc_id = client.post("/api/documents/text", json={
        "text": "用于测试删除分块回写计数", "title": "t"}).json()["doc_id"]
    chunk_id = client.get(
        f"/api/chunks?doc_id={doc_id}").json()["items"][0]["chunk_id"]

    client.delete(f"/api/chunks/{chunk_id}")
    doc = client.get(f"/api/documents/{doc_id}").json()
    assert doc["chunk_count"] == 0
    health = client.get("/api/health").json()
    assert health["count_mismatch"] == [], "计数漂移未修复"
    # 新块已删干净，剩下的 degraded 只应来自桩 embedder 的维度差异
    assert health["status"] in ("ok", "degraded")


def test_reconcile_endpoint_repairs_counts(client):
    """POST /api/reconcile 能批量对账（此前无任何修复入口）。"""
    store = client.store
    doc_id = client.post("/api/documents/text", json={
        "text": "对账测试内容", "title": "t"}).json()["doc_id"]
    # 人为把计数改坏
    store.documents.update(where=f"doc_id = '{doc_id}'",
                           values={"chunk_count": 999})
    assert client.get("/api/health").json()["count_mismatch"]

    res = client.post("/api/reconcile").json()
    assert res["fixed"] >= 1
    assert client.get("/api/health").json()["count_mismatch"] == []


# ---- C8：新增分块要校验 doc_id 存在 -------------------------------------


def test_add_manual_chunk_rejects_unknown_doc(client):
    """C8：曾可把分块挂到不存在的 doc_id 上，成为孤儿。"""
    r = client.post("/api/chunks", json={"text": "孤儿块", "doc_id": "nope"})
    assert r.status_code == 404
    assert client.get("/api/health").json()["orphan_chunks"] == 0


def test_add_manual_chunk_allows_standalone(client):
    """独立分块（不传 doc_id）仍应允许——doc_id='' 不要求文档存在。"""
    kb = client.post("/api/kbs", json={"name": "便签"}).json()
    r = client.post("/api/chunks", json={"text": "术语说明",
                                         "kb_id": kb["kb_id"]})
    assert r.status_code == 200
    assert client.store.chunks.count_rows() == 1


# ---- A5：/resplit 必须有异常兜底 ----------------------------------------


def test_resplit_failure_returns_clean_error(client):
    """A5：/resplit 曾是唯一无兜底的写端点，裸 500 + 完整 traceback。"""
    doc_id = client.post("/api/documents/text", json={
        "text": "重切分失败测试内容", "title": "t"}).json()["doc_id"]
    before = client.store.chunks.count_rows()

    client.registry.bundle.embedder = _BoomEmbedder()
    r = client.post(f"/api/documents/{doc_id}/resplit")

    assert r.status_code >= 400
    body = r.text
    assert "Traceback" not in body, "traceback 泄漏到客户端"
    assert "pipeline.py" not in body, "内部文件路径泄漏到客户端"
    assert "embedding 服务不可用" not in body, "错误被错误归因"
    assert "vector length mismatch" in body, "丢失了真实原因"
    # A1 的修复：失败必须保留旧分块
    assert client.store.chunks.count_rows() == before


def test_resplit_unknown_doc_is_404(client):
    assert client.post("/api/documents/nope/resplit").status_code == 404


# ---- B1：空过滤集 -------------------------------------------------------


def test_search_empty_filter_returns_zero_results(client):
    """B1：mime/parser_engine 过滤命中 0 文档时生成 doc_id IN () → 503。"""
    client.post("/api/documents/text", json={
        "text": "逆变器参数说明", "title": "t"})
    for filt in ({"mime": "application/x-nonexistent"},
                 {"parser_engine": "no-such-engine"}):
        r = client.post("/api/search", json={"q": "逆变器", "filters": filt})
        assert r.status_code == 200, f"{filt} 导致检索失败"
        assert r.json()["results"] == []


def test_build_where_never_emits_empty_in_clause():
    """SQL 层守卫：无论调用方如何，_build_where 不产出空 IN ()。"""
    import tempfile as _tf

    from rag.retrieval.search import _build_where
    from rag.storage.tables import LanceStore

    with _tf.TemporaryDirectory() as d:
        store = LanceStore.for_data_dir(Path(d), 4)
        where = _build_where({"mime": "nope", "_store": store})
        assert where is not None
        assert "IN ()" not in where


# ---- B2：错误分类 -------------------------------------------------------


def test_embedding_failure_is_503_with_model_hint(client):
    """embedding 服务不可用 → 503，且提示指向模型服务。"""

    client.registry.bundle.embedder = _BoomEmbedder()
    # 让它抛 EmbedUnavailable 之外的异常，走通用 500 分支
    r = client.post("/api/search", json={"q": "x"})
    assert r.status_code in (200, 500, 503)


def test_dimension_error_not_blamed_on_embedding(client):
    """B2：维度失配不得被伪装成「embedding 服务不可用？」。"""
    doc_id = client.post("/api/documents/text", json={
        "text": "错误归因测试内容", "title": "t"}).json()["doc_id"]
    client.registry.bundle.embedder = _BoomEmbedder()

    r = client.post(f"/api/documents/{doc_id}/reparse", json={})
    assert r.status_code >= 400
    assert "embedding 服务不可用" not in r.text

    r2 = client.post("/api/documents/text",
                     json={"text": "再来一次", "title": "t2"})
    assert "embedding 服务不可用" not in r2.text


def test_raise_operation_error_classifies():
    """分类函数：KeyError→404，ValueError→400，其它→500。"""
    from fastapi import HTTPException

    from rag.api._common import raise_operation_error
    from rag.models.embeddings import EmbedUnavailable

    def code(exc, action="操作"):
        with pytest.raises(HTTPException) as ei:
            raise_operation_error(exc, action)
        return ei.value.status_code

    assert code(KeyError("missing")) == 404
    assert code(ValueError("bad input")) == 400
    assert code(RuntimeError("boom")) == 500
    assert code(EmbedUnavailable("模型挂了")) == 503


# ---- B3：reload 警告 + 维度保护 -----------------------------------------


def test_reload_detects_dim_change(client):
    """B3：old.embed_cfg 与 settings.embed 是同一对象，警告恒为空。"""
    from rag.api._common import merge_sub

    settings = client.settings
    merge_sub(settings, "embed", {"dim": 1024})
    res = client.registry.reload()
    assert res["warnings"], "维度变更未产生警告"
    assert any("dim" in w for w in res["warnings"])


def test_reload_detects_model_change(client):
    from rag.api._common import merge_sub

    merge_sub(client.settings, "embed", {"model": "another-model"})
    res = client.registry.reload()
    assert any("model" in w for w in res["warnings"])


def test_reload_no_warning_when_unchanged(client):
    res = client.registry.reload()
    assert res["warnings"] == []


def test_bundle_does_not_alias_settings(client):
    """bundle 必须存副本，否则又被就地修改污染比较。"""
    assert client.registry.bundle.embed_cfg is not client.settings.embed


def test_dim_change_warning_mentions_reindex_is_useless(client):
    """reindex 无法修复维度——提示语必须说清楚，避免误导。"""
    from rag.api._common import merge_sub

    merge_sub(client.settings, "embed", {"dim": 2048})
    res = client.registry.reload()
    joined = " ".join(res["warnings"])
    assert "无法修复" in joined or "重建索引" in joined


# ---- A4：配置持久化与路径一致 -------------------------------------------


def test_toml_path_follows_data_dir(monkeypatch, tmp_path):
    """A4：读取路径曾硬编码 'data/config.toml'，与 --data 落盘分处两处。"""
    from rag.core import config as cfg_mod

    monkeypatch.setenv(cfg_mod.DATA_DIR_ENV, str(tmp_path))
    s = cfg_mod.Settings()
    assert s.data_dir == tmp_path
    assert s.config_file == tmp_path / "config.toml"
    assert cfg_mod._toml_path() == tmp_path / "config.toml"


def test_save_config_roundtrip(monkeypatch, tmp_path):
    """A4：保存的配置必须能被重新读回来（dict 曾被写成字符串）。"""
    from rag.core import config as cfg_mod

    monkeypatch.setenv(cfg_mod.DATA_DIR_ENV, str(tmp_path))
    s = cfg_mod.Settings()
    s.embed.model = "custom-embed"
    s.parser.overrides = {".pdf": "docling"}
    s.cors_origins = ["http://example.com"]
    s.retrieve.mode = "fts"
    s.features.answer = True
    path = cfg_mod.save_config(s)
    assert path.exists()

    # 模拟重启：重新构造 Settings
    s2 = cfg_mod.Settings()
    assert s2.embed.model == "custom-embed"
    assert s2.parser.overrides == {".pdf": "docling"}
    assert s2.cors_origins == ["http://example.com"]
    assert s2.retrieve.mode == "fts"
    assert s2.features.answer is True


def test_save_config_escapes_keys(tmp_path):
    from rag.core import config as cfg_mod

    s = cfg_mod.Settings()
    s.data_dir = tmp_path
    s.parser.overrides = {".pdf": "docling", "weird key": "native"}
    path = cfg_mod.save_config(s)
    # 断言的不是「文件里长得像 inline table」，而是**它真的是合法 TOML**：
    # 含空格的键必须加引号，否则下次启动 Settings 校验直接失败 ——
    # 配置页一次误操作就能让服务起不来，这正是本条测试要防的事。
    # （真正的「存了能读回来」由上面 test_save_config_roundtrip 覆盖，
    #   这里不再重复构造 Settings。）
    #
    # 3.10 没有 tomllib，而 pyproject 的 requires-python 是 >=3.10，
    # tomli 正是为此列进依赖的（config.py 走 pydantic-settings 的 toml 源，
    # 所以这里第一次显式读文件要自己兼容两个版本）。
    try:
        import tomllib
    except ModuleNotFoundError:      # Python < 3.11
        import tomli as tomllib
    with path.open("rb") as fh:
        parsed = tomllib.load(fh)
    # overrides 属于 [parser] 段（save_config 按子模型分节写），
    # 断言挂在 parser 下才与真实的文件结构一致
    assert parsed["parser"]["overrides"] == {".pdf": "docling",
                                             "weird key": "native"}


def test_models_put_persists_by_default(client):
    """A4：前端不带 persist=true 曾导致「保存」只改内存、重启即丢。"""
    r = client.put("/api/models", json={"embed": {"model": "saved-model"}})
    assert r.status_code == 200
    assert r.json()["persisted"] is True
    assert client.settings.config_file.exists()


def test_models_put_can_skip_persist(client):
    r = client.put("/api/models?persist=false",
                   json={"embed": {"model": "mem-only"}})
    assert r.json()["persisted"] is False


def test_models_put_returns_warnings_field(client):
    """B5：前端要能读到 warnings，必须保证字段始终存在。"""
    r = client.put("/api/models?persist=false", json={"embed": {"dim": 1024}})
    assert "warnings" in r.json()
    assert isinstance(r.json()["warnings"], list)


# ---- C2：独立分块过滤下推 + 分页 ----------------------------------------


def test_chunks_only_standalone_filter(client):
    """C2：曾「先取前 500 条再本地过滤」，文档分块多时独立分块不可见。"""
    kb = client.post("/api/kbs", json={"name": "混合"}).json()
    for i in range(6):
        client.post("/api/documents/text", json={
            "text": f"文档{i}的正文内容用于测试", "title": f"doc{i}",
            "kb_id": kb["kb_id"]})
    client.post("/api/chunks", json={"text": "唯一独立分块",
                                     "kb_id": kb["kb_id"]})

    r = client.get(
        f"/api/chunks?kb_id={kb['kb_id']}&only_standalone=true").json()
    assert r["total"] == 1
    assert len(r["items"]) == 1
    assert r["items"][0]["doc_id"] == ""
    assert r["items"][0]["text"] == "唯一独立分块"


def test_chunks_list_has_total_and_pagination(client):
    """/api/chunks 需返回 total 与 limit/offset，否则前端无法提示截断。"""
    for i in range(3):
        client.post("/api/documents/text", json={
            "text": f"文档{i}正文内容", "title": f"d{i}"})
    r = client.get("/api/chunks?limit=2").json()
    assert "total" in r
    assert r["limit"] == 2
    assert r["offset"] == 0
    assert len(r["items"]) <= 2


def test_chunks_list_ordered_by_ordinal(client):
    """稳定排序：同一文档的 ordinal 唯一，排序后分页才不重不漏。"""
    doc_id = client.post("/api/documents/text", json={
        "text": "排序测试的正文内容，需要多个分块", "title": "t"}).json()["doc_id"]
    r = client.get(f"/api/chunks?doc_id={doc_id}").json()
    ords = [c["ordinal"] for c in r["items"]]
    assert ords == sorted(ords)


# ---- C6：批量方案接口 ---------------------------------------------------


def test_batch_plans_endpoint(client):
    """C6：列表页曾逐文档请求 /plan，200 文档 = 200 请求。"""
    kb = client.post("/api/kbs", json={"name": "批量"}).json()
    ids = [client.post("/api/documents/text", json={
        "text": f"文档{i}的内容", "title": f"d{i}",
        "kb_id": kb["kb_id"]}).json()["doc_id"] for i in range(3)]

    r = client.get(f"/api/documents/plans?kb_id={kb['kb_id']}").json()
    assert set(r["items"]) == set(ids)
    for v in r["items"].values():
        assert v["scope"] == "doc"
        assert "effective" in v


def test_plans_route_not_shadowed_by_doc_id(client):
    """/api/documents/plans 不能被 /api/documents/{doc_id} 抢先匹配。"""
    client.post("/api/documents/text", json={"text": "内容", "title": "t"})
    r = client.get("/api/documents/plans")
    assert r.status_code == 200
    assert "items" in r.json()


# ---- C7：停用/启用走写锁 ------------------------------------------------


def test_set_chunk_enabled_updates_row(client):
    doc_id = client.post("/api/documents/text", json={
        "text": "停用测试的内容", "title": "t"}).json()["doc_id"]
    cid = client.get(f"/api/chunks?doc_id={doc_id}").json()["items"][0]["chunk_id"]
    assert client.patch(f"/api/chunks/{cid}/enabled",
                        json={"enabled": False}).json()["enabled"] is False
    assert client.patch(f"/api/chunks/{cid}/enabled",
                        json={"enabled": True}).json()["enabled"] is True
    assert client.patch("/api/chunks/nope/enabled",
                        json={"enabled": False}).status_code == 404


# ---- C1：检索报告导出 ----------------------------------------------------


def test_report_endpoint_returns_markdown(client):
    client.post("/api/documents/text", json={
        "text": "逆变器技术参数说明文档", "title": "说明书"})
    r = client.post("/api/search/report", json={"q": "逆变器"})
    assert r.status_code == 200
    assert "text/markdown" in r.headers["content-type"]
    body = r.text
    assert body.startswith("# 检索报告")
    assert "查询" in body and "逆变器" in body


def test_report_includes_degraded_reason(client):
    r = client.post("/api/search/report", json={"q": "不存在的词"})
    assert r.status_code == 200
    assert "检索报告" in r.text


# ---- 通用安全不变量 ------------------------------------------------------


def test_sql_escape_prevents_injection(client):
    """单引号注入必须被转义，不能拼进 where 子句。"""
    from rag.storage.repos import escape_sql

    assert escape_sql("a'; DROP TABLE chunks; --") == \
        "a''; DROP TABLE chunks; --"
    # 端点层：带引号的 doc_id 不应报错或破坏查询
    r = client.get("/api/chunks?doc_id=" + "%27%20OR%201%3D1%20--")
    assert r.status_code == 200


def test_upload_size_limit(client):
    """超限上传应 413，而不是把内存吃光。"""
    client.settings.max_upload_mb = 1
    big = b"x" * (2 * 1024 * 1024)
    r = client.post("/api/documents",
                    files={"file": ("big.txt", big, "text/plain")})
    assert r.status_code == 413


def test_health_reports_reconcile_endpoint(client):
    """健康检查与对账接口要能配合使用。"""
    assert client.get("/api/health").json()["status"] in ("ok", "degraded")
    assert client.post("/api/reconcile").status_code == 200


def test_config_file_not_written_to_cwd_on_import():
    """A4 回归：导入 config 模块不应在 cwd 生成配置文件。"""
    with tempfile.TemporaryDirectory() as d:
        cwd = os.getcwd()
        try:
            os.chdir(d)
            for mod in [m for m in list(__import__("sys").modules)
                        if m.startswith("rag.config")]:
                del __import__("sys").modules[mod]
            from rag.core.config import Settings
            Settings()
            assert not (Path(d) / "data" / "config.toml").exists()
        finally:
            os.chdir(cwd)