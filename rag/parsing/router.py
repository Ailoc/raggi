"""解析引擎路由与回退链。"""
from __future__ import annotations

import logging
from pathlib import Path

from rag.core.config import ParserConfig

logger = logging.getLogger("raggi.parsing")

# 允许的解析引擎（API 请求级覆盖用）
ALLOWED_ENGINES = {"docling", "pymupdf4llm", "unstructured", "native"}

# 回退优先级（PDF 重排版优先 docling，纯文本优先 pymupdf4llm）
_FALLBACK = {
    "docling": ["pymupdf4llm", "native"],
    "pymupdf4llm": ["native"],
    "unstructured": ["native"],
    "native": [],
}


def choose_engine(cfg: ParserConfig, mime: str, ext: str) -> str:
    # 后缀比较一律小写：上传临时文件名由 _safe_suffix 归一，但
    # reparse_doc / 重切分读的是历史 documents 里的 source_uri，
    # 大写后缀（.PDF）在库里是真实存在的——不归一会静默路由到 native，
    # 于是扫描件被判成「抽出 0 个字符」这类难查的问题。
    ext = (ext or "").lower()
    if ext in cfg.overrides:
        return cfg.overrides[ext]
    if cfg.default != "auto":
        return cfg.default
    if mime == "application/pdf" or ext == ".pdf":
        return "pymupdf4llm"
    if ext in (".docx", ".pptx", ".xlsx", ".html", ".htm", ".eml"):
        return "unstructured"
    return "native"


def route(path: Path, cfg: ParserConfig, mime: str | None = None):
    """返回 (engine, documents)；失败时沿回退链尝试，最终抛错。"""
    ext = path.suffix.lower()
    first = choose_engine(cfg, mime or "", ext)
    chain = [first] + _FALLBACK.get(first, [])
    last_err = None
    empty: list[str] = []
    for engine in chain:
        try:
            from rag.parsing.loaders import load_file

            eng, docs = load_file(path, engine, cfg.ocr)
            if not docs:
                # 解析器没报错但抽不出任何文本（典型是扫描件 PDF）。
                # 记下来继续回退，别把「空」当成成功。
                empty.append(engine)
                continue
            return eng, docs
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("engine %s 失败，尝试回退: %s", engine, e)
    if empty and last_err is None:
        raise RuntimeError(
            f"未能从文件中提取到任何文本（已尝试：{', '.join(empty)}）。"
            "若为扫描件，需要带 OCR 的解析器（如 docling / unstructured）")
    raise RuntimeError(f"所有解析引擎均失败: {last_err}")
