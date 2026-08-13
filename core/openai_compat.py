# -*- coding: utf-8 -*-
"""OpenAI 协议兼容层：入站请求归一化 + 出站响应/错误标准化。

这一层存在的理由来自对上游的实测 (.private/probe_out.json)：Mistral 用 pydantic 的
extra_forbidden 校验请求体，任何它不认识的键都会直接 422。而下面这些恰恰是 OpenAI SDK
会默认发出的标准字段：

    logit_bias / seed / user / store / max_completion_tokens   -> 422 extra_forbidden
    logprobs / top_logprobs                                    -> 400 not enabled
    assistant 消息里的 reasoning_content                        -> 422 extra_forbidden
    embeddings 的 dimensions                                    -> 422 extra_forbidden
    reasoning_effort 只接受 none/high, 不认 OpenAI 的 low/medium

其中 reasoning_content 最要命：DeepSeek 风格的客户端在多轮对话里会把上一轮的思考原样回传，
不清理的话第二轮必然 422。

错误方向同理，上游会吐三种互不相同的结构，而 OpenAI 客户端只认 {"error": {...}}：
    {"object":"error","message":"...","type":"...","code":"..."}
    {"object":"error","message":{"detail":[...]},...}     <- message 是对象
    {"detail":[{pydantic 校验错误}]} / {"detail":"Invalid API Key"}
"""
import json
import re
import time

# 上游会 422/400 拒绝、必须丢弃的 OpenAI 标准参数
DROP_PARAMS = frozenset({
    "logit_bias", "seed", "user", "store", "logprobs", "top_logprobs",
    "frequency_penalty_range", "web_search_options", "audio", "reasoning_details",
})

# 需要改名的参数
RENAME_PARAMS = {"max_completion_tokens": "max_tokens"}

# 上游实测接受的参数(除 model/messages/stream 外)
PASS_PARAMS = frozenset({
    "temperature", "top_p", "max_tokens", "stream_options", "stop", "random_seed",
    "response_format", "tools", "tool_choice", "presence_penalty", "frequency_penalty",
    "n", "prediction", "parallel_tool_calls", "prompt_mode", "safe_prompt",
    "reasoning_effort", "metadata", "modalities", "service_tier", "document_image_limit",
    "document_page_limit", "include_image_base64",
})

# 消息体只保留上游认识的键(白名单)，其余一律丢弃，否则 extra_forbidden
MESSAGE_KEYS = frozenset({"role", "content", "tool_calls", "tool_call_id", "prefix"})
TOOL_MESSAGE_KEYS = MESSAGE_KEYS | {"name"}

VALID_ROLES = frozenset({"system", "user", "assistant", "tool", "developer"})

# 上游对分段 content 是严格 schema（extra_forbidden），客户端为别家模型附带的
# 这些字段会让整条请求 422，必须先摘掉。
PART_EXTRA_KEYS = frozenset({"cache_control", "annotations", "providerOptions",
                             "experimental_providerMetadata", "metadata"})

# 各家对分段类型的叫法不一，统一映射到上游认的 tag。
PART_TYPE_ALIASES = {"input_text": "text", "output_text": "text",
                     "input_image": "image_url", "image": "image_url"}

# 上游只让 user 角色带图片等非文本分段，其余角色一律拍平成字符串。
TEXT_ONLY_ROLES = frozenset({"system", "assistant", "tool"})

# 客户端回传上一轮思考的三种写法，都要认出来
THINK_TAG_RE = re.compile(r"<think>(.*?)</think>\s*", re.S | re.I)

# 改写后用的包裹标记。实测 glm-5-2 会在分词前剥掉 <think>...</think>，
# 用这个纯文本标记则三个模型都能完整进上下文。
PASSBACK_OPEN = "[prior reasoning]"
PASSBACK_CLOSE = "[end prior reasoning]"

# OpenAI 的 reasoning_effort 取值 -> 上游只认 none/high
EFFORT_MAP = {
    "none": "none", "minimal": "none", "low": "none",
    "medium": "high", "high": "high", "max": "high", "xhigh": "high",
}

SYSTEM_FINGERPRINT = "fp_mistralpool"


