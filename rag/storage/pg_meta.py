"""Postgres 元数据引擎（多副本/跨机器形态）。

与 `MetaStore`（SQLite）实现**同一套方法**：`query/query_one/value/execute/
executemany/upsert/tx/columns/count/get_state/set_state/state_int/bump_state/
not_null_defaults`。仓储层通过 `getattr(store, "meta", None)` 拿到的就是
其中之一，不需要知道后端是哪个。

方言差异只有三处，全部收敛在本模块：

1. **占位符** `?` → `%s`（扫描时跳过字符串字面量，不碰 `ESCAPE '\\'`）；
2. **LIKE** → `ILIKE`：SQLite 的 `LIKE` 对 ASCII 大小写不敏感，Postgres 的
   `LIKE` 敏感；不改就会让「按标题搜索」在两个后端给出不同结果；
3. **类型**：`REAL` → `DOUBLE PRECISION`（PG 的 real 是 4 字节，时间戳
   和 overlap_ratio 会掉精度）。

连接按线程隔离（psycopg 连接不是线程安全的），与 SQLite 版一致。
"""
from __future__ import annotations

import ast
import logging
import re
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from rag.core.errors import Invalid

from .meta import (  # 列清单与 schema 版本只维护一份
    DOC_COLS,
    JOB_COLS,
    KB_COLS,
    KEY_COLS,
    SCHEMA_VERSION,
)

logger = logging.getLogger("raggi.meta.pg")

PG_DDL = """
CREATE TABLE IF NOT EXISTS meta_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kbs (
    kb_id         TEXT PRIMARY KEY,
    name          TEXT NOT NULL DEFAULT '',
    description   TEXT NOT NULL DEFAULT '',
    chunk_size    INTEGER NOT NULL DEFAULT 0,
    overlap_ratio DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    strategy      TEXT NOT NULL DEFAULT '',
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
    progress      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
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

CREATE TABLE IF NOT EXISTS rate_buckets (
    caller  TEXT PRIMARY KEY,
    tokens  DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    updated DOUBLE PRECISION NOT NULL DEFAULT 0.0
);
CREATE INDEX IF NOT EXISTS idx_rate_updated ON rate_buckets(updated);

CREATE TABLE IF NOT EXISTS idempotency (
    path    TEXT NOT NULL,
    key     TEXT NOT NULL,
    state   TEXT NOT NULL DEFAULT 'pending',
    owner   TEXT NOT NULL DEFAULT '',
    result  TEXT,
    error   TEXT,
    created DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    updated DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    PRIMARY KEY (path, key)
);
CREATE INDEX IF NOT EXISTS idx_idem_updated ON idempotency(updated);
"""

# 列清单（供迁移与排障使用，与 MetaStore 共用同一份）
TABLES = {
    "documents": DOC_COLS,
    "jobs": JOB_COLS,
    "kbs": KB_COLS,
    "apikeys": KEY_COLS,
}


def _redact(url: str) -> str:
    """日志里不带密码。"""
    return re.sub(r"(://[^:/@]+:)[^@]+(@)", r"\1***\2", url)


