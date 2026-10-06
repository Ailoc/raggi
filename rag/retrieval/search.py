"""检索内核：LanceDB 原生 hybrid / vector / fts + RRF/rerank + 过滤 + 上下文窗口。

充分利用 LanceDB：
- query_type="hybrid" 一次完成向量 + FTS 召回
- .rerank() 内置 RRFReranker / CrossEncoderReranker
- .where(prefilter=True) 过滤下推 + 标量索引
- .nprobes / .refine_factor 召回调优

**相关性分数归一**（score_kind）：
LanceDB 三种通道返回的字段与方向都不同——
  vector → `_distance`（余弦距离，越小越相关）
  fts    → `_score`（越大越相关）
  hybrid → `_relevance_score`（RRF 融合，越大越相关）
对外统一成「越大越相关」的 `score`，原始值仍保留在 `scores` 里，
否则 score_threshold 在不同模式下含义相反，调用方无法写通用逻辑。

性能：全程 to_list()/to_arrow()，避免 pandas 构造开销。
"""
from __future__ import annotations

import logging
import time

from rag.core.config import RetrieveConfig
from rag.models.embeddings import Embedder
from rag.parsing.segment import segment
from rag.retrieval.highlight import make_snippet
from rag.storage.repos import chunk_prefilter, contexts_for, docs_query, search_chunks
from rag.storage.tables import LanceStore

logger = logging.getLogger("raggi.retrieve")

# 摘要默认长度（字符）
DEFAULT_SNIPPET_CHARS = 220



def _score_of(row: dict) -> tuple[float, str]:
    """把各通道的原始分数折算成「越大越相关」，并给出其量纲名。

    返回 (归一化分数, score_kind)。score_kind 让调用方知道能不能跨模式比较。

    精排分优先：一旦过了精排，排序依据就是它，标成 rrf/bm25 会说谎。
    """
    if row.get("_rerank_score") is not None:
        return float(row["_rerank_score"]), "rerank"
    if row.get("_relevance_score") is not None:
        return float(row["_relevance_score"]), "rrf"
    if row.get("_score") is not None:
        return float(row["_score"]), "bm25"
    if row.get("_distance") is not None:
        # 余弦距离 ∈ [0, 2]，0 表示完全一致 → 折算到 [-1, 1]，越大越相关
        return 1.0 - float(row["_distance"]) / 2.0, "cosine_similarity"
    return 0.0, "none"


def _doc_meta(store: LanceStore, doc_ids: list[str]) -> dict[str, dict]:
    """批量取命中所属文档的元信息：**一次**窄列原生投影。

    这里带上 `parser_engine` 是在补一个真实缺陷：`SearchHit.parser_engine`
    一直声明要返回「该命中由哪个解析引擎产出」，而它读的是 chunks 行上的
    `parser_engine` —— **chunks 表没有这一列**（它在 documents 上），
    所以 `r.get("parser_engine", "")` 恒为空串，字段一直是个哑字段。
    现在从 documents 侧一次带出，成本不变（同一次查询多一列）。
    """
    if not doc_ids:
        return {}
    try:
        rows = docs_query(store, ["doc_id", "title", "parser_engine"],
                          ids=doc_ids)
        return {str(r["doc_id"]): r for r in rows}
    except Exception:  # noqa: BLE001
        return {}


def _doc_ranges(rows, window: int) -> dict[str, tuple[int, int]]:
    """把命中行按文档合并成 ordinal 闭区间。

    合并是必须的：同一篇文档命中 5 块时，5 个窗口区间并成一段，
    上下文回捞就仍是一条查询；不合并就会按命中条数放大过滤条件。
    """
    merged: dict[str, list[int]] = {}
    for r in rows:
        did = str(r.get("doc_id") or "")
        if not did:
            continue
        lo, hi = int(r["ordinal"]) - window, int(r["ordinal"]) + window
        if did in merged:
            merged[did][0] = min(merged[did][0], lo)
            merged[did][1] = max(merged[did][1], hi)
        else:
            merged[did] = [lo, hi]
    return {d: (lo, hi) for d, (lo, hi) in merged.items()}


def _contexts(store: LanceStore, rows, window: int) -> dict:
    """按文档取回上下文窗口——**一次查询拿完所有文档**。

    改前这里写着「避免 N+1」，实际却是「每篇命中文档一次查询」：
    它只把「每分块一次」收敛成「每文档一次」。一页 8 条命中跨 8 篇文档
    就是 8 × ~5ms，占掉一次 hybrid 检索总耗时的三分之一
    （实测：关掉 include_context 后 75.8ms → 50.9ms）。
    真正的修复是把区间合并成一条 `doc_id IN (…) AND (区间 OR 区间)`。
    """
    try:
        return contexts_for(store, _doc_ranges(rows, window))
    except Exception:  # noqa: BLE001
        return {}


