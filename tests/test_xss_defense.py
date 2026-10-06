"""XSS 防线：检索片段的结构化契约。

**背景**：snippet 早前是后端拼好的 `<mark>` HTML 字符串，前端必须用
`{@html}` 注入。这样安全性完全依赖「后端每次都记得转义」这一条约定——
而入库文本来自 `load_url` 抓取的任意网页正文，攻击者完全能控制内容。
任何一处漏转义（新增调用方、改写 make_snippet、或有人"顺手优化"成
字符串拼接）就是存储型 XSS，且不会有任何测试报警。

现在 snippet 是 `{text, marks}` 结构化数据：
- `text` 是纯文本，前端用 Svelte 原生插值 → **由语言保证转义**；
- `marks` 是高亮区间，由 Snippet 组件自己套 `<mark>`，不需要 HTML 拼接。

这些用例把「新契约」和「不再有 {@html}」都锁死，防止任何人悄悄退回去。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "web" / "src"

XSS_PAYLOAD = '<script>alert(1)</script>'
XSS_ATTR = '"><img src=x onerror=alert(1)>'


# ---- 后端：make_snippet 的结构化契约 ----------------------------------


def test_make_snippet_returns_structure():
    """必须是 {text, marks}，且 marks 为 [start, end) 整数对。"""
    from rag.retrieval.highlight import make_snippet

    s = make_snippet("逆变器参数第1段", "逆变器")
    assert set(s) == {"text", "marks"}, f"字段不对：{set(s)}"
    assert isinstance(s["text"], str)
    assert isinstance(s["marks"], list)
    for m in s["marks"]:
        assert isinstance(m, list) and len(m) == 2
        assert all(isinstance(x, int) for x in m)


def test_make_snippet_does_not_escape():
    """text 是**原始纯文本**，后端不再预转义。

    转义责任已转移到前端插值。若这里再 escape 一次，界面上会把
    `&lt;` 当成字面量显示——这是过度转义，比不转义更容易被误判为 bug。
    """
    from rag.retrieval.highlight import make_snippet

    s = make_snippet(XSS_PAYLOAD, "x")
    assert s["text"] == XSS_PAYLOAD, f"text 被转义或截断了：{s['text']!r}"


def test_make_snippet_never_emits_markup():
    """make_snippet 绝不**自己**插入 <mark> 标签。

    输入里本来就含标签是正常的（用户文档内容不可控），此时它们原样
    留在 text 里；关键是后端没有再往结构里拼标签 —— 高亮只由 marks 表达。
    """
    from rag.retrieval.highlight import make_snippet

    s = make_snippet("a <b> b <mark> c", "b")
    # 标记的高亮必须落在查询词 "b" 上，而不是用户自带的标签上
    for start, end in s["marks"]:
        assert s["text"][start:end].lower() == "b"
    # text 与输入在结构上无差异（没有新增标签）
    assert "<mark>" in s["text"], "输入标签应原样保留为数据"


def test_make_snippet_marks_within_bounds():
    """高亮区间必须落在 text 范围内（前端按此切片）。"""
    from rag.retrieval.highlight import make_snippet

    text = "逆变器参数第1段 额定电压380V"
    s = make_snippet(text, "逆变器 电压")
    for start, end in s["marks"]:
        assert 0 <= start < end <= len(s["text"]), \
            f"区间 [{start},{end}) 越界（text 长 {len(s['text'])}）"


def test_make_snippet_marks_cover_the_term():
    """高亮区间切出来的内容应等于命中词本身。"""
    from rag.retrieval.highlight import make_snippet

    s = make_snippet("逆变器参数第1段", "逆变器")
    assert s["marks"], "未命中"
    for start, end in s["marks"]:
        assert s["text"][start:end] == "逆变器"


def test_make_snippet_marks_are_merged_and_sorted():
    """重叠/乱序区间必须合并排序，否则前端会渲染出 <mark> 嵌套。"""
    from rag.retrieval.highlight import make_snippet

    # "aaaa" 里查 "a" 与 "aa" 会产生相邻/重叠区间
    s = make_snippet("aaaa", "a aa")
    marks = s["marks"]
    assert marks == sorted(marks), f"未排序：{marks}"
    for i in range(1, len(marks)):
        assert marks[i][0] >= marks[i - 1][1], f"区间重叠未合并：{marks}"


def test_make_snippet_highlight_off_gives_no_marks():
    from rag.retrieval.highlight import make_snippet

    s = make_snippet("逆变器参数", "逆变器", highlight=False)
    assert s["marks"] == []
    assert s["text"]


def test_make_snippet_empty_input():
    from rag.retrieval.highlight import make_snippet

    s = make_snippet("", "query")
    assert s == {"text": "", "marks": []}
    s2 = make_snippet(None, "query")  # type: ignore[arg-type]
    assert s2["text"] == ""


def test_snippet_html_is_escaped():
    """保留的 HTML 出口（报告等）必须转义。

    这是唯一还会产出 HTML 的函数，因此它自己必须是安全的：用户内容
    一律经 html.escape，唯一的例外是我们自己插入的 <mark>。
    """
    from rag.retrieval.highlight import snippet_html

    out = snippet_html(XSS_PAYLOAD, "script")
    # 危险标签已被转义（可能夹在 <mark> 里，因此不能断言整串连续）
    assert "<script>" not in out, f"未转义：{out!r}"
    assert "&lt;" in out and "&gt;" in out, f"缺转义实体：{out!r}"


def test_snippet_html_marks_are_escaped_inside():
    """高亮段内部的特殊字符同样要转义（不能只转义普通段）。"""
    from rag.retrieval.highlight import snippet_html

    out = snippet_html("危险<script>x</script>", "危险")
    assert "<script>" not in out
    assert "<mark>" in out


# ---- 端到端：API 返回结构化 snippet -----------------------------------


class _StubEmbedder:
    """不需要真实 embedding 服务的桩（否则检索会 503）。"""
    dim = 4
    model = "stub"

    def embed_one(self, text):
        return [1.0, 0.0, 0.0, 0.0]

    def embed(self, texts):
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]


def _client(tmp_path):
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
    return TestClient(create_app(Ctx(settings, store, registry)),
                      raise_server_exceptions=False)


def _search(client, q, **kw):
    body = {"q": q}
    body.update(kw)
    r = client.post("/api/search", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_search_api_snippet_is_structured(tmp_path):
    """检索接口返回的 snippet 必须是结构化对象。"""
    c = _client(tmp_path)
    c.post("/api/documents/text", json={"text": "普通内容。" * 30, "title": "t"})
    hit = _search(c, "普通内容")["results"][0]
    assert isinstance(hit["snippet"], dict)
    assert "<mark>" not in hit["snippet"]["text"]


def test_xss_payload_survives_as_data_not_markup(tmp_path):
    """入库 XSS payload 后，检索结果里必须仍是**数据**。

    关键在于：payload 会原样留在 text 里（我们不改写用户内容），
    安全性由前端插值保证。所以这里断言的是「接口层没有把用户内容
    拼进 HTML 结构」，而不是「内容被过滤掉」——过滤会破坏正常文档。
    """
    c = _client(tmp_path)
    c.post("/api/documents/text",
           json={"text": XSS_PAYLOAD + " 上下文。" * 20, "title": "xss"})
    snip = _search(c, XSS_PAYLOAD)["results"][0]["snippet"]
    assert isinstance(snip, dict)
    text = snip["text"]
    # 内容原样保留（不篡改数据），但绝不会以标签形式出现在结构里
    assert "<script>" in text, f"内容被改写了：{text[:60]!r}"
    assert "<mark>" not in text
    # 高亮区间由 marks 表达，不靠插标签
    for start, end in snip["marks"]:
        assert 0 <= start < end <= len(text)


# ---- 前端架构守卫：{@html} 不得回归 ------------------------------------


def _svelte_files() -> list[Path]:
    return sorted(SRC.rglob("*.svelte"))


def _strip_comments(text: str) -> str:
    """去掉 HTML / Svelte / CSS 注释。

    必须做：守卫本身与设计说明里都要提到 {@html}（解释「为什么不用」），
    否则文档一提到它就被自己判为违规。
    """
    out: list[str] = []
    i = 0
    while i < len(text):
        nxt = [p for p in (text.find("<!--", i), text.find("/*", i)) if p >= 0]
        if not nxt:
            out.append(text[i:])
            break
        start = min(nxt)
        out.append(text[i:start])
        if text.startswith("<!--", start):
            close, token = "-->", 4
        else:
            close, token = "*/", 2
        end = text.find(close, start + token)
        if end < 0:
            break
        i = end + len(close)
    return "".join(out)


# 真正的 {@html 表达式：以 {@html 开头（允许空格），而不是文档里提到它
_HTML_EXPR = re.compile(r"\{@html\s")


def test_no_raw_html_injection_of_server_data():
    """渲染**服务端数据**的地方不得使用 {@html}。

    ApiDoc 的 renderInline 渲染的是仓内静态清单，且内部先整体转义，
    属于可控例外；除此之外任何 {@html} 都要能指出具体文件。
    """
    offenders: list[str] = []
    for p in _svelte_files():
        text = _strip_comments(p.read_text(encoding="utf-8"))
        for i, line in enumerate(text.splitlines(), 1):
            if not _HTML_EXPR.search(line):
                continue
            # 唯一允许的例外：ApiDoc 的静态清单渲染
            if p.name == "ApiDoc.svelte":
                continue
            offenders.append(f"{p.relative_to(ROOT)}:{i}: {line.strip()}")
    assert not offenders, (
        "服务端数据不得用 {@html} 注入（XSS 面）：\n" + "\n".join(offenders))


def test_snippet_component_uses_no_raw_html():
    """Snippet 组件自身不得含 {@html}。"""
    text = _strip_comments((SRC / "ui" / "Snippet.svelte")
                           .read_text(encoding="utf-8"))
    assert not _HTML_EXPR.search(text), "Snippet 组件仍使用 {@html}"


def test_snippet_component_renders_mark_natively():
    """高亮必须是原生 <mark> 标签，而不是拼 HTML 字符串。"""
    text = _strip_comments((SRC / "ui" / "Snippet.svelte")
                           .read_text(encoding="utf-8"))
    assert "<mark>" in text, "Snippet 组件未渲染 <mark>"


def test_no_component_escapes_snippet_manually():
    """前端不得再手工 escape snippet（后端已不做，重复会双重转义）。"""
    # escapeHtml 之类的手工转义函数若被用在 snippet 上会双重转义
    for rel in ("views/Search.svelte", "ui/AnswerPanel.svelte"):
        text = _strip_comments((SRC / rel).read_text(encoding="utf-8"))
        assert "escapeHtml" not in text, f"{rel} 仍在手工转义 snippet"


def test_apidoc_html_helper_is_self_contained():
    """保留的例外必须自证安全：整体转义先于任何标签插入。"""
    text = _strip_comments((SRC / "views" / "ApiDoc.svelte")
                           .read_text(encoding="utf-8"))
    idx_esc = text.find("function esc(")
    idx_render = text.find("function renderInline(")
    assert idx_esc >= 0 and idx_render > idx_esc, \
        "renderInline 必须定义在 esc 之后"
    body = text[idx_render:idx_render + 400]
    assert body.count("esc(") >= 1, "renderInline 未调用 esc"
    # esc 本身必须覆盖全部四个危险字符
    esc_body = text[idx_esc:idx_esc + 200]
    for ch in ("&", "<", ">", '"'):
        assert ch in esc_body, f"esc 未覆盖 {ch}"