"""Read-only bridge from official Hermes hooks to native card observations.

Gateway transport turn ids and agent turn ids are unrelated UUIDs. Correlation
requires the admitted inbound *message id*, routed profile and session identity;
chat-id-only or most-recent-message guesses are intentionally unsupported.
"""
from __future__ import annotations

import logging
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Callable, NamedTuple

from .turn_metrics import TurnMetrics
from .tool_activity import ToolActivity

logger = logging.getLogger("hermes_feishu_cardkit.lifecycle")
_HOOKS = (
    "post_gateway_admission", "pre_llm_call", "pre_api_request", "post_api_request", "on_session_end",
    "pre_approval_request", "post_approval_response", "on_human_input_request",
    "on_human_input_resolved", "agent_loop_stopped",
    "pre_tool_call", "post_tool_call",
)


def current_scope() -> str:
    try:
        from hermes_constants import hermes_home_key
        return hermes_home_key()
    except Exception:
        return ""  # unknown scope is never conflated with a default profile


def scope_for_source(source: Any) -> str | None:
    """The runtime home pinned by Hermes ingress, never the receiving bot's home.

    The profile string and ambient callback context are insufficient: a receiving
    bot can route a message to another runtime, and callbacks run on SDK threads.
    Older Hermes without RoutingIdentity safely loses optional observations.
    """
    try:
        from gateway.session_identity import identity_of
        from hermes_constants import hermes_home_key
        identity = identity_of(source)
        return hermes_home_key(identity.runtime_home) if identity is not None else None
    except (ImportError, AttributeError, TypeError, ValueError, OSError):
        return None


def _context() -> dict[str, str]:
    try:
        from gateway.session_context import get_session_env
        names = {"platform": "PLATFORM", "session_key": "KEY", "message_id": "MESSAGE_ID",
                 "chat_id": "CHAT_ID", "session_id": "ID"}
        return {key: get_session_env("HERMES_SESSION_" + name, "") for key, name in names.items()}
    except Exception:
        return {}


class ObservationRef(NamedTuple):
    profile_scope: str
    session_id: str
    turn_id: str


@dataclass(frozen=True)
class ObservationSnapshot:
    ref: ObservationRef
    revision: int
    phase: str
    terminal: bool
    metrics: dict[str, Any]
    session_key: str
    chat_id: str
    message_id: str
    activity_kind: str = ""
    tools: dict[str, Any] = field(default_factory=dict)

    @property
    def profile_scope(self) -> str:
        return self.ref.profile_scope


@dataclass
class _Observation:
    ref: ObservationRef
    session_key: str
    chat_id: str
    message_id: str
    phase: str = "running"
    terminal: bool = False
    revision: int = 1
    metrics: TurnMetrics = field(default_factory=TurnMetrics)
    tools: ToolActivity = field(default_factory=ToolActivity)
    activity_kind: str = "thinking"
    pending: dict[str, str] = field(default_factory=dict)
    touched: float = field(default_factory=time.monotonic)


