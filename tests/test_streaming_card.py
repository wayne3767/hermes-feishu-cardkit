"""Behavior contracts for Feishu native streaming (CardKit streaming cards).

Drives the real ``FeishuAdapter.send_stream_frame`` with only the Lark client faked, so the
seed / push / finalize control flow, frame parsing, timeline merging, sequence numbering,
fallback and failure accounting run for real.  Assertions read the fake client's call log and
the consumer-facing return values — the two things the stream consumer keys its fallback on.
"""

from __future__ import annotations

import asyncio
import time
import json
from types import SimpleNamespace
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import pytest

from gateway.config import PlatformConfig
from feishu_cardkit import card_layout as cl
from feishu_cardkit import streaming as fs
from feishu_cardkit.adapter import CardkitFeishuAdapter as FeishuAdapter

CHAT = "oc_chat"
CARD_ID = "card_abc"
MSG_ID = "om_card_msg"
SEP = "\n\n---\n"  # GatewayStreamConsumer._compose_frame_content separator


def _ok(**data: Any) -> SimpleNamespace:
    return SimpleNamespace(success=lambda: True, code=0, msg="ok", data=SimpleNamespace(**data))


def _fail(code: int, msg: str = "boom") -> SimpleNamespace:
    return SimpleNamespace(success=lambda: False, code=code, msg=msg, data=None)


