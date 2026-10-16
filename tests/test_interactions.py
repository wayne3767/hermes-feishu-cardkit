"""Card clicks must affect exactly their originating session; navigation needs receipts."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import importlib.util
import uuid
import weakref
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import pytest

from gateway.platforms.base import MessageEvent, MessageType, SendResult
from feishu_cardkit import card_layout as cl
from feishu_cardkit.adapter import CardkitFeishuAdapter
from feishu_cardkit.interaction import clarify_card
from feishu_cardkit.navigation import navigation_from_result
from feishu_cardkit.streaming import StreamCardTurn
from plugins.platforms.feishu.adapter import FeishuAdapter as Bundled
from test_streaming_card import CHAT, _adapter, _run

HAS_TRUSTED_IDENTITY = (callable(getattr(Bundled, "_source_session_key", None))
                        and importlib.util.find_spec("gateway.session_identity") is not None)


def _source(adapter, *, thread="omt_topic", chat_type="group", message_id="om_in", open_id="ou_owner"):
    source = adapter.build_source(chat_id=CHAT, chat_type=chat_type, user_id="tenant_user",
                                  user_id_alt="on_union", thread_id=thread, message_id=message_id)
    raw = NS(event=NS(message=NS(message_id=message_id), sender=NS(sender_id=NS(open_id=open_id))))
    event = MessageEvent(text="question", message_type=MessageType.TEXT, source=source,
                         message_id=message_id, raw_message=raw)
    adapter._remember_card_source(event)
    return source


def _bound_turn(adapter, **kwargs):
    from gateway.session_identity import RoutingIdentity
    from feishu_cardkit.lifecycle import OBSERVATIONS
    adapter._admins = set()  # tests must not inherit the operator's live Feishu allowlist
    adapter._allowed_group_users = set()
    source = _source(adapter, **kwargs)
    scope = str(Path("/tmp/cardkit-stop-test", uuid.uuid4().hex).resolve())
    source._identity = RoutingIdentity(
        transport_profile="default", runtime_profile="default", multiplexed=False,
        authorization_home=Path(scope), runtime_home=Path(scope), transport=weakref.ref(adapter))
    turn = StreamCardTurn(card_id="card_id", message_id="om_card", chat_id=CHAT, thread_id=source.thread_id)
    adapter._active_sessions[adapter._source_session_key(source)] = asyncio.Event()
    adapter._session_tasks[adapter._source_session_key(source)] = NS(done=lambda: False)
    adapter._bind_card_source(turn, source.message_id)
    key = turn.control_source.session_key
    OBSERVATIONS.observe("post_gateway_admission", profile_scope=scope, platform="feishu",
                         session_key=key, message_id=source.message_id, source={"chat_id": source.chat_id})
    OBSERVATIONS.observe("pre_llm_call", profile_scope=scope, session_id="test-session", turn_id="test-turn",
                         context={"platform": "feishu", "session_key": key, "message_id": source.message_id,
                                  "chat_id": source.chat_id})
    turn.observation_ref = OBSERVATIONS.resolve(scope, source.chat_id, source.message_id, session_key=key)
    adapter._init_streaming_cards()
    adapter._stream_cards["turn1"] = turn
    return turn


def _click(value=None, *, chat=CHAT, open_id="ou_owner", message_id="om_card", token="tok1"):
    return NS(event=NS(action=NS(value=value or dict(cl.STOP_ACTION)), token=token,
                       context=NS(open_chat_id=chat, open_message_id=message_id),
                       operator=NS(open_id=open_id)))


@pytest.mark.skipif(not HAS_TRUSTED_IDENTITY, reason="Hermes lacks exact source/session identity seams")
class TestBoundStop:
    def test_topic_and_union_identity_survive_guarded_dispatch(self):
        adapter, _ = _adapter()
        turn = _bound_turn(adapter)
        seen, pending = [], []

        async def guarded(event):
            seen.append(event)
        adapter._handle_message_with_guards = guarded
        adapter._loop = asyncio.new_event_loop()
        adapter._submit_on_loop = lambda loop, coro: pending.append(coro) or True
        response = adapter._on_card_action_trigger(_click())
        assert response.toast.type == "info"
        _run(pending.pop())
        assert seen[0].text == "/stop"
        assert seen[0].source.thread_id == "omt_topic"
        assert seen[0].source.user_id == "tenant_user"
        assert seen[0].source.user_id_alt == "on_union"
        assert seen[0].metadata["gateway_session_key"] == turn.control_source.session_key
        adapter._loop.close()

    @pytest.mark.parametrize("kwargs", [{"open_id": "ou_other"}, {"chat": "oc_other"},
                                        {"message_id": "om_expired"}, {"message_id": ""}])
    def test_mismatched_callbacks_never_schedule(self, kwargs):
        adapter, _ = _adapter()
        _bound_turn(adapter)
        adapter._loop = asyncio.new_event_loop()
        adapter._submit_on_loop = lambda *args: pytest.fail("untrusted click scheduled")
        assert adapter._on_card_action_trigger(_click(**kwargs)).toast.type == "error"
        adapter._loop.close()

    def test_finalized_while_callback_is_queued_never_stops_next_turn(self):
        adapter, _ = _adapter()
        turn = _bound_turn(adapter)
        adapter._handle_message_with_guards = AsyncMock()
        adapter._stream_cards.clear()
        _run(adapter._dispatch_bound_stop(_click(), turn))
        adapter._handle_message_with_guards.assert_not_called()

    def test_original_admin_guard_still_applies(self):
        adapter, _ = _adapter()
        _bound_turn(adapter)
        adapter._admins = {"ou_someone_else"}
        assert adapter._stop_turn_from_event(_click().event) is None

    def test_dm_stays_dm_and_routed_profile_is_snapshotted_after_admission(self):
        adapter, _ = _adapter()
        source = _source(adapter, chat_type="dm", thread=None)
        source.profile = "xm"
        turn = StreamCardTurn(card_id="", message_id="om_card", chat_id=CHAT)
        binding = adapter._bind_card_source(turn, "om_in")
        assert binding.source.chat_type == "dm"
        assert binding.source.profile == "xm"
        assert binding.session_key == adapter._source_session_key(source)
        assert binding.session_key.startswith("agent:xm:")
        # The live source object must not mutate a card already bound to an admitted turn.
        source.profile = "tz"
        assert binding.source.profile == "xm"

    def test_ambiguous_parent_reply_cannot_choose_an_owner(self):
        adapter, _ = _adapter()
        _source(adapter, message_id="om_a")
        _source(adapter, message_id="om_b", open_id="ou_other")
        adapter._card_sources["om_a"] = replace(adapter._card_sources["om_a"], reply_anchor="om_parent")
        adapter._card_sources["om_b"] = replace(adapter._card_sources["om_b"], reply_anchor="om_parent")
        turn = StreamCardTurn(card_id="", message_id="", chat_id=CHAT)
        assert adapter._bind_card_source(turn, "om_parent") is None
        assert not adapter._can_stop_card(turn)


@pytest.fixture
def clarify_setup(monkeypatch):
    if not HAS_TRUSTED_IDENTITY:
        pytest.skip("Hermes lacks exact source/session identity seams; native clarify falls back to text")
    from feishu_cardkit import lifecycle
    from gateway.session_identity import RoutingIdentity
    from tools import clarify_gateway as clarify
    from plugins.platforms.feishu import adapter as bundled
    from lark_oapi.event.callback.model.p2_card_action_trigger import CallBackCard, P2CardActionTriggerResponse
    monkeypatch.setattr(bundled, "CallBackCard", CallBackCard)
    monkeypatch.setattr(bundled, "P2CardActionTriggerResponse", P2CardActionTriggerResponse)
    adapter, fake = _adapter()
    adapter._loop = NS(is_closed=lambda: False)
    turn = _bound_turn(adapter)
    binding = turn.control_source
    scope = str(Path("/tmp/cardkit-clarify-test").resolve())
    adapter._card_sources["om_in"].source._identity = RoutingIdentity(
        transport_profile="default", runtime_profile="default", multiplexed=False,
        authorization_home=Path(scope), runtime_home=Path(scope))
    monkeypatch.setattr(lifecycle, "current_scope", lambda: scope)
    monkeypatch.setattr(lifecycle.OBSERVATIONS, "active_for_session", lambda *args: NS(
        message_id="om_in", chat_id=CHAT, session_key=binding.session_key, profile_scope=scope))
    entry = clarify.register("c1", binding.session_key, "Which action?", ["Continue", "Stop"])
    yield adapter, fake, turn, entry
    clarify.clear_session(binding.session_key)


def _send_clarify(setup):
    adapter, _, turn, _ = setup
    return _run(adapter.send_clarify(CHAT, "Which action?", ["Continue", "Stop"], "c1",
                                    turn.control_source.session_key, {"thread_id": turn.thread_id}))


class TestClarify:
    def test_native_selection_preserves_question_and_real_answer(self, clarify_setup):
        adapter, _, _, entry = clarify_setup
        result = _send_clarify(clarify_setup)
        response = adapter._on_card_action_trigger(_click(
            {"hermes_cardkit": "clarify", "clarify_id": "c1", "choice": 0}, message_id=result.message_id))
        assert entry.event.is_set() and entry.response == "Continue"
        elements = response.card.data["body"]["elements"]
        assert elements[0]["content"] == "Which action?"
        assert elements[1]["content"] == "1. Continue\n2. Stop"
        assert "Continue" in elements[2]["content"]
        assert all(item["tag"] != "button" for item in elements)

    def test_foreign_operator_cannot_resolve_question(self, clarify_setup):
        adapter, _, _, entry = clarify_setup
        result = _send_clarify(clarify_setup)
        adapter._on_card_action_trigger(_click(
            {"hermes_cardkit": "clarify", "clarify_id": "c1", "choice": 0},
            message_id=result.message_id, open_id="ou_other"))
        assert not entry.event.is_set()

    def test_other_uses_original_text_capture_and_removes_buttons(self, clarify_setup):
        adapter, _, _, entry = clarify_setup
        result = _send_clarify(clarify_setup)
        response = adapter._on_card_action_trigger(_click(
            {"hermes_cardkit": "clarify", "clarify_id": "c1", "choice": "other"}, message_id=result.message_id))
        assert entry.awaiting_text and not entry.event.is_set()
        assert all(e["tag"] != "button" for e in response.card.data["body"]["elements"])
        adapter._on_card_action_trigger(_click(
            {"hermes_cardkit": "clarify", "clarify_id": "c1", "choice": 0}, message_id=result.message_id))
        assert not entry.event.is_set()  # a stale option cannot overwrite the chosen text path

    def test_expiry_preserves_question_without_live_actions(self, clarify_setup):
        adapter, fake, _, _ = clarify_setup
        _send_clarify(clarify_setup)
        _run(adapter.retire_clarify_card("c1", "Timed out"))
        card = json.loads(fake.named("im_update")[-1].request_body.content)
        assert [e["content"] for e in card["body"]["elements"]] == ["Which action?", "1. Continue\n2. Stop", "Timed out"]

    def test_expired_callback_never_resolves(self, clarify_setup):
        from tools import clarify_gateway as clarify
        adapter, _, turn, entry = clarify_setup
        result = _send_clarify(clarify_setup)
        clarify.clear_session(turn.control_source.session_key)
        response = adapter._on_card_action_trigger(_click(
            {"hermes_cardkit": "clarify", "clarify_id": "c1", "choice": 0}, message_id=result.message_id))
        assert entry.response == clarify.CANCELLED
        assert all(e["tag"] != "button" for e in response.card.data["body"]["elements"])

    def test_unsupported_multi_select_keeps_bundled_path(self, clarify_setup):
        adapter, _, _, entry = clarify_setup
        entry.multi_select = True
        with patch("gateway.platforms.base.BasePlatformAdapter.send_clarify", new=AsyncMock(return_value="text")) as fallback:
            assert _send_clarify(clarify_setup) == "text"
            fallback.assert_called_once()

    def test_continuation_is_linked_only_after_matching_delivered_receipt(self, clarify_setup):
        adapter, fake, turn, entry = clarify_setup
        _send_clarify(clarify_setup)
        from tools.clarify_gateway import resolve_gateway_clarify
        resolve_gateway_clarify("c1", "Continue")
        turn.answer = "The continued answer"
        receipt = SendResult(True, message_id="om_cont", raw_response=NS(data=NS(
            message_id="om_cont", chat_id=CHAT, thread_id=turn.thread_id, thread_message_position=9)))
        _run(adapter._after_card_delivery(turn, receipt))
        card = json.loads(fake.named("im_update")[-1].request_body.content)
        assert card["body"]["elements"][0]["content"] == "Which action?"
        button = card["body"]["elements"][-1]
        assert button["text"]["content"] == "查看续答"
        assert "thread_position=9" in button["behaviors"][0]["default_url"]

    def test_eager_empty_seed_does_not_claim_continuation_delivery(self, clarify_setup):
        adapter, fake, turn, _ = clarify_setup
        _send_clarify(clarify_setup)
        from tools.clarify_gateway import resolve_gateway_clarify
        resolve_gateway_clarify("c1", "Continue")
        _run(adapter._after_card_delivery(turn, _receipt(message_position=1)))
        assert not fake.named("im_update")

    def test_missing_receipt_metadata_keeps_answer_without_navigation(self, clarify_setup):
        adapter, fake, turn, _ = clarify_setup
        _send_clarify(clarify_setup)
        from tools.clarify_gateway import resolve_gateway_clarify
        resolve_gateway_clarify("c1", "Continue")
        turn.answer = "continued"
        _run(adapter._after_card_delivery(turn, SendResult(True, message_id="om_next")))
        card = json.loads(fake.named("im_update")[-1].request_body.content)
        assert all(e["tag"] != "button" for e in card["body"]["elements"])
        assert card["body"]["elements"][-1]["content"] == "已选择：Continue"

    def test_live_capacity_uses_text_fallback_without_dropping_prompt(self, clarify_setup, monkeypatch):
        from feishu_cardkit import interaction
        monkeypatch.setattr(interaction, "_PROMPTS_MAX", 0)
        with patch("gateway.platforms.base.BasePlatformAdapter.send_clarify", new=AsyncMock(return_value="text")):
            assert _send_clarify(clarify_setup) == "text"

    def test_natural_timeout_without_event_releases_capacity(self, clarify_setup, monkeypatch):
        from feishu_cardkit import interaction
        from tools import clarify_gateway as clarify
        adapter, _, turn, entry = clarify_setup
        monkeypatch.setattr(interaction, "_PROMPTS_MAX", 1)
        _send_clarify(clarify_setup)
        assert clarify.wait_for_response("c1", timeout=0.001) is None
        assert not entry.event.is_set()
        clarify.register("c2", turn.control_source.session_key, "Next?", ["Yes", "No"])
        result = _run(adapter.send_clarify(CHAT, "Next?", ["Yes", "No"], "c2",
                                          turn.control_source.session_key, {"thread_id": turn.thread_id}))
        assert result.success and set(adapter._clarify_cards) == {"c2"}
        assert not entry.event.is_set()  # pruning must never impersonate the official resolver

    def test_other_pending_text_is_not_evicted_at_capacity(self, clarify_setup, monkeypatch):
        from feishu_cardkit import interaction
        from tools import clarify_gateway as clarify
        adapter, _, turn, entry = clarify_setup
        monkeypatch.setattr(interaction, "_PROMPTS_MAX", 1)
        result = _send_clarify(clarify_setup)
        adapter._on_card_action_trigger(_click(
            {"hermes_cardkit": "clarify", "clarify_id": "c1", "choice": "other"}, message_id=result.message_id))
        clarify.register("c2", turn.control_source.session_key, "Next?", ["Yes", "No"])
        with patch("gateway.platforms.base.BasePlatformAdapter.send_clarify", new=AsyncMock(return_value="text")):
            assert _run(adapter.send_clarify(CHAT, "Next?", ["Yes", "No"], "c2",
                        turn.control_source.session_key, {"thread_id": turn.thread_id})) == "text"
        assert entry.awaiting_text and set(adapter._clarify_cards) == {"c1"}

    def test_retirement_after_other_keeps_real_typed_answer_without_continuation(self, clarify_setup):
        from tools import clarify_gateway as clarify
        adapter, fake, turn, _ = clarify_setup
        result = _send_clarify(clarify_setup)
        adapter._on_card_action_trigger(_click(
            {"hermes_cardkit": "clarify", "clarify_id": "c1", "choice": "other"}, message_id=result.message_id))
        assert clarify.resolve_text_response_for_session(turn.control_source.session_key, "My actual answer")
        _run(adapter.retire_clarify_card("c1", "retired"))
        card = json.loads(fake.named("im_update")[-1].request_body.content)
        assert card["body"]["elements"][-1]["content"] == "已选择：My actual answer"
        assert adapter._clarify_cards["c1"].notice == ""

    def test_skipped_retirement_keeps_gateway_notice(self, clarify_setup):
        from tools import clarify_gateway as clarify
        adapter, fake, turn, _ = clarify_setup
        _send_clarify(clarify_setup)
        assert clarify.resolve_gateway_clarify("c1", clarify.SKIPPED)
        _run(adapter.retire_clarify_card("c1", "Question skipped"))
        turn.answer = "continued without an answer"
        _run(adapter._after_card_delivery(turn, SendResult(True, message_id="om_next")))
        card = json.loads(fake.named("im_update")[-1].request_body.content)
        assert card["body"]["elements"][-1]["content"] == "Question skipped"

    def test_send_failure_exposes_gateway_text_fallback(self, clarify_setup):
        from gateway.run_turn_runner_clarify_delivery import text_fallback_coro
        from test_streaming_card import _fail
        adapter, _, turn, entry = clarify_setup
        adapter._feishu_send_with_retry = AsyncMock(return_value=_fail(9999))
        assert not _send_clarify(clarify_setup).success
        assert "c1" not in adapter._clarify_cards
        adapter.send = AsyncMock(return_value=SendResult(True, message_id="om_text"))
        result = _run(text_fallback_coro(adapter, chat_id=CHAT, question="Which action?",
                                       choices=["Continue", "Stop"], clarify_id="c1",
                                       session_key=turn.control_source.session_key,
                                       metadata={"thread_id": turn.thread_id}))
        assert result.success and entry.awaiting_text
        assert "Which action?" in adapter.send.call_args.kwargs["content"]


def _receipt(**data):
    return SendResult(True, message_id="om_answer", raw_response={"data": {
        "message_id": "om_answer", "chat_id": CHAT, **data}})


class TestNavigation:
    def test_verified_position_builds_expected_link(self):
        nav = navigation_from_result(_receipt(message_position=0), chat_id=CHAT)
        assert nav.url == "https://applink.feishu.cn/client/chat/open?openChatId=oc_chat&position=0"

    @pytest.mark.parametrize("data", [
        {}, {"message_id": "card_container", "message_position": 1},
        {"chat_id": "oc_other", "message_position": 1}, {"message_position": True},
        {"message_position": -1}, {"thread_id": "omt_other", "thread_message_position": 1},
        {"message_app_link": "https://evil.example/client/chat/open?openChatId=oc_chat&position=1"},
        {"message_app_link": "https://applink.feishu.cn/client/chat/open?openChatId=oc_chat&position=1&position=2"},
        {"message_app_link": "https://applink.feishu.cn/client/chat/open?openChatId=oc_chat&position=1", "message_position": 2},
    ])
    def test_unknown_or_inconsistent_receipts_have_no_link(self, data):
        assert navigation_from_result(_receipt(**data), chat_id=CHAT) is None

    def test_failed_delivery_has_no_link_even_if_metadata_present(self):
        receipt = _receipt(message_position=1)
        receipt.success = False
        assert navigation_from_result(receipt, chat_id=CHAT) is None


class TestInteractionCompatibility:
    def test_missing_identity_uses_original_clarify_path(self):
        adapter, _ = _adapter()
        adapter._source_session_key = None
        with patch("gateway.platforms.base.BasePlatformAdapter.send_clarify",
                   new=AsyncMock(return_value=SendResult(True, message_id="om_text"))) as fallback:
            result = _run(adapter.send_clarify(CHAT, "Question", ["Yes", "No"], "unknown", "session"))
            assert result.success
            fallback.assert_awaited_once()

    def test_unbound_card_never_advertises_stop(self):
        adapter, _ = _adapter()
        turn = StreamCardTurn(card_id="", message_id="", chat_id=CHAT)
        assert adapter._bind_card_source(turn, "unknown") is None
        assert not adapter._can_stop_card(turn)