class ObservationStore:
    """Bounded, thread-safe in-memory state; callers receive detached snapshots."""

    def __init__(self, *, max_entries: int = 256, ttl: float = 21600) -> None:
        self._lock = threading.RLock()
        self._max_entries, self._ttl = max_entries, ttl
        self._admissions: OrderedDict[tuple[str, str, str], tuple[str, float]] = OrderedDict()
        self._observations: OrderedDict[ObservationRef, _Observation] = OrderedDict()
        self._listeners: dict[ObservationRef, list[Callable]] = {}

    def _prune(self) -> None:
        now = time.monotonic()
        for key, value in list(self._admissions.items()):
            if now - value[1] > self._ttl:
                self._admissions.pop(key, None)
        while len(self._admissions) > self._max_entries:
            self._admissions.popitem(last=False)
        for ref, obs in list(self._observations.items()):
            if now - obs.touched > self._ttl:
                self._observations.pop(ref, None)
                self._listeners.pop(ref, None)
        while len(self._observations) > self._max_entries:
            ref, _ = self._observations.popitem(last=False)
            self._listeners.pop(ref, None)

    @staticmethod
    def _snapshot(obs: _Observation) -> ObservationSnapshot:
        return ObservationSnapshot(obs.ref, obs.revision, obs.phase, obs.terminal, obs.metrics.snapshot(),
                                   obs.session_key, obs.chat_id, obs.message_id, obs.activity_kind, obs.tools.snapshot())

    def snapshot(self, ref: ObservationRef | None) -> ObservationSnapshot | None:
        with self._lock:
            self._prune()
            obs = self._observations.get(ref)
            return self._snapshot(obs) if obs else None

    def resolve(self, profile_scope: str | None, chat_id: str, message_id: str,
                *, session_key: str | None = None) -> ObservationRef | None:
        """Resolve an inbound id only inside a proven runtime scope.

        A card caller MUST supply its pinned runtime scope and routing session key;
        cross-scope uniqueness alone never proves ownership.
        Ambiguous anchors (including two profiles or agent turns) fail closed. A card
        must keep the first resolved ref and never rebind itself to a later turn.
        """
        if not profile_scope or not chat_id or not message_id:
            return None
        with self._lock:
            self._prune()
            refs = [r for r, o in self._observations.items() if o.chat_id == chat_id and
                    o.message_id == message_id and r.profile_scope == profile_scope
                    and (session_key is None or o.session_key == session_key)]
            return refs[0] if len(refs) == 1 else None

    def active_for_session(self, profile_scope: str | None, session_key: str,
                           chat_id: str) -> ObservationSnapshot | None:
        if not profile_scope or not session_key or not chat_id:
            return None
        with self._lock:
            self._prune()
            found = [o for r, o in self._observations.items() if not o.terminal and
                     o.session_key == session_key and o.chat_id == chat_id and
                     r.profile_scope == profile_scope]
            return self._snapshot(found[0]) if len(found) == 1 else None

    def subscribe(self, ref: ObservationRef, callback: Callable) -> Callable[[], None]:
        """Callbacks are sync, may run on worker threads, and must not block."""
        with self._lock:
            self._listeners.setdefault(ref, []).append(callback)
        def unsubscribe() -> None:
            with self._lock:
                callbacks = self._listeners.get(ref, [])
                if callback in callbacks:
                    callbacks.remove(callback)
                if not callbacks:
                    self._listeners.pop(ref, None)
        return unsubscribe

    def _match(self, scope: str, payload: dict, context: dict) -> _Observation | None:
        sid, tid = str(payload.get("session_id") or ""), str(payload.get("turn_id") or "")
        if sid and tid:
            return self._observations.get(ObservationRef(scope, sid, tid))
        # Human-input hooks omit turn_id. Require exact original inbound message,
        # session AND gateway routing key, never merely the most recent session.
        key = str(payload.get("session_key") or context.get("session_key") or "")
        mid = str(context.get("message_id") or "")
        if not sid or not key or not mid:
            return None
        candidates = [o for r, o in self._observations.items() if r.profile_scope == scope and
                      r.session_id == sid and o.session_key == key and o.message_id == mid and not o.terminal]
        return candidates[0] if len(candidates) == 1 else None

    @staticmethod
    def _outcome(value: Any) -> str | None:
        value = str(value or "").lower()
        if value in {"once", "session", "always", "approved", "answered", "submitted", "resolved"}:
            return "running"
        if value in {"deny", "denied", "blocked"}:
            return "blocked"
        if value in {"timeout", "timed_out"}:
            return "timeout"
        if value in {"error", "notify_failed", "failed"}:
            return "failed"
        if value in {"cancelled", "canceled", "interrupted"}:
            return "stopped"
        return None

    def observe(self, event: str, *, profile_scope: str, context: dict | None = None, **payload: Any) -> None:
        """Consume an official hook payload; no return can change gateway behavior."""
        if not profile_scope:
            return
        context = context or {}
        platform = str(payload.get("platform") or context.get("platform") or "")
        if platform and platform not in {"feishu", "lark"}:
            return
        callbacks = []
        snapshot = None
        with self._lock:
            self._prune()
            if event == "post_gateway_admission":
                source = payload.get("source") or {}
                key, mid, chat = (str(payload.get("session_key") or ""),
                                  str(payload.get("message_id") or ""), str(source.get("chat_id") or ""))
                if key and mid and chat and platform in {"feishu", "lark"}:
                    self._admissions[(profile_scope, key, mid)] = (chat, time.monotonic())
                    self._prune()
                return
            if event == "pre_llm_call":
                sid, tid = str(payload.get("session_id") or ""), str(payload.get("turn_id") or "")
                key, mid = str(context.get("session_key") or ""), str(context.get("message_id") or "")
                admitted = self._admissions.get((profile_scope, key, mid))
                # Persistence-disabled child agents have no pre_llm_call; also reject
                # any explicit child identity rather than inheriting the parent's anchor.
                if not sid or not tid or not admitted or payload.get("parent_session_id"):
                    return
                if context.get("chat_id") != admitted[0]:
                    return
                ref = ObservationRef(profile_scope, sid, tid)
                if ref in self._observations:
                    return
                obs = _Observation(ref, key, admitted[0], mid)
                # Known before the first token: the model this session will call with.
                # It is a fallback only; post_api_request replaces it with the served model.
                obs.metrics.note_model(payload.get("model"))
                self._observations[ref] = obs
                self._prune()
            elif event == "agent_loop_stopped":
                key = str(payload.get("session_key") or "")
                candidates = [o for r, o in self._observations.items() if r.profile_scope == profile_scope
                              and o.session_key == key and key and not o.terminal]
                if len(candidates) != 1:
                    return
                obs = candidates[0]
                obs.phase, obs.terminal = "stopped", True
            else:
                obs = self._match(profile_scope, payload, context)
                if obs is None:
                    return
                if event == "post_api_request":
                    if not obs.metrics.record(**payload):
                        return
                    if not obs.terminal and not obs.pending:
                        obs.phase = "running"
                elif event == "pre_api_request":
                    if obs.terminal or obs.pending:
                        return
                    obs.phase = "running"
                    obs.activity_kind = "thinking"
                elif event in {"pre_tool_call", "post_tool_call"}:
                    if obs.terminal or not obs.tools.record(event, payload):
                        return
                    obs.activity_kind = "tooling" if obs.tools.active else "thinking"
                elif event == "on_session_end":
                    if payload.get("interrupted"):
                        phase = "stopped"
                    elif payload.get("failed"):
                        phase = "failed"
                    elif payload.get("completed"):
                        phase = "done"
                    else:
                        return  # incomplete / unknown is not completed or stopped
                    # A late completion cannot undo an explicit interruption.
                    if obs.terminal and obs.phase == "stopped":
                        return
                    obs.phase, obs.terminal = phase, True
                    obs.pending.clear()
                elif obs.terminal:
                    return
                elif event in {"on_human_input_request", "pre_approval_request"}:
                    kind = payload.get("kind") if event == "on_human_input_request" else "approval"
                    identity = ("human:" + str(payload.get("request_id") or "")) if event.startswith("on_human") else (
                        "approval:" + str(payload.get("request_id") or payload.get("tool_call_id") or ""))
                    if identity.endswith(":"):
                        return
                    obs.pending[identity] = "waiting_approval" if kind == "approval" else "waiting_input"
                    obs.phase = obs.pending[identity]
                elif event in {"on_human_input_resolved", "post_approval_response"}:
                    identity = ("human:" + str(payload.get("request_id") or "")) if event.startswith("on_human") else (
                        "approval:" + str(payload.get("request_id") or payload.get("tool_call_id") or ""))
                    if identity not in obs.pending:
                        return
                    obs.pending.pop(identity)
                    phase = self._outcome(payload.get("outcome") if event.startswith("on_human") else payload.get("choice"))
                    obs.phase = next(iter(obs.pending.values())) if obs.pending else (phase or "running")
                else:
                    return
            obs.revision += 1
            obs.touched = time.monotonic()
            self._observations.move_to_end(obs.ref)
            callbacks, snapshot = list(self._listeners.get(obs.ref, ())), self._snapshot(obs)
        for callback in callbacks:
            try:
                callback(snapshot)
            except Exception:
                logger.debug("card observation subscriber failed", exc_info=True)


