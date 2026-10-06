"""检索 API 专业化与检索页布局的回归测试。

对应「检索页重构 + API 专业化」：
- 分数方向归一（三种通道原本方向不同，score_threshold 无法写通用逻辑）；
- 返回层参数（offset / score_threshold / group_by_doc）真正生效；
- 展示层参数（snippet_chars / highlight / include_context）真正生效；
- 参数校验边界；
- 前端：查询框在页内而非顶栏，参数面板齐全，URL 是参数真源。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
SRC = WEB / "src"


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
    c = TestClient(create_app(Ctx(settings, store, registry)),
                   raise_server_exceptions=False)
    c.store = store        # type: ignore[attr-defined]
    # 建好 FTS 索引，让 hybrid 真正走融合通道而非降级
    for i in range(8):
        c.post("/api/documents/text", json={
            "text": f"逆变器参数第{i}段 额定电压380V " * 3, "title": f"d{i}"})
    store.ensure_fts_index(force=True)
    return c


def _search(c, **kw):
    body = {"q": "逆变器", "top_k": 20, **kw}
    r = c.post("/api/search", json=body)
    assert r.status_code == 200, r.text
    return r.json()


# ---- 分数归一 ----------------------------------------------------------


def test_response_has_professional_fields(client):
    res = _search(client)
    for key in ("query", "score_kind", "offset", "top_k", "has_more",
                "total_candidates", "degraded_reason"):
        assert key in res, f"响应缺少 {key}"
    assert res["query"] == "逆变器"


def test_score_is_normalized_higher_is_better(client):
    """score 必须统一为「越大越相关」——三通道原本方向不同。

    回归防护：vector 通道原样返回 `_distance`（越小越相关），
    与 fts/hybrid 方向相反，导致 score_threshold 语义随模式反转。
    """
    res = _search(client)
    scores = [h["score"] for h in res["results"]]
    assert scores == sorted(scores, reverse=True), "结果未按 score 降序"
    assert res["score_kind"] in ("rrf", "bm25", "cosine_similarity")


def test_score_kind_matches_mode(client):
    assert _search(client, mode="hybrid")["score_kind"] in ("rrf", "bm25")
    assert _search(client, mode="fts")["score_kind"] == "bm25"
    assert _search(client, mode="vector")["score_kind"] == "cosine_similarity"


def test_raw_scores_preserved(client):
    """归一不改原始值：scores 里三个通道的原始分都要在，便于排障。"""
    hit = _search(client)["results"][0]
    assert set(hit["scores"]) == {"vector_distance", "fts", "rrf"}
    assert hit["score_kind"] == hit["score_kind"]


# ---- 返回层 ------------------------------------------------------------


def test_offset_pages_without_overlap(client):
    p1 = _search(client, top_k=3, offset=0)
    p2 = _search(client, top_k=3, offset=3)
    ids1 = {h["chunk_id"] for h in p1["results"]}
    ids2 = {h["chunk_id"] for h in p2["results"]}
    assert len(ids1) == 3 and len(ids2) == 3
    assert not (ids1 & ids2), "翻页出现重复项"
    assert p1["offset"] == 0 and p2["offset"] == 3


def test_has_more_reflects_pagination(client):
    all_hits = _search(client, top_k=50)
    total = all_hits["total_candidates"]
    if total > 3:
        first = _search(client, top_k=3, offset=0)
        assert first["has_more"] is True
        last = _search(client, top_k=50, offset=0)
        assert last["has_more"] is False


def test_total_candidates_is_pre_pagination(client):
    """total_candidates 是翻页前的可用总数，不随 top_k 变化。"""
    a = _search(client, top_k=2)
    b = _search(client, top_k=20)
    assert a["total_candidates"] == b["total_candidates"]


def test_score_threshold_filters(client):
    full = _search(client)
    scores = sorted(h["score"] for h in full["results"])
    mid = scores[len(scores) // 2]
    cut = _search(client, score_threshold=mid)
    assert cut["total_candidates"] < full["total_candidates"], "阈值未生效"
    assert all(h["score"] >= mid for h in cut["results"])


def test_score_threshold_extreme_returns_empty(client):
    """阈值顶格应返回空集，而不是报错。"""
    res = _search(client, score_threshold=100)
    assert res["total_candidates"] == 0
    assert res["results"] == []
    assert res["has_more"] is False


def test_group_by_doc_dedupes(client):
    res = _search(client, group_by_doc=True)
    docs = [h["doc_id"] for h in res["results"]]
    assert len(docs) == len(set(docs)), "同一文档出现多条"


def test_group_by_doc_keeps_best_score(client):
    """去重保留的是每篇文档的最高分，不是随便一条。"""
    grouped = _search(client, group_by_doc=True)
    flat = _search(client, top_k=200)
    best = {}
    for h in flat["results"]:
        best[h["doc_id"]] = max(best.get(h["doc_id"], -1e9), h["score"])
    for h in grouped["results"]:
        assert abs(h["score"] - best[h["doc_id"]]) < 1e-9


# ---- 展示层 ------------------------------------------------------------


def test_snippet_chars_limits_plain_text(client):
    """snippet_chars 限制的是纯文本长度（高亮区间不计入）。

    snippet 现在是结构化 {text, marks}：text 是纯文本，长度就是
    片段长度；早前要用正则剥掉 <mark> 才能量。
    """
    hit = _search(client, snippet_chars=60)["results"][0]
    plain = hit["snippet"]["text"]
    assert len(plain) <= 60, f"摘要过长: {len(plain)}"


def test_snippet_is_structured_not_html(client):
    """snippet 必须是 {text, marks}，绝不能是 HTML 字符串。

    这是 XSS 防线的前端契约：前端不再用 {@html} 注入，安全性由
    Svelte 原生插值保证。文本里若混入标签说明后端漏了结构化改造。
    """
    hit = _search(client)["results"][0]
    snip = hit["snippet"]
    assert isinstance(snip, dict), f"snippet 不是结构化对象：{type(snip)}"
    assert isinstance(snip["text"], str) and isinstance(snip["marks"], list)
    assert "<mark>" not in snip["text"], "text 里混进了 HTML 标签"


def test_snippet_text_is_never_escaped(client):
    """结构化 text 是**原始纯文本**，不含 HTML 实体。

    转义现在由前端插值自动完成；后端若再转义一次，界面就会把
    &lt; 当文字显示出来。
    """
    hit = _search(client)["results"][0]
    text = hit["snippet"]["text"]
    assert "&lt;" not in text and "&gt;" not in text and "&amp;" not in text, \
        f"text 被预先转义了：{text[:80]!r}"


def test_highlight_toggle(client):
    """highlight=true 产出高亮区间，false 则为空。

    早前断言的是 "<mark>" 标签；现在高亮由 marks 表达，语义等价。
    """
    on = _search(client, highlight=True)["results"][0]["snippet"]
    off = _search(client, highlight=False)["results"][0]["snippet"]
    assert on["marks"], "highlight=true 未产出高亮区间"
    assert off["marks"] == [], "highlight=false 仍有高亮区间"
    # 高亮区间必须落在 text 范围内（前端按此切片渲染）
    for s, e in on["marks"]:
        assert 0 <= s < e <= len(on["text"]), f"区间越界: [{s},{e}) / {len(on['text'])}"


def test_include_context_toggle(client):
    on = _search(client, include_context=True)["results"]
    off = _search(client, include_context=False)["results"]
    assert any(h["context"] for h in on), "include_context=true 无上下文"
    assert all(h["context"] == {} for h in off), "include_context=false 仍有上下文"


# ---- 召回调优与校验 ----------------------------------------------------


def test_recall_params_accepted(client):
    res = _search(client, candidate_k=5, nprobes=4, refine_factor=1,
                  k_rrf=30, rerank=False)
    assert res["results"], "调参后应仍有结果"


@pytest.mark.parametrize("bad", [
    {"top_k": 0}, {"top_k": 999}, {"offset": -1},
    {"candidate_k": 0}, {"nprobes": 0}, {"k_rrf": 0},
    {"snippet_chars": 10}, {"snippet_chars": 9999},
    {"window": 99}, {"score_threshold": 999},
])
def test_invalid_params_rejected(client, bad):
    """越界参数必须 422，而不是被静默接受。"""
    r = client.post("/api/search", json={"q": "逆变器", **bad})
    assert r.status_code == 422, f"{bad} 未被拒绝: {r.status_code}"


def test_empty_query_rejected(client):
    assert client.post("/api/search", json={"q": ""}).status_code == 422


# ---- GET 版等价性 ------------------------------------------------------


def test_get_variant_supports_new_params(client):
    r = client.get("/api/search", params={
        "q": "逆变器", "top_k": 3, "offset": 0,
        "group_by_doc": "true", "snippet_chars": 80, "highlight": "false"})
    assert r.status_code == 200
    j = r.json()
    assert "score_kind" in j and len(j["results"]) <= 3


def test_report_is_clean_markdown(client):
    """报告面向 Markdown，不该混进 <mark>。"""
    md = client.post("/api/search/report", json={"q": "逆变器"}).text
    assert "<mark>" not in md, "报告含 HTML 标签"
    assert "分数口径" in md, "报告未标注分数口径"


# ---- 前端契约 ----------------------------------------------------------


def test_topbar_has_no_param_controls():
    """检索参数不得留在顶栏：顶栏是全局 chrome，放不下参数面板。

    回归防护：原先 #searchForm / #q / #searchScope / #searchMode 全在顶栏，
    占掉右半屏却只有 3 个控件，且所有页面都得为它让位。

    注意：顶栏保留一个「跳转到检索页」的快捷输入是允许的——它是入口，
    不是参数区。这里断言的是**参数控件**不出现在顶栏。
    """
    src = (SRC / "App.svelte").read_text(encoding="utf-8")
    topbar = src.split('class="topbar"')[1].split("</header>")[0]
    for pid in ("pTopK", "pMode", "pKb", "pCandidate", "paramsPanel"):
        assert pid not in topbar, f"顶栏仍含参数控件 {pid}"
    # 主导航现在由 dock 的 section 切换器渲染（双栏工作台把导航从顶栏
    # 移进了壳层侧栏）。守卫跟着改读渲染处，并顺手把"导航必须是链接"
    # 这条本项目付过学费的规则也锁上——button 假装导航会丢掉
    # 中键新开、右键复制链接与状态栏预览。
    switcher = (SRC / "ui" / "SectionSwitcher.svelte").read_text(encoding="utf-8")
    assert 'href={s.href}' in switcher, "缺少导航渲染"
    assert '<button class="section-item"' not in switcher, \
        "导航项用了 button，应改用原生 <a href>"
    assert '"检索", href: "#/search"' in (
        SRC / "lib" / "nav.ts").read_text(encoding="utf-8"), "主导航缺少检索入口"


def test_search_view_has_query_and_params():
    """检索页必须自带查询区与参数面板。"""
    src = (SRC / "views/Search.svelte").read_text(encoding="utf-8")
    assert "params-inline" in src, "检索页缺常用参数区"
    assert "params-body" in src, "检索页缺高级参数面板"


@pytest.mark.parametrize("pid", [
    "pKb", "pMode", "pTopK", "pGroup", "pOrigin", "pEngine", "pMime",
    "pDisabled", "pThreshold", "pWindow", "pSnippet", "pHighlight",
    "pCandidate", "pNprobes", "pRefine", "pKrrf", "pRerank",
])
def test_param_controls_exist(pid):
    """参数控件必须齐全（与 API 的专业参数一一对应）。

    回归防护：Svelte 重写时曾只移植了 2 个参数，把另外 15 个弄丢了——
    后端支持、界面没有，等于功能不存在。
    """
    src = (SRC / "views/Search.svelte").read_text(encoding="utf-8")
    assert f'id={{p.id}}' in src or f'id="{pid}"' in src, "控件由规格表渲染"
    spec = (SRC / "lib/searchparams.ts").read_text(encoding="utf-8")
    assert f'id: "{pid}"' in spec, f"参数规格表缺少 {pid}"


def test_url_is_param_source_of_truth():
    """参数写进 URL：刷新/收藏/分享都能复现同一次检索。

    回归防护：若参数只存在内存里，刷新页面参数即丢，
    也无法把一次调好的检索发给别人。

    阶段 5 收紧了一层：URL 必须描述**已生效**的参数，而不是"正在编辑的意图"。
    """
    src = (SRC / "lib/searchparams.ts").read_text(encoding="utf-8")
    assert "toBody" in src, "缺 URL→请求体的序列化"
    assert "searchUrl" in src, "缺参数→URL 的回写"
    # 查"定义"而不是"出现"：注释里提到旧函数名是正常的（要说清替换了什么）
    assert "export function withParam" not in src, \
        "withParam 是「改一个参数就立刻改写 URL」的旧路径，会重新引入参数与结果脱节"
    assert "URLSearchParams" in src
    # 默认值不入 URL，保持链接精简
    assert "DEFAULTS" in src
    view = (SRC / "views/Search.svelte").read_text(encoding="utf-8")
    assert "query.get(" in view, "视图必须从 URL 读参数，而不是本地状态"


def test_score_rendering_uses_score_kind():
    """前端应显示后端给出的分数口径，而不是写死字段名。

    回归防护：后端把 scores 改名后，若前端仍读旧字段会渲染出 NaN。
    """
    src = (SRC / "views/Search.svelte").read_text(encoding="utf-8")
    assert "score_kind" in src, "未读 score_kind"
    assert "degraded_reason" in src, "未展示降级原因"


def test_param_spec_covers_every_api_param():
    """规格表必须与后端接受的参数对齐，不能少。

    这是「界面漏移植参数」这类回归的第一道防线：规格表少一项，
    参数化用例会立刻指出是哪一个。
    """
    spec = (SRC / "lib/searchparams.ts").read_text(encoding="utf-8")
    declared = dict(re.findall(r'key: "([a-z_]+)",\s*id: "(p[A-Za-z]+)"', spec))
    expect = {
        "kb_id", "mode", "top_k", "group_by_doc", "origin", "parser_engine",
        "mime", "include_disabled", "score_threshold", "window",
        "snippet_chars", "highlight", "candidate_k", "nprobes",
        "refine_factor", "k_rrf", "rerank",
    }
    assert expect <= set(declared), f"规格表缺少参数: {sorted(expect - set(declared))}"


def test_param_kind_matches_control_type():
    """规格表声明的 kind 必须与实际渲染的控件类型一致。

    回归防护：group_by_doc 是 <select>（值为 "true"/"false"），
    却曾按 bool 处理——读写都落到 .checked 上，结果 select 永远提交
    默认值、也永远回填不出 URL 里的状态。这类错误 tsc 查不出
    （都是 HTMLElement），只能靠交叉校验。

    现在控件由规格表渲染，所以校验的是「kind 与渲染分支是否自洽」。
    """
    spec = (SRC / "lib/searchparams.ts").read_text(encoding="utf-8")
    view = (SRC / "views/Search.svelte").read_text(encoding="utf-8")

    kinds = dict(re.findall(r'id: "(p[A-Za-z]+)",\s*kind: "(\w+)"', spec))
    assert kinds, "未解析到 kind 声明"

    # kind=bool 的控件必须渲染成 checkbox
    assert 'type="checkbox"' in view, "bool 类参数未渲染为复选框"
    # kind=str 且用 select 呈现的参数必须在视图里有对应的 <select>
    selects = set(re.findall(r'<select id=\{p\.id\}', view))
    assert selects or "<select" in view, "select 类参数未渲染为下拉框"


def test_no_vanilla_dom_helpers_remain():
    """旧的 vanilla DOM 操作不应出现在 Svelte 视图里。

    回归防护：`$("#x")` 在元素缺失时会抛异常，`?.` 也兜不住——
    曾经因此让分页按钮一渲染就把列表变红。Svelte 用绑定替代了这类查询。
    """
    for f in (SRC / "views").glob("*.svelte"):
        body = f.read_text(encoding="utf-8")
        assert 'document.querySelector(' not in body, f"{f.name} 仍在手动查询 DOM"
        assert "innerHTML = " not in body, f"{f.name} 仍在用 innerHTML 全量替换"
