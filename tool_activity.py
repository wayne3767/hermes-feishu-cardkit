"""Bounded live tool activity from official hooks; no arguments or results retained."""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import re
from typing import Any


def _name(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value if re.fullmatch(r"[\w./:+-]{1,160}", value) else ""


@dataclass
class ToolActivity:
    # IDs are needed only to join pre/post hooks and deduplicate repeated events.
    # Card content contains one current label, not a transcript of these calls.
    max_calls: int = 2048
    calls: OrderedDict[str, str] = field(default_factory=OrderedDict)
    active: dict[str, str] = field(default_factory=dict)
    count: int = 0
    latest: str = ""
    state: str = ""

    def record(self, event: str, payload: dict) -> bool:
        call_id = payload.get("tool_call_id")
        name = _name(payload.get("tool_name"))
        if not isinstance(call_id, str) or not call_id or len(call_id) > 256 or not name:
            return False
        terminal = event == "post_tool_call"
        old = self.calls.get(call_id)
        if old == "done" or (old == "running" and not terminal):
            return False
        if old is None:
            self.count += 1
        if name == "skill_view":
            args = payload.get("args")
            skill = _name(args.get("name")) if isinstance(args, dict) else ""
            if skill:
                name += ": " + skill
        self.calls[call_id] = "done" if terminal else "running"
        self.calls.move_to_end(call_id)
        while len(self.calls) > self.max_calls:
            stale, _ = self.calls.popitem(last=False)
            self.active.pop(stale, None)
        self.latest = name
        if terminal:
            self.active.pop(call_id, None)
            status = str(payload.get("status") or "").lower()
            self.state = "failed" if status in {"error", "failed", "blocked", "cancelled", "timeout"} else "done"
            if self.active:
                self.latest = next(reversed(self.active.values()))
                self.state = "running"
        else:
            self.active[call_id] = name
            self.state = "running"
        return True

    def snapshot(self) -> dict:
        icon = {"running": "⏳", "done": "✅", "failed": "⚠️"}.get(self.state, "")
        return {"count": self.count, "label": f"{icon} {self.latest}".strip(),
                "active": bool(self.active)}
