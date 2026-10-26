"""A broken profile reader must not borrow the launching profile's card settings."""

import pytest

from feishu_cardkit import adapter
from gateway.config import PlatformConfig


@pytest.mark.parametrize("missing", [False, True])
def test_profile_reader_failure_never_reads_process_card_options(monkeypatch, missing):
    monkeypatch.setenv("FEISHU_CARD_STOP_BUTTON", "false")
    monkeypatch.setenv("FEISHU_CARD_LOCALE", "en")
    monkeypatch.setenv("FEISHU_CRON_CARD_TEMPLATE", "red")

    def unavailable(*args):
        raise RuntimeError("profile context unavailable")

    monkeypatch.setattr(adapter._bundled, "_get_scoped_secret", None if missing else unavailable)
    assert adapter._setting({}, "card_stop_button", "FEISHU_CARD_STOP_BUTTON", "true") == "true"
    assert adapter._setting({}, "card_locale", "FEISHU_CARD_LOCALE", "") == ""
    assert adapter._setting({}, "cron_card_template", "FEISHU_CRON_CARD_TEMPLATE", "violet") == "violet"
    assert adapter._setting({"card_stop_button": False}, "card_stop_button", "FEISHU_CARD_STOP_BUTTON", "true") is False


def test_adapter_settings_remain_owned_by_construction_profile(monkeypatch):
    profile = {"FEISHU_CARD_LOCALE": "zh", "FEISHU_CARD_STOP_BUTTON": "false"}
    monkeypatch.setattr(adapter._bundled, "_get_scoped_secret", lambda name, default=None: profile.get(name, default))
    first = adapter.CardkitFeishuAdapter(PlatformConfig(enabled=True, extra={}))
    profile.update(FEISHU_CARD_LOCALE="en", FEISHU_CARD_STOP_BUTTON="true")
    second = adapter.CardkitFeishuAdapter(PlatformConfig(enabled=True, extra={}))
    assert first._card_locale == "zh"
    assert second._card_locale == "en"
    assert not hasattr(second, "_stop_button"), "the stop button was removed in 0.7.0"
