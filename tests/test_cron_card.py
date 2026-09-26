"""Cron deliveries as static cards: layout of ``build_cron_card`` and the adapter's ``send`` routing."""

from __future__ import annotations

import json
from typing import Any, Dict

from feishu_cardkit import card_layout as cl
from feishu_cardkit import cron_card as cc

from test_streaming_card import CHAT, _adapter, _fail, _run

REPORT = """📋 示例选煤厂 生产日报 2026-01-02（自动生成 · 06:00 推送）

# 2026年1月2日 生产日报（系统自动汇总）

## 产量
- 入洗原煤 **1,000 t**；精煤 **800 t**，回收率 **80.00%**

## 各班起停车与产量
| 班次 | 起车 | 停车 | 入洗 t |
|---|---|---|---:|
| 夜班 | 01:10 | 08:40 | 500 |
| 早班 | 09:05 | 16:30 | 500 |

## 昨日洞察
ℹ 入洗量正常
看板：https://example.com/"""


def _wrapped(content: str, name: str = "邢美生产日报推送飞书", job_id: str = "abc123") -> str:
    """Hermes's default cron wrapper (cron/scheduler_delivery.py, cron.wrap_response: true)."""
    return (f"Cronjob Response: {name}\n(job_id: {job_id})\n-------------\n\n{content}\n\n"
            f"To stop or manage this job, send me a new message (e.g. \"stop reminder {name}\").")


def _texts(card: Dict[str, Any]) -> str:
    return "\n".join(e.get("content", "") for e in card["body"]["elements"])


class TestSplitTitle:
    def test_title_line_with_subtitle_and_redundant_h1(self):
        title, subtitle, body = cc.split_title(REPORT)
        assert title == "示例选煤厂 生产日报 2026-01-02", "leading emoji dropped, parenthetical split off"
        assert subtitle == "自动生成 · 06:00 推送"
        assert body.startswith("## 产量"), "the H1 restating the title is dropped"

    def test_leading_h1_becomes_title(self):
        title, subtitle, body = cc.split_title("# 周报\n\n- 一\n- 二")
        assert (title, subtitle, body) == ("周报", "", "- 一\n- 二")

    def test_short_line_then_list(self):
        title, subtitle, body = cc.split_title("🔎 2026-01-02 生产日报 午间复核（12:15）\n- 日报已完整，可自动归档")
        assert (title, subtitle, body) == ("2026-01-02 生产日报 午间复核", "12:15", "- 日报已完整，可自动归档")

    def test_no_title_for_lone_line_list_or_long_line(self):
        for text in ("只有一行", "- 列表开头\n- 第二项", "长" * (cc.TITLE_MAX_CHARS + 1) + "\n正文", "| a |\n|---|\n| 1 |"):
            title, subtitle, body = cc.split_title(text)
            assert (title, subtitle, body) == ("", "", text.strip())


class TestUnwrap:
    def test_hermes_wrapper_is_removed(self):
        assert cc.unwrap(_wrapped(REPORT)) == ("邢美生产日报推送飞书", REPORT)

    def test_unwrapped_text_is_untouched(self):
        assert cc.unwrap(REPORT) == ("", REPORT)
        assert cc.unwrap("Cronjob Response: x\n正文") == ("", "Cronjob Response: x\n正文")

    def test_wrapped_card_uses_inner_title_and_names_job_in_footer(self):
        card = cc.build_cron_card(_wrapped(REPORT), sent_at="01-02 06:00")
        assert card["header"]["title"]["content"] == "示例选煤厂 生产日报 2026-01-02"
        text = _texts(card)
        assert "Cronjob Response" not in text and "job_id" not in text and "To stop" not in text
        assert card["body"]["elements"][-1]["content"] == "⏰ 定时任务 · 邢美生产日报推送飞书 · 01-02 06:00"

    def test_wrapped_lone_line_is_titled_by_job_name(self):
        card = cc.build_cron_card(_wrapped("[午间复核失败] 2026-01-02：接口不可用", name="午间复核"))
        assert card["header"]["title"]["content"] == "午间复核" and card["header"]["template"] == "red"

    def test_wrapper_around_nothing_keeps_bundled_path(self):
        assert cc.build_cron_card(_wrapped("")) is None


