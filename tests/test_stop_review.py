"""Real Feishu locking and SDK callback shapes, including a click delayed behind ingress."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch
import weakref
import pytest

from plugins.platforms.feishu.adapter import FeishuAdapter as Bundled
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger
from test_interactions import _adapter, _bound_turn, _click, _run, _source, StreamCardTurn, HAS_TRUSTED_IDENTITY

pytestmark = pytest.mark.skipif(not HAS_TRUSTED_IDENTITY, reason="Hermes lacks exact source/session identity seams")


def _sdk_click():
    return P2CardActionTrigger({"event": {
        "action": {"value": {"hermes_cardkit": "stop"}}, "token": "callback_token",
        "context": {"open_message_id": "om_card", "open_chat_id": "oc_chat"},
        "operator": {"open_id": "ou_owner"}}})


def test_finalized_while_waiting_for_real_chat_lock_never_dispatches_stop():
    async def exercise():
        adapter, _ = _adapter()
        turn = _bound_turn(adapter)
        lock = adapter._get_chat_lock(turn.chat_id)
        await lock.acquire()
        with patch.object(Bundled, "handle_message", new_callable=AsyncMock) as upstream:
            pending = asyncio.create_task(adapter._dispatch_bound_stop(_sdk_click(), turn))
            await asyncio.sleep(0)
            assert not pending.done()
            # The first check passed, but the original card finished during lock contention.
            adapter._stream_cards.clear()
            lock.release()
            await pending
            upstream.assert_not_awaited()
    _run(exercise())


def test_original_card_still_active_reaches_original_handler_once_under_lock():
    async def exercise():
        adapter, _ = _adapter()
        turn = _bound_turn(adapter)
        seen = []
        async def original(event):
            assert adapter._get_chat_lock(turn.chat_id).locked()
            seen.append(event)
        async def original_method(self, event):
            await original(event)
        with patch.object(Bundled, "handle_message", new=original_method):
            await adapter._dispatch_bound_stop(_sdk_click(), turn)
        assert len(seen) == 1 and seen[0].text == "/stop"
        assert seen[0].source.thread_id == turn.control_source.source.thread_id
        assert seen[0].metadata["gateway_session_key"] == turn.control_source.session_key
    _run(exercise())


def test_source_copy_keeps_real_pinned_profile_and_transport_identity():
    from gateway.session_identity import RoutingIdentity, identity_of
    adapter, _ = _adapter()
    source = _source(adapter)
    source.profile = "xm"
    source._identity = RoutingIdentity(
        transport_profile="default", runtime_profile="xm",
        authorization_home=Path("/tmp/cardkit-review-default"),
        runtime_home=Path("/tmp/cardkit-review-xm"), transport=weakref.ref(adapter))
    turn = StreamCardTurn(card_id="card", message_id="om_card", chat_id=source.chat_id)
    binding = adapter._bind_card_source(turn, source.message_id)
    assert binding.session_key.startswith("agent:xm:")
    assert identity_of(binding.source).adapter() is adapter
    assert identity_of(binding.source).transport_profile == "default"


def test_restart_adapter_has_no_authority_for_old_card():
    previous, _ = _adapter()
    _bound_turn(previous)
    restarted, _ = _adapter()
    assert restarted._stop_turn_from_event(_sdk_click().event) is None


def test_action_value_cannot_override_sdk_card_identity():
    adapter, _ = _adapter()
    turn = _bound_turn(adapter)
    click = _sdk_click()
    click.event.action.value.update({"session_key": "agent:tz:any", "message_id": "om_other",
                                    "open_id": "ou_other", "thread_id": "omt_other"})
    assert adapter._stop_turn_from_event(click.event) is turn
    click.event.context.open_message_id = "om_other"
    assert adapter._stop_turn_from_event(click.event) is None


def test_topic_parent_raw_id_cannot_override_current_reply_owner_or_profile():
    adapter, _ = _adapter()
    parent = _source(adapter, message_id="om_parent", open_id="ou_alice")
    parent.profile = "xm"
    child = _source(adapter, message_id="om_child", open_id="ou_bob")
    child.profile = "tz"
    adapter._card_sources["om_child"] = replace(adapter._card_sources["om_child"], reply_anchor="om_parent")
    turn = StreamCardTurn(card_id="", message_id="", chat_id=child.chat_id)
    assert adapter._bind_card_source(turn, "om_parent") is None
    assert not adapter._can_stop_card(turn)


def test_sole_topic_alias_retains_exact_owner_and_profile():
    adapter, _ = _adapter()
    child = _source(adapter, message_id="om_child", open_id="ou_bob")
    child.profile = "tz"
    adapter._card_sources["om_child"] = replace(adapter._card_sources["om_child"], reply_anchor="om_parent")
    turn = StreamCardTurn(card_id="", message_id="", chat_id=child.chat_id)
    binding = adapter._bind_card_source(turn, "om_parent")
    assert binding.owner_open_id == "ou_bob"
    assert binding.source.profile == "tz"
    assert binding.message_id == "om_child"


def test_clarify_binding_rejects_only_available_observation_from_another_profile(monkeypatch):
    from feishu_cardkit import lifecycle
    from gateway.session_identity import RoutingIdentity
    adapter, _ = _adapter()
    source = _source(adapter)
    source._identity = RoutingIdentity(
        transport_profile="default", runtime_profile="default", multiplexed=False,
        authorization_home=Path("/tmp/cardkit-a"), runtime_home=Path("/tmp/cardkit-a"))
    key = adapter._source_session_key(source)
    monkeypatch.setattr(lifecycle, "current_scope", lambda: "/tmp/cardkit-b")
    query = AsyncMock()  # should not query any observation for an unrelated source scope
    monkeypatch.setattr(lifecycle.OBSERVATIONS, "active_for_session", query)
    assert adapter._interaction_binding(key, source.chat_id, {"thread_id": source.thread_id}) is None
    query.assert_not_called()


def test_clarify_binding_requires_pinned_identity_even_with_matching_session(monkeypatch):
    from feishu_cardkit import lifecycle
    adapter, _ = _adapter()
    source = _source(adapter)
    key = adapter._source_session_key(source)
    monkeypatch.setattr(lifecycle, "current_scope", lambda: "/tmp/cardkit-a")
    query = AsyncMock()
    monkeypatch.setattr(lifecycle.OBSERVATIONS, "active_for_session", query)
    assert adapter._interaction_binding(key, source.chat_id, {"thread_id": source.thread_id}) is None
    query.assert_not_called()


def test_retained_old_stream_cannot_stop_a_new_processing_guard():
    adapter, _ = _adapter()
    turn = _bound_turn(adapter)
    adapter._active_sessions[turn.control_source.session_key] = asyncio.Event()
    assert not adapter._can_stop_card(turn)
    assert adapter._stop_turn_from_event(_sdk_click().event) is None


def test_no_exact_agent_observation_hides_and_rejects_stop():
    adapter, _ = _adapter()
    turn = _bound_turn(adapter)
    turn.observation_ref = None
    assert not adapter._can_stop_card(turn)
    assert adapter._stop_turn_from_event(_sdk_click().event) is None


def test_same_task_and_guard_cannot_stop_new_agent_turn_with_old_card():
    from feishu_cardkit.lifecycle import OBSERVATIONS
    adapter, _ = _adapter()
    turn = _bound_turn(adapter)
    ref, key = turn.observation_ref, turn.control_source.session_key
    OBSERVATIONS.observe("on_session_end", profile_scope=ref.profile_scope,
                         session_id=ref.session_id, turn_id=ref.turn_id)
    OBSERVATIONS.observe("pre_llm_call", profile_scope=ref.profile_scope,
                         session_id=ref.session_id, turn_id="next-agent-turn",
                         context={"platform": "feishu", "session_key": key,
                                  "message_id": turn.control_source.message_id, "chat_id": turn.chat_id})
    assert adapter._active_sessions[key] is turn.control_guard
    assert adapter._session_tasks[key] is turn.control_task
    assert adapter._stop_turn_from_event(_sdk_click().event) is None


@pytest.mark.parametrize("field,value", [("segment_finalized", True), ("sealed_at", 123.0),
                                         ("observation_terminal", True), ("phase", "done")])
def test_closed_or_terminal_turn_cannot_accept_stale_stop(field, value):
    adapter, _ = _adapter()
    turn = _bound_turn(adapter)
    setattr(turn, field, value)
    assert adapter._stop_turn_from_event(_sdk_click().event) is None


def test_unpolled_official_terminal_observation_already_blocks_stop(monkeypatch):
    from feishu_cardkit import lifecycle
    from types import SimpleNamespace
    adapter, _ = _adapter()
    turn = _bound_turn(adapter)
    turn.observation_ref = lifecycle.ObservationRef("scope", "session", "turn")
    turn.observation_terminal = False
    monkeypatch.setattr(lifecycle.OBSERVATIONS, "snapshot", lambda ref: SimpleNamespace(terminal=True))
    assert adapter._stop_turn_from_event(_sdk_click().event) is None


def test_original_base_dispatch_claims_command_guard_before_first_await():
    async def exercise():
        adapter, _ = _adapter()
        turn = _bound_turn(adapter)
        key, guard = turn.control_source.session_key, turn.control_guard
        agent_task = asyncio.create_task(asyncio.Event().wait())
        adapter._session_tasks[key] = agent_task
        adapter._bind_card_source(turn, turn.control_source.message_id)
        seen = []
        async def handler(event):
            # This is the first external await of the real base /stop dispatch. Its
            # command guard must already own the lane so no new turn can take it.
            assert adapter._active_sessions[key] is not guard
            assert adapter._get_chat_lock(turn.chat_id).locked()
            seen.append(event.text)
            return None
        adapter._message_handler = handler
        adapter.cancel_session_processing = AsyncMock()
        try:
            await adapter._dispatch_bound_stop(_sdk_click(), turn)
            assert seen == ["/stop"]
            adapter.cancel_session_processing.assert_awaited_once_with(
                key, release_guard=False, discard_pending=False)
        finally:
            agent_task.cancel()
            await asyncio.gather(agent_task, return_exceptions=True)
    _run(exercise())


def test_real_drain_handoff_reusing_guard_rejects_old_stream():
    async def exercise():
        adapter, _ = _adapter()
        turn = _bound_turn(adapter)
        key, guard = turn.control_source.session_key, turn.control_guard
        old_task = asyncio.create_task(asyncio.Event().wait())
        adapter._session_tasks[key] = old_task
        adapter._bind_card_source(turn, turn.control_source.message_id)
        # Real upstream helper clears the SAME Event and starts a fresh owner Task.
        adapter._spawn_drain_task(None, key, delay=60)
        new_task = adapter._session_tasks[key]
        try:
            assert adapter._active_sessions[key] is guard
            assert new_task is not old_task
            assert adapter._stop_turn_from_event(_sdk_click().event) is None
        finally:
            old_task.cancel()
            new_task.cancel()
            await asyncio.gather(old_task, new_task, return_exceptions=True)
    _run(exercise())


def test_default_transport_clarify_worker_keeps_routed_profile_on_gateway_loop(monkeypatch):
    """Exercise the real TurnRunner scheduler, not a mocked ambient scope."""
    from types import ModuleType, SimpleNamespace
    from agent.async_utils import safe_schedule_threadsafe
    from gateway.run_turn_runner import TurnRunner
    from gateway.session_identity import RoutingIdentity
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from feishu_cardkit import lifecycle
    from tools import clarify_gateway as clarify

    async def exercise():
        adapter, fake = _adapter()
        source = _source(adapter)
        source.profile = "xm"
        transport_scope = str(Path("/tmp/cardkit-review-transport").resolve())
        routed_scope = str(Path("/tmp/cardkit-review-xm").resolve())
        source._identity = RoutingIdentity(
            transport_profile="default", runtime_profile="xm", authorization_home=Path(transport_scope),
            runtime_home=Path(routed_scope), transport=weakref.ref(adapter))
        key = adapter._source_session_key(source)
        store = lifecycle.ObservationStore()
        store.observe("post_gateway_admission", profile_scope=routed_scope, platform="feishu",
                      session_key=key, message_id="om_in", source={"chat_id": source.chat_id})
        store.observe("pre_llm_call", profile_scope=routed_scope, session_id="s", turn_id="t",
                      context={"platform": "feishu", "session_key": key, "message_id": "om_in",
                               "chat_id": source.chat_id})
        monkeypatch.setattr(lifecycle, "OBSERVATIONS", store)
        clarify.register("routed", key, "Question", ["Yes", "No"])
        runner = TurnRunner(SimpleNamespace(), SimpleNamespace(_loop_for_step=asyncio.get_running_loop()))
        def agent_worker():
            token = set_hermes_home_override(routed_scope)
            try:
                return runner._schedule(adapter.send_clarify(
                    source.chat_id, "Question", ["Yes", "No"], "routed", key,
                    {"thread_id": source.thread_id}), "test scheduling").result(timeout=5)
            finally:
                reset_hermes_home_override(token)
        token = set_hermes_home_override(transport_scope)
        try:
            result = await asyncio.to_thread(agent_worker)
            assert result.success
            assert lifecycle.current_scope() == transport_scope
            assert "routed" in adapter._clarify_cards  # real native card, not text downgrade
            assert adapter._clarify_cards["routed"].binding.source.profile == "xm"
        finally:
            reset_hermes_home_override(token)
            clarify.clear_session(key)
    # The production module re-exports this exact scheduler. Importing that entry
    # point also activates Hermes's package manager and can exec another Python,
    # replacing pytest itself. Exercise the real TurnRunner and scheduling helper
    # without invoking unrelated process startup or reading the user's config.
    scheduling_exports = ModuleType("gateway.run")
    scheduling_exports.safe_schedule_threadsafe = safe_schedule_threadsafe
    with patch.dict("sys.modules", {"gateway.run": scheduling_exports}):
        _run(exercise())
