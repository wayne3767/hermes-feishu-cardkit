"""``CardkitFeishuAdapter``: Hermes's bundled Feishu adapter plus native streaming cards.

The bundled adapter (``plugins/platforms/feishu/adapter.py`` in Hermes) keeps doing everything it
does — connection, admission, media, approvals.  This subclass layers ``FeishuStreamingCardMixin``
on top and supplies the few pieces the mixin needs from the adapter side: the settings, the
CardKit request builders, the inbound thread cache and a clean shutdown.

Only public-ish seams of the bundled adapter are used (listed in ``REQUIRED_BASE_ATTRS``); when a
future Hermes drops one of them, ``base_is_compatible`` reports it and ``__init__.py`` falls back
to the bundled adapter instead of breaking the channel.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Dict, Optional

from plugins.platforms.feishu import adapter as _bundled

from .card_layout import STOP_ACTION
from .cron_card import DEFAULT_TEMPLATE, TEMPLATES, build_cron_card
from .streaming import FeishuStreamingCardMixin

logger = logging.getLogger("hermes_feishu_cardkit")

# Bundled-adapter seams the mixin relies on.  Kept short on purpose: everything else goes through
# the public BasePlatformAdapter contract (send_stream_frame, send_multiple_images, extract_media).
REQUIRED_BASE_ATTRS = (
    "_client", "_run_blocking", "_feishu_send_with_retry", "_finalize_send_result", "_response_succeeded",
    "_extract_response_field", "_handle_message_event_data", "disconnect",
)
# Seams the stop button needs on top of those; without them the button is simply not shown.
STOP_BUTTON_ATTRS = ("_on_card_action_trigger", "_loop_accepts_callbacks", "_submit_on_loop", "_dispatch_synthetic_event")
_CARDKIT_MODULE = "lark_oapi.api.cardkit.v1"


def base_is_compatible() -> Optional[str]:
    """None when the bundled adapter exposes every seam we use, else a short reason."""
    missing = [name for name in REQUIRED_BASE_ATTRS
               if not hasattr(_bundled.FeishuAdapter, name) and name != "_client"]
    return f"bundled FeishuAdapter lacks {', '.join(missing)}" if missing else None


def _truthy(value: Any) -> bool:
    return value is True or value == 1 or str(value).strip().lower() in {"true", "1", "yes", "on"}


def _setting(extra: dict, key: str, env: str, default: str) -> Any:
    """config.yaml ``platforms.feishu.<key>`` wins over the env var (profile-scoped when Hermes offers it)."""
    if key in extra and extra[key] is not None:
        return extra[key]
    scoped = getattr(_bundled, "_get_scoped_secret", None)
    try:
        value = scoped(env, default) if scoped else os.getenv(env, default)
    except Exception:
        value = os.getenv(env, default)
    return value if value not in (None, "") else default


def _now() -> datetime:
    """Hermes's configured timezone (``timezone`` in config.yaml), not the server clock's zone."""
    try:
        from hermes_time import now
        return now()
    except Exception:
        return datetime.now().astimezone()


def _cardkit():
    return importlib.import_module(_CARDKIT_MODULE)


def _build(request_cls: Any, **fields: Any) -> Any:
    """``request_cls.builder().<field>(value)...build()``."""
    builder = request_cls.builder()
    for name, value in fields.items():
        builder = getattr(builder, name)(value)
    return builder.build()


class CardkitFeishuAdapter(FeishuStreamingCardMixin, _bundled.FeishuAdapter):
    """The bundled Feishu adapter with CardKit streaming cards."""

    def __init__(self, config: Any) -> None:
        super().__init__(config)
        extra = getattr(config, "extra", None) or {}
        self._streaming_card = _truthy(_setting(extra, "streaming_card", "FEISHU_STREAMING_CARD", "true"))
        self._card_math_images = _truthy(_setting(extra, "card_math_images", "FEISHU_CARD_MATH_IMAGES", "true"))
        self._card_locale = str(_setting(extra, "card_locale", "FEISHU_CARD_LOCALE", "")).strip().lower()
        self._cron_card = _truthy(_setting(extra, "cron_card", "FEISHU_CRON_CARD", "true"))
        template = str(_setting(extra, "cron_card_template", "FEISHU_CRON_CARD_TEMPLATE", DEFAULT_TEMPLATE)).strip().lower()
        if template not in TEMPLATES:
            logger.warning("[Feishu] unknown cron card template %r; using %s", template, DEFAULT_TEMPLATE)
            template = DEFAULT_TEMPLATE
        self._cron_card_template = template
        self._stop_button = (_truthy(_setting(extra, "card_stop_button", "FEISHU_CARD_STOP_BUTTON", "true"))
                             and all(hasattr(_bundled.FeishuAdapter, name) for name in STOP_BUTTON_ATTRS))

    # --- Stop button -------------------------------------------------------------------------------

    def _on_card_action_trigger(self, data: Any) -> Any:
        """A click on a generating card's stop button becomes "/stop" from the clicker, through the
        same guarded pipeline as a typed command (so the usual authorization applies and the stop
        reaches the clicker's own session).  Every other click goes to the bundled handler."""
        event = getattr(data, "event", None)
        value = getattr(getattr(event, "action", None), "value", None) or {}
        if not (isinstance(value, dict) and value.get("hermes_cardkit") == STOP_ACTION["hermes_cardkit"]):
            return super()._on_card_action_trigger(data)
        context, operator = getattr(event, "context", None), getattr(event, "operator", None)
        chat_id = str(getattr(context, "open_chat_id", "") or "")
        open_id = str(getattr(operator, "open_id", "") or "")
        token = str(getattr(event, "token", "") or "")
        duplicate = getattr(self, "_is_card_action_duplicate", None)
        if token and callable(duplicate) and duplicate(token):
            return self._toast("info", self._labels.stop_toast)
        loop = getattr(self, "_loop", None)
        if not chat_id or not open_id or not self._loop_accepts_callbacks(loop):
            logger.warning("[Feishu] stop button click dropped (chat=%r, operator present=%s)", chat_id, bool(open_id))
            return self._toast("error", self._labels.stop_failed)
        from gateway.platforms.base import MessageType
        logger.info("[Feishu] stop button clicked by %s in %s; dispatching /stop", open_id, chat_id)
        self._submit_on_loop(loop, self._dispatch_synthetic_event(
            text="/stop", message_type=MessageType.COMMAND, chat_id=chat_id,
            sender_id=SimpleNamespace(open_id=open_id, user_id=None, union_id=None), event_chat_type="group",
            raw_message=data, message_id=token or str(uuid.uuid4()),
        ))
        return self._toast("info", self._labels.stop_toast)

    @staticmethod
    def _toast(kind: str, content: str) -> Any:
        """Card-callback response carrying only a toast (a streaming card cannot be updated inline)."""
        try:
            from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTriggerResponse
        except Exception:
            return None
        return P2CardActionTriggerResponse({"toast": {"type": kind, "content": content}})

    # --- Cron deliveries as cards ------------------------------------------------------------------

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: Optional[Dict[str, Any]] = None) -> Any:
        """Cron deliveries (Hermes marks them with ``metadata["job_id"]``) go out as one static card;
        everything else, and any card Feishu rejects, takes the bundled path unchanged."""
        if self._cron_card and self._client is not None and "job_id" in (metadata or {}):
            card = build_cron_card(content, labels=self._labels, sent_at=_now().strftime("%m-%d %H:%M"),
                                   template=self._cron_card_template)
            if card is not None:
                try:
                    response = await self._feishu_send_with_retry(
                        chat_id=chat_id, msg_type="interactive", payload=json.dumps(card, ensure_ascii=False),
                        reply_to=reply_to, metadata=metadata,
                    )
                    result = self._finalize_send_result(response, "cron card send failed")
                    if result.success:
                        return result
                    logger.warning("[Feishu] cron card for job %s rejected (%s); sending as a regular message",
                                   metadata["job_id"], result.error)
                except Exception as exc:
                    logger.warning("[Feishu] cron card for job %s failed (%s); sending as a regular message",
                                   metadata["job_id"], exc)
        return await super().send(chat_id, content, reply_to=reply_to, metadata=metadata)

    # --- CardKit SDK -------------------------------------------------------------------------------

    @staticmethod
    def _cardkit_available() -> bool:
        try:
            _cardkit()
            return True
        except Exception:
            return False

    @staticmethod
    def _build_card_create_request(card_json: str) -> Any:
        ck = _cardkit()
        return _build(ck.CreateCardRequest, request_body=_build(ck.CreateCardRequestBody, type="card_json", data=card_json))

    @staticmethod
    def _build_card_content_request(*, card_id: str, element_id: str, content: str, sequence: int, uuid_value: str) -> Any:
        ck = _cardkit()
        body = _build(ck.ContentCardElementRequestBody, uuid=uuid_value, content=content, sequence=sequence)
        return _build(ck.ContentCardElementRequest, card_id=card_id, element_id=element_id, request_body=body)

    @staticmethod
    def _build_card_settings_request(*, card_id: str, settings: str, sequence: int, uuid_value: str) -> Any:
        ck = _cardkit()
        body = _build(ck.SettingsCardRequestBody, uuid=uuid_value, settings=settings, sequence=sequence)
        return _build(ck.SettingsCardRequest, card_id=card_id, request_body=body)

    @staticmethod
    def _build_card_update_request(*, card_id: str, card_json: str, sequence: int, uuid_value: str) -> Any:
        ck = _cardkit()
        card = _build(ck.Card, type="card_json", data=card_json)
        body = _build(ck.UpdateCardRequestBody, uuid=uuid_value, card=card, sequence=sequence)
        return _build(ck.UpdateCardRequest, card_id=card_id, request_body=body)

    @staticmethod
    def _build_card_batch_update_request(*, card_id: str, actions_json: str, sequence: int, uuid_value: str) -> Any:
        ck = _cardkit()
        body = _build(ck.BatchUpdateCardRequestBody, uuid=uuid_value, sequence=sequence, actions=actions_json)
        return _build(ck.BatchUpdateCardRequest, card_id=card_id, request_body=body)

    # --- Image upload (own builders so an upstream rename cannot break attachments) ----------------

    @staticmethod
    def _build_image_upload_body(*, image_type: str, image: Any) -> Any:
        im = importlib.import_module("lark_oapi.api.im.v1")
        return _build(im.CreateImageRequestBody, image_type=image_type, image=image)

    @staticmethod
    def _build_image_upload_request(request_body: Any) -> Any:
        im = importlib.import_module("lark_oapi.api.im.v1")
        return _build(im.CreateImageRequest, request_body=request_body)

    # --- Hooks into the bundled lifecycle -----------------------------------------------------------

    async def _handle_message_event_data(self, data: Any) -> None:
        """Remember the inbound message's topic so a card reply stays inside it."""
        message = getattr(getattr(data, "event", None), "message", None)
        if message is not None:
            self.remember_thread_for_message(
                getattr(message, "message_id", None),
                getattr(message, "thread_id", None) or getattr(message, "root_id", None),
            )
        await super()._handle_message_event_data(data)

    async def disconnect(self) -> None:
        """Seal open cards while the SDK pool is still alive, then the bundled teardown."""
        try:
            await self.close_open_stream_cards()
        except Exception as exc:
            logger.debug("closing streaming cards on disconnect failed: %s", exc)
        await super().disconnect()


__all__ = ["CardkitFeishuAdapter", "base_is_compatible", "REQUIRED_BASE_ATTRS"]