class TestBuildCronCard:
    def test_report_layout(self):
        card = cc.build_cron_card(REPORT, sent_at="01-02 06:00")
        assert card["schema"] == "2.0" and card["config"]["width_mode"] == "fill"
        assert card["header"]["title"]["content"] == "示例选煤厂 生产日报 2026-01-02"
        assert card["header"]["subtitle"]["content"] == "自动生成 · 06:00 推送"
        assert card["header"]["template"] == "blue"
        assert card["config"]["summary"]["content"] == "示例选煤厂 生产日报 2026-01-02"
        text = _texts(card)
        assert "## 产量" in text and "| 夜班 | 01:10 | 08:40 | 500 |" in text
        assert "表1" not in text, "cron cards do not number tables"
        assert "# 2026年1月2日" not in text
        elements = card["body"]["elements"]
        assert elements[-2] == {"tag": "hr"}
        assert elements[-1]["content"] == "⏰ 定时任务 · 01-02 06:00" and elements[-1]["text_size"] == "notation"

    def test_written_table_caption_is_kept_without_number(self):
        card = cc.build_cron_card("标题\n\n表1：各班产量\n| a |\n|---|\n| 1 |")
        assert "<font color=\"grey\">各班产量</font>" in _texts(card)

    def test_failure_is_red(self):
        assert cc.build_cron_card("[午间复核失败] 2026-01-02：接口不可用")["header"]["template"] == "red"
        lone = cc.build_cron_card("[午间复核失败] 2026-01-02：接口不可用")
        assert lone["header"]["title"]["content"] == "定时任务" and "接口不可用" in _texts(lone)
        assert cc.build_cron_card("⚠ 同步失败\n- 详情")["header"]["template"] == "red"

    def test_english_labels(self):
        card = cc.build_cron_card("only line", labels=cl.EN)
        assert card["header"]["title"]["content"] == "Scheduled task"

    def test_bundled_path_for_empty_media_or_oversized(self):
        assert cc.build_cron_card("") is None and cc.build_cron_card("  \n") is None
        assert cc.build_cron_card("报告\nMEDIA:/tmp/a.png") is None
        assert cc.build_cron_card("标题\n\n" + "长文本。" * 5000) is None


class TestAdapterSend:
    def _sent(self, fake) -> list:
        return [(r.request_body.msg_type, r.request_body.content) for r in fake.named("im_create")]

    def test_cron_delivery_is_one_card(self):
        adapter, fake = _adapter()
        result = _run(adapter.send(CHAT, _wrapped(REPORT), metadata={"job_id": "job1", "notify": True}))
        assert result.success
        sent = self._sent(fake)
        assert len(sent) == 1 and sent[0][0] == "interactive"
        card = json.loads(sent[0][1])
        assert card["header"]["title"]["content"] == "示例选煤厂 生产日报 2026-01-02"
        assert not fake.named("create"), "a static card needs no CardKit entity"

    def test_regular_send_is_unchanged(self):
        adapter, fake = _adapter()
        _run(adapter.send(CHAT, REPORT))
        assert [t for t, _ in self._sent(fake)] == ["post"]

    def test_rejected_card_falls_back_to_bundled_send(self):
        adapter, fake = _adapter()
        fake.scripts["im_create"] = [_fail(230099, "card invalid")]
        result = _run(adapter.send(CHAT, REPORT, metadata={"job_id": "job1"}))
        assert result.success
        assert [t for t, _ in self._sent(fake)] == ["interactive", "post"]

    def test_disabled_by_setting(self):
        adapter, fake = _adapter()
        adapter._cron_card = False
        _run(adapter.send(CHAT, REPORT, metadata={"job_id": "job1"}))
        assert [t for t, _ in self._sent(fake)] == ["post"]
