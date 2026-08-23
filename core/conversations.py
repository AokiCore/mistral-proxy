# -*- coding: utf-8 -*-
"""GLM-5.2 等 Z.ai 模型只能通过 Mistral 的 Conversations API（/v1/conversations）
调用，走标准 /v1/chat/completions 会一直返回 429。

本模块负责两件事：
  1. 判定一个模型是否只能走 conversations 端点；
  2. 在 chat/completions 与 conversations 两种格式之间互转，让下游客户端照常
     用 OpenAI 的 messages/temperature/max_tokens 写法调用 GLM，代理在底层偷偷
     换成 inputs/completion_args 发给上游，再把响应转回 chat completions 形态。

Conversations API 端点（完整列表）：
  POST /v1/conversations                           创建对话（非流式）
  POST /v1/conversations (stream=true)             创建对话（流式）
  POST /v1/conversations/{conversation_id}          追加消息（非流式）
  POST /v1/conversations/{conversation_id} (stream) 追加消息（流式）
  GET  /v1/conversations                            列出所有对话
  GET  /v1/conversations/{conversation_id}          获取对话信息
  DELETE /v1/conversations/{conversation_id}        删除对话
  GET  /v1/conversations/{conversation_id}/history  获取所有条目
  POST /v1/conversations/{conversation_id}/restart  从指定条目重启对话

Conversations 请求体：
  model (string|null)
  inputs (string | array[Entry])  — Entry 类型：
    MessageInputEntry  {role:"user"|"assistant", content:string|array[Chunk]}
    MessageOutputEntry {role:string, content:string|array[Chunk]}
    FunctionResultEntry, FunctionCallEntry, ToolExecutionEntry, AgentHandoffEntry
  completion_args (CompletionArgs) — 白名单参数：
    temperature, max_tokens, top_p, stop, random_seed,
    presence_penalty, frequency_penalty,
    reasoning_effort (enum: none|minimal|low|medium|high|xhigh),
    response_format, tool_choice (enum: auto|none|any|required), prediction
  instructions (string|null)  — 系统指令
  tools (array[Tool]|null)    — FunctionTool 等
  agent_id, agent_version (string|null)
  stream (boolean)
  guardrails (array[GuardrailConfig]|null)  — {block_on_error: boolean}

Conversations 非流式响应：
  {object:"conversation.response", conversation_id,
   outputs:[{type:"message.output", role:"assistant",
             content:string|array[Chunk], id:"msg_..."}],
   usage:{prompt_tokens, completion_tokens, total_tokens},
   guardrails}

  Chunk 类型（content 数组元素）：
    TextChunk           {type:"text", text:string}
    ThinkChunk          {type:"thinking", thinking:array[Chunk], closed:boolean, signature}
    ImageURLChunk       {type:"image_url", image_url:{url, detail}}
    ToolFileChunk       {type:"tool_file", tool:enum, file_id}
    DocumentURLChunk    {type:"document_url", document_url, document_name}
    ToolReferenceChunk  {type:"tool_reference", tool:enum, title, description}

Conversations 流式事件（ConversationEvents 枚举）：
  conversation.response.started  -> ResponseStartedEvent {conversation_id}
  conversation.response.done     -> ResponseDoneEvent {usage, created_at}
  conversation.response.error    -> ResponseErrorEvent {code, message}
  message.output.delta           -> MessageOutputEvent {id, content:string|Chunk}
  tool.execution.started/delta/done -> ToolExecution{Started/Delta/Done}Event
  agent.handoff.started/done     -> AgentHandoff{Started/Done}Event
  function.call.delta            -> FunctionCallDeltaEvent
"""
import json
import time

from core.openai_compat import _extract_effort

SYSTEM_FINGERPRINT = "fp_mistralpool"

# 已知只能走 conversations 端点的模型 id 前缀。上游同时暴露 glm-5-2 与
# zai-glm-5-2 两个别名，都指向同一个 Z.ai 模型。
CONV_MODEL_PREFIXES = ("glm-", "zai-glm-")


def needs_conversations(model_id: str) -> bool:
    """该模型是否必须走 /v1/conversations 而非 /chat/completions。"""
    if not model_id:
        return False
    name = model_id.strip().lower()
    return any(name.startswith(p) for p in CONV_MODEL_PREFIXES)


