"""A public footer label must not expose or mutate the observed backend model."""
from copy import deepcopy
import time

import pytest

from gateway.config import PlatformConfig
from feishu_cardkit.adapter import CardkitFeishuAdapter
from feishu_cardkit.card_layout import FOOTER_ELEMENT_ID
from feishu_cardkit.streaming import StreamCardTurn


@pytest.mark.parametrize("phase", ["running", "done"])
def test_model_label_is_profile_local_and_preserves_actual_metrics(monkeypatch, phase):
    from feishu_cardkit import adapter as module
    monkeypatch.setattr(module._bundled, "_get_scoped_secret", lambda name, default=None: default)
    public = CardkitFeishuAdapter(PlatformConfig(enabled=True, extra={"card_model_label": "public-model-v2.0"}))
    ordinary = CardkitFeishuAdapter(PlatformConfig(enabled=True, extra={}))
    metrics = {"model": "private-backend-model", "speed": 143, "speed_updated_at": time.monotonic(),
               "tokens": {"output_tokens": 1000}, "provider": "private-provider"}
    original = deepcopy(metrics)
    turn = StreamCardTurn(card_id="card", message_id="message", chat_id="chat", metrics=metrics,
                          started_at=100, finished_at=122)
    card = public._card(turn, phase, "answer")
    footer = next(e["content"] for e in card["body"]["elements"] if e.get("element_id") == FOOTER_ELEMENT_ID)
    assert footer.endswith("22s · 143 tok/s · public-model-v2.0")
    assert "private-backend-model" not in footer and "private-provider" not in footer
    assert public._footer(turn, phase) == footer, "live footer ticks use the same label as full cards"
    assert "private-backend-model" in ordinary._footer(turn, phase)
    assert metrics == original, "the display label cannot replace measured model/usage data"
