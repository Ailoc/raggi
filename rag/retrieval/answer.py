"""生成式问答：检索 → 编号上下文 → LLM → 带引用的答案（DESIGN §9）。

受 features.answer 开关控制（默认关闭，保持系统"检索"纯粹性）。

**流式与非流式共用同一套准备逻辑**（`prepare`）：检索、取全文、编号引用
都在这里完成，两条路径不会各自算一遍上下文——否则流式版的引用编号可能与
非流式版不一致，而这类偏差极难发现。
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator

from rag.core.config import RetrieveConfig
from rag.models.embeddings import Embedder
from rag.retrieval.search import search
from rag.storage.repos import texts_by_id
from rag.storage.tables import LanceStore

logger = logging.getLogger("raggi.answer")

_PROMPT = """基于以下检索到的上下文回答问题。若上下文不足以回答，请明确说明"上下文不足"，不要编造。
回答时在每条依据后标注引用编号 [n]，n 对应下方上下文编号。

【上下文】
{context}

【问题】
{question}"""

_PROMPT_WITH_HISTORY = """基于以下检索到的上下文回答问题。若上下文不足以回答，请明确说明"上下文不足"，不要编造。
回答时在每条依据后标注引用编号 [n]，n 对应下方【上下文】的编号。
【对话历史】只用于理解指代和追问；其中的引用编号属于过去的轮次，不代表本轮编号。

【对话历史】
{history}

【上下文】
{context}

