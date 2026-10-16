"""Optional Feishu/Lark links from a successful, matching IM receipt.

The position-based protocol was reviewed against the MIT-licensed
baileyh8/hermes-feishu-streaming-card v5 message_navigation.py. This implementation
does not infer positions from message IDs and makes no extra network requests.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit


@dataclass(frozen=True)
class Navigation:
    message_id: str
    chat_id: str
    url: str


def _get(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def _position(value: Any) -> str | None:
    if type(value) not in (int, str) or not re.fullmatch(r"[0-9]{1,20}", str(value)):
        return None
    return str(value)


def navigation_from_result(result: Any, *, chat_id: str, thread_id: str | None = None,
                           domain: str = "feishu") -> Navigation | None:
    if not _get(result, "success"):
        return None
    host = {"feishu": "applink.feishu.cn", "lark": "applink.larksuite.com"}.get(domain)
    data = _get(_get(result, "raw_response"), "data")
    message_id = _get(data, "message_id")
    if (not host or not isinstance(message_id, str) or not message_id.startswith("om_")
            or message_id != _get(result, "message_id") or _get(data, "chat_id") != chat_id):
        return None
    receipt_thread = _get(data, "thread_id")
    if receipt_thread is not None and not isinstance(receipt_thread, str):
        return None
    if (receipt_thread or None) != (thread_id or None):
        return None
    path = "/client/thread/open" if thread_id else "/client/chat/open"
    position_key = "thread_position" if thread_id else "position"
    receipt_position = _get(data, "thread_message_position" if thread_id else "message_position")
    url = _get(data, "message_app_link")
    if not url:
        position = _position(receipt_position)
        if position is None:
            return None
        query = ({"open_thread_id": thread_id, "open_chat_id": chat_id} if thread_id
                 else {"openChatId": chat_id})
        query[position_key] = position
        url = f"https://{host}{path}?{urlencode(query)}"
    if (not isinstance(url, str) or len(url) > 2048
            or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url)):
        return None
    try:
        link = urlsplit(url)
        if (link.scheme != "https" or link.hostname != host or link.path != path
                or link.username or link.password or link.fragment or link.port not in (None, 443)):
            return None
        query = parse_qs(link.query, keep_blank_values=True, strict_parsing=True, max_num_fields=12)
    except (TypeError, ValueError):
        return None
    allowed = ({"open_thread_id", "openthreadid", "open_chat_id", "openchatid", position_key}
               if thread_id else {"openChatId", position_key})
    if set(query) - allowed or any(len(values) != 1 for values in query.values()):
        return None
    checks = [(('open_chat_id', 'openchatid'), chat_id),
              (('open_thread_id', 'openthreadid'), thread_id)] if thread_id else [(('openChatId',), chat_id)]
    for names, expected in checks:
        present = [query[name][0] for name in names if name in query]
        if not present or any(value != expected for value in present):
            return None
    position = _position(query.get(position_key, [None])[0])
    if position is None or (receipt_position is not None and _position(receipt_position) != position):
        return None
    return Navigation(message_id, chat_id, url)