def _is_app_layer_reranker(reranker) -> bool:
    """判断精排器该在应用层跑，还是交给 LanceDB 内部。

    LanceDB 自带的 reranker（RRFReranker / CrossEncoderReranker）应由
    `.rerank()` 内部调用；而我们自己的 HTTP 精排器不是它的子类，
    塞进去会因协议不匹配而失败——它只在应用层用。
    """
    if reranker is None:
        return False
    try:
        from lancedb.rerankers import Reranker
    except ImportError:  # pragma: no cover
        return True
    return not isinstance(reranker, Reranker)


def _apply_rerank(reranker, q: str, rows: list[dict]) -> tuple[list[dict], str | None]:
    """在应用层做精排，失败时降级并返回原因。

    精排是**可选增强**：服务不可用时不该让整次检索失败——
    召回结果本身仍然可用，标注降级原因即可。
    """
    try:
        return _crossencoder_rerank(reranker, q, rows), None
    except Exception as e:  # noqa: BLE001
        logger.warning("精排失败，已跳过: %s", e)
        return rows, f"精排不可用，已按召回分数排序: {_rerank_short(e)}"


def _rerank_short(exc: BaseException, limit: int = 160) -> str:
    msg = str(exc).strip() or exc.__class__.__name__
    return msg.splitlines()[0][:limit]


def _crossencoder_rerank(reranker, q: str, rows: list[dict]) -> list[dict]:
    """应用层精排：把候选文本交给精排器重新打分排序。

    入参用 DataFrame 是因为要同时满足 LanceDB 原生 CrossEncoderReranker
    与我们的 HttpReranker——两者都按这个约定实现。
    """
    import pandas as pd

    df = pd.DataFrame({
        "text": [r.get("text", "") for r in rows],
        "id": list(range(len(rows))),
    })
    out = reranker.rerank(q, df)
    ranked_ids = out["id"].tolist()
    scores = out["_relevance_score"].tolist() if "_relevance_score" in out else [
        0.0] * len(ranked_ids)
    by_id = {i: r for i, r in enumerate(rows)}
    result = []
    for rid, sc in zip(ranked_ids, scores):
        r = dict(by_id[int(rid)])
        r["_rerank_score"] = float(sc)
        result.append(r)
    return result


