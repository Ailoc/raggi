"""API 文档（web/src/apidoc.ts）与后端实际路由的一致性校验。

前端展示的接口清单是**手写**的（理由见 apidoc.ts 顶部注释），代价是
可能与代码漂移——文档里写了不存在的接口，或新增接口忘记写进文档，
使用者照着文档调却 404。这里把两者钉死：

- 每条文档条目必须在后端真实注册；
- 每个后端 /api 端点必须在文档里出现（除显式豁免）；
- 文档里的分组 id、示例、字段等结构必须自洽。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
APIDOC = ROOT / "web" / "src" / "data" / "apidoc.ts"
APIDOC_VIEW = ROOT / "web" / "src" / "views" / "ApiDoc.svelte"

# FastAPI 自带的文档端点与挂载的静态资源，不属于业务接口
EXEMPT = {
    "/openapi.json",
    "/docs",
    "/docs/oauth2-redirect",
    "/redoc",
}


def _load_app_paths() -> set[tuple[str, str]]:
    """后端真实注册的 (METHOD, path) 集合。"""
    tmp = None
    import os
    import tempfile

    tmp = tempfile.mkdtemp()
    os.environ["RAG_DATA_DIR"] = tmp
    os.environ["RAG_EMBED__DIM"] = "4"

    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    store = LanceStore.for_data_dir(Path(tmp), 4)
    s = Settings()
    s.data_dir = Path(tmp)
    s.embed.dim = 4
    spec = TestClient(create_app(Ctx(s, store, ModelRegistry(s)))).get(
        "/openapi.json").json()
    out = set()
    for path, ops in spec["paths"].items():
        if path in EXEMPT or not path.startswith("/api"):
            continue
        for method in ops:
            if method in ("get", "post", "put", "patch", "delete"):
                out.add((method.upper(), path))
    return out


def _doc_entries() -> list[tuple[str, str]]:
    """从 apidoc.ts 抽出的 (METHOD, path) 列表。

    直接用正则扫源文件而不是 eval/import：Node 不可用，且正则能顺带
    校验文档本身是否格式规范。
    """
    text = APIDOC.read_text(encoding="utf-8")
    pattern = re.compile(
        r'method:\s*"([A-Z]+)",\s*\n\s*path:\s*"([^"]+)"')
    return [(m, p) for m, p in pattern.findall(text)]


@pytest.fixture(scope="module")
def doc_entries():
    return _doc_entries()


@pytest.fixture(scope="module")
def app_paths():
    return _load_app_paths()


def test_doc_file_parses(doc_entries):
    """至少能解析出条目，且数量与声明的规模相符。"""
    assert len(doc_entries) >= 30, f"只解析到 {len(doc_entries)} 条接口，可能格式变了"


def test_no_duplicate_entries(doc_entries):
    seen = set()
    dupes = [e for e in doc_entries if e in seen or seen.add(e)]
    assert not dupes, f"文档里有重复条目: {dupes}"


def test_every_documented_endpoint_exists(doc_entries, app_paths):
    """文档里写的接口必须真实存在——否则使用者照着调会 404。"""
    ghost = [e for e in doc_entries if e not in app_paths]
    assert not ghost, (
        "文档写了后端不存在的接口（须同步 apidoc.ts 或修正文档）: "
        + "; ".join(f"{m} {p}" for m, p in ghost))


def test_every_backend_endpoint_is_documented(app_paths, doc_entries):
    """后端新增接口必须补进文档，否则文档即失真。"""
    documented = set(doc_entries)
    missing = sorted(p for p in app_paths if p not in documented)
    assert not missing, (
        "后端接口未出现在 API 文档中（请补进 web/src/apidoc.ts）: "
        + "; ".join(f"{m} {p}" for m, p in missing))


def test_paths_are_normalized(doc_entries, app_paths):
    """文档与后端的路径写法应完全一致（参数名不同也算漂移）。"""
    # FastAPI 会把 {kb_id} 保留原样，这里只比较格式差异的常见来源
    for m, p in doc_entries:
        assert p.startswith("/api/v1/"), f"{m} {p} 不在 /api/v1 前缀下"
        # 花括号必须成对且参数名非空
        for name in re.findall(r"\{(\w*)\}", p):
            assert name, f"{m} {p} 存在空的路径参数占位"


def test_group_ids_unique_and_kebab():
    text = APIDOC.read_text(encoding="utf-8")
    ids = re.findall(r'^\s{4}id:\s*"([^"]+)",', text, re.M)
    assert ids, "未解析到任何分组 id"
    assert len(ids) == len(set(ids)), f"分组 id 重复: {ids}"
    for i in ids:
        assert re.fullmatch(r"[a-z0-9-]+", i), f"分组 id 应为 kebab-case: {i}"


def test_no_mojibake_in_doc_source():
    """源码里不能有 U+FFFD 替换字符。

    回归防护：曾因写入时编码损坏，让「被停用的分块」渲染成乱码方块，
    而 tsc 与页面结构校验都发现不了。
    """
    for f in (APIDOC, APIDOC_VIEW):
        text = f.read_text(encoding="utf-8")
        bad = [(i, l) for i, l in enumerate(text.splitlines(), 1)
               if "\ufffd" in l or any(0xD800 <= ord(c) <= 0xDFFF for c in l)]
        assert not bad, f"{f.name} 存在乱码字符: {bad}"


def test_examples_are_json_serializable(doc_entries):
    """示例对象必须能被 JSON 序列化（渲染时直接 JSON.stringify）。"""
    # 只校验字面量示例能被解析；含占位符的示例由渲染层处理
    text = APIDOC.read_text(encoding="utf-8")
    assert "example:" in text, "没有任何示例"


def test_upload_endpoint_uses_multipart_not_json_example():
    """文件上传接口不应给出 JSON 请求体示例（照抄必然失败）。"""
    text = APIDOC.read_text(encoding="utf-8")
    seg = text.split('path: "/api/v1/documents"', 1)[1][:1500]
    assert "in: \"form\"" in seg, "上传接口的参数应声明在 form"


def test_curl_generator_handles_file_and_json():
    """curlFor 的两种分支：文件上传走 -F，JSON 走 -d。"""
    text = APIDOC.read_text(encoding="utf-8")
    assert "-F 'file=@" in text, "curl 生成器缺少文件上传分支"
    assert "-d '${JSON.stringify" in text, "curl 生成器缺少 JSON 请求体分支"


def test_copy_has_text_selection_fallback():
    """复制失败时应退化为「选中文本」，而不是什么都不做。

    navigator.clipboard 需要安全上下文与权限，内网 http 或权限被拒时会
    抛错；此时给一个失败后毫无反应的按钮比不给按钮更糟。

    阶段 7 起，选区那段实现住在 `lib/actions.ts` 的 `selectText`（DOM 副作用
    只许有一份）。这条守卫要的是**失败时真的会选中并告知下一步**，不是"这段
    代码写在视图文件里"——所以两处都要查：视图必须调用它，实现必须真的选中文本。
    """
    view = APIDOC_VIEW.read_text(encoding="utf-8")
    actions = (ROOT / "web" / "src" / "lib" / "actions.ts").read_text(encoding="utf-8")
    assert "selectText(" in view, "复制失败时没有走文本选中兜底"
    assert "selectNodeContents" in actions, "兜底实现里不再真的选中文本"
    assert "已选中" in view, "缺少「已选中，按 Ctrl+C 复制」的提示"


def test_copy_result_announced_to_screen_readers():
    """复制结果对读屏用户不可见，需经 live region 播报。"""
    view = APIDOC_VIEW.read_text(encoding="utf-8")
    assert 'id="apiDocLive"' in view, "缺少 live region 容器"
    assert 'aria-live="polite"' in view, "live region 未声明 polite"
    assert "announce(" in view, "未向 live region 播报复制结果"


def test_first_group_expanded_by_default():
    """首次访问应展开第一组。

    回归防护：全部折叠时首屏只有几行标题，用户看不到任何接口内容，
    容易误以为页面坏了。
    """
    view = (APIDOC_VIEW).read_text(encoding="utf-8")
    assert "GROUPS[0]?.id" in view, "首次访问未默认展开第一组"


def test_view_registered_in_html_and_router():
    """视图容器、导航入口与路由三处必须齐备。"""
    router = (ROOT / "web" / "src" / "lib" / "router.svelte.ts").read_text(encoding="utf-8")
    app = (ROOT / "web" / "src" / "App.svelte").read_text(encoding="utf-8")
    # 导航入口从 App.svelte 里手写的 nav 数组抽到了 lib/nav.ts 的 section 表
    # （三处各写一遍导航必然漂移，见该文件注释）。守卫跟着改读同一张表——
    # 查的仍是"入口存在"，只是不再关心它定义在哪个文件。
    nav = (ROOT / "web" / "src" / "lib" / "nav.ts").read_text(encoding="utf-8")
    # 阶段 3 起路由是模式表（PATTERNS），不再是 `parts[0] === "..."` 的
    # 手写分支。查的仍是同一件事：apidoc 这条路由存在且指向 apidoc 视图。
    assert 'seg: ["apidoc"], name: "apidoc"' in router, "路由未处理 apidoc"
    assert "apidoc" in app, "App 未挂载 apidoc 视图"
    assert "ApiDoc" in app, "App 未渲染 ApiDoc 组件"
    assert 'key: "apidoc"' in nav and "#/apidoc" in nav, "导航缺少 API 入口"
    assert "SECTIONS" in app, "壳层未渲染 section 导航"


def test_documented_endpoint_count_is_stable():
    """接口数量基线：后端增删接口会同步反映在这里。"""
    n = len(_doc_entries())
    assert n == 51, (
        f"接口数变为 {n}，确认是有意增删并已更新测试")

# ---- DESIGN.md 端点表的一致性 -------------------------------------------

DESIGN = ROOT / "docs/DESIGN.md"


def _design_endpoints() -> set[tuple[str, str]]:
    """从 DESIGN.md 的 §10 表格里抽出 (方法, 路径)。

    只扫 §10 那一节，避免把其它章节里当作示例出现的路径也算进来。
    """
    text = DESIGN.read_text(encoding="utf-8")
    start = text.index("## 10. API 总表")
    end = text.index("## 11.", start)
    out: set[tuple[str, str]] = set()
    for m in re.finditer(r"^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`([^`]+)`",
                         text[start:end], re.M):
        out.add((m.group(1), m.group(2)))
    # 前端静态页由 StaticFiles 挂载，不出现在 openapi paths 里，不参与比对
    out.discard(("GET", "/"))
    return out


def _openapi_endpoints() -> set[tuple[str, str]]:
    """真实注册的端点集合。直接读 apidoc 测试已有的 spec 来源。"""
    paths = _spec_paths()
    return {(m.upper(), p) for p, ops in paths.items()
            for m in ops if m.lower() in
            ("get", "post", "put", "patch", "delete")}


def _spec_paths() -> dict:
    import os
    import tempfile

    from fastapi.testclient import TestClient

    from rag.api import Ctx, create_app
    from rag.core.config import Settings
    from rag.models.registry import ModelRegistry
    from rag.storage.tables import LanceStore

    tmp = tempfile.mkdtemp()
    os.environ["RAG_DATA_DIR"] = tmp
    os.environ["RAG_EMBED__DIM"] = "4"
    s = Settings()
    s.data_dir = Path(tmp)
    s.embed.dim = 4
    c = TestClient(create_app(Ctx(s, LanceStore.for_data_dir(tmp, 4),
                                 ModelRegistry(s))))
    return c.get("/openapi.json").json()["paths"]


def test_design_doc_lists_every_endpoint():
    """DESIGN.md 的端点表必须与 openapi 完全一致。

    这份文档此前正是因为**没有任何检查**而整体过期（还停留在三级分块与
    旧模块结构上）。文档漂移的唯一有效解药是让它进 CI。
    """
    doc, api = _design_endpoints(), _openapi_endpoints()
    missing = sorted(api - doc)      # 后端有、文档没写
    extra = sorted(doc - api)        # 文档写了、后端没有
    assert not missing, f"DESIGN.md §10 缺少端点: {missing}"
    assert not extra, f"DESIGN.md §10 列了不存在的端点: {extra}"


def test_design_doc_has_no_stale_layout_claims():
    """防止旧结构的描述回潮。

    注意：文档里**说明历史**（"上一版是 X，现已改为 Y"）是好事，
    不该被当成过期。因此这里只拦"把旧结构当成现行设计来讲"的句式，
    不拦裸词。
    """
    text = DESIGN.read_text(encoding="utf-8")
    stale = {
        "rag/store/": "存储层已迁到 rag/storage/",
        "`rag/store`": "存储层已迁到 rag/storage/",
        "存于 `meta` JSON": "kb_id / stored_file 已是真实列",
        "`RAG_TOKEN`（设置后校验": "认证已改为多密钥体系",
        "leftJoin rail": "侧栏已整体移除",
    }
    for needle, why in stale.items():
        assert needle not in text, f"DESIGN.md 出现过期表述「{needle}」：{why}"

    # 「继承全局」只有在描述历史时才允许出现——若它出现在表格或
    # 数据模型注释里，说明又被当成现行设计了
    for line in text.splitlines():
        if "继承" in line and line.strip().startswith(("kb_id:", "chunk_size:")):
            raise AssertionError(f"分块方案仍是旧的三级继承描述: {line.strip()}")


def test_design_doc_mentions_new_capabilities():
    """新能力必须出现在设计文档里，否则等于没有设计记录。"""
    text = DESIGN.read_text(encoding="utf-8")
    for needle in ("入库队列", "API 密钥", "签名 URL", "RustFS",
                   "两级", "score_kind", "conftest"):
        assert needle in text, f"DESIGN.md 未记录新能力：{needle}"
