"""API 密钥：签发、校验、吊销。

设计取舍（安全优先，且不引入新依赖）：

- **只存哈希**：库里保存 `sha256(secret)`，明文仅在创建时返回一次。
  库被拷贝 / 备份泄露时不会直接变成可用凭据。
- **明文只在创建时出现一次**：标准做法（GitHub / Stripe 同款），
  丢了只能吊销重签，无法找回。
- **前缀用于索引而非校验**：`rg_` + 8 位随机 id 便于人工核对
  「这是谁的 key」而不必比对全串；校验仍走完整哈希（见 verify）。
- **权限作用域**：粗粒度的 read / write 两档即可覆盖单机 RAG 的
  真实需求（只读检索 vs 可写管理），不做过细的端点级 ACL。
- **过期与吊销**：expires_at 与 revoked_at 都是普通列，校验时判断；
  「吊销」不删除行，保留审计痕迹。

密钥通过 `Authorization: Bearer rg_...` 或 `X-API-Key: rg_...` 传递，
两者等效。
"""
from __future__ import annotations

import datetime
import hashlib
import logging
import secrets
import threading
import uuid
from typing import TYPE_CHECKING

from rag.core.cache import TTLCache

from ..sql import escape_sql, fetch_rows
from ._engine import meta_of as _meta

if TYPE_CHECKING:
    # 只在注解里出现 ⇒ 运行时不需要，去掉它就断掉 tables↔repos 这条边
    # （详见 kbs.py 里同一段的说明）。
    from ..tables import LanceStore

logger = logging.getLogger("raggi.apikeys")

# 密钥前缀：便于在日志/文档里一眼识别，也方便未来的密钥扫描器
KEY_PREFIX = "rg_"
# 明文段长度（base62 字符）
SECRET_LEN = 32
# 前缀中携带的随机 id 长度
PUBLIC_LEN = 8

# 权限作用域
SCOPE_READ = "read"      # 检索 / 读取 / 健康与统计
SCOPE_WRITE = "write"    # 读 + 入库 / 编辑 / 删除 / 配置变更
SCOPES = (SCOPE_READ, SCOPE_WRITE)

# 仅这些前缀的路径属于「受控 API」，其余（静态资源）不校验
API_PREFIX = "/api"

# touch 的节流状态：key_id → 最近一次写入的 ISO 时间戳（截到分钟）。
# 进程内即可 —— 多 worker 部署下最多多写几次，不影响正确性。
_touch_lock = threading.Lock()
_touch_cache: dict[str, str] = {}
# 上界：没有它，长跑进程里每把见过的密钥都会留一项（历史遗留、被吊销后
# 不再使用的 key 会一直占着）。超过就丢掉最旧的一半。
_TOUCH_CACHE_MAX = 1024

# ---- 鉴权读缓存 --------------------------------------------------------
# 为什么要有：`verify()` 每个带密钥的请求都要查一次库。实测在 2000 行的
# documents 上做一次 LanceDB 点查 ~5.7ms（apikeys 表小，但也得付这份
# 「每查询固定开销」），而鉴权是每个请求的必经路径——它直接把并发上限
# 钉在了 1/5.7ms。见 docs/PERF-CONCURRENCY-2026-10-05.md §2.2。
#
# 缓存的正确性边界（必须说清，不然这就是个安全口子）：
# - TTL 5s：吊销/过期最长 5s 后失效；
# - **同进程内立即失效**：create_key / revoke 会 bump epoch 并清空缓存，
#   所以「界面上吊销 → 立刻再也进不来」这条用户体验不变；
# - **跨进程**（多 worker 部署）只能等 TTL，因为缓存是各进程私有的。
#   这一条如实写进 docs/PERF-FINAL-DECISION-2026-10-05.md 的契约变化表。
_VERIFY_TTL_SECONDS = 5.0
_HAS_KEYS_TTL_SECONDS = 10.0
# 两张缓存都收在 core.cache.TTLCache 里（线程安全 + 上界 + 主动失效）
_verify_cache = TTLCache(max_items=512, ttl_seconds=_VERIFY_TTL_SECONDS)
_has_keys_cache = TTLCache(max_items=4, ttl_seconds=_HAS_KEYS_TTL_SECONDS)