【问题】
{question}"""


def _history_text(history: list[dict] | list | None) -> str:
    """把最近几轮折算成可读文本。

    历史只用于理解指代和追问；回答的事实依据仍然只能来自本轮
    检索到的上下文，提示词里也明确写了这一点。
    """
    lines: list[str] = []
    for turn in history or []:
        role = turn.get("role") if isinstance(turn, dict) else getattr(turn, "role", None)
        content = turn.get("content") if isinstance(turn, dict) else getattr(turn, "content", None)
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        text = content.strip()
        if not text:
            continue
        lines.append(f"{'用户' if role == 'user' else '助手'}：{text}")
    return "\n\n".join(lines)


async def prepare(
    store: LanceStore,
    embedder: Embedder,
    reranker,
    q: str,
    cfg: RetrieveConfig,
    filters: dict | None = None,
    top_k: int | None = None,
    *,
    history: list | None = None,
    search_opts: dict | None = None,
) -> dict:
    """检索并构造提示词，返回 {prompt, citations, res}。

    抽出来是为了让 answer / answer_stream 共用：流式与非流式的差异只应
    存在于「怎么吐字」，不该存在于「喂给模型什么」。
    """
    opts = dict(search_opts or {})
    res = await asyncio.to_thread(
        search, store, embedder, reranker, q, cfg, filters, top_k, **opts)
    results = res["results"]
    if not results:
        return {
            "prompt": None,
            "citations": [],
            "res": res,
            "empty_text": "（未检索到相关内容）",
        }

    # 一次性取回命中分块全文（避免 snippet 过短影响答案质量）。
    # 必须放线程池：这是同步 LanceDB 读，跑在事件循环里会阻塞整个
    # 服务——而这里正处在 answer_stream 的协程内，阻塞等于服务无响应。
    def _fetch():
        return texts_by_id(store, [r["chunk_id"] for r in results])

    texts: dict[str, str] = {}
    try:
        texts = await asyncio.to_thread(_fetch)
    except Exception as e:  # noqa: BLE001
        logger.warning("取回命中全文失败: %s", e)

    context_parts: list[str] = []
    citations = []
    for i, r in enumerate(results, 1):
        body = texts.get(r["chunk_id"], r.get("snippet", ""))
        context_parts.append(f"[{i}] {body}")
        citations.append({
            "index": i,
            "chunk_id": r["chunk_id"],
            "kb_id": r.get("kb_id", ""),
            "doc_id": r["doc_id"],
            "title": r["title"],
            "heading_path": r["heading_path"],
            "page": r["page"],
            "ordinal": r["ordinal"],
            "score": r["score"],
            "snippet": r["snippet"],
        })

    context = "\n\n".join(context_parts)
    history_text = _history_text(history)
    prompt = (_PROMPT_WITH_HISTORY if history_text else _PROMPT).format(
        history=history_text, context=context, question=q)
    return {
        "prompt": prompt,
        "citations": citations,
        "res": res,
        "empty_text": None,
    }


async def answer(
    store: LanceStore,
    embedder: Embedder,
    reranker,
    chat,
    q: str,
    cfg: RetrieveConfig,
    filters: dict | None = None,
    top_k: int | None = None,
    *,
    history: list | None = None,
    search_opts: dict | None = None,
) -> dict:
    """返回 {answer, citations, took_ms, mode, degraded_reason}。"""
    prepared = await prepare(store, embedder, reranker, q, cfg,
                             filters, top_k, history=history,
                             search_opts=search_opts)
    res = prepared["res"]
    if prepared["empty_text"]:
        return {
            "answer": prepared["empty_text"],
            "citations": [],
            "took_ms": res["took_ms"],
            "mode": res["mode"],
            "degraded_reason": res["degraded_reason"],
        }

    t0 = asyncio.get_running_loop().time()
    resp = await chat.ainvoke(prepared["prompt"])
    took_ms = round((asyncio.get_running_loop().time() - t0) * 1000, 1)
    content = getattr(resp, "content", str(resp))
    return {
        "answer": content,
        "citations": prepared["citations"],
        "took_ms": took_ms,
        "mode": res["mode"],
        "degraded_reason": res["degraded_reason"],
    }


async def answer_stream(
    store: LanceStore,
    embedder: Embedder,
    reranker,
    chat,
    q: str,
    cfg: RetrieveConfig,
    filters: dict | None = None,
    top_k: int | None = None,
    *,
    history: list | None = None,
    search_opts: dict | None = None,
) -> AsyncIterator[dict]:
    """逐段产出答案，事件形状 {event, data}。

    事件序列（客户端按顺序消费）：
      sources  —— 检索命中的引用表；在生成**之前**发出，让界面先显示
                  「依据哪些分块」，而不是空等文字出现；
      delta    —— 答案增量文本，可出现多次；
      done     —— 汇总 {took_ms, mode, degraded_reason}；
      error    —— 出错时的 {message}（客户端据此结束 loading）。

    引用先于正文是关键设计：用户判断要不要继续读，取决于依据是否靠谱，
    而依据在生成之前就已确定。
    """
    prepared = await prepare(store, embedder, reranker, q, cfg,
                             filters, top_k, history=history,
                             search_opts=search_opts)
    res = prepared["res"]
    if prepared["empty_text"]:
        # 契约是 sources → delta* → done，即使没有命中也要先发空的
        # sources：客户端在 delta 里可能已经停止 reading 状态，顺序反过来
        # 会让它把“空引用”当成一次迟到的更新。
        yield {"event": "sources", "data": {"citations": []}}
        yield {"event": "delta", "data": {"text": prepared["empty_text"]}}
        yield {"event": "done", "data": {
            "took_ms": res["took_ms"],
            "mode": res["mode"],
            "degraded_reason": res["degraded_reason"],
        }}
        return

    yield {"event": "sources",
           "data": {"citations": prepared["citations"]}}

    t0 = asyncio.get_running_loop().time()
    got_any = False
    async for piece in chat.astream(prepared["prompt"]):
        text = _delta_text(piece)
        if not text:
            continue
        got_any = True
        yield {"event": "delta", "data": {"text": text}}
    took_ms = round((asyncio.get_running_loop().time() - t0) * 1000, 1)

    # provider 不支持流式时会一次返回整段（此时 astream 仍可用，
    # 只是只产出一个片段）；完全没产出说明出问题了，要说清楚而不是静默结束
    if not got_any:
        yield {"event": "delta",
               "data": {"text": "（模型未返回内容）"}}
    yield {"event": "done", "data": {
        "took_ms": took_ms,
        "mode": res["mode"],
        "degraded_reason": res["degraded_reason"],
    }}


def _delta_text(piece) -> str:
    """从流式片段里取出增量文本。

    各 provider 的片段形状不一：LangChain 的 chunk 有 content 字符串，
    也有 content 是 [{type,text}] 列表的（部分 OpenAI 兼容端点）。
    都归一化成纯文本，缺失就返回空串跳过。
    """
    content = getattr(piece, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(item, str):
                parts.append(item)
        return "".join(parts)
    return ""
