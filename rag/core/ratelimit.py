"""限流：令牌桶（按调用方维度）。

**为什么限写不限读**：检索是读多写少、单次代价低，挡它只会影响正常使用；
而入库/编辑/删除才是真正吃资源、可能被滥用打爆入库队列的路径。

**为什么按调用方而不是全局**：单机自用时全局桶会让一个误写的脚本把
所有人的配额吃掉；按 key_id 分桶则互不影响。

**为什么令牌桶而不是固定窗口**：固定窗口在窗口边界会放行 2 倍流量
（打满当前窗口、立刻再打满下一个），令牌桶用「匀速补充 + 允许突发」
平滑掉这个尖峰，也天然给出 Retry-After。
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("raggi.ratelimit")

# 幂等与无害的写：不消耗配额。
# 停用/启用分块同理——它只是翻一个布尔列，不触发重新向量化。
# 检索也是：POST /search 是**读**语义（只是不接受 body 而已），限它会
# 直接影响正常使用。
# 注意写成裸路径（不含版本前缀）：调用方传入的 path 含 /api/v1，
# 这里统一按「去掉前缀后的路径」比对。
_IDEMPOTENT_PATHS = (
    "/documents/text",
    "/documents/url",
    "/search",
)


@dataclass
class _Bucket:
    tokens: float
    updated: float


@dataclass
class RateLimiter:
    """按调用方的令牌桶。

    limit<=0 表示关闭（单机自用的默认），此时所有方法都是空操作——
    不给默认配置强加一个「某天突然开始报 429」的惊喜。
    """

    limit: int = 0
    burst: int = 0
    # 有 MetaStore 时使用跨进程共享桶；None（lancedb 回退形态）时退回
    # 进程内桶。共享模式用 wall clock，因为 monotonic 不能跨进程比较。
    meta: Any | None = field(default=None, repr=False, compare=False)
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _shared_gc_at: float = field(default=0.0, init=False, repr=False)
    _shared_error_logged: bool = field(default=False, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.burst <= 0:
            self.burst = max(1, self.limit)

    @property
    def shared(self) -> bool:
        return self.meta is not None

    @property
    def enabled(self) -> bool:
        return self.limit > 0

    def allow(self, key: str) -> tuple[bool, int]:
        """取一个令牌。返回 (是否放行, 建议等待秒数)。

        Retry-After 用「补一个令牌要多久」计算，这是保证速率的最小等待
        时间——比它短就一定会再被拒。
        """
        if not self.enabled:
            return True, 0
        if self.shared:
            return self._allow_shared(key)
        now = time.monotonic()
        with self._lock:
            b = self._buckets.get(key)
            if b is None:
                b = _Bucket(tokens=float(self.burst), updated=now)
                self._buckets[key] = b
                self._gc(now)
            else:
                # 按经过时间补充，上限为 burst
                b.tokens = min(float(self.burst),
                                b.tokens + (now - b.updated) * self.limit / 60.0)
                b.updated = now
            if b.tokens >= 1.0:
                b.tokens -= 1.0
                return True, 0
            # 还差多少令牌，以及补一个令牌要多久
            need = 1.0 - b.tokens
            retry = int(need * 60.0 / self.limit) + 1
            return False, max(1, retry)

    def _allow_shared(self, key: str) -> tuple[bool, int]:
        """SQLite 共享令牌桶（多 worker 下额度不会被乘以进程数）。

        整个 read-modify-write 在一个 BEGIN IMMEDIATE 事务里完成；
        busy_timeout 让并发请求排队而不是直接报 SQLITE_BUSY。

        数据库暂时不可用时选择**放行并告警**：限流是保护措施，不能
        因为观测/元数据层故障把所有写请求一起打死。鉴权路径的默认值
        相反（拿不准就拒绝），因为那是权限边界。
        """
        now = time.time()
        try:
            with self.meta.tx() as conn:
                row = conn.execute(
                    "SELECT tokens, updated FROM rate_buckets WHERE caller=?",
                    (key,)).fetchone()
                if row is None:
                    tokens = float(self.burst)
                    updated = now
                else:
                    tokens = min(
                        float(self.burst),
                        float(row["tokens"])
                        + (now - float(row["updated"])) * self.limit / 60.0)
                    updated = now
                if tokens >= 1.0:
                    tokens -= 1.0
                    allowed, retry = True, 0
                else:
                    need = 1.0 - tokens
                    retry = max(1, int(need * 60.0 / self.limit) + 1)
                    allowed = False
                conn.execute(
                    "INSERT INTO rate_buckets(caller, tokens, updated) "
                    "VALUES(?,?,?) ON CONFLICT(caller) DO UPDATE SET "
                    "tokens=excluded.tokens, updated=excluded.updated",
                    (key, tokens, updated))
                self._gc_shared(conn, now)
                return allowed, retry
        except Exception as e:  # noqa: BLE001
            if not self._shared_error_logged:
                self._shared_error_logged = True
                logger.warning(
                    "共享限流不可用，本次放行（后续同类错误不再逐条记录）: %s", e)
            return True, 0

    def _gc_shared(self, conn, now: float) -> None:
        """每小时最多清一次长期不用的桶；桶已补满的与新建等价。"""
        if now - self._shared_gc_at < 3600:
            return
        self._shared_gc_at = now
        stale = self.burst / max(1, self.limit / 60.0) * 2 + 60
        conn.execute(
            "DELETE FROM rate_buckets WHERE updated < ? AND tokens >= ?",
            (now - stale, float(self.burst)))

    def _gc(self, now: float) -> None:
        """清掉长期不用的桶，避免 key 无限增长。

        以「桶已补满且久未使用」为标准：这样的桶与新建的等价，
        留着没有意义；正在被限制的桶一定更近，不会被误删。
        """
        stale = self.burst / max(1, self.limit / 60.0) * 2 + 60
        cutoff = now - stale
        dead = [k for k, b in self._buckets.items()
                if b.updated < cutoff and b.tokens >= self.burst]
        for k in dead:
            del self._buckets[k]

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()
        if self.meta is not None:
            try:
                self.meta.execute("DELETE FROM rate_buckets")
            except Exception as e:  # noqa: BLE001
                logger.warning("清空共享限流桶失败: %s", e)


def caller_key(request, key_row: dict | None) -> str:
    """限流维度：按有效密钥分，未鉴权时按客户端 IP。

    未鉴权场景（本机自用）按 IP 足够；而把所有未鉴权请求归到一个桶会
    让并发页面加载互相影响。
    """
    if key_row and key_row.get("key_id"):
        return f"key:{key_row['key_id']}"
    client = request.client
    return f"ip:{client.host}" if client else "ip:unknown"


def normalize_path(path: str) -> str:
    """去掉 /api 或 /api/v1 前缀，统一带上开头的斜杠。

    豁免名单因此不必关心版本：加了 /api/v2 也不会让豁免失效。
    """
    for prefix in ("/api/v1/", "/api/"):
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    return "/" + path.lstrip("/")


def is_limited_method(request) -> bool:
    """只有写方法参与限流。"""
    return request.method in ("POST", "PUT", "PATCH", "DELETE")


def is_idempotent(path: str) -> bool:
    """路径是否在幂等豁免名单里。

    内容哈希去重让「同一段文本重复入库」本来就安全，挡它只会让
    脚本作者困惑：明明没入库成功，却被告知「太频繁」。
    """
    rel = normalize_path(path)
    return any(rel == p or rel.startswith(p + "?") for p in _IDEMPOTENT_PATHS)

# ---- Redis 共享令牌桶 ------------------------------------------------------
#
# 与 SQLite 版语义一致：整个 read-modify-write 在一段 Lua 里原子完成，
# 多副本共享同一份额度。墙钟时间由应用传入（Redis TIME 在同一台机器上
# 也可能与副本不同，统一用调用方时间更可预期）。
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local burst = tonumber(ARGV[3])
local ttl = tonumber(ARGV[4])
local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = burst
  ts = now
end
tokens = math.min(burst, tokens + (now - ts) / 1000.0 * rate)
local allowed = 0
local retry = 0
if tokens >= 1 then
  tokens = tokens - 1
  allowed = 1
else
  retry = math.ceil((1 - tokens) / rate)
end
redis.call('HSET', key, 'tokens', tostring(tokens), 'ts', tostring(now))
redis.call('PEXPIRE', key, ttl)
return {allowed, retry}
"""


