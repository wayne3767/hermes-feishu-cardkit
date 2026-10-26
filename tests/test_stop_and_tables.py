"""Stop button on generating cards, and long markdown tables as paged table components."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from feishu_cardkit import card_layout as cl

from test_streaming_card import CHAT, _adapter, _elements, _run


def _table(rows: int, header: str = "| 日期 | 入洗 t | 备注 |", sep: str = "|---|---:|---|") -> str:
    body = "\n".join(f"| 9-{i:02d} | {1000 + i:,} | 正常 |" for i in range(1, rows + 1))
    return f"{header}\n{sep}\n{body}"


class TestTableComponents:
    def test_long_table_becomes_paged_component(self):
        els = cl.layout_answer_elements("表1：9 月产量\n" + _table(7))
        caption, table = els[0], els[1]
        assert caption["content"] == "<font color=\"grey\">表1　9 月产量</font>"
        assert table["tag"] == "table" and table["page_size"] == cl.TABLE_PAGE_SIZE
        assert table["row_height"] == "auto" and table["row_max_height"] == "999px"
        assert [c["display_name"] for c in table["columns"]] == ["日期", "入洗 t", "备注"]
        assert [c["horizontal_align"] for c in table["columns"]] == ["left", "right", "left"]
        assert all(c["data_type"] == "markdown" for c in table["columns"])
        assert len(table["rows"]) == 7 and table["rows"][0] == {"c0": "9-01", "c1": "1,001", "c2": "正常"}
        assert table["element_id"].startswith(cl.ANSWER_ELEMENT_ID)
        assert "freeze_first_column" not in table

    def test_short_table_stays_markdown(self):
        els = cl.layout_answer_elements(_table(cl.MARKDOWN_TABLE_ROWS))
        assert els[-1]["tag"] == "markdown" and els[-1]["content"].startswith("| 日期 |")

    def test_alignment_rules_and_wide_tables(self):
        header = "| a | b | c | d | e |"
        table = cl.table_component([header, "|:-:|:--|---|---|---|", "| x | y | 1.5 | 20% | — |", "| x | y | -2 | 3% | 4 |"])
        assert [c["horizontal_align"] for c in table["columns"]] == ["center", "left", "right", "right", "right"]
        assert table["freeze_first_column"] is True

    def test_escaped_pipe_and_ragged_rows(self):
        table = cl.table_component(["| a | b |", "|---|---|", "| x \\| y |", "| 1 | 2 | 3 |"])
        assert table["rows"] == [{"c0": "x | y", "c1": "", "c2": ""}, {"c0": "1", "c1": "2", "c2": "3"}]
        assert [column["display_name"] for column in table["columns"]] == ["a", "b", " "]

    def test_text_after_table_in_same_block_stays_prose(self):
        els = cl.layout_answer_elements(_table(6) + "\n注：单位为 t", number_tables=False)
        assert [e["tag"] for e in els] == ["table", "markdown"] and els[1]["content"] == "注：单位为 t"

    def test_at_most_five_components_per_card(self):
        els = cl.layout_answer_elements("\n\n".join(_table(6) for _ in range(6)))
        tags = [e["tag"] for e in els if e["tag"] != "markdown" or e["content"].startswith("|")]
        assert tags == ["table"] * cl.MAX_TABLE_COMPONENTS + ["markdown"]

    def test_code_block_table_is_not_converted(self):
        els = cl.layout_answer_elements("```\n" + _table(7) + "\n```")
        assert all(e["tag"] == "markdown" for e in els)


class TestStopButton:
    """The stop button was removed on purpose (0.7.0); the tool-chain toggle is the only card
    control left. The callback stays so buttons already sitting in chats still work."""

    def test_no_phase_renders_a_stop_button(self):
        kw = dict(title="T", markdown="x", timeline_md="", tool_count=0, footer="f")
        for phase in (cl.PHASE_RUNNING, cl.PHASE_DONE, cl.PHASE_STOPPED,
                      cl.PHASE_WAITING_APPROVAL, cl.PHASE_FAILED, cl.PHASE_SEGMENT):
            assert cl.STOP_BUTTON_ID not in _elements(cl.build_stream_card(phase=phase, **kw))

    def test_streaming_and_sealed_cards_have_no_stop_button(self):
        adapter, fake = _adapter()
        _run(adapter.send_stream_frame("", chat_id=CHAT, turn_id="t1"))
        assert cl.STOP_BUTTON_ID not in _elements(fake.card_of(fake.named("create")[0]))
        _run(adapter.send_stream_frame("完成", chat_id=CHAT, turn_id="t1", finalize=True))
        assert cl.STOP_BUTTON_ID not in _elements(fake.card_of(fake.named("update")[-1]))

    def _click(self, value, *, chat="oc_x", open_id="ou_1", token="tok1"):
        return SimpleNamespace(event=SimpleNamespace(
            action=SimpleNamespace(value=value, tag="button"), token=token,
            context=SimpleNamespace(open_chat_id=chat), operator=SimpleNamespace(open_id=open_id)))

    def test_unbound_click_cannot_dispatch_stop(self):
        adapter, _ = _adapter()
        loop = asyncio.new_event_loop()
        adapter._loop = loop
        dispatched, scheduled = [], []
        adapter._dispatch_synthetic_event = lambda **kw: dispatched.append(kw) or asyncio.sleep(0)
        adapter._submit_on_loop = lambda lp, coro: scheduled.append(coro) or True
        response = adapter._on_card_action_trigger(self._click(dict(cl.STOP_ACTION)))
        assert response.toast.type == "error"
        assert not dispatched and not scheduled
        for coro in scheduled:
            loop.run_until_complete(coro)
        loop.close()

    def test_duplicate_click_is_not_dispatched_twice(self):
        adapter, _ = _adapter()
        adapter._loop = asyncio.new_event_loop()
        adapter._stop_turn_from_event = lambda event: object()  # independently verified binding
        adapter._is_card_action_duplicate = lambda token: True
        adapter._submit_on_loop = lambda *a: (_ for _ in ()).throw(AssertionError("dispatched"))
        assert adapter._on_card_action_trigger(self._click(dict(cl.STOP_ACTION))).toast.content == "已请求停止"
        adapter._loop.close()

    def test_other_clicks_go_to_the_bundled_handler(self):
        adapter, _ = _adapter()
        seen = []
        from plugins.platforms.feishu.adapter import FeishuAdapter as Bundled
        original = Bundled._on_card_action_trigger
        Bundled._on_card_action_trigger = lambda self, data: seen.append(data) or "bundled"
        try:
            click = self._click({"hermes_action": "approve"})
            assert adapter._on_card_action_trigger(click) == "bundled" and seen == [click]
        finally:
            Bundled._on_card_action_trigger = original


