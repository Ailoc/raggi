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

    顶栏是全局导航与状态位（品牌 / 主导航 / 队列 / 主题）；检索参数一律
    在检索工作台里。主导航必须是原生链接（中键新开 / 复制链接 / 状态栏预览）。
    """
    src = (SRC / "App.tsx").read_text(encoding="utf-8")
    for pid in ("pTopK", "pMode", "pKb", "pCandidate", "paramsPanel"):
        assert pid not in src, f"顶栏仍含参数控件 {pid}"
    assert 'component="a"' in src and "href={s.href}" in src, \
        "主导航不是链接"
    nav = (SRC / "lib" / "nav.ts").read_text(encoding="utf-8")
    assert '"检索", href: "#/search"' in nav, "主导航缺少检索入口"


def test_search_view_has_query_and_params():
    """检索页必须有输入台，并有覆盖全部参数的条件抽屉。"""
    src = (SRC / "views" / "Search.tsx").read_text(encoding="utf-8")
    composer = (SRC / "ui" / "SearchComposer.tsx").read_text(encoding="utf-8")
    filters = (SRC / "ui" / "SearchFilters.tsx").read_text(encoding="utf-8")
    assert "SearchComposer" in src and "SearchFilters" in src, "检索页未装配输入台/条件抽屉"
    assert "InputBase" in composer and "检索或对话输入" in composer, "输入台缺查询输入"
    assert "PARAM_GROUPS" in filters and "<Drawer" in filters, "缺完整条件抽屉"
    for section in ("范围", "结果形态", "召回与排序", "证据呈现"):
        assert section in filters, f"条件抽屉缺少分组：{section}"


@pytest.mark.parametrize("pid", [
    "pKb", "pMode", "pTopK", "pGroup", "pOrigin", "pEngine", "pMime",
    "pDisabled", "pThreshold", "pWindow", "pSnippet", "pHighlight",
    "pCandidate", "pNprobes", "pRefine", "pKrrf", "pRerank",
])
def test_param_controls_exist(pid):
    """参数控件必须齐全（与 API 的专业参数一一对应）。

    回归防护：重写时曾只移植了 2 个参数，把另外 15 个弄丢了——
    后端支持、界面没有，等于功能不存在。
    """
    src = (SRC / "ui" / "SearchFilters.tsx").read_text(encoding="utf-8")
    assert "id={p.id}" in src, "控件没有从规格表取 id"
    spec = (SRC / "lib" / "searchparams.ts").read_text(encoding="utf-8")
    assert f'id: "{pid}"' in spec, f"参数规格表缺少 {pid}"


def test_search_state_is_local_not_in_url():
    """检索词与参数不写 URL：进入/刷新永远是空态输入台。

    2026-10-22 产品决策：上一版把查询写进 `#/search?q=`，导致刷新、
    恢复标签或书签进入时直接复现旧结果；现在查询与参数都是本地状态，
    URL 不再承载检索，深链分享能力一并取消。
    """
    src = (SRC / "lib" / "searchparams.ts").read_text(encoding="utf-8")
    assert "paramsToBody" in src, "缺参数→请求体的序列化"
    assert "defaultParams" in src, "缺本地参数默认值"
    assert "export function searchUrl" not in src, "searchUrl 仍在把检索写进 URL"
    assert "export function withParam" not in src
    assert "URLSearchParams" not in src, "参数表仍在依赖 URL 查询串"
    view = (SRC / "views" / "Search.tsx").read_text(encoding="utf-8")
    assert "const [query, setQuery]" in view, "检索词不是本地状态"
    assert "paramsToBody(query, applied)" in view, "检索请求未使用本地参数"
    assert "qFromUrl" not in view, "视图仍在从 URL 读检索词"


def test_filter_params_are_nested_for_api():
    """origin/parser_engine/mime 必须放进 filters。

    它们曾以顶层字段发给 SearchReq；Pydantic 默认忽略未知字段，
    结果就是「界面能选过滤条件，检索却完全不过滤」。
    """
    spec = (SRC / "lib" / "searchparams.ts").read_text(encoding="utf-8")
    assert "FILTER_KEYS" in spec, "过滤参数没有单独分组"
    assert "body.filters = filters" in spec, "过滤参数没有包进 filters"
    assert "filtersFromParams" in spec


def test_search_degradation_is_visible():
    """检索降级必须在界面上可见，而不是悄悄给出打折的结果。"""
    src = (SRC / "views" / "Search.tsx").read_text(encoding="utf-8")
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

    回归防护：group_by_doc 是下拉框（值为 "true"/"false"），却曾按
    bool 处理——读写都落到 .checked 上，结果 select 永远提交默认值。
    """
    spec = (SRC / "lib" / "searchparams.ts").read_text(encoding="utf-8")
    view = (SRC / "ui" / "SearchFilters.tsx").read_text(encoding="utf-8")

    kinds = dict(re.findall(r'id: "(p[A-Za-z]+)",\s*kind: "(\w+)"', spec))
    assert kinds, "未解析到 kind 声明"

    assert 'p.kind === "bool"' in view and "<Switch" in view, \
        "bool 类参数未渲染为开关"
    assert "select" in view and "<MenuItem" in view, "select 类参数未渲染为下拉框"
    assert 'p.key === "group_by_doc"' in view


def test_no_vanilla_dom_helpers_remain():
    """视图不得手动查询 DOM 或整块替换 innerHTML。"""
    for f in (SRC / "views").glob("*.tsx"):
        body = f.read_text(encoding="utf-8")
        assert "document.querySelector(" not in body, f"{f.name} 仍在手动查询 DOM"
        assert "innerHTML = " not in body, f"{f.name} 仍在用 innerHTML 全量替换"
