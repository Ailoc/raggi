"""高亮片段：**结构化**输出，不产生 HTML。

DESIGN：早前这里返回拼好的 `<mark>` HTML，前端用 `{@html}` 注入。安全
完全依赖本模块每次都记得转义一次——任何一处漏掉（新增引擎、新增调用
方、或有人顺手改成"直接拼字符串更快"）就是存储型 XSS：入库文本来自 URL
抓取，能拿到任意网页正文。

现在改成返回 `{text, marks}`：
- `text` 是**纯文本**，前端用普通 `{text}` 插值 → Svelte 自动转义，
  安全性由语言本身保证，而不是由每个人记住调用 escape；
- `marks` 是命中区间 `[[start, end], ...]`（相对 `text` 的偏移），
  前端自己渲染 `<mark>`，不需要任何 HTML 拼接。

保留 `make_snippet` 的旧签名（返回结构化 dict），但**不再返回 HTML**；
`snippet_html` 仅供 Markdown 报告等明确需要 HTML 的场景，且必须先 escape。
"""
from __future__ import annotations

import re

from rag.parsing.segment import segment


def _merge(spans: list[tuple[int, int]]) -> list[list[int]]:
    """合并重叠/相邻区间，避免 <mark><mark> 嵌套或重复高亮。"""
    if not spans:
        return []
    spans = sorted(spans)
    out = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def make_snippet(text: str, query: str, window: int = 220,
                 highlight: bool = True) -> dict:
    """截取命中词附近的片段，返回 `{text, marks}`。

    marks 是相对 `text` 的 [start, end) 区间列表（已合并），由前端渲染成
    高亮。`highlight=False` 时 marks 为空 —— 调用方要嵌进 Markdown/终端
    等不认 HTML 的地方时不该带标记。

    注意：这里**不做 HTML 转义**，因为不再输出 HTML。Svelte 的 `{expr}`
    会自动转义，安全性不依赖这里。
    """
    raw = text or ""
    terms = [t for t in segment(query).split() if t.strip()]
    pos = -1
    if terms:
        low = raw.lower()
        for t in terms:
            i = low.find(t.lower())
            if i >= 0:
                pos = i
                break
    if pos < 0:
        snip = raw[:window]
    else:
        start = max(0, pos - 60)
        snip = raw[start:start + window]
    if not highlight or not terms:
        return {"text": snip, "marks": []}
    marks = _merge([(m.start(), m.end()) for m in
                    _iter_terms(snip, terms)])
    return {"text": snip, "marks": marks}


def _iter_terms(snip: str, terms: list[str]):
    """在片段内找出所有命中词的位置（大小写不敏感）。"""
    for t in terms:
        if not t:
            continue
        for m in re.finditer(re.escape(t), snip, flags=re.IGNORECASE):
            yield m


def snippet_html(text: str, query: str, window: int = 220) -> str:
    """生成 HTML 片段（**必须先转义**）。

    仅供 Markdown 报告等明确需要 HTML 的出口使用。前端不要走这里——
    界面渲染一律用结构化的 make_snippet + Svelte 原生插值。
    """
    import html

    snip = make_snippet(text, query, window=window, highlight=False)
    marks = make_snippet(text, query, window=window, highlight=True)["marks"]
    out: list[str] = []
    cursor = 0
    for s, e in marks:
        if s < cursor:      # 合并后不应出现，防御性跳过
            continue
        out.append(html.escape(snip["text"][cursor:s]))
        out.append(f"<mark>{html.escape(snip['text'][s:e])}</mark>")
        cursor = e
    out.append(html.escape(snip["text"][cursor:]))
    return "".join(out)