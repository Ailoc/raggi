"""设计重构与原文对照查看器的回归测试。

覆盖三块：
1. **列迁移**：kb_id / stored_file 从 meta JSON 提升为真实列，
   老数据必须无损迁移且可重复执行；
2. **原文文件接口**：字节一致性、内容类型、签名 URL 鉴权、路径穿越防护；
3. **前端契约**：原文/分块双栏结构、按文件类型选择渲染方式、
   PDF 用零依赖的内置查看器而非 PDF.js。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
SRC = WEB / "src"


# ---- 1. meta → 列 迁移 -------------------------------------------------


def test_legacy_meta_kb_id_migrates_to_column(tmp_path):
    """老数据把 kb_id 存在 meta JSON 里，迁移必须把它搬进真实列。"""
    from rag.storage.tables import LanceStore
    from rag.storage.repos import fetch_rows, upsert_documents

    store = LanceStore.for_data_dir(tmp_path, 4)
    upsert_documents(store, [
        {"doc_id": "d1", "title": "旧文档", "kb_id": "", "stored_file": None,
         "meta": json.dumps({"kb_id": "kb-1", "stored_file": "d1.pdf",
                             "chunking": {"chunk_size": 512}})},
        {"doc_id": "d2", "title": "无归属", "kb_id": "", "stored_file": None,
         "meta": "{}"},
    ])
    store._backfill_doc_columns_from_meta()

    rows = {r["doc_id"]: r for r in fetch_rows(
        store.documents.search().select(["doc_id", "kb_id", "stored_file", "meta"]))}
    assert rows["d1"]["kb_id"] == "kb-1"
    assert rows["d1"]["stored_file"] == "d1.pdf"
    assert rows["d2"]["kb_id"] == ""
    # chunking 是嵌套结构，仍留在 meta 里且不能被破坏
    assert json.loads(rows["d1"]["meta"])["chunking"]["chunk_size"] == 512


def test_migration_is_idempotent(tmp_path):
    """重复迁移不改变结果（服务每次启动都会调用）。"""
    from rag.storage.tables import LanceStore
    from rag.storage.repos import fetch_rows, upsert_documents

    store = LanceStore.for_data_dir(tmp_path, 4)
    upsert_documents(store, [{
        "doc_id": "d1", "title": "t", "kb_id": "", "stored_file": None,
        "meta": json.dumps({"kb_id": "kb-1"})}])
    store._backfill_doc_columns_from_meta()
    store._backfill_doc_columns_from_meta()
    store._backfill_doc_columns_from_meta()
    row = fetch_rows(store.documents.search().select(["kb_id"]))[0]
    assert row["kb_id"] == "kb-1"


def test_migration_does_not_overwrite_column(tmp_path):
    """列里已有值时以列为准，不被 meta 里的旧值覆盖。"""
    from rag.storage.tables import LanceStore
    from rag.storage.repos import fetch_rows, upsert_documents

    store = LanceStore.for_data_dir(tmp_path, 4)
    upsert_documents(store, [{
        "doc_id": "d1", "title": "t", "kb_id": "new-kb", "stored_file": None,
        "meta": json.dumps({"kb_id": "old-kb"})}])
    store._backfill_doc_columns_from_meta()
    assert fetch_rows(store.documents.search().select(["kb_id"]))[0]["kb_id"] == "new-kb"


def test_upsert_documents_fills_missing_columns(tmp_path):
    """调用点可以不写全新列——缺失列必须自动补默认值。

    回归防护：documents 加列后，所有只传部分列的老调用点（含测试）
    都会撞上 Arrow 的 partial-schema 报错。分块早就有这个兜底，
    文档表必须有同一套机制。
    """
    from rag.storage.tables import LanceStore
    from rag.storage.repos import fetch_rows, upsert_documents

    store = LanceStore.for_data_dir(tmp_path, 4)
    # 故意不传 kb_id / stored_file / meta 等后来新增的列
    upsert_documents(store, [{"doc_id": "d1", "title": "最小行"}])
    row = fetch_rows(store.documents.search().select(
        ["doc_id", "kb_id", "stored_file"]))[0]
    assert row["doc_id"] == "d1"
    assert row["kb_id"] == ""


def test_no_scattered_meta_parsing():
    """meta 解析必须收敛到极少数集中位置。

    回归防护：kb_id / stored_file 曾散落在 20 处做
    `json.loads(meta) if isinstance(meta, str) else meta` 防御式解析，
    任何一处漏判就静默拿到空值。提升为真实列后应只剩：
    迁移函数本身 + chunking 的集中 helper。

    只统计真正的解析调用（赋值或 return），不统计注释与文档字符串里
    提到该写法的地方。
    """
    call = re.compile(r"(?:return|=)\s+_?json\.loads\(\s*meta")
    hits = []
    for f in sorted((ROOT / "rag").rglob("*.py")):
        if "build/" in str(f):
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if call.search(code):
                hits.append(f"{f.relative_to(ROOT)}:{i}")
    assert len(hits) <= 2, f"meta 解析点过多（应已收敛到 ≤2）: {hits}"


# ---- 2. 原文文件接口 ---------------------------------------------------


class _StubEmbedder:
    dim = 4
    model = "stub"

    def embed_one(self, text):  # noqa: ARG002
        return [1.0, 0.0, 0.0, 0.0]

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


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
    registry.bundle.embedder = _StubEmbedder()
    ctx = Ctx(settings, store, registry)
    c = TestClient(create_app(ctx), raise_server_exceptions=False)
    c.store = store        # type: ignore[attr-defined]
    c.settings = settings  # type: ignore[attr-defined]
    c.ctx = ctx            # type: ignore[attr-defined]
    return c


def _make_pdf(tmp_path: Path, pages: int = 2) -> bytes:
    import pymupdf

    d = pymupdf.open()
    for i in range(pages):
        p = d.new_page()
        p.insert_text((60, 80), f"Page {i + 1} content about inverters.")
    out = tmp_path / "manual.pdf"
    d.save(str(out))
    return out.read_bytes()


def test_pdf_ingest_works_without_langchain_loader(client, tmp_path):
    """PDF 解析必须直接走 pymupdf4llm。

    回归防护：langchain-community 0.4 移除了 PyMuPDF4LLMLoader
    （该包正在 sunset），继续引用会让所有 PDF 入库 500——
    而 PDF 恰恰是 RAG 最主要的文档类型。
    """
    raw = _make_pdf(tmp_path)
    r = client.post("/api/documents",
                    files={"file": ("manual.pdf", raw, "application/pdf")},
                    data={"title": "手册"})
    assert r.status_code == 200, f"PDF 入库失败: {r.text[:200]}"
    assert r.json()["status"] == "ready"
    assert r.json()["engine"] == "pymupdf4llm"


def test_pdf_pages_mapped(client, tmp_path):
    """按页解析要给出页码映射，否则无法跳页。"""
    raw = _make_pdf(tmp_path, pages=3)
    did = client.post(
        "/api/documents",
        files={"file": ("m.pdf", raw, "application/pdf")},
        data={"title": "m"}).json()["doc_id"]
    detail = client.get(f"/api/documents/{did}").json()
    pages = sorted({c["page"] for c in detail["chunks"] if c.get("page")})
    assert pages == [1, 2, 3], f"页码映射不正确: {pages}"


def test_file_endpoint_returns_exact_bytes(client, tmp_path):
    raw = _make_pdf(tmp_path)
    did = client.post(
        "/api/documents",
        files={"file": ("m.pdf", raw, "application/pdf")},
        data={"title": "m"}).json()["doc_id"]
    detail = client.get(f"/api/documents/{did}").json()
    assert detail["has_file"] is True
    assert detail["file_url"]

    r = client.get(detail["file_url"])
    assert r.status_code == 200
    assert r.content == raw, "返回字节与上传原文不一致"
    assert r.headers["content-type"].startswith("application/pdf")
    assert "inline" in r.headers.get("content-disposition", "")
    assert "bytes" in r.headers.get("accept-ranges", "")


def test_file_endpoint_download_flag(client, tmp_path):
    raw = _make_pdf(tmp_path)
    did = client.post(
        "/api/documents",
        files={"file": ("m.pdf", raw, "application/pdf")},
        data={"title": "m"}).json()["doc_id"]
    url = client.get(f"/api/documents/{did}").json()["file_url"]
    r = client.get(url + "?download=1")
    assert "attachment" in r.headers.get("content-disposition", "")


def test_file_endpoint_404_without_archive(client):
    """粘贴文本入库的文档没有留档原文，应给出可读的 404。"""
    did = client.post("/api/documents/text",
                      json={"text": "粘贴内容", "title": "t"}).json()["doc_id"]
    detail = client.get(f"/api/documents/{did}").json()
    assert detail["has_file"] is False
    assert detail["file_url"] is None
    r = client.get(f"/api/documents/{did}/file")
    assert r.status_code == 404
    assert "留档原文" in r.json()["detail"]


def test_signed_url_required_when_auth_enabled(client, tmp_path):
    """启用鉴权后：裸链接 401，签名链接 200。

    这是浏览器内嵌预览的前提——<iframe> 无法携带 Authorization 头。
    """
    from rag.storage.repos import keys as apikeys

    raw = _make_pdf(tmp_path)
    did = client.post(
        "/api/documents",
        files={"file": ("m.pdf", raw, "application/pdf")},
        data={"title": "m"}).json()["doc_id"]
    key = apikeys.create_key(client.store, "k", scope="read")[1]
    h = {"Authorization": f"Bearer {key}"}

    detail = client.get(f"/api/documents/{did}", headers=h).json()
    assert "sig=" in (detail["file_url"] or ""), "启用鉴权后应返回签名链接"

    assert client.get(f"/api/documents/{did}/file").status_code == 401
    signed = client.get(detail["file_url"])
    assert signed.status_code == 200
    assert signed.content == raw


def test_signed_url_rejects_tampering(client, tmp_path):
    # 先把文档准备好，再开鉴权——create_key 一旦执行就强制鉴权，
    # 之后不带凭据的入库会 401
    raw = _make_pdf(tmp_path)
    did = client.post(
        "/api/documents",
        files={"file": ("m.pdf", raw, "application/pdf")},
        data={"title": "m"}).json()["doc_id"]
    other = client.post("/api/documents/text",
                        json={"text": "x", "title": "y"}).json()["doc_id"]

    from rag.storage.repos import keys as apikeys

    apikeys.create_key(client.store, "k", scope="read")
    exp, sig = client.ctx.signer.sign(did)
    # 签名被篡改
    assert client.get(
        f"/api/documents/{did}/file?exp={exp}&sig=deadbeef").status_code == 403
    # 拿 A 的签名读 B：签名与 doc_id 绑定，必须被拒
    assert client.get(
        f"/api/documents/{other}/file?exp={exp}&sig={sig}").status_code == 403


def test_signed_url_rejects_expired(client):
    """过期签名必须被拒——即使签名本身完全正确。

    签名在中间件里先校验、早于文档查询，因此不需要真实文档。
    这里直接构造「签名有效但时间已过」的链接：sign() 会把 ttl 下限
    钳到 1 秒，所以不能靠传负数来造过期。
    """
    import time

    from rag.storage.repos import keys as apikeys

    apikeys.create_key(client.store, "k", scope="read")
    doc_id = "any-doc-id"
    past = int(time.time()) - 100
    sig = client.ctx.signer._digest(doc_id, past)   # 签名对，但时间过了
    r = client.get(f"/api/documents/{doc_id}/file?exp={past}&sig={sig}")
    assert r.status_code == 403, f"过期签名未被拒绝: {r.status_code}"
    assert "过期" in r.json()["detail"] or "签名" in r.json()["detail"]


def test_signer_ttl_floor():
    """ttl 下限钳到 1 秒：避免签出「立即过期」的不可用链接。"""
    import time

    from rag.core.signing import UrlSigner

    import tempfile
    from pathlib import Path

    s = UrlSigner(Path(tempfile.mkdtemp()))
    exp, _ = s.sign("d", ttl=-999)
    assert exp >= int(time.time()), "过期时间不应早于当前时刻"


def test_preview_ttl_covers_reading_session():
    """原文链接的有效期要够读完一份文档。

    回归防护：TTL 曾是 600 秒。读一份 PDF 动辄十几分钟，读到一半点
    右侧分块时 iframe 会带过期签名重新加载，直接变成 403 空白页。
    """
    from rag.core.signing import DEFAULT_TTL

    assert DEFAULT_TTL >= 1800, (
        f"原文链接有效期仅 {DEFAULT_TTL}s，读长文档中途会失效")


def test_path_traversal_rejected(tmp_path):
    """stored_file 即使被写脏也不能读到 files_dir 之外。"""
    from rag.core.signing import safe_join

    base = tmp_path / "files"
    base.mkdir()
    assert safe_join(base, "ok.pdf") is not None
    for bad in ("../secret", "../../etc/passwd", "a/b.pdf",
                "..\\windows", ".hidden", ""):
        assert safe_join(base, bad) is None, f"未拦截 {bad!r}"


# ---- 3. 前端契约 -------------------------------------------------------


def test_doc_view_is_two_pane():
    """文档详情页必须是「左原文 / 右分块」双栏。

    阶段 4 起双栏骨架搬进了 ui/SplitView.svelte。原来这条守卫查的是
    `id="docSplit"` / `id="sourceBody"` / `id="chunkWrap"` 三个 id——
    而它们**没有任何代码引用**（只是当年手写 DOM 时代留下的挂钩），
    等于把"存在三个没用的 id"当成了契约。改查真正的结构：
    视图用 SplitView，且 SplitView 确实渲染出两个带标签的面板。
    """
    view = (WEB / "src" / "views" / "DocView.svelte").read_text(encoding="utf-8")
    assert "SplitView" in view, "文档页未使用双栏组件"
    assert 'leftLabel="原文"' in view, "缺少原文面板标签"
    assert 'rightLabel="分块列表"' in view, "缺少分块面板标签"
    assert "{#snippet leftBody()}" in view and "{#snippet rightBody()}" in view, \
        "双栏槽位缺失"

    split = (WEB / "src" / "ui" / "SplitView.svelte").read_text(encoding="utf-8")
    assert split.count('<section class="pane"') == 2, "SplitView 必须恰好两栏"
    assert "aria-label={leftLabel}" in split and "aria-label={rightLabel}" in split, \
        "两栏缺少可读名称"
    assert "原文" in view and "分块" in view


def test_source_uses_native_pdf_viewer():
    """PDF 用浏览器内置查看器（iframe + #page），不引入 PDF.js。

    这是刻意的取舍：项目零运行时依赖，而主流浏览器都内置 PDF 查看器
    且支持 #page=N 跳页——分块到页码的联动用这一条就够。
    """
    src = (SRC / "lib" / "source.ts").read_text(encoding="utf-8")
    assert "iframe" in src, "未使用 iframe 内嵌原文"
    assert "#page=" in src, "缺少按页码跳转"
    assert "pdfjs" not in src.lower(), "不应引入 PDF.js"
    assert "cdn" not in src.lower(), "不应依赖外部 CDN"


def test_source_handles_each_file_kind():
    """按类型分派：PDF / 图片 / 文本 / 需下载。"""
    src = (SRC / "lib" / "source.ts").read_text(encoding="utf-8")
    for token in ("pdf", "image", "text", "download"):
        assert f'"{token}"' in src or f"'{token}'" in src, f"缺少 {token} 分支"
    assert "application/pdf" in src


def test_source_falls_back_to_parsed_text():
    """没有留档原文件时用解析全文兜底，而不是空白。"""
    view = (SRC / "views" / "DocView.svelte").read_text(encoding="utf-8")
    assert "doc.text" in view, "未回退到解析全文"


def test_chunk_click_reveals_source():
    """点分块要驱动原文定位，且尊重 offset_valid。"""
    view = (SRC / "views" / "DocView.svelte").read_text(encoding="utf-8")
    assert "revealChunk" in view, "未接入原文定位"
    assert "offset_valid" in view, "未检查偏移有效性"
    assert "is-selected" in view, "缺少选中态"


def test_chunks_api_exposes_locator_fields():
    """分块列表必须返回定位所需字段，否则前端无从跳转。"""
    api = (ROOT / "rag/api/chunks.py").read_text(encoding="utf-8")
    for col in ("page", "char_start", "char_end", "offset_valid"):
        assert f'"{col}"' in api, f"/api/chunks 未返回 {col}"


def test_lists_surface_total_for_pagination():
    """文档列表与分块列表都要把 total 呈现给用户。

    回归防护：两者原先都硬截断（文档 200 / 分块 500）且丢弃 total——
    超出上限的内容静默消失，用户不知道还有东西没看到，
    更没法把它们调出来。
    """
    docs = (SRC / "views" / "KbDocs.svelte").read_text(encoding="utf-8")
    assert "total" in docs, "文档列表未读取 total"
    assert "再加载" in docs, "文档列表缺「加载更多」"
    assert "limit=${limit}" in docs or "limit=" in docs, "文档列表限额未参数化"

    chunks = (SRC / "views" / "DocView.svelte").read_text(encoding="utf-8")
    assert "total" in chunks, "分块列表未读取 total"
    assert "再加载" in chunks, "分块列表缺「加载更多」"


def test_pagination_uses_api_total_not_item_count():
    """分页判断必须基于后端 total，而不是已取回的条数。

    只比较「取回条数 < 请求上限」会漏掉「刚好取满」的情况——
    恰好 200 篇时用户不会看到任何提示，但可能还有 201 篇。
    """
    # total 缺失时要有兜底（旧响应可能没有该字段），否则页脚会显示 undefined
    docs = (SRC / "views" / "KbDocs.svelte").read_text(encoding="utf-8")
    assert "r.total ??" in docs, "文档列表未对 total 缺失做兜底"
    # 阶段 2 起分页判断挪进了 lib/loadstate.svelte.ts 的 Pager：
    # 视图侧只写 `pager.more`，而它的定义是 offset < total（total 来自后端）。
    # 守卫跟着挪，但牙齿不变：判断依据必须是后端 total，且不得拿
    # "已取回条数 < 请求上限" 当分页依据。
    assert "pager.more" in docs, "文档列表未用基于后端 total 的分页判断"
    assert "< limit" not in docs, "分页判断退回了「已取回条数 < 请求上限」"
    paged = (SRC / "lib" / "loadstate.svelte.ts").read_text(encoding="utf-8")
    assert "this.offset < this.total" in paged, \
        "Pager.more 必须基于后端 total，而不是已取回的条数"

    chunks = (SRC / "views" / "DocView.svelte").read_text(encoding="utf-8")
    assert "r.total ??" in chunks, "分块列表未对 total 缺失做兜底"
    # 阶段 4：DocView 的分页判断也搬进了 Pager（offset < total）
    assert "pager.more" in chunks, "分块列表未用基于后端 total 的分页判断"
    assert "< limit" not in chunks, "分页判断退回了「已取回条数 < 请求上限」"


def test_doc_view_chunk_management():
    """文档页的分块要能编辑 / 新增 / 删除 / 批量启停，而不是只读表格。

    回归防护：Svelte 重写时这些操作一度全部丢失——后端端点齐备、
    CSS 类也都在，只有界面没跟上，用户只能删了重传。
    """
    view = (SRC / "views" / "DocView.svelte").read_text(encoding="utf-8")
    for label in ("编辑", "删除", "新增分块", "批量停用", "批量启用"):
        assert label in view, f"缺少「{label}」入口"
    ops = (SRC / "lib" / "chunkops.ts").read_text(encoding="utf-8")
    assert "/enabled" in ops, "启停未走 /enabled 端点（停用应保留数据）"
    assert "force: true" in ops, "解析分块编辑须显式 force，否则后端 403"
    assert 'method: "DELETE"' in ops, "缺少删除调用"
    # 独立分块与文档分块共用同一套操作，避免两边行为漂移
    chunks = (SRC / "views" / "KbChunks.svelte").read_text(encoding="utf-8")
    assert "chunkops" in chunks, "独立分块页未复用分块操作"


def test_chunk_table_is_virtualized():
    """长分块表按视口窗口化渲染，节点数与数据量脱钩。

    回归防护：3000 块的手册曾渲染 3000 个 <tr>，输入过滤时每次按键
    都要 diff 全表。窗口化依赖固定行高、窗口切片与占位行，缺一不可。
    """
    view = (SRC / "views" / "DocView.svelte").read_text(encoding="utf-8")
    assert "windowOf" in view, "未接入窗口计算"
    assert "visibleChunks" in view, "未按窗口切片渲染"
    assert "padTop" in view and "padBottom" in view, "缺少撑高占位行"
    lib = (SRC / "lib" / "virtual.ts").read_text(encoding="utf-8")
    assert "OVERSCAN" in lib, "未预留过扫描缓冲，快速滚动会先看到空白"
    assert "ROW_H" in lib, "未固定行高——变高行需要逐行测量，得不偿失"