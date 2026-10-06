"""分块方案两级：知识库 → 文档。

**为什么取消「全局」这一级**：三级继承（全局 → 知识库 → 文档）要求用户
在脑子里维护一个「当前值来自哪一级」的三态推理，实测难以理解。
现在：

- 知识库**始终持有一组具体参数**（创建时用系统默认值预填，可改）；
- 文档可选覆盖；
- `settings.split` 退居为「新建知识库时的预填值」，不是运行时的继承层。

于是不再存在「chunk_size = 0 表示继承」这种需要解释的状态。


设计取舍：
- 文档级覆盖存 documents.meta.chunking（NULL/缺省 = 继承知识库），
  与既有 meta.kb_id / meta.stored_file 同一惯例，避免 LanceDB 加列。
- 知识库级存 kbs.chunk_size，chunk_size == 0 表示继承全局。
- 重叠一律按百分比 overlap_ratio 存（0-50）。若存绝对字符数，文档
  覆盖 chunk_size 时有效重叠比例会被静默改变（RAGFlow 同此设计）。
- 入库时把生效方案快照进 meta.chunking.snapshot，否则改设置后无法解释
  「这个块为什么是 1024 而知识库现在写的是 512」。
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from .chunk_limits import clamp_ratio, clamp_size, overlap_chars
from .repos import docs_query, get_document, kbs_query, set_doc_fields, set_kb_fields
from .sql import escape_sql, fetch_rows
from .sql import scalar as _scalar

if TYPE_CHECKING:
    # 只在注解里出现（文件顶部有 `from __future__ import annotations`）。
    # 留着模块级导入本身不算错，但它让「storage 里谁依赖谁」这张图
    # 多一条看不见的边 —— 同仓的 kbs/keys 都按注解处理，这里保持一致。
    from .tables import LanceStore

logger = logging.getLogger("raggi.plan")


def _meta(meta) -> dict:
    try:
        if not meta:
            return {}
        return json.loads(meta) if isinstance(meta, str) else dict(meta)
    except (TypeError, ValueError):
        return {}


def global_plan(settings) -> dict:
    """全局默认方案（来自 settings.split）。

    存的是绝对 chunk_overlap，这里换算回百分比展示，使三级方案
    可以在同一把尺子下比较（否则 64 字符重叠在不同块大小下含义不同）。
    """
    size = clamp_size(settings.split.chunk_size) or 512
    overlap = max(0, int(settings.split.chunk_overlap or 0))
    ratio = round(overlap * 100.0 / size, 1) if size else 0.0
    return {"chunk_size": size, "overlap_ratio": ratio,
            "source": "global", "custom": False,
            "overlap_chars": overlap_chars(size, ratio)}


def kb_plan(store: LanceStore, kb_id: str,
            settings) -> dict | None:
    """知识库的分块方案；kb 不存在返回 None。

    存量数据里 chunk_size == 0 表示「继承全局」——那是旧的三级模型。
    读取时按系统默认值兜底（真正的物化迁移见 materialize_kb_defaults），
    这样即使迁移还没跑，运行行为也不会错。
    """
    if not kb_id:
        return None
    rows = fetch_rows(store.kbs.search().where(
        f"kb_id = '{escape_sql(kb_id)}'").select(
        ["chunk_size", "overlap_ratio"]))
    if not rows:
        return None
    row = rows[0]
    size = clamp_size(_scalar(row.get("chunk_size")))
    ratio = clamp_ratio(_scalar(row.get("overlap_ratio")))
    if size <= 0:
        # 旧数据：物化为系统默认（比率按绝对重叠换算）
        g = global_plan(settings)
        return {"chunk_size": g["chunk_size"],
                "overlap_ratio": g["overlap_ratio"],
                "custom": False, "materialized": True}
    return {"chunk_size": size, "overlap_ratio": ratio, "custom": True}


def _doc_meta(store: LanceStore, doc_id: str) -> dict:
    """读文档的 meta（只承载 chunking 嵌套结构）。

    kb_id / stored_file 已提升为真实列，不再从这里取。
    """
    row = docs_query(store, ["meta"], eq={"doc_id": doc_id})
    return _meta(row[0].get("meta")) if row else {}


def doc_plan(store: LanceStore, doc_id: str) -> dict:
    """文档级覆盖（不含继承解析）。无覆盖时 chunk_size == 0。"""
    meta = _doc_meta(store, doc_id)
    ck = _meta(meta.get("chunking"))
    size = clamp_size(ck.get("chunk_size"))
    ratio = clamp_ratio(ck.get("overlap_ratio"))
    return {"chunk_size": size, "overlap_ratio": ratio,
            "custom": size > 0,
            "snapshot": ck.get("snapshot") or None}


def resolve_plan(store: LanceStore, settings, *,
                 kb_id: str = "", doc_id: str = "") -> dict:
    """解析最终生效方案：文档覆盖 > 知识库 > 系统默认。

    两级模型下知识库**总是**给出具体值，所以「知识库」不是条件分支，
    而是基线；只有文档覆盖是可选的。系统默认仅在文档不属于任何知识库
    时兜底（例如手工新建的独立内容）。
    """
    plan = global_plan(settings)
    plan["source"] = "default"
    if kb_id:
        kp = kb_plan(store, kb_id, settings)
        if kp:
            plan = {"chunk_size": kp["chunk_size"],
                    "overlap_ratio": kp["overlap_ratio"],
                    "source": "kb", "custom": kp.get("custom", True)}
    if doc_id:
        dp = doc_plan(store, doc_id)
        if dp["custom"]:
            plan = {"chunk_size": dp["chunk_size"],
                    "overlap_ratio": dp["overlap_ratio"],
                    "source": "doc", "custom": True}
    plan["overlap_chars"] = overlap_chars(
        plan["chunk_size"], plan["overlap_ratio"])
    return plan


def materialize_kb_defaults(store: LanceStore, settings) -> int:
    """把存量知识库里「继承全局」的占位值物化成具体数值。

    旧模型用 chunk_size == 0 表示继承，新增或修改时都要在运行时
    重新解析；两级模型下每个知识库直接持有数值。本迁移幂等：
    只处理 chunk_size <= 0 的行。

    返回物化的知识库数量。
    """
    g = global_plan(settings)
    rows = kbs_query(store, ["kb_id", "chunk_size", "overlap_ratio"])
    moved = 0
    for r in rows:
        if clamp_size(_scalar(r.get("chunk_size"))) > 0:
            continue
        try:
            set_kb_fields(store, str(r["kb_id"]),
                          chunk_size=g["chunk_size"],
                          overlap_ratio=g["overlap_ratio"])
            moved += 1
        except Exception as e:  # noqa: BLE001
            # warning 而不是 debug：这是启动期的一次性**迁移**，半途而废意味着
            # 有些知识库的 chunk_size 还停在 0（旧语义「继承」），而两级模型
            # 已经取消那一层 —— 少了这条日志，没人知道它没迁完。
            logger.warning("物化知识库 %s 的方案失败: %s", r.get("kb_id"), e)
    if moved:
        logger.info("已把 %d 个知识库的「继承全局」物化为具体方案 "
                    "(%d 字符 / %.1f%%)",
                    moved, g["chunk_size"], g["overlap_ratio"])
    return moved


def set_kb_plan(store: LanceStore, kb_id: str, *, chunk_size,
                overlap_ratio, settings=None) -> None:
    """写知识库方案。

    两级模型下知识库始终持有具体值：chunk_size 传 0/None 表示
    「恢复系统默认」，此时**物化**为默认数值，而不是留一个需要
    运行时解释的 0。
    """
    size = clamp_size(chunk_size)
    ratio = clamp_ratio(overlap_ratio)
    if size <= 0:
        g = global_plan(settings) if settings is not None else {
            "chunk_size": 512, "overlap_ratio": 12.5}
        size, ratio = g["chunk_size"], g["overlap_ratio"]
    set_kb_fields(store, kb_id, chunk_size=size, overlap_ratio=ratio)


def set_doc_plan(store: LanceStore, doc_id: str, *,
                 chunk_size, overlap_ratio) -> None:
    """写文档覆盖方案。chunk_size 为 None/0 时清除覆盖（恢复继承）。"""
    row = get_document(store, doc_id)
    if row is None:
        raise KeyError("document not found")
    meta = _meta(row.get("meta"))
    if not chunk_size:
        meta.pop("chunking", None)
    else:
        ck = _meta(meta.get("chunking"))
        ck.pop("snapshot", None)   # 改覆盖后旧快照失效，待重切分再写
        ck["chunk_size"] = clamp_size(chunk_size)
        ck["overlap_ratio"] = clamp_ratio(overlap_ratio)
        meta["chunking"] = ck
    set_doc_fields(store, doc_id, meta=json.dumps(meta))


def stamp_snapshot(store: LanceStore, doc_id: str,
                   plan: dict) -> None:
    """入库/重切分后把生效方案快照进 meta.chunking.snapshot。"""
    row = get_document(store, doc_id)
    if row is None:
        return
    meta = _meta(row.get("meta"))
    ck = _meta(meta.get("chunking"))
    ck["snapshot"] = {"chunk_size": plan["chunk_size"],
                      "overlap_ratio": plan["overlap_ratio"],
                      "source": plan["source"]}
    meta["chunking"] = ck
    set_doc_fields(store, doc_id, meta=json.dumps(meta))
