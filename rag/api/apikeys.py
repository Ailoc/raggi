"""API 密钥管理端点：列出 / 签发 / 吊销。

**这组端点本身需要 write 作用域**（见 api/auth.py）：否则一把 read
密钥就能签发新密钥把自己提权成 write，权限体系形同虚设。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request

from rag.api._common import get_ctx
from rag.api.responses import (ApiKeyCreatedOut, ApiKeyListOut, KeyRevokedOut,
                               error_responses)
from rag.api.schemas import ApiKeyCreateReq
from rag.storage.repos import keys as apikeys

router = APIRouter(
    tags=["keys"],
    responses=error_responses(not_found=True),
)


def _public(row: dict) -> dict:
    """对外视图：附带派生状态，且绝不包含哈希。"""
    item = dict(row)
    item.pop("key_hash", None)
    item["state"] = apikeys.key_state(item)
    item["expired"] = item["state"] == "expired"
    item["active"] = item["state"] == "active"
    return item


@router.get("/keys")
async def api_keys_list(request: Request) -> ApiKeyListOut:
    """列出全部 API 密钥（含已吊销 / 已过期）。

    响应不含任何哈希或明文——列表接口不该有能力还原凭据。
    """
    ctx = get_ctx(request)
    rows = await asyncio.to_thread(apikeys.list_keys, ctx.store)
    return {"items": [_public(r) for r in rows],
            "total": len(rows),
            "legacy_token_enabled": bool((ctx.settings.token or "").strip())}


@router.post("/keys")
async def api_keys_create(request: Request,
                          body: ApiKeyCreateReq) -> ApiKeyCreatedOut:
    """签发一把新密钥。

    **明文密钥只在此响应中出现一次**，请立刻保存——
    服务端只保留 sha256 哈希，丢失后无法找回，只能吊销重签。
    """
    ctx = get_ctx(request)
    try:
        row, secret = await asyncio.to_thread(
            apikeys.create_key, ctx.store, body.name,
            scope=body.scope,
            expires_in_days=body.expires_in_days,
            note=body.note or "")
    except ValueError as e:
        raise HTTPException(400, str(e))
    out = _public(row)
    out["key"] = secret
    out["warning"] = (
        "这是唯一一次显示完整密钥。请立即保存；服务端只保存 "
        "sha256 哈希，无法找回。")
    return out


@router.delete("/keys/{key_id}")
async def api_keys_revoke(request: Request, key_id: str) -> KeyRevokedOut:
    """吊销密钥（行保留以便审计，重复调用幂等）。"""
    ctx = get_ctx(request)
    ok = await asyncio.to_thread(apikeys.revoke, ctx.store, key_id)
    if not ok:
        raise HTTPException(404, "key not found")
    return {"ok": True, "key_id": key_id, "state": "revoked"}