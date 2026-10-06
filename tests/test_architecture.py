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
    内容会静默进错库，界面上没有任何异常。
    """
    src = (ROOT / "web/src/ui/UploadPanel.svelte").read_text(encoding="utf-8")
    assert "composeKbId" in src, "未记录待入库的目标库"
    assert 'fd.append("kb_id", target)' in src or 'fd.append("kb_id", composeKbId)' in src, \
        "提交未使用绑定的目标库"
    assert "composeKbId = kbId" in src, "选定文件时未锁定目标库"
    assert 'id="composeTarget"' in src, "界面未显示入库目标"


def test_search_context_is_remembered():
    """顶栏/面包屑点「检索」不该丢掉关键词与参数。

    这条守卫原来钉的是 `location.hash = prev`——于是它把"恢复逻辑写在 go()
    里"当成了契约，而 dock 的「检索」是 `<a href>`，点击不经过 go()：功能对
    真实路径一直是死的（实测地址栏停在裸 #/search、检索框为空），守卫却一直是
    绿的。钉字符串存在只能证明代码写过，不能证明用户走得到——现在改成钉
    "每条 hashchange 都会经过 syncRoute"这个真正的收口点。
    """
    router = (ROOT / "web/src/lib/router.svelte.ts").read_text(encoding="utf-8")
    assert "lastSearch" in router
    assert "rememberSearch" in router
    sync = router[router.index("export function syncRoute"):
                  router.index("export function rememberSearch")]
    assert "lastSearch()" in sync, "裸 #/search 未恢复上次检索（或恢复又搬回 go()）"
    assert 'bare === "/search"' in sync, "恢复分支不在 hashchange 的收口点上"
    search = (ROOT / "web/src/views/Search.svelte").read_text(encoding="utf-8")
    assert "rememberSearch(location.hash)" in search


def test_reparse_entry_exists():
    """文档必须能「重新解析（换引擎）」。

    回归防护：后端 /reparse 一直存在且保留 doc_id/kb_id/created_at，
    但前端一度没有任何调用——解析失败的文档只能删掉重传。
    入口从列表页与详情页共用 lib/docops.ts，走异步队列。
    """
    ops = (ROOT / "web/src/lib/docops.ts").read_text(encoding="utf-8")
    assert "reparse?wait=false" in ops, "重新解析未走异步队列（应立刻返回 job_id）"
    assert '"engine"' in ops, "重新解析未提供换引擎的选择"
    assert "resplit?wait=false" in ops, "重新切分未走异步队列"
    for name in ("KbDocs.svelte", "DocView.svelte"):
        view = (ROOT / "web/src/views" / name).read_text(encoding="utf-8")
        assert "reparseDoc" in view and "resplitDoc" in view, f"{name} 缺少入口"

    # 前端发 wait=false，后端就必须真认这个参数。早前 plan.py 的
    # api_doc_resplit 压根没有 wait 形参：查询参数被静默丢弃，请求一路
    # 同步阻塞到重切完成，且响应里没有前端轮询所需的 job_id——这个断言
    # 只看前端字符串，反而把错误契约锁成了「正确」。
    from inspect import signature

    from rag.api.plan import api_doc_resplit

    params = signature(api_doc_resplit).parameters
    assert "wait" in params, "resplit 端点不接受 wait 参数（前端传的会被丢弃）"
    assert params["wait"].default is True, "resplit 应默认同步等待"


def test_chunk_row_does_not_hijack_enter():
    """行内按钮的 Enter/Space 不能被行的 keydown 吃掉。

    回归防护：tr 没有 tabindex 时，keydown 只可能由行内按钮冒泡触发，
    而 Enter 的默认行为（点击按钮）又被 preventDefault——键盘用户
    按 Enter 打不开编辑/删除，只会选中行。
    """
    view = (ROOT / "web/src/views/DocView.svelte").read_text(encoding="utf-8")
    assert 'tabindex="0"' in view, "行不可聚焦，键盘交互无从谈起"
    # 判断事件来源：来自行内按钮的冒泡必须放行，否则 Enter 的默认
    # 点击行为被吃掉，键盘用户打不开编辑/删除
    assert "e.target !== e.currentTarget" in view, "未判断事件来源"
    # 行内按钮与勾选框都不该顺带选中整行
    assert 'closest("button, input' in view, "行内控件的点击会顺带选中行"


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
    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    import tempfile

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
    """错误态要能重试，而不是只给一句话。

    回归防护：后端重启期间打开列表页，用户只能猜是不是要刷新浏览器。
    """
    for f in (ROOT / "web/src/views").glob("*.svelte"):
        body = f.read_text(encoding="utf-8")
        if 'kind="error"' not in body:
            continue
        seg = body.split('kind="error"')[1][:300]
        assert "action=" in seg, f"{f.name} 的错误态没有重试入口"


def test_search_params_layered():
    """常用的「范围与模式」不该藏在折叠里。

    回归防护：17 个控件全部平铺，第一次使用的人会先被
    nprobes / k_rrf 这类术语劝退。
    """
    view = (ROOT / "web/src/views/Search.svelte").read_text(encoding="utf-8")
    assert "params-inline" in view, "常用参数未提出折叠"
    assert "高级参数" in view, "调优参数未收进「高级参数」"


def test_upload_failures_are_kept():
    """批量上传失败的文件要留在队列里，且不重复成功的那些。"""
    src = (ROOT / "web/src/ui/UploadPanel.svelte").read_text(encoding="utf-8")
    assert "succeeded" in src, "未区分成功/失败项"
    assert "files.filter((f) => !succeeded.has(f))" in src, "未只移除成功项"


def test_subpages_have_a_way_back():
    """子页面都要有"回到上一级"的入口，而且不能只靠浏览器后退。

    阶段 3 起这个入口是**工作区标签条**（文档 / 分块 / 设置），不再是每个
    子页各自摆一个 backlink。理由：四个子页各写一份"返回"，文案与位置
    迟早漂移，而且用户在标签条已经能看到自己在哪的情况下还要多一个
    指向同一处的链接。

    所以守卫改查"标签条确实覆盖了这些视图、且是可点的链接"：
      - 每个子页仍要写面包屑（层级由壳层统一渲染）
      - 承载这些视图的路由必须渲染 WorkspaceTabs
      - 标签条用 `<a href>`：中键新开、右键复制链接、状态栏预览都要在
    """
    for name in ("KbDocs.svelte", "KbChunks.svelte", "DocView.svelte", "KbSettings.svelte"):
        view = (ROOT / "web/src/views" / name).read_text(encoding="utf-8")
        assert "setCrumbs(" in view, f"{name} 未写入面包屑"

    app = (ROOT / "web/src/App.svelte").read_text(encoding="utf-8")
    assert "WorkspaceTabs" in app, "工作区标签条未由壳层渲染"
    # kb 与 doc 两种路由都在标签条之下渲染，四个子页全都覆盖到
    assert 'route.name === "kb" || route.name === "doc"' in app, \
        "标签条未覆盖文档详情页（进去后就没有回来的地方了）"

    tabs = (ROOT / "web/src/ui" / "WorkspaceTabs.svelte").read_text(encoding="utf-8")
    assert "href={href(\"kb\", kbId, t.key)}" in tabs, "标签条未生成可分享的链接"
    assert '<a class="wstab-item"' in tabs, "标签条用了非链接元素，导航语义会丢"


def test_standalone_chunks_surface():
    """独立分块必须有独立入口，且过滤下推到服务端。

    回归防护：这个视图在 Svelte 重写时整体丢失——路由注释还在，
    但没有组件、没有入口，挂在库下的分块在界面上完全不可见。
    """
    view = (ROOT / "web/src/views/KbChunks.svelte").read_text(encoding="utf-8")
    # 两种写法都算下推：字面量拼 URL，或经 URLSearchParams 构造
    # （阶段 2 把查询串改成 URLSearchParams，行为没变，只是字面量拆开了）
    assert "only_standalone=true" in view or 'only_standalone: "true"' in view, \
        "过滤未下推，文档分块一多独立分块会被截断"
    assert "独立分块" in view
    router = (ROOT / "web/src/lib/router.svelte.ts").read_text(encoding="utf-8")
    assert '"standalone"' in router, \
        "旧地址 #/kb/{id}/standalone 必须仍被识别（重定向到分块标签的独立块范围）"
    app = (ROOT / "web/src/App.svelte").read_text(encoding="utf-8")
    assert "KbChunks" in app, "未挂载分块视图"
    # 阶段 3：独立分块不再是单独一页，而是"分块"标签上的一个范围筛选。
    # 守卫跟着改，但查的仍是同一件事：**这个视图必须到得了**，
    # 而且范围必须是服务端参数（否则文档分块一多，独立块就被挤出可见范围）。
    chunks = (ROOT / "web/src/views/KbChunks.svelte").read_text(encoding="utf-8")
    assert "scope=standalone" in chunks, "分块标签没有切到独立块范围的入口"
    assert 'only_standalone: String(only)' in chunks \
        or "only_standalone=true" in chunks, "范围未下推到服务端"
    settings = (ROOT / "web/src/views/KbSettings.svelte").read_text(encoding="utf-8")
    assert "scope=standalone" in settings, "设置页缺少进入独立分块的入口"


def _strip_comments(text: str) -> str:
    """去掉 HTML/块注释与整行 `//` 注释。

    文本型守卫查的是**代码**。本仓库已经因为这个误伤过六次（说明文字里写被禁的
    类名、组件名、写法都会把守卫判红），所以新加的文本断言一律先看代码。
    """
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(ln for ln in text.split("\n") if not ln.lstrip().startswith("//"))


def test_chunk_content_filter_is_server_side():
    """分块内容过滤必须下推到服务端，界面不许再对结果筛第二遍。

    这条同时守三处：后端有参数、前端用它、文档写了它。三处漏任何一处，
    界面上的命中数就变成"已加载窗口里的命中"——而它读起来像全库答案。
    """
    api = (ROOT / "rag/api/chunks.py").read_text(encoding="utf-8")
    assert "q: str = \"\"" in api, "GET /api/chunks 未声明 q 参数"
    # 关键语义：过滤必须在**分页之前**发生在服务端，且 total 是过滤后的条数。
    # 锚点从旧实现的 `rows[offset:offset + limit], len(rows)` 改成现在这个形态：
    # 过滤条件并进 WHERE（`text ILIKE …`）、total 来自 `count_rows(..., where)`。
    # 守的仍然是同一条不变量——换实现可以，把过滤挪回分页之后或挪回前端不行。
    assert "text ILIKE" in api, "q 不再是服务端过滤（被挪回 Python 或前端了）"
    assert "escape_like(needle)" in api, \
        "ILIKE 的模式串没过 escape_like：查询词里的 % / _ 会被当通配符"
    assert "count_rows(ctx.store.chunks, where)" in api, \
        "total 不是过滤后的计数，翻页会出现「匹配 3 块却还有加载更多」"
    assert "rows[offset:offset + limit]" not in api, \
        "又回到了「取回全表再在 Python 里切片」"

    view = _strip_comments((ROOT / "web/src/views/KbChunks.svelte").read_text(encoding="utf-8"))
    assert 'sp.set("q", lastQuery)' in view, "分块页没把过滤词发给服务端"
    assert "items.filter(" not in view, "前端仍在已取回的数组里筛第二遍（两处各筛迟早分叉）"
    # 旧的诚实措辞现在反而是错的：命中数已经是全库口径
    assert "在已加载的" not in view, "文案还停留在客户端过滤时代的口径"
    # 而 debounce 的依赖必须同步读出来，否则这个 effect 根本不会重跑
    i_q = view.find("const q = filter.trim()")
    i_t = view.find("const t = setTimeout(")
    assert i_q != -1 and i_t != -1 and i_q < i_t, "过滤 debounce 的依赖读在回调里，不会重跑"

    doc = (ROOT / "web/src/data/apidoc.ts").read_text(encoding="utf-8")
    # 必须限定在 /chunks 那一条里查：整个文件写 `name: "q"` 的地方有好几处
    # （documents、search 都有 q），全文件匹配等于"在别处找到了它"——变异测试实测漏过一次。
    # 锚点用不带闭合引号的短语：文案末尾多一个句号就会让 index() 抛 ValueError。
    i = doc.find("分块列表（按文档")
    assert i != -1, "/chunks 列表条目的文案被改了——守卫的锚点需要跟着改，别让端点文档失去检查"
    entry = doc[i:doc.find("fields:", i)]
    # 锚点自检：截到的必须是 /chunks 列表那一条本身，而不是滑到了别的端点
    assert "only_standalone" in entry, "定位 /chunks 列表条目失败，后面的断言会失去意义"
    assert 'name: "q", in: "query"' in entry, "接口文档未记录 /chunks 的 q 参数"
    assert "分页前" in entry, "/chunks 的 q 没说明它在分页前过滤（total 口径最容易被误读）"


def test_plan_editing_ui():
    """分块方案要能在界面上改：知识库默认 + 文档级覆盖。

    回归防护：两级方案的后端早已就绪，但 Svelte 界面上没有任何
    方案入口，用户只能改 config.toml。
    """
    kb_set = (ROOT / "web/src/views/KbSettings.svelte").read_text(encoding="utf-8")
    assert "/plan" in kb_set, "知识库设置页没有方案入口"
    assert 'method: "PUT"' in kb_set, "方案未写回"
    ops = (ROOT / "web/src/lib/docops.ts").read_text(encoding="utf-8")
    assert "/plan" in ops and 'method: "PUT"' in ops, "文档级方案未写回"
    doc = (ROOT / "web/src/views/DocView.svelte").read_text(encoding="utf-8")
    assert "分块方案" in doc and "editDocPlan" in doc, "文档页没有方案覆盖入口"
    assert "需重切" in doc, "方案变更后未提示需要重新切分"
    docs = (ROOT / "web/src/views/KbDocs.svelte").read_text(encoding="utf-8")
    assert "documents/plans" in docs, "文档列表未批量读取方案（会产生 N+1）"
    assert "需重切" in docs, "列表未标注「需重切」状态"


def test_document_rename_ui():
    """重命名要能在界面上完成，而不是只能删了重传。"""
    ops = (ROOT / "web/src/lib/docops.ts").read_text(encoding="utf-8")
    assert "/api/documents/" in ops and 'method: "PUT"' in ops, "未调用 PUT /api/documents/{id}"
    assert "重命名文档" in ops, "缺少重命名对话"
    for name in ("KbDocs.svelte", "DocView.svelte"):
        view = (ROOT / "web/src/views" / name).read_text(encoding="utf-8")
        assert "renameDocDialog" in view, f"{name} 缺少重命名入口"


def test_form_dialog_is_mounted():
    """表单对话必须挂载在应用根部，否则 formDialog 永远不显示。"""
    app = (ROOT / "web/src/App.svelte").read_text(encoding="utf-8")
    assert "FormDialog" in app, "App 未挂载 FormDialog"
    form = (ROOT / "web/src/ui/FormDialog.svelte").read_text(encoding="utf-8")
    assert 'role="dialog"' in form
    assert "Escape" in form, "未处理 Escape"


def test_health_actions_exposed():
    """健康页要能一键重建索引 / 修复计数，而不是只给一坨 JSON。"""
    settings = (ROOT / "web/src/views/Settings.svelte").read_text(encoding="utf-8")
    assert "/api/reindex" in settings, "缺少重建索引入口"
    assert "/api/reconcile" in settings, "缺少修复计数入口"


def test_streaming_answer_ui():
    """问答要真的流式：SSE 客户端 + 面板，且引用先于正文。

    回归防护：只有后端 SSE 而前端一次性等结果，等于白做流式；
    反过来只做面板不解析事件流，界面会一直空转。
    """
    sse = (ROOT / "web/src/lib/sse.ts").read_text(encoding="utf-8")
    assert "getReader()" in sse, "未按流读取响应体"
    assert "AbortController" in sse, "无法中断请求（停止按钮会变成假停止）"
    # 四个事件都要处理，漏一个界面就会永远转圈
    for ev in ("sources", "delta", "done", "error"):
        assert f'case "{ev}"' in sse, f"未处理 {ev} 事件"

    panel = (ROOT / "web/src/ui/AnswerPanel.svelte").read_text(encoding="utf-8")
    assert "streamPost" in panel, "面板未使用流式请求"
    assert "onSources" in panel, "未接收引用（sources 事件）"
    assert "onDelta" in panel, "未接收增量文本（delta 事件）"
    assert ".stop()" in panel or "stop()" in panel, "停止按钮未真正断开连接"
    assert "onDestroy" in panel, "组件卸载时未断开，后端会继续生成"

    search = (ROOT / "web/src/views/Search.svelte").read_text(encoding="utf-8")
    assert "AnswerPanel" in search, "检索页未接入问答面板"


def test_job_cancel_entry_points():
    """取消能力要有入口，且文案如实说明「运行中不会立即停」。

    回归防护：只报「已取消」会让用户以为任务停了，实际还在写库。
    """
    cancel = (ROOT / "web/src/lib/jobcancel.ts").read_text(encoding="utf-8")
    assert 'method: "DELETE"' in cancel, "未用 DELETE 取消"
    assert "/api/jobs/" in cancel, "取消端点不对"
    # 文案来自服务端（按 outcome 组织），前端不自己编一套
    assert "r.message" in cancel, "未转述服务端给出的取消说明"

    docs = (ROOT / "web/src/views/KbDocs.svelte").read_text(encoding="utf-8")
    assert "cancelDocJob" in docs, "文档列表缺少取消入口"
    assert "job_id" in docs, "任务映射未保留 job_id（无法取消）"

    upload = (ROOT / "web/src/ui/UploadPanel.svelte").read_text(encoding="utf-8")
    assert "cancelCurrent" in upload, "入库面板缺少停止入口"
    assert "停止后" in upload, "未在界面上说明取消的能力边界"


def test_batch_operations_use_batch_endpoints():
    """批量操作必须走批量端点，而不是在客户端循环单条调用。

    回归防护：循环在 200 条时是 200 个请求且中途失败后无法回答
    「哪些块被停用了」——这正是批量端点要解决的问题。
    """
    ops = (ROOT / "web/src/lib/chunkops.ts").read_text(encoding="utf-8")
    assert "batch-enabled" in ops, "批量启停未走批量端点"
    assert "for (const c of chunks)" not in ops, "仍在客户端循环调用单条端点"

    batch = (ROOT / "web/src/lib/docbatch.ts").read_text(encoding="utf-8")
    assert "/api/documents/batch" in batch, "批量删除未走批量端点"
    assert "skipped" in batch, "未如实回报被跳过的数量"

    docs = (ROOT / "web/src/views/KbDocs.svelte").read_text(encoding="utf-8")
    assert "deleteDocsBatch" in docs, "文档列表未接批量删除"
    assert "pickedCount" in docs, "缺少多选状态的呈现"