def chat_to_conversations(body: dict) -> tuple[dict, bool]:
    """把 chat/completions 请求体转成 conversations 请求体。

    返回 (conversations 请求体, stream)。messages 拆成 inputs + instructions：
      - system 消息合并进 instructions；
      - user/assistant 消息变成 MessageInputEntry {type:"message.input", role, content}；
      - assistant 消息带 tool_calls 时，每个 tool_call 变成 FunctionCallEntry
        {type:"function.call", name, arguments, tool_call_id}；
      - tool 消息变成 FunctionResultEntry {type:"function.result", result, tool_call_id}。

    tools 透传：OpenAI 的 {type:"function", function:{name,description,parameters}}
    与 Conversations API 的 FunctionTool 结构一致，原样保留。
    """
    messages = body.get("messages") or []
    stream = bool(body.get("stream"))

    instructions_parts: list[str] = []
    inputs: list[dict] = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role") or "user"
        content = msg.get("content")
        # content 可能是字符串或分段数组；conversations 只认字符串
        text = _content_to_text(content)
        if role == "system":
            if text:
                instructions_parts.append(text)
            continue

        # assistant 带 tool_calls：先发文本（若有），再逐个 tool_call → FunctionCallEntry
        if role == "assistant" and msg.get("tool_calls"):
            if text:
                inputs.append(_entry("message.input", role=role, content=text))
            for tc in msg["tool_calls"]:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                inputs.append(_entry("function.call",
                                     tool_call_id=tc.get("id") or "",
                                     name=fn.get("name") or "",
                                     arguments=fn.get("arguments") or ""))
            continue

        # tool 角色消息 → FunctionResultEntry
        if role == "tool":
            inputs.append(_entry("function.result",
                                 tool_call_id=msg.get("tool_call_id") or "",
                                 result=text))
            continue

        # 普通 user/assistant 消息
        inputs.append(_entry("message.input", role=role, content=text))

    # CompletionArgs 支持的参数（与上游 schema 对齐）：
    # temperature, max_tokens, top_p, stop, random_seed, presence_penalty,
    # frequency_penalty, reasoning_effort, response_format, tool_choice, prediction
    completion_args: dict = {}
    for key in ("temperature", "max_tokens", "top_p", "stop", "random_seed",
                "presence_penalty", "frequency_penalty",
                "response_format", "tool_choice", "prediction"):
        val = body.get(key)
        if val is not None:
            completion_args[key] = val

    # reasoning_effort：从 OpenAI/OpenRouter/DeepSeek/Qwen 四种写法里提取
    # Conversations API 实际只接受 none/high 两档（文档写了 6 档但上游 422），
    # 与 normalize_chat_request 里的 _extract_effort 同源逻辑。
    effort = _extract_effort(body)
    if effort is not None:
        completion_args["reasoning_effort"] = effort

    out: dict = {
        "model": body.get("model"),
        "inputs": inputs,
        "completion_args": completion_args,
        "instructions": "\n\n".join(instructions_parts),
    }
    # tools：OpenAI 的 function tool 格式 {type:"function", function:{...}} 与
    # Conversations API 的 FunctionTool 结构一致，原样透传。
    # 非 function 类型的 tool（code_interpreter 等）也原样保留。
    raw_tools = body.get("tools")
    if isinstance(raw_tools, list) and raw_tools:
        out["tools"] = raw_tools
    if stream:
        out["stream"] = True
    return out, stream