class _FakeLark:
    """Records CardKit + IM calls; per-method response scripts drive each test."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.scripts: Dict[str, List[Any]] = {}
        cardkit = SimpleNamespace(
            card=SimpleNamespace(create=self._m("create"), settings=self._m("settings"),
                                 update=self._m("update"), batch_update=self._m("batch")),
            card_element=SimpleNamespace(content=self._m("content")),
        )
        self.cardkit = SimpleNamespace(v1=cardkit)
        self.im = SimpleNamespace(v1=SimpleNamespace(message=SimpleNamespace(
            create=self._m("im_create"), reply=self._m("im_reply"), update=self._m("im_update"))))

    def _m(self, name: str):
        def _call(request: Any) -> Any:
            self.calls.append((name, request))
            script = self.scripts.get(name)
            if script:
                result = script.pop(0)
                if isinstance(result, Exception):
                    raise result
                return result
            return {"create": _ok(card_id=CARD_ID), "im_create": _ok(message_id=MSG_ID),
                    "im_reply": _ok(message_id=MSG_ID)}.get(name, _ok())
        return _call

    def named(self, name: str) -> List[Any]:
        return [req for n, req in self.calls if n == name]

    def pushes(self, element_id: str) -> List[str]:
        return [r.request_body.content for r in self.named("content") if r.element_id == element_id]

    def card_of(self, request: Any) -> Dict[str, Any]:
        body = request.request_body
        return json.loads(body.data if hasattr(body, "data") else body.card.data)


def _adapter(*, streaming_card: bool = True, bot_name: str = "") -> tuple[FeishuAdapter, _FakeLark]:
    adapter = FeishuAdapter(PlatformConfig(enabled=True, extra={"streaming_card": streaming_card}))
    adapter._bot_name = bot_name
    fake = _FakeLark()
    adapter._client = fake

    async def _direct(func, *args):
        return func(*args)
    adapter._run_blocking = _direct  # type: ignore[method-assign]
    adapter._model_label = lambda: "test-model"  # type: ignore[method-assign]
    return adapter, fake


def _run(coro):
    return asyncio.run(coro)


def _sealed_for(adapter: FeishuAdapter, chat_id: str):
    """The most recently sealed card for a chat, or None."""
    cards = [c for c in adapter._completed_cards.values() if c.turn.chat_id == chat_id]
    return cards[-1] if cards else None


def _answer_text(card: Dict[str, Any]) -> str:
    """Joined markdown of the answer body (prose, centred figures, table captions, tables)."""
    return "\n\n".join(e["content"] for e in card["body"]["elements"]
                       if str(e.get("element_id", "")).startswith(cl.ANSWER_ELEMENT_ID))


def _elements(card: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """element_id → element, including elements nested inside the collapsible panel."""
    found: Dict[str, Dict[str, Any]] = {}

    def _walk(items: List[Dict[str, Any]]) -> None:
        for e in items:
            if e.get("element_id"):
                found[e["element_id"]] = e
            _walk(e.get("elements") or [])
    _walk(card["body"]["elements"])
    return found


# --- pure helpers ------------------------------------------------------------------------------

class TestFrameParsing:
    def test_plain_text_is_all_answer(self):
        assert fs.split_stream_frame("Hello ▌") == ("Hello ▌", [])

    def test_overlay_is_split_off_and_cursor_stripped(self):
        text = "Hello" + SEP + '🔍 Searching the web for "x"\n💻 terminal\n```\nls\n```▌'
        answer, lines = fs.split_stream_frame(text)
        assert answer == "Hello"
        assert lines == ['🔍 Searching the web for "x"', "💻 terminal", "```", "ls", "```"]

    def test_overlay_only_frame_has_empty_answer(self):
        assert fs.split_stream_frame(SEP + "🔍 Searching...▌") == ("", ["🔍 Searching..."])

    def test_bare_overlay_before_any_text_is_progress(self):
        # _compose_frame_content omits the separator while nothing has accumulated.
        assert fs.split_stream_frame("🔍 Searching...▌") == ("", ["🔍 Searching..."])
        assert fs.split_stream_frame("🔍 Searching...\n💻 terminal\n```\nls\n```▌") == ("", ["🔍 Searching...", "💻 terminal", "```", "ls", "```"])
        # Once answer text exists, an emoji-led continuation is answer, not progress.
        assert fs.split_stream_frame("✅ Done▌", previous_answer="Hello") == ("✅ Done▌", [])

    def test_finalize_never_splits_emoji_led_answer(self):
        adapter, fake = _adapter()
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        assert _run(adapter.send_stream_frame("✅ 全部完成", finalize=True, chat_id=CHAT, turn_id="t1")) is True
        assert _elements(fake.card_of(fake.named("update")[0]))[cl.ANSWER_ELEMENT_ID]["content"] == "✅ 全部完成"

    def test_rule_inside_prose_stays_in_answer(self):
        text = "Part one" + SEP + "Part two is ordinary prose."
        assert fs.split_stream_frame(text) == (text, [])

    def test_cjk_answer_is_never_mistaken_for_an_overlay(self):
        # CJK code points sit above U+2000 too; only real emoji open a tool line.
        assert fs.split_stream_frame("密度 δ 是 1.45▌") == ("密度 δ 是 1.45▌", [])
        assert fs.split_stream_frame("前文" + SEP + "后文是中文正文") == ("前文" + SEP + "后文是中文正文", [])
        assert fs.split_stream_frame("✅ 完成▌") == ("", ["✅ 完成"])

    def test_rule_followed_by_emoji_prose_kept_when_answer_diverges(self):
        # The head no longer extends what was streamed: the tail is not a fresh overlay.
        text = "Completely different" + SEP + "✅ looks like a tool line"
        assert fs.split_stream_frame(text, previous_answer="Hello world") == (text, [])

    def test_count_tool_entries_handles_bare_terminal_fences(self):
        lines = ["🔍 Searching", "💻 terminal", "```", "ls", "```", "```", "pwd", "```"]
        assert fs.count_tool_entries(lines) == 3

    def test_merge_overlay_appends_only_new_lines(self):
        turn = fs.StreamCardTurn(card_id="c", message_id="m", chat_id=CHAT)
        assert fs.merge_overlay(turn, ["a"]) is True
        assert fs.merge_overlay(turn, ["a"]) is False
        assert fs.merge_overlay(turn, ["a", "b"]) is True
        assert fs.merge_overlay(turn, []) is False  # delta cleared the overlay
        assert fs.merge_overlay(turn, ["c"]) is True  # fresh overlay after the clear
        assert turn.timeline == ["a", "b", "c"]

    def test_render_timeline_caps_lines_and_bytes(self):
        lines = [f"🔧 tool {i}" for i in range(cl.TIMELINE_MAX_LINES + 10)]
        md = cl.render_timeline(lines)
        assert md.startswith("… 已省略 10 行")
        assert md.endswith(lines[-1])
        assert cl.render_timeline([]) == cl.ZH.timeline_empty

    def test_footer_and_duration(self):
        assert cl.format_duration(32.4) == "32s" and cl.format_duration(177) == "2m57s"
        assert cl.render_footer(phase="done", tool_count=4, elapsed=32, model="m") == "✅ 已完成 · 32s · m · 4 次工具调用"
        assert cl.render_footer(phase="running", tool_count=1, elapsed=3, activity="🔍 x") == "⏳ 生成中 · 1 次工具调用 · 🔍 x"
        assert cl.render_footer(phase="stopped", tool_count=0, elapsed=5) == "⏹ 已停止 · 5s"

    def test_card_layout_phases(self):
        running = cl.build_stream_card(title="T", markdown="", timeline_md="x", tool_count=0, footer="f", phase="running")
        assert running["header"]["template"] == "blue" and running["config"]["streaming_mode"] is True
        els = _elements(running)
        assert els[cl.ANSWER_ELEMENT_ID]["content"] == cl.SEED_PLACEHOLDER
        assert cl.TIMELINE_PANEL_ID in els and els[cl.TIMELINE_PANEL_ID]["expanded"] is False
        done = cl.build_stream_card(title="T", markdown="a", timeline_md="x", tool_count=0, footer="f", phase="done")
        assert done["header"]["template"] == "green" and done["config"]["streaming_mode"] is False
        assert cl.TIMELINE_PANEL_ID not in _elements(done), "no panel on a tool-free completed card"
        done_tools = cl.build_stream_card(title="T", markdown="a", timeline_md="x", tool_count=2, footer="f", phase="done")
        assert _elements(done_tools)[cl.TIMELINE_PANEL_ID]["header"]["title"]["content"] == "思考与工具 · 2 次工具调用"


# --- probe -----------------------------------------------------------------------------------

class TestProbe:
    def test_class_declares_native_streaming(self):
        assert FeishuAdapter.SUPPORTS_NATIVE_STREAMING is True

    def test_probe_requires_client_config_and_sdk(self):
        adapter, _ = _adapter()
        with patch.object(FeishuAdapter, "_cardkit_available", staticmethod(lambda: True)):
            assert adapter.supports_native_streaming(chat_type="dm") is True
            adapter._client = None
            assert adapter.supports_native_streaming() is False
        adapter._client = object()
        with patch.object(FeishuAdapter, "_cardkit_available", staticmethod(lambda: False)):
            assert adapter.supports_native_streaming() is False

    def test_probe_honours_opt_out(self):
        adapter, _ = _adapter(streaming_card=False)
        with patch.object(FeishuAdapter, "_cardkit_available", staticmethod(lambda: True)):
            assert adapter.supports_native_streaming() is False

    def test_env_opt_out(self, monkeypatch):
        monkeypatch.setenv("FEISHU_STREAMING_CARD", "false")
        assert FeishuAdapter(PlatformConfig())._streaming_card is False
        monkeypatch.setenv("FEISHU_STREAMING_CARD", "True")
        assert FeishuAdapter(PlatformConfig())._streaming_card is True


# --- seed / push / finalize ------------------------------------------------------------------

class TestTurnLifecycle:
    def test_seed_creates_running_card_and_sends_it(self):
        adapter, fake = _adapter(bot_name="小助手")
        assert _run(adapter.send_stream_frame("", chat_id=CHAT, reply_to="om_user", turn_id="t1")) is True
        (create,) = fake.named("create")
        assert create.request_body.type == "card_json"
        card = fake.card_of(create)
        assert card["schema"] == "2.0" and card["config"]["streaming_mode"] is True
        assert card["header"]["title"]["content"] == "小助手" and card["header"]["template"] == "blue"
        els = _elements(card)
        assert els[cl.ANSWER_ELEMENT_ID]["content"] == cl.SEED_PLACEHOLDER
        assert els[cl.FOOTER_ELEMENT_ID]["content"].startswith("⏳ 生成中")
        (reply,) = fake.named("im_reply")  # reply_to → reply, not a fresh chat message
        assert reply.message_id == "om_user"
        assert json.loads(reply.request_body.content) == {"type": "card", "data": {"card_id": CARD_ID}}
        assert reply.request_body.msg_type == "interactive"
        assert not fake.named("content"), "seed must not push content"
        turn = adapter._stream_cards[f"{CHAT}:t1"]
        assert (turn.card_id, turn.message_id) == (CARD_ID, MSG_ID)

    def test_default_title_without_bot_name(self):
        adapter, fake = _adapter()
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        assert fake.card_of(fake.named("create")[0])["header"]["title"]["content"] == cl.DEFAULT_CARD_TITLE

    def test_pushes_are_cumulative_with_increasing_sequence(self):
        adapter, fake = _adapter()
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        assert _run(adapter.send_stream_frame("Hel▌", chat_id=CHAT, turn_id="t1")) is True
        assert _run(adapter.send_stream_frame("Hello▌", chat_id=CHAT, turn_id="t1")) is True
        assert _run(adapter.send_stream_frame("Hello▌", chat_id=CHAT, turn_id="t1")) is True  # unchanged: no call
        pushes = fake.named("content")
        assert [p.request_body.content for p in pushes] == ["Hel▌", "Hello▌"]
        assert [p.request_body.sequence for p in pushes] == [1, 2]
        assert all(p.card_id == CARD_ID and p.element_id == cl.ANSWER_ELEMENT_ID for p in pushes)
        assert len({p.request_body.uuid for p in pushes}) == 2

    def test_tool_overlay_goes_to_timeline_not_answer(self):
        adapter, fake = _adapter()
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        _run(adapter.send_stream_frame(SEP + '🔍 Searching the web for "x"▌', chat_id=CHAT, turn_id="t1"))
        assert fake.pushes(cl.ANSWER_ELEMENT_ID) == [], "tool-only frame leaves the placeholder"
        (batch,) = fake.named("batch")
        actions = json.loads(batch.request_body.actions)
        by_id = {a["params"]["element_id"]: a["params"]["partial_element"] for a in actions}
        assert by_id[cl.TIMELINE_ELEMENT_ID]["content"] == '🔍 Searching the web for "x"'
        assert by_id[cl.TIMELINE_PANEL_ID]["header"]["title"]["content"] == "思考与工具 · 1 次工具调用"
        assert '🔍 Searching the web for "x"' in by_id[cl.FOOTER_ELEMENT_ID]["content"]
        # Text arrives: overlay clears, answer streams, timeline keeps the entry.
        _run(adapter.send_stream_frame("The answer▌", chat_id=CHAT, turn_id="t1"))
        assert fake.pushes(cl.ANSWER_ELEMENT_ID) == ["The answer▌"]
        assert len(fake.named("batch")) == 1
        # Second tool after text (strategy B): stacked below the text, appended to the timeline.
        _run(adapter.send_stream_frame("The answer" + SEP + "💻 terminal\n```\nls\n```▌", chat_id=CHAT, turn_id="t1"))
        turn = adapter._stream_cards[f"{CHAT}:t1"]
        assert turn.timeline == ['🔍 Searching the web for "x"', "💻 terminal", "```", "ls", "```"]
        assert turn.tool_count == 2
        assert len(fake.named("batch")) == 2

    def test_batch_failure_falls_back_to_timeline_content_push(self):
        adapter, fake = _adapter()
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        fake.scripts["batch"] = [_fail(10002, "action is invalid")]
        _run(adapter.send_stream_frame(SEP + "🔍 Searching▌", chat_id=CHAT, turn_id="t1"))
        assert fake.pushes(cl.TIMELINE_ELEMENT_ID) == ["🔍 Searching"]

    def test_finalize_lands_text_then_seals_with_completed_layout(self):
        adapter, fake = _adapter()
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        _run(adapter.send_stream_frame(SEP + "🔍 Searching▌", chat_id=CHAT, turn_id="t1"))
        _run(adapter.send_stream_frame("Hello▌", chat_id=CHAT, turn_id="t1"))
        assert _run(adapter.send_stream_frame("Hello world", finalize=True, chat_id=CHAT, turn_id="t1")) is True
        names = [n for n, _ in fake.calls]
        assert names.index("update") > names.index("content")
        assert fake.pushes(cl.ANSWER_ELEMENT_ID)[-1] == "Hello world"
        (update,) = fake.named("update")
        card = fake.card_of(update)
        assert card["config"]["streaming_mode"] is False and card["header"]["template"] == "green"
        assert card["header"]["subtitle"]["content"] == "已完成"
        els = _elements(card)
        assert els[cl.ANSWER_ELEMENT_ID]["content"] == "Hello world"
        assert els[cl.TIMELINE_PANEL_ID]["header"]["title"]["content"] == "思考与工具 · 1 次工具调用"
        assert els[cl.TIMELINE_ELEMENT_ID]["content"] == "🔍 Searching"
        assert els[cl.FOOTER_ELEMENT_ID]["content"].startswith("✅ 已完成 · ") and "test-model" in els[cl.FOOTER_ELEMENT_ID]["content"]
        assert card["config"]["summary"]["content"] == "Hello world"
        assert not fake.named("settings"), "a successful full update already closes streaming mode"
        seqs = [c.request_body.sequence for n, c in fake.calls if n in ("content", "batch", "update")]
        assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
        assert f"{CHAT}:t1" not in adapter._stream_cards

    def test_finalize_without_new_text_skips_redundant_push(self):
        adapter, fake = _adapter()
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        _run(adapter.send_stream_frame("done▌", chat_id=CHAT, turn_id="t1"))
        assert _run(adapter.send_stream_frame("done", finalize=True, chat_id=CHAT, turn_id="t1")) is True
        assert fake.pushes(cl.ANSWER_ELEMENT_ID) == ["done▌"]
        assert len(fake.named("update")) == 1

    def test_tool_only_turn_finalizes_with_placeholder(self):
        adapter, fake = _adapter()
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        assert _run(adapter.send_stream_frame("", finalize=True, chat_id=CHAT, turn_id="t1")) is True
        assert _elements(fake.card_of(fake.named("update")[0]))[cl.ANSWER_ELEMENT_ID]["content"] == "✅"

    def test_emoji_led_answer_is_pruned_from_timeline_on_finalize(self):
        adapter, fake = _adapter()
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        _run(adapter.send_stream_frame("✅ 检查通过▌", chat_id=CHAT, turn_id="t1"))  # mistaken for a tool line
        assert adapter._stream_cards[f"{CHAT}:t1"].timeline == ["✅ 检查通过"]
        assert _run(adapter.send_stream_frame("✅ 检查通过，一切正常。", finalize=True, chat_id=CHAT, turn_id="t1")) is True
        card = fake.card_of(fake.named("update")[0])
        assert cl.TIMELINE_PANEL_ID not in _elements(card), "no tool panel once the bogus entry is dropped"
        assert "0 次工具调用" in _elements(card)[cl.FOOTER_ELEMENT_ID]["content"]

    def test_first_content_frame_without_seed_opens_card_with_text(self):
        adapter, fake = _adapter()
        assert _run(adapter.send_stream_frame("Hi there▌", chat_id=CHAT, turn_id="t2")) is True
        card = fake.card_of(fake.named("create")[0])
        assert _elements(card)[cl.ANSWER_ELEMENT_ID]["content"] == "Hi there▌"
        assert not fake.named("content")
        assert adapter._stream_cards[f"{CHAT}:t2"].answer == "Hi there▌"

    def test_concurrent_turns_do_not_share_cards(self):
        adapter, fake = _adapter()
        fake.scripts["create"] = [_ok(card_id="card_1"), _ok(card_id="card_2")]
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="a"))
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="b"))
        _run(adapter.send_stream_frame("A", chat_id=CHAT, turn_id="a"))
        _run(adapter.send_stream_frame("B", chat_id=CHAT, turn_id="b"))
        pushes = {p.request_body.content: p.card_id for p in fake.named("content")}
        assert pushes == {"A": "card_1", "B": "card_2"}


# --- fallback contract -----------------------------------------------------------------------

class TestFallbackContract:
    def test_finalize_on_unknown_turn_returns_false_without_calls(self):
        adapter, fake = _adapter()
        assert _run(adapter.send_stream_frame("x", finalize=True, chat_id=CHAT, turn_id="ghost")) is False
        assert fake.calls == []

    def test_missing_chat_id_returns_false(self):
        adapter, fake = _adapter()
        assert _run(adapter.send_stream_frame("x", chat_id="")) is False
        assert fake.calls == []

    def test_intermediate_push_failure_is_fire_and_forget(self):
        adapter, fake = _adapter()
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        fake.scripts["content"] = [_fail(230099, "rate limited")]
        assert _run(adapter.send_stream_frame("partial", chat_id=CHAT, turn_id="t1")) is True
        turn = adapter._stream_cards[f"{CHAT}:t1"]
        assert turn.answer == "", "rejected frame must not count as landed"

    def test_final_push_rejected_still_lands_via_full_update(self):
        adapter, fake = _adapter()
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        fake.scripts["content"] = [_fail(230099, "streaming closed")]
        assert _run(adapter.send_stream_frame("final", finalize=True, chat_id=CHAT, turn_id="t1")) is True
        assert _elements(fake.card_of(fake.named("update")[0]))[cl.ANSWER_ELEMENT_ID]["content"] == "final"

    def test_full_update_rejected_falls_back_to_settings_close(self):
        adapter, fake = _adapter()
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        fake.scripts["update"] = [_fail(230099)]
        assert _run(adapter.send_stream_frame("final", finalize=True, chat_id=CHAT, turn_id="t1")) is True  # content push landed
        assert len(fake.named("settings")) == 1

    def test_final_that_cannot_land_returns_false_and_seals(self):
        adapter, fake = _adapter()
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        fake.scripts["content"] = [_fail(230099)]
        fake.scripts["update"] = [_fail(230099)]
        assert _run(adapter.send_stream_frame("final", finalize=True, chat_id=CHAT, turn_id="t1")) is False
        assert len(fake.named("settings")) == 1
        assert f"{CHAT}:t1" not in adapter._stream_cards

    def test_stale_turn_skips_streaming_push_and_repairs_on_finalize(self):
        adapter, fake = _adapter()
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        adapter._stream_cards[f"{CHAT}:t1"].started_at -= fs.STREAMING_TTL_SECONDS + 1
        assert _run(adapter.send_stream_frame("late", chat_id=CHAT, turn_id="t1")) is True
        assert not fake.named("content")
        assert _run(adapter.send_stream_frame("late final", finalize=True, chat_id=CHAT, turn_id="t1")) is True
        assert not fake.named("content") and len(fake.named("update")) == 1

    def test_oversized_final_hands_delivery_back_to_consumer(self):
        adapter, fake = _adapter()
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        big = "字" * (cl.CARD_MAX_BYTES // 3 + 100)  # 3 bytes per char → over the byte cap
        assert _run(adapter.send_stream_frame(big, chat_id=CHAT, turn_id="t1")) is True
        assert not fake.named("content"), "oversized intermediate is not pushed"
        assert _run(adapter.send_stream_frame(big, finalize=True, chat_id=CHAT, turn_id="t1")) is False
        head = _elements(fake.card_of(fake.named("update")[0]))[cl.ANSWER_ELEMENT_ID]["content"]
        assert head.endswith("…") and len(head.encode("utf-8")) <= cl.CARD_MAX_BYTES
        assert f"{CHAT}:t1" not in adapter._stream_cards


# --- failure accounting -----------------------------------------------------------------------

class TestFailureAccounting:
    def test_permission_error_disables_transport_immediately(self):
        adapter, fake = _adapter()
        fake.scripts["create"] = [_fail(fs.NO_PERMISSION_CODE, "no permission")]
        with patch.object(FeishuAdapter, "_cardkit_available", staticmethod(lambda: True)):
            assert adapter.supports_native_streaming() is True
            assert _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1")) is False
            assert adapter.supports_native_streaming() is False
        assert not fake.named("im_create") and not fake.named("im_reply")

    def test_transient_open_failures_disable_after_threshold(self):
        adapter, fake = _adapter()
        fake.scripts["create"] = [RuntimeError("net")] * fs.MAX_OPEN_FAILURES
        with patch.object(FeishuAdapter, "_cardkit_available", staticmethod(lambda: True)):
            for i in range(fs.MAX_OPEN_FAILURES):
                assert _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id=f"t{i}")) is False
                assert adapter.supports_native_streaming() is (i < fs.MAX_OPEN_FAILURES - 1)

    def test_success_resets_failure_counter(self):
        adapter, fake = _adapter()
        fake.scripts["create"] = [RuntimeError("net"), _ok(card_id=CARD_ID)]
        assert _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1")) is False
        assert _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t2")) is True
        assert adapter._stream_card_open_failures == 0

    def test_card_message_send_failure_counts_as_open_failure(self):
        adapter, fake = _adapter()
        fake.scripts["im_create"] = [_fail(230001, "bot not in chat")]
        assert _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1")) is False
        assert adapter._stream_card_open_failures == 1
        assert not adapter._stream_cards


# --- routing & lifecycle ----------------------------------------------------------------------

class TestRoutingAndShutdown:
    def test_topic_reply_stays_in_thread(self):
        adapter, fake = _adapter()
        adapter.remember_thread_for_message("om_user", "omt_thread")
        _run(adapter.send_stream_frame("", chat_id=CHAT, reply_to="om_user", turn_id="t1"))
        assert fake.named("im_reply")[0].request_body.reply_in_thread is True

    def test_plain_reply_does_not_open_thread(self):
        adapter, fake = _adapter()
        adapter.remember_thread_for_message("om_user", None)
        _run(adapter.send_stream_frame("", chat_id=CHAT, reply_to="om_user", turn_id="t1"))
        assert fake.named("im_reply")[0].request_body.reply_in_thread is False

    def test_thread_cache_is_bounded(self):
        adapter, _ = _adapter()
        for i in range(fs._THREAD_CACHE_MAX + 5):
            adapter.remember_thread_for_message(f"om_{i}", "omt")
        assert len(adapter._stream_thread_ids) == fs._THREAD_CACHE_MAX
        assert "om_0" not in adapter._stream_thread_ids

    def test_close_open_stream_cards_marks_them_stopped(self):
        adapter, fake = _adapter()
        fake.scripts["create"] = [_ok(card_id="c1"), _ok(card_id="c2")]
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="a"))
        _run(adapter.send_stream_frame("partial▌", chat_id="oc_other", turn_id="b"))
        fake.scripts["update"] = [RuntimeError("gone"), _ok()]
        _run(adapter.close_open_stream_cards())
        updates = fake.named("update")
        assert {u.card_id for u in updates} == {"c1", "c2"}
        stopped = [fake.card_of(u) for u in updates if u.card_id == "c2"][0]
        assert stopped["header"]["template"] == "grey" and _elements(stopped)[cl.ANSWER_ELEMENT_ID]["content"] == "partial"
        assert [s.card_id for s in fake.named("settings")] == ["c1"], "settings close only where the full update failed"
        assert adapter._stream_cards == {}


# --- consumer integration ----------------------------------------------------------------------

class TestConsumerIntegration:
    """The real GatewayStreamConsumer over the real adapter: one card, frames in order, sealed once."""

    def test_consumer_streams_one_card_and_seals_it(self):
        from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

        adapter, fake = _adapter()

        async def _scenario() -> None:
            with patch.object(FeishuAdapter, "_cardkit_available", staticmethod(lambda: True)):
                consumer = GatewayStreamConsumer(
                    adapter, CHAT, StreamConsumerConfig(chat_type="dm", cursor="▌", edit_interval=0.01),
                )
                task = asyncio.create_task(consumer.run())
                await asyncio.sleep(0.05)
                consumer.on_tool_progress('🔍 Searching the web for "feishu"')
                await asyncio.sleep(0.1)
                consumer.on_delta("Hello")
                await asyncio.sleep(0.1)
                consumer.on_delta(" world")
                consumer.finish("Hello world")
                await asyncio.wait_for(task, timeout=5)

        _run(_scenario())
        assert len(fake.named("create")) == 1
        answer_pushes = fake.pushes(cl.ANSWER_ELEMENT_ID)
        assert answer_pushes and answer_pushes[-1] == "Hello world"
        assert all("Searching" not in p and "---" not in p for p in answer_pushes), "tool lines never reach the answer"
        (batch,) = fake.named("batch")
        assert "Searching the web" in batch.request_body.actions
        (update,) = fake.named("update")
        card = fake.card_of(update)
        assert card["header"]["template"] == "green"
        els = _elements(card)
        assert els[cl.ANSWER_ELEMENT_ID]["content"] == "Hello world"
        assert els[cl.TIMELINE_ELEMENT_ID]["content"] == '🔍 Searching the web for "feishu"'
        seqs = [c.request_body.sequence for n, c in fake.calls if n in ("content", "batch", "update", "settings")]
        assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))
        assert adapter._stream_cards == {}
        assert not fake.named("im_update"), "native path never falls back to message.update"


class TestMathRendering:
    def test_answer_pushes_and_final_card_use_unicode_math(self):
        adapter, fake = _adapter()
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        _run(adapter.send_stream_frame("密度 $\\delta_p = 1.45$，偏差 $E_p = \\frac{\\delta_{75}▌", chat_id=CHAT, turn_id="t1"))
        first = fake.pushes(cl.ANSWER_ELEMENT_ID)[-1]
        assert "δₚ = 1.45" in first and "$E_p = \\frac{\\delta_{75}▌" in first, "closed formula converted, open one kept raw"
        final = "密度 $\\delta_p = 1.45$，偏差 $E_p = \\frac{\\delta_{75} - \\delta_{25}}{2}$"
        assert _run(adapter.send_stream_frame(final, finalize=True, chat_id=CHAT, turn_id="t1")) is True
        assert fake.pushes(cl.ANSWER_ELEMENT_ID)[-1] == "密度 δₚ = 1.45，偏差 Eₚ = (δ₇₅ - δ₂₅)/2"
        card = fake.card_of(fake.named("update")[0])
        assert _elements(card)[cl.ANSWER_ELEMENT_ID]["content"] == "密度 δₚ = 1.45，偏差 Eₚ = (δ₇₅ - δ₂₅)/2"
        assert card["config"]["summary"]["content"].startswith("密度 δₚ")


class TestFormulaImages:
    """Display formulas become uploaded images in the completed card; inline stays Unicode."""

    @staticmethod
    def _with_images(adapter: FeishuAdapter, fake: _FakeLark, render):
        fake.im.v1.image = SimpleNamespace(create=fake._m("image"))
        fake.scripts["image"] = [_ok(image_key="img_1"), _ok(image_key="img_2"), _ok(image_key="img_3")]
        adapter._card_math_images = True
        return patch("feishu_cardkit.streaming.mathtext_available", lambda: True), \
            patch("feishu_cardkit.streaming.render_formula_png", render)

    def test_blocks_become_images_inline_stays_unicode(self):
        adapter, fake = _adapter()
        p1, p2 = self._with_images(adapter, fake, lambda body: b"\x89PNG" + body.encode())

        async def _scenario():
            with p1, p2:
                await adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1")
                await adapter.send_stream_frame("密度 $\\delta_p$：\n\n$$E_p = \\frac{a}{b}$$\n\n再来▌", chat_id=CHAT, turn_id="t1")
                await asyncio.sleep(0)  # let the prefetch task run
                final = "密度 $\\delta_p$：\n\n$$E_p = \\frac{a}{b}$$\n\n再来一个 \\[I = \\frac{E_p}{\\delta_p - 1}\\] 完"
                return await adapter.send_stream_frame(final, finalize=True, chat_id=CHAT, turn_id="t1")

        assert _run(_scenario()) is True
        uploads = fake.named("image")
        assert len(uploads) == 2 and all(u.request_body.image_type == "message" for u in uploads)
        content = _elements(fake.card_of(fake.named("update")[0]))[cl.ANSWER_ELEMENT_ID]["content"]
        assert "![公式](img_1)" in content and "![公式](img_2)" in content
        assert "δₚ" in content and "$" not in content and "frac" not in content
        # Intermediate pushes never carry image markdown (typewriter text stays Unicode).
        assert all("![" not in p for p in fake.pushes(cl.ANSWER_ELEMENT_ID))

    def test_renderer_failure_falls_back_to_unicode(self):
        adapter, fake = _adapter()
        p1, p2 = self._with_images(adapter, fake, lambda body: None)

        async def _scenario():
            with p1, p2:
                await adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1")
                return await adapter.send_stream_frame("$$\\begin{bmatrix} a & b \\\\ c & d \\end{bmatrix}$$", finalize=True, chat_id=CHAT, turn_id="t1")

        assert _run(_scenario()) is True
        assert not fake.named("image")
        content = _elements(fake.card_of(fake.named("update")[0]))[cl.ANSWER_ELEMENT_ID]["content"]
        assert "[a b; c d]" in content and "![" not in content

    def test_image_cache_reuses_uploads_across_turns(self):
        adapter, fake = _adapter()
        p1, p2 = self._with_images(adapter, fake, lambda body: b"png")

        async def _scenario():
            with p1, p2:
                for turn in ("a", "b"):
                    await adapter.send_stream_frame("", chat_id=CHAT, turn_id=turn)
                    await adapter.send_stream_frame("$$x^2$$", finalize=True, chat_id=CHAT, turn_id=turn)

        _run(_scenario())
        assert len(fake.named("image")) == 1
        assert all("![公式](img_1)" in _elements(fake.card_of(u))[cl.ANSWER_ELEMENT_ID]["content"] for u in fake.named("update"))

    def test_setting_off_keeps_unicode(self):
        adapter, fake = _adapter()
        p1, p2 = self._with_images(adapter, fake, lambda body: b"png")
        adapter._card_math_images = False

        async def _scenario():
            with p1, p2:
                await adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1")
                return await adapter.send_stream_frame("$$x^2$$", finalize=True, chat_id=CHAT, turn_id="t1")

        assert _run(_scenario()) is True
        assert not fake.named("image")
        assert "x²" in _elements(fake.card_of(fake.named("update")[0]))[cl.ANSWER_ELEMENT_ID]["content"]

    def test_env_toggle(self, monkeypatch):
        monkeypatch.setenv("FEISHU_CARD_MATH_IMAGES", "false")
        assert FeishuAdapter(PlatformConfig())._card_math_images is False


class TestPostStreamImages:
    """MEDIA images delivered right after a streamed turn are embedded into that turn's card."""

    @staticmethod
    def _finalized(adapter: FeishuAdapter, fake: _FakeLark) -> None:
        fake.im.v1.image = SimpleNamespace(create=fake._m("image"))
        fake.scripts["image"] = [_ok(image_key="img_a"), _ok(image_key="img_b")]
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        assert _run(adapter.send_stream_frame("答案正文", finalize=True, chat_id=CHAT, turn_id="t1")) is True
        assert _sealed_for(adapter, CHAT) is not None

    def test_images_are_appended_to_the_completed_card(self, tmp_path):
        adapter, fake = _adapter()
        self._finalized(adapter, fake)
        files = []
        for name in ("a.png", "b.png"):
            f = tmp_path / name; f.write_bytes(b"\x89PNG"); files.append(f"file://{f}")
        with patch.object(FeishuAdapter, "send_image_file", side_effect=AssertionError("must not send separately")):
            _run(adapter.send_multiple_images(CHAT, [(files[0], "曲线"), (files[1], "")]))
        assert len(fake.named("image")) == 2
        updates = fake.named("update")
        assert len(updates) == 2, "completed layout, then the embedded layout"
        card_json = fake.card_of(updates[-1])
        assert _answer_text(card_json) == ("答案正文\n\n![曲线](img_a)\n<font color=\"grey\">图1　曲线</font>"
                                          "\n\n![b](img_b)\n<font color=\"grey\">图2　b</font>")
        figures = [e for e in card_json["body"]["elements"] if e.get("text_align") == "center"]
        assert len(figures) == 2 and all(e["content"].startswith("![") for e in figures), "figure + caption centred"
        assert fake.card_of(updates[-1])["header"]["template"] == "green"
        assert updates[-1].request_body.sequence > updates[0].request_body.sequence
        assert _sealed_for(adapter, CHAT) is None, "a card takes one attachment batch"

    def test_no_recent_card_uses_normal_delivery(self, tmp_path):
        adapter, fake = _adapter()
        f = tmp_path / "a.png"; f.write_bytes(b"png")
        sent = []
        async def _send_image_file(**kw):
            sent.append(kw["image_path"]); return SimpleNamespace(success=True)
        with patch.object(FeishuAdapter, "send_image_file", side_effect=_send_image_file):
            _run(adapter.send_multiple_images(CHAT, [(f"file://{f}", "")]))
        assert sent == [str(f)] and not fake.named("update")

    def test_expired_card_or_remote_url_uses_normal_delivery(self, tmp_path):
        adapter, fake = _adapter()
        self._finalized(adapter, fake)
        done = _sealed_for(adapter, CHAT)
        done.sealed_at = time.monotonic() - fs.COMPLETED_CARD_TTL_SECONDS - 1
        calls = []
        async def _send_image(**kw):
            calls.append(kw); return SimpleNamespace(success=True)
        with patch.object(FeishuAdapter, "send_image_file", side_effect=_send_image), \
             patch.object(FeishuAdapter, "send_image", side_effect=_send_image):
            f = tmp_path / "a.png"; f.write_bytes(b"png")
            _run(adapter.send_multiple_images(CHAT, [(f"file://{f}", "")]))
            done.sealed_at = time.monotonic()
            adapter._completed_cards[done.turn.card_id] = done
            _run(adapter.send_multiple_images(CHAT, [("https://example.com/x.png", "")]))
        assert len(calls) == 2 and len(fake.named("update")) == 1

    def test_upload_failure_falls_back_without_losing_images(self, tmp_path):
        adapter, fake = _adapter()
        self._finalized(adapter, fake)
        fake.scripts["image"] = [_fail(230001, "upload refused")]
        f = tmp_path / "a.png"; f.write_bytes(b"png")
        sent = []
        async def _send_image_file(**kw):
            sent.append(kw["image_path"]); return SimpleNamespace(success=True)
        with patch.object(FeishuAdapter, "send_image_file", side_effect=_send_image_file):
            _run(adapter.send_multiple_images(CHAT, [(f"file://{f}", "")]))
        assert sent == [str(f)] and len(fake.named("update")) == 1