class RequestError(Exception):
    """入站请求本身不合法，不必转发上游。"""

    def __init__(self, message: str, err_type: str = "invalid_request_error",
                 param: str | None = None, code: str | None = None, status: int = 400):
        super().__init__(message)
        self.message = message
        self.type = err_type
        self.param = param
        self.code = code
        self.status = status


def error_envelope(message: str, err_type: str = "api_error",
                   param: str | None = None, code: str | None = None) -> dict:
    return {"error": {"message": message, "type": err_type, "param": param, "code": code}}


def type_for_status(status: int) -> str:
    """HTTP 状态码 -> OpenAI 错误 type。"""
    if status == 401:
        return "authentication_error"
    if status == 403:
        return "permission_error"
    if status == 404:
        return "not_found_error"
    if status == 429:
        return "rate_limit_error"
    if 400 <= status < 500:
        return "invalid_request_error"
    return "api_error"


def _flatten_pydantic(detail: list) -> tuple[str, str | None]:
    """把 pydantic 校验错误数组压成一句人话 + 出错字段名。"""
    parts, param = [], None
    for item in detail:
        if not isinstance(item, dict):
            parts.append(str(item))
            continue
        loc = [str(x) for x in (item.get("loc") or []) if x != "body"]
        field = ".".join(loc)
        param = param or (field or None)
        msg = item.get("msg") or item.get("type") or "invalid"
        parts.append(f"{field}: {msg}" if field else str(msg))
    return "; ".join(parts) or "invalid request", param


def normalize_error(status: int, body: bytes | str | dict) -> dict:
    """把上游任意形态的错误体转成 OpenAI 的 {"error": {...}} 包络。"""
    data = body
    if isinstance(body, (bytes, bytearray)):
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            data = body.decode("utf-8", "replace")
    elif isinstance(body, str):
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            pass

    if not isinstance(data, dict):
        return error_envelope(str(data)[:600] or f"upstream returned {status}",
                              type_for_status(status))

    # 已经是 OpenAI 形态就原样用
    if isinstance(data.get("error"), dict):
        err = dict(data["error"])
        err.setdefault("type", type_for_status(status))
        err.setdefault("param", None)
        err.setdefault("code", None)
        if not isinstance(err.get("message"), str):
            err["message"] = json.dumps(err.get("message"), ensure_ascii=False)
        return {"error": err}

    param = data.get("param")
    code = data.get("code")
    err_type = data.get("type") or type_for_status(status)
    message = data.get("message")

    # message 可能是 {"detail": [...]} 这种嵌套形态
    if isinstance(message, dict) and "detail" in message:
        message = message["detail"]
    if message is None and "detail" in data:
        message = data["detail"]

    if isinstance(message, list):
        message, detail_param = _flatten_pydantic(message)
        param = param or detail_param
        err_type = "invalid_request_error"
    elif isinstance(message, dict):
        message = json.dumps(message, ensure_ascii=False)
    elif message is None:
        message = f"upstream returned {status}"

    return error_envelope(str(message), err_type, param,
                          str(code) if code is not None else None)


# ---------------- 请求归一化 ----------------

def _extract_effort(payload: dict) -> str | None:
    """从 OpenAI / OpenRouter / DeepSeek / Qwen 四种写法里提取思考强度。"""
    raw = payload.get("reasoning_effort")

    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        raw = reasoning.get("effort", raw)
        if reasoning.get("enabled") is False or reasoning.get("exclude") is True:
            raw = raw or "high"
        if reasoning.get("max_tokens"):
            raw = raw or "high"

    thinking = payload.get("thinking")
    if isinstance(thinking, dict):
        kind = thinking.get("type")
        if kind == "enabled":
            raw = raw or "high"
        elif kind == "disabled":
            raw = "none"
        if thinking.get("budget_tokens"):
            raw = raw or "high"
    elif isinstance(thinking, bool):
        raw = "high" if thinking else "none"

    if "enable_thinking" in payload:
        raw = "high" if payload["enable_thinking"] else "none"

    if raw is None:
        return None
    return EFFORT_MAP.get(str(raw).lower(), "high")


