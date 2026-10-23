"""架构重写的回归测试：存储后端抽象 + 分块方案两级化。

对应本轮重写的两块核心能力：
1. **存储后端可切换**（local 默认 / s3 接 RustFS）——配置校验、连接参数、
   path-style 寻址等 S3 兼容要点；
2. **分块方案两级化**——知识库持有具体值，不再有运行时的「继承全局」。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "web" / "src"


# ---- 存储后端抽象 ------------------------------------------------------


def test_local_backend_is_default(tmp_path):
    from rag.core.config import Settings, StorageConfig

    assert StorageConfig().backend == "local"
    assert Settings().storage.backend == "local"


def test_local_backend_uri_and_blobs(tmp_path):
    from rag.storage.backend import LocalBackend

    b = LocalBackend(tmp_path)
    assert b.kind == "local"
    assert b.lance_uri() == str(tmp_path / "lancedb")
    assert b.lance_options() == {}

    b.put_blob("a.pdf", b"hello")
    assert b.blob_exists("a.pdf")
    assert b.read_blob("a.pdf") == b"hello"
    assert b.local_path("a.pdf") is not None, "本地后端应提供直接路径"
    b.delete_blob("a.pdf")
    assert not b.blob_exists("a.pdf")


@pytest.mark.parametrize("bad", ["../x", "a/b", "..\\x", ".hidden", ""])
def test_local_backend_rejects_unsafe_names(tmp_path, bad):
    from rag.core.errors import Invalid
    from rag.storage.backend import LocalBackend

    b = LocalBackend(tmp_path)
    with pytest.raises(Invalid):
        b.put_blob(bad, b"x")


def _s3_cfg(**kw):
    base = dict(backend="s3", endpoint="http://127.0.0.1:9000",
                bucket="raggi", access_key="ak", secret_key="sk",
                region="us-east-1", allow_http=True, prefix="env1")
    base.update(kw)
    return type("C", (), base)


def test_s3_backend_uri_and_prefix():
    from rag.storage.backend import S3Backend

    b = S3Backend(endpoint="http://127.0.0.1:9000", bucket="raggi",
                  access_key="ak", secret_key="sk", prefix="env1")
    assert b.lance_uri() == "s3://raggi/env1/lancedb"


def test_s3_options_enable_path_style_and_http():
    """RustFS / MinIO 默认不支持 virtual-host 寻址，且本地多无 TLS。

    这两项配错会表现为「连接超时」或「NoSuchBucket」——很难从现象反推，
    因此在这里钉死。
    """
    from rag.storage.backend import S3Backend

    opts = S3Backend(endpoint="http://127.0.0.1:9000", bucket="raggi",
                     access_key="ak", secret_key="sk").lance_options()
    assert opts["virtual_hosted_style_request"] == "false", "未启用 path-style"
    assert opts["allow_http"] == "true", "未允许明文 HTTP"
    assert opts["aws_endpoint"] == "http://127.0.0.1:9000"
    assert opts["aws_access_key_id"] == "ak"
    assert opts["bucket"] == "raggi"


def test_s3_backend_has_no_local_path():
    """对象存储没有本地路径——路由层据此回落到流式读取。"""
    from rag.storage.backend import S3Backend

    b = S3Backend(endpoint="http://x:9000", bucket="b",
                  access_key="a", secret_key="s")
    assert b.local_path("a.pdf") is None


@pytest.mark.parametrize("kw,msg", [
    ({"endpoint": ""}, "endpoint"),
    ({"bucket": ""}, "bucket"),
    ({"access_key": ""}, "access_key"),
    ({"secret_key": ""}, "secret_key"),
])
def test_s3_requires_full_config(kw, msg):
    """配置不全时在**构造阶段**报错，而不是等首次请求才炸。"""
    from rag.core.errors import Invalid
    from rag.storage.backend import build_backend

    with pytest.raises(Invalid, match=msg):
        build_backend(_s3_cfg(**kw), Path("/tmp/x"))


def test_unknown_backend_rejected():
    from rag.core.errors import Invalid
    from rag.storage.backend import build_backend

    with pytest.raises(Invalid, match="未知的存储后端"):
        build_backend(_s3_cfg(backend="ftp"), Path("/tmp/x"))


def test_lance_store_takes_backend(tmp_path):
    """LanceStore 由后端提供连接参数——业务层不知道数据在哪。"""
    from rag.storage.backend import LocalBackend
    from rag.storage.tables import LanceStore

    store = LanceStore(LocalBackend(tmp_path), dim=4)
    assert store.backend.kind == "local"
    assert store.uri == str(tmp_path / "lancedb")


def test_stats_reflects_backend_kind(tmp_path):
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(tmp_path, 4)
    s = store.stats()
    assert s["backend"] == "local"
    # 本地给磁盘指标；对象存储给对象数——两者不混为一谈
    assert "disk_free_bytes" in s
    assert "object_count" not in s


# ---- 分块方案两级化 ----------------------------------------------------


def _store(tmp_path):
    from rag.storage.tables import LanceStore

    return LanceStore.for_data_dir(tmp_path, 4)


def test_create_kb_materializes_plan(tmp_path):
    """新建知识库直接持有具体值，不留「chunk_size=0 表示继承」。"""
    from rag.storage.repos import kbs

    store = _store(tmp_path)
    kb = kbs.create_kb(store, "库", default_size=768, default_ratio=15.0)
    assert kb["chunk_size"] == 768, "未物化系统默认值"
    assert kb["overlap_ratio"] == 15.0


def test_create_kb_explicit_values_win(tmp_path):
    from rag.storage.repos import kbs

    store = _store(tmp_path)
    kb = kbs.create_kb(store, "库", chunk_size=1024, overlap_ratio=20,
                       default_size=512, default_ratio=10.0)
    assert kb["chunk_size"] == 1024


def test_resolve_plan_source_is_kb_not_global(tmp_path):
    """两级模型下没有「global」这个用户可见层级。"""
    from rag.core.config import Settings
    from rag.storage import plan as chunking
    from rag.storage.repos import kbs

    store, s = _store(tmp_path), Settings()
    kb = kbs.create_kb(store, "库", default_size=512, default_ratio=10.0)
    p = chunking.resolve_plan(store, s, kb_id=kb["kb_id"])
    assert p["source"] == "kb"
    assert p["chunk_size"] == 512

    # 无知识库归属时才落回系统默认，且名字是 default 而非 global
    p2 = chunking.resolve_plan(store, s)
    assert p2["source"] == "default"


def test_legacy_inherit_marker_materialized(tmp_path):
    """存量 chunk_size=0（旧的「继承全局」）要迁移成具体值。

    回归防护：迁移若漏掉，旧的继承语义会残留在数据里，
    用户仍要面对「这个值来自哪一级」的推理。
    """
    from rag.core.config import Settings, SplitConfig
    from rag.storage import plan as chunking
    from rag.storage.repos import kbs

    store = _store(tmp_path)
    kb = kbs.create_kb(store, "旧库")
    # 手工退回旧形态
    store.kbs.update(where=f"kb_id = '{kb['kb_id']}'",
                     values={"chunk_size": 0, "overlap_ratio": 0.0})

    s = Settings()
    s.split = SplitConfig(chunk_size=640, chunk_overlap=64)
    moved = chunking.materialize_kb_defaults(store, s)
    assert moved == 1

    after = kbs.get_kb(store, kb["kb_id"])
    assert after["chunk_size"] == 640, "未物化为系统默认值"
    # 幂等
    assert chunking.materialize_kb_defaults(store, s) == 0


def test_kb_plan_read_falls_back_before_migration(tmp_path):
    """迁移没跑也要行为正确——读取时按系统默认兜底。"""
    from rag.core.config import Settings, SplitConfig
    from rag.storage import plan as chunking
    from rag.storage.repos import kbs

    store = _store(tmp_path)
    kb = kbs.create_kb(store, "旧库")
    store.kbs.update(where=f"kb_id = '{kb['kb_id']}'",
                     values={"chunk_size": 0, "overlap_ratio": 0.0})
    s = Settings()
    s.split = SplitConfig(chunk_size=640, chunk_overlap=64)

    p = chunking.kb_plan(store, kb["kb_id"], s)
    assert p["chunk_size"] == 640
    assert p.get("materialized") is True


def test_set_kb_plan_materializes_reset(tmp_path):
    """「恢复默认」应物化为默认值，而不是写回 0。"""
    from rag.core.config import Settings, SplitConfig
    from rag.storage import plan as chunking
    from rag.storage.repos import kbs

    store = _store(tmp_path)
    kb = kbs.create_kb(store, "库", chunk_size=1024, overlap_ratio=20)
    s = Settings()
    s.split = SplitConfig(chunk_size=512, chunk_overlap=64)

    chunking.set_kb_plan(store, kb["kb_id"], chunk_size=0,
                         overlap_ratio=0, settings=s)
    after = kbs.get_kb(store, kb["kb_id"])
    assert after["chunk_size"] == 512, "恢复默认后仍留 0"
    assert after["chunk_size"] != 0


# ---- 前端契约 ----------------------------------------------------------


def test_compose_binds_target_kb():
    """待入库内容必须绑定目标库。

    回归防护：原先提交时才读当前的 curKb——用户选好文件后切换知识库，
    内容会静默进错库，界面上没有任何异常。React 版的入库对话框以 kbId
    为 key：换库即重建，队列与目标一起重置；提交用的是打开时锁定的那个 id。
    """
    src = (ROOT / "web/src/views/KbDocs.tsx").read_text(encoding="utf-8")
    assert 'const target = kbId;' in src, "未记录待入库的目标库"
    assert 'fd.append("kb_id", target)' in src, "提交未使用锁定的目标库"
    assert 'key={kbId}' in src, "切换知识库时入库面板未重置"
    assert '入库到「{kbName(kbId)}」' in src, "界面未显示入库目标"


def test_bare_search_starts_empty():
    """顶栏点「检索」应回到空态输入台，而不是上次的查询与结果。

    回归防护：路由层曾把裸 `#/search` 重写成 sessionStorage 里的上次检索，
    用户点导航看到的是旧查询结果，而不是新的默认页。
    """
    router = (ROOT / "web/src/lib/router.ts").read_text(encoding="utf-8")
    assert "rememberSearch" not in router and "lastSearch" not in router, \
        "路由层仍在恢复上次检索"
    read_route = router[router.index("export function readRoute"):]
    assert 'raw === "/search"' not in read_route, "裸 #/search 仍被重写"
    assert "location.replace(prev)" not in read_route
    search = (ROOT / "web/src/views/Search.tsx").read_text(encoding="utf-8")
    assert "searchUrl" not in search, "检索词仍在写 URL"
    assert 'query.trim() !== ""' in search, "检索视图没有按查询词判断空态"
    assert "newSearch" in search and "raggi:newSearch" in search, "缺少回到空态的入口"


def test_reparse_entry_exists():
    """文档必须能「重新解析（换引擎）」与「重新切分」。

    后端端点一直存在且支持 wait=false；前端提交后必须把 job_id 交给
    轮询者——否则页面永远停在旧状态，用户只能离开再回来。
    """
    ops = (ROOT / "web/src/lib/ops.tsx").read_text(encoding="utf-8")
    assert "reparse?wait=false" in ops, "重新解析未走异步队列"
    assert '"engine"' in ops, "重新解析未提供换引擎的选择"
    assert "resplit?wait=false" in ops, "重新切分未走异步队列"
    for name in ("KbDocs.tsx", "DocView.tsx"):
        view = (ROOT / "web/src/views" / name).read_text(encoding="utf-8")
        assert "reparseDoc" in view and "resplitDoc" in view, f"{name} 缺少入口"

    from inspect import signature

    from rag.api.plan import api_doc_resplit

    params = signature(api_doc_resplit).parameters
    assert "wait" in params, "resplit 端点不接受 wait 参数（前端传的会被丢弃）"
    assert params["wait"].default is True, "resplit 应默认同步等待"


def test_chunk_row_does_not_hijack_enter():
    """行内按钮的 Enter/Space 不能被行的 keydown 吃掉。

    React 版的行可聚焦（↑/↓ 换块），但键盘处理必须先判断事件来源；
    操作区还要 stopPropagation，否则点编辑会顺带把选中项换走。
    """
    view = (ROOT / "web/src/views/DocView.tsx").read_text(encoding="utf-8")
    assert "tabIndex={0}" in view, "行不可聚焦，键盘交互无从谈起"
    assert "e.target !== e.currentTarget" in view, "未判断事件来源"
    assert "e.stopPropagation()" in view, "行内控件会冒泡触发行点击"


def test_no_lancedb_connection_outside_storage():
    """边界规则：只有 `storage/` 能连接数据库、开关表。

    换存储后端（local ↔ RustFS）时只需改 storage 一层——这是重写的
    核心收益，所以要用测试把它钉住。

    刻意**不禁止**导入 LanceDB 的公开辅助类型（RRFReranker /
    CrossEncoderReranker / ColumnOrdering）：那些是检索与排序的
    算法构件，不是存储访问；把它们也赶到 storage 层只会造成
    无谓的间接层。
    """
    banned = re.compile(
        r"lancedb\.connect|\.create_table\(|\.open_table\(|db\.create_|db\.open_")
    offenders = []
    for f in sorted((ROOT / "rag").rglob("*.py")):
        rel = str(f.relative_to(ROOT))
        if rel.startswith("rag/storage/"):
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            if banned.search(code):
                offenders.append(f"{rel}:{i}: {line.strip()[:60]}")
    assert not offenders, (
        "storage/ 之外出现数据库连接/建表调用: " + "; ".join(offenders))


# ---- 补齐的交互与端点 --------------------------------------------------


def test_document_rename_endpoint(client_factory=None):
    """PUT /api/documents/{id} 让标题可改。

    回归防护：此前标题完全不可改——粘贴文本的标题取首行前 60 字，
    写错就永久错了。
    """
    import tempfile

    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    with tempfile.TemporaryDirectory() as d:
        s = Settings()
        s.data_dir = Path(d)
        s.embed.dim = 4
        reg = ModelRegistry(s)

        class _E:
            model, dim = "stub", 4

            def embed(self, ts):
                return [[1.0, 0, 0, 0] for _ in ts]

            def embed_one(self, t):  # noqa: ARG002
                return [1.0, 0, 0, 0]

        reg.bundle.embedder = _E()
        c = TestClient(create_app(Ctx(s, LanceStore.for_data_dir(d, 4), reg)),
                       raise_server_exceptions=False)
        did = c.post("/api/documents/text",
                     json={"text": "内容", "title": "旧"}).json()["doc_id"]

        r = c.put(f"/api/documents/{did}", json={"title": "新标题"})
        assert r.status_code == 200, r.text
        assert c.get(f"/api/documents/{did}").json()["title"] == "新标题"
        assert c.put(f"/api/documents/{did}",
                     json={"title": "   "}).status_code == 400
        assert c.put("/api/documents/nope",
                     json={"title": "x"}).status_code == 404


def test_models_exposes_retrieve_defaults():
    """前端要显示「留空 = 用多少」，因此 /api/models 需要给出检索默认值。"""
    from rag.api.models import router  # noqa: F401  仅确认模块可导入
    src = (ROOT / "rag/api/models.py").read_text(encoding="utf-8")
    assert '"retrieve"' in src, "未暴露检索默认值"


def test_error_states_offer_retry():
    """错误态要能重试，而不是只给一句话。"""
    for f in (ROOT / "web/src/views").glob("*.tsx"):
        body = f.read_text(encoding="utf-8")
        for m in __import__("re").finditer(r'severity="error"', body):
            seg = body[m.start(): m.start() + 700]
            assert "重试" in seg or "重新检测" in seg, \
                f"{f.name} 的错误态没有重试入口"


def test_search_params_layered():
    """常用范围与召回模式留在输入台，其余完整条件收进抽屉。"""
    view = (ROOT / "web/src/views/Search.tsx").read_text(encoding="utf-8")
    composer = (ROOT / "web/src/ui/SearchComposer.tsx").read_text(encoding="utf-8")
    filters = (ROOT / "web/src/ui/SearchFilters.tsx").read_text(encoding="utf-8")
    picker = (ROOT / "web/src/ui/KbMultiSelect.tsx").read_text(encoding="utf-8")
    assert "<KbMultiSelect" in composer, "输入台缺知识库范围"
    assert "全部知识库" in picker and "kbValue" in composer, \
        "输入台的知识库范围没有走共用选择器"
    assert "<RetrievalModeSelect" in composer, "输入台缺召回模式选择器"
    assert "SearchFilters" in view and "<Dialog" in filters, "完整条件未收进浮动面板"
    assert "召回与排序" in filters, "调优参数没有分组"


def test_upload_failures_are_kept():
    """批量上传失败的文件要留在队列里，且不重复成功的那些。"""
    src = (ROOT / "web/src/views/KbDocs.tsx").read_text(encoding="utf-8")
    assert "succeeded" in src, "未区分成功/失败项"
    assert "!succeeded.has(f)" in src, "未只移除成功项"
    assert "失败项已保留" in src, "未告知失败项可重试"


def test_subpages_have_a_way_back():
    """子页面都要有"回到上一级"的入口，且入口是可点的链接。

    阶段 3 起入口是工作区标签条（文档 / 分块 / 设置）。用 `<a href>`
    而不是按钮：中键新开、右键复制链接、状态栏预览都要保留。
    """
    for name in ("KbDocs.tsx", "KbChunks.tsx", "DocView.tsx", "KbSettings.tsx"):
        view = (ROOT / "web/src/views" / name).read_text(encoding="utf-8")
        assert "setCrumbs(" in view, f"{name} 未写入面包屑"

    app = (ROOT / "web/src/App.tsx").read_text(encoding="utf-8")
    assert "WorkspaceTabs" in app, "工作区标签条未由壳层渲染"
    assert 'route.name === "kb" || route.name === "doc"' in app, \
        "标签条未覆盖文档详情页"

    tabs = (ROOT / "web/src/ui/WorkspaceTabs.tsx").read_text(encoding="utf-8")
    assert 'href={href("kb", kbId, t.key)}' in tabs, "标签条未生成可分享的链接"
    assert 'component="a"' in tabs, "标签条用了非链接元素，导航语义会丢"


def test_standalone_chunks_surface():
    """独立分块必须有独立入口，且过滤下推到服务端。"""
    view = (ROOT / "web/src/views/KbChunks.tsx").read_text(encoding="utf-8")
    assert "only_standalone: String(only)" in view, \
        "过滤未下推，文档分块一多独立分块会被截断"
    assert "独立分块" in view
    router = (ROOT / "web/src/lib/router.ts").read_text(encoding="utf-8")
    assert '"standalone"' in router, \
        "旧地址 #/kb/{id}/standalone 必须仍被识别（重定向到分块标签的独立块范围）"
    app = (ROOT / "web/src/App.tsx").read_text(encoding="utf-8")
    assert "KbChunks" in app, "未挂载分块视图"
    assert "?scope=standalone" in view, "分块标签没有切到独立块范围的入口"
    settings = (ROOT / "web/src/views/KbSettings.tsx").read_text(encoding="utf-8")
    assert "?scope=standalone" in settings, "设置页缺少进入独立分块的入口"


def _strip_comments(text: str) -> str:
    """去掉 HTML/块注释与整行 `//` 注释。

    文本型守卫查的是**代码**。本仓库已经因为这个误伤过六次（说明文字里写被禁的
    类名、组件名、写法都会把守卫判红），所以新加的文本断言一律先看代码。
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(ln for ln in text.split("\n") if not ln.lstrip().startswith("//"))


