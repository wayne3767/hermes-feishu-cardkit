"""Native card controls bound to the original admitted message, never callback claims."""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import logging
import json
from typing import Any, Optional
import uuid

logger = logging.getLogger("hermes_feishu_cardkit")
SOURCE_CACHE_MAX = 512
CLARIFY_ACTION = "clarify"
_PROMPTS_MAX = 64


@dataclass
class ClarifyCard:
    clarify_id: str
    binding: "SourceBinding"
    question: str
    choices: list[str]
    entry: Any
    message_id: str = ""
    notice: str = ""
    navigation: Any = None
    interactive: bool = True
    retired: bool = False


def clarify_card(state: ClarifyCard, *, language: str = "zh") -> dict:
    """Retain the question and real answer; settled cards never keep active buttons."""
    from tools import clarify_gateway as clarify
    with clarify._lock:
        return _clarify_card_snapshot(state, language=language)


def _clarify_card_snapshot(state: ClarifyCard, *, language: str) -> dict:
    """Render while holding the official resolver lock, with no I/O under that lock."""
    english = language == "en"
    notice = state.notice
    if getattr(state.entry, "event", None) and state.entry.event.is_set():
        response = state.entry.response
        if isinstance(response, str) and not response.startswith("\x00"):
            notice = ("Answer: " if english else "已选择：") + response
        elif not notice:
            notice = "No longer active" if english else "问题已失效"
    elements = [{"tag": "markdown", "content": state.question}]
    if notice:
        elements.append({"tag": "markdown", "content": "\n".join(
            f"{index}. {choice}" for index, choice in enumerate(state.choices, 1))})
        elements.append({"tag": "markdown", "content": notice})
    else:
        for index, choice in enumerate(state.choices):
            elements.append({
                "tag": "button", "text": {"tag": "plain_text", "content": choice}, "type": "default",
                "behaviors": [{"type": "callback", "value": {
                    "hermes_cardkit": CLARIFY_ACTION, "clarify_id": state.clarify_id, "choice": index}}],
            })
        elements.append({
            "tag": "button", "text": {"tag": "plain_text", "content": "Other" if english else "其他"},
            "type": "default", "behaviors": [{"type": "callback", "value": {
                "hermes_cardkit": CLARIFY_ACTION, "clarify_id": state.clarify_id, "choice": "other"}}],
        })
    if state.navigation is not None and notice:
        elements.append({
            "tag": "button", "text": {"tag": "plain_text", "content": "Continue reading" if english else "查看续答"},
            "type": "default", "behaviors": [{"type": "open_url", "default_url": state.navigation.url}],
        })
    return {"schema": "2.0", "config": {"wide_screen_mode": True},
            "header": {"template": "blue" if notice else "orange", "title": {
                "tag": "plain_text", "content": "Question" if english else "需要你确认"}},
            "body": {"elements": elements}}


@dataclass(frozen=True)
class SourceBinding:
    source: Any
    owner_open_id: str
    message_id: str
    session_key: str
    reply_anchor: Optional[str] = None