def normalize_content_part(part):
    """把一段 content 整理成上游认识的形状；实在认不出来返回 None 由调用方丢弃。"""
    if isinstance(part, str):
        return {"type": "text", "text": part}
    if not isinstance(part, dict):
        return None

    kind = PART_TYPE_ALIASES.get(part.get("type"), part.get("type"))
    if kind is None:
        # 少数客户端不写 type，按存在的键推断
        if "text" in part:
            kind = "text"
        elif "image_url" in part:
            kind = "image_url"
        else:
            return None

    if kind == "text":
        text = part.get("text")
        return {"type": "text", "text": text if isinstance(text, str) else ""}
    if kind == "image_url":
        return {"type": "image_url", "image_url": part.get("image_url")}
    # 其余类型（音频、文档等）原样透传，只摘掉会触发 extra_forbidden 的字段
    clean = {k: v for k, v in part.items() if k not in PART_EXTRA_KEYS}
    clean["type"] = kind
    return clean


def normalize_content(content, role: str):
    """分段 content 逐段清洗；文本角色拍平成字符串。"""
    if not isinstance(content, list):
        return content
    parts = [p for p in (normalize_content_part(x) for x in content) if p is not None]
    if role in TEXT_ONLY_ROLES:
        return "\n".join(p["text"] for p in parts if p.get("type") == "text")
    return parts


def _thinking_text(part) -> str:
    """从一个 ThinkChunk 里抠出纯文本。"""
    if not isinstance(part, dict) or part.get("type") != "thinking":
        return ""
    inner = part.get("thinking")
    if isinstance(inner, str):
        return inner
    if not isinstance(inner, list):
        return ""
    return "".join(x.get("text") or "" for x in inner
                   if isinstance(x, dict) and isinstance(x.get("text"), str))


def extract_reasoning(msg: dict) -> tuple[str, object]:
    """把客户端回传的上一轮思考挑出来，返回 (思考文本, 剥掉思考后的 content)。

    三种写法都要认：分段里的 ThinkChunk、DeepSeek 风格的 reasoning_content 字段、
    正文里内联的 <think>...</think>（本网关 think_tags 模式自己发出去的那种）。
    """
    chunks, content = [], msg.get("content")

    raw = msg.get("reasoning_content")
    if isinstance(raw, str) and raw.strip():
        chunks.append(raw.strip())

    if isinstance(content, list):
        kept = []
        for part in content:
            text = _thinking_text(part)
            if text.strip():
                chunks.append(text.strip())
            else:
                kept.append(part)
        content = kept
    elif isinstance(content, str) and "<think" in content.lower():
        def take(m):
            chunks.append(m.group(1).strip())
            return ""
        content = THINK_TAG_RE.sub(take, content)

    return "\n".join(c for c in chunks if c), content


def apply_passback(msg: dict, content, mode: str):
    """按 reasoning_passback 决定怎么安置回传的思考。"""
    if mode == "native":
        return content
    reasoning, stripped = extract_reasoning(msg)
    if not reasoning or mode == "off":
        return stripped
    if isinstance(stripped, str):
        body = stripped
    elif isinstance(stripped, list):
        body = "\n".join(p.get("text") or "" for p in stripped
                         if isinstance(p, dict) and p.get("type") == "text")
    else:
        body = ""       # 只带 tool_calls 的助手消息，content 是 None
    return f"{PASSBACK_OPEN}\n{reasoning}\n{PASSBACK_CLOSE}\n{body}".strip()


def sanitize_messages(messages, passback: str = "text") -> list[dict]:
    if not isinstance(messages, list) or not messages:
        raise RequestError("'messages' must be a non-empty array", param="messages")
    out = []
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise RequestError(f"messages[{i}] must be an object", param=f"messages[{i}]")
        role = msg.get("role")
        if role not in VALID_ROLES:
            raise RequestError(
                f"messages[{i}].role must be one of {sorted(VALID_ROLES)}, got {role!r}",
                param=f"messages[{i}].role")
        # OpenAI 的 developer 角色上游不认，等价降级为 system
        if role == "developer":
            role = "system"
        allowed = TOOL_MESSAGE_KEYS if role == "tool" else MESSAGE_KEYS
        clean = {k: v for k, v in msg.items() if k in allowed}
        clean["role"] = role
        # 只有 assistant 会带上一轮的思考；其余角色没有这回事
        if role == "assistant":
            clean["content"] = apply_passback(msg, clean.get("content"), passback)
        if "content" in clean:
            clean["content"] = normalize_content(clean["content"], role)
        if clean.get("content") is None and not clean.get("tool_calls"):
            clean["content"] = ""
        out.append(clean)
    return out


