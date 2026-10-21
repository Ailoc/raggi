"""切分：结构感知的分段 + 段内递归字符切分（保偏移闭合）。

**这是分块策略的单一实现**——四档策略共享同一套段规划与偏移纪律：

- `auto`      ：正文里出现 markdown 标题 → structure；否则 recursive
- `structure` ：标题行分节（节内 RCT）——标题与本节正文同块，
                检索命中正文时主题词就在同一块里
- `recursive` ：纯递归字符切分（分隔符含中英句末）
- `semantic`  ：句子向量断点（需 embed_fn；缺失时降级为 structure）

两条硬约束（回归防护见 tests/test_parsing_layer.py 与 test_chunking_plan.py）：

1. **偏移闭合**：每块都是全文的连续子串（`text == full[start:end]`）。
   因此超长表格按行切而**不复制表头**——复制会造出非连续区间，
   破坏"块即原文切片"这条贯穿存储/高亮的根基。
2. **页边界不是切分边界**：跨页段落不再被硬切（旧实现逐页独立切分，
   页末一句被砍成两半、overlap 也跨不过去）；page 归属改由调用方按
   块起点映射，跨页块归首页。

另外两点是这次重构的动机（用户审查发现的问题）：

- **保护原子段**：markdown 表格与代码围栏整体成块（超长才按行切），
  不再出现"半张表"这种坏块——表格行之间是单 `\\n`，旧实现会在那里切断。
- **尾块合并**：小于 MIN_MERGE_CHARS 的碎块并入相邻块（碎片块对检索
  只有噪声没有信号）。

历史注记：不能用 MarkdownHeaderTextSplitter——其 AST 往返会改写原文
（如 "\\n\\n" 序列化为 "  \\n"），破坏偏移闭合（DESIGN §6.2）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

# 小于它的块并入相邻块：与 chunk_limits.MIN_CHUNK_SIZE 同一把尺子
MIN_MERGE_CHARS = 64

# 段落 → 行 → 句末（中文）→ 句末（英文）→ 子句 → 空格 → 全角空格
# 英文句末此前完全缺失：英文文档只会在空格处断，句子完整度白丢
SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "，", "、",
              ". ", "? ", "! ", "; ", ", ", " ", "　"]

_HEADING_RE = re.compile(r"^#{1,6}\s+.+$", re.MULTILINE)
_FENCE_RE = re.compile(r"^(?:`{3,}|~{3,})[^\n]*\n.*?\n(?:`{3,}|~{3,})[ \t]*$",
                       re.MULTILINE | re.DOTALL)
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
# 句子终止符：中文标点 + 英文 .!?; + 省略号；换行天然断开
_SENT_RE = re.compile(r"[^。！？!?；;….\n]+[。！？!?；;….]*")

# 语义断点的自适应阈值：取相邻句相似度的下四分位（中文句向量普遍高相似，
# 固定阈值在不同语料上必然失准）
_SEM_QUANTILE = 0.25
_SEM_MIN_GAP_RATIO = 0.25   # 断点间距 < size/4 时丢弃（防碎片）


@dataclass(frozen=True)
class Span:
    """一个块：全文区间 + 归属信息（全部相对全文）。"""
    start: int
    end: int
    text: str
    heading_path: str
    offset_valid: bool


def _headings(text: str) -> list[tuple[int, str, int]]:
    """扫描 markdown 标题行 → [(级别, 标题, 字符偏移)]。"""
    return [
        (len(m.group(1)), m.group(2).strip(), m.start())
        for m in re.finditer(r"^(#{1,6})\s+(.+?)\s*$", text, re.MULTILINE)
    ]


def heading_path_for(headings: list[tuple[int, str, int]],
                     char_start: int) -> str:
    """按 chunk 起始偏移取最近前置标题路径（面包屑）。

    规则：高级别标题重置路径，同级标题覆盖前一同级。
    """
    path: list[str] = []
    for level, title, offset in headings:
        if offset > char_start:
            break
        path = path[:level - 1]
        path.append(title)
    return " > ".join(path)


def _line_spans(text: str, start: int = 0,
                end: int | None = None) -> list[tuple[int, int]]:
    """行区间（含行尾换行符），**绝对偏移**。"""
    stop = len(text) if end is None else end
    return [(start + m.start(), start + m.end())
            for m in re.finditer(r"[^\n]*\n?", text[start:stop])]


def _blocks_by_pattern(text: str, row_re: re.Pattern,
                       min_rows: int = 2) -> list[tuple[int, int]]:
    """连续匹配行合并成块（表格用）。"""
    out: list[tuple[int, int]] = []
    lines = [(m.start(), m.end()) for m in re.finditer(r"[^\n]*\n?", text)]
    i = 0
    while i < len(lines):
        s, e = lines[i]
        if row_re.match(text[s:e].rstrip("\n")):
            j = i
            while (j + 1 < len(lines)
                   and row_re.match(text[lines[j + 1][0]:
                                        lines[j + 1][1]].rstrip("\n"))):
                j += 1
            if j - i + 1 >= min_rows:
                out.append((s, lines[j][1]))
                i = j + 1
                continue
        i += 1
    return out


def _atoms(text: str) -> list[tuple[int, int]]:
    """原子保护区：代码围栏 + 表格（可能相邻/重叠，合并成不相交区间）。"""
    raw = [(m.start(), m.end()) for m in _FENCE_RE.finditer(text)]
    raw += _blocks_by_pattern(text, _TABLE_ROW_RE, min_rows=2)
    if not raw:
        return []
    raw.sort()
    merged = [raw[0]]
    for s, e in raw[1:]:
        if s <= merged[-1][1]:      # 相邻或重叠
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def _plan_segments(text: str, strategy: str) -> list[tuple[int, int, str]]:
    """段规划：返回 [(start, end, kind)]，kind ∈ {"prose", "atom"}。

    这是"页边界不再是切分边界"的落点——分段只看语义边界
    （标题行、原子保护区），与解析分页无关。
    """
    atoms = _atoms(text)
    bounds: set[int] = {0, len(text)}
    for s, e in atoms:
        bounds.add(s)
        bounds.add(e)
    if strategy == "structure":
        for _lvl, _t, off in _headings(text):
            bounds.add(off)     # 标题行首是节边界 → 标题归本节
    pts = sorted(b for b in bounds if 0 <= b <= len(text))
    segs: list[tuple[int, int, str]] = []
    for a, b in zip(pts, pts[1:]):
        if b <= a:
            continue
        kind = "atom" if any(s <= a and b <= e for s, e in atoms) else "prose"
        segs.append((a, b, kind))
    return segs


def _rct_spans(text: str, seg_start: int, seg_end: int,
               size: int, overlap: int) -> list[tuple[int, int]]:
    """段内递归切分 → 全文区间（find from cursor 保偏移闭合）。"""
    piece = text[seg_start:seg_end]
    rcts = RecursiveCharacterTextSplitter(
        chunk_size=size, chunk_overlap=overlap,
        separators=SEPARATORS, keep_separator=True,
    )
    docs = rcts.split_documents([Document(page_content=piece)])
    out: list[tuple[int, int]] = []
    cursor = 0
    for doc in docs:
        content = doc.page_content
        if not content:
            continue
        rel = piece.find(content, cursor)
        if rel < 0:
            rel = piece.find(content)     # 重叠块起点回退的兜底
            if rel < 0:
                continue
        cursor = rel
        out.append((seg_start + rel, seg_start + rel + len(content)))
    return out


# 原子段的宽容上限：表格/代码整体成块优先于块长约束——半张表是坏块，
# 略超 size 的完整表是好块（embedding 服务端超限自会截断，两害相权）
_ATOM_SOFT_FACTOR = 2


def _atom_spans(text: str, seg_start: int, seg_end: int,
                size: int) -> list[tuple[int, int]]:
    """原子段：≤ 2×size 整体一块；再长才按**行边界**切（不复制表头——
    复制会造出非连续区间，破坏偏移闭合，见模块头注释）。"""
    if seg_end - seg_start <= size * _ATOM_SOFT_FACTOR:
        return [(seg_start, seg_end)]
    out: list[tuple[int, int]] = []
    cur = seg_start
    acc = 0
    for ls, le in _line_spans(text, seg_start, seg_end):
        if acc and acc + (le - ls) > size:
            out.append((cur, ls))
            cur = ls
            acc = 0
        acc += le - ls
    if cur < seg_end:
        out.append((cur, seg_end))
    return out


def _split_sentences(piece: str) -> list[tuple[int, int]]:
    """句子区间（相对 piece）：中文/英文句末标点收尾。"""
    out: list[tuple[int, int]] = []
    for m in _SENT_RE.finditer(piece):
        s = m.group().strip()
        if not s:
            continue
        lead = len(m.group()) - len(m.group().lstrip())
        trail = len(m.group()) - len(m.group().rstrip())
        out.append((m.start() + lead, m.end() - trail))
    return out


def _cos(a: list[float], b: list[float]) -> float:
    """余弦相似度（纯 Python：几百个句子的小规模，不值得引 numpy）。"""
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    denom = (na ** 0.5) * (nb ** 0.5)
    return dot / denom if denom else 0.0


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    vs = sorted(values)
    idx = max(0, min(len(vs) - 1, int(round(q * (len(vs) - 1)))))
    return vs[idx]


def _semantic_cuts(piece: str, size: int,
                   embed_fn) -> list[int]:
    """句子向量断点（相对 piece 的切点偏移）。句子太少或向量失败时返回 []。"""
    sents = _split_sentences(piece)
    if len(sents) < 4:
        return []                      # 句子太少，语义断点没有意义
    texts = [piece[s:e] for s, e in sents]
    try:
        vecs = embed_fn(texts)
    except Exception:                  # noqa: BLE001
        return []                      # embedding 不可用 → 交由调用方降级
    if not vecs or len(vecs) != len(texts):
        return []
    sims = [_cos(vecs[i - 1], vecs[i]) for i in range(1, len(vecs))]
    threshold = _quantile(sims, _SEM_QUANTILE)
    min_gap = max(MIN_MERGE_CHARS, size // 4)
    cuts: list[int] = []
    last = 0
    for i, sim in enumerate(sims, start=1):
        cut = sents[i][0]
        if sim <= threshold and cut - last >= min_gap:
            cuts.append(cut)
            last = cut
    return cuts


# 缝隙容忍：RCT 会丢掉段尾空白，使相邻段的块之间出现几个字符的空隙
# （实测 "## 标题" 与前一节之间就隔着一个 "\n\n"），不把它当"相邻"
# 就会出现标题独立成块这种碎块。
_GAP_TOL = 8


def _adjacent(a_end: int, b_start: int) -> bool:
    return b_start - a_end <= _GAP_TOL


def _merge_small(spans: list[tuple[int, int, bool]],
                 heading_starts: set[int],
                 threshold: int) -> list[tuple[int, int]]:
    """碎块合并（两轮）。`is_atom=True` 的块不并入他人（保护原子性）。

    回归防护：只做"并入前块"一轮时，**标题行**会成为漏网的小块——
    它与前一节之间隔着被 RCT 丢掉的段尾空白，`out[-1][1] >= s` 为假。
    第二轮"并入后块"把这些块并给它们的后一块。

    另一个易错点：以标题开头的小块在第一轮就"并入前块"会让标题归到
    **上一节**的尾部（实测 "## 参数表" 被并进了前一节）——标题描述的是
    后面的内容，所以标题块在参数里被显式排除，只允许第二轮并发给后块。

    **阈值必须由调用方按 chunk_size 折算**（`min(MIN_MERGE_CHARS, size/2)`），
    不能用固定 64：RCT+overlap 的正常输出就是 60/64 这类"接近但不等于
    size"的块，size=64 时固定阈值会把**每一块**都当碎块链式并回一整块
    （实测 1089 字符的文档被合并成 1 块）。

    合并后的区间是并集——缝隙文本一并纳入，仍是原文连续子串，偏移闭合不受影响。
    """
    out: list[tuple[int, int, bool]] = []
    for s, e, is_atom in spans:
        if (out and not is_atom and not out[-1][2]
                and (e - s) < threshold
                and s not in heading_starts
                and _adjacent(out[-1][1], s)):
            out[-1] = (out[-1][0], max(out[-1][1], e), out[-1][2])
        else:
            out.append((s, e, is_atom))

    res: list[tuple[int, int, bool]] = []
    i = 0
    while i < len(out):
        s, e, is_atom = out[i]
        # 标题块允许并入原子块（"## 参数表" + 它的表格 = 一块，语义正确，
        # 表格内部完整性也不受影响）；普通小块仍不得污染原子块。
        may_join_atom = s in heading_starts
        if (not is_atom and (e - s) < threshold
                and i + 1 < len(out)
                and (may_join_atom or not out[i + 1][2])
                and _adjacent(e, out[i + 1][0])):
            _s2, e2, a2 = out[i + 1]
            res.append((s, max(e, e2), a2))
            i += 2
        else:
            res.append((s, e, is_atom))
            i += 1
    return [(s, e) for s, e, _ in res]


def _effective_strategy(text: str, strategy: str) -> str:
    """归一策略：未知值按 auto；auto 看正文有没有 markdown 标题。"""
    if strategy not in ("auto", "structure", "recursive", "semantic"):
        strategy = "auto"
    if strategy == "auto":
        return "structure" if _headings(text) else "recursive"
    return strategy


def split_document(text: str, chunk_size: int = 512, chunk_overlap: int = 64,
                   strategy: str = "auto",
                   embed_fn=None) -> list[Span]:
    """结构感知切分 → Span 列表（保序、保偏移闭合）。

    embed_fn: 批量向量函数（`list[str] -> list[list[float]]`），
    仅 semantic 策略使用；不可用时自动降级为 structure。
    """
    if not text:
        return []
    size = max(1, int(chunk_size))
    overlap = max(0, min(int(chunk_overlap), size // 2))
    strat = _effective_strategy(text, strategy)
    if strat == "semantic" and embed_fn is None:
        strat = "structure"

    spans: list[tuple[int, int, bool]] = []
    for seg_s, seg_e, kind in _plan_segments(text, strat):
        if kind == "atom":
            for s, e in _atom_spans(text, seg_s, seg_e, size):
                spans.append((s, e, True))
            continue
        cuts: list[int] = []
        if strat == "semantic":
            cuts = _semantic_cuts(text[seg_s:seg_e], size, embed_fn)
        if cuts:
            pieces: list[tuple[int, int]] = []
            prev = 0
            for c in cuts + [seg_e - seg_s]:
                pieces.append((seg_s + prev, seg_s + c))
                prev = c
            for ps, pe in pieces:
                if pe - ps <= size:
                    spans.append((ps, pe, False))
                else:
                    for s, e in _rct_spans(text, ps, pe, size, overlap):
                        spans.append((s, e, False))
        else:
            for s, e in _rct_spans(text, seg_s, seg_e, size, overlap):
                spans.append((s, e, False))

    heads = _headings(text)
    # 合并阈值按 size 折算：固定 64 在 size=64 时会把正常块全判成碎块
    merge_threshold = min(MIN_MERGE_CHARS, max(16, size // 2))
    merged = _merge_small(spans, {off for _l, _t, off in heads},
                          merge_threshold)
    out: list[Span] = []
    for s, e in merged:
        if e <= s:
            continue
        out.append(Span(
            start=s, end=e, text=text[s:e],
            heading_path=heading_path_for(heads, s),
            offset_valid=True,
        ))
    return out


def split_text(text: str, chunk_size: int = 512,
               chunk_overlap: int = 64) -> list[Document]:
    """兼容入口（旧签名）：rc 策略 + 偏移闭合，返回带 metadata 的 Document。

    新代码请用 `split_document()`（策略/原子段/页映射齐全）；这里保留是
    因为 `tests/test_parsing_layer.py` 用它来钉"text == 全文[cs:ce]"这条
    根基性契约——适配器与主实现同源，不存在两份切分逻辑。
    """
    docs: list[Document] = []
    for sp in split_document(text, chunk_size, chunk_overlap,
                             strategy="recursive"):
        docs.append(Document(page_content=sp.text, metadata={
            "char_start": sp.start,
            "heading_path": sp.heading_path,
        }))
    return docs
