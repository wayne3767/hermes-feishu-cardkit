"""Real regressions: API schema, official activity, epoch timings and serialized writes."""
import asyncio
import json
import re
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from feishu_cardkit import approval_reopen, card_layout as cl, lifecycle, streaming as fs
from feishu_cardkit.turn_metrics import TurnMetrics
from feishu_cardkit.tool_activity import ToolActivity
from test_lifecycle_cards import _adapter, admitted
from test_streaming_card import _adapter as plain_adapter, _run, _elements, _ok, CHAT
from test_approval_reopen import _Consumer


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


@pytest.mark.parametrize('phase', ['running', 'done', 'stopped', 'waiting_approval', 'waiting_input',
                                   'failed', 'timeout', 'blocked', 'unknown', 'segment'])
def test_actual_generated_cards_meet_platform_id_limit_and_have_one_activity_line(phase):
    card = cl.build_stream_card(title='unused', markdown='回答', activity='tool\n' + '数据' * 200,
                                tool_count=4, footer='f', phase=phase)
    nodes = list(walk(card))
    ids = [node['element_id'] for node in nodes if 'element_id' in node]
    assert len(set(ids)) == len(ids)
    assert all(re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,19}', item) for item in ids)
    assert all(node.get('tag') not in {'button', 'collapsible_panel'} for node in nodes)
    activity = card['body']['elements'][0]
    assert activity['element_id'] == cl.ACTIVITY_ELEMENT_ID
    assert '\n' not in activity['content'] and len(activity['content']) <= cl.ACTIVITY_MAX_CHARS
    assert card['body']['elements'][1]['element_id'] == cl.ANSWER_ELEMENT_ID


def test_official_events_show_latest_skill_and_tool_without_retaining_arguments():
    activity = ToolActivity()
    a = dict(tool_call_id='a', tool_name='skill_view', args={'name': 'report-writing', 'secret': 'PRIVATE'})
    b = dict(tool_call_id='b', tool_name='terminal', args={'command': 'PRIVATE_COMMAND'})
    assert activity.record('pre_tool_call', a)
    assert not activity.record('pre_tool_call', a)
    assert activity.snapshot() == {'count': 1, 'label': '⏳ skill_view: report-writing', 'active': True}
    assert activity.record('pre_tool_call', b)
    assert activity.record('post_tool_call', {**a, 'status': 'success', 'result': 'PRIVATE_RESULT'})
    assert activity.snapshot() == {'count': 2, 'label': '⏳ terminal', 'active': True}
    assert activity.record('post_tool_call', {**b, 'status': 'error'})
    assert activity.snapshot() == {'count': 2, 'label': '⚠️ terminal', 'active': False}
    assert not activity.record('post_tool_call', b)
    assert 'PRIVATE' not in repr(activity)


def test_tool_hook_then_text_then_another_tool_changes_status_and_replaces_activity(monkeypatch):
    store = lifecycle.ObservationStore()
    monkeypatch.setattr(lifecycle, 'OBSERVATIONS', store)
    context = admitted(store)
    event = dict(profile_scope='/test/profile-a', context=context, session_id='s-1', turn_id='agent-turn-1')
    adapter, fake = _adapter()

    async def scenario():
        await adapter.send_stream_frame('', chat_id=CHAT, reply_to='in-1', turn_id='t')
        turn = adapter._stream_cards[f'{CHAT}:t']
        store.observe('pre_tool_call', **event, tool_name='skill_view', tool_call_id='a', args={'name':'report-writing'})
        await adapter.send_stream_frame('', chat_id=CHAT, turn_id='t')
        assert adapter._status(turn, 'running') == '调用工具中'
        assert turn.activity == '⏳ skill_view: report-writing'
        store.observe('post_tool_call', **event, tool_name='skill_view', tool_call_id='a', args={'name':'report-writing'}, status='success')
        await adapter.send_stream_frame('我已读取资料', chat_id=CHAT, turn_id='t')
        assert adapter._status(turn, 'running') == '生成中'
        store.observe('pre_tool_call', **event, tool_name='terminal', tool_call_id='b', args={'command':'SECRET'})
        await adapter.send_stream_frame('我已读取资料', chat_id=CHAT, turn_id='t')
        assert adapter._status(turn, 'running') == '调用工具中', 'existing prose does not mean still generating'
        assert turn.activity == '⏳ terminal' and turn.tool_count == 2
        actions = json.loads(fake.named('batch')[-1].request_body.actions)
        line = actions[0]['params']['partial_element']['content']
        assert line == '⏳ terminal' and 'skill_view' not in line and 'SECRET' not in line
        store.observe('on_session_end', **event, completed=True)
        await adapter.send_stream_frame('完成', chat_id=CHAT, turn_id='t', finalize=True)
        card = fake.card_of(fake.named('update')[-1])
        assert _elements(card)[cl.ACTIVITY_ELEMENT_ID]['content'] == '工具调用已结束 · 2 次工具调用'
    _run(scenario())