def normalize_chat_request(payload: dict, supports_reasoning: bool = True,
                           passback: str = "text") -> tuple[dict, dict]:
    """把任意 OpenAI 风格的 chat 请求整理成上游能接受的形态。

    返回 (转发给上游的 body, 本地元信息)。元信息里带 stream / exclude_reasoning 等本层决策。
    """
    if not isinstance(payload, dict):
        raise RequestError("request body must be a JSON object")

    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise RequestError("'model' is required", param="model")

    body: dict = {"model": model,
                  "messages": sanitize_messages(payload.get("messages"), passback)}

    stream = bool(payload.get("stream"))
    if stream:
        body["stream"] = True

    for key, value in payload.items():
        if key in ("model", "messages", "stream", "reasoning", "thinking",
                   "enable_thinking", "reasoning_effort"):
            continue
        if key in DROP_PARAMS:
            continue
        target = RENAME_PARAMS.get(key, key)
        if target in PASS_PARAMS and value is not None:
            body[target] = value

    if isinstance(body.get("stop"), str):
        body["stop"] = [body["stop"]]

    effort = _extract_effort(payload)
    if effort is not None and supports_reasoning:
        body["reasoning_effort"] = effort
    elif effort is not None:
        body.pop("reasoning_effort", None)

    reasoning = payload.get("reasoning")
    exclude = bool(isinstance(reasoning, dict) and reasoning.get("exclude"))
    stream_options = payload.get("stream_options")
    include_usage = bool(isinstance(stream_options, dict)
                         and stream_options.get("include_usage"))

    meta = {"stream": stream, "exclude_reasoning": exclude, "effort": effort,
            "requested_model": model, "include_usage": include_usage}
    return body, meta


def normalize_embeddings_request(payload: dict) -> tuple[dict, dict]:
    if not isinstance(payload, dict):
        raise RequestError("request body must be a JSON object")
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        raise RequestError("'model' is required", param="model")

    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        items = [raw_input]
    elif isinstance(raw_input, list):
        if not raw_input:
            raise RequestError("'input' must not be empty", param="input")
        if not all(isinstance(x, str) for x in raw_input):
            raise RequestError(
                "'input' must be a string or array of strings "
                "(token-id arrays are not supported upstream)", param="input")
        items = raw_input
    else:
        raise RequestError("'input' is required", param="input")

    encoding_format = payload.get("encoding_format") or "float"
    if encoding_format not in ("float", "base64"):
        raise RequestError("'encoding_format' must be 'float' or 'base64'",
                           param="encoding_format")

    # dimensions 上游会 422，本层接受但只能在返回后截断
    dimensions = payload.get("dimensions")
    if dimensions is not None and (not isinstance(dimensions, int) or dimensions < 1):
        raise RequestError("'dimensions' must be a positive integer", param="dimensions")

    return ({"model": model, "input": items},
            {"encoding_format": encoding_format, "dimensions": dimensions})


# ---------------- 响应标准化 ----------------

def clean_usage(usage, reasoning_tokens: int = 0) -> dict:
    """去掉上游 usage 里的 null 噪声字段, 补上 OpenAI 的 completion_tokens_details。

    reasoning_tokens 是本层的估算值, 上游若真带了就尊重上游 (setdefault)。
    流式路径的 usage 块也走这里, 保证两条路径行为一致。
    """
    if not isinstance(usage, dict):
        usage = {}
    out = {k: v for k, v in usage.items()
           if v is not None and k not in ("prompt_token_details", "request_count",
                                          "prompt_audio_seconds")}
    out.setdefault("prompt_tokens", 0)
    out.setdefault("completion_tokens", 0)
    out.setdefault("total_tokens", out["prompt_tokens"] + out["completion_tokens"])
    details = dict(out.get("completion_tokens_details") or {})
    details.setdefault("reasoning_tokens", reasoning_tokens)
    out["completion_tokens_details"] = details
    return out


