"""元数据引擎：SQLite(WAL)。诊断报告 R1 的正解。

为什么是 SQLite 而不是「把 LanceDB 调快」
-----------------------------------------
本项目的四张 OLTP 表（`jobs` / `apikeys` / `kbs` / `documents`）画像是
「点查 + 高频小写 + 事务性状态机」，而 LanceDB 的画像是「列存 + 批写 +
版本快照」。把前者放进后者里，实测代价是（同一台机器）：

| 操作 | LanceDB | SQLite |
|---|---|---|
| 点查 doc_id | 5.68ms（无索引）/ 3.88ms（有 BTree） | **0.008ms** |
| 列表 filter+order+limit | 6.76ms（且要 materialize 全表） | **0.100ms** |
| count with filter | 1.19ms | 0.030ms |
| 一次小写的副作用 | **一个新版本 + 一个新数据文件** | 一个 WAL 页追加 |

最后一行才是关键：LanceDB 的每一次小写都会在表上留下碎片与版本，
于是「用得越久越慢」。实测一次入库给 `jobs` 表带来 ≈5 个版本/文件，
48 次入库后同一份数据的读延迟从 13.4ms 涨到 30.7ms
（docs/PERF-CONCURRENCY-2026-10-05.md §2.6）。

仍然只服务一个数据目录、零外部进程
----------------------------------
`sqlite3` 是 Python 标准库，文件就在 `data/raggi.db`（+ `-wal`/`-shm`），
拷走 `data/` 仍然等于备份全部 —— 「极简单机」的定位没有被打断。

并发形态
--------
WAL 允许「多读单写」：读者不阻塞写者，写者之间由 SQLite 自己的锁串行化，
`busy_timeout` 让撞锁的一方等待而不是立刻报错。连接**按线程隔离**
（`sqlite3` 的连接不是线程安全的，`check_same_thread=False` 只是关掉检查、
不等于安全）。

迁移与回退
----------
`storage.meta_engine = auto | sqlite | lancedb`：
- `auto`（默认）：SQLite 为空时从 LanceDB 的旧表**一次性导入**，之后以 SQLite 为准；
- `lancedb`：完全退回改造前的读写路径（回退开关，不需要改代码）。
LanceDB 里的旧表**保留不动**，所以回退不需要恢复备份。
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from rag.core.errors import Invalid

logger = logging.getLogger("raggi.meta")

SCHEMA_VERSION = 1

# DDL 里刻意不写 FOREIGN KEY：级联规则由仓储层的事务负责（要同时改
# LanceDB 的 chunks，数据库约束管不到它），留着只会给人一种「约束会兜住」的错觉。
DDL = """
CREATE TABLE IF NOT EXISTS meta_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kbs (
    kb_id         TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    chunk_size    INTEGER NOT NULL DEFAULT 0,
    overlap_ratio REAL NOT NULL DEFAULT 0.0,
    created_at    TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_kbs_updated ON kbs(updated_at DESC, kb_id);

CREATE TABLE IF NOT EXISTS documents (
    doc_id        TEXT PRIMARY KEY,
    title         TEXT NOT NULL DEFAULT '',
    source_uri    TEXT,
    mime          TEXT NOT NULL DEFAULT 'text/plain',
    parser_engine TEXT NOT NULL DEFAULT 'native',
    content_hash  TEXT NOT NULL DEFAULT '',
    char_count    INTEGER NOT NULL DEFAULT 0,
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'indexing',
    error         TEXT,
    kb_id         TEXT NOT NULL DEFAULT '',
    stored_file   TEXT,
    meta          TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT '',
    text          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_docs_kb ON documents(kb_id, created_at DESC, doc_id);
CREATE INDEX IF NOT EXISTS idx_docs_hash ON documents(content_hash);
CREATE INDEX IF NOT EXISTS idx_docs_status ON documents(status);

CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    doc_id        TEXT NOT NULL DEFAULT '',
    kb_id         TEXT NOT NULL DEFAULT '',
    stage         TEXT NOT NULL DEFAULT 'pending',
    progress      REAL NOT NULL DEFAULT 0.0,
    total         INTEGER NOT NULL DEFAULT 0,
    parser_engine TEXT NOT NULL DEFAULT '',
    error         TEXT,
    started_at    TEXT NOT NULL DEFAULT '',
    ended_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_started ON jobs(started_at DESC, job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_doc ON jobs(doc_id);

CREATE TABLE IF NOT EXISTS apikeys (
    key_id       TEXT PRIMARY KEY,
    name         TEXT NOT NULL DEFAULT '',
    prefix       TEXT NOT NULL DEFAULT '',
    key_hash     TEXT NOT NULL DEFAULT '',
    scope        TEXT NOT NULL DEFAULT 'read',
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT '',
    last_used_at TEXT,
    expires_at   TEXT NOT NULL DEFAULT '',
    revoked_at   TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_key_hash ON apikeys(key_hash);

-- 这段 DDL 只有「建」没有「删」。它曾预留过 `idempotency` / `outbox` /
-- `jobs.worker` 三处**当时没有调用点**的结构（幂等键最终落在进程内
-- `IdempotencyStore`，跨引擎级联最终靠启动期对账，任务领取从未实现）。
-- 现已从 DDL 删除，但**故意不对存量库 DROP**：启动期删表是数据丢失面，
-- 而它们要么 0 行要么 1 列，留着只难看、不影响读写。新建的库不再有此负担。
"""

# 列清单归 schema 所在层：这四份清单**就是上面 DDL 的列序**，
# 仓储层（`repos/*.py`）从这里 import，而不是各写一份。
# 之前 jobs 的列清单在 meta.py 与 repos/jobs.py 各有一份，两处靠人肉同步。
#
# 注意 `DOC_COLS` **必须含 `text`**。这份清单曾经漏掉它，于是自动迁移把
# 每篇文档的正文写成空串，而「行数一致」的校验照样通过 —— 用户看到的是
# 文档还在、字数还有 495，但打开就是空白，且重新切分会报「该文档无正文」。
# 校验因此也升级成逐列摘要比对（见 import_from_lance）。
DOC_COLS = ["doc_id", "title", "source_uri", "mime", "parser_engine",
            "content_hash", "char_count", "chunk_count", "text", "status",
            "error", "kb_id", "stored_file", "meta", "created_at",
            "updated_at"]
JOB_COLS = ["job_id", "doc_id", "kb_id", "stage", "progress", "total",
            "parser_engine", "error", "started_at", "ended_at"]
KB_COLS = ["kb_id", "name", "description", "chunk_size", "overlap_ratio",
           "created_at", "updated_at"]
KEY_COLS = ["key_id", "name", "prefix", "key_hash", "scope", "note",
            "created_at", "last_used_at", "expires_at", "revoked_at"]


class MetaStore:
    """SQLite 元数据引擎。连接按线程隔离。"""

    def __init__(self, path: Path | str, *, busy_timeout_ms: int = 5000):
        self.path = Path(path)
        self.busy_timeout_ms = int(busy_timeout_ms)
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._cols: dict[str, set[str]] = {}
        self._defaults: dict[str, dict] = {}
        self._ready = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._bootstrap()

    # ---- 连接 ----------------------------------------------------------
    def _bootstrap(self) -> None:
        with self._init_lock:
            conn = self._conn()
            conn.executescript(DDL)
            row = conn.execute(
                "SELECT value FROM meta_state WHERE key='schema_version'").\
                fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO meta_state(key, value) VALUES(?,?)",
                    ("schema_version", str(SCHEMA_VERSION)))
            elif int(row[0] or 0) > SCHEMA_VERSION:
                raise Invalid(
                    f"data 目录的元数据 schema 版本 {row[0]} 高于当前代码支持的 "
                    f"{SCHEMA_VERSION}：请用与数据匹配的 Raggi 版本启动，"
                    "或从备份恢复。降级运行会静默改坏数据。")
            self._ready = True

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(str(self.path), timeout=self.busy_timeout_ms
                                / 1000.0, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
            c.execute("PRAGMA journal_mode=WAL")
            # NORMAL 而不是 FULL：WAL 下 NORMAL 仍然保证已提交事务不丢，
            # 只把 fsync 推迟到 checkpoint —— 这正是我们要的写入延迟下降。
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA temp_store=MEMORY")
            self._local.conn = c
        return c

    @property
    def db(self) -> sqlite3.Connection:
        return self._conn()

    # ---- 基础读写 ------------------------------------------------------
    def query(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        return [dict(r) for r in self._conn().execute(sql, params).fetchall()]

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        r = self._conn().execute(sql, params).fetchone()
        return dict(r) if r is not None else None

    def value(self, sql: str, params: tuple | dict = (), default=None):
        r = self._conn().execute(sql, params).fetchone()
        if r is None:
            return default
        return r[0]

    def execute(self, sql: str, params: tuple | dict = ()):
        return self._conn().execute(sql, params)

    def executemany(self, sql: str, seq) -> None:
        self._conn().executemany(sql, list(seq))

    def not_null_defaults(self, table: str) -> dict:
        """`NOT NULL` 且带 DEFAULT 的列 → 默认值（求值一次后缓存）。

        为什么必须有：SQLite 的列默认值只在**该列被省略**时生效；显式传
        NULL 不触发默认值，而是直接撞 `NOT NULL` 约束。仓储层的写法是
        「按列清单从字典里取」（`{k: r.get(k) for k in DETAIL_COLS}`），
        缺列天然就是 None。LanceDB 那边有 `fill_missing` 兜住，SQLite
        这边不兜就会出现「同一份写入在一条引擎上成功、另一条上报错」——
        而这正是双引擎分派最贵的缺陷类型。
        """
        cached = self._defaults.get(table)
        if cached is not None:
            return cached
        out: dict = {}
        try:
            info = self.query(f"PRAGMA table_info({table})")
        except sqlite3.Error:
            info = []
        for col in info:
            if not col.get("notnull") or col.get("dflt_value") is None:
                continue
            expr = str(col["dflt_value"])
            # 只接受字面量型默认值（`''` / `0` / `0.0` / `'native'`）；
            # 表达式型默认值求值可能改变状态，宁可不管。
            if re.fullmatch(r"[+-]?(\d+(\.\d*)?|\.\d+|'[^']*')", expr):
                try:
                    out[str(col["name"])] = self.value(f"SELECT {expr}")
                except sqlite3.Error:
                    continue
        if out:
            self._defaults[table] = out
        return out

    def upsert(self, table: str, rows: list[dict], pk: str) -> None:
        """按主键幂等写入（`INSERT … ON CONFLICT DO UPDATE`）。

        列以**第一行的键**为准，缺列填 None —— 与 LanceDB 那边
        「按 schema 补齐缺列」的纪律一致，调用点不必记得写全所有列。
        唯一的例外是 `NOT NULL DEFAULT …` 的列：那里 None 会换成默认值，
        理由见 `not_null_defaults`。
        """
        if not rows:
            return
        cols = list(rows[0].keys())
        defaults = self.not_null_defaults(table)
        for r in rows:
            for c in cols:
                if r.get(c) is None and c in defaults:
                    r[c] = defaults[c]
                else:
                    r.setdefault(c, None)
        placeholders = ", ".join("?" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != pk)
        sql = (f"INSERT INTO {table} ({', '.join(cols)}) "
               f"VALUES ({placeholders}) "
               f"ON CONFLICT({pk}) DO UPDATE SET {updates}")
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(sql, [tuple(r[c] for c in cols) for r in rows])
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def tx(self):
        """`with meta.tx():` —— BEGIN IMMEDIATE / COMMIT / ROLLBACK。

        IMMEDIATE 而不是默认 DEFERRED：后者在「先读后写」的事务里会在
        升级写锁时才发现冲突并直接 SQLITE_BUSY，而我们要的是
        「排队等 busy_timeout」。
        """
        return _Tx(self._conn())

    # ---- 计数与状态 ----------------------------------------------------
    def columns(self, table: str) -> set[str]:
        """表的真实列名（按表缓存）。给仓储层做投影白名单用。"""
        cached = self._cols.get(table)
        if cached is not None:
            return cached
        try:
            names = {str(r["name"]) for r in
                     self.query(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            names = set()
        if names:
            self._cols[table] = names
        return names

    def count(self, table: str, where: str | None = None,
              params: tuple = ()) -> int:
        sql = f"SELECT count(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return int(self.value(sql, params, 0) or 0)

    def get_state(self, key: str, default: str = "") -> str:
        return str(self.value(
            "SELECT value FROM meta_state WHERE key=?", (key,), default)
            or default)

    def set_state(self, key: str, value: str) -> None:
        self._conn().execute(
            "INSERT INTO meta_state(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))

    def stats(self) -> dict:
        out = {}
        for t in ("documents", "jobs", "kbs", "apikeys"):
            try:
                out[t] = self.count(t)
            except sqlite3.Error:
                out[t] = -1
        try:
            out["integrity"] = str(self.value("PRAGMA integrity_check"))
        except sqlite3.Error:
            out["integrity"] = "unknown"
        out["journal_mode"] = str(self.value("PRAGMA journal_mode"))
        out["file"] = str(self.path)
        return out


class _Tx:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._depth = 0

    def __enter__(self):
        if self._depth == 0:
            self._conn.execute("BEGIN IMMEDIATE")
        self._depth += 1
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        self._depth -= 1
        if self._depth > 0:
            return False
        if exc_type is None:
            self._conn.execute("COMMIT")
        else:
            self._conn.execute("ROLLBACK")
        return False


def open_meta_store(settings, *, force_disabled: bool = False) -> MetaStore | None:
    """按配置打开元数据引擎；`meta_engine=lancedb` 时返回 None（退回旧路径）。"""
    engine = (getattr(getattr(settings, "storage", None), "meta_engine",
                      "auto") or "auto").lower()
    if force_disabled or engine == "lancedb":
        return None
    return MetaStore(Path(settings.data_dir) / "raggi.db")


def needs_import(meta: MetaStore) -> bool:
    """SQLite 侧一片空白 → 该从 LanceDB 旧表导入一次。"""
    try:
        total = sum(meta.count(t) for t in
                    ("documents", "jobs", "kbs", "apikeys"))
    except sqlite3.Error:
        return False
    return total == 0


def prepare_meta_store(settings, lance_store, *, log=logger):
    """启动期装配元数据引擎，返回 `MetaStore | None`。

    返回 None 的两种情况都必须是**安全**的：
    - `meta_engine=lancedb`：用户显式回退，走改造前的读写路径；
    - 导入失败：宁可退回旧路径也不带着「一半在 SQLite、一半在 LanceDB」
      的分裂状态启动 —— 那种状态比拒绝启动更难查，而且它会静默改坏数据。

    auto 模式下如果 SQLite 侧是空的，就从 LanceDB 旧表一次性导入。
    这条静默导入是刻意选的：单机工具的升级路径必须是
    「换个版本重启就能用」，而不是「先照 README 跑一次迁移脚本」。
    """
    engine = (getattr(getattr(settings, "storage", None), "meta_engine",
                      "auto") or "auto").lower()
    if engine == "lancedb":
        log.info("元数据引擎：lancedb（回退路径，OLTP 仍在 LanceDB 上）")
        return None
    try:
        meta = open_meta_store(settings)
        if meta is not None and needs_import(meta):
            report = import_from_lance(meta, lance_store)
            log.info("元数据已从 LanceDB 导入 SQLite：%s", report)
        return meta
    except Exception as e:  # noqa: BLE001
        log.error("元数据引擎初始化失败，退回 LanceDB 读写路径（功能不受影响，"
                  "但 jobs/apikeys 的写放大仍在）：%s", e)
        return None


def _digest(rows: list[dict], cols: list[str]) -> str:
    """逐列摘要。用来判断「搬过去的还是不是原来那份数据」。

    为什么必须摘要而不是数行数：本项目真实的迁移事故就是**列清单漏了一个
    `text`** —— 行数、主键集合完全一致，`count` 类校验全绿，而每篇文档的
    正文都被写成了空串。
    """
    import hashlib

    h = hashlib.sha256()
    for r in rows:
        for c in cols:
            v = r.get(c)
            h.update(f"{c}={'' if v is None else v}\x1f".encode("utf-8"))
        h.update(b"\x1e")
    return h.hexdigest()[:16]


def import_from_lance(meta: MetaStore, lance_store) -> dict:
    """一次性把旧 LanceDB 的 OLTP 表搬进 SQLite（幂等、可重跑、逐列校验）。

    为什么允许静默导入而不是强制 `rag migrate`：单机工具的升级路径
    必须是「换个版本重启就能用」。同时这里**只读不删** LanceDB 的旧表，
    所以 `meta_engine=lancedb` 随时可以退回，导入失败也不会丢数据。

    校验做三件事，任何一项不过都直接抛错（拒绝启用 SQLite）：
    行数、主键集合、逐列摘要。第三条是事故之后加的。
    """
    from rag.storage.sql import scalar_rows

    report: dict[str, Any] = {}
    specs = (
        ("kbs", "kbs", KB_COLS, lance_store.kbs, "kb_id"),
        ("apikeys", "apikeys", KEY_COLS, lance_store.apikeys, "key_id"),
        ("documents", "documents", DOC_COLS, lance_store.documents, "doc_id"),
        ("jobs", "jobs", JOB_COLS, lance_store.jobs, "job_id"),
    )
    for name, table, cols, src, pk in specs:
        try:
            if meta.count(table) > 0:
                report[name] = "skipped（已有数据）"
                continue
            rows = scalar_rows(src, cols=cols)
            cleaned = []
            for r in rows:
                row = {}
                for c in cols:
                    v = r.get(c)
                    if isinstance(v, (bytes, bytearray)):
                        v = v.decode("utf-8", "replace")
                    elif isinstance(v, float) and v != v:   # NaN/None 归一
                        v = None
                    row[c] = v
                cleaned.append(row)
            if cleaned:
                meta.upsert(table, cleaned, pk)
            after_rows = meta.query(
                f"SELECT {', '.join(cols)} FROM {table}"
                f" ORDER BY {pk}")
            after = len(after_rows)
            src_digest = _digest(
                sorted(cleaned, key=lambda r: str(r.get(pk))), cols)
            dst_digest = _digest(after_rows, cols)
            ok = after == len(rows) and src_digest == dst_digest
            report[name] = {"imported": after, "source": len(rows),
                            "digest_ok": src_digest == dst_digest, "ok": ok}
            if not ok:
                raise Invalid(
                    f"{name} 导入校验失败：源 {len(rows)} 行 / 目标 {after} 行，"
                    f"列摘要 {'一致' if src_digest == dst_digest else '不一致' }"
                    f"（{src_digest} vs {dst_digest}）")
        except Invalid:
            raise
        except Exception as e:  # noqa: BLE001
            report[name] = f"失败：{type(e).__name__}: {str(e)[:120]}"
            raise Invalid(
                f"从 LanceDB 导入 {name} 失败（{e}）。数据目录 "
                f"{meta.path} 保持未启用状态；可先用 "
                "storage.meta_engine=lancedb 退回旧路径再排查。") from e
    meta.set_state("imported_from_lance_at", str(time.time()))
    logger.info("元数据已从 LanceDB 导入 SQLite：%s", report)
    return report