class TestPositionedImages:
    """With the raw response in hand, images land where their MEDIA: lines stood, captioned 图N."""

    def test_title_from_filename(self):
        assert cl.title_from_filename("/tmp/hermes_fig_正态分布密度曲线对比_1725600000.png") == "正态分布密度曲线对比"
        assert cl.title_from_filename("/tmp/fig-ash_trend-20260906.png") == "ash trend"
        assert cl.title_from_filename("/x/灰分趋势.png") == "灰分趋势"
        assert cl.title_from_filename("/x/1725600000.png") == "图片"

    def _setup(self, tmp_path):
        adapter, fake = _adapter()
        adapter._card_math_images = False  # keep the scripted image keys for the figures, not formulas
        fake.im.v1.image = SimpleNamespace(create=fake._m("image"))
        fake.scripts["image"] = [_ok(image_key="img_a"), _ok(image_key="img_b")]
        a = tmp_path / "hermes_fig_正态分布曲线_1725600000.png"; a.write_bytes(b"png")
        b = tmp_path / "hermes_fig_灰分趋势_1725600001.png"; b.write_bytes(b"png")
        raw = (f"第一段讲曲线。\n\n**MEDIA:{a}**\n\n第二段讲灰分，公式 $$E_p = \\frac{{a}}{{b}}$$ 在这里。\n\n"
               f"MEDIA:{b}\n图2：本周精煤灰分趋势\n\n结尾。")
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        assert _run(adapter.send_stream_frame("第一段讲曲线。\n\n第二段讲灰分，公式 $$E_p = \\frac{a}{b}$$ 在这里。\n\n结尾。",
                                              finalize=True, chat_id=CHAT, turn_id="t1")) is True
        adapter.extract_media(raw)  # what the gateway does right before send_multiple_images
        return adapter, fake, a, b

    def test_images_land_at_their_media_positions_with_captions(self, tmp_path):
        adapter, fake, a, b = self._setup(tmp_path)
        with patch.object(FeishuAdapter, "send_image_file", side_effect=AssertionError("must not send separately")):
            _run(adapter.send_multiple_images(CHAT, [(f"file://{a}", ""), (f"file://{b}", "")]))
        content = _answer_text(fake.card_of(fake.named("update")[-1]))
        assert content == (
            "第一段讲曲线。\n\n![正态分布曲线](img_a)\n<font color=\"grey\">图1　正态分布曲线</font>\n\n"
            "第二段讲灰分，公式\n\n　　Eₚ = a/b\n\n在这里。\n\n"
            "![本周精煤灰分趋势](img_b)\n<font color=\"grey\">图2　本周精煤灰分趋势</font>\n\n结尾。"
        )
        assert "MEDIA:" not in content and "**" not in content

    def test_unmentioned_file_is_appended_and_numbered_last(self, tmp_path):
        adapter, fake, a, b = self._setup(tmp_path)
        c = tmp_path / "hermes_fig_额外_1.png"; c.write_bytes(b"png")
        fake.scripts["image"] = [_ok(image_key="img_a"), _ok(image_key="img_c")]
        _run(adapter.send_multiple_images(CHAT, [(f"file://{a}", ""), (f"file://{c}", "")]))
        content = _answer_text(fake.card_of(fake.named("update")[-1]))
        assert content.index("图1　正态分布曲线") < content.index("结尾。") < content.index("图2　额外")

    def test_stale_raw_response_falls_back_to_appending(self, tmp_path):
        adapter, fake, a, b = self._setup(tmp_path)
        raw, _ = adapter._pending_media[-1]
        adapter._pending_media[-1] = (raw, time.monotonic() - fs.PENDING_MEDIA_TTL_SECONDS - 1)
        _run(adapter.send_multiple_images(CHAT, [(f"file://{a}", "")]))
        content = _answer_text(fake.card_of(fake.named("update")[-1]))
        assert content.endswith("<font color=\"grey\">图1　正态分布曲线</font>") and content.startswith("第一段讲曲线。")


