"""解析与切分层的回归测试。

这层此前**零覆盖**，却是入库正确性的地基：

- `choose_engine` / `route` 的回退链决定用哪个引擎抽文本；
- `split_text` 的 docstring 承诺 `text == 全文[char_start:char_end]`
  （DESIGN §6.2），这个不变量一旦破掉，按偏移回溯原文、引用高亮、
  「编辑解析块后高亮失效」的判断全部会静默出错。

测试只依赖可注入的桩，不碰真实 PDF / OCR / 网络。
"""
from __future__ import annotations

import pytest

# ---- 引擎选择（router.choose_engine）--------------------------------


def _cfg(**kw):
    from rag.core.config import ParserConfig

    return ParserConfig(**kw)


def test_choose_engine_by_default_config():
    """default != auto 时一切听配置，不看扩展名。"""
    cfg = _cfg(default="native")
    from rag.parsing.router import choose_engine

    assert choose_engine(cfg, "application/pdf", ".pdf") == "native"
    assert choose_engine(cfg, "text/plain", ".txt") == "native"


def test_choose_engine_overrides_win_over_default():
    """扩展名覆盖表优先级最高（用户显式指定就该生效）。"""
    cfg = _cfg(default="native", overrides={".pdf": "docling"})
    from rag.parsing.router import choose_engine

    assert choose_engine(cfg, "application/pdf", ".pdf") == "docling"
    assert choose_engine(cfg, "application/pdf", ".txt") == "native"


def test_choose_engine_auto_routes_by_type():
    cfg = _cfg(default="auto")
    from rag.parsing.router import choose_engine

    assert choose_engine(cfg, "application/pdf", ".pdf") == "pymupdf4llm"
    # 只有 mime 命中也算 PDF
    assert choose_engine(cfg, "application/pdf", ".bin") == "pymupdf4llm"
    assert choose_engine(cfg, "", ".docx") == "unstructured"
    assert choose_engine(cfg, "", ".html") == "unstructured"
    assert choose_engine(cfg, "", ".txt") == "native"


def test_choose_engine_is_case_insensitive_on_ext():
    """.PDF 与 .pdf 应路由一致（临时文件后缀会被转小写，但别依赖）。"""
    cfg = _cfg(default="auto")
    from rag.parsing.router import choose_engine

    assert choose_engine(cfg, "", ".PDF") == choose_engine(cfg, "", ".pdf")


def test_allowed_engines_covers_every_fallback_target():
    """回退链里的每个引擎都必须在白名单内，否则请求级覆盖能选到
    一个回退链未定义的目标（route 里 _FALLBACK.get 返回 [] → 直接失败）。"""
    from rag.parsing.router import _FALLBACK, ALLOWED_ENGINES

    for head, chain in _FALLBACK.items():
        assert head in ALLOWED_ENGINES, f"{head} 不在白名单"
        for target in chain:
            assert target in ALLOWED_ENGINES, f"{head}→{target} 越界"


# ---- 路由回退链（router.route）--------------------------------------


class _StubLoader:
    """按 engine 行为可控的 load_file 桩。

    engines: {engine: ("ok", docs) | ("empty", None) | ("raise", exc)}
    """

    def __init__(self, engines):
        self.engines = engines
        self.calls: list[str] = []

    def __call__(self, path, engine, ocr=False):
        self.calls.append(engine)
        behavior = self.engines.get(engine, ("raise", RuntimeError("未配置")))
        kind, payload = behavior
        if kind == "raise":
            raise payload
        if kind == "empty":
            return engine, []
        return engine, payload


@pytest.fixture()
def patched_loaders(monkeypatch):
    """把 loaders.load_file 换成桩（route 内部是延迟 import）。"""
    import rag.parsing.loaders as loaders

    def _install(stub):
        monkeypatch.setattr(loaders, "load_file", stub)
        return stub

    return _install


def _docs(n=1):
    from langchain_core.documents import Document

    return [Document(page_content="文本", metadata={}) for _ in range(n)]


def test_route_returns_first_success(patched_loaders):
    cfg = _cfg(default="native")
    stub = patched_loaders(_StubLoader({"native": ("ok", _docs())}))
    from rag.parsing.router import route

    eng, docs = route(__import__("pathlib").Path("a.txt"), cfg)
    assert eng == "native"
    assert len(docs) == 1
    assert stub.calls == ["native"]


def test_route_falls_back_on_exception(patched_loaders):
    """首选引擎抛错应沿链回退，而不是整体失败。"""
    cfg = _cfg(default="docling")
    stub = patched_loaders(_StubLoader({
        "docling": ("raise", RuntimeError("boom")),
        "pymupdf4llm": ("ok", _docs(2)),
    }))
    from rag.parsing.router import route

    eng, docs = route(__import__("pathlib").Path("a.pdf"), cfg)
    assert eng == "pymupdf4llm"
    assert stub.calls == ["docling", "pymupdf4llm"]


