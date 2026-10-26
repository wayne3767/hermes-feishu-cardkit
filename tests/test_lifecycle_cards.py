"""Official event observations must survive the actual native-card lifecycle."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest

from feishu_cardkit import card_layout as cl, lifecycle, streaming as fs
from feishu_cardkit.interaction import SourceBinding
from gateway.config import Platform
from gateway.session import SessionSource
RoutingIdentity = pytest.importorskip("gateway.session_identity", reason="Hermes lacks pinned RoutingIdentity").RoutingIdentity
from test_streaming_card import _adapter as _base_adapter, _run, _elements, CHAT


def _adapter():
    """These integration scenarios receive a message routed to one known runtime.

    Supply ingress's real RoutingIdentity instead of relying on the unsafe former
    assumption that a lone process-wide observation must belong to this card.
    """
    adapter, fake = _base_adapter()
    def bind(turn, reply_to):
        if not reply_to:
            turn.control_source = None
            return None
        source = SessionSource(platform=Platform.FEISHU, chat_id=turn.chat_id, message_id=reply_to,
                               profile="profile-a")
        source._identity = RoutingIdentity(
            transport_profile="receiving-bot", runtime_profile="profile-a",
            authorization_home=Path("/test/receiving-bot"), runtime_home=Path("/test/profile-a"))
        turn.control_source = SourceBinding(source, "owner", reply_to, "exact-session-key")
        return turn.control_source
    adapter._bind_card_source = bind
    return adapter, fake


def admitted(store, *, scope="/test/profile-a", message="in-1", sid="s-1", tid="agent-turn-1"):
    context = {"platform": "feishu", "session_key": "exact-session-key", "message_id": message,
               "chat_id": CHAT, "session_id": sid}
    store.observe("post_gateway_admission", profile_scope=scope, platform="feishu",
                  session_key=context["session_key"], message_id=message, source={"chat_id": CHAT})
    store.observe("pre_llm_call", profile_scope=scope, context=context,
                  platform="feishu", session_id=sid, turn_id=tid)
    return context


def test_seed_before_agent_events_waits_without_false_stop_then_shows_actual_usage(monkeypatch):
    store = lifecycle.ObservationStore()
    monkeypatch.setattr(lifecycle, "OBSERVATIONS", store)
    monkeypatch.setattr(fs, "_WATCHDOG_POLL_SECONDS", .01)
    monkeypatch.setattr(fs, "IDLE_SEAL_SECONDS", .04)
    adapter, fake = _adapter()
    adapter._footer_usage = True

    async def scenario():
        await adapter.send_stream_frame("初始正文", chat_id=CHAT, reply_to="in-1", turn_id="consumer-unrelated-id")
        turn = adapter._stream_cards[f"{CHAT}:consumer-unrelated-id"]
        assert turn.observation_ref is None
        context = admitted(store)
        event = dict(profile_scope="/test/profile-a", context=context, platform="feishu",
                     session_id="s-1", turn_id="agent-turn-1")
        store.observe("post_api_request", **event, api_request_id="req-1", response_model="actual-model",
                      model="configured-model", provider="provider-a", started_at=1,
                      usage={"prompt_tokens": 120, "output_tokens": 15}, context_length=1000)
        store.observe("on_human_input_request", **event, request_id="ask-1", kind="clarify")
        await asyncio.sleep(.09)
        assert turn.phase == "waiting_input"
        assert turn in adapter._stream_cards.values(), "an explicit wait outlives the text idle threshold"
        waiting = fake.card_of(fake.named("update")[-1])
        assert waiting["header"]["title"]["content"] == "等待输入"
        store.observe("on_human_input_resolved", **event, request_id="ask-1", outcome="answered")
        for _ in range(2):  # duplicate transport observation is counted once
            store.observe("post_api_request", **event, api_request_id="req-2", response_model="fallback-model",
                          provider="provider-b", started_at=2,
                          usage={"prompt_tokens": 200, "output_tokens": 25}, context_length=2000)
        store.observe("on_session_end", **event, completed=True, failed=False, interrupted=False)
        assert await adapter.send_stream_frame("最终完整正文", finalize=True, chat_id=CHAT,
                                              turn_id="consumer-unrelated-id")
        return fake.card_of(fake.named("update")[-1])

    final = _run(scenario())
    footer = _elements(final)[cl.FOOTER_ELEMENT_ID]["content"]
    assert final["header"]["title"]["content"] == "已完成"
    assert "fallback-model" in footer and "configured-model" not in footer
    assert footer.startswith("✅ 已完成 · ")
    assert "↑320" in footer and "↓40" in footer and "200/2,000 (10%)" in footer, "opt-in usage stays available" 


def test_same_chat_without_exact_inbound_identity_never_borrows_metrics(monkeypatch):
    store = lifecycle.ObservationStore()
    monkeypatch.setattr(lifecycle, "OBSERVATIONS", store)
    context = admitted(store)
    store.observe("post_api_request", profile_scope="/test/profile-a", context=context, session_id="s-1",
                  turn_id="agent-turn-1", api_request_id="req", response_model="other-user-model",
                  usage={"prompt_tokens": 999, "output_tokens": 99})
    adapter, fake = _adapter()
    async def scenario():
        await adapter.send_stream_frame("", chat_id=CHAT, reply_to="other-inbound", turn_id="t")
        await adapter.send_stream_frame("answer", finalize=True, chat_id=CHAT, turn_id="t")
    _run(scenario())
    footer = _elements(fake.card_of(fake.named("update")[-1]))[cl.FOOTER_ELEMENT_ID]["content"]
    assert "other-user-model" not in footer and "↑" not in footer


def test_interrupt_does_not_mark_other_sessions_in_the_same_chat(monkeypatch):
    adapter, _ = _adapter()
    async def scenario():
        await adapter.send_stream_frame("A", chat_id=CHAT, turn_id="a")
        await adapter.send_stream_frame("B", chat_id=CHAT, turn_id="b")
        a, b = adapter._stream_cards[f"{CHAT}:a"], adapter._stream_cards[f"{CHAT}:b"]
        a.control_source = SimpleNamespace(session_key="session-a")
        b.control_source = SimpleNamespace(session_key="session-b")
        await adapter.interrupt_session_activity("session-a", CHAT)
        assert a.stop_requested_at is not None and b.stop_requested_at is None
    _run(scenario())


def test_status_labels_are_distinct_and_terminal_buttons_retire():
    for phase, label in {"waiting_approval": "等待确认", "waiting_input": "等待输入",
                         "failed": "执行失败", "blocked": "已拒绝", "timeout": "已超时",
                         "unknown": "暂未收到进展"}.items():
        card = cl.build_stream_card(title="T", markdown="body", timeline_md="tool", tool_count=1,
                                    footer=cl.render_footer(phase=phase, tool_count=1, elapsed=2),
                                    phase=phase, stop_button=True)
        assert card["header"]["title"]["content"] == label
        if phase in {"failed", "blocked", "timeout"}:
            assert cl.STOP_BUTTON_ID not in _elements(card)


def test_metrics_only_frame_refreshes_footer_without_closing_typewriter(monkeypatch):
    store = lifecycle.ObservationStore()
    monkeypatch.setattr(lifecycle, "OBSERVATIONS", store)
    context = admitted(store)
    adapter, fake = _adapter()
    adapter._footer_usage = True
    adapter._stop_button = True
    adapter._can_stop_card = lambda turn: bool(turn.observation_ref) and turn.observation_terminal is False
    async def scenario():
        await adapter.send_stream_frame("begin", chat_id=CHAT, reply_to="in-1", turn_id="consumer")
        seed = fake.card_of(fake.named("create")[-1])
        assert cl.STOP_BUTTON_ID in _elements(seed)
        assert seed["config"]["streaming_mode"] is True
        turn = adapter._stream_cards[f"{CHAT}:consumer"]
        assert not turn.dirty and not turn.controls_dirty, "the seed already includes its known controls"
        store.observe("post_api_request", profile_scope="/test/profile-a", context=context, session_id="s-1",
                      turn_id="agent-turn-1", api_request_id="req", response_model="actual",
                      usage={"prompt_tokens": 77, "output_tokens": 8})
        await adapter.send_stream_frame("begin more", chat_id=CHAT, turn_id="consumer")
        assert not adapter._stream_cards[f"{CHAT}:consumer"].streaming_closed
    _run(scenario())
    import json
    actions = json.loads(fake.named("batch")[-1].request_body.actions)
    assert "↑77" in actions[-1]["params"]["partial_element"]["content"]


@pytest.mark.parametrize("driver", ["frame", "watchdog"])
@pytest.mark.parametrize("fail_first", [False, True])
def test_late_observation_adds_stop_and_retries_without_closing_typewriter(monkeypatch, driver, fail_first):
    from test_streaming_card import _fail
    store = lifecycle.ObservationStore()
    monkeypatch.setattr(lifecycle, "OBSERVATIONS", store)
    monkeypatch.setattr(fs, "_WATCHDOG_POLL_SECONDS", 60)
    adapter, fake = _adapter()
    adapter._stop_button = True
    # This test owns only the layout pipeline. Exact task/guard ownership is tested
    # by the Stop integration suite; model the moment its official proof arrives.
    adapter._can_stop_card = lambda turn: bool(turn.observation_ref) and turn.observation_terminal is False

    async def scenario():
        key = f"{CHAT}:consumer"
        await adapter.send_stream_frame("begin", chat_id=CHAT, reply_to="in-1", turn_id="consumer")
        turn = adapter._stream_cards[key]
        turn.watchdog.cancel()
        await asyncio.gather(turn.watchdog, return_exceptions=True)
        assert cl.STOP_BUTTON_ID not in _elements(fake.card_of(fake.named("create")[-1]))
        admitted(store)  # same running phase and no metrics: only the control proof changed
        if fail_first:
            fake.scripts["update"] = [_fail(999)]

        async def refresh(via):
            if via == "frame":
                await adapter.send_stream_frame("begin", chat_id=CHAT, turn_id="consumer")
            else:
                watcher = asyncio.create_task(adapter._seal_when_idle(key, turn))
                await asyncio.sleep(0)  # one real watchdog pass, then its long poll sleep
                watcher.cancel()
                await asyncio.gather(watcher, return_exceptions=True)

        await refresh(driver)
        if fail_first:
            assert turn.controls_dirty and not turn.dirty and not turn.streaming_closed
            # Either path can repair the other's failed full layout write.
            await refresh("watchdog" if driver == "frame" else "frame")
        assert not turn.controls_dirty and not turn.dirty and not turn.streaming_closed
        assert len(fake.named("update")) == (2 if fail_first else 1)
        for request in fake.named("update"):
            card = fake.card_of(request)
            assert cl.STOP_BUTTON_ID in _elements(card)
            assert card["config"]["streaming_mode"] is True
        before = len(fake.named("update"))
        await adapter.send_stream_frame("begin continued", chat_id=CHAT, turn_id="consumer")
        assert fake.pushes(cl.ANSWER_ELEMENT_ID)[-1] == "begin continued"
        assert len(fake.named("update")) == before
        assert not turn.streaming_closed and not fake.named("settings")

    _run(scenario())


def test_real_gateway_boundary_is_a_segment_and_continues_observing_timeout(monkeypatch):
    from gateway.stream_consumer import GatewayStreamConsumer
    store = lifecycle.ObservationStore()
    monkeypatch.setattr(lifecycle, "OBSERVATIONS", store)
    monkeypatch.setattr(fs, "_WATCHDOG_POLL_SECONDS", .01)
    context = admitted(store)
    adapter, fake = _adapter()
    async def scenario():
        consumer = GatewayStreamConsumer(adapter, CHAT, initial_reply_to_id="in-1")
        await consumer._send_frame("我需要确认", finalize=False)
        consumer._accumulated = "我需要确认"
        assert await consumer._finalize_boundary_stream("Clarify")
        segment = fake.card_of(fake.named("update")[-1])
        assert segment["header"]["title"]["content"] == "本段已结束"
        assert not adapter._stream_cards and adapter._observed_segments
        event = dict(profile_scope="/test/profile-a", context=context, session_id="s-1", turn_id="agent-turn-1")
        store.observe("on_human_input_request", **event, request_id="ask", kind="clarify")
        await asyncio.sleep(.03)
        assert fake.card_of(fake.named("update")[-1])["header"]["title"]["content"] == "等待输入"
        store.observe("on_human_input_resolved", **event, request_id="ask", outcome="timeout")
        await asyncio.sleep(.03)
        card = fake.card_of(fake.named("update")[-1])
        assert card["header"]["title"]["content"] == "已超时"
        assert cl.STOP_BUTTON_ID not in _elements(card)
        store.observe("on_session_end", **event, completed=True, failed=False, interrupted=False)
        await asyncio.sleep(.03)
        assert not adapter._observed_segments
        assert fake.card_of(fake.named("update")[-1])["header"]["title"]["content"] == "本段已结束"
    _run(scenario())


def test_lost_wait_observation_becomes_unknown_instead_of_waiting_forever(monkeypatch):
    store = lifecycle.ObservationStore(ttl=.04)
    monkeypatch.setattr(lifecycle, "OBSERVATIONS", store)
    monkeypatch.setattr(fs, "_WATCHDOG_POLL_SECONDS", .01)
    monkeypatch.setattr(fs, "IDLE_SEAL_SECONDS", .06)
    context = admitted(store)
    store.observe("on_human_input_request", profile_scope="/test/profile-a", context=context,
                  session_id="s-1", turn_id="agent-turn-1", request_id="ask", kind="clarify")
    adapter, fake = _adapter()
    async def scenario():
        await adapter.send_stream_frame("waiting", chat_id=CHAT, reply_to="in-1", turn_id="consumer")
        await asyncio.sleep(.15)
        assert not adapter._stream_cards
    _run(scenario())
    assert fake.card_of(fake.named("update")[-1])["header"]["title"]["content"] == "暂未收到进展"


def test_image_attachment_keeps_failed_state(monkeypatch):
    from test_streaming_card import _ok
    adapter, fake = _adapter()
    adapter._card_math_images = False
    fake.im.v1.image = SimpleNamespace(create=fake._m("image"))
    fake.scripts["image"] = [_ok(image_key="img_test")]
    async def scenario():
        await adapter.send_stream_frame("partial", chat_id=CHAT, turn_id="t")
        turn = adapter._stream_cards[f"{CHAT}:t"]
        turn.phase = "failed"
        await adapter.send_stream_frame("partial result", finalize=True, chat_id=CHAT, turn_id="t")
        done = adapter._completed_cards[turn.card_id]
        async def upload(_):
            return "img_test"
        adapter._upload_image = upload
        assert await adapter._embed_images(done, [("/tmp/example.png", "figure")], None)
    _run(scenario())
    assert fake.card_of(fake.named("update")[-1])["header"]["title"]["content"] == "执行失败"


def test_closed_segment_terminal_update_retries_before_retiring_observer(monkeypatch):
    from test_streaming_card import _fail
    store = lifecycle.ObservationStore()
    monkeypatch.setattr(lifecycle, "OBSERVATIONS", store)
    monkeypatch.setattr(fs, "_WATCHDOG_POLL_SECONDS", .01)
    context = admitted(store)
    adapter, fake = _adapter()
    async def scenario():
        await adapter.send_stream_frame("before prompt", chat_id=CHAT, reply_to="in-1", turn_id="c")
        await adapter.send_stream_frame("before prompt", finalize=True, chat_id=CHAT, turn_id="c")
        before = len(fake.named("update"))
        fake.scripts["update"] = [_fail(999)]
        store.observe("on_session_end", profile_scope="/test/profile-a", context=context, session_id="s-1",
                      turn_id="agent-turn-1", completed=False, failed=True, interrupted=False)
        await asyncio.sleep(.05)
        assert len(fake.named("update")) >= before + 2
        assert not adapter._observed_segments
    _run(scenario())
    assert fake.card_of(fake.named("update")[-1])["header"]["title"]["content"] == "执行失败"


def test_shutdown_retires_observed_waiting_segment_without_claiming_completion(monkeypatch):
    store = lifecycle.ObservationStore()
    monkeypatch.setattr(lifecycle, "OBSERVATIONS", store)
    context = admitted(store)
    store.observe("on_human_input_request", profile_scope="/test/profile-a", context=context,
                  session_id="s-1", turn_id="agent-turn-1", request_id="ask", kind="clarify")
    adapter, fake = _adapter()
    async def scenario():
        await adapter.send_stream_frame("question", chat_id=CHAT, reply_to="in-1", turn_id="c")
        await adapter.send_stream_frame("question", finalize=True, chat_id=CHAT, turn_id="c")
        await adapter.close_open_stream_cards()
        assert not adapter._observed_segments
    _run(scenario())
    assert fake.card_of(fake.named("update")[-1])["header"]["title"]["content"] == "暂未收到进展"


def test_optional_binding_failure_cannot_block_original_inbound_handler(monkeypatch):
    from plugins.platforms.feishu.adapter import FeishuAdapter as Bundled
    adapter, _ = _adapter()
    seen = []
    async def original(self, event):
        seen.append(event)
    monkeypatch.setattr(Bundled, "_handle_message_with_guards", original)
    adapter._source_session_key = None  # old Hermes lacks the exact session helper
    event = SimpleNamespace()
    _run(adapter._handle_message_with_guards(event))
    assert seen == [event]


def test_long_boundary_observation_never_restores_stale_partial_text(monkeypatch):
    store = lifecycle.ObservationStore()
    monkeypatch.setattr(lifecycle, "OBSERVATIONS", store)
    monkeypatch.setattr(fs, "_WATCHDOG_POLL_SECONDS", .01)
    context = admitted(store)
    adapter, fake = _adapter()
    async def scenario():
        await adapter.send_stream_frame("旧正文", chat_id=CHAT, reply_to="in-1", turn_id="c")
        assert not await adapter.send_stream_frame("最新正文\n\n" + "完整内容。" * 6000,
                                                    finalize=True, chat_id=CHAT, turn_id="c")
        store.observe("on_human_input_request", profile_scope="/test/profile-a", context=context,
                      session_id="s-1", turn_id="agent-turn-1", request_id="ask", kind="clarify")
        await asyncio.sleep(.04)
    _run(scenario())
    from test_streaming_card import _answer_text
    assert _answer_text(fake.card_of(fake.named("update")[-1])).startswith("最新正文")


def test_boundary_footer_fallback_still_says_segment_ended(monkeypatch):
    from test_streaming_card import _fail
    store = lifecycle.ObservationStore()
    monkeypatch.setattr(lifecycle, "OBSERVATIONS", store)
    monkeypatch.setattr(fs, "FINAL_UPDATE_RETRY_DELAYS", ())
    admitted(store)
    adapter, fake = _adapter()
    async def scenario():
        await adapter.send_stream_frame("preamble", chat_id=CHAT, reply_to="in-1", turn_id="c")
        fake.scripts["update"] = [_fail(999), _fail(999)]
        await adapter.send_stream_frame("preamble", finalize=True, chat_id=CHAT, turn_id="c")
    _run(scenario())
    import json
    actions = json.loads(fake.named("batch")[-1].request_body.actions)
    assert "本段已结束" in actions[0]["params"]["partial_element"]["content"]
