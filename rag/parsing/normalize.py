"""把 LangChain Documents 归一化为带偏移/页码映射的 ParsedDoc。"""
from __future__ import annotations

from dataclasses import dataclass, field

from langchain_core.documents import Document


@dataclass
class SourceSpan:
    content: str
    page: int | None
    start: int
    end: int


@dataclass
class ParsedDoc:
    text: str
    spans: list[SourceSpan] = field(default_factory=list)


def normalize(documents: list[Document]) -> ParsedDoc:
    parts: list[str] = []
    spans: list[SourceSpan] = []
    offset = 0
    for d in documents:
        content = d.page_content or ""
        page = d.metadata.get("page") or d.metadata.get("page_number")
        spans.append(SourceSpan(content, page, offset, offset + len(content)))
        parts.append(content)
        offset += len(content) + 1  # 以换行分隔拼接
    return ParsedDoc(text="\n".join(parts), spans=spans)
