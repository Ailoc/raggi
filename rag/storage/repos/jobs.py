"""任务表（jobs）仓储。

jobs 是纯观测数据，但它是**写最热**的表：一次入库要写 5–8 次（queued →
parse/load/split/embed/write → 终态）。实测一次入库给 jobs 表带来 ≈5 个
Lance 版本 / 5 个数据文件，48 次入库后是 226 个版本、224 个文件，
`list_versions()` 因此线性变慢（0.17ms/版本），而且这张表**一个索引都没有**，
每次 stage 更新都是一次全表扫描。见 docs/PERF-CONCURRENCY-2026-10-05.md §2.6。

所以它是第一块搬进 SQLite(WAL) 的表：读写全收在本模块、没有跨表外键，
风险最小、收益最大。

分派规则：`store.meta` 存在 → SQLite；不存在（`storage.meta_engine=lancedb`
这个回退开关）→ 原来的 LanceDB 路径。旧表在 LanceDB 里保留不删，
因此回退不需要恢复备份。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..meta import JOB_COLS
from ..sql import escape_sql, only_cols, scalar_rows

if TYPE_CHECKING:
    from ..tables import LanceStore

# 终态：不再变化，前端可以停止轮询。
# cancelled 也算终态：排队中被撤销，或运行中在阶段边界退出。
TERMINAL_STAGES = frozenset({"done", "failed", "cancelled"})

# JOB_COLS 不在这里定义：它就是 `meta.DDL` 里 jobs 的列序，
# 归 schema 层（`..meta`）持有，两处各写一份时靠人肉同步。


def _meta(store: "LanceStore"):
    """元数据引擎；None 表示走 LanceDB 回退路径。"""
    return getattr(store, "meta", None)


def add_job(store: "LanceStore", row: dict) -> None:
    """写入一条任务记录（入队时即写，前端拿到 job_id 就能查到状态）。"""
    meta = _meta(store)
    if meta is not None:
        meta.upsert("jobs", [{k: row.get(k) for k in JOB_COLS}], "job_id")
        return
    store.jobs.add([{k: v for k, v in row.items() if k in JOB_COLS}])


def set_job(store: "LanceStore", job_id: str, **values) -> bool:
    """更新任务的若干字段。

    失败不抛 —— 任务状态是观测数据，不该拖垮入库本身。
    """
    if not values:
        return False
    meta = _meta(store)
    if meta is not None:
        sets = ", ".join(f"{k}=?" for k in values)
        try:
            meta.execute(f"UPDATE jobs SET {sets} WHERE job_id=?",
                         (*values.values(), job_id))
            return True
        except Exception:  # noqa: BLE001
            return False
    payload = {k: v for k, v in values.items() if k in JOB_COLS}
    if not payload:
        return False
    try:
        store.jobs.update(where=f"job_id = '{escape_sql(job_id)}'",
                          values=payload)
        return True
    except Exception:  # noqa: BLE001
        return False


def get_job(store: "LanceStore", job_id: str,
            cols: list[str] | None = None) -> dict | None:
    # 投影过白名单：列名做不成 SQL 占位符，不拦就是注入通道。
    # 放在分派**之前** —— 两条引擎路径都要收窄，只收 SQLite 那条等于没收。
    want = only_cols(cols or JOB_COLS, set(JOB_COLS), table="jobs")
    meta = _meta(store)
    if meta is not None:
        return meta.query_one(
            f"SELECT {', '.join(want)} FROM jobs WHERE job_id=?", (job_id,))
    rows = scalar_rows(store.jobs, cols=want,
                       where=f"job_id = '{escape_sql(job_id)}'", limit=1)
    return rows[0] if rows else None


def list_jobs(store: "LanceStore", limit: int = 50,
              kb_id: str = "") -> tuple[list[dict], int]:
    """最近任务（按开始时间倒序）+ 总数。

    排序与分页下推：改前是「全表取回 + Python 排序 + 切片」，
    而这张表恰恰是增长最快的一张（任务记录保留 7 天）。
    """
    n = max(1, min(int(limit), 500))
    meta = _meta(store)
    if meta is not None:
        where, params = ("", ())
        if kb_id:
            where, params = "WHERE kb_id=?", (kb_id,)
        rows = meta.query(
            f"SELECT {', '.join(JOB_COLS)} FROM jobs {where} "
            "ORDER BY started_at DESC, job_id LIMIT ?", (*params, n))
        return rows, meta.count("jobs", where or None, params)
    where = f"kb_id = '{escape_sql(kb_id)}'" if kb_id else None
    rows = scalar_rows(store.jobs, cols=JOB_COLS, where=where,
                       order_by=[("started_at", False), ("job_id", True)],
                       limit=n)
    try:
        total = store.jobs.count_rows(filter=where) if where \
            else store.jobs.count_rows()
    except Exception:  # noqa: BLE001
        total = len(rows)
    return rows, int(total)


def is_terminal(row: dict | None) -> bool:
    return bool(row) and str(row.get("stage")) in TERMINAL_STAGES


def prune_jobs(store: "LanceStore", cutoff_iso: str) -> int:
    """删除 started_at 早于 cutoff **且已终结**的任务记录，返回删除条数。

    未终结的任务不清：started_at 很老但仍在跑的任务，删了就没法查状态了。
    一条 DELETE 搞定，不逐条删 —— 逐条就是 N 次写、N 个新版本。
    """
    meta = _meta(store)
    if meta is not None:
        return meta.execute(
            "DELETE FROM jobs WHERE started_at < ? AND ended_at IS NOT NULL",
            (cutoff_iso,)).rowcount or 0
    where = (f"started_at < '{escape_sql(cutoff_iso)}' "
             "AND ended_at IS NOT NULL")
    try:
        before = store.jobs.count_rows(filter=where)
    except Exception:  # noqa: BLE001
        before = len(scalar_rows(store.jobs, cols=["job_id"], where=where))
    try:
        store.jobs.delete(where=where)
    except Exception:  # noqa: BLE001
        return 0
    return int(before)