def conversations_response_to_chat(parsed: dict, requested_model: str,
                                   reasoning_tokens: int = 0) -> dict:
    """把非流式 conversations 响应转回 chat.completion 形态。

    outputs 里可能出现的 entry 类型：
      - message.output: assistant 的文本/思考回复
        content 可能是纯字符串，也可能是分段数组（reasoning_effort=high 时）：
          [{"type":"thinking","thinking":[{"type":"text","text":"..."}],"closed":true},
           {"type":"text","text":"..."}]
        thinking 段提取为 reasoning_content，text 段拼成 content。
      - function.call: 函数调用，转成 OpenAI 的 tool_calls
        {name, arguments, tool_call_id}
    """
    if not isinstance(parsed, dict):
        return parsed

    content = ""
    reasoning_content = ""
    msg_id = ""
    tool_calls: list[dict] = []
    for entry in parsed.get("outputs") or []:
        if not isinstance(entry, dict):
            continue
        etype = entry.get("type")
        if etype == "message.output":
            raw_content = entry.get("content")
            msg_id = entry.get("id") or ""
            if isinstance(raw_content, str):
                content = raw_content
            elif isinstance(raw_content, list):
                text_parts: list[str] = []
                think_parts: list[str] = []
                for part in raw_content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type")
                    if ptype == "text":
                        text_parts.append(part.get("text") or "")
                    elif ptype == "thinking":
                        inner = part.get("thinking")
                        if isinstance(inner, list):
                            for t in inner:
                                if isinstance(t, dict):
                                    think_parts.append(t.get("text") or "")
                        elif isinstance(inner, str):
                            think_parts.append(inner)
                content = "".join(text_parts)
                reasoning_content = "\n".join(think_parts)
        elif etype == "function.call":
            tool_calls.append({
                "id": entry.get("tool_call_id") or "",
                "type": "function",
                "function": {
                    "name": entry.get("name") or "",
                    "arguments": entry.get("arguments") or "",
                },
            })

    usage = parsed.get("usage") or {}
    completion_tokens = int(usage.get("completion_tokens") or 0)

    message: dict = {"role": "assistant", "content": content}
    if reasoning_content:
        message["reasoning_content"] = reasoning_content
    if tool_calls:
        message["tool_calls"] = tool_calls

    finish_reason = "tool_calls" if tool_calls else "stop"

    return {
        "id": parsed.get("conversation_id") or msg_id or f"chatcmpl-{int(time.time())}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": requested_model,
        "system_fingerprint": SYSTEM_FINGERPRINT,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
            "logprobs": None,
        }],
        "usage": _clean_usage(usage, reasoning_tokens),
    }