def translate_sql(sql: str) -> str:
    """SQLite 方言 → Postgres：占位符、LIKE、字面量百分号。

    只做这三件事，其余 SQL 文本保持原样——这一点很重要：仓储层的 SQL
    是唯一事实，适配器不应顺手改写语义。
    """
    out: list[str] = []
    in_str = False
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == "'":
            # 处理 '' 转义：连续两个单引号仍在字符串内
            out.append(ch)
            if in_str and i + 1 < len(sql) and sql[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            in_str = not in_str
            i += 1
            continue
        if in_str:
            out.append(ch)
            i += 1
            continue
        if ch == "?":
            out.append("%s")
        elif ch == "%":
            out.append("%%")
        else:
            out.append(ch)
        i += 1
    text = "".join(out)
    # SQLite 的 LIKE 对 ASCII 大小写不敏感；PG 用 ILIKE 对齐
    return re.sub(r"\bLIKE\b", "ILIKE", text)


class _PgConn:
    """把 psycopg 连接包装成 SQLite 连接那样的 `execute` 接口。"""

    def __init__(self, store: "PostgresMetaStore", conn):
        self._store = store
        self._conn = conn

    def execute(self, sql: str, params: tuple | dict = ()):
        cur = self._conn.cursor()
        cur.execute(translate_sql(sql), params)
        return cur

    def executemany(self, sql: str, seq) -> None:
        cur = self._conn.cursor()
        cur.executemany(translate_sql(sql), list(seq))


class PostgresMetaStore:
    """Postgres 元数据引擎（与 MetaStore 同接口）。"""

    def __init__(self, url: str, *, connect_timeout_s: float = 5.0):
        if not url:
            raise Invalid("storage.postgres_url 为空")
        try:
            import psycopg  # noqa: F401  可选依赖
        except ImportError as e:  # pragma: no cover - 取决于安装形态
            raise Invalid(
                "未安装 psycopg（pip install 'raggi[postgres]'）") from e
        self._psycopg = psycopg
        self.url = url
        self.path = _redact(url)
        self._connect_timeout = max(0.5, float(connect_timeout_s))
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._cols: dict[str, set[str]] = {}
        self._defaults: dict[str, dict] = {}
        self._bootstrap()

    # ---- 连接 ----------------------------------------------------------

    def _conn(self):
        conn = getattr(self._local, "conn", None)
        if conn is None or conn.closed:
            from psycopg.rows import dict_row

            conn = self._psycopg.connect(
                self.url, connect_timeout=self._connect_timeout,
                row_factory=dict_row)
            conn.autocommit = True
            self._local.conn = conn
        return conn

    @property
    def db(self):
        return self._conn()

    def close(self) -> None:  # pragma: no cover - 进程退出/测试清理
        conn = getattr(self._local, "conn", None)
        if conn is not None and not conn.closed:
            conn.close()

    def _bootstrap(self) -> None:
        with self._init_lock:
            conn = self._conn()
            with conn.cursor() as cur:
                cur.execute(PG_DDL)
            row = self.value(
                "SELECT value FROM meta_state WHERE key='schema_version'")
            if row is None:
                self.execute(
                    "INSERT INTO meta_state(key, value) VALUES(?,?)",
                    ("schema_version", str(SCHEMA_VERSION)))
            elif int(row or 0) > SCHEMA_VERSION:
                raise Invalid(
                    f"元数据 schema 版本 {row} 高于当前代码支持的 "
                    f"{SCHEMA_VERSION}：请用与数据匹配的 Raggi 版本启动，"
                    "或从备份恢复。降级运行会静默改坏数据。")

    # ---- 基础读写 ------------------------------------------------------

    def query(self, sql: str, params: tuple | dict = ()) -> list[dict]:
        with self._conn().cursor() as cur:
            cur.execute(translate_sql(sql), params)
            return [dict(r) for r in cur.fetchall()]

    def query_one(self, sql: str, params: tuple | dict = ()) -> dict | None:
        with self._conn().cursor() as cur:
            cur.execute(translate_sql(sql), params)
            row = cur.fetchone()
            return dict(row) if row is not None else None

    def value(self, sql: str, params: tuple | dict = (), default=None):
        row = self.query_one(sql, params)
        if row is None:
            return default
        return next(iter(row.values()), default)

    def execute(self, sql: str, params: tuple | dict = ()):
        cur = self._conn().cursor()
        cur.execute(translate_sql(sql), params)
        return cur

    def executemany(self, sql: str, seq) -> None:
        with self._conn().cursor() as cur:
            cur.executemany(translate_sql(sql), list(seq))

    # ---- schema 反射 ----------------------------------------------------

    def columns(self, table: str) -> set[str]:
        cached = self._cols.get(table)
        if cached is not None:
            return cached
        rows = self.query(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table,))
        names = {str(r["name"]) for r in rows}
        if names:
            self._cols[table] = names
        return names

    def not_null_defaults(self, table: str) -> dict:
        cached = self._defaults.get(table)
        if cached is not None:
            return cached
        rows = self.query(
            "SELECT column_name AS name, column_default AS dflt "
            "FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ? "
            "AND is_nullable = 'NO' AND column_default IS NOT NULL",
            (table,))
        out: dict[str, Any] = {}
        for row in rows:
            raw = str(row["dflt"] or "")
            # PG 的默认值带类型转换：`'text'::text` / `0.0`::double precision
            expr = raw.split("::", 1)[0].strip()
            try:
                out[str(row["name"])] = ast.literal_eval(expr)
            except (ValueError, SyntaxError):
                continue
        if out:
            self._defaults[table] = out
        return out

    # ---- 写入 ----------------------------------------------------------

    def upsert(self, table: str, rows: list[dict], pk: str) -> None:
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
        placeholders = ", ".join("%s" for _ in cols)
        updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != pk)
        sql = (f"INSERT INTO {table} ({', '.join(cols)}) "
               f"VALUES ({placeholders}) "
               f"ON CONFLICT({pk}) DO UPDATE SET {updates}")
        with self._conn().cursor() as cur:
            cur.executemany(sql, [tuple(r[c] for c in cols) for r in rows])

    @contextmanager
    def tx(self) -> Iterator[_PgConn]:
        """事务：psycopg 连接 autocommit=True，这里显式开关事务。"""
        conn = self._conn()
        with conn.transaction():
            yield _PgConn(self, conn)

    # ---- 计数与状态 ----------------------------------------------------

    def count(self, table: str, where: str | None = None,
              params: tuple = ()) -> int:
        sql = f"SELECT count(*) AS v FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return int(self.value(sql, params, 0) or 0)

    def get_state(self, key: str, default: str = "") -> str:
        return str(self.value(
            "SELECT value FROM meta_state WHERE key=?", (key,), default)
            or default)

    def set_state(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO meta_state(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)))

    def bump_state(self, key: str, amount: int = 1) -> int:
        step = int(amount)
        self.execute(
            "INSERT INTO meta_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value = CAST(CAST(meta_state.value AS BIGINT) + ? AS TEXT)",
            (key, str(step), step))
        return self.state_int(key, 0)

    def state_int(self, key: str, default: int = 0) -> int:
        raw = self.get_state(key, str(default))
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    def stats(self) -> dict:
        out: dict[str, Any] = {"engine": "postgres", "file": self.path}
        for t in ("documents", "jobs", "kbs", "apikeys"):
            try:
                out[t] = self.count(t)
            except Exception:  # noqa: BLE001
                out[t] = -1
        return out


def copy_from_sqlite(src, pg: PostgresMetaStore) -> dict[str, int]:
    """把 SQLite 元数据复制到 Postgres（幂等 upsert，可重复执行）。

    只复制**真源表**与 `meta_state`；`rate_buckets` / `idempotency` 是
    短生命周期状态，切库后重新生成即可（旧值带过去反而可能让新实例
    误判"这个请求已经处理过"）。
    """
    pks = {"documents": "doc_id", "jobs": "job_id",
           "kbs": "kb_id", "apikeys": "key_id"}
    report: dict[str, int] = {}
    for table, cols in TABLES.items():
        rows = src.query(f"SELECT {', '.join(cols)} FROM {table}")
        if rows:
            pg.upsert(table, rows, pks[table])
        report[table] = len(rows)
    state = src.query("SELECT key, value FROM meta_state")
    for row in state:
        pg.set_state(str(row["key"]), str(row["value"]))
    report["meta_state"] = len(state)
    return report
