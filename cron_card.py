"""Static cards for cron deliveries: a scheduled job's output as one Card JSON 2.0 message.

Hermes delivers a cron job's text through the live adapter's ``send`` with ``metadata["job_id"]``
set; the bundled adapter turns that text into a ``post`` message, where headings and wide tables
read poorly.  ``build_cron_card`` lays the same text out as a card instead:

    header      the text's first line (a short title line, or a leading ``# heading``); a trailing
                ``（…）`` becomes the subtitle; red when the title (or a lone line) reports a failure, blue otherwise
    body        the rest, laid out like a completed streaming card (prose, headings, tables);
                an ``# H1`` right under the title line restates it and is dropped
    hr
    footer      "⏰ 定时任务 · 09-26 06:00"

Pure functions only; ``adapter.py`` decides when to use it and falls back to the bundled ``send``
when this returns None or Feishu rejects the card.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from .card_layout import CARD_JSON_MAX_BYTES, Labels, ZH, card_json_bytes, layout_answer_elements

TITLE_MAX_CHARS = 80
_LEADING_SYMBOLS_RE = re.compile(r"^[^\w\s（(【\[#*`|>-]+\s*")
_SUBTITLE_RE = re.compile(r"^(.+?)\s*[（(]([^（）()]+)[）)]\s*$")
_H1_RE = re.compile(r"^#\s+(.+?)\s*#*\s*$")
_NOT_A_TITLE_RE = re.compile(r"^\s*(?:#|\||[-*+]\s|\d+[.)、]\s|>|```|!\[)")
_FAILURE_RE = re.compile(r"失败|异常|错误|failed|failure|error", re.IGNORECASE)
_MEDIA_RE = re.compile(r"(?m)^\s*MEDIA:")


def split_title(text: str) -> Tuple[str, str, str]:
    """``(title, subtitle, body)``; title is empty when the text has no title line."""
    lines = text.strip().split("\n")
    first = lines[0].strip() if lines else ""
    rest = "\n".join(lines[1:]).strip("\n")
    heading = _H1_RE.match(first)
    if heading:
        title = heading.group(1)
    elif rest.strip() and len(first) <= TITLE_MAX_CHARS and not _NOT_A_TITLE_RE.match(first):
        title = first.strip("* ")
        body_lines = rest.lstrip("\n").split("\n")
        if _H1_RE.match(body_lines[0].strip()):  # "# 2026年9月25日 生产日报" under the title line
            rest = "\n".join(body_lines[1:]).strip("\n")
    else:
        return "", "", text.strip()
    title = _LEADING_SYMBOLS_RE.sub("", title).strip()
    subtitle = ""
    match = _SUBTITLE_RE.match(title)
    if match:
        title, subtitle = match.group(1).strip(), match.group(2).strip()
    return title, subtitle, rest


def build_cron_card(text: str, *, labels: Labels = ZH, sent_at: str = "") -> Optional[Dict[str, Any]]:
    """Card JSON 2.0 for one cron delivery, or None when the text should keep the bundled path
    (empty, carries ``MEDIA:`` attachments, or would exceed Feishu's card size limit)."""
    if not text or not text.strip() or _MEDIA_RE.search(text):
        return None
    title, subtitle, body = split_title(text)
    failed = bool(_FAILURE_RE.search(title or body.split("\n", 1)[0]))
    title = title or labels.scheduled
    elements = layout_answer_elements(body, labels, number_tables=False) if body.strip() else []
    footer = " · ".join(["⏰ " + labels.scheduled] + ([sent_at] if sent_at else []))
    elements += [{"tag": "hr"}, {"tag": "markdown", "text_size": "notation", "content": footer}]
    header: Dict[str, Any] = {"title": {"tag": "plain_text", "content": title},
                              "template": "red" if failed else "blue"}
    if subtitle:
        header["subtitle"] = {"tag": "plain_text", "content": subtitle}
    card = {
        "schema": "2.0",
        "config": {"width_mode": "fill", "summary": {"content": title}},
        "header": header,
        "body": {"elements": elements},
    }
    return card if card_json_bytes(card) <= CARD_JSON_MAX_BYTES else None


__all__ = ["build_cron_card", "split_title", "TITLE_MAX_CHARS"]
