"""Identity, official hook contract and metrics boundaries (no network calls)."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
import pytest

from feishu_cardkit.lifecycle import ObservationStore, ObservationRef
from feishu_cardkit.turn_metrics import TurnMetrics


def _begin(store, *, scope="/profile/a", session="s1", turn="t1", key="route1", message="om1", chat="oc1"):
    context = {"platform": "feishu", "session_key": key, "message_id": message, "chat_id": chat}
    store.observe("post_gateway_admission", profile_scope=scope, platform="feishu", session_key=key,
                  message_id=message, source={"chat_id": chat})
    store.observe("pre_llm_call", profile_scope=scope, context=context,
                  session_id=session, turn_id=turn, platform="feishu")
    return ObservationRef(scope, session, turn), context


def _post(store, ref, **extra):
    payload = dict(api_request_id="request1", usage={"prompt_tokens": 200, "output_tokens": 10},
                   model="configured", response_model="actual", provider="actual-provider", context_length=1000)
    payload.update(extra)
    store.observe("post_api_request", profile_scope=ref.profile_scope, session_id=ref.session_id,
                  turn_id=ref.turn_id, **payload)


def test_exact_admission_required_no_chat_or_session_guess():
    store = ObservationStore()
    ref, context = _begin(store)
    assert store.resolve(ref.profile_scope, "oc1", "om1") == ref
    assert store.resolve(None, "oc1", "om1") is None
    assert store.active_for_session(None, "route1", "oc1") is None
    assert store.resolve(None, "oc1", "om-other") is None
    assert store.resolve("/profile/b", "oc1", "om1") is None
    for altered in ({**context, "message_id": "other"}, {**context, "session_key": "other"},
                    {**context, "chat_id": "other"}):
        store.observe("pre_llm_call", profile_scope=ref.profile_scope, context=altered,
                      session_id="s2", turn_id="t2", platform="feishu")
    assert store.snapshot(ObservationRef(ref.profile_scope, "s2", "t2")) is None


def test_routed_profile_and_same_anchor_ambiguity_are_fail_closed():
    store = ObservationStore()
    a, _ = _begin(store)
    assert store.resolve(a.profile_scope, "oc1", "om1") == a
    b, _ = _begin(store, scope="/profile/b")
    assert store.resolve(None, "oc1", "om1") is None
    assert store.resolve("/profile/b", "oc1", "om1") == b
    c, _ = _begin(store, turn="t2")
    assert store.resolve("/profile/a", "oc1", "om1") is None
    _post(store, a)
    assert store.snapshot(a).metrics["model"] == "actual"
    assert store.snapshot(b).metrics == store.snapshot(c).metrics == {}


def test_auxiliary_or_child_events_cannot_contaminate_parent():
    store = ObservationStore()
    ref, context = _begin(store)
    store.observe("pre_llm_call", profile_scope=ref.profile_scope, context=context, session_id="child",
                  turn_id="child", parent_session_id=ref.session_id)
    assert store.resolve(ref.profile_scope, "oc1", "om1") == ref
    store.observe("post_auxiliary_call", profile_scope=ref.profile_scope,
                  session_id=ref.session_id, turn_id=ref.turn_id, api_request_id="aux", usage={"prompt_tokens": 99})
    _post(store, ObservationRef(ref.profile_scope, "child", "child"))
    assert store.snapshot(ref).metrics == {}


def test_metrics_dedupe_missing_usage_and_actual_model():
    metrics = TurnMetrics()
    assert metrics.snapshot() == {}
    assert metrics.record(api_request_id="a", usage={"input_tokens": 10, "cache_read_tokens": 40,
                          "prompt_tokens": 50, "output_tokens": 3}, response_model="actual", model="configured",
                          provider="provider1", context_length=100, api_call_count=1)
    assert not metrics.record(api_request_id="a", usage={"prompt_tokens": 999, "output_tokens": 999})
    assert metrics.snapshot() == {"model": "actual", "provider": "provider1",
                                  "tokens": {"input_tokens": 50, "output_tokens": 3},
                                  "context": {"used_tokens": 50, "max_tokens": 100}}
    assert metrics.record(api_request_id="b", model="fallback", provider="provider2", api_call_count=2)
    assert metrics.snapshot() == {"model": "fallback", "provider": "provider2"}


def test_declared_model_is_a_fallback_until_a_served_model_arrives():
    metrics = TurnMetrics()
    assert metrics.snapshot() == {}
    assert metrics.note_model("deepseek-v4.1-flash")
    assert metrics.snapshot() == {"model": "deepseek-v4.1-flash"}, "known before the first token"
    assert not metrics.note_model("https://secret.example/token")  # rejected like every untrusted label
    metrics.record(api_request_id="a", response_model="served-model", provider="p")
    assert metrics.snapshot()["model"] == "served-model", "the served model wins"


def test_context_is_latest_request_not_turn_sum_and_handles_out_of_order():
    metrics = TurnMetrics()
    for rid, sequence, prompt in (("new", 2, 200), ("old", 1, 100)):
        metrics.record(api_request_id=rid, api_call_count=sequence, model=rid, context_length=1000,
                       usage={"prompt_tokens": prompt, "output_tokens": 10})
    assert metrics.snapshot()["tokens"] == {"input_tokens": 300, "output_tokens": 20}
    assert metrics.snapshot()["context"] == {"used_tokens": 200, "max_tokens": 1000}
    assert metrics.snapshot()["model"] == "new"


def test_invalid_counts_and_missing_capacity_are_hidden_not_zero():
    metrics = TurnMetrics()
    metrics.record(api_request_id="a", usage={"prompt_tokens": True, "output_tokens": -1}, context_length=0)
    assert metrics.snapshot() == {}
    zero = TurnMetrics()
    zero.record(api_request_id="a", usage={"prompt_tokens": 0, "output_tokens": 0}, context_length=100)
    assert zero.snapshot()["tokens"] == {"input_tokens": 0, "output_tokens": 0}


def test_human_wait_requires_matching_request_and_exact_turn_context():
    store = ObservationStore()
    ref, context = _begin(store)
    identity = dict(profile_scope=ref.profile_scope, session_id=ref.session_id, session_key="route1")
    store.observe("on_human_input_request", **identity, context=context, kind="clarify", request_id="r1")
    assert store.snapshot(ref).phase == "waiting_input"
    store.observe("on_human_input_resolved", **identity, context=context, request_id="wrong", outcome="answered")
    assert store.snapshot(ref).phase == "waiting_input"
    store.observe("on_human_input_resolved", **identity, context={**context, "message_id": "wrong"},
                  request_id="r1", outcome="answered")
    assert store.snapshot(ref).phase == "waiting_input"
    store.observe("on_human_input_resolved", **identity, context=context, request_id="r1", outcome="answered")
    assert store.snapshot(ref).phase == "running"


def test_approval_denial_and_timeout_are_interaction_not_turn_termination():
    for outcome, phase in (("deny", "blocked"), ("timeout", "timeout"), ("notify_failed", "failed")):
        store = ObservationStore()
        ref, context = _begin(store)
        identity = dict(profile_scope=ref.profile_scope, session_id=ref.session_id, turn_id=ref.turn_id,
                        tool_call_id="tool1")
        store.observe("pre_approval_request", **identity, context=context)
        assert store.snapshot(ref).phase == "waiting_approval"
        store.observe("post_approval_response", **identity, context=context, choice=outcome)
        assert store.snapshot(ref).phase == phase
        assert not store.snapshot(ref).terminal
        _post(store, ref)
        assert store.snapshot(ref).phase == "running"


def test_parallel_waits_do_not_resume_until_all_resolved():
    store = ObservationStore()
    ref, context = _begin(store)
    identity = dict(profile_scope=ref.profile_scope, session_id=ref.session_id, context=context)
    for request in ("a", "b"):
        store.observe("on_human_input_request", **identity, kind="approval", request_id=request)
    store.observe("on_human_input_resolved", **identity, request_id="a", outcome="once")
    assert store.snapshot(ref).phase == "waiting_approval"
    store.observe("on_human_input_resolved", **identity, request_id="b", outcome="deny")
    assert store.snapshot(ref).phase == "blocked"


def test_stop_is_session_and_profile_scoped_late_completion_cannot_undo():
    store = ObservationStore()
    a, _ = _begin(store)
    b, _ = _begin(store, scope="/profile/b")
    store.observe("agent_loop_stopped", profile_scope=a.profile_scope, session_key="route1", platform="feishu")
    assert store.snapshot(a).phase == "stopped"
    assert store.snapshot(a).terminal
    assert store.snapshot(b).phase == "running"
    store.observe("on_session_end", profile_scope=a.profile_scope, session_id=a.session_id,
                  turn_id=a.turn_id, completed=True)
    assert store.snapshot(a).phase == "stopped"


def test_completion_and_failure_not_inferred_from_missing_flags():
    store = ObservationStore()
    ref, _ = _begin(store)
    identity = dict(profile_scope=ref.profile_scope, session_id=ref.session_id, turn_id=ref.turn_id)
    store.observe("on_session_end", **identity)
    assert not store.snapshot(ref).terminal
    store.observe("on_session_end", **identity, failed=True)
    assert store.snapshot(ref).phase == "failed"
    assert store.snapshot(ref).terminal


def test_snapshots_detached_subscribers_and_bounded_retention():
    store = ObservationStore(max_entries=2)
    ref, _ = _begin(store)
    updates = []
    unsubscribe = store.subscribe(ref, updates.append)
    _post(store, ref)
    updates[0].metrics["model"] = "cannot mutate store"
    assert store.snapshot(ref).metrics["model"] == "actual"
    unsubscribe()
    _post(store, ref, api_request_id="request2")
    assert len(updates) == 1
    _begin(store, message="om2", turn="t2")
    _begin(store, message="om3", turn="t3")
    assert store.snapshot(ref) is None


def test_official_context_and_register_hook_contract(monkeypatch):
    import feishu_cardkit.lifecycle as bridge
    from gateway.session_context import set_session_vars, clear_session_vars
    from hermes_cli.plugins import VALID_HOOKS
    if not {"post_gateway_admission", "pre_llm_call"} <= VALID_HOOKS:
        pytest.skip("Hermes lacks official admission hook needed for exact card/turn correlation")
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    callbacks = {}
    store = ObservationStore()
    monkeypatch.setattr(bridge, "OBSERVATIONS", store)
    bridge.register_hooks(SimpleNamespace(register_hook=lambda event, cb: callbacks.__setitem__(event, cb)))
    assert set(callbacks) <= VALID_HOOKS
    token = set_hermes_home_override("/test/cardkit-profile")
    tokens = set_session_vars(platform="feishu", session_key="routing", message_id="om-actual", chat_id="oc1")
    try:
        assert bridge._context()["session_key"] == "routing"
        callbacks["post_gateway_admission"](platform="feishu", session_key="routing",
                                            source={"chat_id": "oc1"}, message_id="om-actual")
        callbacks["pre_llm_call"](platform="feishu", session_id="session", turn_id="agent-uuid")
        ref = store.resolve("/test/cardkit-profile", "oc1", "om-actual")
        assert ref and ref.turn_id == "agent-uuid"
        assert ref.profile_scope == "/test/cardkit-profile"
    finally:
        clear_session_vars(tokens)
        reset_hermes_home_override(token)


def test_separate_profile_import_names_share_process_store():
    import feishu_cardkit.lifecycle as bridge
    # Match Hermes's per-profile plugin namespace behavior without loading any
    # configured user plugins or touching runtime services.
    package_name = "cardkit_profile_namespace_test"
    root = Path(bridge.__file__).parent
    spec = importlib.util.spec_from_file_location(package_name, root / "__init__.py", submodule_search_locations=[str(root)])
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    try:
        spec.loader.exec_module(package)
        other = importlib.import_module(package_name + ".lifecycle")
        assert other.OBSERVATIONS is bridge.OBSERVATIONS
    finally:
        for name in list(sys.modules):
            if name == package_name or name.startswith(package_name + "."):
                sys.modules.pop(name, None)


def test_caps_and_untrusted_scalar_fields():
    metrics = TurnMetrics(max_requests=1)
    assert not metrics.record(api_request_id=[])
    assert not metrics.record(api_request_id="bad", started_at=float("nan"))
    assert metrics.record(api_request_id="ok", response_model="https://secret.example/token", provider="bad\nvalue",
                          usage={"prompt_tokens": 10, "output_tokens": 2})
    assert metrics.snapshot() == {"tokens": {"input_tokens": 10, "output_tokens": 2}}
    assert metrics.record(api_request_id="overflow", usage={"prompt_tokens": 10, "output_tokens": 2})
    assert metrics.snapshot() == {}
    assert not metrics.record(api_request_id="another")
    assert len(metrics._seen) == 1


def test_old_hermes_skips_unsupported_hooks_and_registration_failure(monkeypatch):
    import feishu_cardkit.lifecycle as bridge
    import hermes_cli.plugins as plugins
    monkeypatch.setattr(plugins, "VALID_HOOKS", {"pre_llm_call", "post_api_request"})
    attempted = []
    def register(event, _callback):
        attempted.append(event)
        if event == "pre_llm_call":
            raise ValueError("old runtime rejected hook")
    assert bridge.register_hooks(SimpleNamespace(register_hook=register)) == ("post_api_request",)
    assert attempted == ["pre_llm_call", "post_api_request"]


def test_real_plugin_manager_and_human_approval_hooks_preserve_context(monkeypatch, tmp_path):
    import feishu_cardkit.lifecycle as bridge
    import hermes_cli.plugins as plugins
    lifecycle = pytest.importorskip("hermes_cli.lifecycle", reason="Hermes lacks lifecycle dispatch facade")
    from gateway.session_context import set_session_vars, clear_session_vars
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.approval_context import set_current_observability_context, reset_current_observability_context
    human_input_request = pytest.importorskip("tools.human_input_hooks", reason="Hermes lacks human-input hooks").human_input_request
    if not set(bridge._HOOKS) <= plugins.VALID_HOOKS:
        pytest.skip("Hermes lacks admission/human-input lifecycle hooks required by this integration")
    manager = plugins.PluginManager(scope_key=str(tmp_path))
    manager._discovered = True
    store = ObservationStore()
    monkeypatch.setattr(bridge, "OBSERVATIONS", store)
    # Exercise Hermes's actual dispatch and timeout/context propagation, avoiding
    # configured plugins and built-in telemetry that would write runtime state.
    monkeypatch.setattr(lifecycle, "invoke_hook", manager.invoke_hook)
    monkeypatch.setattr(plugins, "_resolve_hook_callback_timeout", lambda: 2.0)
    context = plugins.PluginContext(plugins.PluginManifest(name="cardkit-test"), manager)
    capabilities = bridge.register_hooks(context)
    assert set(capabilities) == set(bridge._HOOKS)
    token = set_hermes_home_override(tmp_path)
    tokens = set_session_vars(platform="feishu", session_key="routing", message_id="om1", chat_id="oc1")
    approval_tokens = set_current_observability_context(session_id="s1", turn_id="t1", tool_call_id="tool1")
    try:
        assert manager.invoke_hook("post_gateway_admission", platform="feishu", session_key="routing",
                                   source={"chat_id": "oc1"}, message_id="om1") == []
        assert manager.invoke_hook("pre_llm_call", platform="feishu", session_id="s1", turn_id="t1") == []
        ref = store.resolve(str(tmp_path.resolve()), "oc1", "om1")
        assert ref is not None
        # Real human-input payload has request_id and session_id but NO turn_id.
        with human_input_request("approval", session_key="routing") as human:
            assert store.snapshot(ref).phase == "waiting_approval"
            human.outcome = "timeout"
        assert store.snapshot(ref).phase == "timeout"
        assert not store.snapshot(ref).terminal
        manager.invoke_hook("pre_api_request", session_id="s1", turn_id="t1")
        assert store.snapshot(ref).phase == "running"
        # A legacy approval event without a correlation id cannot invent a wait.
        manager.invoke_hook("pre_approval_request", session_id="s1", turn_id="t1")
        assert store.snapshot(ref).phase == "running"
    finally:
        reset_current_observability_context(approval_tokens)
        clear_session_vars(tokens)
        reset_hermes_home_override(token)