@pytest.fixture()
def chunk_client(tmp_path):
    """带桩 embedder 的 app 客户端（只为这条行为断言服务）。

    刻意不叫 `client`：本文件没有全局 client fixture，
    别的名文件里的同名 fixture 不可见，叫 client 会让人以为它有。
    """
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    class _Stub:
        dim = 4
        model = "stub"

        def embed(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        def embed_one(self, text):
            return [1.0, 0.0, 0.0, 0.0]

    store = LanceStore.for_data_dir(tmp_path, 4)
    settings = Settings()
    settings.data_dir = tmp_path
    settings.embed.dim = 4
    registry = ModelRegistry(settings)
    registry.bundle.embedder = _Stub()
    c = TestClient(create_app(Ctx(settings, store, registry)),
                   raise_server_exceptions=False)
    c.store = store      # type: ignore[attr-defined]
    return c


def test_chunk_content_filter_is_server_side(chunk_client):
    """分块内容过滤必须下推到服务端，界面不许再对结果筛第二遍。

    这条守的不变量：`q` 过滤发生在**分页之前**、`total` 是过滤后的条数。
    漏任何一处，界面上的命中数就变成"已加载窗口里的命中"——而它读起来
    像全库答案。后端部分发真实请求断言可观察结果；前端断言接线。
    """
    for text in ("苹果 香蕉 葡萄 常见水果列表", "龙脑香 dipterocarp 稀有树种"):
        chunk_client.post("/api/documents/text",
                          json={"text": text, "title": text[:6]})
    all_chunks = chunk_client.get("/api/chunks", params={"limit": 100}).json()
    assert all_chunks["total"] >= 2, f"种子数据不足，断言会空转: {all_chunks['total']}"

    hit = chunk_client.get("/api/chunks", params={"q": "dipterocarp",
                                                  "limit": 100}).json()
    assert hit["items"], "服务端过滤把该命中的也滤掉了"
    assert all("dipterocarp" in it["text"] for it in hit["items"]), \
        "返回了不含过滤词的块：q 没真的下推"
    assert hit["total"] == len(hit["items"]) < all_chunks["total"], \
        f"total 不是过滤后的计数（{hit['total']} vs 全库 {all_chunks['total']}）"

    wild = chunk_client.get("/api/chunks", params={"q": "%",
                                                   "limit": 100}).json()
    assert all("%" in it["text"] for it in wild["items"]), \
        "查询词里的 % 被当成通配符了（escape_like 没生效）"

    view = _strip_comments(
        (ROOT / "web/src/views/KbChunks.tsx").read_text(encoding="utf-8"))
    assert 'sp.set("q", query)' in view, "分块页没把过滤词发给服务端"
    assert "items.filter((c) => c.text" not in view, \
        "前端仍在已取回的数组里筛第二遍（两处各筛迟早分叉）"


def test_plan_editing_ui():
    """分块方案要能在界面上改：知识库默认 + 文档级覆盖。"""
    kb_set = (ROOT / "web/src/views/KbSettings.tsx").read_text(encoding="utf-8")
    assert "/plan" in kb_set, "知识库设置页没有方案入口"
    assert 'method: "PUT"' in kb_set, "方案未写回"
    ops = (ROOT / "web/src/lib/ops.tsx").read_text(encoding="utf-8")
    assert "/plan" in ops and 'method: "PUT"' in ops, "文档级方案未写回"
    doc = (ROOT / "web/src/views/DocView.tsx").read_text(encoding="utf-8")
    assert "分块方案" in doc and "editDocPlanDialog" in doc, "文档页没有方案覆盖入口"
    assert "需重切" in doc, "方案变更后未提示需要重新切分"
    docs = (ROOT / "web/src/views/KbDocs.tsx").read_text(encoding="utf-8")
    assert "documents/plans" in docs, "文档列表未批量读取方案（会产生 N+1）"
    assert "需重切" in docs, "列表未标注「需重切」状态"


def test_document_rename_ui():
    """重命名要能在界面上完成，而不是只能删了重传。"""
    ops = (ROOT / "web/src/lib/ops.tsx").read_text(encoding="utf-8")
    assert "/api/documents/" in ops and 'method: "PUT"' in ops, "未调用 PUT /api/documents/{id}"
    assert "重命名文档" in ops, "缺少重命名对话"
    for name in ("KbDocs.tsx", "DocView.tsx"):
        view = (ROOT / "web/src/views" / name).read_text(encoding="utf-8")
        assert "renameDocDialog" in view, f"{name} 缺少重命名入口"


def test_form_dialog_is_mounted():
    """对话框渲染器必须挂载在应用根部，否则 formDialog 永远不显示。

    焦点陷阱与 Escape 交给 MUI Dialog（自带 role="dialog" 与键盘处理），
    不再自己写一份挂在面板上的监听。
    """
    main = (ROOT / "web/src/main.tsx").read_text(encoding="utf-8")
    assert "DialogHost" in main, "入口未挂载 DialogHost"
    form = (ROOT / "web/src/lib/dialog.tsx").read_text(encoding="utf-8")
    assert 'from "@mui/material/Dialog"' in form
    assert "onClose=" in form, "对话框未处理关闭"


def test_health_actions_exposed():
    """健康页要能一键重建索引 / 修复计数，而不是只给一坨 JSON。"""
    settings = (ROOT / "web/src/views/Settings.tsx").read_text(encoding="utf-8")
    assert "/api/reindex" in settings, "缺少重建索引入口"
    assert "/api/reconcile" in settings, "缺少修复计数入口"


def test_streaming_answer_ui():
    """对话要真的流式：SSE 客户端 + 消息流，且卸载时断开。"""
    sse = (ROOT / "web/src/lib/sse.ts").read_text(encoding="utf-8")
    assert "getReader()" in sse, "未按流读取响应体"
    assert "AbortController" in sse, "无法中断请求（停止按钮会变成假停止）"
    for ev in ("sources", "delta", "done", "error"):
        assert f'case "{ev}"' in sse, f"未处理 {ev} 事件"

    search = (ROOT / "web/src/views/Search.tsx").read_text(encoding="utf-8")
    assert "streamPost" in search, "对话未使用流式请求"
    assert "onSources" in search, "未接收引用（sources 事件）"
    assert "onDelta" in search, "未接收增量文本（delta 事件）"
    assert "chatStream.current?.stop()" in search, "停止按钮/卸载未真正断开连接"
    assert "chatSearchCtrl.current?.abort()" in search, "停止时未中止证据检索"
    assert "historyFrom" in search, "多轮对话没有携带历史"

    thread = (ROOT / "web/src/ui/ChatThread.tsx").read_text(encoding="utf-8")
    assert "onCitation" in thread, "引用不可跳回证据"


def test_job_cancel_entry_points():
    """取消能力要有入口，且文案如实说明「运行中不会立即停」。"""
    cancel = (ROOT / "web/src/lib/jobcancel.ts").read_text(encoding="utf-8")
    assert 'method: "DELETE"' in cancel, "未用 DELETE 取消"
    assert "/api/jobs/" in cancel, "取消端点不对"
    assert "r.message" in cancel, "未转述服务端给出的取消说明"

    docs = (ROOT / "web/src/views/KbDocs.tsx").read_text(encoding="utf-8")
    assert "cancelDocJob" in docs, "文档列表缺少取消入口"
    assert "job_id" in docs, "任务映射未保留 job_id（无法取消）"
    assert "停止后" in docs, "未在界面上说明取消的能力边界"


def test_batch_operations_use_batch_endpoints():
    """批量操作必须走批量端点，而不是在客户端循环单条调用。"""
    ops = (ROOT / "web/src/lib/ops.tsx").read_text(encoding="utf-8")
    assert "batch-enabled" in ops, "批量启停未走批量端点"
    assert "for (const c of chunks)" not in ops, "仍在客户端循环调用单条端点"
    assert "/api/documents/batch" in ops, "批量删除未走批量端点"
    assert "skipped" in ops, "未如实回报被跳过的数量"

    docs = (ROOT / "web/src/views/KbDocs.tsx").read_text(encoding="utf-8")
    assert "deleteDocsBatch" in docs, "文档列表未接批量删除"
    assert "pickedCount" in docs, "缺少多选状态的呈现"