class CardInteractionMixin:
    """Reuse Hermes authorization while retaining the exact source of each card."""

    def _init_card_interactions(self) -> None:
        if not hasattr(self, "_card_sources"):
            self._card_sources: OrderedDict[str, SourceBinding] = OrderedDict()
            self._clarify_cards: OrderedDict[str, ClarifyCard] = OrderedDict()

    @property
    def _interaction_language(self) -> str:
        from .card_layout import EN
        return "en" if self._labels is EN else "zh"

    def _remember_card_source(self, event: Any) -> None:
        self._init_card_interactions()
        source = getattr(event, "source", None)
        message_id = str(getattr(event, "message_id", "") or "")
        raw_event = getattr(getattr(event, "raw_message", None), "event", None)
        sender_id = getattr(getattr(raw_event, "sender", None), "sender_id", None)
        open_id = str(getattr(sender_id, "open_id", "") or "")
        if not (source and message_id and open_id and getattr(source, "chat_id", None)):
            return
        # A callback must never become an inbound ownership record. Only real SDK messages do.
        raw_message = getattr(raw_event, "message", None)
        if str(getattr(raw_message, "message_id", "") or "") != message_id:
            return
        # Keep the original object until a real outgoing card binds it: Hermes admission may
        # assign source.profile while handling this very event. Snapshot only after routing.
        session_key = self._source_session_key(deepcopy(source))
        anchor = getattr(event, "reply_to_message_id", None) if source.thread_id else None
        self._card_sources[message_id] = SourceBinding(source, open_id, message_id, session_key, anchor)
        self._card_sources.move_to_end(message_id)
        while len(self._card_sources) > SOURCE_CACHE_MAX:
            self._card_sources.popitem(last=False)

    async def _handle_message_with_guards(self, event: Any) -> None:
        # The eventual output is the evidence this source passed admission; remembering it
        # beforehand is necessary because the first stream frame can run inside the handler.
        # Older Hermes versions lack the exact session-key helper. Losing an optional control
        # binding must never block ordinary inbound messages or guess their destination.
        if callable(getattr(self, "_source_session_key", None)):
            try:
                self._remember_card_source(event)
            except Exception:
                logger.debug("Card source binding unavailable", exc_info=True)
        await super()._handle_message_with_guards(event)

    async def handle_message(self, event: Any) -> None:
        # Feishu calls this *inside* its per-chat lock. A queued click can outlive its
        # original card while waiting for that lock, so checking only before the await
        # in _dispatch_bound_stop would let an old button stop a subsequent session turn.
        # This private object reference is set only by our authenticated callback path;
        # keep the bundled handler responsible for identity, authorization and dispatch.
        expected_turn = getattr(event, "_cardkit_stop_turn", None)
        if expected_turn is not None:
            current = self._stop_turn_from_event(getattr(getattr(event, "raw_message", None), "event", None))
            if current is not expected_turn:
                return
        await super().handle_message(event)

    def _bind_card_source(self, turn: Any, reply_to: Optional[str]) -> Optional[SourceBinding]:
        self._init_card_interactions()
        # A topic reply anchor may also be an earlier sender's raw message id. Treat
        # direct and alias matches equally: picking the old direct match would attach
        # the current card to the wrong owner and potentially another routed profile.
        candidates = [entry for entry in self._card_sources.values()
                      if reply_to and entry.source.chat_id == turn.chat_id
                      and (entry.message_id == reply_to or entry.reply_anchor == reply_to)]
        binding = candidates[0] if len(candidates) == 1 else None
        if binding:
            source = deepcopy(binding.source)
            binding = SourceBinding(source, binding.owner_open_id, binding.message_id,
                                    self._source_session_key(source), binding.reply_anchor)
        turn.control_source = binding
        turn.control_guard = (getattr(self, "_active_sessions", {}).get(binding.session_key)
                              if binding else None)
        turn.control_task = (getattr(self, "_session_tasks", {}).get(binding.session_key)
                             if binding else None)
        return binding

    def _can_stop_card(self, turn: Any) -> bool:
        binding, guard = getattr(turn, "control_source", None), getattr(turn, "control_guard", None)
        task = getattr(turn, "control_task", None)
        if (not isinstance(binding, SourceBinding) or guard is None
                or getattr(self, "_active_sessions", {}).get(binding.session_key) is not guard
                or task is None or getattr(self, "_session_tasks", {}).get(binding.session_key) is not task
                or not callable(getattr(task, "done", None)) or task.done()
                or guard.is_set() or getattr(turn, "segment_finalized", False)
                or getattr(turn, "sealed_at", None) is not None
                or getattr(turn, "observation_terminal", None) is True
                or getattr(turn, "phase", None) in {"done", "stopped", "failed", "blocked", "timeout"}):
            return False
        ref = getattr(turn, "observation_ref", None)
        if ref is None:
            return False
        from .lifecycle import OBSERVATIONS, scope_for_source
        snapshot = OBSERVATIONS.snapshot(ref)
        scope = scope_for_source(binding.source)
        if (snapshot is None or snapshot.terminal or not scope or snapshot.profile_scope != scope
                or snapshot.session_key != binding.session_key or snapshot.chat_id != turn.chat_id
                or snapshot.chat_id != binding.source.chat_id or snapshot.message_id != binding.message_id):
            return False
        active = OBSERVATIONS.active_for_session(scope, binding.session_key, turn.chat_id)
        return active is not None and active.ref == ref

    def _stop_turn_from_event(self, event: Any) -> Any:
        """Resolve only SDK message context; the callback value contains no authority."""
        context, operator = getattr(event, "context", None), getattr(event, "operator", None)
        message_id = str(getattr(context, "open_message_id", "") or "")
        chat_id = str(getattr(context, "open_chat_id", "") or "")
        open_id = str(getattr(operator, "open_id", "") or "")
        if not (message_id and chat_id and open_id):
            return None
        candidates = [turn for turn in tuple(getattr(self, "_stream_cards", {}).values())
                      if turn.message_id == message_id and turn.chat_id == chat_id]
        if len(candidates) != 1:
            return None
        turn = candidates[0]
        binding = getattr(turn, "control_source", None)
        if not self._can_stop_card(turn) or binding.owner_open_id != open_id:
            return None
        if self._source_session_key(deepcopy(binding.source)) != binding.session_key:
            return None
        validator = getattr(self, "_validate_card_action", None)
        if not callable(validator) or validator(event=event, state={"chat_id": chat_id},
                                              label="stop", ident=message_id) is None:
            return None
        return turn

    async def _dispatch_bound_stop(self, data: Any, expected_turn: Any) -> None:
        # Revalidate on the adapter loop: finalize/replacement can occur after the SDK callback.
        turn = self._stop_turn_from_event(getattr(data, "event", None))
        if turn is None or turn is not expected_turn:
            return
        from gateway.platforms.base import MessageEvent, MessageType
        binding = turn.control_source
        token = str(getattr(getattr(data, "event", None), "token", "") or "")
        event = MessageEvent(
            text="/stop", message_type=MessageType.COMMAND, source=deepcopy(binding.source),
            raw_message=data, message_id=token or str(uuid.uuid4()),
            channel_prompt=self._resolve_channel_prompt(binding.source.chat_id, binding.source.thread_id),
            timestamp=datetime.now(), metadata={"gateway_session_key": binding.session_key},
        )
        event._cardkit_stop_turn = expected_turn
        await self._handle_message_with_guards(event)

    def _interaction_binding(self, session_key: str, chat_id: str, metadata: Any) -> Optional[SourceBinding]:
        """Require an admitted active turn; never infer the owner from the latest chat message."""
        self._init_card_interactions()
        try:
            from .lifecycle import OBSERVATIONS, current_scope, scope_for_source
            request_scope = current_scope()
            if not request_scope:
                return None
            thread_id = (metadata or {}).get("thread_id")
            candidates = []
            for cached in self._card_sources.values():
                source = deepcopy(cached.source)
                scope = scope_for_source(source)
                if (not scope or scope != request_scope or source.chat_id != chat_id
                        or (source.thread_id or None) != (thread_id or None)
                        or self._source_session_key(source) != session_key):
                    continue
                snapshot = OBSERVATIONS.active_for_session(scope, session_key, chat_id)
                if (snapshot and snapshot.profile_scope == scope and snapshot.session_key == session_key
                        and snapshot.chat_id == chat_id and snapshot.message_id == cached.message_id):
                    candidates.append(SourceBinding(source, cached.owner_open_id, cached.message_id,
                                                    session_key, cached.reply_anchor))
        except (ImportError, AttributeError):
            return None
        return candidates[0] if len(candidates) == 1 else None

    async def send_clarify(self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
                           session_key: str, metadata: Optional[dict[str, Any]] = None) -> Any:
        # The bundled webhook handler discards synchronous card callback responses, so it
        # cannot reliably replace buttons with an answer receipt. Preserve its text path.
        if getattr(self, "_connection_mode", "") != "websocket":
            return await super().send_clarify(chat_id, question, choices, clarify_id, session_key, metadata)
        binding = self._interaction_binding(session_key, chat_id, metadata)
        if binding is None:
            return await super().send_clarify(chat_id, question, choices, clarify_id, session_key, metadata)
        from tools import clarify_gateway as clarify
        with clarify._lock:
            entry = clarify._entries.get(clarify_id)
            native = bool(entry and entry.session_key == session_key and not entry.event.is_set()
                          and not entry.multi_select and choices and 1 <= len(choices) <= 10)
        # Open-ended, multiple-select, unknown owner and future unsupported cases preserve the
        # bundled numbered-text path, including its standard free-text interception.
        if not native or binding is None or self._client is None:
            return await super().send_clarify(chat_id, question, choices, clarify_id, session_key, metadata)
        while len(self._clarify_cards) >= _PROMPTS_MAX:
            with clarify._lock:
                # Natural timeout removes the official entry without setting its Event. Other
                # text-input prompts remain live; pending sends reserve capacity until acked.
                settled = next((key for key, value in self._clarify_cards.items()
                                if value.message_id and (value.retired or value.entry.event.is_set()
                                    or clarify._entries.get(value.clarify_id) is not value.entry)), None)
            if settled is None:
                return await super().send_clarify(chat_id, question, choices, clarify_id, session_key, metadata)
            self._clarify_cards.pop(settled)
        state = ClarifyCard(clarify_id, binding, question, [str(choice) for choice in choices], entry)
        card = clarify_card(state, language=self._interaction_language)
        from .card_layout import CARD_JSON_MAX_BYTES
        payload = json.dumps(card, ensure_ascii=False)
        if len(payload.encode("utf-8")) > CARD_JSON_MAX_BYTES:
            return await super().send_clarify(chat_id, question, choices, clarify_id, session_key, metadata)
        self._clarify_cards[clarify_id] = state  # reserve capacity before the network await
        try:
            response = await self._feishu_send_with_retry(
                chat_id=chat_id, msg_type="interactive", payload=payload, reply_to=None, metadata=metadata)
        except BaseException:
            self._clarify_cards.pop(clarify_id, None)
            raise
        result = self._finalize_send_result(response, "clarify card send failed")
        if result.success and result.message_id:
            with clarify._lock:
                state.message_id = result.message_id
                self._clarify_cards.move_to_end(clarify_id)
                no_longer_pending = clarify._entries.get(clarify_id) is not state.entry
                settled = state.retired or state.entry.event.is_set() or no_longer_pending
                if no_longer_pending and not state.entry.event.is_set() and not state.notice:
                    state.retired = True
                    state.interactive = False
                    state.notice = "Question no longer active" if self._interaction_language == "en" else "问题已失效"
            if settled:
                # A timeout, cancellation or typed reply can win while IM send waits for its
                # acknowledgement. Refresh this exact delivered message once its id exists.
                await self._replace_clarify_card(state)
        else:
            self._clarify_cards.pop(clarify_id, None)
        return result

    def _on_clarify_card_action(self, event: Any, value: dict) -> Any:
        """The official resolver owns first-writer wins, expiry and the waiting agent."""
        self._init_card_interactions()
        clarify_id = value.get("clarify_id")
        state = self._clarify_cards.get(clarify_id) if isinstance(clarify_id, str) else None
        context, operator = getattr(event, "context", None), getattr(event, "operator", None)
        if (state is None or str(getattr(context, "open_message_id", "") or "") != state.message_id
                or str(getattr(context, "open_chat_id", "") or "") != state.binding.source.chat_id
                or str(getattr(operator, "open_id", "") or "") != state.binding.owner_open_id
                or self._source_session_key(deepcopy(state.binding.source)) != state.binding.session_key
                or self._validate_card_action(event=event, state={"chat_id": state.binding.source.chat_id},
                                              label="clarify", ident=state.clarify_id) is None):
            return self._card_response()
        from tools import clarify_gateway as clarify
        with clarify._lock:
            if not state.interactive:
                return self._card_response(clarify_card(state, language=self._interaction_language))
            live = clarify._entries.get(state.clarify_id)
            if live is not state.entry or live.event.is_set():
                if not state.entry.event.is_set():
                    state.notice = "Question no longer active" if self._interaction_language == "en" else "问题已失效"
                return self._card_response(clarify_card(state, language=self._interaction_language))
            choice = value.get("choice")
            if choice == "other":
                if clarify.mark_awaiting_text(state.clarify_id):
                    state.interactive = False
                    state.notice = "Reply in this conversation" if self._interaction_language == "en" else "请在当前会话中回复"
            elif type(choice) is int and 0 <= choice < len(state.choices):
                if clarify.resolve_gateway_clarify(state.clarify_id, state.choices[choice]):
                    state.notice = ""
            else:
                return self._card_response()
            return self._card_response(clarify_card(state, language=self._interaction_language))

    async def _replace_clarify_card(self, state: ClarifyCard) -> bool:
        """Use the existing IM update transport; failure never changes answer delivery."""
        try:
            from lark_oapi.api.im.v1 import UpdateMessageRequest, UpdateMessageRequestBody
            from tools import clarify_gateway as clarify
            with clarify._lock:
                message_id = state.message_id
                if not message_id:
                    return False  # a late successful send acknowledgement will refresh the state
                card = clarify_card(state, language=self._interaction_language)
            payload = json.dumps(card, ensure_ascii=False)
            from .card_layout import CARD_JSON_MAX_BYTES
            if len(payload.encode("utf-8")) > CARD_JSON_MAX_BYTES:
                return False
            body = UpdateMessageRequestBody.builder().content(payload).msg_type("interactive").build()
            request = UpdateMessageRequest.builder().message_id(message_id).request_body(body).build()
            response = await self._run_blocking(self._client.im.v1.message.update, request)
            return bool(self._response_succeeded(response))
        except Exception:
            logger.debug("clarify card refresh failed", exc_info=True)
            return False

    async def retire_clarify_card(self, clarify_id: str, notice: str) -> None:
        self._init_card_interactions()
        state = self._clarify_cards.get(clarify_id)
        if state is None:
            return
        from tools import clarify_gateway as clarify
        with clarify._lock:
            state.interactive = False
            state.retired = True
            response = state.entry.response
            if state.entry.event.is_set() and isinstance(response, str) and not response.startswith("\x00"):
                state.notice = ""  # a real typed answer supersedes the earlier "reply here" hint
            else:
                state.notice = notice
        await self._replace_clarify_card(state)

    async def _after_card_delivery(self, turn: Any, result: Any) -> None:
        """Only a successful continuation receipt can add a navigation control."""
        binding = getattr(turn, "control_source", None)
        if (not getattr(result, "success", False) or not isinstance(binding, SourceBinding)
                or not str(getattr(turn, "answer", "") or "").strip()):
            return
        self._init_card_interactions()
        from .navigation import navigation_from_result
        navigation = navigation_from_result(result, chat_id=turn.chat_id, thread_id=turn.thread_id,
                                            domain=getattr(self, "_domain_name", "feishu"))
        from tools import clarify_gateway as clarify
        for state in list(self._clarify_cards.values()):
            with clarify._lock:
                if (state.binding.message_id != binding.message_id or state.binding.session_key != binding.session_key
                        or not state.entry.event.is_set() or not isinstance(state.entry.response, str)
                        or state.entry.response.startswith("\x00")):
                    continue
                state.notice = ""
                state.navigation = navigation
            await self._replace_clarify_card(state)
