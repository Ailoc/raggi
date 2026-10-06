"""切分：RCT 递归字符切分（保留原文内容，确保偏移闭合）
+ markdown 标题扫描（基于字符偏移归属 heading_path）。

注意：不使用 MarkdownHeaderTextSplitter——其 markdown AST 往返
会改写原文（如把 "\\n\\n" 序列化为 "  \\n"），破坏
char_start/char_end 偏移闭合性（DESIGN §6.2 要求
text == 全文[char_start:char_end]）。
"""
from __future__ import annotations

import re

from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

_HEADERS = [("#", "h1"), ("##", "h2"), ("###", "h3"), ("####", "h4")]
# ATX 标题行：1–4 个 # 后接空格（与 _HEADERS 一致，5+ 级不匹配）
_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*$", re.MULTILINE)


def _headings(text: str) -> list[tuple[int, str, int]]:
    """扫描 markdown 标题行 → [(级别, 标题, 字符偏移)]。"""
    return [
        (len(m.group(1)), m.group(2), m.start())
        for m in _HEADING_RE.finditer(text)
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


def split_text(text: str, chunk_size: int = 512,
               chunk_overlap: int = 64) -> list[Document]:
    """递归字符切分，并为每块附加相对偏移与 heading_path。

    RCT 原样保留内容；配合 span 内游标查找（find from cursor），
    保证 text == 全文[char_start:char_end]（偏移闭合）。
    每块 metadata 附加：
      - char_start: 相对 span 的起始偏移（-1 表示未定位）
      - heading_path: 最近前置标题面包屑
    """
    rcts = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=["\n\n", "\n", "。", "！", "？", "，", "、", " "],
        keep_separator=True,
    )
    docs = rcts.split_documents(
        [Document(page_content=text, metadata={})])
    headings = _headings(text)
    cursor = 0  # 查找游标：重叠分块的起点可能落在前一块内部
    for doc in docs:
        rel = text.find(doc.page_content, cursor)
        if rel >= 0:
            cursor = rel
            doc.metadata["char_start"] = rel
            doc.metadata["heading_path"] = heading_path_for(
                headings, rel)
        else:
            doc.metadata["char_start"] = -1
            doc.metadata["heading_path"] = ""
    return docs
