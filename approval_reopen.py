"""Keep the answer in a card after an approval prompt.

Hermes core finalizes the native stream at an approval boundary and then degrades the
transport to ``one send() at got_done`` — correct for edit-in-place platforms, where a
typing bubble that keeps updating above the prompt reads as a stuck message.  A CardKit
card is the opposite shape: the card *is* the answer and the approval prompt is its own
message, so the post-approval work belongs in a card again.  The consumer already
implements exactly that for clarify answers (``reopen=True``: finalize the current
stream, keep native streaming enabled, re-seed a fresh card on the next frame); the
approval branch simply never asks for it.

This module opts the card platform into the clarify treatment by wrapping the one method
the approval branch calls.  Nothing on disk is modified, the wrapper is scoped to
adapters that declare ``cardkit_approval_reopen = True``, and every failure path leaves
Hermes's own behaviour untouched (approvals then continue as ordinary messages).
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger("hermes_feishu_cardkit.approval_reopen")

_PATCHED_FLAG = "_hermes_cardkit_approval_reopen"
# Chats whose next finalize is an approval boundary (not the end of the turn), so the card
# seals as "本段已结束" even when no official observation proves it.
_BOUNDARY_TTL_SECONDS = 60.0
_boundaries: Dict[str, float] = {}


def consume_approval_boundary(chat_id: str) -> bool:
    """True once, if an approval boundary was opened for this chat moments ago."""
    now = time.monotonic()
    for key in [k for k, seen in _boundaries.items() if now - seen > _BOUNDARY_TTL_SECONDS]:
        _boundaries.pop(key, None)
    return _boundaries.pop(str(chat_id or ""), None) is not None


def _is_card_adapter(adapter: Any) -> bool:
    """True only for this plugin's adapter (it declares the opt-in on the mixin)."""
    return bool(getattr(adapter, "cardkit_approval_reopen", False) is True)


def install_approval_reopen(*, consumer_cls: Optional[type] = None) -> bool:
    """Wrap ``GatewayStreamConsumer.close_for_approval_prompt`` once; True when active.

    ``consumer_cls`` is injectable for tests.  Idempotent: a second call reports the state
    of the existing wrapper instead of stacking another one.
    """
    if consumer_cls is None:
        try:
            from gateway.stream_consumer import GatewayStreamConsumer as consumer_cls
        except Exception as exc:  # older Hermes without the native-stream consumer
            logger.debug("Approval reopen unavailable: %s", exc)
            return False
    original = getattr(consumer_cls, "close_for_approval_prompt", None)
    if not callable(original):
        logger.debug("Approval reopen unavailable: consumer has no close_for_approval_prompt")
        return False
    if getattr(original, _PATCHED_FLAG, False):
        return True

    def close_for_approval_prompt(self, placeholder=None, reason="Approval", reopen=False):
        # Clarify already asks for reopen; approvals never do. Only our card platform
        # turns its approval boundary into a re-openable one.
        if not reopen and reason != "Clarify" and _is_card_adapter(getattr(self, "adapter", None)):
            reopen = True
            # Only a boundary that will actually finalize an open stream needs the segment
            # marker; otherwise a later turn in the same chat could inherit it.
            if getattr(self, "_use_native_streaming", False) and getattr(self, "_native_stream_opened", False):
                _boundaries[str(getattr(self, "chat_id", "") or "")] = time.monotonic()
        return original(self, placeholder, reason=reason, reopen=reopen)

    setattr(close_for_approval_prompt, _PATCHED_FLAG, True)
    setattr(close_for_approval_prompt, "__wrapped__", original)
    consumer_cls.close_for_approval_prompt = close_for_approval_prompt
    logger.info("Approval boundaries keep native streaming: post-approval work continues in a card")
    return True


__all__ = ["install_approval_reopen"]
