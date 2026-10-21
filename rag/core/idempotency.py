"""幂等键：重试同一个请求不会产生第二份数据。

**为什么需要**：网络超时后客户端无法区分「服务端没收到」与「收到了但
响应丢了」，最常见的应对是重试——而对入库这类非幂等的写操作，重试就会
产生第二份文档。带 `Idempotency-Key` 的重试会返回**第一次的结果**。

**边界（要如实说明）**：
- 内容哈希去重已经挡住了「同一内容重复入库」，但那是**事后**发现并返回
  `status=duplicate`，调用方拿不到「这是我第一次提交的结果」这个确定性；
- 幂等键解决的是**过程**幂等：同一个键的第二次请求根本不进流水线。
- 记录有 TTL（默认 24 小时），过期后同一键会被当成新请求——这是有意
  的取舍：永久保留会让键空间无界增长，而实际重试都发生在分钟级。

**2026-10-20 起：跨进程 + 同键并发合流**
SQLite（`storage.meta_engine` 的默认真源）里有一张 `idempotency` 表：
- `pending`：某个请求正在执行；同键的其它请求等待（有上限），而不是
  同时再跑一遍解析/向量化；
- `done`：保存首次结果，等待者与后续重试直接回放；
- `failed`：不保留行，失败重试可以真正再试一次。

没有 MetaStore（`meta_engine=lancedb` 回退）时退回进程内实现，行为与
旧版一致；这是显式的降级边界，不假装跨进程。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger("raggi.idempotency")

HEADER = "Idempotency-Key"
DEFAULT_TTL_SECONDS = 24 * 3600
# pending 的接管时间：进程被杀后，另一个请求最多等这么久就可以接管。
# 取 15 分钟是为了覆盖大文档的 parse/embed；正常请求远短于此。
PENDING_TTL_SECONDS = 15 * 60
# 同键等待上限：超过就返回 409，让客户端稍后重试，而不是无限占着连接。
CLAIM_WAIT_SECONDS = 20.0
# 键长度上限：键会落进日志，限制长度避免被用来撑爆内存/日志
MAX_KEY_LEN = 200
# 同一键最多记多少条结果（同一键不该产生多次成功，防的是键被复用）
MAX_ENTRIES = 1000

# 与 requestid 同样的理由：只收短且安全的标识
_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


@dataclass
class Claim:
    """一次幂等键认领的结果。

    kind:
    - ``owned``：本请求拿到执行权，token 用于 finish/fail；
    - ``replay``：首次请求已完成，result 是它的结果；
    - ``pending``：首次请求仍在执行且等待超时，调用方应返回 409/稍后重试。
    """
    kind: Literal["owned", "replay", "pending"]
    result: dict | None = None
    token: str | None = None


@dataclass
class _Entry:
    state: Literal["pending", "done"]
    result: dict | None
    owner: str
    created: float
    updated: float


def _scope(path: str, key: str) -> str:
    """进程内键：与 SQL 的 (path,key) 复合主键等价。"""
    return f"{path}\n{key}"


def _dumps(result: dict) -> str | None:
    try:
        return json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError) as e:
        logger.warning("幂等结果无法序列化，将不缓存: %s", e)
        return None


@dataclass
class IdempotencyStore:
    """幂等存储：有 MetaStore 时跨进程，否则进程内。"""

    ttl: float = DEFAULT_TTL_SECONDS
    pending_ttl: float = PENDING_TTL_SECONDS
    meta: Any | None = field(default=None, repr=False, compare=False)
    _entries: dict[str, _Entry] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _cond: threading.Condition = field(init=False, repr=False)
    _shared_gc_at: float = field(default=0.0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._cond = threading.Condition(self._lock)

    @property
    def shared(self) -> bool:
        return self.meta is not None

    # ---- 旧的 get/put 接口（测试与兼容）--------------------------------

    def get(self, key: str, path: str) -> dict | None:
        """取出该键此前**完成**的结果；没有或已过期则返回 None。"""
        now = time.time()
        if self.shared:
            try:
                row = self.meta.query_one(
                    "SELECT result, updated FROM idempotency "
                    "WHERE path=? AND key=? AND state='done'", (path, key))
            except Exception as e:  # noqa: BLE001
                logger.warning("读取幂等结果失败（按未命中处理）: %s", e)
                return None
            if row is None:
                return None
            if now - float(row["updated"]) > self.ttl:
                try:
                    self.meta.execute(
                        "DELETE FROM idempotency WHERE path=? AND key=? "
                        "AND state='done'", (path, key))
                except Exception:  # noqa: BLE001
                    pass
                return None
            try:
                value = json.loads(row["result"] or "{}")
            except (TypeError, ValueError):
                return None
            return value if isinstance(value, dict) else None

        with self._lock:
            e = self._entries.get(_scope(path, key))
            if e is None or e.state != "done":
                return None
            if now - e.updated > self.ttl:
                del self._entries[_scope(path, key)]
                return None
            return dict(e.result) if e.result is not None else None

    def put(self, key: str, path: str, result: dict) -> None:
        """记录该键的完成结果（供测试与旧的直接调用）。

        API 路径应使用 claim/finish/fail，避免覆盖一个 pending 的认领。
        """
        now = time.time()
        if self.shared:
            payload = _dumps(result)
            if payload is None:
                return
            try:
                self.meta.execute(
                    "INSERT INTO idempotency"
                    "(path,key,state,owner,result,error,created,updated) "
                    "VALUES(?,?,'done','',?,NULL,?,?) "
                    "ON CONFLICT(path,key) DO UPDATE SET state='done', owner='', "
                    "result=excluded.result, error=NULL, updated=excluded.updated",
                    (path, key, payload, now, now))
            except Exception as e:  # noqa: BLE001
                logger.warning("写入幂等结果失败: %s", e)
            return
        with self._cond:
            self._gc_mem(now)
            self._entries[_scope(path, key)] = _Entry(
                state="done", result=dict(result), owner="",
                created=now, updated=now)
            self._cond.notify_all()

    # ---- 认领 / 完成 ----------------------------------------------------

    def claim(self, key: str, path: str,
              wait_seconds: float = CLAIM_WAIT_SECONDS) -> Claim:
        """认领一个幂等键。

        - 已完成且未过期 → replay；
        - 别的请求正在跑 → 等待（最多 wait_seconds），超时 → pending；
        - 无记录 / 过期 / 失败留下的空位 / pending 超时 → owned（本请求执行）。
        """
        token = uuid.uuid4().hex
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        while True:
            if self.shared:
                claim = self._claim_shared(key, path, token)
                if claim is not None:
                    return claim
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return Claim("pending")
                time.sleep(min(0.05, remaining))
            else:
                claim = self._claim_mem(key, path, token, deadline)
                if claim is not None:
                    return claim

    def _claim_shared(self, key: str, path: str, token: str) -> Claim | None:
        now = time.time()
        try:
            with self.meta.tx() as conn:
                self._gc_shared(conn, now)
                row = conn.execute(
                    "SELECT state, owner, result, updated FROM idempotency "
                    "WHERE path=? AND key=?", (path, key)).fetchone()
                if row is not None:
                    state = str(row["state"])
                    updated = float(row["updated"])
                    if state == "done":
                        if now - updated <= self.ttl:
                            try:
                                result = json.loads(row["result"] or "{}")
                            except (TypeError, ValueError):
                                result = None
                            if isinstance(result, dict):
                                return Claim("replay", result=result)
                        # 过期结果视同不存在，继续走认领
                    elif state == "pending" and now - updated <= self.pending_ttl:
                        return None  # 让上层等待
                conn.execute(
                    "INSERT INTO idempotency"
                    "(path,key,state,owner,result,error,created,updated) "
                    "VALUES(?,?,'pending',?,NULL,NULL,?,?) "
                    "ON CONFLICT(path,key) DO UPDATE SET "
                    "state='pending', owner=excluded.owner, result=NULL, "
                    "error=NULL, created=excluded.created, "
                    "updated=excluded.updated",
                    (path, key, token, now, now))
                return Claim("owned", token=token)
        except Exception as e:  # noqa: BLE001
            # 存储不可用时不把幂等变成单点故障：本次请求继续执行，
            # 但不缓存结果（token=None）。不做“退回进程内认领”——
            # 那会让 finish/fail 继续走共享分支，状态机自我矛盾。
            logger.warning("共享幂等存储不可用，本次请求不做幂等记录: %s", e)
            return Claim("owned", token=None)

    def _claim_mem(self, key: str, path: str, token: str,
                   deadline: float) -> Claim | None:
        now = time.time()
        with self._cond:
            self._gc_mem(now)
            entry = self._entries.get(_scope(path, key))
            if entry is not None:
                if entry.state == "done" and now - entry.updated <= self.ttl:
                    return Claim("replay", result=dict(entry.result or {}))
                if entry.state == "pending" and now - entry.updated <= self.pending_ttl:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return Claim("pending")
                    # 条件变量等待，不让同键的等待者空转
                    self._cond.wait(timeout=min(0.1, remaining))
                    return None
            self._entries[_scope(path, key)] = _Entry(
                state="pending", result=None, owner=token,
                created=now, updated=now)
            return Claim("owned", token=token)

    def finish(self, key: str, path: str, token: str | None,
               result: dict) -> bool:
        """把 owned 的认领标为完成；token 不匹配说明已被接管，不覆盖。"""
        if token is None:
            return False
        now = time.time()
        if self.shared:
            payload = _dumps(result)
            if payload is None:
                return False
            try:
                cur = self.meta.execute(
                    "UPDATE idempotency SET state='done', result=?, error=NULL, "
                    "updated=? WHERE path=? AND key=? AND state='pending' "
                    "AND owner=?", (payload, now, path, key, token))
                if cur.rowcount == 0:
                    logger.warning("幂等认领已失效，结果不写入: %s", path)
                    return False
                return True
            except Exception as e:  # noqa: BLE001
                logger.warning("写入幂等结果失败: %s", e)
                return False
        with self._cond:
            entry = self._entries.get(_scope(path, key))
            if entry is None or entry.state != "pending" or entry.owner != token:
                return False
            entry.state = "done"
            entry.result = dict(result)
            entry.updated = now
            self._cond.notify_all()
            return True

    def fail(self, key: str, path: str, token: str | None,
             error: str | None = None) -> bool:
        """释放认领，让重试可以真正重新执行。"""
        if token is None:
            return False
        if self.shared:
            try:
                cur = self.meta.execute(
                    "DELETE FROM idempotency WHERE path=? AND key=? "
                    "AND state='pending' AND owner=?", (path, key, token))
                return cur.rowcount > 0
            except Exception as e:  # noqa: BLE001
                logger.warning("释放幂等认领失败: %s", e)
                return False
        with self._cond:
            ent = self._entries.get(_scope(path, key))
            if ent is None or ent.state != "pending" or ent.owner != token:
                return False
            del self._entries[_scope(path, key)]
            self._cond.notify_all()
            return True

    # ---- GC -------------------------------------------------------------

    def _gc_shared(self, conn, now: float) -> None:
        """清理过期完成项与超时 pending；每小时最多一次。"""
        if now - self._shared_gc_at < 3600:
            return
        self._shared_gc_at = now
        try:
            conn.execute(
                "DELETE FROM idempotency WHERE "
                "(state='done' AND updated < ?) OR "
                "(state='pending' AND updated < ?)",
                (now - self.ttl, now - self.pending_ttl))
            excess = int(conn.execute(
                "SELECT count(*) FROM idempotency").fetchone()[0]) - MAX_ENTRIES
            if excess > 0:
                conn.execute(
                    "DELETE FROM idempotency WHERE rowid IN "
                    "(SELECT rowid FROM idempotency WHERE state='done' "
                    "ORDER BY updated ASC LIMIT ?)", (excess,))
        except Exception as e:  # noqa: BLE001
            logger.warning("幂等记录清理失败: %s", e)

    def _gc_mem(self, now: float) -> None:
        dead = [
            k for k, e in self._entries.items()
            if (e.state == "done" and now - e.updated > self.ttl)
            or (e.state == "pending" and now - e.updated > self.pending_ttl)
        ]
        for k in dead:
            del self._entries[k]
        if len(self._entries) > MAX_ENTRIES:
            ordered = sorted(self._entries.items(), key=lambda kv: kv[1].updated)
            for k, _ in ordered[: len(ordered) // 2]:
                del self._entries[k]

    def reset(self) -> None:
        with self._cond:
            self._entries.clear()
            self._cond.notify_all()
        if self.shared:
            try:
                self.meta.execute("DELETE FROM idempotency")
            except Exception as e:  # noqa: BLE001
                logger.warning("清空共享幂等记录失败: %s", e)


def normalize_key(request) -> str | None:
    """取入站的 Idempotency-Key；缺失或不安全则返回 None（不启用幂等）。"""
    raw = request.headers.get(HEADER) or request.headers.get(
        "idempotency-key")
    if not raw:
        return None
    key = raw.strip()
    if not key or len(key) > MAX_KEY_LEN or not set(key) <= _ALLOWED:
        return None
    return key