def normalize_chat_response(body: dict, reasoning_tokens: int = 0) -> dict:
    """整理非流式 chat 响应，使其符合 OpenAI 客户端的预期。"""
    if not isinstance(body, dict):
        return body
    body.pop("p", None)
    body.setdefault("object", "chat.completion")
    body.setdefault("created", int(time.time()))
    body.setdefault("system_fingerprint", SYSTEM_FINGERPRINT)

    for choice in body.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        choice.setdefault("index", 0)
        choice.setdefault("finish_reason", "stop")
        choice.setdefault("logprobs", None)
        msg = choice.get("message")
        if not isinstance(msg, dict):
            continue
        msg.pop("index", None)
        if msg.get("tool_calls") is None:
            msg.pop("tool_calls", None)
        else:
            for call in msg["tool_calls"]:
                if isinstance(call, dict):
                    call.pop("index", None)
                    call.setdefault("type", "function")
            # OpenAI 在有 tool_calls 时 content 为 null
            if msg.get("content") == "":
                msg["content"] = None
        msg.setdefault("role", "assistant")

    body["usage"] = clean_usage(body.get("usage"), reasoning_tokens)
    return body


def normalize_stream_event(event: dict) -> dict:
    """整理一条流式事件。"""
    if not isinstance(event, dict):
        return event
    event.pop("p", None)
    event.setdefault("object", "chat.completion.chunk")
    event.setdefault("system_fingerprint", SYSTEM_FINGERPRINT)
    for choice in event.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        choice.setdefault("index", 0)
        delta = choice.get("delta")
        if isinstance(delta, dict):
            delta.pop("index", None)
            if delta.get("tool_calls") is None:
                delta.pop("tool_calls", None)
    return event


def normalize_embeddings_response(body: dict, encoding_format: str = "float",
                                  dimensions: int | None = None) -> dict:
    """上游忽略 encoding_format 且不支持 dimensions, 这两个语义在本层补齐。"""
    import base64
    import struct

    if not isinstance(body, dict):
        return body
    body.setdefault("object", "list")
    for i, item in enumerate(body.get("data") or []):
        if not isinstance(item, dict):
            continue
        item.setdefault("object", "embedding")
        item.setdefault("index", i)
        vec = item.get("embedding")
        if not isinstance(vec, list):
            continue
        if dimensions and dimensions < len(vec):
            vec = vec[:dimensions]
            item["embedding"] = vec
        if encoding_format == "base64":
            item["embedding"] = base64.b64encode(
                struct.pack(f"<{len(vec)}f", *vec)).decode("ascii")
    body["usage"] = {k: v for k, v in (clean_usage(body.get("usage"))).items()
                     if k in ("prompt_tokens", "total_tokens")}
    return body


# OpenAI 的审核分类 -> Mistral 的分类。Mistral 没有 flagged 字段，本层按 categories 合成。
MODERATION_CATEGORY_MAP = {
    "sexual": "sexual",
    "sexual/minors": "sexual",
    "harassment": "hate_and_discrimination",
    "harassment/threatening": "hate_and_discrimination",
    "hate": "hate_and_discrimination",
    "hate/threatening": "hate_and_discrimination",
    "violence": "violence_and_threats",
    "violence/graphic": "violence_and_threats",
    "self-harm": "selfharm",
    "self-harm/intent": "selfharm",
    "self-harm/instructions": "selfharm",
    "illicit": "criminal",
    "illicit/violent": "dangerous",
}


def normalize_moderations_response(body: dict) -> dict:
    """补上 flagged，并在保留 Mistral 原生分类的同时补一份 OpenAI 分类别名。"""
    if not isinstance(body, dict):
        return body
    body.setdefault("object", "list")
    for result in body.get("results") or []:
        if not isinstance(result, dict):
            continue
        categories = result.get("categories") or {}
        scores = result.get("category_scores") or {}
        for oa_name, mistral_name in MODERATION_CATEGORY_MAP.items():
            if mistral_name in categories:
                categories.setdefault(oa_name, categories[mistral_name])
                scores.setdefault(oa_name, scores.get(mistral_name, 0.0))
        result["categories"] = categories
        result["category_scores"] = scores
        result["flagged"] = any(bool(v) for v in categories.values())
    body["usage"] = {k: v for k, v in (clean_usage(body.get("usage"))).items()
                     if k in ("prompt_tokens", "total_tokens")}
    return body
