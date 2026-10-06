"""鉴权：API 密钥 + 旧版 Bearer token 双通道，附带权限作用域。

三种凭据（按优先级）：

1. **API 密钥**：`Authorization: Bearer rg_...` 或 `X-API-Key: rg_...`。
   可单独吊销、可设过期、有 read/write 作用域，是对外集成的推荐方式。
2. **旧版静态 token**：`settings.token` 非空时接受 `Bearer <token>`。
   为兼容既有部署保留，等价于 write 全权限。
3. **未启用鉴权**：`token` 为空且没有任何密钥时不拦截（单机自用）。

**密钥管理接口本身要求 write 作用域**——否则一把 read 密钥就能签发
新密钥、提权到 write，等于把权限体系作废。
"""
from __future__ import annotations

import re

from fastapi.responses import JSONResponse

from rag.storage.repos import keys as apikeys

# 密钥管理端点：需要 write 作用域（防止 read 密钥自我提权）。
# 两个前缀都要覆盖：/api/v1 是规范路径，/api 是兼容别名。
ADMIN_PREFIXES = ("/api/v1/keys", "/api/keys")

# 只读方法：read 作用域即可
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


class AuthError(Exception):
    """鉴权失败。由中间件转成 JSON 响应。

    **不能直接 raise HTTPException**：在 Starlette 中间件里抛出的
    HTTPException 不会走 ExceptionMiddleware（它在中间件栈的内层），
    最终变成裸 500「Internal Server Error」，前端拿不到任何原因。
    故这里用自定义异常，由中间件显式构造响应。
    """

    def __init__(self, status_code: int, detail: str,
                 headers: dict | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.headers = headers or {}

    def to_response(self) -> JSONResponse:
        return JSONResponse(
            {"detail": self.detail},
            status_code=self.status_code,
            headers=self.headers)


def extract_secret(request) -> str:
    """从请求里取出凭证明文。

    两种头都支持：`Authorization: Bearer <key>` 与 `X-API-Key: <key>`。
    后者对浏览器 fetch 更省事，也避免与其它 Bearer 用途冲突。
    """
    hdr = request.headers.get("authorization", "")
    if hdr.lower().startswith("bearer "):
        return hdr[7:].strip()
    return (request.headers.get("x-api-key") or "").strip()


def is_protected(path: str) -> bool:
    """该路径是否需要鉴权（静态前端与 OPTIONS 放行）。"""
    return path.startswith("/api")


# 原文文件路径：/api/documents/{doc_id}/file
_FILE_RE = re.compile(r"^/api(?:/v1)?/documents/([^/]+)/file$")


def _is_file_path(path: str) -> bool:
    return _FILE_RE.match(path) is not None


def _doc_id_of(path: str) -> str:
    m = _FILE_RE.match(path)
    return m.group(1) if m else ""


def _keys_configured(ctx) -> bool:
    """「有没有配密钥」。读不出来时抛 503，**绝不**当成「没配」而放行。

    下面两处都把 `has_keys() == False` 当作放行条件（无凭据直通、
    以及零密钥时对 ADMIN_PREFIXES 开放）。所以「apikeys 表暂时读不出来」
    一旦被翻译成 False，效果是整个 API 在那一刻变成无鉴权 ——
    而且这个答案会被缓存 10 秒。鉴权拿不准时的默认值只能是拒绝。
    """
    try:
        return apikeys.has_keys(ctx.store)
    except apikeys.ApiKeysUnavailable as e:
        raise AuthError(503, "鉴权配置暂时不可读，请稍后重试") from e


def authenticate(ctx, request) -> dict | None:
    """校验请求凭据。

    返回值：
    - dict：有效密钥的元数据（含 scope / key_id）
    - None：未启用任何鉴权，或凭据是旧版静态 token / 有效签名（视为已授权）

    失败抛 AuthError（由中间件转成 401/403）。
    """
    path = request.url.path
    if not is_protected(path):
        return None

    secret = extract_secret(request)
    legacy = (ctx.settings.token or "").strip()
    q = request.query_params
    exp = q.get("exp") or ""
    sig = q.get("sig") or ""

    # 未配置任何鉴权 → 放行（保持单机自用的零摩擦体验）
    if not secret and not legacy and not _keys_configured(ctx):
        return None

    # CORS 预检不带凭据，必须放行，否则浏览器端调用会被 401 挡住
    if request.method == "OPTIONS":
        return None

    # 签名 URL：<iframe>/<img>/<embed> 无法携带请求头，原文预览只能
    # 靠查询串签名。签名与 doc_id 绑定且短期有效，等价于一次授权。
    if sig and _is_file_path(path):
        if ctx.signer.verify(_doc_id_of(path), exp, sig):
            return None
        raise AuthError(403, "原文链接已过期或签名无效")

    # 1) 先试 API 密钥
    if secret.startswith(apikeys.KEY_PREFIX):
        try:
            row = apikeys.verify(ctx.store, secret)
        except apikeys.ApiKeyAuthError as e:
            raise AuthError(401, f"API 密钥无效：{e}",
                            headers={"WWW-Authenticate": "Bearer"})
        need_write = request.method not in SAFE_METHODS
        if not apikeys.scope_allows(row.get("scope", "read"), need_write):
            raise AuthError(
                403, f"该密钥权限为 {row.get('scope')}，无法执行写操作")
        # 只有「签发 / 吊销」需要 write。列举是只读的，允许 read 密钥查看
        # ——否则用户签发第一把密钥后，浏览器立刻失去查看密钥页的权限，
        # 而此时它手里只有刚创建的那把。列举不构成提权（拿不到明文）。
        if path.startswith(ADMIN_PREFIXES) and request.method != "GET":
            if row.get("scope") != apikeys.SCOPE_WRITE:
                raise AuthError(403, "签发或吊销密钥需要 write 权限")
        # 使用时间是观测数据，失败不影响请求
        apikeys.touch(ctx.store, str(row.get("key_id")))
        return row

    # 2) 旧版静态 token（兼容既有部署，等价 write 全权限）
    if legacy and secret == legacy:
        return None

    # 3) **引导例外**：系统里一把密钥都没有时，允许无凭据访问管理接口。
    #    否则创建第一把密钥后就再也发不出第二把——把自己锁在门外；
    #    前端也需要先读到空列表才能渲染「还没有密钥」的引导。
    #    只在「零密钥」时成立；一旦有密钥，管理接口即受保护。
    if not _keys_configured(ctx) and path.startswith(ADMIN_PREFIXES):
        return None
    if not secret:
        raise AuthError(
            401, "缺少凭据：请提供 API 密钥"
                 "（Authorization: Bearer rg_... 或 X-API-Key）",
            headers={"WWW-Authenticate": "Bearer"})
    raise AuthError(401, "凭据无效", headers={"WWW-Authenticate": "Bearer"})