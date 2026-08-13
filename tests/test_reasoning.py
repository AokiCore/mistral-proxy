# -*- coding: utf-8 -*-
"""思考格式转换。样例结构取自对上游的实测 (.private/probe2_out.json)。"""
import copy

from core import reasoning as R

CHUNKS = [
    {"type": "thinking", "thinking": [{"type": "text", "text": "想一想"}], "closed": True},
    {"type": "text", "text": "结论"},
]


def chunk(text):
    return [{"type": "thinking", "thinking": [{"type": "text", "text": text}], "closed": True}]


# ---------- 拆分 ----------

def test_split_chunks():
    assert R.split_chunks(CHUNKS) == ("想一想", "结论")
    assert R.split_chunks("纯文本") == ("", "纯文本")
    assert R.split_chunks(None) == ("", "")
    assert R.split_chunks([None, 42, {"no": "text"}]) == ("", "")


def test_split_handles_bare_string_thinking():
    assert R.split_chunks([{"type": "thinking", "thinking": ["裸串"]}]) == ("裸串", "")


# ---------- 非流式 ----------

def test_reasoning_content_mode_emits_both_aliases():
    """DeepSeek 系客户端读 reasoning_content，OpenRouter 系读 reasoning，两个都给。"""
    msg = {"content": copy.deepcopy(CHUNKS)}
    assert R.apply_to_message(msg, R.REASONING_CONTENT) == (3, 2)
    assert msg["content"] == "结论"
    assert msg["reasoning_content"] == "想一想"
    assert msg["reasoning"] == "想一想"


def test_think_tags_mode_inlines():
    msg = {"content": copy.deepcopy(CHUNKS)}
    R.apply_to_message(msg, R.THINK_TAGS)
    assert msg["content"] == "<think>\n想一想\n</think>\n\n结论"
    assert "reasoning_content" not in msg


def test_strip_mode_drops_thinking():
    msg = {"content": copy.deepcopy(CHUNKS)}
    R.apply_to_message(msg, R.STRIP)
    assert msg["content"] == "结论"
    assert "reasoning_content" not in msg


def test_passthrough_mode_is_noop():
    msg = {"content": copy.deepcopy(CHUNKS)}
    R.apply_to_message(msg, R.PASSTHROUGH)
    assert msg["content"] == CHUNKS


def test_plain_string_content_untouched():
    msg = {"content": "已经是纯文本"}
    assert R.apply_to_message(msg, R.REASONING_CONTENT) == (0, len("已经是纯文本"))
    assert msg["content"] == "已经是纯文本"


def test_no_thinking_means_no_reasoning_key():
    msg = {"content": [{"type": "text", "text": "只有答案"}]}
    R.apply_to_message(msg, R.REASONING_CONTENT)
    assert msg["content"] == "只有答案"
    assert "reasoning_content" not in msg


# ---------- 流式 ----------

def feed_all(mode, deltas):
    """按上游实测的顺序喂：思考增量是 list，正文增量是 str。"""
    conv = R.StreamConverter(mode=mode)
    out = []
    for d in deltas:
        event = {"choices": [{"delta": {"content": d}}]}
        conv.feed(event)
        out.append(event["choices"][0]["delta"])
    return conv, out


def test_stream_reasoning_content():
    conv, deltas = feed_all(R.REASONING_CONTENT, [chunk("想"), chunk("一想"), "结", "论"])
    assert deltas[0]["reasoning_content"] == "想"
    assert deltas[0]["content"] == ""
    assert deltas[2]["content"] == "结"
    assert "reasoning_content" not in deltas[2]
    assert conv.think_chars == 3 and conv.answer_chars == 2


def test_stream_think_tags_opens_and_closes():
    """<think> 只在第一段思考前出现一次，</think> 在增量切回纯文本时补上。"""
    conv, deltas = feed_all(R.THINK_TAGS, [chunk("想"), chunk("一想"), "结", "论"])
    joined = "".join(d["content"] for d in deltas)
    assert joined == "<think>\n想一想\n</think>\n\n结论"
    assert joined.count("<think>") == 1
    assert joined.count("</think>") == 1
    assert conv.finalize() is None


def test_stream_think_tags_finalizes_unclosed():
    """只思考没正文（比如撞 max_tokens 截断）时，收尾必须补闭合标签。"""
    conv, deltas = feed_all(R.THINK_TAGS, [chunk("想到一半就没了")])
    assert "".join(d["content"] for d in deltas).startswith("<think>\n")
    tail = conv.finalize()
    assert tail == {"content": R.THINK_CLOSE}
    assert conv.finalize() is None


def test_stream_strip_drops_thinking():
    _, deltas = feed_all(R.STRIP, [chunk("想"), "答"])
    assert deltas[0]["content"] == ""
    assert deltas[1]["content"] == "答"


def test_stream_passthrough_keeps_chunks():
    _, deltas = feed_all(R.PASSTHROUGH, [chunk("想")])
    assert deltas[0]["content"] == chunk("想")


def test_stream_ignores_deltas_without_content():
    conv = R.StreamConverter(mode=R.REASONING_CONTENT)
    event = {"choices": [{"delta": {"role": "assistant"}}]}
    conv.feed(event)
    assert event["choices"][0]["delta"] == {"role": "assistant"}


def test_stream_tolerates_junk():
    conv = R.StreamConverter()
    for junk in ({}, {"choices": None}, {"choices": [{}]}, {"choices": [{"delta": "x"}]}):
        conv.feed(junk)


# ---------- reasoning tokens 估算 ----------

def test_reasoning_tokens_proportional():
    """上游不单独上报 reasoning_tokens，按字符占比从 completion_tokens 里切。"""
    conv, _ = feed_all(R.REASONING_CONTENT, [chunk("x" * 75), "y" * 25])
    assert conv.reasoning_tokens(100) == 75


def test_reasoning_tokens_zero_without_thinking():
    conv, _ = feed_all(R.REASONING_CONTENT, ["只有答案"])
    assert conv.reasoning_tokens(100) == 0
    assert R.reasoning_tokens_for(0, 10, 100) == 0
    assert R.reasoning_tokens_for(10, 0, 0) == 0
