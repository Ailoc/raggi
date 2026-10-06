"""入口：CLI（rag serve / rag reindex / rag migrate）+ 装配 store / registry / app。

`rag serve` 现在是 **supervisor**：仍然是一个命令、一个端口、一个数据目录，
但默认拉起多个只读 API 进程（`--processes auto` = min(cpu, 4)）。

为什么要多进程：同一份 LanceDB 读，同进程多线程实测只有 **1.28×** 并行度
（线程数加到 8 就不再涨），而多进程能到 **2.68×**
（docs/PERF-CONCURRENCY-2026-10-05.md §2.4）。单进程的天花板是 GIL，
不是核数，也不是数据库。

安全前提：LanceDB 的写入与索引维护由 `data/lance-write.lock` 这把
**跨进程文件锁**串行化（`storage/filelock.py`），因此多进程读 +
多进程写不会互相踩。s3 后端下这把锁不成立，`LanceStore` 会退回进程内
互斥并打 warning——那种形态请保持 `--processes 1`。
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from pathlib import Path

# 启动期维护的「最近做过」标记：多进程下每个 worker 都会跑一次
# 建索引/FTS 重建，那是同一份活干 N 遍（还互相抢写锁）。用标记文件把
# 它收敛成「每台机器每 MAINTAIN_MARKER_TTL 秒最多一次」。
MAINTAIN_MARKER = ".last_index_maintenance"
MAINTAIN_MARKER_TTL_SECONDS = 300.0

log = logging.getLogger("raggi")


def build_app():
    """装配并返回 ASGI 应用。uvicorn 以 factory 形式在每个 worker 进程里调它。"""
    from rag.api import Ctx, create_app
    from rag.core.config import settings
    from rag.models.registry import ModelRegistry
    from rag.storage.backend import build_backend
    from rag.storage.tables import LanceStore

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    backend = build_backend(settings.storage, settings.data_dir)
    store = LanceStore(backend, settings.embed.dim)
    log.info("存储后端：%s → %s，写锁：%s", backend.kind, backend.lance_uri(),
             store.lock_kind)

    # 分块方案两级化：把存量知识库里「继承全局」的占位 0 物化为具体值
    from rag.storage import plan as chunking
    from rag.storage.meta import prepare_meta_store

    chunking.materialize_kb_defaults(store, settings)
    # OLTP 元数据（jobs / apikeys / kbs / documents）走 SQLite(WAL)。
    # 必须在 registry 之前、且每个 worker 进程各建一次连接（MetaStore
    # 内部按线程隔离连接）；auto 模式下首启会从 LanceDB 旧表一次性导入。
    meta = prepare_meta_store(settings, store, log=log)
    registry = ModelRegistry(settings)
    app = create_app(Ctx(settings, store, registry, meta=meta))
    return app


def should_run_startup_maintenance(data_dir: Path,
                                   now: float | None = None) -> bool:
    """这台机器最近做过启动期索引维护吗。

    多进程形态下每个 worker 都会跑一遍建索引 / FTS 重建，那是同一份活干
    N 遍——而且它们会排队抢同一把跨进程写锁，把冷启动时间乘以进程数。
    这里用数据目录里的一个标记文件把它收敛掉。

    标记只在「维护成功启动」时写入；崩溃的进程留下过期标记的代价是
    下一次启动多做一遍，而不是少做——这个方向是安全的。
    """
    marker = data_dir / MAINTAIN_MARKER
    try:
        mtime = marker.stat().st_mtime
    except OSError:
        return True
    return (now or time.time()) - mtime > MAINTAIN_MARKER_TTL_SECONDS


def mark_maintenance_done(data_dir: Path) -> None:
    try:
        (data_dir / MAINTAIN_MARKER).touch()
    except OSError as e:  # pragma: no cover
        log.debug("写维护标记失败（不影响功能）: %s", e)


def _resolve_processes(raw: str | None) -> int:
    """`auto` → min(cpu, 4)。

    上限取 4 是这台机器实测的拐点：读并行度在 4 进程时到 2.68×，
    再往上（8 进程）反而回落到 401 ops/s——核已经被占满了，
    多出来的只是内存与上下文切换（每进程还各有一份表句柄缓存）。

    `raw` 来自 `settings.api_processes`，因此 CLI `--processes`、
    环境变量 `RAG_API_PROCESSES`、config.toml 三条路都走同一个解析。
    """
    import math

    if raw in (None, "", "auto"):
        return max(1, min(os.cpu_count() or 1, 4))
    if raw in ("0", "1"):
        return 1
    try:
        n = int(float(raw))
    except ValueError:
        # 非法值退回**最保守**的 1 进程，而不是 auto：
        # 配置写错时不该让服务悄悄起成多进程（写锁形态会变）。
        log.warning("--processes / api_processes 的值 %r 不是数字，"
                    "按 1 个进程启动", raw)
        return 1
    return max(1, min(n, max(1, math.floor((os.cpu_count() or 1) * 2))))


def main() -> None:
    ap = argparse.ArgumentParser(prog="rag", description="Raggi 极简单机 RAG")
    ap.add_argument("command", nargs="?", default="serve",
                    choices=["serve", "reindex", "migrate"])
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--data", default=None, help="数据目录（覆盖 RAG_DATA_DIR）")
    ap.add_argument("--check", action="store_true",
                    help="migrate：只校验行数一致性，不改变已有 SQLite 数据")
    ap.add_argument("--processes", default=None,
                    help="只读 API 进程数：auto（默认，min(cpu,4)）或数字；"
                         "1 = 原来的单进程形态。覆盖 RAG_API_PROCESSES "
                         "与 config.toml 的 api_processes")
    args = ap.parse_args()

    if args.data:
        os.environ["RAG_DATA_DIR"] = args.data
    if args.host:
        os.environ["RAG_HOST"] = args.host
    if args.port:
        os.environ["RAG_PORT"] = str(args.port)
    if args.processes is not None:
        # 与 --host/--port 同一套做法：写进环境，让 Settings 成为唯一的
        # 配置读取点。早前这里是「先解析 argv 再绕过 Settings」，
        # 于是 config.toml 里的 api_processes 根本没有发言权。
        os.environ["RAG_API_PROCESSES"] = str(args.processes)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from rag.core.config import settings

    if args.command == "serve":
        import uvicorn

        n_proc = _resolve_processes(settings.api_processes)

        # uvloop 是 libuv 事件循环的 Python 绑定，能提升高并发下的事件分发
        # 效率。可选依赖：缺了就退回 asyncio 默认循环，不影响功能。
        loop = "auto"
        try:
            import uvloop  # noqa: F401  仅探测可用性

            loop = "uvloop"
        except ImportError:
            log.debug("uvloop 未安装，使用 asyncio 默认事件循环")

        log.info("Raggi 启动: http://%s:%s  (data=%s, processes=%d, loop=%s)",
                 settings.host, settings.port, settings.data_dir, n_proc, loop)
        if n_proc <= 1:
            # 单进程：直接传 app 对象，冷启动最快，行为与改造前完全一致
            uvicorn.run(build_app(), host=settings.host, port=settings.port,
                        loop=loop)
        else:
            # 多进程：uvicorn 需要 import 串 + factory=True，
            # 每个 worker 自己装配一份 store/registry（共享同一个数据目录）
            uvicorn.run("rag.server:build_app", factory=True,
                        host=settings.host, port=settings.port,
                        workers=n_proc, loop=loop)
    elif args.command == "migrate":
        # 显式迁移/校验入口。启动期本来就会 auto 导入，这条命令的存在意义是
        # 让运维能**先看一眼再切**：升级前跑一次 `rag migrate --check`，
        # 行数不一致就先去备份，而不是等启动日志里报错。
        from rag.storage.backend import build_backend
        from rag.storage.meta import import_from_lance, needs_import, open_meta_store
        from rag.storage.tables import LanceStore

        settings.data_dir.mkdir(parents=True, exist_ok=True)
        store = LanceStore(build_backend(settings.storage, settings.data_dir),
                           settings.embed.dim)
        meta = open_meta_store(settings)
        if meta is None:
            log.error("storage.meta_engine=lancedb：回退开关是打开的，"
                      "migrate 不会导入。改回 auto/sqlite 再跑。")
            raise SystemExit(2)
        if needs_import(meta) or args.check:
            report = import_from_lance(meta, store)
            log.info("迁移结果：%s", report)
            bad = [k for k, v in report.items()
                   if isinstance(v, dict) and not v.get("ok")]
            if bad:
                log.error("以下表行数校验未通过：%s（data/ 未改动旧表，"
                          "可用 storage.meta_engine=lancedb 退回）", bad)
                raise SystemExit(3)
        else:
            report = {t: meta.count(t) for t in
                      ("documents", "jobs", "kbs", "apikeys")}
            log.info("SQLite 已有数据，跳过导入：%s", report)
        log.info("元数据引擎：%s", meta.stats())
    elif args.command == "reindex":
        from rag.storage.backend import build_backend
        from rag.storage.tables import LanceStore

        settings.data_dir.mkdir(parents=True, exist_ok=True)
        store = LanceStore(build_backend(settings.storage, settings.data_dir),
                           settings.embed.dim)
        store.ensure_vector_index(force=True)
        store.ensure_fts_index(force=True)
        store.ensure_scalar_indexes(force=True)
        store.optimize()
        store.compact_tables()
        mark_maintenance_done(settings.data_dir)
        log.info("reindex 完成")


if __name__ == "__main__":
    main()