class TestAnswerLayout:
    """Completed cards split the answer into prose, centred figures and captioned tables."""

    def test_figure_blocks_are_centred_and_prose_keeps_ids(self):
        md = "开头。\n\n![曲线](img_1)\n<font color=\"grey\">图1　曲线</font>\n\n结尾。"
        els = cl.layout_answer_elements(md)
        assert [e.get("text_align") for e in els] == [None, "center", None]
        assert [e["element_id"] for e in els] == [cl.ANSWER_ELEMENT_ID, f"{cl.ANSWER_ELEMENT_ID}_1", f"{cl.ANSWER_ELEMENT_ID}_2"]
        assert els[1]["content"].startswith("![曲线](img_1)")

    def test_table_gets_centred_title_from_caption_line(self):
        md = "参数如下：\n\n表1：各参数含义\n| 符号 | 含义 |\n|---|---|\n| μ | 均值 |\n\n然后。\n\n| a | b |\n|---|---|\n| 1 | 2 |"
        els = cl.layout_answer_elements(md)
        contents = [e["content"] for e in els]
        assert contents[0] == "参数如下："
        assert els[1]["text_align"] == "center" and contents[1] == "<font color=\"grey\">表1　各参数含义</font>"
        assert contents[2].startswith("| 符号 | 含义 |")
        assert contents[3] == "然后。"
        assert contents[4] == "<font color=\"grey\">表2</font>", "table without a caption line is numbered only"
        assert contents[5].startswith("| a | b |")
        assert "表1：" not in "".join(contents), "the raw caption line is consumed"

    def test_caption_in_previous_paragraph_is_used(self):
        md = "表1：各参数含义\n\n| 符号 | 含义 |\n|---|---|\n| μ | 均值 |"
        els = cl.layout_answer_elements(md)
        assert [e["content"] for e in els][:2] == ["<font color=\"grey\">表1　各参数含义</font>", "| 符号 | 含义 |\n|---|---|\n| μ | 均值 |"]

    def test_bold_caption_line_and_english_table_word(self):
        md = "**Table 3: Results**\n| x |\n|---|\n| 1 |"
        els = cl.layout_answer_elements(md)
        assert els[0]["content"] == "<font color=\"grey\">表1　Results</font>"

    def test_pipe_text_that_is_not_a_table_stays_prose(self):
        md = "命令 | 管道 | 说明\n没有分隔行"
        els = cl.layout_answer_elements(md)
        assert len(els) == 1 and els[0]["content"] == md

    def test_completed_card_uses_layout_but_running_card_does_not(self):
        running = cl.build_stream_card(title="T", markdown="| a |\n|---|\n| 1 |", timeline_md="", tool_count=0, footer="f", phase="running")
        assert [e.get("element_id") for e in running["body"]["elements"][:1]] == [cl.ANSWER_ELEMENT_ID]
        assert "表1" not in json.dumps(running, ensure_ascii=False)
        done = cl.build_stream_card(title="T", markdown="| a |\n|---|\n| 1 |", timeline_md="", tool_count=0, footer="f", phase="done")
        assert "表1" in json.dumps(done, ensure_ascii=False)


