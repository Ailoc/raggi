"""系统端点：健康 / 容量统计 / 版本管理 / 回滚 / 重建索引。

这两个「观测端点」原来是全服务最贵的读：
- `/health` 要对账（扫全部 chunk 的 doc_id 与 embed_model、列版本）≈ 40ms，
  写入变多后升到 89ms；
- `/stats` 还要**递归遍历 data 目录**算字节数，32 并发时 p50 到 5s。
前端与运维脚本会反复打它们，于是它们自己就能把服务打死
（见 docs/PERF-CONCURRENCY-2026-10-05.md §2.1）。

处置是**结果缓存 + 并发合流**，而不是把端点拆成两个：
拆端点会改响应契约（界面的「健康 / 数据」面板都在读这些字段），
而这两个数本来就是「观测快照」，几秒钟的陈旧不影响它的用途。
`X-Snapshot-Age-Ms` 响应头把「这是缓存」如实告诉调用方。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from rag.api._common import get_ctx
from rag.api.responses import (HealthOut, OkOut, ReconcileOut, RollbackOut,
                               StatsOut, VersionsOut)
from rag.api.schemas import RollbackReq
from rag.core.cache import TTLCache
from rag.storage.repos import kbs
from rag.storage.health import health

router = APIRouter(tags=["system"])

# 快照存活时间：3s 足够让「打开面板 → 连续刷新」只算一次，
# 又不至于让 reindex/入库后的界面长时间停留在旧数。
SNAPSHOT_TTL_SECONDS = 3.0
_health_cache = TTLCache(max_items=8, ttl_seconds=SNAPSHOT_TTL_SECONDS)
_stats_cache = TTLCache(max_items=8, ttl_seconds=SNAPSHOT_TTL_SECONDS)


def invalidate_snapshots() -> None:
    """任何一次**成功的写**都要让健康/容量快照立刻作废。

    只靠 TTL 会出现一类很难受的假象：用户改完分块、或点了「修复计数」，
    界面再读 `/health` 看到的还是 3 秒前的旧账（`test_reconcile_endpoint_repairs_counts`
    最初就是在这里红的）。缓存省的是「重复读」的成本，不能省掉「我刚改完了」这个事实。

    粗粒度清空（不按库/按字段失效）是有意的：这里只有一个 store，
    而判错的代价是用户看到过期状态。
    """
    _health_cache.clear()
    _stats_cache.clear()


def _health_key(ctx):
    """缓存键 = **数据目录** + 生效的 embed(dim, model)。

    数据目录必须进键：`/health` 对账的是「这个库」的状态。只按
    (dim, model) 缓存会让两个不同的库互相串数据 —— 测试里每个用例都是
    一个独立 tmp_path，第一个用例的 degraded/ok 会被后面的用例读到。
    生产上同一进程只有一份 store，但这条纪律不该依赖部署形态。

    (dim, model) 也要进键：`PUT /api/models` 改维度或换模型后，
    旧快照里的 dim_mismatch / embedding_model_mismatch 会变成假数据。
    """
    cfg = ctx.registry.bundle.embed_cfg
    return (ctx.store.uri, cfg.dim, cfg.model)


@router.get("/health")
async def api_health(request: Request,
                     fresh: bool = False) -> HealthOut:
    """健全性对账（带 3s 快照）。`fresh=true` 强制现算，用于排障。"""
    ctx = get_ctx(request)
    key = _health_key(ctx)
    if fresh:
        _health_cache.invalidate(key)
    age_ms = _health_cache.age_ms(key)
    snap = _health_cache.get(key)
    if snap is None:
        snap = await asyncio.to_thread(
            health, ctx.store, ctx.registry.bundle.embed_cfg.dim,
            ctx.registry.bundle.embed_cfg.model)
        _health_cache.set(key, snap)
        age_ms = 0.0
    # 诚实标注「这份对账是多少毫秒前的」：调用方据此知道刚才那次入库
    # 还没反映进来，而不是把旧快照当成实时状态去做判断。
    request.state.snapshot_age_ms = round(age_ms, 1)
    return snap


@router.get("/stats")
async def api_stats(request: Request, fresh: bool = False) -> StatsOut:
    """容量/索引/版本/磁盘统计（带 3s 快照）。

    里面最贵的两项：递归遍历 data 目录算体积、以及 `list_versions()`
    （实测 ≈0.17ms/版本，本仓库真实实例的 jobs 表有 379 个版本 = 64ms）。
    """
    ctx = get_ctx(request)
    key = ctx.store.uri
    if fresh:
        _stats_cache.invalidate(key)
    age_ms = _stats_cache.age_ms(key)
    snap = _stats_cache.get(key)
    if snap is None:
        snap = await asyncio.to_thread(ctx.store.stats)
        _stats_cache.set(key, snap)
        age_ms = 0.0
    request.state.snapshot_age_ms = round(age_ms, 1)
    return snap


@router.get("/versions")
async def api_versions(request: Request, table: str = "chunks") -> VersionsOut:
    """列出 LanceDB 表的历史版本（回滚用）。"""
    ctx = get_ctx(request)
    versions = await asyncio.to_thread(
        ctx.store.table_versions, table)
    return {"table": table, "versions": versions}


@router.post("/rollback")
async def api_rollback(request: Request, body: RollbackReq) -> RollbackOut:
    """把指定表回滚到历史版本。

    注意：LanceDB 版本按表管理，回滚 chunks 表后 documents 表需对账
    （/api/health），必要时重放 documents。
    """
    ctx = get_ctx(request)
    await asyncio.to_thread(
        ctx.store.restore_version, body.version, body.table)
    return {"ok": True, "table": body.table,
            "rolled_back_to": body.version,
            "note": "仅回滚指定表；请用 /api/health 对账"}


@router.post("/reindex")
async def api_reindex(request: Request) -> OkOut:
    ctx = get_ctx(request)
    await asyncio.to_thread(_reindex, ctx)
    return {"ok": True}


@router.post("/reconcile")
async def api_reconcile(request: Request) -> ReconcileOut:
    """按实际分块数回写 documents.chunk_count（修复计数漂移）。

    供 /api/health 报出 count_mismatch 后一键修复。
    """
    ctx = get_ctx(request)
    fixed = await asyncio.to_thread(kbs.reconcile_doc_counts, ctx.store)
    return {"ok": True, "fixed": fixed}


def _reindex(ctx) -> None:
    ctx.store.ensure_vector_index(force=True)
    ctx.store.ensure_fts_index(force=True)
    ctx.store.ensure_scalar_indexes(force=True)
    ctx.store.optimize()