def test_epoch_timestamps_exclude_first_token_wait_and_pair_each_tokens_duration():
    metrics = TurnMetrics()
    metrics.record(api_request_id='a', usage={'output_tokens': 200}, first_chunk_at=1793030405.0,
                   ended_at=1793030409.0, api_duration=9.0)
    assert metrics.snapshot()['speed'] == 50
    assert metrics.snapshot()['speed_estimated'] is False
    metrics.record(api_request_id='b', usage={'output_tokens': 10000})
    assert metrics.snapshot()['speed'] == 50, 'tokens with no duration must not inflate the rate'
    estimate = TurnMetrics()
    estimate.record(api_request_id='c', usage={'output_tokens': 100}, api_duration=4)
    assert estimate.snapshot()['speed'] == 25 and estimate.snapshot()['speed_estimated']


def test_live_speed_does_not_count_seed_wait_or_tool_wait_or_persist_after_silence():
    adapter, _ = plain_adapter()
    turn = fs.StreamCardTurn(card_id='c', message_id='m', chat_id=CHAT)
    with patch.object(fs.time, 'monotonic', return_value=100):
        adapter._note_speed(turn, '')
    with patch.object(fs.time, 'monotonic', return_value=160):
        adapter._note_speed(turn, '文' * 30)
        assert adapter._speed(turn, 'running') == ''
    with patch.object(fs.time, 'monotonic', return_value=162):
        adapter._note_speed(turn, '文' * 90)
        assert adapter._speed(turn, 'running') == '≈ 30 tok/s'
    with patch.object(fs.time, 'monotonic', return_value=200):
        assert adapter._speed(turn, 'running') == ''
        adapter._note_speed(turn, '文' * 900)
        assert adapter._speed(turn, 'running') == ''
    assert turn.decode_seconds == 2 and turn.decode_tokens == 60


def test_final_elapsed_is_frozen_when_card_later_receives_images():
    adapter, fake = plain_adapter()
    async def scenario():
        await adapter.send_stream_frame('', chat_id=CHAT, turn_id='t')
        turn = adapter._stream_cards[f'{CHAT}:t']
        await adapter.send_stream_frame('答案', chat_id=CHAT, turn_id='t', finalize=True)
        elapsed = turn.elapsed
        with patch.object(fs.time, 'monotonic', return_value=turn.finished_at + 100):
            assert turn.elapsed == elapsed
    _run(scenario())


def test_approval_marker_cannot_leak_across_adapters_or_turns():
    cls = type('BoundConsumer', (_Consumer,), {})
    approval_reopen.install_approval_reopen(consumer_cls=cls)
    a, b = SimpleNamespace(cardkit_approval_reopen=True), SimpleNamespace(cardkit_approval_reopen=True)
    consumer = cls(a)
    consumer._use_native_streaming = consumer._native_stream_opened = True
    consumer.chat_id, consumer._turn_id = CHAT, 'a'
    consumer.close_for_approval_prompt('confirm')
    assert not approval_reopen.consume_approval_boundary(b, f'{CHAT}:a')
    assert not approval_reopen.consume_approval_boundary(a, f'{CHAT}:b')
    assert approval_reopen.consume_approval_boundary(a, f'{CHAT}:a')
    assert not approval_reopen.consume_approval_boundary(a, f'{CHAT}:a')


def test_real_mac_cursor_is_removed_before_final_render():
    adapter, fake = plain_adapter()
    _run(adapter.send_stream_frame('', chat_id=CHAT, turn_id='t'))
    _run(adapter.send_stream_frame('答案 ▉', chat_id=CHAT, turn_id='t'))
    _run(adapter.send_stream_frame('答案', chat_id=CHAT, turn_id='t', finalize=True))
    assert '▉' not in json.dumps(fake.card_of(fake.named('update')[-1]), ensure_ascii=False)


def test_concurrent_seed_and_finalize_share_one_card():
    adapter, fake = plain_adapter()
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        original = adapter._create_card
        async def delayed(card):
            entered.set()
            await release.wait()
            return await original(card)
        adapter._create_card = delayed
        first = asyncio.create_task(adapter.send_stream_frame('', chat_id=CHAT, turn_id='t'))
        await entered.wait()
        second = asyncio.create_task(adapter.send_stream_frame('', chat_id=CHAT, turn_id='t'))
        final = asyncio.create_task(adapter.send_stream_frame('最终答案', chat_id=CHAT, turn_id='t', finalize=True))
        await asyncio.sleep(0)
        release.set()
        assert all(await asyncio.gather(first, second, final))
        assert len(fake.named('create')) == 1
        assert not adapter._stream_cards
        assert _elements(fake.card_of(fake.named('update')[-1]))[cl.ANSWER_ELEMENT_ID]['content'] == '最终答案'
    _run(scenario())
