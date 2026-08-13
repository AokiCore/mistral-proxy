# -*- coding: utf-8 -*-
"""OpenAI 协议兼容层。每条断言都对应一个上游实测出来的不兼容点。"""
import base64
import json
import struct

import pytest

from core.openai_compat import (RequestError, normalize_chat_request,
                                normalize_chat_response, normalize_embeddings_request,
                                normalize_embeddings_response, normalize_error,
                                normalize_moderations_response, normalize_stream_event,
                                sanitize_messages)

BASE = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}


# ---------- 会被上游 422 拒绝的标准 OpenAI 参数 ----------

@pytest.mark.parametrize("param,value", [
    ("logit_bias", {"1": 1}), ("seed", 42), ("user", "u1"), ("store", True),
    ("logprobs", True), ("top_logprobs", 2),
])
def test_params_upstream_rejects_are_dropped(param, value):
    body, _ = normalize_chat_request({**BASE, param: value})
    assert param not in body


def test_max_completion_tokens_renamed():
    """OpenAI 已用 max_completion_tokens 取代 max_tokens，上游只认后者。"""
    body, _ = normalize_chat_request({**BASE, "max_completion_tokens": 100})
    assert body["max_tokens"] == 100
    assert "max_completion_tokens" not in body


def test_supported_params_pass_through():
    body, _ = normalize_chat_request({
        **BASE, "temperature": 0.5, "top_p": 0.9, "n": 2, "presence_penalty": 0.1,
        "frequency_penalty": 0.2, "response_format": {"type": "json_object"},
        "tools": [{"type": "function"}], "tool_choice": "auto"})
    for key in ("temperature", "top_p", "n", "presence_penalty", "frequency_penalty",
                "response_format", "tools", "tool_choice"):
        assert key in body


def test_stop_string_becomes_list():
    body, _ = normalize_chat_request({**BASE, "stop": "END"})
    assert body["stop"] == ["END"]


# ---------- 消息清洗 ----------

def test_replayed_reasoning_content_is_stripped():
    """DeepSeek 风格客户端会把上一轮思考回传，上游对此直接 422。"""
    body, _ = normalize_chat_request({**BASE, "messages": [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "yo", "reasoning_content": "思考过程",
         "reasoning": "思考过程", "reasoning_details": [{"text": "x"}]},
        {"role": "user", "content": "again"}]})
    assistant = body["messages"][1]
    # 这些键当字段转发就是 422，必须消失；内容本身改写进正文，多轮里才不丢上下文
    assert set(assistant) == {"role", "content"}
    assert assistant["content"].endswith("yo")
    assert "思考过程" in assistant["content"]


def test_unknown_message_keys_dropped():
    body, _ = normalize_chat_request({**BASE, "messages": [
        {"role": "assistant", "content": "x", "refusal": None, "annotations": [],
         "audio": None, "function_call": {}, "cache_control": {"type": "ephemeral"}}]})
    assert set(body["messages"][0]) == {"role", "content"}


def test_developer_role_downgraded_to_system():
    body, _ = normalize_chat_request({**BASE, "messages": [
        {"role": "developer", "content": "be terse"},
        {"role": "user", "content": "hi"}]})
    assert body["messages"][0]["role"] == "system"


def test_tool_message_keeps_name_and_id():
    msgs = sanitize_messages([{"role": "tool", "content": "42", "tool_call_id": "c1",
                               "name": "calc"}])
    assert msgs[0] == {"role": "tool", "content": "42", "tool_call_id": "c1", "name": "calc"}