class TestRobustness:
    """Abandoned turns, concurrent turns and cross-chat deliveries."""

    def test_abandoned_turn_is_sealed_as_stopped_by_the_watchdog(self, monkeypatch):
        monkeypatch.setattr(fs, "STREAMING_TTL_SECONDS", 0.05)
        monkeypatch.setattr(fs, "_ABANDON_GRACE_SECONDS", 0.0)
        adapter, fake = _adapter()

        async def _scenario():
            await adapter.send_stream_frame("partial▌", chat_id=CHAT, turn_id="t1")  # never finalized
            await asyncio.sleep(0.3)

        _run(_scenario())
        assert f"{CHAT}:t1" not in adapter._stream_cards
        (update,) = fake.named("update")
        card = fake.card_of(update)
        assert card["header"]["template"] == "grey" and card["config"]["streaming_mode"] is False
        assert _answer_text(card).startswith("partial")

    def test_finalized_turn_cancels_its_watchdog(self):
        adapter, fake = _adapter()

        async def _scenario():
            await adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1")
            turn = adapter._stream_cards[f"{CHAT}:t1"]
            await adapter.send_stream_frame("done", finalize=True, chat_id=CHAT, turn_id="t1")
            await asyncio.sleep(0)
            return turn.watchdog.cancelled() or turn.watchdog.done()

        assert _run(_scenario()) is True

    def test_attachments_pick_the_card_whose_text_they_belong_to(self, tmp_path):
        adapter, fake = _adapter()
        adapter._card_math_images = False
        fake.im.v1.image = SimpleNamespace(create=fake._m("image"))
        fake.scripts["create"] = [_ok(card_id="card_1"), _ok(card_id="card_2")]
        fake.scripts["image"] = [_ok(image_key="img_x")]
        f = tmp_path / "hermes_fig_曲线_1.png"; f.write_bytes(b"png")
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="a"))
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="b"))
        assert _run(adapter.send_stream_frame("回答 A 的正文。", finalize=True, chat_id=CHAT, turn_id="a")) is True
        assert _run(adapter.send_stream_frame("回答 B 的正文。", finalize=True, chat_id=CHAT, turn_id="b")) is True
        adapter.extract_media(f"回答 A 的正文。\n\nMEDIA:{f}")  # A's attachment arrives after B sealed
        _run(adapter.send_multiple_images(CHAT, [(f"file://{f}", "")]))
        embedded = [u for u in fake.named("update") if "img_x" in json.dumps(fake.card_of(u))]
        assert len(embedded) == 1 and embedded[0].card_id == "card_1"

    def test_pending_media_survives_another_chat_finishing_in_between(self, tmp_path):
        adapter, fake = _adapter()
        adapter._card_math_images = False
        fake.im.v1.image = SimpleNamespace(create=fake._m("image"))
        fake.scripts["image"] = [_ok(image_key="img_x")]
        f = tmp_path / "hermes_fig_曲线_1.png"; f.write_bytes(b"png")
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        _run(adapter.send_stream_frame("正文。", finalize=True, chat_id=CHAT, turn_id="t1"))
        adapter.extract_media(f"正文。\n\nMEDIA:{f}")
        adapter.extract_media("另一个聊天的回答，没有附件。")  # a later extract from another chat
        _run(adapter.send_multiple_images(CHAT, [(f"file://{f}", "")]))
        content = _answer_text(fake.card_of(fake.named("update")[-1]))
        assert content.index("正文。") < content.index("](img_x)"), "positioned from the matching raw response"

    def test_fenced_pipes_are_not_tables_and_titles_are_bracket_safe(self):
        md = "示例：\n\n```\nls | grep x\n---|---\n```\n\n说明。"
        els = cl.layout_answer_elements(md)
        assert len(els) == 1 and "表1" not in json.dumps(els, ensure_ascii=False)
        block = cl.image_block(1, "f(x) [a]", "k")
        assert block.startswith("![f（x） ［a］](k)")
        assert cl.summary_for("![公式](k)\n<font color=\"grey\">图1　x</font>\n\n真正的第一句。", cl.PHASE_DONE) == "真正的第一句。"


