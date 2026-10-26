"""Approval boundaries keep the transport streaming, so the continuation lands in a card."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig

from feishu_cardkit import approval_reopen
from feishu_cardkit import streaming as fs
from feishu_cardkit.adapter import CardkitFeishuAdapter as FeishuAdapter
from test_streaming_card import _adapter, _elements, _ok, _run, CHAT


class _Consumer:
    """Minimal stand-in for ``GatewayStreamConsumer`` (only the wrapped call matters)."""

    def __init__(self, adapter):
        self.adapter = adapter
        self.seen = []

    def close_for_approval_prompt(self, placeholder=None, reason="Approval", reopen=False):
        self.seen.append(reopen)
        return reopen


def test_wrapper_opts_only_the_card_adapter_into_reopen():
    fake_consumer = type("FakeConsumer", (_Consumer,), {})
    assert approval_reopen.install_approval_reopen(consumer_cls=fake_consumer) is True
    card_adapter = SimpleNamespace(cardkit_approval_reopen=True)
    plain_adapter = SimpleNamespace()
    assert fake_consumer(card_adapter).close_for_approval_prompt("x") is True
    assert fake_consumer(plain_adapter).close_for_approval_prompt("x") is False
    assert fake_consumer(card_adapter).close_for_approval_prompt("x", reopen=True) is True
    # Idempotent: a second install neither stacks a wrapper nor changes behaviour.
    assert approval_reopen.install_approval_reopen(consumer_cls=fake_consumer) is True
    assert fake_consumer(card_adapter).close_for_approval_prompt("x") is True


def test_real_consumer_reopens_a_card_after_the_approval_boundary():
    adapter, fake = _adapter()
    fake.scripts["create"] = [_ok(card_id="card_1"), _ok(card_id="card_2")]
    assert approval_reopen.install_approval_reopen() is True

    async def _scenario() -> None:
        with patch.object(FeishuAdapter, "_cardkit_available", staticmethod(lambda: True)):
            consumer = GatewayStreamConsumer(
                adapter, CHAT, StreamConsumerConfig(chat_type="dm", cursor="▌", edit_interval=0.01))
            task = asyncio.create_task(consumer.run())
            await asyncio.sleep(0.05)
            consumer.on_tool_progress("🔍 开始分析")
            await asyncio.sleep(0.1)
            boundary = consumer.close_for_approval_prompt("本段已结束", reason="Approval")
            if isinstance(boundary, tuple):  # (future, cancelled_flag) on this Hermes
                boundary = boundary[0]
            await asyncio.wait_for(boundary, timeout=5)
            consumer.on_tool_progress("💻 审批后继续")
            await asyncio.sleep(0.1)
            consumer.on_delta("第二段正文")
            consumer.finish("第二段正文")
            await asyncio.wait_for(task, timeout=5)

    _run(_scenario())
    creates = fake.named("create")
    assert len(creates) == 2, "the continuation opens a fresh card instead of plain messages"
    second = fake.card_of(creates[1])
    assert second["config"]["streaming_mode"] is True
    first_sealed = fake.card_of(fake.named("update")[0])
    assert first_sealed["header"]["title"]["content"] == "本段已结束"
    final = fake.card_of(fake.named("update")[-1])
    assert _elements(final)[fs.ANSWER_ELEMENT_ID]["content"] == "第二段正文"
    assert "审批后继续" in _elements(final)[fs.TIMELINE_ELEMENT_ID]["content"]
    assert str(getattr(adapter, "cardkit_approval_reopen", False)) == "True"
