"""Feishu native streaming: one CardKit streaming card per turn.

Feishu has no typing indicator and ``im.v1.message.update`` re-renders a whole ``post`` on every
edit, so the edit transport reads as a message that keeps flickering.  CardKit's streaming mode
(Feishu 7.20+) gives the real thing: a card entity is created with ``streaming_mode: true``, sent
as an ``interactive`` message, then text is pushed into the answer element and the client renders
a typewriter animation between frames.  The final full ``card.update`` seals it with the completed
layout (see ``card_layout.py``).

Contract with ``GatewayStreamConsumer`` (gateway/stream_consumer_transport.py):
- the empty seed frame opens the card (typing before the first token);
- intermediate frames are cumulative and fire-and-forget (a later frame overwrites); the consumer
  composes them as ``answer + "\\n\\n---\\n" + tool-progress lines`` and ``split_stream_frame``
  takes that apart again so only the latest tool activity appears above the answer;
- a ``finalize=True`` frame must either land the text or return False so the consumer's
  send()/edit fallback delivers exactly once;
- ``finalize`` on an unknown turn returns False (never seed-and-close).

After the turn the gateway hands attachments to ``send_multiple_images``; images then go INTO the
completed card, each where its ``MEDIA:`` line stood (``extract_media`` keeps the raw response so
the positions are known), captioned ``图N　标题``.

Limits: CardKit allows 10 updates/s per card and auto-closes streaming after 10 minutes; past
that window a turn keeps updating the same card with full ``card.update`` writes (no typewriter).
Card JSON is capped at 30 KB by Feishu: every card is measured as serialized, and
an oversized answer is cut at a paragraph boundary; a final that no longer fits is
handed back to the consumer (return False) and goes out as ordinary chunked messages below the card.
The final seal retries, then falls back to a slimmer card, so a card is not left "生成中".

The consumer never finalizes a cancelled turn and has no per-turn abandon hook for native streams
(``_abandon_native_stream`` only serves drafts), so an idle watchdog seals a silent card as "stopped";
``interrupt_session_activity`` (/stop, /new) shortens its wait to a few seconds, and a frame for an
idle-sealed turn revives the same card rather than opening a second one.
The user-facing design was pioneered by baileyh8/hermes-feishu-streaming-card (MIT) as an
out-of-tree patch; see ``card_layout.py`` for what is shared with it (wording only).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import io
import logging
import os
import time
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import unquote

from .card_layout import (
    ACTIVITY_MAX_CHARS, ACTIVE_PHASES, ANSWER_ELEMENT_ID, CARD_JSON_MAX_BYTES, CARD_MAX_BYTES, PHASE_DONE,
    PHASE_RUNNING, PHASE_STOPPED, PHASE_UNKNOWN, PHASE_WAITING_APPROVAL, PHASE_WAITING_INPUT,
    PHASE_BLOCKED, PHASE_TIMEOUT, PHASE_FAILED, PHASE_SEGMENT, ImageItem, Labels, append_images, build_stream_card, card_json_bytes, clip_markdown_head,
    clip_markdown_tail, estimate_tokens, footer_update_actions, format_speed, json_text_bytes, labels_for, place_images,
    render_answer, render_footer, render_activity, status_label, summary_for, activity_update_actions, utf8_len,
)
from . import approval_reopen
from .math_image import formula_digest, mathtext_available, render_formula_png
from .math_text import convert_math, hide_open_formula, iter_block_formulas

logger = logging.getLogger("hermes_feishu_cardkit")

# Streaming mode auto-closes server-side after 10 min; past this a content push may be refused,
# so the turn switches to full card.update frames.
STREAMING_TTL_SECONDS = 570.0
# Feishu "app has no permission to access this API": the app lacks ``cardkit:card:write``.
NO_PERMISSION_CODE = 99991672
# Consecutive open failures before the adapter pauses the transport; a permission error disables it
# for the adapter's lifetime, anything else for OPEN_FAILURE_COOLDOWN_SECONDS (then one retry).
MAX_OPEN_FAILURES = 3
OPEN_FAILURE_COOLDOWN_SECONDS = 600.0
# A completed card accepts the turn's post-stream images for this long after it was sealed.
COMPLETED_CARD_TTL_SECONDS = 90.0
# Images whose raw response matches no card go to the chat's only fresh card sealed this recently.
UNMATCHED_ATTACH_WINDOW_SECONDS = 10.0
MAX_EMBEDDED_IMAGES = 6
# The raw response seen by extract_media() is only trusted for the delivery that follows it.
PENDING_MEDIA_TTL_SECONDS = 30.0
# Raw responses seen by extract_media() but not yet claimed by a delivery (several chats may
# finish at once); sealed cards kept for attachment matching.
_PENDING_MEDIA_MAX = 8
_COMPLETED_CARDS_MAX = 32
# A turn the consumer never finalizes (cancelled, /stop, crashed) is sealed as "stopped" once it has
# sent no frame for IDLE_SEAL_SECONDS (STOP_IDLE_SECONDS after an interrupt in its chat); a later
# frame for the same turn revives the card within _REVIVE_TTL_SECONDS.  /stop and /new reach the
# adapter through interrupt_session_activity, so the long window only covers crashes and silent
# cancels; a turn silent that long while alive (one very long tool call) flips to "stopped" and back.
IDLE_SEAL_SECONDS = 300.0
STOP_IDLE_SECONDS = 5.0
_WATCHDOG_POLL_SECONDS = 5.0
_REVIVE_TTL_SECONDS = 1800.0
# The final full update is retried after these delays before the slimmer fallbacks.
FINAL_UPDATE_RETRY_DELAYS = (1.0, 3.0)
# Headroom kept for tool lines that arrive after the last answer push.
_ACTIVITY_GROWTH_RESERVE = 1024
# Live decode speed: a windowed estimate over the answer text landed so far; official usage
# replaces it once a call finishes.  ``decode_seconds`` accumulates only the gaps between frames
# that actually grew the answer, so tool time never drags the average down.
SPEED_WINDOW_SECONDS = 12.0
SPEED_MIN_SPAN_SECONDS = 1.0
SPEED_MAX_IDLE_SECONDS = 12.0
SPEED_MAX_GAP_SECONDS = 10.0  # a longer gap between text frames is tool time, not decoding
SPEED_AVERAGE_MIN_SECONDS = 0.5
# The footer (status, elapsed, speed) refreshes at most this often while text streams.
FOOTER_TICK_SECONDS = 2.0
FORMULA_IMAGE_WAIT_SECONDS = 8.0  # finalize waits this long for in-flight formula uploads
_THREAD_CACHE_MAX = 512
_FORMULA_IMAGE_CACHE_MAX = 256
# GatewayStreamConsumer._compose_frame_content joins answer and tool-progress lines with this.
_PROGRESS_SEPARATOR = "\n\n---\n"
# Interim frames end with the consumer's cursor (▌ by default); it lands after the overlay.
_CURSOR_CHARS = "▌▍▎▏█▉▊▋"
# Tool-progress lines open with the tool emoji (get_tool_emoji / friendly verbs: 🔍 💻 📖 ⚙️ …).
_EMOJI_RANGES = ((0x1F000, 0x1FAFF), (0x2600, 0x27BF), (0x2300, 0x23FF), (0x2B00, 0x2BFF), (0x2190, 0x21FF), (0x2900, 0x297F))


# --- Turn model -------------------------------------------------------------------------------

@dataclass
class StreamCardTurn:
    """One open streaming card: ``sequence`` must increase across every CardKit write."""
    card_id: str
    message_id: str
    chat_id: str
    sequence: int = 0
    answer: str = ""  # last answer text landed in the answer element (cursor included)
    timeline: List[str] = field(default_factory=list)  # latest fallback activity only, never full history
    tool_total: int = 0
    tool_activity: str = ""
    activity_kind: str = ""  # official thinking/tooling, or generating after new text
    tools_observed: bool = False
    activity_dirty: bool = False
    finished_at: Optional[float] = None
    overlay: List[str] = field(default_factory=list)  # tool lines the consumer currently shows
    started_at: float = field(default_factory=time.monotonic)
    formula_tasks: Dict[str, "asyncio.Task[Optional[str]]"] = field(default_factory=dict)  # digest → upload
    watchdog: Optional["asyncio.Task[None]"] = None  # seals the card if the turn is abandoned
    thread_id: Optional[str] = None
    revivable: bool = False  # keyed by a real turn_id: a frame after an idle seal may revive it
    last_frame_at: float = field(default_factory=time.monotonic)
    stop_requested_at: Optional[float] = None  # an interrupt (/stop, /new) reached this chat
    sealed_at: Optional[float] = None  # idle-sealed as "stopped" (parked for revival)
    streaming_closed: bool = False  # streaming mode is off: frames go out as full card.update writes
    answer_clipped: bool = False  # the answer element shows only the newest part of ``answer``
    dirty: bool = False  # a full-update frame is owed (failed write, or revived card still grey)
    revivals: int = 0  # bumped on revival; an idle seal still in flight for an older life gives up
    reply_to: Optional[str] = None
    phase: str = PHASE_RUNNING
    status_word: str = ""  # last status shown in the header/footer (思考中 / 调用工具中 / 生成中 …)
    header_dirty: bool = False  # the header subtitle (status) needs a full-layout refresh
    footer_at: float = field(default_factory=time.monotonic)  # last footer write (the elapsed ticker)
    last_markdown: str = ""  # the markdown the card was sealed with (images/formulas already placed)
    speed_samples: "deque[Tuple[float, int]]" = field(default_factory=lambda: deque(maxlen=48))
    speed_tokens: int = 0  # estimated tokens in the answer landed so far
    decode_seconds: float = 0.0  # time spent between frames that grew the answer
    decode_tokens: int = 0
    metrics: Dict[str, Any] = field(default_factory=dict)
    observation_ref: Any = None
    observation_revision: int = -1
    observation_terminal: Optional[bool] = None
    observation_lost: bool = False
    segment_finalized: bool = False
    footer_dirty: bool = False
    delivery_result: Any = None
    write_lock: Any = field(default_factory=asyncio.Lock, repr=False)

    def cancel_background(self) -> None:
        try:
            current = asyncio.current_task()
        except RuntimeError:
            current = None
        for task in [self.watchdog, *self.formula_tasks.values()]:
            if task is not None and task is not current and not task.done():
                task.cancel()  # never the watchdog that is sealing this very turn

    def next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence

    @property
    def stale(self) -> bool:
        return self.elapsed >= STREAMING_TTL_SECONDS

    @property
    def elapsed(self) -> float:
        return max(0.0, (self.finished_at or time.monotonic()) - self.started_at)

    @property
    def tool_count(self) -> int:
        return self.tool_total

    @property
    def activity(self) -> str:
        """Latest tool line outside code fences, trimmed for the footer."""
        if self.tool_activity:
            return self.tool_activity
        latest = ""
        for line, in_fence in _walk_fenced(self.timeline):
            if not in_fence and line:
                latest = line
        return latest if len(latest) <= ACTIVITY_MAX_CHARS else latest[:ACTIVITY_MAX_CHARS - 1].rstrip() + "…"


@dataclass
class CompletedCard:
    """A sealed card that may still receive the turn's attachments."""
    turn: StreamCardTurn
    answer: str  # final answer text (raw markdown from the model)
    markdown: str  # the answer as rendered into the card
    sealed_at: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.sealed_at > COMPLETED_CARD_TTL_SECONDS

    def matches(self, raw: str) -> bool:
        """Is ``raw`` (a response with MEDIA: tags) the text this card was sealed with?"""
        from gateway.platforms.base import BasePlatformAdapter
        cleaned = BasePlatformAdapter.strip_media_directives_for_display(raw).strip()
        mine = self.answer.strip()
        return bool(mine) and (cleaned == mine or cleaned.startswith(mine[:200]))


