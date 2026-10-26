"""Small, request-id deduplicated accumulator for official Hermes usage events.

Only sanitized scalar fields are retained: never request bodies, responses, prompts,
URLs or credentials. Missing usage is unknown, including in accumulated totals.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import re
from typing import Any


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _seconds(value: Any) -> float | None:
    """A usable, bounded duration in seconds (event stamps and API durations share this)."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        return None
    return float(value) if 0 <= value <= 86_400 else None


def _label(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 160 or "://" in value:
        return ""
    return value if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:+() -]{0,159}", value) else ""


@dataclass
class TurnMetrics:
    max_requests: int = 2048
    _seen: set[str] = field(default_factory=set)
    _input: int | None = 0
    _output: int | None = 0
    _latest_order: tuple[float, int] = (-1, -1)
    _model: str = ""
    _declared_model: str = ""  # the model pre_llm_call said this session will call with
    _decode_seconds: float = 0.0  # time spent streaming output (TTFT excluded when known)
    _provider: str = ""
    _used: int | None = None
    _capacity: int | None = None
    _capped: bool = False

    def record(self, *, api_request_id: str | None = None, usage: Any = None, response_model: Any = None,
               model: Any = None, provider: Any = None, context_length: Any = None,
               api_call_count: Any = None, started_at: Any = None, api_duration: Any = None,
               first_chunk_at: Any = None, ended_at: Any = None, **_: Any) -> bool:
        if not isinstance(api_request_id, str) or not api_request_id or len(api_request_id) > 256:
            return False
        if self._capped or api_request_id in self._seen:
            return False
        if started_at is not None and (not isinstance(started_at, (int, float)) or
                                       isinstance(started_at, bool) or not math.isfinite(started_at)):
            return False
        if len(self._seen) >= self.max_requests:
            self._capped = True  # bounded memory; incomplete figures are no longer publishable
            return True
        self._seen.add(api_request_id)
        usage = usage if isinstance(usage, dict) else {}
        # Hermes canonical input_tokens excludes cache buckets; prompt_tokens is the
        # complete input count and must be used for both totals and context usage.
        prompt = _count(usage.get("prompt_tokens"))
        output = _count(usage.get("output_tokens"))
        self._input = self._input + prompt if self._input is not None and prompt is not None else None
        self._output = self._output + output if self._output is not None and output is not None else None
        decode = self._decode_duration(first_chunk_at, ended_at, api_duration)
        if output is not None and decode:
            self._decode_seconds += decode
        stamp = float(started_at) if isinstance(started_at, (int, float)) else 0.0
        order = (stamp, _count(api_call_count) or 0)
        if order >= self._latest_order:
            self._latest_order = order
            self._model = _label(response_model) or _label(model)
            self._provider = _label(provider)
            self._used, self._capacity = prompt, _count(context_length)
        return True

    @staticmethod
    def _decode_duration(first_chunk_at: Any, ended_at: Any, api_duration: Any) -> float:
        """Streaming time for one call: from the first chunk to the end, else the call duration."""
        first, ended = _seconds(first_chunk_at), _seconds(ended_at)
        if first is not None and ended is not None and ended > first:
            return ended - first
        duration = _seconds(api_duration)
        return duration if duration and duration > 0 else 0.0

    def note_model(self, model: Any = None) -> bool:
        """Remember the model the session declared (``pre_llm_call``) before any response arrives.

        Only a fallback: once an official response names the model it served, that one wins, so
        a fallback model or a provider swap is never reported as the configured default.
        """
        label = _label(model)
        if not label:
            return False
        self._declared_model = label
        return True

    def snapshot(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        model = self._model or self._declared_model
        if model:
            result["model"] = model
        if not self._seen or self._capped:
            return result  # totals are incomplete: only the model identity is publishable
        if self._provider:
            result["provider"] = self._provider
        tokens = {}
        if self._input is not None:
            tokens["input_tokens"] = self._input
        if self._output is not None:
            tokens["output_tokens"] = self._output
        if tokens:
            result["tokens"] = tokens
        if self._output and self._decode_seconds > 0:
            result["speed"] = self._output / self._decode_seconds  # tokens per second, per turn
        if self._used is not None and self._capacity:
            result["context"] = {"used_tokens": self._used, "max_tokens": self._capacity}
        return result
