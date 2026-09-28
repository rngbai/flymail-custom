"""供可信下游服务读取邮件缓存的通用内部接口。"""
from __future__ import annotations

import hmac
import os
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from data_paths import DOWNLOADS_DIR
from db import (
    get_account_by_id,
    get_accounts,
    get_cached_message_detail,
    get_cached_messages_by_folder,
    list_cached_attachments,
    search_cached_messages_by_folder,
)
from errors import AppError
from providers.factory import ProviderFactory
from services.token import ensure_token
from utils.logger import get_logger

router = APIRouter(prefix="/internal/api", tags=["internal"])
logger = get_logger("routes.internal")


def _require_token(request: Request) -> None:
    expected = str(os.environ.get("FLYMAIL_SERVICE_TOKEN") or "").strip()
    supplied = str(request.headers.get("X-FlyMail-Service-Token") or "").strip()
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        raise AppError(401, "service token required")


def _account_id(value: str) -> str:
    return (value or os.environ.get("FLYMAIL_DEFAULT_ACCOUNT_ID") or "").strip()


def _message_uid(message_id: str, account_id: str) -> int:
    text = str(message_id or "").strip()
    if account_id and text.startswith(account_id + "_"):
        text = text[len(account_id) + 1 :]
    try:
        return int(text.rsplit("_", 1)[-1])
    except ValueError as exc:
        raise AppError(400, "invalid message id") from exc


def _safe_local_path(value: str) -> Path:
    candidate = Path(value).resolve()
    root = DOWNLOADS_DIR.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise AppError(403, "attachment is outside FlyMail storage") from exc
    return candidate


async def _account(value: str):
    account_id = _account_id(value)
    if not account_id:
        raise AppError(400, "account_id required")
    account = await get_account_by_id(account_id)
    if not account:
        raise AppError(404, "mail account not found")
    return account


@router.get("/accounts")
async def internal_accounts(request: Request):
    """返回账号摘要，不返回 OAuth Token、授权码或其他凭据。"""
    _require_token(request)
    accounts = await get_accounts("")
    return {
        "accounts": [
            {
                "id": account.id,
                "email": account.email,
                "provider": account.provider,
                "status": account.status,
                "remark": account.remark,
                "group_name": account.group_name,
            }
            for account in accounts
        ]
    }


@router.get("/messages")
async def internal_messages(
    request: Request,
    account_id: str = Query(""),
    folder: str = Query("INBOX"),
    query: str = Query(""),
    page: int = Query(1),
    page_size: int = Query(50),
    read_filter: str = Query(""),
    attachment_only: bool = Query(False),
):
    """分页读取或搜索已缓存邮件；不会在请求中主动连接外部邮箱。"""
    _require_token(request)
    account = await _account(account_id)
    folder = (folder or "INBOX").strip()
    query = (query or "").strip()
    read_filter = (read_filter or "").strip().lower()
    if not folder or len(folder) > 128:
        raise AppError(400, "folder invalid")
    if len(query) > 256:
        raise AppError(400, "query too long")
    if read_filter not in {"", "read", "unread"}:
        raise AppError(400, "read_filter invalid")
    page = max(1, page)
    page_size = min(200, max(1, page_size))
    loader = search_cached_messages_by_folder if query else get_cached_messages_by_folder
    args = [account.user_uid, account.id, folder]
    if query:
        args.append(query)
    result = await loader(
        *args,
        page=page,
        page_size=page_size,
        read_filter=read_filter,
        attachment_filter=attachment_only,
    )
    result["account_id"] = account.id
    result["folder"] = folder
    result["query"] = query
    return result


async def _detail(request: Request, message_id: str):
    _require_token(request)
    account = await _account(request.query_params.get("account_id") or "")
    folder = (request.query_params.get("folder") or "INBOX").strip()
    uid = _message_uid(message_id, account.id)
    detail = await get_cached_message_detail(account.id, uid, folder)
    if not detail:
        raise AppError(404, "message not found")
    detail["attachments"] = await list_cached_attachments(
        account.id, uid, detail.get("folder") or folder
    )
    return account, detail


@router.get("/messages/{message_id}")
async def internal_message_detail(request: Request, message_id: str):
    _, detail = await _detail(request, message_id)
    return detail


@router.get("/messages/{message_id}/attachments/{part_number}")
async def internal_attachment(
    request: Request, message_id: str, part_number: int
):
    _, detail = await _detail(request, message_id)
    attachment = next(
        (
            item
            for item in detail.get("attachments", [])
            if int(item.get("part_number") or 0) == int(part_number)
        ),
        None,
    )
    if not attachment or not attachment.get("local_path"):
        raise AppError(404, "attachment not cached")
    path = _safe_local_path(attachment["local_path"])
    if not path.is_file():
        raise AppError(404, "attachment file not found")
    filename = attachment.get("filename") or path.name
    return Response(
        content=path.read_bytes(),
        media_type=attachment.get("content_type") or "application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.get("/messages/{message_id}/raw")
async def internal_raw_message(request: Request, message_id: str):
    account, detail = await _detail(request, message_id)
    receiver = None
    try:
        receiver = ProviderFactory.get_receiver(account.provider)
        await receiver.connect(await ensure_token(account))
        raw = await receiver.fetch_raw_message(
            str(message_id), folder=detail.get("folder") or "INBOX"
        )
        return Response(
            content=raw,
            media_type="message/rfc822",
            headers={"X-FlyMail-Raw-Source": "remote"},
        )
    except Exception as exc:
        logger.warning(
            "remote raw fetch failed, fallback to cached reconstruction message=%s error=%s",
            message_id,
            type(exc).__name__,
        )
    finally:
        if receiver:
            try:
                await receiver.disconnect()
            except Exception:
                pass

    message = EmailMessage(policy=SMTP)
    if detail.get("from_addr"):
        message["From"] = detail["from_addr"]
    if detail.get("to_addr"):
        message["To"] = detail["to_addr"]
    message["Subject"] = detail.get("subject") or ""
    if detail.get("date"):
        message["Date"] = detail["date"]
    message.set_content(detail.get("body_text") or "")
    for attachment in detail.get("attachments", []):
        local_path = attachment.get("local_path") or ""
        if not local_path:
            continue
        path = _safe_local_path(local_path)
        if not path.is_file():
            continue
        content_type = str(
            attachment.get("content_type") or "application/octet-stream"
        )
        maintype, _, subtype = content_type.partition("/")
        message.add_attachment(
            path.read_bytes(),
            maintype=maintype or "application",
            subtype=subtype or "octet-stream",
            filename=attachment.get("filename") or path.name,
        )
    return Response(
        content=message.as_bytes(),
        media_type="message/rfc822",
        headers={"X-FlyMail-Raw-Source": "cached-reconstruction"},
    )