class RedisRateLimiter:
    """跨副本共享的写限流（Redis 令牌桶）。

    Redis 不可用时**退回进程内令牌桶**并告警：限流是保护措施，不能因为
    缓存层故障把所有写请求打死；但也不能完全放行——那样一次 Redis 抖动
    就会把入库队列打满。
    """

    def __init__(self, url: str, *, namespace: str = "raggi",
                 limit: int = 0, burst: int = 0, timeout_ms: int = 300):
        self.limit = max(0, int(limit))
        self.burst = int(burst) if burst > 0 else self.limit
        self._client = None
        self._error_logged = False
        self._fallback = RateLimiter(limit=self.limit, burst=self.burst)
        if self.limit <= 0:
            return
        try:
            from rag.core.redis import RedisClient

            self._client = RedisClient(
                url, namespace=f"{namespace}:rl", timeout_ms=timeout_ms,
                max_connections=16)
        except Exception as e:  # noqa: BLE001
            logger.warning("Redis 限流初始化失败，退回进程内限流: %s", e)

    @property
    def enabled(self) -> bool:
        return self.limit > 0 and self._client is not None

    def allow(self, key: str) -> tuple[bool, int]:
        if not self.enabled or self._client is None:
            return self._fallback.allow(key)
        rate = self.limit / 60.0
        ttl_ms = max(60_000, int(self.burst / max(rate, 1e-9) * 2000) + 60_000)
        try:
            allowed, retry = self._client.eval(
                _TOKEN_BUCKET_LUA, [f"bucket:{key}"],
                [int(time.time() * 1000), rate, float(self.burst), ttl_ms])
        except Exception as e:  # noqa: BLE001
            if not self._error_logged:
                self._error_logged = True
                logger.warning(
                    "Redis 限流不可用，退回进程内令牌桶（后续同类错误不再逐条记录）: %s",
                    e)
            return self._fallback.allow(key)
        return bool(int(allowed)), int(retry)