def _invalidate_read_cache() -> None:
    """任何改变 apikeys 表的操作都要调它，否则「吊销后仍能进」会持续一整个 TTL。"""
    _verify_cache.clear()
    _has_keys_cache.clear()


def _find_by_hash(store: LanceStore, key_hash: str) -> dict | None:
    meta = _meta(store)
    if meta is not None:
        # 主键唯一索引 idx_key_hash：点查从「一次表扫描」变成一次索引探测
        return meta.query_one(
            "SELECT key_id, name, prefix, scope, created_at, last_used_at,"
            " expires_at, revoked_at FROM apikeys WHERE key_hash=?",
            (key_hash,))
    # 窄列 + 原生投影：只取校验真正需要的 8 列（见 api/auth.py 的同一条教训——
    # 不指定投影时 LanceDB 不保证列裁剪，`text` 这类大列会被一起搬回 Python）。
    from ..sql import scalar_rows

    rows = scalar_rows(store.apikeys,
                       cols=["key_id", "name", "prefix", "scope", "created_at",
                             "last_used_at", "expires_at", "revoked_at"],
                       where=f"key_hash = '{escape_sql(key_hash)}'", limit=1)
    return rows[0] if rows else None


def verify(store: LanceStore, secret: str) -> dict:
    """校验明文密钥，返回其元数据；失败抛 ApiKeyAuthError。

    库中只存 `sha256(secret)`，因此这里是**用哈希查表**而非明文比对：
    早前注释声称用 hmac.compare_digest 做常数时间比较，实际并没有调用——
    表查询由 DataFusion 执行，无法逐字节控制比较开销。

    真正的时序防护来自「只存哈希」本身：即便比较耗时泄露，也只能暴露
    某个哈希的存在性，而攻击者无法从哈希反推密钥。
    """
    if not secret or not secret.startswith(KEY_PREFIX):
        raise ApiKeyAuthError("密钥格式不正确")
    # 缓存键必须带上 store.uri：一个进程可能同时挂着多个数据目录
    # （测试里每个用例都是独立 tmp_path）。只按 key_hash 缓存会让
    # A 库签发的密钥在 B 库里也被判为有效——那是鉴权层面的越权。
    key_hash = _hash(secret)
    ckey = (store.uri, key_hash)
    row = _verify_cache.get(ckey)
    if row is not None:
        row = dict(row)              # 副本：调用方改返回值不能污染缓存
    if row is None:
        row = _find_by_hash(store, key_hash)
        if row is None:
            # 不区分「不存在」与「不匹配」，避免泄露密钥是否存在
            raise ApiKeyAuthError("密钥无效")
        _verify_cache.set(ckey, dict(row))

    # 以下三项**每次都要重新判**（不能只信缓存）：吊销可能发生在别处，
    # 而「过期」本身就是随时间变化的量——缓存只省去查库，不省去判定。
    if row.get("revoked_at"):
        raise ApiKeyAuthError(f"密钥已于 {row['revoked_at']} 吊销")
    exp = row.get("expires_at")
    if exp:
        try:
            if datetime.datetime.fromisoformat(exp) <= \
                    datetime.datetime.now(datetime.timezone.utc):
                raise ApiKeyAuthError(f"密钥已于 {exp} 过期")
        except ValueError:
            pass  # 过期时间格式异常不阻断（数据损坏时从严见下方说明）
    return row