def search(
    store: LanceStore,
    embedder: Embedder,
    reranker,
    q: str,
    cfg: RetrieveConfig,
    filters: dict | None = None,
    top_k: int | None = None,
    *,
    offset: int = 0,
    score_threshold: float | None = None,
    group_by_doc: bool = False,
    include_context: bool = True,
    snippet_chars: int | None = None,
    highlight: bool = True,
) -> dict:
    """执行检索。

    除 cfg 与 filters 外的参数对应 API 的返回层 / 展示层开关；
    召回层（candidate_k / nprobes / refine_factor / k_rrf）通过 cfg 传入。
    """
    top_k = top_k or cfg.top_k
    filters = dict(filters or {})
    # where 由仓储层生成（SQL 文本只许出现在 storage 层）。改前这里还有一行
    # `filters["_store"] = store`——把 store 塞进「用户可传的过滤条件」字典里
    # 一路带到 SQL 拼装处，那是一条隐式通道：filters 同时扮演两种角色，
    # 任何人把它原样回显或转发就会带出内部状态。
    where = chunk_prefilter(store, filters)
    q_seg = segment(q) if cfg.use_jieba else q
    q_vec = embedder.embed_one(q)

    t0 = time.perf_counter()
    degraded = None
    # 应用层精排器（HTTP / 本地 CrossEncoder）在所有通道下统一在召回之后应用；
    # LanceDB 原生 reranker 则交给它内部的融合流程。
    app_rerank = _is_app_layer_reranker(reranker)
    # 交给 hybrid 通道的融合器：应用层精排时仍要 RRF，保证两路召回被正确归一，
    # 所以这里传 None 而不是把精排器塞进引擎。
    native_reranker = None if app_rerank else reranker
    try:
        rows = search_chunks(
            store, mode=cfg.mode, vector=q_vec, text=q_seg, where=where,
            limit=cfg.candidate_k, nprobes=cfg.nprobes,
            refine_factor=cfg.refine_factor, rrf_k=cfg.k_rrf,
            native_reranker=native_reranker)
    except Exception as e:  # noqa: BLE001
        # 索引缺失等原因时降级为纯向量召回
        logger.warning("%s 检索失败，降级 vector: %s", cfg.mode, e)
        # 回给客户端的只有「降级了 + 原文在日志里」这句事实。
        # 异常原文常含文件路径、Arrow 表名、SQL 片段——api/__init__.py 的
        # 兜底 handler 刻意不回传这类信息，降级路径不能自己破例。
        degraded = f"{cfg.mode} 不可用，已降级 vector（详见服务端日志）"
        rows = search_chunks(
            store, mode="vector", vector=q_vec, where=where,
            limit=cfg.candidate_k, nprobes=cfg.nprobes,
            refine_factor=cfg.refine_factor)

    # 精排失败时降级而非报错——召回结果本身仍然可用
    if app_rerank and rows:
        rows, rerank_err = _apply_rerank(reranker, q, rows)
        if rerank_err:
            degraded = f"{degraded}；{rerank_err}" if degraded else rerank_err

    # ---- 返回层：先算分 → 阈值过滤 → 去重 → 翻页 ----
    scored = [(r, *_score_of(r)) for r in rows]
    # score_kind 必须整列一致才敢对外声明。精排失败降级后结果集可能
    # 混有 rerank 分与未重排的原始分，取首行的 kind 会让调用方按错误
    # 的口径去卡 score_threshold（该字段是调用方可见的语义约定）。
    kinds = {s[2] for s in scored}
    score_kind = (kinds.pop() if len(kinds) == 1 else "mixed") if scored \
        else "none"

    if score_threshold is not None:
        scored = [s for s in scored if s[1] >= score_threshold]

    if group_by_doc:
        # 同一文档只留最高分：跨文档浏览时避免一篇文章霸屏
        best: dict[str, tuple] = {}
        for item in scored:
            did = item[0]["doc_id"]
            if did not in best or item[1] > best[did][1]:
                best[did] = item
        scored = list(best.values())

    total_candidates = len(scored)
    page = scored[offset:offset + top_k]
    has_more = offset + top_k < total_candidates
    rows = [s[0] for s in page]

    doc_meta = _doc_meta(store, sorted({r["doc_id"] for r in rows
                                        if r.get("doc_id")}))
    titles = {k: (v.get("title") or "") for k, v in doc_meta.items()}
    win = cfg.window
    ctx_by_doc = _contexts(store, rows, win) if (include_context and win) else {}
    snip_len = snippet_chars or DEFAULT_SNIPPET_CHARS
    results = []
    for r, score, kind in page:
        context: dict = {}
        if include_context and win:
            lo, hi = int(r["ordinal"]) - win, int(r["ordinal"]) + win
            context = {k: v for k, v in ctx_by_doc.get(r["doc_id"], {}).items()
                       if lo <= k <= hi}
        results.append({
            "chunk_id": r["chunk_id"],
            "doc_id": r["doc_id"],
            "kb_id": r.get("kb_id", "") or "",
            "title": titles.get(r["doc_id"], "") or "",
            "heading_path": r.get("heading_path", ""),
            "page": r.get("page"),
            "ordinal": int(r["ordinal"]),
            "char_start": int(r.get("char_start", 0) or 0),
            "char_end": int(r.get("char_end", 0) or 0),
            "offset_valid": bool(r.get("offset_valid", True)),
            # score 统一为「越大越相关」，kind 说明它的量纲
            "score": score,
            "score_kind": kind,
            "scores": {
                # 原始值保留：vector 是余弦距离（越小越近），其余越大越相关
                "vector_distance": float(r.get("_distance", 0.0) or 0.0),
                "fts": float(r.get("_score", 0.0) or 0.0),
                "rrf": float(r.get("_relevance_score", 0.0) or 0.0),
            },
            "snippet": make_snippet(r.get("text", ""), q, window=snip_len,
                                    highlight=highlight),
            "parser_engine": r.get("parser_engine", ""),
            "edited": bool(r.get("edited", False)),
            "enabled": bool(r.get("enabled", True)),
            "origin": r.get("origin", "parsed"),
            "context": context,
        })

    return {
        "query": q,
        "took_ms": round((time.perf_counter() - t0) * 1000, 1),
        "mode": cfg.mode,
        "score_kind": score_kind,
        "degraded_reason": degraded,
        # total_candidates 是**过滤/去重后、翻页前**的可用总数，用于判断还有没有下一页
        "total_candidates": total_candidates,
        "offset": offset,
        "top_k": top_k,
        "has_more": has_more,
        "results": results,
    }
