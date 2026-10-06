"""LanceDB 连接、建表、索引维护。

充分使用：建表、向量索引（HNSW / IVF_HNSW_SQ / IVF_PQ 按规模自适应）、
原生 FTS、标量索引(BTREE)、版本/optimize。
索引采用「按需 / 增量重建 + 后台合并调度」策略，避免每次入库都全量重建。
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from pathlib import Path

import lancedb
import pyarrow as pa
from lancedb.index import FTS, BTree, HnswFlat, IvfHnswSq, IvfPq

from .repos.documents import docs_count
from .repos.jobs import list_jobs as _list_jobs
from .schema import ApiKey, Document, Job, KnowledgeBase, chunk_schema

logger = logging.getLogger("raggi.store")

# 行数低于该阈值不建向量索引（暴力扫描即可）
MIN_VECTOR_INDEX_ROWS = 256
# 未索引行占比超过该比例时重建向量索引
REBUILD_UNINDEXED_RATIO = 0.2
# 小于该行数的库，FTS 直接重建（便宜且保证新增/编辑立即生效）
FTS_REBUILD_ALWAYS_BELOW = 50000
# 但 FTS 重建不能「每次入库都来一遍」：它是全表扫，且每次重建都会在
# chunks 表上多留下版本（实测 0.1s/次、+2 版本）。这两条给出节流边界。
FTS_MIN_INTERVAL_SECONDS = 60.0
FTS_BACKLOG_ROWS = 200
# 版本/碎片治理：超过这两个数就顺手做一次 optimize（compact + 旧版本清理）。
# 只在「写得多」时触发，不给普通读路径增加任何开销。
COMPACT_VERSION_THRESHOLD = 40
COMPACT_DEBOUNCE_SECONDS = 300.0
# 清理旧版本时保留的时间下界：必须**远大于**最长请求/检索时长，
# 否则可能回收掉仍在被读的快照。
OPTIMIZE_KEEP_OLDER_THAN = timedelta(minutes=10)
# 索引类型切换阈值（DESIGN §5.3）
HNSW_MAX_ROWS = 50_000
IVF_HNSW_SQ_MAX_ROWS = 1_000_000


class LanceStore:
    """LanceDB 表管理。

    连接参数来自 `Backend`——业务层不需要知道数据在本地磁盘还是
    S3 兼容对象存储（RustFS / MinIO / AWS）。
    """

    def __init__(self, backend, dim: int, *, process_lock: bool = True):
        self.backend = backend
        self.uri = backend.lance_uri()
        self.dim = dim
        opts = backend.lance_options()
        self.db = lancedb.connect(self.uri, storage_options=opts or None)
        # 串行化「写入 + 索引维护」，避免并发入库竞争索引 / 重复文档。
        #
        # 本地后端下这是一把**跨进程**文件锁（flock），于是「多进程读 +
        # 单写临界区」成为安全形态——这一条是解掉 DESIGN §16
        # 「不支持 --workers>1」的关键：线程并行度实测封顶 1.28×，
        # 进程并行度是 2.68×（docs/PERF-CONCURRENCY-2026-10-05.md §2.4）。
        # s3 后端下每台机器各有一把本地锁等于没锁，因此退回进程内互斥并**告警**。
        self._lock = self._build_write_lock(backend, process_lock)
        # 表句柄缓存：避免每次属性访问都重复 open_table（读表元数据）
        self._tables: dict[str, object] = {}
        # FTS 重建与 compaction 的节流时间戳（monotonic 秒；None = 从未做过）
        self._last_fts_at: float | None = None
        self._last_compact_at: float | None = None
        self._ensure_tables()
        # 后台索引维护：单线程 + 去抖合并，入库响应不等待索引重建
        self._maint_lock = threading.Lock()
        self._maint_pending = False
        self._maint_thread: threading.Thread | None = None

    def _build_write_lock(self, backend, process_lock: bool):
        """按存储后端选择写锁形态。"""
        from .filelock import FileLock, ReentrantMutex

        if not process_lock:
            return ReentrantMutex()
        if getattr(backend, "kind", "local") != "local":
            logger.warning(
                "存储后端是 %s：本地文件锁无法跨机互斥，写锁退回**进程内**互斥。"
                "多机部署请把 LanceDB 的写入集中到单一进程"
                "（见 docs/PERF-FINAL-DECISION-2026-10-05.md 的 D2）。",
                getattr(backend, "kind", "?"))
            return ReentrantMutex()
        data_dir = getattr(backend, "data_dir", None)
        if not data_dir:
            return ReentrantMutex()
        return FileLock(Path(data_dir) / "lance-write.lock")

    @property
    def lock_kind(self) -> str:
        """写锁形态（`/api/health` 与运维日志据此判断能不能安全开多进程）。"""
        from .filelock import FileLock

        return "cross-process" if isinstance(self._lock, FileLock) \
            else "in-process"

    @classmethod
    def for_data_dir(cls, data_dir, dim: int) -> "LanceStore":
        """从本地数据目录构造（脚本与测试的便捷入口）。

        生产路径应显式构造 `Backend` 再传入——那样才能在配置层面
        选择 local / s3。
        """
        from pathlib import Path as _Path

        from .backend import LocalBackend

        return cls(LocalBackend(_Path(data_dir)), dim)

    @property
    def meta(self):
        """OLTP 元数据引擎（SQLite/WAL）；None = `meta_engine=lancedb` 回退。

        **为什么做成属性而不是随手挂上的实例属性**：仓储层的引擎分派全靠
        `getattr(store, "meta", None)`，而这个属性过去由装配层
        （`api.Ctx.__init__`）在外部赋值 —— 忘了赋、或者另一条装配路径
        （脚本、测试、`rag migrate`）没赋，读写就会**分别落到两个引擎上**，
        而且不报错。真实事故有两起：`has_keys()` 读 Lance 的空表 ⇒ 整个 API
        变成无鉴权；列表读 SQLite、健康检查读 Lance ⇒ 「8 篇文档 / 说只有 7 篇」。
        声明在这里至少让「它的存在与含义」有一个权威定义点。
        """
        return self.__dict__.get("_meta_store")

    @meta.setter
    def meta(self, value) -> None:
        self.__dict__["_meta_store"] = value

    @property
    def kbs(self):
        """知识库表（句柄缓存）。"""
        t = self._tables.get("kbs")
        if t is None:
            t = self._table("kbs")
        return t

    def _ensure_tables(self) -> None:
        # list_tables() 返回 ListTablesResponse（含 .tables 名称列表），
        # 不是表对象——早前按对象取 .name 会直接 AttributeError。
        resp = self.db.list_tables()
        names = set(getattr(resp, "tables", resp) or [])
        if "documents" not in names:
            self.db.create_table("documents", schema=Document)
        if "jobs" not in names:
            self.db.create_table("jobs", schema=Job)
        if "kbs" not in names:
            self.db.create_table("kbs", schema=KnowledgeBase)
        if "apikeys" not in names:
            self.db.create_table("apikeys", schema=ApiKey)
        if "chunks" not in names:
            self.db.create_table("chunks", schema=chunk_schema(self.dim))
        # 旧库迁移：为后加的列补齐（add_columns 只保证列存在）
        self._migrate_columns()
        # 维度守护：配置 dim 与已有表不匹配时 loudly 告警
        # （写入会失败，必须改配置或换数据目录后 reindex）
        actual = self.vector_dim()
        if actual is not None and actual != self.dim:
            logger.warning(
                "向量维度不匹配：配置 dim=%d，现有 chunks 表 "
                "dim=%d。请将 RAG_EMBED__DIM 改为 %d（或更换数据"
                "目录）后重启并 reindex",
                self.dim, actual, actual)

    def _migrate_columns(self) -> None:
        """给已存在的表补齐新版本引入的列（幂等）。

        LanceDB add_columns 只加列、存量行填 NULL，因此加列后必须
        立即回填默认值——否则 `where enabled = true` 会把历史分块
        全部过滤掉（检索静默变空）。
        """
        self._ensure_cols("chunks", {
            "kb_id": pa.string(),
            "enabled": pa.bool_(),
            # fts_stale：编辑/新增后待重建 FTS 的标记（ensure_fts_index 清除）。
            # 缺它时所有走 merge_insert 的写路径（新增 / 编辑分块）都会
            # 报 "Field 'fts_stale' not found in target schema" → 500。
            "fts_stale": pa.bool_(),
        }, backfill={"kb_id": "", "enabled": True, "fts_stale": False})
        self._ensure_cols("kbs", {
            "chunk_size": pa.int64(),
            "overlap_ratio": pa.float64(),
        }, backfill={"chunk_size": 0, "overlap_ratio": 0.0})
        # jobs：kb_id 用于把任务链回知识库（早于 doc_id 生成即已知）。
        # 缺它时所有入队写路径报 "Append with different schema:
        # missing=[kb_id]" → 500。
        self._ensure_cols("jobs", {
            "kb_id": pa.string(),
        }, backfill={"kb_id": ""})
        # documents：kb_id / stored_file 从 meta JSON 提升为真实列
        migrated = self._ensure_cols("documents", {
            "kb_id": pa.string(),
            "stored_file": pa.string(),
        }, backfill={"kb_id": "", "stored_file": None})
        if migrated:
            # 回填不能靠常量：要从历史行的 meta JSON 里把值搬出来
            self._backfill_doc_columns_from_meta()

    def _backfill_doc_columns_from_meta(self) -> None:
        """把 documents.meta JSON 里的 kb_id / stored_file 搬进新列。

        迁移是幂等的：只处理新列为空、而 meta 里有值的行。
        搬完不清空 meta（旧版本回滚时仍可读），也不影响 chunking 结构。
        """
        import json as _json

        from .sql import escape_sql, fetch_rows
        # ↑ 原来是 `from .repos import …`，注释写的是「writer 反向依赖本模块，
        # 模块级导入会成环」。两处都是过期的：`writer` 这个模块从来没有存在过，
        # 而 escape_sql/fetch_rows 的家在 `sql.py`，repos 只是再导出它们 ——
        # 绕道再导出才制造了那条本不存在的「环」。

        try:
            rows = fetch_rows(self.documents.search().select(
                ["doc_id", "kb_id", "stored_file", "meta"]))
        except Exception as e:  # noqa: BLE001
            logger.warning("文档列回填读取失败，跳过: %s", e)
            return
        moved = 0
        for r in rows:
            try:
                meta = r.get("meta") or "{}"
                d = _json.loads(meta) if isinstance(meta, str) else (meta or {})
            except (TypeError, ValueError):
                continue
            values: dict = {}
            if not r.get("kb_id") and d.get("kb_id"):
                values["kb_id"] = str(d["kb_id"])
            if not r.get("stored_file") and d.get("stored_file"):
                values["stored_file"] = str(d["stored_file"])
            if not values:
                continue
            try:
                self.documents.update(
                    where=f"doc_id = '{escape_sql(r['doc_id'])}'",
                    values=values)
                moved += 1
            except Exception as e:  # noqa: BLE001
                logger.debug("回填 %s 失败: %s", r.get("doc_id"), e)
        if moved:
            logger.info("已把 %d 行文档的 kb_id/stored_file 从 meta 迁移到列", moved)

    def _ensure_cols(self, table_name: str, cols: dict,
                     backfill: dict | None = None) -> bool:
        """补齐缺失的列并回填默认值。返回是否发生了加列（供后续数据迁移判断）。"""
        try:
            tbl = self._table(table_name)
            existing = set(tbl.schema.names)
            missing = {k: t for k, t in cols.items() if k not in existing}
            if not missing:
                return False
            tbl.add_columns(pa.schema(
                [pa.field(k, t) for k, t in missing.items()]))
            logger.info("%s 表新增列: %s", table_name, list(missing))
            for col in missing:
                try:
                    tbl.update(where=f"{col} IS NULL",
                               values={col: (backfill or {}).get(col)})
                except Exception as e:  # noqa: BLE001
                    logger.debug("回填 %s.%s 失败: %s", table_name, col, e)
            return True
        except Exception as e:  # noqa: BLE001
            # 缺列会让所有 merge_insert 写路径 500，宁可启动失败也别
            # 带病运行——这里必须把错误抛出去，而不是降级成 warning。
            raise RuntimeError(
                f"{table_name} 表列迁移失败（{e}）。数据目录 "
                f"schema 落后于代码，请备份 data/ 后重试。") from e

    def vector_dim(self) -> int | None:
        """chunks 表向量列的实际维度（表不存在或无法识别时 None）。"""
        try:
            field = self.chunks.schema.field("vector")
            t = field.type
            if hasattr(t, "list_size"):
                return int(t.list_size)
            if hasattr(t, "value_type") and hasattr(
                    t.value_type, "list_size"):
                return int(t.value_type.list_size)
        except Exception as e:  # noqa: BLE001
            logger.debug("vector_dim 探测失败: %s", e)
        return None

    def _table(self, name: str):
        t = self._tables.get(name)
        if t is None:
            t = self.db.open_table(name)
            self._tables[name] = t
        return t

    @property
    def documents(self):
        return self._table("documents")

    @property
    def chunks(self):
        return self._table("chunks")

    @property
    def jobs(self):
        return self._table("jobs")

    @property
    def apikeys(self):
        return self._table("apikeys")

    def write_lock(self):
        """写入 / 索引维护的互斥锁：`with store.write_lock(): ...`

        本地后端下这是**跨进程**文件锁（可重入，语义与原来的 RLock 一致）；
        临界区的划分没有变化——慢 IO（解析、向量化）仍然必须在锁外，
        现在它卡住的不再只是一个线程，而是整台机器上所有进程的写路径。
        """
        return self._lock

    # ---- 索引维护 ----------------------------------------------------------
    @staticmethod
    def _is_commit_conflict(exc: BaseException) -> bool:
        """LanceDB 的乐观提交冲突（同一个版本被别的事务抢先提交）。

        多进程形态下这**是会被触发成真**的：启动期 4 个 worker 可能同时判断
        「该建 FTS 索引」，flock 让它们排队，但排队不等于事情没被别人做过 ——
        后到的那几个会撞 `Retryable commit conflict ... preempted by
        concurrent transaction CreateIndex`。改前是单进程，这条路径不会发生。
        """
        text = str(exc)
        return "commit conflict" in text or "preempted by concurrent" in text

    def _refresh(self, table_name: str) -> None:
        """提交冲突后重新读表版本，否则重试还是打在旧快照上。"""
        try:
            self._tables.pop(table_name, None)
            self._table(table_name).checkout_latest()
        except Exception as e:  # noqa: BLE001
            logger.debug("refresh %s 版本失败: %s", table_name, e)

    def _index_state(self, column: str, table_name: str = "chunks") -> tuple[bool, int]:
        """返回 (是否存在覆盖该列的索引, 未索引行数)。

        兼容 lancedb 各版本 list_indices() 的返回形态
        （Index 对象或 dict）。
        """
        try:
            tbl = self._table(table_name)
            for idx in tbl.list_indices():
                if isinstance(idx, dict):
                    cols = idx.get("columns") or []
                    if column in cols:
                        return True, int(
                            idx.get("num_unindexed_rows", 0) or 0)
                else:
                    cols = getattr(idx, "columns", None) or []
                    if column in cols:
                        return True, int(
                            getattr(idx, "num_unindexed_rows", 0) or 0)
        except Exception as e:  # noqa: BLE001
            logger.debug("list_indices 失败: %s", e)
        return False, 0

    def _vector_needs_rebuild(self, n: int) -> bool:
        exists, unindexed = self._index_state("vector")
        if not exists:
            return n >= MIN_VECTOR_INDEX_ROWS
        return unindexed >= max(MIN_VECTOR_INDEX_ROWS, int(n * REBUILD_UNINDEXED_RATIO))

    def _vector_index_config(self, n: int):
        """按规模选择索引配置（DESIGN §5.3，统一 API）。

        返回 (config, 类型名)。小库 HNSWFlat，中库 IVF_HNSW_SQ，
        百万级以上 IVF_PQ 省内存（配合 refine_factor 补召回）。
        """
        num_partitions = max(2, min(256, n // 256))
        n_sub = next((c for c in (64, 32, 16, 8, 4, 2, 1)
                        if self.dim % c == 0), 1)
        if n <= HNSW_MAX_ROWS:
            return HnswFlat(distance_type="cosine"), "HNSW"
        if n <= IVF_HNSW_SQ_MAX_ROWS:
            return IvfHnswSq(distance_type="cosine",
                             num_partitions=num_partitions), "IVF_HNSW_SQ"
        return IvfPq(distance_type="cosine",
                     num_partitions=num_partitions,
                     num_sub_vectors=n_sub), "IVF_PQ"

    def ensure_vector_index(self, force: bool = False) -> None:
        tbl = self.chunks
        try:
            with self._lock:
                n = tbl.count_rows()
                if n < MIN_VECTOR_INDEX_ROWS:
                    return  # 数据过少，暴力扫描即可，避免分区数不合理
                if not force and not self._vector_needs_rebuild(n):
                    return  # 索引已覆盖绝大多数行，跳过全量重建
                config, type_name = self._vector_index_config(n)
                tbl.create_index("vector", config=config)
                logger.info("vector index rebuilt (n=%d, %s)", n, type_name)
        except Exception as e:  # noqa: BLE001
            logger.warning("vector index build skipped: %s", e)

    def _create_fts_index_locked(self, tbl) -> None:
        """建 FTS 索引，对**乐观提交冲突**重试。

        多进程下这不再是理论问题：启动期 4 个 worker 会同时判断「该重建 FTS」，
        跨进程写锁让它们排队，但排队不等于别人没干过 —— 后到的会撞
        `Retryable commit conflict ... preempted by concurrent transaction
        CreateIndex`。改前是单进程，这条路径根本不会走到。
        重试前先 checkout_latest：不然还是打在旧快照上，一定再冲突。
        """
        last: Exception | None = None
        for attempt in range(3):
            try:
                tbl.create_index("text_seg", config=FTS())
                return
            except Exception as e:  # noqa: BLE001
                last = e
                if not self._is_commit_conflict(e):
                    raise
                logger.info("FTS 索引提交与并发写入撞车（第 %d 次），重读版本后重试",
                            attempt + 1)
                self._refresh("chunks")
        raise last if last else RuntimeError("FTS 索引创建失败")

    def ensure_fts_index(self, force: bool = False) -> None:
        """重建全文索引（tantivy），带**积压阈值 + 最小间隔**双重节流。

        改前的策略是「行数 < 50000 就每次入库都全量重建」——听起来无害，
        实际是每次重建都全表扫一遍 chunks 并在表上留下新版本，
        高频入库时版本目录无界增长（诊断报告 §2.6）。
        现在：小库仍保证「编辑/新增立即能搜到」，但两次重建之间至少间隔
        `FTS_MIN_INTERVAL_SECONDS`，除非积压行数已经大到不能再拖。
        """
        tbl = self.chunks
        try:
            with self._lock:
                n = tbl.count_rows()
                if not force:
                    exists, unindexed = self._index_state("text_seg")
                    if exists:
                        backlog = max(FTS_BACKLOG_ROWS,
                                      int(n * REBUILD_UNINDEXED_RATIO))
                        if unindexed < backlog and not self._fts_interval_elapsed():
                            return
                self._create_fts_index_locked(tbl)
                self._last_fts_at = time.monotonic()
                # 重建后清除 stale 标记（fts_stale 语义：编辑后未重建 FTS 的行数）
                try:
                    self.chunks.update(
                        where="fts_stale = true", values={"fts_stale": False})
                except Exception as e:  # noqa: BLE001
                    # warning：标记清不掉，`/api/health` 就会**永远**显示
                    # 「有 N 条待重建 FTS」，用户对着一个不会自己好的黄徽章。
                    logger.warning("清除 fts_stale 标记失败（健康检查会一直显示待重建）: %s", e)
                logger.info("fts index rebuilt (n=%d)", n)
        except Exception as e:  # noqa: BLE001
            logger.warning("fts index build skipped: %s", e)

    def _fts_interval_elapsed(self) -> bool:
        """距离上一次 FTS 重建是否已超过最小间隔（首次总是 True）。"""
        if self._last_fts_at is None:
            return True
        return (time.monotonic() - self._last_fts_at) >= FTS_MIN_INTERVAL_SECONDS

    def ensure_scalar_indexes(self, force: bool = False) -> None:
        try:
            with self._lock:
                # 注意：不要索引 chunks.status —— 该列不存在于 schema，
                # 旧代码写在这里会让整轮标量索引静默失败。
                for col in ("doc_id", "origin", "kb_id", "chunk_id"):
                    exists, _ = self._index_state(col)
                    if exists and not force:
                        continue
                    try:
                        self.chunks.create_index(col, config=BTree())
                    except Exception as e:  # noqa: BLE001
                        # warning：这几列没索引就等于**全表扫描**。上面那行注释
                        # 自己就在说「旧代码写在这里会让整轮标量索引静默失败」，
                        # 而 debug 级在默认日志下等于继续静默。
                        logger.warning("chunks.%s 索引没能建立，点查将退化为全表扫: %s",
                                       col, e)
                # 点查列必须有索引。缺一张就退化成全表扫：实测
                # documents.doc_id 无索引时点查 5.68ms，建完 BTree 3.88ms
                # （剩下的固定开销是 Lance 查询侧的，见诊断报告 §2.3——
                #  这条治不了，要靠 storage/sql.scalar_rows 的原生投影）。
                for table_name, col in (("documents", "doc_id"),
                                        ("documents", "content_hash"),
                                        ("kbs", "kb_id"),
                                        ("jobs", "job_id"),
                                        # 任务列表按「开始时间倒序」取最近 N 条
                                        ("jobs", "started_at"),
                                        # 每次带密钥的请求都要按哈希查一次，必须走索引，
                                        # 否则密钥表一大就退化成全表扫描
                                        ("apikeys", "key_hash")):
                    exists, _ = self._index_state(col, table_name)
                    if exists and not force:
                        continue
                    try:
                        self._table(table_name).create_index(col, config=BTree())
                    except Exception as e:  # noqa: BLE001
                        # 同上：apikeys.key_hash 没索引 ⇒ 每个带密钥的请求都全表扫；
                        # jobs.started_at 没索引 ⇒ 任务列表每次排序扫描。
                        logger.warning(
                            "%s.%s 索引没能建立，相关点查/排序将退化为全表扫: %s",
                            table_name, col, e)
        except Exception as e:  # noqa: BLE001
            logger.warning("scalar index build skipped: %s", e)

    def optimize(self, older_than_hours: int = 24) -> None:
        try:
            with self._lock:
                # 新版 optimize 同时处理 compaction 与旧版本清理
                self.chunks.optimize(
                    cleanup_older_than=timedelta(
                        hours=older_than_hours))
        except Exception as e:  # noqa: BLE001
            logger.warning("optimize skipped: %s", e)

    def _version_count(self, table_name: str) -> int:
        try:
            return len(self._table(table_name).list_versions())
        except Exception:  # noqa: BLE001
            return 0

    def compact_tables(self, table_names=("chunks", "documents", "jobs",
                                          "kbs", "apikeys")) -> dict:
        """碎片合并 + 旧版本回收，按「版本数超阈值 或 距上次超时」触发。

        这条是「越用越慢」的根治手段之一（诊断报告 §2.6）：
        - 一次入库过去会在 `jobs` 表上留下 ≈5 个版本/文件；
        - `list_versions()` 实测 ≈0.17ms/版本，而 `/api/health` 与
          `/api/stats` 都要调它——379 个版本就是 64ms 的纯开销。

        只在后台维护线程里跑；`delete_unverified` 保持默认 False
        （未提交的事务不能被删）。清理下界 `OPTIMIZE_KEEP_OLDER_THAN`
        必须远大于最长请求时长，否则会回收掉仍在被读的快照。
        """
        now = time.monotonic()
        out: dict = {}
        for name in table_names:
            try:
                versions = self._version_count(name)
            except Exception:  # noqa: BLE001
                continue
            need = (versions > COMPACT_VERSION_THRESHOLD
                    or (self._last_compact_at is not None
                        and (now - self._last_compact_at)
                        > COMPACT_DEBOUNCE_SECONDS))
            if not need:
                out[name] = {"versions": versions, "compacted": False}
                continue
            try:
                with self._lock:
                    self._table(name).optimize(
                        cleanup_older_than=OPTIMIZE_KEEP_OLDER_THAN)
                out[name] = {"versions": versions, "compacted": True,
                             "versions_after": self._version_count(name)}
            except Exception as e:  # noqa: BLE001
                logger.debug("compact %s skipped: %s", name, e)
                out[name] = {"versions": versions, "compacted": False,
                             "error": str(e)[:120]}
        if out:
            self._last_compact_at = now
        return out

    # ---- 版本管理 ----------------------------------------------------------
    def table_versions(self, table_name: str = "chunks") -> list[dict]:
        """列出表的历史版本（回滚用）。"""
        try:
            raw = self._table(table_name).list_versions()
            return [dict(v) if not isinstance(v, dict) else v
                    for v in raw]
        except Exception as e:  # noqa: BLE001
            logger.warning("list_versions() 失败: %s", e)
            return []

    def restore_version(self, version: int, table_name: str = "chunks") -> None:
        """把表回滚到指定版本（chunks 表回滚后 documents 表需对账/重放）。"""
        with self._lock:
            self._table(table_name).restore(version)

    # ---- 容量统计 ----------------------------------------------------------
    def stats(self) -> dict:
        """容量/索引/版本统计（/api/stats）。

        存储维度按后端给不同指标——本地给磁盘占用，对象存储给对象数，
        两者没有可比性，硬凑成同一组字段只会误导。
        """
        # 与 /api/health 同一个读入口：否则两个端点会给出两个不同的文档数
        # （元数据真源在 SQLite 时，直接数 Lance 旧表会少数）。
        # docs_count / list_jobs 已提到模块级导入 —— 这条边不是环：
        # repos 各模块只在注解里用 LanceStore（都在 `if TYPE_CHECKING` 下），
        # 由 tests/test_import_order.py 的双向探针盯着这一点。
        out = {
            "doc_count": docs_count(self),
            "chunk_count": self.chunks.count_rows(),
        }
        try:
            _rows, out["job_count"] = _list_jobs(self, limit=1)
        except Exception:  # noqa: BLE001
            out["job_count"] = self.jobs.count_rows()
        try:
            out.update(self.backend.stats())
        except Exception as e:  # noqa: BLE001
            logger.debug("后端容量统计失败: %s", e)
            out["backend"] = getattr(self.backend, "kind", "?")
        indexes = {}
        for name in ("chunks", "documents"):
            try:
                entries = []
                for i in self._table(name).list_indices():
                    if isinstance(i, dict):
                        entries.append({
                            "columns": i.get("columns"),
                            "type": i.get("index_type") or i.get("type"),
                        })
                    else:
                        entries.append({
                            "columns": getattr(i, "columns", None),
                            "type": getattr(i, "index_type",
                                            getattr(i, "type", None)),
                        })
                indexes[name] = entries
            except Exception:  # noqa: BLE001
                indexes[name] = []
        out["indexes"] = indexes
        out["versions"] = len(self.table_versions())
        return out

    # ---- 后台维护调度 ------------------------------------------------------
    def schedule_maintenance(self, debounce_seconds: float = 2.0,
                             force_fts: bool = False) -> None:
        """请求一次后台索引维护（标量/向量/FTS + 碎片与版本治理）。

        去抖合并：已有任务 pending 则跳过；维护在单后台线程执行，不阻塞入库响应。

        `force_fts=True` 给「用户可感知的编辑」用（改/增/停用分块）——
        这些操作必须**立刻**能搜到，不能等最小间隔；普通入库走节流路径。
        """
        with self._maint_lock:
            if self._maint_pending:
                return
            self._maint_pending = True
        do_force = force_fts

        def _worker():
            try:
                threading.Event().wait(debounce_seconds)  # 去抖窗口
                self.ensure_scalar_indexes()
                self.ensure_vector_index()
                self.ensure_fts_index(force=do_force)
                # 碎片合并 + 旧版本回收：按阈值触发，不在普通读路径上花成本
                self.compact_tables()
            except Exception as e:  # noqa: BLE001
                logger.warning("background maintenance failed: %s", e)
            finally:
                with self._maint_lock:
                    self._maint_pending = False

        self._maint_thread = threading.Thread(
            target=_worker, name="raggi-maint", daemon=True)
        self._maint_thread.start()