def has_keys(store: LanceStore) -> bool:
    """是否存在任何密钥（用于判断是否应启用强制鉴权）。

    带 10s 缓存：这是**每个请求**都要回答的问题（未配密钥时直接放行），
    而 apikeys 表只在签发/吊销时变化。签发与吊销都会主动失效它。

    **数不出来时必须抛，不能回答 False。** 这两个调用点都拿
    「一把密钥都没有」当**放行**条件（`api/auth.py` 的无凭据直通与
    ADMIN_PREFIXES 的引导分支）：回答 False 等于在读取失败的那一刻
    **把整个 API 变成无鉴权**，而且这个答案会被缓存 10 秒 ——
    一次瞬时故障就是 10 秒的全体放行。上面那段注释早就写着
    「数错了不是性能问题而是安全问题」，却把唯一的错误路径写成了放行值。
    """
    def _load() -> bool:
        mdb = _meta(store)
        if mdb is not None:
            # 「密钥在哪个引擎」必须和「数哪个引擎」一致。数错了不是性能
            # 问题而是**安全**问题：authenticate 里「一把密钥都没有 → 直接放行」
            # 的引导分支会永远成立，整个 API 变成无鉴权。
            # （这条是被 tests/test_meta_store.py 的双引擎对齐测试抓出来的。）
            try:
                return mdb.count("apikeys") > 0
            except Exception as e:  # noqa: BLE001
                logger.error("apikeys 计数失败（拒绝猜测『无密钥』以免放行）: %s",
                             e)
                raise ApiKeysUnavailable(f"鉴权配置暂时不可读：{e}") from e
        try:
            return store.apikeys.count_rows() > 0
        except Exception as e:  # noqa: BLE001
            logger.error("apikeys 计数失败（拒绝猜测『无密钥』以免放行）: %s", e)
            raise ApiKeysUnavailable(f"鉴权配置暂时不可读：{e}") from e

    value = _has_keys_cache.cached(store.uri, _load)
    return bool(value)


class ApiKeysUnavailable(RuntimeError):
    """读不出「有没有密钥」。这与「没有密钥」是两件事，绝不能混。

    API 层把它映射成 503（重试可能就好），而不是 200（放行）或 401（误导）。
    """


class ApiKeyAuthError(RuntimeError):
    """密钥校验失败。调用方映射为 401，并带上可行动的原因。"""


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def generate_secret() -> str:
    """生成明文密钥：rg_<public>_<secret>，整体一次性展示给用户。"""
    public = "".join(
        secrets.choice("abcdefghijkmnpqrstuvwxyz23456789")
        for _ in range(PUBLIC_LEN))
    body = "".join(
        secrets.choice("abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ"
                       "23456789")
        for _ in range(SECRET_LEN))
    return f"{KEY_PREFIX}{public}_{body}"


def public_id(secret: str) -> str:
    """从明文密钥取出公开 id（前缀段），用于人工核对。"""
    parts = secret.split("_")
    return f"{KEY_PREFIX}{parts[1]}" if len(parts) > 1 else KEY_PREFIX


def _validate_scope(scope: str) -> str:
    if scope not in SCOPES:
        raise ValueError(
            f"未知权限 {scope!r}，可选：{' / '.join(SCOPES)}")
    return scope


