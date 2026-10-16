"""Multi-profile ownership proofs: same incoming message, independent bot/runtime."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.config import Platform
from gateway.session import SessionSource
_identity_api = pytest.importorskip("gateway.session_identity", reason="Hermes lacks pinned RoutingIdentity")
RoutingIdentity, canonical_identity = _identity_api.RoutingIdentity, _identity_api.canonical_identity
from feishu_cardkit import lifecycle
from feishu_cardkit.interaction import SourceBinding
from feishu_cardkit.streaming import StreamCardTurn
from test_streaming_card import _adapter, CHAT


def source_for(profile, *, home=None, receiving="default", message="shared-message"):
    """Exercise Hermes's canonical ingress resolver, not a serialized profile claim."""
    source = SessionSource(platform=Platform.FEISHU, chat_id=CHAT, message_id=message, user_id="owner")
    runtime_home = Path(home or f"/review/{profile}")
    runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True), _primary_profile_name=receiving,
        _transport_owner=lambda _: None, _profile_name_for_source=lambda _: profile,
        _resolve_profile_home_for_source=lambda _: runtime_home,
    )
    identity = canonical_identity(source, runner=runner, transport_profile=receiving,
                                  primary_home=Path("/review/transport"))
    assert isinstance(identity, RoutingIdentity)
    return source


def begin(store, *, profile="a", key=None, message="shared-message", sid=None, tid=None):
    scope = f"/review/{profile}"
    key = key or f"agent:{profile}:route"
    sid, tid = sid or f"session-{profile}", tid or f"turn-{profile}"
    context = dict(platform="feishu", session_key=key, message_id=message, chat_id=CHAT)
    store.observe("post_gateway_admission", profile_scope=scope, platform="feishu", session_key=key,
                  message_id=message, source={"chat_id": CHAT})
    store.observe("pre_llm_call", profile_scope=scope, context=context, session_id=sid, turn_id=tid)
    store.observe("post_api_request", profile_scope=scope, session_id=sid, turn_id=tid,
                  api_request_id="request-1", response_model=f"model-{profile}",
                  usage={"prompt_tokens": 123, "output_tokens": 45})
    return lifecycle.ObservationRef(scope, sid, tid)


def bound_turn(source, *, key=None):
    turn = StreamCardTurn(card_id="card", message_id="outgoing", chat_id=CHAT, reply_to=source.message_id)
    turn.control_source = SourceBinding(source, "owner", source.message_id,
                                        key or f"agent:{source.profile}:route")
    return turn


def test_late_second_profile_never_borrows_first_profiles_observation(monkeypatch):
    store = lifecycle.ObservationStore()
    monkeypatch.setattr(lifecycle, "OBSERVATIONS", store)
    begin(store, profile="a")
    adapter, _ = _adapter()
    turn = bound_turn(source_for("b"))
    assert not adapter._observe_turn(turn)
    assert turn.observation_ref is None and turn.metrics == {}
    own = begin(store, profile="b")
    assert adapter._observe_turn(turn)
    assert turn.observation_ref == own
    assert turn.metrics["model"] == "model-b"


def test_remembered_event_gets_core_pinned_identity_before_real_card_binding(monkeypatch):
    from gateway.platforms.base import MessageEvent, MessageType
    from gateway.session_identity import identity_of
    store = lifecycle.ObservationStore()
    monkeypatch.setattr(lifecycle, "OBSERVATIONS", store)
    adapter, _ = _adapter()
    adapter.gateway_runner = SimpleNamespace(
        config=SimpleNamespace(multiplex_profiles=True), _primary_profile_name="default",
        _transport_owner=lambda _: None, _owning_profile=lambda *args: (adapter, "default"),
        _profile_name_for_source=lambda *args, **kwargs: "b",
        _resolve_profile_home_for_source=lambda _: Path("/review/b"),
    )
    source = SessionSource(platform=Platform.FEISHU, chat_id=CHAT, user_id="owner", message_id="shared-message")
    event = MessageEvent(text="question", source=source, message_type=MessageType.TEXT, message_id=source.message_id,
                         raw_message=SimpleNamespace(event=SimpleNamespace(
                             sender=SimpleNamespace(sender_id=SimpleNamespace(open_id="owner")),
                             message=SimpleNamespace(message_id=source.message_id))))
    adapter._remember_card_source(event)
    assert adapter._card_sources[source.message_id].source is source
    assert identity_of(source) is None  # remembering does not guess or replace the original
    assert not adapter._drop_unresolved(event)  # real BasePlatformAdapter ingress pins this object
    assert identity_of(source).runtime_home == Path("/review/b")
    key = adapter._source_session_key(source)
    expected = begin(store, profile="b", key=key)
    turn = StreamCardTurn(card_id="card", message_id="out", chat_id=CHAT, reply_to=source.message_id)
    adapter._bind_card_source(turn, source.message_id)
    assert lifecycle.scope_for_source(turn.control_source.source) == "/review/b"
    adapter._observe_turn(turn)
    assert turn.observation_ref == expected


