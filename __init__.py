"""hermes-feishu-cardkit — native streaming cards for Hermes Agent's Feishu / Lark channel.

Installed as a Hermes platform plugin, this package re-registers the ``feishu`` platform with an
adapter that subclasses Hermes's bundled one (see ``adapter.py``).  Hermes's platform registry is
last-writer-wins and user plugins are discovered after bundled ones, so no Hermes file is modified
and ``hermes update`` cannot undo the install.

Registration stays light: the bundled adapter module (~200 ms to import) is loaded only when the
gateway actually builds the adapter or needs one of its helpers, never on plain ``hermes`` startup.
"""

from __future__ import annotations

import importlib.util
import logging
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("hermes_feishu_cardkit")

__version__ = "0.1.0"

# Registry metadata mirrored from the bundled entry (plain values; the callables are proxied lazily).
_STATIC_ENTRY = dict(
    label="Feishu / Lark", required_env=["FEISHU_APP_ID", "FEISHU_APP_SECRET"],
    install_hint="Run `hermes setup` to install Feishu support.", allowed_users_env="FEISHU_ALLOWED_USERS",
    allow_all_env="FEISHU_ALLOW_ALL_USERS", cron_deliver_env_var="FEISHU_HOME_CHANNEL", max_message_length=8000,
    emoji="🪽", allow_update_command=True,
)
_captured: Optional[Dict[str, Any]] = None


def _bundled_entry() -> Dict[str, Any]:
    """The kwargs the bundled Feishu plugin passes to ``register_platform`` (captured once)."""
    global _captured
    if _captured is None:
        from plugins.platforms.feishu import adapter as bundled

        class _Capture:
            kwargs: Dict[str, Any] = {}

            def register_platform(self, **kwargs: Any) -> None:
                self.kwargs = kwargs

            def __getattr__(self, _name: str) -> Callable[..., None]:
                return lambda *a, **k: None  # any other registration the bundled plugin makes is ignored

        capture = _Capture()
        bundled.register(capture)
        _captured = capture.kwargs
    return _captured


def _proxy(name: str) -> Callable[..., Any]:
    def call(*args: Any, **kwargs: Any) -> Any:
        fn = _bundled_entry().get(name)
        return fn(*args, **kwargs) if fn else None
    call.__name__ = f"bundled_{name}"
    return call


async def _standalone_send(*args: Any, **kwargs: Any) -> Any:
    fn = _bundled_entry().get("standalone_sender_fn")
    if fn is None:
        return {"error": "bundled Feishu plugin has no standalone sender"}
    return await fn(*args, **kwargs)


def _deps_present() -> bool:
    """PASSIVE probe (status displays call it): is lark-oapi installed?"""
    return importlib.util.find_spec("lark_oapi") is not None


def _is_connected(config: Any) -> bool:
    """Feishu counts as connected once app_id is configured (same rule as the bundled entry)."""
    extra = getattr(config, "extra", None) or {}
    return bool(extra.get("app_id"))


def build_adapter(config: Any) -> Any:
    """CardkitFeishuAdapter, or the bundled adapter when this Hermes no longer exposes the seams we need."""
    from .adapter import CardkitFeishuAdapter, base_is_compatible
    problem = base_is_compatible()
    if problem:
        logger.warning("hermes-feishu-cardkit disabled (%s); using the bundled Feishu adapter", problem)
        return _bundled_entry()["adapter_factory"](config)
    return CardkitFeishuAdapter(config)


def register(ctx: Any) -> None:
    """Plugin entry point: take over the ``feishu`` platform entry."""
    ctx.register_platform(
        name="feishu", adapter_factory=build_adapter, check_fn=_deps_present,
        ensure_deps_fn=_proxy("ensure_deps_fn"), is_connected=_is_connected, validate_config=_is_connected,
        setup_fn=_proxy("setup_fn"), apply_yaml_config_fn=_proxy("apply_yaml_config_fn"),
        standalone_sender_fn=_standalone_send, **_STATIC_ENTRY,
    )
    logger.info("hermes-feishu-cardkit %s: 'feishu' platform now served by CardkitFeishuAdapter", __version__)


__all__ = ["register", "build_adapter", "__version__"]