def create_key(store: LanceStore, name: str, *,
               scope: str = SCOPE_READ,
               expires_in_days: int | None = None,
               note: str = "") -> tuple[dict, str]:
    """签发一把密钥。返回 (入库行, 明文密钥)。

    明文只在这一次返回——调用方负责立刻展示给用户。
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("密钥名称不能为空")
    scope = _validate_scope(scope)
    if expires_in_days is not None:
        expires_in_days = int(expires_in_days)
        if expires_in_days <= 0:
            raise ValueError("有效期必须为正整数天")
    secret = generate_secret()
    key_id = str(uuid.uuid4())
    now = _now()
    expires_at = ""
    if expires_in_days:
        expires_at = (
            datetime.datetime.now(datetime.timezone.utc)
            + datetime.timedelta(days=expires_in_days)).isoformat()
    row = {
        "key_id": key_id,
        "name": name,
        # 公开段，用于列表里辨认；不是校验依据
        "prefix": public_id(secret),
        "key_hash": _hash(secret),
        "scope": scope,
        "note": note or "",
        "created_at": now,
        "last_used_at": None,
        "expires_at": expires_at,
        "revoked_at": None,
    }
    meta = _meta(store)
    if meta is not None:
        meta.upsert("apikeys", [row], "key_id")
    else:
        store.apikeys.add([row])
    # 新增密钥改变了「有没有密钥」这个全局判断，必须让 has_keys 缓存失效，
    # 否则配好第一把密钥后的 10s 内服务仍处于「无密钥 → 放行」的宽松态。
    _invalidate_read_cache()
    return row, secret


def revoke(store: LanceStore, key_id: str) -> bool:
    """吊销密钥。行保留以便审计，重复吊销幂等。"""
    meta = _meta(store)
    if meta is not None:
        cur = meta.query_one(
            "SELECT key_id, revoked_at FROM apikeys WHERE key_id=?", (key_id,))
        if cur is None:
            return False
        if cur.get("revoked_at"):
            return True
        meta.execute("UPDATE apikeys SET revoked_at=? WHERE key_id=?",
                     (_now(), key_id))
        _invalidate_read_cache()
        return True
    rows = fetch_rows(store.apikeys.search().where(
        f"key_id = '{escape_sql(key_id)}'").select(["key_id", "revoked_at"]))
    if not rows:
        return False
    if rows[0].get("revoked_at"):
        return True
    store.apikeys.update(
        where=f"key_id = '{escape_sql(key_id)}'",
        values={"revoked_at": _now()})
    # 吊销必须立刻对本进程生效：缓存不清就等于吊销要等一个 TTL 才管用
    _invalidate_read_cache()
    return True


def touch(store: LanceStore, key_id: str) -> None:
    """记录最近使用时间。失败不影响主流程（这是观测数据，不是业务）。

    **按密钥节流**：每请求写一次会给 apikeys 表制造一个新 Lance 版本，
    而 optimize(cleanup_older_than) 只清理 chunks 表 —— 高频轮询下
    apikeys 的版本目录会无界增长。last_used_at 是观测用途，
    分钟级精度足够，因此同一密钥一分钟内只写一次。
    """
    now = _now()
    bucket = now[:16]      # ISO 前 16 位即「YYYY-MM-DDTHH:MM」
    with _touch_lock:
        if _touch_cache.get(key_id) == bucket:
            return
        if len(_touch_cache) >= _TOUCH_CACHE_MAX:
            for k in list(_touch_cache)[: _TOUCH_CACHE_MAX // 2]:
                _touch_cache.pop(k, None)
        _touch_cache[key_id] = bucket
    try:
        meta = _meta(store)
        if meta is not None:
            meta.execute("UPDATE apikeys SET last_used_at=? WHERE key_id=?",
                         (now, key_id))
        else:
            store.apikeys.update(
                where=f"key_id = '{escape_sql(key_id)}'",
                values={"last_used_at": now})
    except Exception as e:  # noqa: BLE001
        logger.debug("更新 last_used_at 失败: %s", e)


def list_keys(store: LanceStore) -> list[dict]:
    """列出全部密钥（含已吊销 / 已过期），按创建时间倒序。

    **不含任何哈希或明文**——列表接口不该有能力还原凭据。
    """
    meta = _meta(store)
    if meta is not None:
        rows = meta.query(
            "SELECT key_id, name, prefix, scope, note, created_at,"
            " last_used_at, expires_at, revoked_at FROM apikeys"
            " ORDER BY created_at DESC, key_id")
        return [{**r, "expired": _is_expired(r.get("expires_at"))}
                for r in rows]
    rows = fetch_rows(store.apikeys.search().select(
        ["key_id", "name", "prefix", "scope", "note",
         "created_at", "last_used_at", "expires_at", "revoked_at"]))
    out = []
    for r in rows:
        item = dict(r)
        item["expired"] = _is_expired(item.get("expires_at"))
        out.append(item)
    out.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return out


def _is_expired(expires_at) -> bool:
    if not expires_at:
        return False
    try:
        return datetime.datetime.fromisoformat(
            expires_at) <= datetime.datetime.now(datetime.timezone.utc)
    except (TypeError, ValueError):
        return False


def key_state(row: dict) -> str:
    """派生展示用状态：active / revoked / expired。"""
    if row.get("revoked_at"):
        return "revoked"
    return "expired" if _is_expired(row.get("expires_at")) else "active"


def scope_allows(scope: str, need_write: bool) -> bool:
    """判断作用域是否允许该操作。write 蕴含 read。"""
    if scope == SCOPE_WRITE:
        return True
    return not need_write