def conversations_stream_events(lease, requested_model: str):
    """生成器：把上游 conversations 的 SSE 事件流转成 chat.completion.chunk 流。

    上游事件（ConversationEvents 枚举）：
      conversation.response.started -> ResponseStartedEvent: 发首个空 delta 块（带 id/model）
      message.output.delta          -> MessageOutputEvent: content 增量
      function.call.delta           -> FunctionCallEvent: {id, name, arguments, tool_call_id}
                                       转成 OpenAI 的 tool_calls delta
      conversation.response.done    -> ResponseDoneEvent: usage；收尾
      conversation.response.error   -> ResponseErrorEvent: {code, message}
      tool.execution.*              -> 忽略（code_interpreter/web_search 等工具调用）
      agent.handoff.*              -> 忽略（多 agent 交接）

    message.output.delta 的 content 可能是：
      - 字符串（文本增量）-> 转成 content delta
      - {"type":"thinking","thinking":[{"type":"text","text":"..."}],...}（思考增量）
        -> 转成 reasoning_content delta
      - 其他 chunk 类型（TextChunk/ImageURLChunk 等）-> 忽略
    """
    import json as _json

    conv_id = ""
    msg_id = ""
    started = False
    usage = None
    error_msg = ""
    has_tool_calls = False
    # OpenAI 语义下，同一次函数调用的所有参数分片必须共用同一个 index，
    # 客户端按 index 聚合并拼接 arguments。上游的每个 function.call.delta 都带
    # 完整的 id/name/tool_call_id，所以这里要按 tool_call_id 去重：只有第一次
    # 见到的调用才递增 index 并携带 id/name，后续分片只带增量参数。
    tool_call_index = 0
    seen_calls: dict[str, int] = {}

    def base_chunk(delta: dict) -> dict:
        return {
            "id": conv_id or msg_id or f"chatcmpl-{int(time.time())}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": requested_model,
            "system_fingerprint": SYSTEM_FINGERPRINT,
            "choices": [{"index": 0, "delta": delta, "finish_reason": None,
                         "logprobs": None}],
        }

    async def _gen():
        nonlocal conv_id, msg_id, started, usage, error_msg
        nonlocal has_tool_calls, tool_call_index
        try:
            async for line in lease.response.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                done_marker = chr(91) + "DONE" + chr(93)
                if data == done_marker:
                    break
                try:
                    event = _json.loads(data)
                except _json.JSONDecodeError:
                    continue

                etype = event.get("type")
                if etype == "conversation.response.started":
                    conv_id = event.get("conversation_id") or conv_id
                    if not started:
                        started = True
                        yield _sse(base_chunk({"role": "assistant", "content": ""}))
                elif etype == "message.output.delta":
                    msg_id = event.get("id") or msg_id
                    content = event.get("content")
                    if isinstance(content, str):
                        if content:
                            yield _sse(base_chunk({"content": content}))
                    elif isinstance(content, dict) and content.get("type") == "thinking":
                        # 思考增量：提取文本，作为 reasoning_content 发出
                        thinking = content.get("thinking")
                        texts = []
                        if isinstance(thinking, list):
                            for t in thinking:
                                if isinstance(t, dict):
                                    texts.append(t.get("text") or "")
                        elif isinstance(thinking, str):
                            texts.append(thinking)
                        text = "".join(texts)
                        if text:
                            yield _sse(base_chunk({"reasoning_content": text}))
                elif etype == "function.call.delta":
                    # FunctionCallEvent: {id, name, arguments, tool_call_id, output_index}
                    # 转成 OpenAI 的 tool_calls delta。同一个调用（按 tool_call_id
                    # 识别）的后续分片复用首个分片分配的 index，只带增量 arguments；
                    # 客户端按 index 聚合并拼接出完整参数。
                    has_tool_calls = True
                    tc_id = event.get("tool_call_id") or event.get("id") or ""
                    name = event.get("name") or ""
                    arguments = event.get("arguments") or ""

                    if tc_id and tc_id in seen_calls:
                        fragment: dict = {"index": seen_calls[tc_id]}
                        if arguments:
                            fragment["function"] = {"arguments": arguments}
                            yield _sse(base_chunk({"tool_calls": [fragment]}))
                        continue

                    index = tool_call_index
                    tool_call_index += 1
                    seen_calls[tc_id or f"\x00anon{index}"] = index
                    fn: dict = {"arguments": arguments}
                    if name:
                        fn["name"] = name
                    yield _sse(base_chunk({"tool_calls": [{
                        "index": index,
                        "id": tc_id or f"call_{index}",
                        "type": "function",
                        "function": fn,
                    }]}))
                elif etype == "conversation.response.done":
                    usage = event.get("usage") or {}
                elif etype == "conversation.response.error":
                    # 上游在流中报错：{code, message}
                    # 不发独立错误块（chat.completion.chunk 没有 error 字段），
                    # 只记录消息，收尾块照常用 stop。客户端会看到内容截断。
                    error_msg = event.get("message") or "upstream conversation error"

            # 收尾块
            finish_reason = "tool_calls" if has_tool_calls else "stop"
            yield _sse({
                "id": conv_id or msg_id or f"chatcmpl-{int(time.time())}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": requested_model,
                "system_fingerprint": SYSTEM_FINGERPRINT,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason,
                             "logprobs": None}],
                "usage": _clean_usage(usage or {}, 0),
            })
            yield b"data: " + done_marker.encode() + b"\n\n"
        finally:
            await lease.aclose()

    return _gen()



def _content_to_text(content) -> str:
    """把 chat 消息的 content（字符串或分段数组）压成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                parts.append(part.get("text") or "")
            elif isinstance(part, str):
                parts.append(part)
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def _entry(entry_type: str, **fields) -> dict:
    """构造一条 conversations 输入 entry。

    上游的 inputs 是按 type 判别的联合类型（MessageInputEntry / FunctionCallEntry /
    FunctionResultEntry），SDK 序列化时总是带 object:"entry" 和 type 字段。
    不带 type 的话上游按判别联合校验会直接失败，多轮对话第二轮就 422。
    """
    return {"object": "entry", "type": entry_type, **fields}


def _clean_usage(usage: dict, reasoning_tokens: int) -> dict:
    from core.openai_compat import clean_usage
    return clean_usage(usage, reasoning_tokens)


def _sse(event: dict) -> bytes:
    return b"data: " + json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n\n"