# --- Frame parsing ----------------------------------------------------------------------------

def _walk_fenced(lines: List[str]):
    """Yield ``(stripped_line, inside_code_fence)``; fence markers themselves are skipped."""
    in_fence = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        yield stripped, in_fence


def _starts_with_emoji(text: str) -> bool:
    code = ord(text[0]) if text else 0
    return any(lo <= code <= hi for lo, hi in _EMOJI_RANGES)


def _looks_like_progress_overlay(lines: List[str]) -> bool:
    """Tool-progress overlays are emoji-led one-liners and ``terminal`` code fences, never prose."""
    if not lines:
        return False
    for stripped, in_fence in _walk_fenced(lines):
        if not in_fence and not _starts_with_emoji(stripped):  # blank line or prose (ASCII / CJK)
            return False
    return sum(line.strip().startswith("```") for line in lines) % 2 == 0


def _strip_cursor_line(lines: List[str]) -> List[str]:
    if lines and lines[-1]:
        lines[-1] = lines[-1].rstrip(_CURSOR_CHARS)
    return lines


def split_stream_frame(text: str, previous_answer: str = "") -> Tuple[str, List[str]]:
    """Undo ``_compose_frame_content``: ``(answer, tool_lines)``.

    The split is only taken when the tail reads as a progress overlay AND the head is consistent
    with the answer streamed so far (cumulative frames only ever extend it), so a ``---`` rule
    inside the answer itself stays in the answer.  Before any answer text the consumer sends the
    overlay bare, without the separator."""
    prev = previous_answer.rstrip(_CURSOR_CHARS)
    idx = text.rfind(_PROGRESS_SEPARATOR)
    if idx < 0:
        bare = _strip_cursor_line(text.split("\n"))
        if not prev and _looks_like_progress_overlay(bare):
            return "", [line for line in bare if line.strip()]
        return text, []
    head, lines = text[:idx], _strip_cursor_line(text[idx + len(_PROGRESS_SEPARATOR):].split("\n"))
    current = head.rstrip(_CURSOR_CHARS)
    if not _looks_like_progress_overlay(lines) or (prev and current and not (current.startswith(prev) or prev.startswith(current))):
        return text, []
    return head, [line for line in lines if line.strip()]


def count_tool_entries(lines: List[str]) -> int:
    """Tool calls in a timeline: emoji-led lines, plus bare code fences (consecutive ``terminal``
    calls drop their header line)."""
    count, in_fence, previous_was_header = 0, False, False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```"):
            if not in_fence and not previous_was_header:
                count += 1  # a bare fence is a consecutive terminal call
            in_fence, previous_was_header = not in_fence, False
        elif not in_fence and stripped:
            count += 1
            previous_was_header = True
    return count


def merge_overlay(turn: StreamCardTurn, lines: List[str]) -> bool:
    """Read the latest activity from the cumulative overlay; never keep its history.

    The consumer keeps overlay lines until the next text delta, so a later frame either extends
    the current overlay (append the tail) or starts a fresh one (append it all)."""
    if lines == turn.overlay:
        return False
    new = lines[len(turn.overlay):] if turn.overlay and lines[:len(turn.overlay)] == turn.overlay else lines
    turn.overlay = list(lines)
    if not turn.tools_observed and new:
        turn.tool_total += count_tool_entries(new)
        headers = [line for line, fenced in _walk_fenced(new)
                   if line.strip() and not fenced and not line.strip().startswith("```")]
        turn.timeline = headers[-1:] or ["💻 terminal"]
        turn.activity_kind = "tooling"
        turn.activity_dirty = True
    return bool(new)


# --- Adapter mixin ----------------------------------------------------------------------------

