"""ClawBot HTTP client. Errors never include the bot token or a context token."""

from __future__ import annotations

import base64
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Protocol

from ..http import quiet_client

logger = logging.getLogger(__name__)

ILINK_BASE = "https://ilinkai.weixin.qq.com"
# Longest diagnostic kept in the DB/log. p11e: 200 characters, enough for a
# ret/errcode/errmsg summary without ever storing a token.
_ERROR_LIMIT = 200

# p11e redaction. Diagnostics may echo a URL or a token-bearing parameter from
# an upstream error; nothing that reaches ``last_error`` or the log may keep it.
_URL_QUERY_RE = re.compile(r"(https?://[^\s?]+)\?[^\s]*", re.IGNORECASE)
_BEARER_RE = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_SECRET_PARAM_RE = re.compile(
    r"(?i)\b(token|access_token|context_token|api_key|apikey|key|secret|password|authorization|signature|sig)(\s*[=:]\s*)([^&\s,;\"']+)"
)
_LONG_SECRET_RE = re.compile(r"\b[A-Za-z0-9_+/=-]{32,}\b")
_SK_SECRET_RE = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b", re.IGNORECASE)


@dataclass(frozen=True)
class SendResult:
    ok: bool
    ret: int | None = None
    error: str | None = None
    # p11e: keep the raw (but short) upstream diagnostics. ``ret`` above stays
    # the delivery code used by the queue state machine.
    errcode: int | None = None
    errmsg: str | None = None
    http_status: int | None = None


@dataclass(frozen=True)
class InboundMessage:
    user_key: str
    text: str
    context_token: str


@dataclass(frozen=True)
class UpdatesResult:
    msgs: tuple[InboundMessage, ...]
    new_buf: str
    ok: bool = True


class WechatClient(Protocol):
    def send_message(self, context_token: str, text: str) -> SendResult: ...

    def get_updates(self, buf: str, timeout_sec: int = 35) -> UpdatesResult: ...


def redact_send_text(value: object, limit: int = _ERROR_LIMIT) -> str:
    """Collapse a diagnostic to one safe line.

    Removes URL query strings, Bearer credentials, named token/key parameters,
    and long secret-looking blobs. Never pass the bot token or a context token
    through here expecting it to be "masked": the point is that they do not
    survive at all.
    """
    text = " ".join(str(value).split())
    text = _URL_QUERY_RE.sub(r"\1?***", text)
    text = _BEARER_RE.sub("Bearer ***", text)
    text = _SECRET_PARAM_RE.sub(r"\1\2***", text)
    text = _SK_SECRET_RE.sub("***", text)
    text = _LONG_SECRET_RE.sub("***", text)
    return text[: max(int(limit), 0)]


def _short_error(value: object) -> str:
    return redact_send_text(value)


@dataclass
class MockWechatClient:
    """Records sends in memory. Never opens a socket."""

    sent: list[dict[str, str]] = field(default_factory=list)

    def send_message(self, context_token: str, text: str) -> SendResult:
        self.sent.append({"context_token": context_token, "text": text})
        return SendResult(ok=True, ret=0)

    def get_updates(self, buf: str, timeout_sec: int = 35) -> UpdatesResult:
        del timeout_sec
        return UpdatesResult(msgs=(), new_buf=buf or "")


_CHANNEL_VERSION = "asset-tracker/0.3"
_keys_logged = False


def _headers(token: str) -> dict[str, str]:
    uin = base64.b64encode(str(int.from_bytes(os.urandom(4), "big")).encode()).decode()
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": uin,
    }


def _extract_text(item: dict) -> str:
    text = item.get("text") or item.get("content") or ""
    if isinstance(text, dict):
        text = text.get("text") or ""
    if text:
        return str(text)
    for part in item.get("item_list") or []:
        if not isinstance(part, dict):
            continue
        inner = part.get("text_item")
        if isinstance(inner, dict) and inner.get("text"):
            return str(inner["text"])
    return ""