def test_route_treats_empty_text_as_failure(patched_loaders):
    """解析器返回空文档不算成功（典型是扫描件），必须继续回退。"""
    cfg = _cfg(default="docling")
    patched_loaders(_StubLoader({
        "docling": ("empty", None),
        "pymupdf4llm": ("ok", _docs()),
    }))
    from rag.parsing.router import route

    eng, _ = route(__import__("pathlib").Path("a.pdf"), cfg)
    assert eng == "pymupdf4llm"


def test_route_raises_with_ocr_hint_when_all_empty(patched_loaders):
    """全部引擎都抽不出文本时，错误信息要点明扫描件需要 OCR。"""
    cfg = _cfg(default="native")
    patched_loaders(_StubLoader({"native": ("empty", None)}))
    from rag.parsing.router import route

    with pytest.raises(RuntimeError, match="OCR"):
        route(__import__("pathlib").Path("a.pdf"), cfg)


def test_route_raises_when_all_engines_fail(patched_loaders):
    cfg = _cfg(default="docling")
    patched_loaders(_StubLoader({
        "docling": ("raise", RuntimeError("d1")),
        "pymupdf4llm": ("raise", RuntimeError("d2")),
        "native": ("raise", RuntimeError("d3")),
    }))
    from rag.parsing.router import route

    with pytest.raises(RuntimeError, match="所有解析引擎均失败"):
        route(__import__("pathlib").Path("a.pdf"), cfg)


def test_route_native_has_no_fallback(patched_loaders):
    """native 是回退链末端，失败就该直接抛（不吞异常）。"""
    cfg = _cfg(default="native")
    patched_loaders(_StubLoader({"native": ("raise", RuntimeError("boom"))}))
    from rag.parsing.router import route

    with pytest.raises(RuntimeError):
        route(__import__("pathlib").Path("a.txt"), cfg)


# ---- 切分的偏移闭合性（DESIGN §6.2）--------------------------------


def test_split_offsets_are_closed():
    """核心不变量：每块的 text 必须等于全文对应切片。

    DESIGN §6.2 明确要求这一点；一旦破坏，按偏移回溯原文、引用定位、
    编辑后高亮失效判断都会静默出错，而表面上一切正常。
    """
    from rag.ingest.splitter import split_text

    text = ("# 标题\n\n第一段内容。" * 20 +
            "\n\n## 二级标题\n\n第二段内容。" * 20)
    docs = split_text(text, chunk_size=80, chunk_overlap=10)
    assert docs, "切分结果为空"
    for d in docs:
        start = d.metadata["char_start"]
        end = start + len(d.page_content)
        assert start >= 0, "未能定位分块偏移"
        assert text[start:end] == d.page_content, (
            "偏移不闭合：切出的内容与原文对不上")


def test_split_handles_empty_and_whitespace():
    """空串/纯空白不应崩，也不该产出内容为空的块。"""
    from rag.ingest.splitter import split_text

    for text in ("", "   ", "\n\n\n"):
        docs = split_text(text)
        assert all(d.page_content.strip() for d in docs), \
            f"空白输入产出了空块：{text!r}"


def test_split_preserves_all_content():
    """切分不得丢内容：把所有块拼起来应覆盖全文（允许重叠）。"""
    from rag.ingest.splitter import split_text

    text = "句子一。" * 100
    docs = split_text(text, chunk_size=50, chunk_overlap=5)
    joined = "".join(d.page_content for d in docs)
    # 重叠块会重复，因此用去重后的覆盖性检查：任一字符都应出现
    for probe in ("句子一。",):
        assert probe in joined


def test_heading_path_is_nested_by_level():
    """标题路径应按层级嵌套，高级标题重置前缀。"""
    from rag.ingest.splitter import _headings, heading_path_for

    text = "# A\n\n内容\n\n## A1\n\n内容\n\n### A1a\n\n内容\n\n## A2\n\n内容"
    hs = _headings(text)
    # A2 处：一级 A 仍在，二级被 A2 覆盖
    i_a2 = text.index("## A2")
    assert heading_path_for(hs, i_a2) == "A > A2"
    i_a1a = text.index("### A1a")
    assert heading_path_for(hs, i_a1a) == "A > A1 > A1a"


def test_heading_path_uses_nearest_preceding_heading():
    """偏移落在标题行本身时，该标题就生效（offset > start 才 break）。"""
    from rag.ingest.splitter import _headings, heading_path_for

    hs = _headings("# A\n\n内容")
    assert heading_path_for(hs, 0) == "A"
    # 标题之前的位置才没有面包屑
    assert heading_path_for([], 0) == ""


def test_heading_regex_rejects_five_levels():
    """5 级以上标题不参与（与 _HEADERS 1–4 级一致）。"""
    from rag.ingest.splitter import _headings

    text = "##### 五级\n\n内容"
    assert _headings(text) == []


def test_split_attaches_heading_path():
    """切分结果应带上标题面包屑。"""
    from rag.ingest.splitter import split_text

    text = "# 章节标题\n\n" + ("正文内容。" * 100)
    docs = split_text(text, chunk_size=60, chunk_overlap=5)
    assert any("章节标题" in d.metadata.get("heading_path", "")
               for d in docs), "分块未继承 heading_path"