def test_routed_runtime_wins_over_ambient_and_receiving_profile(monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    store = lifecycle.ObservationStore()
    monkeypatch.setattr(lifecycle, "OBSERVATIONS", store)
    begin(store, profile="a")
    expected = begin(store, profile="b")
    source = source_for("b", receiving="default")
    assert lifecycle.scope_for_source(source) == "/review/b"
    token = set_hermes_home_override("/review/a")  # SDK/receiving-bot context is unrelated
    try:
        adapter, _ = _adapter()
        turn = bound_turn(source)
        adapter._observe_turn(turn)
        assert turn.observation_ref == expected
    finally:
        reset_hermes_home_override(token)


def test_same_profile_wrong_session_key_cannot_claim_observation(monkeypatch):
    store = lifecycle.ObservationStore()
    monkeypatch.setattr(lifecycle, "OBSERVATIONS", store)
    begin(store, profile="b", key="different-session")
    adapter, _ = _adapter()
    turn = bound_turn(source_for("b"))
    adapter._observe_turn(turn)
    assert turn.observation_ref is None and turn.metrics == {}


def test_existing_wrong_ref_is_withdrawn_and_never_rebound(monkeypatch):
    store = lifecycle.ObservationStore()
    monkeypatch.setattr(lifecycle, "OBSERVATIONS", store)
    wrong = begin(store, profile="a")
    begin(store, profile="b")
    adapter, _ = _adapter()
    turn = bound_turn(source_for("b"))
    turn.observation_ref, turn.metrics = wrong, {"model": "previous-foreign-model"}
    assert adapter._observe_turn(turn)
    assert turn.metrics == {} and turn.observation_lost
    assert turn.observation_ref == wrong  # a live card cannot change owners


def test_profile_string_or_missing_old_hermes_identity_never_substitutes_for_proof(monkeypatch):
    store = lifecycle.ObservationStore()
    monkeypatch.setattr(lifecycle, "OBSERVATIONS", store)
    begin(store, profile="b")
    source = SessionSource(platform=Platform.FEISHU, chat_id=CHAT, profile="b", message_id="shared-message")
    assert lifecycle.scope_for_source(source) is None
    adapter, _ = _adapter()
    turn = bound_turn(source)
    assert not adapter._observe_turn(turn)
    assert turn.metrics == {}
    import gateway.session_identity as identities
    monkeypatch.delattr(identities, "identity_of")
    assert lifecycle.scope_for_source(source) is None


def test_plugin_manager_namespaces_async_and_worker_contexts_stay_separate(monkeypatch, tmp_path):
    import sys
    import hermes_cli.plugins as plugins
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from gateway.session_context import set_session_vars, clear_session_vars
    store = lifecycle.ObservationStore()
    monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 2.0)
    package_root = str(Path(lifecycle.__file__).parent)
    managers, modules, names = [], [], []
    manifest = plugins.PluginManifest(name="cardkit-profile-review", key="cardkit-profile-review",
                                      source="project", path=package_root)
    for profile in ("a", "b"):
        manager = plugins.PluginManager(scope_key=str(tmp_path / profile))
        manager._discovered = True
        name = manager._directory_module_name(manifest)
        names.append(name)
        module = manager._load_directory_module(manifest, module_name=name)
        import importlib
        bridge = importlib.import_module(module.__name__ + ".lifecycle")
        modules.append(bridge)
        managers.append(manager)
        monkeypatch.setattr(bridge, "OBSERVATIONS", store)
        bridge.register_hooks(plugins.PluginContext(manifest, manager))
    assert names[0] != names[1]

    async def run_profile(index):
        manager = managers[index]
        scope = manager.scope_key
        token = set_hermes_home_override(scope)
        context_tokens = set_session_vars(platform="feishu", session_key=f"route-{index}",
                                          message_id="same-message", chat_id=CHAT)
        try:
            await manager.ainvoke_hook("post_gateway_admission", platform="feishu", session_key=f"route-{index}",
                                      message_id="same-message", source={"chat_id": CHAT})
            await asyncio.to_thread(manager.invoke_hook, "pre_llm_call", platform="feishu",
                                    session_id=f"s-{index}", turn_id="same-turn-id")
            await asyncio.to_thread(manager.invoke_hook, "post_api_request", platform="feishu",
                                    session_id=f"s-{index}", turn_id="same-turn-id", api_request_id="same-request-id",
                                    response_model=f"model-{index}", usage={"prompt_tokens": index + 1, "output_tokens": 1})
            ref = store.resolve(scope, CHAT, "same-message", session_key=f"route-{index}")
            assert ref.profile_scope == scope
            assert store.snapshot(ref).metrics["model"] == f"model-{index}"
        finally:
            clear_session_vars(context_tokens)
            reset_hermes_home_override(token)
    async def concurrent():
        await asyncio.gather(run_profile(0), run_profile(1))
    try:
        asyncio.run(concurrent())
    finally:
        for name in list(sys.modules):
            if any(name == prefix or name.startswith(prefix + ".") for prefix in names):
                sys.modules.pop(name, None)