class TestLocale:
    """Feishu (China) speaks Chinese; Lark (international) speaks English; FEISHU_CARD_LOCALE overrides."""

    def test_labels_for(self):
        assert cl.labels_for("feishu") is cl.ZH and cl.labels_for("lark") is cl.EN
        assert cl.labels_for("lark", "zh") is cl.ZH and cl.labels_for("feishu", "en") is cl.EN

    def test_lark_domain_renders_english_card(self, tmp_path):
        adapter, fake = _adapter()
        adapter._domain_name = "lark"
        adapter._card_math_images = False
        fake.im.v1.image = SimpleNamespace(create=fake._m("image"))
        fake.scripts["image"] = [_ok(image_key="img_a")]
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        running = fake.card_of(fake.named("create")[0])
        assert running["header"]["subtitle"]["content"] == "Generating" and running["config"]["summary"]["content"] == "Generating…"
        _run(adapter.send_stream_frame(SEP + "🔍 Searching▌", chat_id=CHAT, turn_id="t1"))
        assert "1 tool calls" in fake.named("batch")[0].request_body.actions
        assert _run(adapter.send_stream_frame("Table 1: results\n| a |\n|---|\n| 1 |", finalize=True, chat_id=CHAT, turn_id="t1")) is True
        done = fake.card_of(fake.named("update")[0])
        assert done["header"]["subtitle"]["content"] == "Done"
        assert _elements(done)[cl.FOOTER_ELEMENT_ID]["content"].startswith("✅ Done · ")
        assert "Table 1　results" in _answer_text(done)
        f = tmp_path / "hermes_fig_ash_trend_1725600000.png"; f.write_bytes(b"png")
        _run(adapter.send_multiple_images(CHAT, [(f"file://{f}", "")]))
        assert "Figure 1　ash trend" in _answer_text(fake.card_of(fake.named("update")[-1]))

    def test_env_locale_override(self, monkeypatch):
        monkeypatch.setenv("FEISHU_CARD_LOCALE", "en")
        assert FeishuAdapter(PlatformConfig())._card_locale == "en"
