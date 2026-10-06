"""请求 ID：让「用户看到的错」和「服务端日志里的错」能对上。

单机工具出问题时，排障最花时间的是「客户端那条 500 到底对应服务端
哪一行日志」。一个请求 ID 贯穿响应头、错误体与日志行即可解决。

ID 优先采用**上游给的** `X-Request-ID`——多层代理下只要最外层传进来，
整条链路就用同一个，不必每层各自生成。
"""
from __future__ import annotations

import uuid

HEADER = "X-Request-ID"
# 从入站头取回请求 ID 时允许的字符：只收短且安全的标识，
# 避免把任意长字符串（含控制字符）写进日志或回显。
_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.")


def new_request_id() -> str:
    return uuid.uuid4().hex[:16]


def incoming_id(request) -> str | None:
    """取上游传来的请求 ID，不合法则返回 None（改为自生成）。"""
    raw = request.headers.get(HEADER) or request.headers.get("x-request-id")
    if not raw:
        return None
    rid = raw.strip()
    # 限长防日志注入，长度上限也避免回显一个巨大的头
    if not rid or len(rid) > 64 or not set(rid) <= _ALLOWED:
        return None
    return rid


def resolve(request) -> str:
    return incoming_id(request) or new_request_id()