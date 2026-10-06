"""中文分词：jieba 预分词，生成 FTS 索引目标列 text_seg。"""
from __future__ import annotations

import logging

import jieba

jieba.setLogLevel(logging.INFO)


def segment(text: str) -> str:
    """空格分词，供 Tantivy FTS 索引（中文按词索引，避免整句成词）。"""
    return " ".join(jieba.cut(text or ""))


def segment_batch(texts: list[str]) -> list[str]:
    """批量分词（入库流水线用，保持与 segment 完全一致的语义）。"""
    return [segment(t) for t in texts]