def test_assistant_tool_calls_preserved():
    calls = [{"id": "1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    msgs = sanitize_messages([{"role": "assistant", "content": None, "tool_calls": calls}])
    assert msgs[0]["tool_calls"] == calls


# ---------- 分段 content：上游是严格 schema，客户端的各种写法都要先抹平 ----------

@pytest.mark.parametrize("role", ["system", "assistant", "tool", "developer"])
def test_text_only_roles_flattened_to_string(role):
    msgs = sanitize_messages([{"role": role, "content": [
        {"type": "text", "text": "one"}, {"type": "text", "text": "two"}]}])
    assert msgs[0]["content"] == "one\ntwo"


def test_cache_control_stripped_from_parts():
    """带 cache_control 的分段会让上游 422（extra_forbidden）。"""
    msgs = sanitize_messages([{"role": "user", "content": [
        {"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]}])
    assert msgs[0]["content"] == [{"type": "text", "text": "hi"}]


def test_annotations_stripped_from_parts():
    msgs = sanitize_messages([{"role": "user", "content": [
        {"type": "text", "text": "hi", "annotations": []}]}])
    assert msgs[0]["content"] == [{"type": "text", "text": "hi"}]


@pytest.mark.parametrize("given,expected", [("input_text", "text"), ("output_text", "text"),
                                            ("input_image", "image_url"), ("image", "image_url")])
def test_part_type_aliases_mapped(given, expected):
    part = {"type": given, "text": "hi"} if expected == "text" else {"type": given,
                                                                     "image_url": "u"}
    msgs = sanitize_messages([{"role": "user", "content": [part]}])
    assert msgs[0]["content"][0]["type"] == expected


def test_bare_string_element_wrapped():
    msgs = sanitize_messages([{"role": "user", "content": ["hi"]}])
    assert msgs[0]["content"] == [{"type": "text", "text": "hi"}]


def test_part_without_type_inferred():
    msgs = sanitize_messages([{"role": "user", "content": [{"text": "hi"}]}])
    assert msgs[0]["content"] == [{"type": "text", "text": "hi"}]


def test_null_text_coerced_to_empty():
    msgs = sanitize_messages([{"role": "user", "content": [{"type": "text", "text": None}]}])
    assert msgs[0]["content"] == [{"type": "text", "text": ""}]


def test_image_kept_for_user_dropped_for_system():
    image = {"type": "image_url", "image_url": {"url": "https://x/y.png"}}
    user = sanitize_messages([{"role": "user", "content": [{"type": "text", "text": "a"}, image]}])
    assert user[0]["content"] == [{"type": "text", "text": "a"}, image]
    # 上游不允许 system 带图片，拍平时只留文本
    system = sanitize_messages([{"role": "system",
                                 "content": [{"type": "text", "text": "a"}, image]}])
    assert system[0]["content"] == "a"


def test_unrecognisable_part_dropped():
    msgs = sanitize_messages([{"role": "user", "content": [{"foo": "bar"}, 42,
                                                           {"type": "text", "text": "keep"}]}])
    assert msgs[0]["content"] == [{"type": "text", "text": "keep"}]


def test_plain_string_content_untouched():
    msgs = sanitize_messages([{"role": "user", "content": "hi"}])
    assert msgs[0]["content"] == "hi"


# ---------- 上一轮思考的回传 ----------
# 实测上游对这件事极不一致：glm-5-2 在分词前就把 ThinkChunk 和 <think>...</think>
# 两种形式一起剥掉（prompt_tokens 与完全不传时相同），mistral-medium 收到 ThinkChunk
# 直接 400。所以默认改写成纯文本标记，这个形式三个模型都验证过能进上下文。

THINK_PART = {"type": "thinking", "thinking": [{"type": "text", "text": "step one"}]}


def test_thinkchunk_folded_into_text():
    msgs = sanitize_messages([{"role": "assistant",
                               "content": [THINK_PART, {"type": "text", "text": "答案"}]}])
    content = msgs[0]["content"]
    assert "step one" in content, "思考不能丢"
    assert "<think>" not in content, "不能用会被 glm 剥掉的 <think>"
    assert content.endswith("答案")


def test_reasoning_content_field_folded():
    """DeepSeek 风格客户端会把思考放在 reasoning_content 里，直接转发会 422。"""
    msgs = sanitize_messages([{"role": "assistant", "content": "答案",
                               "reasoning_content": "step one"}])
    assert "reasoning_content" not in msgs[0]
    assert "step one" in msgs[0]["content"]


def test_inline_think_tag_rewritten():
    """think_tags 模式自己发出去的 <think>，回传时必须换个包装，否则 glm 会吞掉。"""
    msgs = sanitize_messages([{"role": "assistant",
                               "content": "<think>step one</think>答案"}])
    assert "<think>" not in msgs[0]["content"]
    assert "step one" in msgs[0]["content"]
    assert msgs[0]["content"].endswith("答案")


def test_passback_off_drops_reasoning():
    msgs = sanitize_messages([{"role": "assistant", "content": "答案",
                               "reasoning_content": "step one"}], passback="off")
    assert msgs[0]["content"] == "答案"
    assert "step one" not in json.dumps(msgs[0], ensure_ascii=False)


def test_passback_native_keeps_thinkchunk():
    msgs = sanitize_messages([{"role": "assistant",
                               "content": [THINK_PART, {"type": "text", "text": "答案"}]}],
                             passback="native")
    # native 下不改写，但 assistant 仍要拍平成字符串（上游对该角色只收字符串更稳）
    assert "step one" not in msgs[0]["content"], "native 模式不负责搬运思考"


def test_reasoning_without_answer_still_kept():
    msgs = sanitize_messages([{"role": "assistant", "content": "",
                               "reasoning_content": "step one"}])
    assert "step one" in msgs[0]["content"]


def test_no_reasoning_leaves_content_alone():
    msgs = sanitize_messages([{"role": "assistant", "content": "答案"}])
    assert msgs[0]["content"] == "答案"


def test_user_message_think_tag_not_touched():
    """用户自己写的 <think> 是正文，不是上一轮思考，不能动。"""
    msgs = sanitize_messages([{"role": "user", "content": "<think>foo</think>bar"}])
    assert msgs[0]["content"] == "<think>foo</think>bar"


def test_tool_calls_survive_passback():
    calls = [{"id": "1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]
    msgs = sanitize_messages([{"role": "assistant", "content": None, "tool_calls": calls,
                               "reasoning_content": "step one"}])
    assert msgs[0]["tool_calls"] == calls
    assert "step one" in msgs[0]["content"]


@pytest.mark.parametrize("messages", [None, [], "nope", [{"role": "wizard", "content": "x"}],
                                      ["not a dict"]])
def test_bad_messages_rejected_locally(messages):
    with pytest.raises(RequestError):
        normalize_chat_request({**BASE, "messages": messages})


def test_missing_model_rejected():
    with pytest.raises(RequestError):
        normalize_chat_request({"messages": [{"role": "user", "content": "hi"}]})


# ---------- reasoning_effort 映射 ----------

@pytest.mark.parametrize("given,expected", [
    ("minimal", "none"), ("low", "none"), ("none", "none"),
    ("medium", "high"), ("high", "high"), ("max", "high"),
])
def test_effort_mapped_to_supported_values(given, expected):
    """上游只接受 none/high，OpenAI 的 low/medium/minimal 必须映射。"""
    body, _ = normalize_chat_request({**BASE, "reasoning_effort": given})
    assert body["reasoning_effort"] == expected


def test_openrouter_reasoning_object():
    body, meta = normalize_chat_request({**BASE, "reasoning": {"effort": "high"}})
    assert body["reasoning_effort"] == "high"
    assert "reasoning" not in body
    assert meta["exclude_reasoning"] is False


def test_openrouter_exclude_flag():
    _, meta = normalize_chat_request({**BASE, "reasoning": {"effort": "high", "exclude": True}})
    assert meta["exclude_reasoning"] is True


def test_deepseek_thinking_object():
    body, _ = normalize_chat_request({**BASE, "thinking": {"type": "enabled"}})
    assert body["reasoning_effort"] == "high"
    body, _ = normalize_chat_request({**BASE, "thinking": {"type": "disabled"}})
    assert body["reasoning_effort"] == "none"


def test_qwen_enable_thinking():
    body, _ = normalize_chat_request({**BASE, "enable_thinking": True})
    assert body["reasoning_effort"] == "high"


def test_effort_dropped_for_non_reasoning_model():
    body, _ = normalize_chat_request({**BASE, "reasoning_effort": "high"},
                                     supports_reasoning=False)
    assert "reasoning_effort" not in body


# ---------- 错误包络 ----------

def test_mistral_error_shape():
    body = normalize_error(400, b'{"object":"error","message":"Invalid model: x",'
                                b'"type":"invalid_model","param":null,"code":"1500"}')
    assert body == {"error": {"message": "Invalid model: x", "type": "invalid_model",
                              "param": None, "code": "1500"}}


def test_nested_pydantic_inside_message():
    raw = (b'{"object":"error","message":{"detail":[{"type":"extra_forbidden",'
           b'"loc":["body","seed"],"msg":"Extra inputs are not permitted"}]},'
           b'"type":"invalid_request_error","param":null,"code":null}')
    err = normalize_error(422, raw)["error"]
    assert "seed" in err["message"]
    assert err["param"] == "seed"
    assert err["type"] == "invalid_request_error"


def test_bare_pydantic_detail_array():
    raw = b'{"detail":[{"type":"missing","loc":["body","messages"],"msg":"Field required"}]}'
    err = normalize_error(422, raw)["error"]
    assert err["message"] == "messages: Field required"
    assert err["param"] == "messages"


def test_bare_detail_string():
    err = normalize_error(401, b'{"detail":"Invalid API Key"}')["error"]
    assert err["message"] == "Invalid API Key"
    assert err["type"] == "authentication_error"


def test_non_json_error_body():
    err = normalize_error(502, b"<html>bad gateway</html>")["error"]
    assert "bad gateway" in err["message"]
    assert err["type"] == "api_error"


def test_already_openai_shaped_passes_through():
    err = normalize_error(400, {"error": {"message": "boom", "type": "x"}})["error"]
    assert err["message"] == "boom" and err["param"] is None


# ---------- 响应清理 ----------

def test_response_drops_null_tool_calls_and_p():
    body = normalize_chat_response({
        "p": "abcdef", "choices": [{"message": {"role": "assistant", "content": "hi",
                                                "tool_calls": None}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}})
    assert "p" not in body
    assert "tool_calls" not in body["choices"][0]["message"]
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["index"] == 0


def test_response_nulls_content_when_tool_calls_present():
    """OpenAI 在有 tool_calls 时 content 是 null，上游给的是空串。"""
    body = normalize_chat_response({"choices": [{"message": {
        "content": "", "tool_calls": [{"id": "1", "index": 0,
                                       "function": {"name": "f", "arguments": "{}"}}]}}]})
    msg = body["choices"][0]["message"]
    assert msg["content"] is None
    assert "index" not in msg["tool_calls"][0]
    assert msg["tool_calls"][0]["type"] == "function"


def test_usage_gets_reasoning_tokens_and_loses_nulls():
    body = normalize_chat_response({"choices": [], "usage": {
        "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
        "prompt_audio_seconds": None, "request_count": None, "prompt_token_details": None}},
        reasoning_tokens=7)
    usage = body["usage"]
    assert usage["completion_tokens_details"]["reasoning_tokens"] == 7
    for noise in ("prompt_audio_seconds", "request_count", "prompt_token_details"):
        assert noise not in usage


def test_stream_event_cleanup():
    event = normalize_stream_event({"p": "abc", "choices": [
        {"delta": {"content": "x", "tool_calls": None}}]})
    assert "p" not in event
    assert "tool_calls" not in event["choices"][0]["delta"]
    assert event["object"] == "chat.completion.chunk"


# ---------- embeddings ----------

def test_embeddings_input_forms():
    body, _ = normalize_embeddings_request({"model": "e", "input": "hi"})
    assert body["input"] == ["hi"]
    body, _ = normalize_embeddings_request({"model": "e", "input": ["a", "b"]})
    assert body["input"] == ["a", "b"]


def test_embeddings_token_ids_rejected_with_clear_message():
    with pytest.raises(RequestError, match="token-id"):
        normalize_embeddings_request({"model": "e", "input": [[1, 2, 3]]})


def test_embeddings_base64_encoding_done_locally():
    """上游接受 encoding_format 但忽略它，只会返回 float 数组，所以本层自己编。"""
    body = normalize_embeddings_response(
        {"data": [{"embedding": [1.0, 2.0]}], "usage": {"prompt_tokens": 1}}, "base64")
    raw = base64.b64decode(body["data"][0]["embedding"])
    assert struct.unpack("<2f", raw) == (1.0, 2.0)


def test_embeddings_dimensions_truncated_locally():
    """上游对 dimensions 直接 422，本层接受并在返回后截断。"""
    body = normalize_embeddings_response(
        {"data": [{"embedding": [1.0, 2.0, 3.0, 4.0]}], "usage": {}}, "float", 2)
    assert body["data"][0]["embedding"] == [1.0, 2.0]


def test_embeddings_dimensions_validated():
    with pytest.raises(RequestError):
        normalize_embeddings_request({"model": "e", "input": "x", "dimensions": 0})


# ---------- moderations ----------

def test_moderations_gets_flagged_and_openai_aliases():
    body = normalize_moderations_response({"results": [{
        "categories": {"violence_and_threats": True, "sexual": False},
        "category_scores": {"violence_and_threats": 0.95, "sexual": 0.01}}],
        "usage": {"prompt_tokens": 5}})
    result = body["results"][0]
    assert result["flagged"] is True
    assert result["categories"]["violence"] is True      # OpenAI 名
    assert result["categories"]["violence_and_threats"] is True  # 原生名保留
    assert result["category_scores"]["violence"] == 0.95


def test_moderations_not_flagged_when_all_false():
    body = normalize_moderations_response({"results": [{
        "categories": {"sexual": False}, "category_scores": {"sexual": 0.01}}]})
    assert body["results"][0]["flagged"] is False