# Hermes uses a separate plugin module namespace per profile. A receiving bot's
# adapter can live in a different namespace from the routed agent. One versioned
# process-local registry connects them; every stored identity still includes scope.
_shared = sys.modules.setdefault("_hermes_feishu_cardkit_observations_v1", ModuleType(
    "_hermes_feishu_cardkit_observations_v1"))
# setdefault is atomic even if two profile namespaces import in parallel.
OBSERVATIONS: ObservationStore = _shared.__dict__.setdefault("store", ObservationStore())


def register_hooks(ctx: Any) -> tuple[str, ...]:
    register = getattr(ctx, "register_hook", None)
    if not callable(register):
        return ()
    try:
        from hermes_cli.plugins import VALID_HOOKS
    except ImportError:
        logger.info("CardKit lifecycle observations unavailable on this Hermes version")
        return ()
    installed = []
    for event in _HOOKS:
        if event not in VALID_HOOKS:
            continue
        def observe(_event: str = event, **payload: Any) -> None:
            OBSERVATIONS.observe(_event, profile_scope=current_scope(), context=_context(), **payload)
        try:
            register(event, observe)
            installed.append(event)
        except Exception:
            logger.warning("CardKit could not register lifecycle hook %s", event, exc_info=True)
    logger.info("CardKit lifecycle capabilities: %s", ", ".join(installed) or "none")
    return tuple(installed)


__all__ = ["OBSERVATIONS", "ObservationStore", "ObservationRef", "ObservationSnapshot", "current_scope",
           "scope_for_source", "register_hooks"]