class LiveWechatClient:
    """POST to iLink. Callers must not log the token or the returned context token."""

    def __init__(
        self,
        bot_token: str,
        base_url: str = ILINK_BASE,
        to_user_id: str = "",
        bot_id: str = "",
    ) -> None:
        self._token = bot_token
        self._base = (base_url or ILINK_BASE).rstrip("/")
        self._to_user_id = to_user_id
        self._bot_id = bot_id

    def send_message(self, context_token: str, text: str) -> SendResult:
        if not context_token:
            return SendResult(ok=False, error="no_context_token")
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": self._to_user_id,
                "client_id": base64.b16encode(os.urandom(8)).decode().lower(),
                "message_type": 2,
                "message_state": 2,
                "context_token": context_token,
                "item_list": [{"type": 1, "text_item": {"text": text}}],
            },
            "base_info": {"channel_version": _CHANNEL_VERSION},
        }
        try:
            response = quiet_client(timeout=20).post(
                f"{self._base}/ilink/bot/sendmessage",
                headers=_headers(self._token),
                json=body,
            )
        except Exception as exc:  # noqa: BLE001 - network failure stays a short label
            return SendResult(ok=False, error=_short_error(type(exc).__name__))
        if response.status_code >= 400:
            return SendResult(
                ok=False,
                ret=response.status_code,
                error=f"http_{response.status_code}",
                http_status=response.status_code,
            )
        try:
            parsed = response.json()
        except Exception:  # noqa: BLE001
            # An empty 200 body is how iLink acknowledges a delivered message.
            if not response.content:
                return SendResult(ok=True, ret=0, http_status=response.status_code)
            return SendResult(ok=False, error="bad_response", http_status=response.status_code)
        ret = parsed.get("ret") if isinstance(parsed, dict) else None
        errcode = parsed.get("errcode") if isinstance(parsed, dict) else None
        errmsg = (
            parsed.get("errmsg") or parsed.get("err_msg") or parsed.get("message")
            if isinstance(parsed, dict)
            else None
        )
        code = ret if ret not in (None, 0) else errcode
        try:
            code_int = int(code) if code is not None else None
        except (TypeError, ValueError):
            code_int = None
        try:
            errcode_int = int(errcode) if errcode is not None else None
        except (TypeError, ValueError):
            errcode_int = None
        if code_int not in (None, 0):
            return SendResult(
                ok=False,
                ret=code_int,
                error=f"ret={code_int}",
                errcode=errcode_int,
                errmsg=_short_error(errmsg) if errmsg else None,
                http_status=response.status_code,
            )
        return SendResult(ok=True, ret=0, http_status=response.status_code)

    def get_updates(self, buf: str, timeout_sec: int = 35) -> UpdatesResult:
        global _keys_logged
        try:
            response = quiet_client(timeout=timeout_sec + 10).post(
                f"{self._base}/ilink/bot/getupdates",
                headers=_headers(self._token),
                json={
                    "get_updates_buf": buf or "",
                    "base_info": {"channel_version": _CHANNEL_VERSION},
                },
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:  # noqa: BLE001
            logger.warning("wechat getupdates failed (%s)", type(exc).__name__)
            return UpdatesResult(msgs=(), new_buf=buf or "", ok=False)
        if not isinstance(body, dict):
            return UpdatesResult(msgs=(), new_buf=buf or "", ok=False)
        if not _keys_logged:
            _keys_logged = True
            logger.info("wechat getupdates response keys=%s", sorted(body.keys()))
        code = body.get("ret") if body.get("ret") not in (None, 0) else body.get("errcode")
        if code not in (None, 0):
            logger.warning("wechat getupdates ret=%s", code)
            return UpdatesResult(msgs=(), new_buf=buf or "", ok=False)
        new_buf = str(body.get("get_updates_buf") or body.get("buf") or buf or "")
        raw_msgs = body.get("msgs") or body.get("messages") or []
        parsed: list[InboundMessage] = []
        if isinstance(raw_msgs, list):
            for item in raw_msgs:
                message = _parse_inbound(item)
                if message is not None:
                    parsed.append(message)
        return UpdatesResult(msgs=tuple(parsed), new_buf=new_buf)


def _parse_inbound(item: object) -> InboundMessage | None:
    if not isinstance(item, dict):
        return None
    # Skip the bot's own echoes (message_type 2); user messages are type 1.
    if item.get("message_type") == 2:
        return None
    text = _extract_text(item)
    context = str(item.get("context_token") or "")
    user_key = str(
        item.get("from_user_id")
        or item.get("from_user")
        or item.get("user_id")
        or item.get("openid")
        or item.get("ilink_user_id")
        or ""
    )
    return InboundMessage(user_key=user_key, text=text, context_token=context)


def get_wechat_client() -> WechatClient:
    """mock → Mock. live without a token degrades to Mock and logs a warning (no secret)."""
    from ...config import settings

    if settings.wechat_push_mode != "live":
        return MockWechatClient()
    if not settings.wechat_bot_token:
        logger.warning("无 WECHAT_BOT_TOKEN，降级 mock")
        return MockWechatClient()
    return LiveWechatClient(
        settings.wechat_bot_token,
        base_url=settings.wechat_ilink_baseurl or ILINK_BASE,
        to_user_id=settings.wechat_ilink_user_id,
        bot_id=settings.wechat_ilink_bot_id,
    )
