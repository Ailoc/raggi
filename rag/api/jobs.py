"""入库任务查询端点：任务列表 / 单个任务状态。

入库走队列后，客户端有两种使用方式：
- **同步**（`wait=true`，默认）：请求等到任务结束，直接拿结果；
- **异步**（`wait=false`）：立即返回 job_id，用本组端点轮询进度。

前端用异步：上传大文件时界面立刻可用，进度条由轮询驱动，
不必让 HTTP 连接挂到解析与向量化全部完成。
"""
from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import APIRouter, HTTPException, Request

from rag.api._common import get_ctx
from rag.api.responses import JobCancelOut, JobListOut, JobOut, error_responses
from rag.ingest.queue import TERMINAL_STAGES, get_job, list_jobs

router = APIRouter(
    tags=["jobs"],
    responses=error_responses(not_found=True),
)


@router.get("/jobs")
async def api_jobs(request: Request, limit: int = 50, offset: int = 0,
                   status: Literal["all", "active", "failed", "done",
                                   "cancelled"] = "all",
                   active_only: bool = False) -> JobListOut:
    """最近的任务列表（按开始时间倒序）。

    `status` 在**分页之前**过滤，`total` 是过滤后的行数；`offset` 真正
    下推到 SQL/Lance。旧实现的 offset 被忽略、active_only 先截断再过滤，
    会出现「第 2 页等于第 1 页」「活跃任务排在 50 条之后就从界面消失」。

    active_only 是兼容旧客户端的别名：等价于 status=active（当 status 仍为
    all 时）。每行带 terminal 派生字段，与单任务端点保持同一形状。
    """
    ctx = get_ctx(request)
    if active_only and status == "all":
        status = "active"
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    items, total = await asyncio.to_thread(
        list_jobs, ctx.store, limit, "", offset, status)
    out = []
    for j in items:
        row = dict(j)
        row["terminal"] = row.get("stage") in TERMINAL_STAGES
        out.append(row)
    return {"items": out, "total": total,
            "queue": ctx.queue.stats()}


@router.get("/jobs/{job_id}")
async def api_job(request: Request, job_id: str) -> JobOut:
    """单个任务状态。done / failed / cancelled 是终态，前端可停止轮询。"""
    ctx = get_ctx(request)
    row = await asyncio.to_thread(get_job, ctx.store, job_id)
    if row is None:
        raise HTTPException(404, "job not found")
    row = dict(row)
    row["terminal"] = row.get("stage") in TERMINAL_STAGES
    return row


# outcome → 给用户看的话。区分「真取消了」和「在停止中」很重要：
# 前者任务不会再动，后者当前阶段还会跑完（技术边界，见 IngestQueue.cancel）。
_CANCEL_MESSAGES = {
    "cancelled": "任务已取消（尚未开始执行）",
    "running": "已请求取消：当前阶段结束后停止"
               "（解析与向量化无法中途打断）",
    "finished": "任务已结束，无需取消",
    "unknown": "任务不存在",
}


@router.delete("/jobs/{job_id}")
async def api_job_cancel(request: Request, job_id: str) -> JobCancelOut:
    """取消一个入库任务。

    能力边界必须说清楚：**排队中的任务是真取消**（从队列撤销，永不执行）；
    **运行中的只能协作式取消**——解析（PyMuPDF）与向量化（HTTP）都不响应
    中断，已发出的调用必须跑完，任务会在下一个阶段边界自行退出。
    """
    ctx = get_ctx(request)
    outcome = await asyncio.to_thread(ctx.queue.cancel, job_id)
    row = await asyncio.to_thread(get_job, ctx.store, job_id)
    if outcome == "unknown":
        raise HTTPException(404, "job not found")
    return {
        "ok": True,
        "job_id": job_id,
        "outcome": outcome,
        "stage": str((row or {}).get("stage") or ""),
        "message": _CANCEL_MESSAGES.get(outcome, ""),
    }