class FeishuStreamingCardMixin:
    """``send_stream_frame`` / ``supports_native_streaming`` / in-card attachments for ``FeishuAdapter``.

    Relies on the adapter for: ``_client``, ``_run_blocking``, ``_feishu_send_with_retry``,
    ``_finalize_send_result``, ``_response_succeeded``, ``_extract_response_field``, the
    ``_build_card_*_request`` / ``_build_image_upload_*`` builders, ``_cardkit_available()``,
    ``self._streaming_card``, ``self._card_math_images``, ``self._card_locale``, ``self._domain_name`` and
    ``self._bot_name``.
    """

    SUPPORTS_NATIVE_STREAMING = True
    # Opt in (approval_reopen.py): after an approval prompt the same transport keeps
    # streaming, so the continuation lands in a fresh card instead of ordinary messages.
    cardkit_approval_reopen = True

    def _init_streaming_cards(self) -> None:
        """Idempotent; also reached lazily for adapters built without ``__init__`` (test skeletons)."""
        if "_stream_cards" in self.__dict__:
            return
        self._stream_cards: Dict[str, StreamCardTurn] = {}  # "<chat>:<turn>" → open card
        self._opening_cards: Dict[str, asyncio.Task] = {}
        self._completed_cards: "OrderedDict[str, CompletedCard]" = OrderedDict()  # card_id → sealed card
        self._idle_sealed: "OrderedDict[str, StreamCardTurn]" = OrderedDict()  # key → idle-sealed turn
        self._observed_segments: "OrderedDict[str, StreamCardTurn]" = OrderedDict()
        self._stream_card_unavailable: Optional[str] = None
        self._stream_card_retry_at: Optional[float] = None  # None while unavailable = until restart
        self._stream_card_open_failures = 0
        # inbound message_id → thread_id, so a card replying inside a topic stays in the topic
        # (frames carry reply_to but not the send metadata that normally routes threads).
        self._stream_thread_ids: "OrderedDict[str, Optional[str]]" = OrderedDict()
        self._formula_image_keys: "OrderedDict[str, str]" = OrderedDict()  # formula digest → image_key
        self._pending_media: "deque[Tuple[str, float]]" = deque(maxlen=_PENDING_MEDIA_MAX)  # (raw, seen_at)

    # --- Gateway-facing surface ------------------------------------------------------------------

    def supports_native_streaming(self, chat_type: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None) -> bool:
        """Cards work in DMs, groups and topics alike; only config, the SDK and prior hard failures gate it."""
        del chat_type, metadata
        self._init_streaming_cards()
        return bool(getattr(self, "_streaming_card", False) and self._client is not None
                    and self._stream_cards_usable() and self._cardkit_available())

    async def send_stream_frame(
        self, text: str, *, finalize: bool = False, chat_id: Optional[str] = None,
        reply_to: Optional[str] = None, **kwargs: Any,
    ) -> bool:
        """Seed / cumulative push / finalize for one turn's card; ``turn_id`` keys concurrent turns."""
        chat = (chat_id or "").strip()
        if not chat:
            logger.warning("[Feishu] send_stream_frame: chat_id required")
            return False
        self._init_streaming_cards()
        key = f"{chat}:{kwargs.get('turn_id') or 'default'}"
        turn = self._stream_cards.get(key) or self._revive(key)
        if turn is None:
            opening = self._opening_cards.get(key)
            if finalize and opening is None:
                logger.debug("[Feishu] send_stream_frame: no open card to finalize (key=%s)", key)
                return False
            owner = opening is None
            if owner:
                opening = asyncio.create_task(self._open(chat, key, reply_to=reply_to, text=text,
                                                        revivable=bool(kwargs.get("turn_id"))))
                self._opening_cards[key] = opening
            try:
                turn = await asyncio.shield(opening)
            finally:
                if opening.done() and self._opening_cards.get(key) is opening:
                    self._opening_cards.pop(key, None)
            if turn is None:
                return False
            if owner or (not finalize and not text):
                return True
        async with turn.write_lock:
            turn.last_frame_at = time.monotonic()
            self._observe_turn(turn)
            if finalize:
                return await self._finalize(key, turn, text)
            if turn.phase == PHASE_UNKNOWN:
                turn.phase, turn.dirty = PHASE_RUNNING, True
            return await self._push(turn, text)

    async def close_open_stream_cards(self) -> None:
        """Best-effort seal of every open card (disconnect / restart) so none stays "typing"."""
        self._init_streaming_cards()
        opening = list(self._opening_cards.values())
        for pending in opening:
            pending.cancel()
        if opening:
            await asyncio.gather(*opening, return_exceptions=True)
        self._opening_cards.clear()
        segments, self._observed_segments = list(self._observed_segments.values()), OrderedDict()
        for segment in segments:
            await self._retire_segment_observer(segment)
        turns, self._stream_cards = list(self._stream_cards.values()), {}
        for turn in turns:
            turn.cancel_background()
            try:
                async with turn.write_lock:
                    turn.phase = PHASE_STOPPED
                    turn.finished_at = time.monotonic()
                    await self._seal(turn, self._card(turn, PHASE_STOPPED, render_answer(turn.answer.rstrip(_CURSOR_CHARS))))
            except Exception as exc:
                logger.debug("[Feishu] streaming card %s close on shutdown failed: %s", turn.card_id, exc)

    async def interrupt_session_activity(self, session_key: str, chat_id: str, metadata: Any = None) -> None:
        """Gateway interrupt (/stop, /new): the consumer abandons its turn without a final frame, so
        ask the watchdog to seal this chat's open cards once they fall silent for
        ``STOP_IDLE_SECONDS``.  A turn that keeps streaming (another session in a group) is only
        sealed at a gap and revived by its next frame.  Then the base behaviour."""
        try:
            self._init_streaming_cards()
            thread = (metadata or {}).get("thread_id") if isinstance(metadata, dict) else None
            now = time.monotonic()
            for turn in self._stream_cards.values():
                binding = getattr(turn, "control_source", None)
                bound_key = getattr(binding, "session_key", None)
                if turn.chat_id == chat_id and bound_key and bound_key == session_key:
                    turn.stop_requested_at = now
        except Exception as exc:  # never let card bookkeeping break an interrupt
            logger.debug("[Feishu] marking streaming cards stopped failed: %s", exc)
        parent = getattr(super(), "interrupt_session_activity", None)
        if parent is None:
            return
        try:
            params = inspect.signature(parent).parameters
            takes_metadata = "metadata" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())
        except (TypeError, ValueError):
            takes_metadata = False
        if takes_metadata:
            await parent(session_key, chat_id, metadata=metadata)
        else:
            await parent(session_key, chat_id)

    def remember_thread_for_message(self, message_id: Optional[str], thread_id: Optional[str]) -> None:
        if not message_id:
            return
        self._init_streaming_cards()
        self._stream_thread_ids[message_id] = thread_id or None
        self._stream_thread_ids.move_to_end(message_id)
        while len(self._stream_thread_ids) > _THREAD_CACHE_MAX:
            self._stream_thread_ids.popitem(last=False)

    def extract_media(self, content: str):
        """Base behaviour, plus: remember the raw response so ``send_multiple_images`` can put each
        image where its ``MEDIA:`` line stood (the streamed text reaches the card with tags stripped)."""
        from gateway.platforms.base import BasePlatformAdapter
        self._init_streaming_cards()
        self._pending_media.append((content, time.monotonic()))
        return BasePlatformAdapter.extract_media(content)

    async def send_multiple_images(
        self, chat_id: str, images: List[Tuple[str, str]], metadata: Optional[Dict[str, Any]] = None,
        human_delay: float = 0.0,
    ) -> None:
        """Local images delivered right after a streamed turn go into that turn's completed card.
        Anything that does not fit — no fresh card, remote URLs, too many files, an upload or update
        failure — goes the normal way, so nothing is ever lost."""
        self._init_streaming_cards()
        local = [(unquote(url[7:]), alt) for url, alt in images if url.startswith("file://")]
        if len(local) == len(images) and 0 < len(images) <= MAX_EMBEDDED_IMAGES:
            raw = self._take_pending_media([path for path, _alt in local])
            done = self._completed_card_for(chat_id, raw)
            if done is not None:
                if await self._embed_images(done, local, raw):
                    return
                logger.info("[Feishu] card embedding unavailable for %d image(s); sending them as messages", len(images))
        await super().send_multiple_images(chat_id, images, metadata=metadata, human_delay=human_delay)

    def _completed_card_for(self, chat_id: str, raw: Optional[str]) -> Optional[CompletedCard]:
        """The sealed card these attachments belong to: the one whose text ``raw`` carries, else
        the chat's only candidate if it was sealed moments ago (with two cards in play, or an older
        one, a guess could put the images on the wrong answer; they go out as messages instead)."""
        for card_id in [k for k, card in self._completed_cards.items() if card.expired]:
            self._completed_cards.pop(card_id, None)
        candidates = [card for card in self._completed_cards.values() if card.turn.chat_id == chat_id]
        if raw:
            for card in reversed(candidates):
                if card.matches(raw):
                    return card
        if len(candidates) == 1 and time.monotonic() - candidates[0].sealed_at <= UNMATCHED_ATTACH_WINDOW_SECONDS:
            return candidates[0]
        return None

    # --- Turn lifecycle --------------------------------------------------------------------------

    async def _open(self, chat: str, key: str, *, reply_to: Optional[str], text: str,
                    revivable: bool = False) -> Optional[StreamCardTurn]:
        """Create the card entity, deliver it as an interactive message, register the turn."""
        turn = StreamCardTurn(card_id="", message_id="", chat_id=chat, revivable=revivable, reply_to=reply_to)
        bind = getattr(self, "_bind_card_source", None)
        if callable(bind):
            bind(turn, reply_to)
        self._observe_turn(turn)
        answer, overlay = split_stream_frame(text)
        merge_overlay(turn, overlay)
        view, clipped = self._answer_view(turn, answer)
        # The raw answer is what the card will show; set it before rendering so the seed's
        # status word already reflects a first frame that carries text.
        turn.answer, turn.answer_clipped = answer, clipped
        try:
            card_id = await self._create_card(self._card(turn, turn.phase, view))
            if not card_id:
                return None
            thread_id = self._stream_thread_ids.get(reply_to or "")
            response = await self._feishu_send_with_retry(
                chat_id=chat, msg_type="interactive", payload=_json({"type": "card", "data": {"card_id": card_id}}),
                reply_to=reply_to, metadata={"thread_id": thread_id, "reply_to_message_id": reply_to} if thread_id else None,
            )
            result = self._finalize_send_result(response, "streaming card send failed")
            if not result.success:
                self._note_open_failure(result.error or "send failed", code=getattr(response, "code", None))
                return None
        except Exception as exc:
            self._note_open_failure(str(exc))
            return None
        turn.card_id, turn.message_id, turn.answer, turn.answer_clipped = card_id, result.message_id or "", answer, clipped
        turn.status_word = self._status(turn, PHASE_RUNNING)  # what the seed card already shows
        self._note_speed(turn, answer)
        turn.delivery_result = result
        turn.thread_id = thread_id
        turn.last_frame_at = time.monotonic()
        turn.watchdog = asyncio.ensure_future(self._seal_when_idle(key, turn))
        self._stream_cards[key] = turn
        self._stream_card_open_failures = 0
        after_delivery = getattr(self, "_after_card_delivery", None)
        if callable(after_delivery):
            try:
                await after_delivery(turn, result)
            except Exception:
                logger.debug("Continuation navigation unavailable", exc_info=True)
        logger.info("[Feishu] streaming card opened: card=%s message=%s chat=%s", card_id, turn.message_id, chat)
        return turn

    def _revive(self, key: str) -> Optional[StreamCardTurn]:
        """A frame for a turn the watchdog sealed as idle: the turn was alive after all (a long
        tool call, or a group turn caught by another session's /stop).  Reopen the same card —
        streaming mode is off now, so it continues with full updates — instead of a second card."""
        turn = self._idle_sealed.pop(key, None)
        if turn is None or time.monotonic() - (turn.sealed_at or 0.0) > _REVIVE_TTL_SECONDS:
            return None
        turn.sealed_at, turn.stop_requested_at, turn.finished_at = None, None, None
        turn.streaming_closed = turn.dirty = True
        turn.revivals += 1
        turn.last_frame_at = time.monotonic()
        turn.watchdog = asyncio.ensure_future(self._seal_when_idle(key, turn))
        self._stream_cards[key] = turn
        logger.info("[Feishu] streaming card %s revived: its turn sent a frame after the idle seal", turn.card_id)
        return turn

    async def _push(self, turn: StreamCardTurn, text: str) -> bool:
        """Cumulative intermediate frame; failures are fire-and-forget (the next frame overwrites)."""
        answer, overlay = split_stream_frame(text, turn.answer)
        activity_changed = merge_overlay(turn, overlay)
        if turn.stale and not turn.streaming_closed:
            turn.streaming_closed = True
            logger.info("[Feishu] streaming card %s passed the streaming window; continuing with full updates", turn.card_id)
        if turn.phase != PHASE_RUNNING:
            turn.streaming_closed = True
        if turn.streaming_closed:
            return await self._push_full(turn, answer or turn.answer, activity_changed)
        if answer and answer != turn.answer:
            view, clipped = self._answer_view(turn, answer)
            if await self._push_element(turn, ANSWER_ELEMENT_ID, view):
                turn.answer, turn.answer_clipped = answer, clipped
                if not overlay:
                    turn.activity_kind = "generating"
            self._prefetch_formula_images(turn, answer)
        self._note_status(turn)
        self._note_speed(turn, turn.answer)
        if time.monotonic() - turn.footer_at >= FOOTER_TICK_SECONDS:
            turn.footer_dirty = True  # keep the elapsed time and the decode rate moving
        if activity_changed or turn.activity_dirty:
            activity = render_activity(turn.activity, phase=turn.phase, tool_count=turn.tool_count, labels=self._labels)
            if await self._batch_update(turn, activity_update_actions(activity, self._footer(turn, turn.phase))):
                turn.activity_dirty = turn.footer_dirty = False
                turn.footer_at = time.monotonic()
            else:
                # Keep the owed update for the next frame/watchdog, including when no text follows.
                turn.activity_dirty = True
        elif turn.footer_dirty:
            if await self._batch_update(turn, footer_update_actions(self._footer(turn, turn.phase))):
                turn.footer_dirty, turn.footer_at = False, time.monotonic()
        return True

    async def _push_full(self, turn: StreamCardTurn, answer: str, activity_changed: bool) -> bool:
        """Frame for a card whose streaming mode is off (past CardKit's window, or revived): the
        whole running layout goes out as one ``card.update`` — no typewriter, same card, and
        at the consumer's edit pace (~1/s) well inside CardKit's 10 updates/s per card."""
        if (answer == turn.answer and not activity_changed and not turn.dirty
                and not turn.activity_dirty and not turn.footer_dirty and not turn.header_dirty):
            return True
        view, clipped = self._answer_view(turn, answer, streaming=False)
        previous = turn.answer
        turn.answer = answer  # the card is built from this frame, so its status word must be too
        if answer != previous and not turn.overlay:
            turn.activity_kind = "generating"
        card, _ = self._fit_card(turn, turn.phase, view, streaming=False)
        if await self._replace(turn, card, level=logging.DEBUG):
            turn.answer, turn.answer_clipped, turn.dirty = answer, clipped, False
            turn.activity_dirty = turn.footer_dirty = turn.header_dirty = False
            turn.status_word = self._status(turn, turn.phase)
            turn.footer_at = time.monotonic()
            self._note_speed(turn, answer)
        else:
            turn.answer, turn.dirty = previous, True
        if answer:
            self._prefetch_formula_images(turn, answer)
        return True

    async def _finalize(self, key: str, turn: StreamCardTurn, text: str) -> bool:
        """Land the final text, then seal with the completed layout; False hands delivery back."""
        self._stream_cards.pop(key, None)
        if turn.watchdog is not None:
            turn.watchdog.cancel()
        self._observe_turn(turn)
        # Hermes also finalizes an interim segment before a human prompt. Only the official
        # terminal event proves the entire agent turn finished; a transport finalize does not.
        approval_boundary = approval_reopen.consume_approval_boundary(self, key)
        turn.segment_finalized = turn.observation_terminal is False or approval_boundary
        phase = (turn.phase if turn.segment_finalized or turn.phase in {
            PHASE_STOPPED, PHASE_FAILED, PHASE_BLOCKED, PHASE_TIMEOUT} else PHASE_DONE)
        turn.phase = phase
        turn.finished_at = time.monotonic()
        # Finalize frames are pure text (no overlay), so no split: an answer that happens to open
        # with an emoji line must not be mistaken for tool progress — and one that was, earlier in
        # the stream, is corrected below for the legacy overlay fallback.
        final = (text or turn.answer or self._labels.tool_only).rstrip(_CURSOR_CHARS)
        # Legacy overlay parsing can mistake an emoji-led answer for one tool header.
        # Official tool events never need this textual inference.
        if (not turn.tools_observed and turn.tool_total == 1 and turn.timeline
                and final.startswith(turn.timeline[0])):
            turn.timeline.clear()
            turn.tool_total = 0
        turn.overlay = []
        self._note_speed(turn, final)  # sample final deltas; a lone final frame has no measured rate
        if utf8_len(final) > CARD_MAX_BYTES:
            # Cannot fit one card: show what fits and let the consumer send the full text below.
            head = render_answer(clip_markdown_head(final, CARD_MAX_BYTES - 16, "…"))
            await self._seal_final(turn, phase, head, head)
            turn.answer = final
            await self._track_closed_segment(turn)
            logger.info("[Feishu] streaming card %s final exceeds card limit; falling back to send()", turn.card_id)
            return False
        landed = final == turn.answer.rstrip(_CURSOR_CHARS) and not turn.answer_clipped
        if not landed and not turn.stale and not turn.streaming_closed:
            # Finish the typewriter before the layout switch; the full update below carries the
            # same text, so a rejected push is repaired there.  An answer too big for the running
            # card is left to the completed layout (which clips it and hands delivery back).
            view, clipped = self._answer_view(turn, final, hold_open_math=False)
            if not clipped:
                landed = await self._push_element(turn, ANSWER_ELEMENT_ID, view)
        await self._await_formula_images(turn, final)
        shown, markdown = await self._seal_final(turn, phase, self._render_markdown(final), render_answer(final))
        turn.answer = final
        turn.last_markdown = markdown
        await self._track_closed_segment(turn)
        if shown:
            if not turn.segment_finalized:
                self._completed_cards[turn.card_id] = CompletedCard(turn, final, markdown)
            while len(self._completed_cards) > _COMPLETED_CARDS_MAX:
                self._completed_cards.popitem(last=False)
            after_delivery = getattr(self, "_after_card_delivery", None)
            if callable(after_delivery) and turn.delivery_result is not None and not turn.segment_finalized:
                # A clarification can resume into the original card. Only the successful final
                # write proves that card now contains the continuation, not its earlier seed.
                turn.answer = final
                try:
                    await after_delivery(turn, turn.delivery_result)
                except Exception:
                    logger.debug("Final continuation navigation unavailable", exc_info=True)
            return True  # the completed layout carries the answer and closes streaming mode
        if shown is False:
            logger.info("[Feishu] streaming card %s sealed with a clipped answer; consumer sends the full text", turn.card_id)
            return False
        if not landed:
            logger.warning("[Feishu] streaming card %s could not land the final text; consumer fallback", turn.card_id)
        return landed

    async def _seal_final(self, turn: StreamCardTurn, phase: str, markdown: str, plain: str,
                          abort: Optional[Any] = None) -> Tuple[Optional[bool], str]:
        """Seal the card in ``phase`` so it never keeps saying "生成中":

        1. the full layout, retried after ``FINAL_UPDATE_RETRY_DELAYS`` (blips, rate limits);
        2. a slim card — ``plain`` (Unicode math, no images) in one element, tool lines reduced
           to their count — for rejections that are about the content;
        3. last resort: the footer alone via batch update, then streaming mode off (the header
           can only change through a full update, so it may still read "生成中").

        Returns ``(True, markdown)`` when a card shows the whole answer, ``(False, markdown)`` when
        it shows a clipped one, ``(None, markdown)`` when no full update landed.  ``abort()`` stops
        the ladder between attempts (an idle seal whose turn came back to life)."""
        card, clipped = self._fit_card(turn, phase, markdown)
        for delay in (0.0, *FINAL_UPDATE_RETRY_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            if abort is not None and abort():
                return None, markdown
            if await self._replace(turn, card):
                return not clipped, markdown
        card, clipped = self._fit_card(turn, phase, plain, slim=True)
        if abort is not None and abort():
            return None, markdown
        if await self._replace(turn, card):
            logger.warning("[Feishu] streaming card %s sealed with the slim layout after full updates failed", turn.card_id)
            return not clipped, plain
        footer = self._footer(turn, phase)
        await self._batch_update(turn, footer_update_actions(footer), level=logging.WARNING)
        await self._close_streaming(turn)
        logger.warning("[Feishu] streaming card %s: no full update landed; footer set to %r and streaming closed",
                       turn.card_id, footer)
        return None, markdown

    async def _embed_images(self, done: CompletedCard, local: List[Tuple[str, str]], raw: Optional[str]) -> bool:
        """Upload the turn's images and rewrite its completed card with them in place."""
        keys: List[str] = []
        for path, _alt in local:
            key = await self._upload_image(path)
            if not key:
                return False
            keys.append(key)
        items: List[ImageItem] = [(path, key, alt) for (path, alt), key in zip(local, keys)]
        labels = self._labels
        markdown = ((place_images(raw, items, self._render_markdown, labels) if raw else None)
                    or append_images(done.markdown, items, labels))
        async with done.turn.write_lock:
            card, clipped = self._fit_card(done.turn, done.turn.phase, markdown, streaming=False)
            if clipped:
                logger.info("[Feishu] streaming card %s has no room for %d image(s)", done.turn.card_id, len(items))
                return False
            if not await self._replace(done.turn, card):
                return False
        done.turn.last_markdown = markdown
        self._completed_cards.pop(done.turn.card_id, None)
        logger.info("[Feishu] %d image(s) embedded into streaming card %s (%s)",
                    len(items), done.turn.card_id, "positioned" if raw else "appended")
        return True

    async def _seal_when_idle(self, key: str, turn: StreamCardTurn) -> None:
        """Observe real state even while no text arrives. Silence alone never means stopped."""
        while True:
            if self._stream_cards.get(key) is not turn:
                return
            async with turn.write_lock:
                changed = self._observe_turn(turn)
                self._note_status(turn)
                full = turn.header_dirty or turn.dirty
                if changed or full or turn.footer_dirty or turn.activity_dirty:
                    if turn.phase != PHASE_RUNNING:
                        turn.streaming_closed = True
                    if full:
                        view, _ = self._answer_view(turn, turn.answer, streaming=not turn.streaming_closed)
                        card, _ = self._fit_card(turn, turn.phase, view, streaming=not turn.streaming_closed)
                        refreshed = await self._replace(turn, card, level=logging.DEBUG)
                        # Retain the original reason for a retry: a failed header or
                        # footer refresh must not force the next frame out of streaming.
                        turn.dirty = turn.dirty and not refreshed
                        turn.activity_dirty = turn.activity_dirty and not refreshed
                        turn.header_dirty = turn.header_dirty and not refreshed
                        turn.footer_dirty = not refreshed
                        if refreshed:
                            turn.footer_at = time.monotonic()
                    else:
                        actions = (activity_update_actions(
                            render_activity(turn.activity, phase=turn.phase, tool_count=turn.tool_count, labels=self._labels),
                            self._footer(turn, turn.phase)) if turn.activity_dirty
                            else footer_update_actions(self._footer(turn, turn.phase)))
                        written = await self._batch_update(turn, actions)
                        turn.activity_dirty = turn.activity_dirty and not written
                        turn.footer_dirty = not written
                        turn.footer_at = time.monotonic() if written else turn.footer_at
                elif (turn.phase in ACTIVE_PHASES
                      and time.monotonic() - turn.footer_at >= FOOTER_TICK_SECONDS * 1.5):
                    # A quiet turn still moves: refresh the elapsed time (and the status word)
                    # every poll so a long tool call never looks frozen.
                    written = await self._batch_update(turn, footer_update_actions(self._footer(turn, turn.phase)))
                    turn.footer_at = time.monotonic() if written else turn.footer_at
            # An explicit human wait is still active even if it lasts longer than our text idle
            # threshold. Its real resolution/timeout observer will change this state.
            if turn.phase in {PHASE_WAITING_APPROVAL, PHASE_WAITING_INPUT} and turn.stop_requested_at is None:
                await asyncio.sleep(_WATCHDOG_POLL_SECONDS)
                continue
            limit = STOP_IDLE_SECONDS if turn.stop_requested_at is not None else IDLE_SEAL_SECONDS
            remaining = turn.last_frame_at + limit - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(min(remaining, _WATCHDOG_POLL_SECONDS))
                continue
            async with turn.write_lock:
                # A text/final frame may have won while the watchdog waited for the lock.
                if self._stream_cards.get(key) is not turn:
                    return
                if time.monotonic() < turn.last_frame_at + limit:
                    continue
                self._stream_cards.pop(key, None)
                turn.cancel_background()
                if turn.revivable:
                    turn.sealed_at = time.monotonic()
                    self._park_idle(key, turn)
                phase = (PHASE_STOPPED if turn.stop_requested_at is not None else turn.phase
                         if turn.phase in {PHASE_DONE, PHASE_STOPPED, PHASE_FAILED, PHASE_BLOCKED, PHASE_TIMEOUT}
                         else PHASE_UNKNOWN)
                turn.phase = phase
                turn.finished_at = time.monotonic()
                logger.info("[Feishu] streaming card %s idle for %.0fs (%s); sealing as %s", turn.card_id,
                            time.monotonic() - turn.last_frame_at,
                            "interrupted" if turn.stop_requested_at else "no frames", phase)
                markdown = render_answer(turn.answer.rstrip(_CURSOR_CHARS))
                await self._seal_final(turn, phase, markdown, markdown)
                return

    def _park_idle(self, key: str, turn: StreamCardTurn) -> None:
        now = time.monotonic()
        for stale_key in [k for k, t in self._idle_sealed.items() if now - (t.sealed_at or 0.0) > _REVIVE_TTL_SECONDS]:
            self._idle_sealed.pop(stale_key, None)
        self._idle_sealed[key] = turn
        while len(self._idle_sealed) > _COMPLETED_CARDS_MAX:
            self._idle_sealed.popitem(last=False)

    async def _track_closed_segment(self, turn: StreamCardTurn) -> None:
        if not turn.segment_finalized:
            return
        turn.streaming_closed = True
        self._observed_segments[turn.card_id] = turn
        while len(self._observed_segments) > _COMPLETED_CARDS_MAX:
            _, old = self._observed_segments.popitem(last=False)
            await self._retire_segment_observer(old)
        turn.watchdog = asyncio.ensure_future(self._watch_closed_segment(turn))

    async def _retire_segment_observer(self, turn: StreamCardTurn) -> None:
        """Shutdown/eviction cannot leave an unobserved card claiming it is still waiting."""
        turn.cancel_background()
        async with turn.write_lock:
            if not turn.observation_terminal:
                turn.phase = PHASE_UNKNOWN
            try:
                await self._seal_final(turn, turn.phase, self._render_markdown(turn.answer), render_answer(turn.answer))
            except Exception:
                logger.debug("Closed segment retirement failed", exc_info=True)

    async def _watch_closed_segment(self, turn: StreamCardTurn) -> None:
        """Keep an interim pre-prompt card truthful without retaining a native stream slot."""
        try:
            while self._observed_segments.get(turn.card_id) is turn:
                await asyncio.sleep(_WATCHDOG_POLL_SECONDS)
                async with turn.write_lock:
                    changed = self._observe_turn(turn)
                    if changed or turn.dirty or turn.footer_dirty:
                        card, _ = self._fit_card(turn, turn.phase, self._render_markdown(turn.answer), streaming=False)
                        turn.dirty = not await self._replace(turn, card, level=logging.DEBUG)
                        turn.footer_dirty = turn.dirty
                    if turn.observation_terminal or turn.observation_lost:
                        if turn.dirty:
                            await self._seal_final(turn, turn.phase, self._render_markdown(turn.answer),
                                                   render_answer(turn.answer))
                        break
        finally:
            if self._observed_segments.get(turn.card_id) is turn:
                self._observed_segments.pop(turn.card_id, None)

    def _take_pending_media(self, paths: List[str]) -> Optional[str]:
        """The freshest raw response that mentions the delivered files; claimed once."""
        now = time.monotonic()
        names = [os.path.basename(path) for path in paths]
        for entry in reversed(self._pending_media):
            raw, seen_at = entry
            if now - seen_at <= PENDING_MEDIA_TTL_SECONDS and any(name in raw for name in names):
                self._pending_media.remove(entry)
                return raw
        return None

    # --- Rendering ---------------------------------------------------------------------------------

    @property
    def _labels(self) -> Labels:
        return labels_for(getattr(self, "_domain_name", ""), getattr(self, "_card_locale", ""))

    def _footer(self, turn: StreamCardTurn, phase: str) -> str:
        if turn.segment_finalized and phase in {PHASE_RUNNING, PHASE_DONE}:
            phase = PHASE_SEGMENT
        return render_footer(
            phase=phase, tool_count=turn.tool_count, elapsed=turn.elapsed,
            status=self._status(turn, phase), speed=self._speed(turn, phase), labels=self._labels,
            metrics=turn.metrics, show_usage=bool(getattr(self, "_footer_usage", False)),
        )

    def _status(self, turn: StreamCardTurn, phase: str) -> str:
        """思考中 / 调用工具中 / 生成中 while running; the phase word otherwise."""
        if turn.segment_finalized and phase in {PHASE_RUNNING, PHASE_DONE}:
            phase = PHASE_SEGMENT
        answer = turn.answer.rstrip(_CURSOR_CHARS).strip()
        if phase == PHASE_RUNNING and turn.activity_kind:
            return {"tooling": self._labels.tooling, "thinking": self._labels.thinking,
                    "generating": self._labels.running}.get(turn.activity_kind, self._labels.thinking)
        return status_label(phase, self._labels, has_answer=bool(answer), has_tools=turn.tool_count > 0)

    def _note_status(self, turn: StreamCardTurn) -> bool:
        """Refresh the status word when it changes; True when it did.

        The footer is an element, so a batch update carries the new word cheaply. The header is
        the card title now, which only a full card update can change: the watchdog applies it
        within one poll, the answer content it re-sends is identical, preserving the streamed text.
        """
        status = self._status(turn, turn.phase)
        if status == turn.status_word:
            return False
        turn.status_word, turn.footer_dirty, turn.header_dirty = status, True, True
        return True

    @staticmethod
    def _note_speed(turn: StreamCardTurn, text: str) -> None:
        """Sample the estimated token count of the answer landed so far (live decode rate)."""
        tokens = estimate_tokens(text)
        now = time.monotonic()
        if not tokens:
            return  # a seed is not a decoded token; waiting for the first token is not decode time
        if tokens == turn.speed_tokens and turn.speed_samples:
            return
        if tokens > turn.speed_tokens and turn.speed_samples:
            gap = now - turn.speed_samples[-1][0]
            if 0 < gap <= SPEED_MAX_GAP_SECONDS:
                turn.decode_seconds += gap
                turn.decode_tokens += tokens - turn.speed_tokens
            else:
                turn.speed_samples.clear()
        elif tokens < turn.speed_tokens:
            turn.speed_samples.clear()
        turn.speed_tokens = tokens
        turn.speed_samples.append((now, tokens))

    @staticmethod
    def _live_speed(turn: StreamCardTurn) -> float:
        """Tokens per second over the newest samples; 0 until the window spans enough time."""
        now = time.monotonic()
        samples = [(t, n) for t, n in turn.speed_samples if now - t <= SPEED_WINDOW_SECONDS]
        if len(samples) < 2:
            return 0.0
        (t0, n0), (t1, n1) = samples[0], samples[-1]
        if n1 <= n0 or t1 - t0 < SPEED_MIN_SPAN_SECONDS or now - t1 > SPEED_MAX_IDLE_SECONDS:
            return 0.0
        return (n1 - n0) / (t1 - t0)

    @staticmethod
    def _average_speed(turn: StreamCardTurn) -> float:
        """Estimated rate from paired text deltas only; a single final frame has no rate."""
        if not turn.decode_tokens or turn.decode_seconds < SPEED_AVERAGE_MIN_SECONDS:
            return 0.0
        return turn.decode_tokens / turn.decode_seconds

    def _speed(self, turn: StreamCardTurn, phase: str) -> str:
        """The live rate while running; the official per-turn rate once the turn is over."""
        measured = turn.metrics.get("speed")
        if phase not in ACTIVE_PHASES and isinstance(measured, (int, float)):
            value = format_speed(float(measured))
            return ("≈ " if turn.metrics.get("speed_estimated") and value else "") + value
        if phase in ACTIVE_PHASES:
            if turn.activity_kind == "tooling":
                return ""
            value = format_speed(self._live_speed(turn))
            if value:
                return "≈ " + value
            if (isinstance(measured, (int, float)) and
                    time.monotonic() - turn.metrics.get("speed_updated_at", 0.0) <= SPEED_MAX_IDLE_SECONDS):
                value = format_speed(measured)
                return ("≈ " if turn.metrics.get("speed_estimated") and value else "") + value
            return ""
        value = format_speed(self._average_speed(turn))
        return "≈ " + value if value else ""

    def _card(self, turn: StreamCardTurn, phase: str, markdown: str, *, streaming: Optional[bool] = None,
              plain: bool = False) -> Dict[str, Any]:
        if turn.segment_finalized and phase in {PHASE_RUNNING, PHASE_DONE}:
            phase = PHASE_SEGMENT
        return build_stream_card(
            title="", markdown=markdown, activity=turn.activity, tool_count=turn.tool_count,
            footer=self._footer(turn, phase), phase=phase, status=self._status(turn, phase),
            summary=summary_for(markdown, phase, self._labels), labels=self._labels,
            streaming=streaming, plain=plain, **self._reading_settings(),
        )

    def _reading_settings(self) -> Dict[str, Any]:
        from .reading import resolve_reading
        if "_card_reading" not in self.__dict__:
            self._card_reading = resolve_reading(getattr(getattr(self, "config", None), "extra", None) or {})
        return dict(self._card_reading["values"])

    def describe_card_config(self) -> Dict[str, Any]:
        """An in-process readback of display options, never credentials or model settings."""
        self._reading_settings()
        return {"loaded": dict(self._card_reading["values"]), "sources": dict(self._card_reading["sources"])}

    def _observe_turn(self, turn: StreamCardTurn) -> bool:
        """Optional official events, matched by exact inbound message and immutable turn identity.

        Older Hermes versions still render cards. Missing or ambiguous observations stay hidden.
        Never combine the stream consumer's random ID with the agent's independent turn ID.
        """
        try:
            from .lifecycle import OBSERVATIONS, scope_for_source
            binding = getattr(turn, "control_source", None)
            scope = scope_for_source(getattr(binding, "source", None))
            session_key = getattr(binding, "session_key", None)
            message_id = getattr(binding, "message_id", None)
            if turn.observation_ref is None:
                # The same incoming Feishu message can reach several bots. A unique
                # observation in the process at this instant does not prove ownership.
                if not scope or not session_key or not message_id:
                    return False
                turn.observation_ref = OBSERVATIONS.resolve(scope, turn.chat_id, message_id,
                                                            session_key=session_key)
            snapshot = OBSERVATIONS.snapshot(turn.observation_ref) if turn.observation_ref is not None else None
            if snapshot is not None and (snapshot.profile_scope != scope or snapshot.session_key != session_key
                                         or snapshot.message_id != message_id or snapshot.chat_id != turn.chat_id):
                snapshot = None
            if snapshot is None:
                if turn.observation_ref is not None and not turn.observation_lost:
                    turn.observation_lost = True
                    turn.metrics, turn.footer_dirty = {}, True
                    if not turn.observation_terminal:
                        turn.phase, turn.dirty = PHASE_UNKNOWN, True
                    return True
                return False
            if snapshot.revision == turn.observation_revision:
                return False
            turn.observation_revision = snapshot.revision
            turn.observation_terminal = snapshot.terminal
            if snapshot.activity_kind:
                if snapshot.activity_kind == "tooling" and turn.activity_kind != "tooling":
                    turn.speed_samples.clear()
                turn.activity_kind = snapshot.activity_kind
            if snapshot.tools.get("count"):
                activity = snapshot.tools.get("label", "")
                if activity != turn.tool_activity or snapshot.tools["count"] != turn.tool_total:
                    turn.tool_activity = activity
                    turn.tool_total = snapshot.tools["count"]
                    turn.activity_dirty = True
                turn.tools_observed = True
                turn.timeline.clear()
            metrics = dict(snapshot.metrics)
            if metrics != turn.metrics:
                turn.metrics, turn.footer_dirty = metrics, True
            if snapshot.phase != turn.phase:
                turn.phase, turn.dirty = snapshot.phase, True
            return True
        except Exception:
            logger.debug("Card lifecycle observation unavailable", exc_info=True)
            return False

    def _fit_card(self, turn: StreamCardTurn, phase: str, markdown: str, *, streaming: Optional[bool] = None,
                  slim: bool = False) -> Tuple[Dict[str, Any], bool]:
        """Fit the whole card, preserving a single bounded activity line and the answer."""
        card = self._card(turn, phase, markdown, streaming=streaming, plain=slim)
        size = card_json_bytes(card)
        if size <= CARD_JSON_MAX_BYTES:
            return card, False
        budget = utf8_len(markdown) - (size - CARD_JSON_MAX_BYTES) - 64
        for _ in range(4):
            clipped = clip_markdown_head(markdown, max(0, budget), "…")
            card = self._card(turn, phase, clipped, streaming=streaming, plain=slim)
            over = card_json_bytes(card) - CARD_JSON_MAX_BYTES
            if over <= 0:
                break
            budget -= over + 256
        logger.info("[Feishu] streaming card %s answer clipped to fit the card (%d bytes of markdown)",
                    turn.card_id, utf8_len(markdown))
        return card, True

    def _answer_view(self, turn: StreamCardTurn, answer: str, *, streaming: bool = True,
                     hold_open_math: bool = True) -> Tuple[str, bool]:
        """What the running card's answer element shows, and whether it is clipped: the answer, or
        once it outgrows the card its newest paragraphs under a "earlier text omitted" note (the
        completed layout or the consumer's fallback delivers the whole text).  A formula still
        being typed at the end is held back (``hold_open_math``) until it closes."""
        if hold_open_math:
            body = answer.rstrip(_CURSOR_CHARS)
            answer = hide_open_formula(body) + answer[len(body):]
        rendered = render_answer(answer)
        skeleton = card_json_bytes(self._card(turn, PHASE_RUNNING, "", streaming=streaming))
        budget = min(CARD_MAX_BYTES, CARD_JSON_MAX_BYTES - skeleton - _ACTIVITY_GROWTH_RESERVE)
        if json_text_bytes(rendered) <= budget:
            return rendered, False
        note, raw_budget = self._labels.clipped_head, budget - (json_text_bytes(rendered) - utf8_len(rendered))
        view = clip_markdown_tail(rendered, max(0, raw_budget), note)
        while json_text_bytes(view) > budget and raw_budget > 0:
            raw_budget -= json_text_bytes(view) - budget + 256
            view = clip_markdown_tail(rendered, max(0, raw_budget), note)
        return view, True

    def _render_markdown(self, text: str) -> str:
        """Unicode math for inline formulas; display formulas as images where one was typeset."""
        return convert_math(text, block_renderer=self._formula_image if self._formula_images_enabled() else None)

    def _formula_image(self, body: str) -> Optional[str]:
        key = self._formula_image_keys.get(formula_digest(body))
        return f"![公式]({key})" if key else None

    # --- Formula images ----------------------------------------------------------------------------

    def _formula_images_enabled(self) -> bool:
        return bool(getattr(self, "_card_math_images", True)) and mathtext_available()

    def _prefetch_formula_images(self, turn: StreamCardTurn, answer: str) -> None:
        """Render + upload every display formula that has closed so far, off the critical path."""
        if not self._formula_images_enabled():
            return
        for body in iter_block_formulas(answer):
            digest = formula_digest(body)
            if digest not in self._formula_image_keys and digest not in turn.formula_tasks:
                turn.formula_tasks[digest] = asyncio.ensure_future(self._upload_formula_image(body, digest))

    async def _upload_formula_image(self, body: str, digest: str) -> Optional[str]:
        try:
            png = await self._run_blocking(render_formula_png, body)
        except Exception as exc:  # e.g. the SDK pool is already shut down
            logger.debug("[Feishu] formula render skipped: %s", exc)
            return None
        if not png:
            logger.info("[Feishu] formula not typeset (mathtext declined): %.60r", body)
            return None
        key = await self._upload_image(png)
        if key:
            self._formula_image_keys[digest] = key
            while len(self._formula_image_keys) > _FORMULA_IMAGE_CACHE_MAX:
                self._formula_image_keys.popitem(last=False)
        return key

    async def _await_formula_images(self, turn: StreamCardTurn, answer: str) -> None:
        """Give in-flight formula uploads a bounded window before the completed layout is built."""
        self._prefetch_formula_images(turn, answer)
        if not turn.formula_tasks:
            return
        started = time.monotonic()
        done, pending = await asyncio.wait(list(turn.formula_tasks.values()), timeout=FORMULA_IMAGE_WAIT_SECONDS)
        for task in pending:
            task.cancel()
        ready = sum(1 for t in done if not t.cancelled() and t.exception() is None and t.result())
        logger.info("[Feishu] formula images: %d ready, %d still pending after %.1fs (card=%s)",
                    ready, len(pending), time.monotonic() - started, turn.card_id)

    # --- Feishu API calls ---------------------------------------------------------------------------

    async def _upload_image(self, source: Union[str, bytes]) -> Optional[str]:
        """Upload a local file (path) or in-memory PNG (bytes) as a message image → image_key."""
        label = source if isinstance(source, str) else f"<{len(source)} bytes>"
        try:
            with (open(source, "rb") if isinstance(source, str) else io.BytesIO(source)) as handle:
                request = self._build_image_upload_request(self._build_image_upload_body(image_type="message", image=handle))
                response = await self._run_blocking(self._client.im.v1.image.create, request)
        except Exception as exc:
            logger.warning("[Feishu] image upload failed (%s): %s", label, exc)
            return None
        key = self._extract_response_field(response, "image_key")
        if not key:
            logger.warning("[Feishu] image upload returned no image_key (%s): [%s] %s",
                           label, getattr(response, "code", None), getattr(response, "msg", None))
        return key

    async def _create_card(self, card: Dict[str, Any]) -> Optional[str]:
        response = await self._run_blocking(self._client.cardkit.v1.card.create, self._build_card_create_request(_json(card)))
        if not self._response_succeeded(response):
            code = getattr(response, "code", None)
            self._note_open_failure(f"[{code}] {getattr(response, 'msg', 'card create failed')}", code=code)
            return None
        return self._extract_response_field(response, "card_id")

    async def _push_element(self, turn: StreamCardTurn, element_id: str, text: str) -> bool:
        """Streaming text push into one element (typewriter rendering)."""
        request = self._build_card_content_request(card_id=turn.card_id, element_id=element_id, content=text,
                                                   sequence=turn.next_sequence(), uuid_value=_uuid())
        return await self._card_op(turn, "content push", self._client.cardkit.v1.card_element.content, request)

    async def _batch_update(self, turn: StreamCardTurn, actions: List[Dict[str, Any]], *, level: int = logging.DEBUG) -> bool:
        request = self._build_card_batch_update_request(card_id=turn.card_id, actions_json=_json(actions),
                                                        sequence=turn.next_sequence(), uuid_value=_uuid())
        return await self._card_op(turn, "batch update", self._client.cardkit.v1.card.batch_update, request, level=level)

    async def _replace(self, turn: StreamCardTurn, card: Dict[str, Any], *, level: int = logging.WARNING) -> bool:
        """Full card rewrite (``card.update``): the completed layout, and the repair path."""
        card_json = _json(card)
        request = self._build_card_update_request(card_id=turn.card_id, card_json=card_json,
                                                  sequence=turn.next_sequence(), uuid_value=_uuid())
        return await self._card_op(turn, f"full update ({utf8_len(card_json)} B)", self._client.cardkit.v1.card.update,
                                   request, level=level)

    async def _close_streaming(self, turn: StreamCardTurn) -> bool:
        request = self._build_card_settings_request(card_id=turn.card_id, settings=_json({"config": {"streaming_mode": False}}),
                                                    sequence=turn.next_sequence(), uuid_value=_uuid())
        return await self._card_op(turn, "close", self._client.cardkit.v1.card.settings, request, level=logging.WARNING)

    async def _seal(self, turn: StreamCardTurn, card: Dict[str, Any]) -> bool:
        """Completed layout if the API takes it, else at least switch streaming mode off."""
        return await self._replace(turn, card) or await self._close_streaming(turn)

    async def _card_op(self, turn: StreamCardTurn, what: str, method: Any, request: Any, *,
                       level: int = logging.DEBUG) -> bool:
        """One CardKit call; rejections and exceptions read as False.  Logged at ``level``: DEBUG
        for fire-and-forget frames, WARNING for seals and closes (error code only, no content)."""
        try:
            response = await self._run_blocking(method, request)
        except Exception as exc:
            logger.log(level, "[Feishu] streaming card %s %s raised %s: %.200s", turn.card_id, what, type(exc).__name__, exc)
            return False
        if self._response_succeeded(response):
            return True
        logger.log(level, "[Feishu] streaming card %s %s rejected: code=%s msg=%.200s",
                   turn.card_id, what, getattr(response, "code", None), getattr(response, "msg", None))
        return False

    # --- Failure accounting ------------------------------------------------------------------------

    def _stream_cards_usable(self) -> bool:
        """False while the transport is disabled; a cooldown pause ends with one trial open (a
        failure pauses again straight away, a success resets the count)."""
        if self._stream_card_unavailable is None:
            return True
        if self._stream_card_retry_at is None or time.monotonic() < self._stream_card_retry_at:
            return False
        logger.info("[Feishu] streaming cards re-enabled after cooldown (last failure: %s)", self._stream_card_unavailable)
        self._stream_card_unavailable, self._stream_card_retry_at = None, None
        self._stream_card_open_failures = MAX_OPEN_FAILURES - 1
        return True

    def _note_open_failure(self, reason: str, *, code: Any = None) -> None:
        """Permission errors disable the transport until restart; anything else pauses it for
        ``OPEN_FAILURE_COOLDOWN_SECONDS`` after N misses in a row."""
        self._stream_card_open_failures += 1
        if code == NO_PERMISSION_CODE:
            self._stream_card_unavailable, self._stream_card_retry_at = reason, None
            logger.warning("[Feishu] streaming cards disabled: the app lacks the cardkit:card:write scope (%s). "
                           "Grant it in the Feishu developer console or set FEISHU_STREAMING_CARD=false to silence this.", reason)
        elif self._stream_card_open_failures >= MAX_OPEN_FAILURES:
            self._stream_card_unavailable = reason
            self._stream_card_retry_at = time.monotonic() + OPEN_FAILURE_COOLDOWN_SECONDS
            logger.warning("[Feishu] streaming cards paused for %.0fs after %d consecutive open failures (last: %s); "
                           "replies fall back to edited messages", OPEN_FAILURE_COOLDOWN_SECONDS,
                           self._stream_card_open_failures, reason)
        else:
            logger.warning("[Feishu] streaming card open failed (%d/%d): %s", self._stream_card_open_failures, MAX_OPEN_FAILURES, reason)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _uuid() -> str:
    return str(uuid.uuid4())


__all__ = [
    "FeishuStreamingCardMixin", "StreamCardTurn", "CompletedCard", "split_stream_frame", "merge_overlay",
    "count_tool_entries", "STREAMING_TTL_SECONDS", "NO_PERMISSION_CODE", "MAX_OPEN_FAILURES",
    "OPEN_FAILURE_COOLDOWN_SECONDS", "COMPLETED_CARD_TTL_SECONDS", "UNMATCHED_ATTACH_WINDOW_SECONDS",
    "MAX_EMBEDDED_IMAGES", "PENDING_MEDIA_TTL_SECONDS", "FORMULA_IMAGE_WAIT_SECONDS", "IDLE_SEAL_SECONDS",
    "STOP_IDLE_SECONDS", "FINAL_UPDATE_RETRY_DELAYS",
]
