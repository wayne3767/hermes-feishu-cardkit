"""Regressions found by independently reviewing the official clarify lifecycle."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from test_interactions import clarify_setup  # pytest fixture, with a pinned routing identity
from test_streaming_card import CHAT, _ok, _run
from gateway.platforms.base import SendResult


@pytest.mark.parametrize("outcome,notice,expected", [
    ("timeout", "Timed out", "Timed out"),
    ("typed_answer", "Question answered", "已选择：Continue"),
    ("cancelled", "Question cancelled", "Question cancelled"),
])
def test_settlement_before_send_ack_refreshes_only_the_delivered_card(
    clarify_setup, outcome, notice, expected,
):
    """A late IM acknowledgement must carry forward retirement and actual replies."""
    from tools import clarify_gateway as clarify

    adapter, fake, turn, entry = clarify_setup
    key = turn.control_source.session_key

    async def scenario():
        started, release_ack = asyncio.Event(), asyncio.Event()
        sends = []

        async def delayed_send(**kwargs):
            sends.append(kwargs)
            started.set()
            await release_ack.wait()
            return _ok(message_id="om_late_clarify_ack")

        adapter._feishu_send_with_retry = delayed_send
        send_task = asyncio.create_task(adapter.send_clarify(
            CHAT, "Which action?", ["Continue", "Stop"], "c1", key,
            {"thread_id": turn.thread_id},
        ))
        await started.wait()
        try:
            assert adapter._clarify_cards["c1"].message_id == ""
            if outcome == "timeout":
                assert clarify.wait_for_response("c1", timeout=0.001) is None
                assert entry.response is None  # the resolver owns timeout signaling
            elif outcome == "typed_answer":
                assert clarify.resolve_text_response_for_session(key, "1")
                assert entry.response == "Continue"
            else:
                assert clarify.clear_session(key) == 1
                assert entry.response == clarify.CANCELLED

            await adapter.retire_clarify_card("c1", notice)
            assert fake.named("im_update") == [], "there is no message id to update before ACK"
        finally:
            release_ack.set()
            result = await send_task

        assert result.success and result.message_id == "om_late_clarify_ack"
        assert len(sends) == 1, "retirement must update the existing card, never send another prompt"
        updates = fake.named("im_update")
        assert len(updates) == 1 and updates[0].message_id == "om_late_clarify_ack"
        card = json.loads(updates[0].request_body.content)
        elements = card["body"]["elements"]
        assert [element["content"] for element in elements] == [
            "Which action?", "1. Continue\n2. Stop", expected,
        ]
        assert all(element["tag"] != "button" for element in elements)
        if outcome == "timeout":
            assert entry.response is None, "a display refresh must not fabricate an answer"
        elif outcome == "typed_answer":
            assert entry.response == "Continue"
        else:
            assert entry.response == clarify.CANCELLED

    _run(scenario())


def test_webhook_clarify_keeps_official_text_capture_without_native_card(clarify_setup):
    """The current upstream webhook handler discards inline card response payloads."""
    adapter, _, turn, entry = clarify_setup
    adapter._connection_mode = "webhook"
    adapter._feishu_send_with_retry = AsyncMock(side_effect=AssertionError("native card must not be sent"))
    adapter.send = AsyncMock(return_value=SendResult(True, message_id="om_text_clarify"))
    result = _run(adapter.send_clarify(
        CHAT, "Which action?", ["Continue", "Stop"], "c1", turn.control_source.session_key,
        {"thread_id": turn.thread_id},
    ))
    assert result.success and result.message_id == "om_text_clarify"
    adapter.send.assert_awaited_once()
    text = adapter.send.call_args.kwargs["content"]
    assert "Which action?" in text and "Continue" in text and "Stop" in text
    assert entry.awaiting_text and not entry.event.is_set()
    assert not adapter._clarify_cards
    adapter._feishu_send_with_retry.assert_not_called()
