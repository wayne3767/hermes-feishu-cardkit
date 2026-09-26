"""The plugin's ``feishu`` registration must keep every field the bundled entry registers."""

from __future__ import annotations

import feishu_cardkit as plugin


class _Ctx:
    def __init__(self) -> None:
        self.kwargs: dict = {}

    def register_platform(self, **kwargs) -> None:
        self.kwargs = kwargs


def test_registration_covers_the_bundled_entry():
    bundled = {k: v for k, v in plugin._bundled_entry().items() if v not in (None, "", [], False)}
    ctx = _Ctx()
    plugin.register(ctx)
    missing = sorted(set(bundled) - set(ctx.kwargs))
    assert not missing, f"bundled Feishu registers {missing}; mirror them in __init__.py"
    drifted = sorted(k for k, v in bundled.items() if not callable(v) and ctx.kwargs[k] != v)
    assert not drifted, f"static fields differ from the bundled entry: {drifted} (update _STATIC_ENTRY)"
    assert ctx.kwargs["name"] == "feishu" and callable(ctx.kwargs["adapter_factory"])
