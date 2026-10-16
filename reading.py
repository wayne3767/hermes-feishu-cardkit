"""Resolve card reading preferences without reading global environment or profile files.

The caller may inject its active profile's environment getter. Explicit adapter
configuration wins; invalid values fall back safely and never appear in diagnostics.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Mapping, Optional


PRESETS = {
    "classic": {"timeline_expanded": False, "width_mode": "default"},
    "task": {"timeline_expanded": False, "width_mode": "fill"},
    "detailed": {"timeline_expanded": True, "width_mode": "fill"},
}
_FIELDS = {
    "reading_preset": ("card_reading_preset", "FEISHU_CARD_READING_PRESET"),
    "timeline_expanded": ("card_timeline_expanded", "FEISHU_CARD_TIMELINE_EXPANDED"),
    "width_mode": ("card_width_mode", "FEISHU_CARD_WIDTH_MODE"),
}


def _boolean(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if type(value) is int and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "on"}:
            return True
        if text in {"false", "0", "no", "off"}:
            return False
    return None


def resolve_reading(
    extra: Mapping[str, Any], *, env_get: Optional[Callable[[str], Any]] = None,
    source_label: str = "config",
) -> Dict[str, Dict[str, Any]]:
    """Return only public normalized ``values`` and their ``sources``.

    Precedence: explicit configuration, injected profile environment, preset.
    Missing/invalid preset uses classic. There is deliberately no ``os.getenv``
    fallback: a failed profile getter must not read another profile's environment.
    ``source_label='file/next-load'`` is useful for a file-only offline preview.
    """
    def setting(field: str):
        key, env = _FIELDS[field]
        if key in extra and extra[key] is not None:
            return extra[key], f"{source_label}:{key}"
        if env_get is not None:
            try:
                value = env_get(env)
            except Exception:
                return None, "environment-unavailable"
            if value not in (None, ""):
                return value, f"profile-environment:{env}"
        return None, ""

    raw, source = setting("reading_preset")
    preset = raw.strip().lower() if isinstance(raw, str) else ""
    if preset not in PRESETS:
        preset = "classic"
        source = f"default (invalid or unavailable {source})" if source else "default"
    values = {"reading_preset": preset, **PRESETS[preset]}
    sources = {"reading_preset": source}
    for field in ("timeline_expanded", "width_mode"):
        raw, explicit_source = setting(field)
        parsed = (_boolean(raw) if field == "timeline_expanded"
                  else raw.strip().lower() if isinstance(raw, str) else None)
        valid = parsed is not None if field == "timeline_expanded" else parsed in {"default", "compact", "fill"}
        if valid:
            values[field] = parsed
            sources[field] = explicit_source
        else:
            origin = f"preset:{preset} ({source})"
            sources[field] = f"{origin}; invalid or unavailable {explicit_source}" if explicit_source else origin
    return {"values": values, "sources": sources}


__all__ = ["PRESETS", "resolve_reading"]
