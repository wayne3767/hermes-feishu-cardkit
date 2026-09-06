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
  takes that apart again so tool lines land in the timeline panel;
- a ``finalize=True`` frame must either land the text or return False so the consumer's
  send()/edit fallback delivers exactly once;
- ``finalize`` on an unknown turn returns False (never seed-and-close).

After the turn the gateway hands attachments to ``send_multiple_images``; images then go INTO the
completed card, each where its ``MEDIA:`` line stood (``extract_media`` keeps the raw response so
the positions are known), captioned ``图N　标题``.

Limits: CardKit allows 10 updates/s per card and auto-closes streaming after 10 minutes; a
sealed card is repaired with a full ``card.update``.  Card JSON is capped at 30 KB by Feishu, so a
final that no longer fits is handed back to the consumer (return False) and goes out as ordinary
chunked messages below the card.
The user-facing design was pioneered by baileyh8/hermes-feishu-streaming-card (MIT) as an
out-of-tree patch; see ``card_layout.py`` for what is shared with it (wording only).
"""

from __future__ import annotations

import asyncio
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
    ACTIVITY_MAX_CHARS, ANSWER_ELEMENT_ID, CARD_MAX_BYTES, DEFAULT_CARD_TITLE, PHASE_DONE, PHASE_RUNNING,
    PHASE_STOPPED, TIMELINE_ELEMENT_ID, ImageItem, Labels, append_images, build_stream_card, labels_for, place_images,
    render_answer, render_footer, render_timeline, summary_for, timeline_update_actions, truncate_utf8, utf8_len,
)
from .math_image import formula_digest, mathtext_available, render_formula_png
from .math_text import convert_math, iter_block_formulas

logger = logging.getLogger("hermes_feishu_cardkit")

# Streaming mode auto-closes server-side after 10 min; past this a content push may be refused.
STREAMING_TTL_SECONDS = 570.0
# Feishu "app has no permission to access this API": the app lacks ``cardkit:card:write``.
NO_PERMISSION_CODE = 99991672
# Consecutive open failures before the adapter stops offering the transport for its lifetime.
MAX_OPEN_FAILURES = 3
# A completed card accepts the turn's post-stream images for this long after it was sealed.
COMPLETED_CARD_TTL_SECONDS = 90.0
MAX_EMBEDDED_IMAGES = 6
# The raw response seen by extract_media() is only trusted for the delivery that follows it.
PENDING_MEDIA_TTL_SECONDS = 30.0
# Raw responses seen by extract_media() but not yet claimed by a delivery (several chats may
# finish at once); sealed cards kept for attachment matching.
_PENDING_MEDIA_MAX = 8
_COMPLETED_CARDS_MAX = 32
# A turn the consumer never finalizes (cancelled, /stop, crashed) is sealed as "stopped" after
# the streaming window plus a little grace, so no card stays blue forever.
_ABANDON_GRACE_SECONDS = 30.0
FORMULA_IMAGE_WAIT_SECONDS = 8.0  # finalize waits this long for in-flight formula uploads
_THREAD_CACHE_MAX = 512
_FORMULA_IMAGE_CACHE_MAX = 256
# GatewayStreamConsumer._compose_frame_content joins answer and tool-progress lines with this.
_PROGRESS_SEPARATOR = "\n\n---\n"
# Interim frames end with the consumer's cursor (▌ by default); it lands after the overlay.
_CURSOR_CHARS = "▌▍▎▏█▊▋"
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
    timeline: List[str] = field(default_factory=list)  # every tool-progress line seen this turn
    overlay: List[str] = field(default_factory=list)  # tool lines the consumer currently shows
    started_at: float = field(default_factory=time.monotonic)
    formula_tasks: Dict[str, "asyncio.Task[Optional[str]]"] = field(default_factory=dict)  # digest → upload
    watchdog: Optional["asyncio.Task[None]"] = None  # seals the card if the turn is abandoned

    def cancel_background(self) -> None:
        for task in [self.watchdog, *self.formula_tasks.values()]:
            if task is not None and not task.done():
                task.cancel()

    def next_sequence(self) -> int:
        self.sequence += 1
        return self.sequence

    @property
    def stale(self) -> bool:
        return self.elapsed >= STREAMING_TTL_SECONDS

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def tool_count(self) -> int:
        return count_tool_entries(self.timeline)

    @property
    def activity(self) -> str:
        """Latest tool line outside code fences, trimmed for the footer."""
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
    """Fold the frame's overlay into the turn timeline; True when new lines were added.

    The consumer keeps overlay lines until the next text delta, so a later frame either extends
    the current overlay (append the tail) or starts a fresh one (append it all)."""
    if lines == turn.overlay:
        return False
    new = lines[len(turn.overlay):] if turn.overlay and lines[:len(turn.overlay)] == turn.overlay else lines
    turn.overlay = list(lines)
    turn.timeline.extend(new)
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

    def _init_streaming_cards(self) -> None:
        """Idempotent; also reached lazily for adapters built without ``__init__`` (test skeletons)."""
        if "_stream_cards" in self.__dict__:
            return
        self._stream_cards: Dict[str, StreamCardTurn] = {}  # "<chat>:<turn>" → open card
        self._completed_cards: "OrderedDict[str, CompletedCard]" = OrderedDict()  # card_id → sealed card
        self._stream_card_unavailable: Optional[str] = None
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
                    and self._stream_card_unavailable is None and self._cardkit_available())

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
        turn = self._stream_cards.get(key)
        if turn is None:
            if finalize:
                logger.debug("[Feishu] send_stream_frame: no open card to finalize (key=%s)", key)
                return False
            return await self._open(chat, key, reply_to=reply_to, text=text) is not None
        if finalize:
            return await self._finalize(key, turn, text)
        return await self._push(turn, text)

    async def close_open_stream_cards(self) -> None:
        """Best-effort seal of every open card (disconnect / restart) so none stays "typing"."""
        self._init_streaming_cards()
        turns, self._stream_cards = list(self._stream_cards.values()), {}
        for turn in turns:
            turn.cancel_background()
            try:
                await self._seal(turn, self._card(turn, PHASE_STOPPED, render_answer(turn.answer.rstrip(_CURSOR_CHARS))))
            except Exception as exc:
                logger.debug("[Feishu] streaming card %s close on shutdown failed: %s", turn.card_id, exc)

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
        the chat's most recent one (concurrent turns in one chat resolve through ``raw``)."""
        for card_id in [k for k, card in self._completed_cards.items() if card.expired]:
            self._completed_cards.pop(card_id, None)
        candidates = [card for card in self._completed_cards.values() if card.turn.chat_id == chat_id]
        if raw:
            for card in reversed(candidates):
                if card.matches(raw):
                    return card
        return candidates[-1] if candidates else None

    # --- Turn lifecycle --------------------------------------------------------------------------

    async def _open(self, chat: str, key: str, *, reply_to: Optional[str], text: str) -> Optional[StreamCardTurn]:
        """Create the card entity, deliver it as an interactive message, register the turn."""
        turn = StreamCardTurn(card_id="", message_id="", chat_id=chat)
        answer, overlay = split_stream_frame(text)
        merge_overlay(turn, overlay)
        answer = truncate_utf8(answer, CARD_MAX_BYTES)
        try:
            card_id = await self._create_card(self._card(turn, PHASE_RUNNING, render_answer(answer)))
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
        turn.card_id, turn.message_id, turn.answer = card_id, result.message_id or "", answer
        turn.watchdog = asyncio.ensure_future(self._seal_if_abandoned(key))
        self._stream_cards[key] = turn
        self._stream_card_open_failures = 0
        logger.info("[Feishu] streaming card opened: card=%s message=%s chat=%s", card_id, turn.message_id, chat)
        return turn

    async def _push(self, turn: StreamCardTurn, text: str) -> bool:
        """Cumulative intermediate frame; failures are fire-and-forget (the next frame overwrites)."""
        answer, overlay = split_stream_frame(text, turn.answer)
        timeline_changed = merge_overlay(turn, overlay)
        if turn.stale:
            return True  # streaming mode is gone server-side; finalize repairs with a full update
        if answer and answer != turn.answer and utf8_len(answer) <= CARD_MAX_BYTES:
            if await self._push_element(turn, ANSWER_ELEMENT_ID, render_answer(answer)):
                turn.answer = answer
            self._prefetch_formula_images(turn, answer)
        if timeline_changed:
            labels = self._labels
            footer = render_footer(phase=PHASE_RUNNING, tool_count=turn.tool_count, elapsed=turn.elapsed,
                                   activity=turn.activity, labels=labels)
            actions = timeline_update_actions(turn.tool_count, render_timeline(turn.timeline, labels), footer, labels)
            if not await self._batch_update(turn, actions):
                await self._push_element(turn, TIMELINE_ELEMENT_ID, render_timeline(turn.timeline, labels))
        return True

    async def _finalize(self, key: str, turn: StreamCardTurn, text: str) -> bool:
        """Land the final text, then seal with the completed layout; False hands delivery back."""
        self._stream_cards.pop(key, None)
        if turn.watchdog is not None:
            turn.watchdog.cancel()
        # Finalize frames are pure text (no overlay), so no split: an answer that happens to open
        # with an emoji line must not be mistaken for tool progress — and one that was, earlier in
        # the stream, is dropped from the timeline now.
        final = (text or turn.answer or self._labels.tool_only).rstrip(_CURSOR_CHARS)
        turn.timeline = [line for line in turn.timeline if line.strip() not in final]
        turn.overlay = []
        if utf8_len(final) > CARD_MAX_BYTES:
            # Cannot fit one card: show what fits and let the consumer send the full text below.
            head = truncate_utf8(final, CARD_MAX_BYTES - 16).rstrip() + "\n\n…"
            await self._seal(turn, self._card(turn, PHASE_DONE, render_answer(head)))
            logger.info("[Feishu] streaming card %s final exceeds card limit; falling back to send()", turn.card_id)
            return False
        landed = final == turn.answer.rstrip(_CURSOR_CHARS)
        if not landed and not turn.stale:
            # Finish the typewriter before the layout switch; the full update below carries the
            # same text, so a rejected push is repaired there.
            landed = await self._push_element(turn, ANSWER_ELEMENT_ID, render_answer(final))
        await self._await_formula_images(turn, final)
        markdown = self._render_markdown(final)
        if await self._replace(turn, self._card(turn, PHASE_DONE, markdown)):
            self._completed_cards[turn.card_id] = CompletedCard(turn, final, markdown)
            while len(self._completed_cards) > _COMPLETED_CARDS_MAX:
                self._completed_cards.popitem(last=False)
            return True  # the completed layout carries the answer and closes streaming mode
        await self._close_streaming(turn)
        if not landed:
            logger.warning("[Feishu] streaming card %s could not land the final text; consumer fallback", turn.card_id)
        return landed

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
        if not await self._replace(done.turn, self._card(done.turn, PHASE_DONE, markdown)):
            return False
        self._completed_cards.pop(done.turn.card_id, None)
        logger.info("[Feishu] %d image(s) embedded into streaming card %s (%s)",
                    len(items), done.turn.card_id, "positioned" if raw else "appended")
        return True

    async def _seal_if_abandoned(self, key: str) -> None:
        """The consumer never finalizes a cancelled turn (/stop, session reset, crash): once the
        streaming window has passed, seal whatever is on screen as "stopped"."""
        await asyncio.sleep(STREAMING_TTL_SECONDS + _ABANDON_GRACE_SECONDS)
        turn = self._stream_cards.pop(key, None)
        if turn is None:
            return
        turn.cancel_background()
        logger.info("[Feishu] streaming card %s abandoned by its turn; sealing as stopped", turn.card_id)
        await self._seal(turn, self._card(turn, PHASE_STOPPED, render_answer(turn.answer.rstrip(_CURSOR_CHARS))))

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

    def _card(self, turn: StreamCardTurn, phase: str, markdown: str) -> Dict[str, Any]:
        labels = self._labels
        footer = render_footer(
            phase=phase, tool_count=turn.tool_count, elapsed=turn.elapsed,
            model=self._model_label() if phase == PHASE_DONE else "",
            activity=turn.activity if phase == PHASE_RUNNING else "", labels=labels,
        )
        return build_stream_card(
            title=str(getattr(self, "_bot_name", "") or "").strip() or DEFAULT_CARD_TITLE, markdown=markdown,
            timeline_md=render_timeline(turn.timeline, labels), tool_count=turn.tool_count, footer=footer, phase=phase,
            summary=summary_for(markdown, phase, labels), labels=labels,
        )

    def _render_markdown(self, text: str) -> str:
        """Unicode math for inline formulas; display formulas as images where one was typeset."""
        return convert_math(text, block_renderer=self._formula_image if self._formula_images_enabled() else None)

    def _formula_image(self, body: str) -> Optional[str]:
        key = self._formula_image_keys.get(formula_digest(body))
        return f"![公式]({key})" if key else None

    @staticmethod
    def _model_label() -> str:
        """Model shown in the completed footer; best-effort from the profile config."""
        try:
            from hermes_cli.config import load_config_readonly
            return str((load_config_readonly().get("model") or {}).get("default") or "").strip()
        except Exception:
            return ""

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

    async def _batch_update(self, turn: StreamCardTurn, actions: List[Dict[str, Any]]) -> bool:
        request = self._build_card_batch_update_request(card_id=turn.card_id, actions_json=_json(actions),
                                                        sequence=turn.next_sequence(), uuid_value=_uuid())
        return await self._card_op(turn, "batch update", self._client.cardkit.v1.card.batch_update, request)

    async def _replace(self, turn: StreamCardTurn, card: Dict[str, Any]) -> bool:
        """Full card rewrite (``card.update``): the completed layout, and the repair path."""
        request = self._build_card_update_request(card_id=turn.card_id, card_json=_json(card),
                                                  sequence=turn.next_sequence(), uuid_value=_uuid())
        return await self._card_op(turn, "full update", self._client.cardkit.v1.card.update, request)

    async def _close_streaming(self, turn: StreamCardTurn) -> bool:
        request = self._build_card_settings_request(card_id=turn.card_id, settings=_json({"config": {"streaming_mode": False}}),
                                                    sequence=turn.next_sequence(), uuid_value=_uuid())
        return await self._card_op(turn, "close", self._client.cardkit.v1.card.settings, request)

    async def _seal(self, turn: StreamCardTurn, card: Dict[str, Any]) -> bool:
        """Completed layout if the API takes it, else at least switch streaming mode off."""
        return await self._replace(turn, card) or await self._close_streaming(turn)

    async def _card_op(self, turn: StreamCardTurn, what: str, method: Any, request: Any) -> bool:
        """One CardKit call; rejections and exceptions are logged at DEBUG and read as False."""
        try:
            response = await self._run_blocking(method, request)
        except Exception as exc:
            logger.debug("[Feishu] streaming card %s %s raised: %s", turn.card_id, what, exc)
            return False
        if self._response_succeeded(response):
            return True
        logger.debug("[Feishu] streaming card %s %s rejected: [%s] %s",
                     turn.card_id, what, getattr(response, "code", None), getattr(response, "msg", None))
        return False

    # --- Failure accounting ------------------------------------------------------------------------

    def _note_open_failure(self, reason: str, *, code: Any = None) -> None:
        """Permission errors disable the transport at once; anything else after N misses in a row."""
        self._stream_card_open_failures += 1
        if code == NO_PERMISSION_CODE:
            self._stream_card_unavailable = reason
            logger.warning("[Feishu] streaming cards disabled: the app lacks the cardkit:card:write scope (%s). "
                           "Grant it in the Feishu developer console or set FEISHU_STREAMING_CARD=false to silence this.", reason)
        elif self._stream_card_open_failures >= MAX_OPEN_FAILURES:
            self._stream_card_unavailable = reason
            logger.warning("[Feishu] streaming cards disabled after %d consecutive open failures (last: %s); "
                           "replies fall back to edited messages", self._stream_card_open_failures, reason)
        else:
            logger.warning("[Feishu] streaming card open failed (%d/%d): %s", self._stream_card_open_failures, MAX_OPEN_FAILURES, reason)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _uuid() -> str:
    return str(uuid.uuid4())


__all__ = [
    "FeishuStreamingCardMixin", "StreamCardTurn", "CompletedCard", "split_stream_frame", "merge_overlay",
    "count_tool_entries", "STREAMING_TTL_SECONDS", "NO_PERMISSION_CODE", "MAX_OPEN_FAILURES",
    "COMPLETED_CARD_TTL_SECONDS", "MAX_EMBEDDED_IMAGES", "PENDING_MEDIA_TTL_SECONDS", "FORMULA_IMAGE_WAIT_SECONDS",
]
