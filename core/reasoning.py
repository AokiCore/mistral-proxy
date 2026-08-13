# -*- coding: utf-8 -*-
"""思考(reasoning)内容的格式转换。

Mistral 的实测格式 (reasoning_effort=high 时才出现, 见 .private/probe2_out.json):

  非流式  message.content = [
              {"type":"thinking","thinking":[{"type":"text","text":"..."}],"closed":true},
              {"type":"text","text":"最终答案"}]
  流式    思考增量  delta.content = [{"type":"thinking","thinking":[{"type":"text","text":"片段"}],
                                     "closed":true}]      <- closed 恒为 true, 不能当结束标记
          正文增量  delta.content = "片段"                  <- 纯字符串
          所以流里 content 的类型会从 list 切回 str, 这个切换点就是思考结束点。

对外输出格式没有官方标准, 现状是三派:
  - DeepSeek 系 (Qwen/Zhipu/Moonshot/火山等): message.reasoning_content 字符串 —— 客户端支持最广
  - OpenRouter: message.reasoning + reasoning_details 数组, reasoning_content 只作为入参别名
  - MiniMax 等: 直接在 content 里塞 <think>...</think>
默认同时输出 reasoning_content 和 reasoning 两个键 (同一个字符串), 覆盖前两派;
只认 <think> 标签的客户端用 think_tags 模式。
"""
from dataclasses import dataclass, field

REASONING_CONTENT = "reasoning_content"
THINK_TAGS = "think_tags"
PASSTHROUGH = "passthrough"
STRIP = "strip"

MODES = (REASONING_CONTENT, THINK_TAGS, PASSTHROUGH, STRIP)

THINK_OPEN = "<think>\n"
THINK_CLOSE = "\n</think>\n\n"


def split_chunks(content) -> tuple[str, str]:
    """把 Mistral 的 content 拆成 (思考文本, 正文)。content 为字符串时全算正文。"""
    if isinstance(content, str):
        return "", content
    if not isinstance(content, list):
        return "", ""
    think, answer = [], []
    for chunk in content:
        if not isinstance(chunk, dict):
            continue
        if chunk.get("type") == "thinking":
            for part in chunk.get("thinking") or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    think.append(part["text"])
                elif isinstance(part, str):
                    think.append(part)
        elif isinstance(chunk.get("text"), str):
            answer.append(chunk["text"])
    return "".join(think), "".join(answer)


def content_chars(content) -> tuple[int, int]:
    think, answer = split_chunks(content)
    return len(think), len(answer)


def reasoning_tokens_for(think_chars: int, answer_chars: int, completion_tokens: int) -> int:
    """Mistral 不单独上报 reasoning_tokens, 按字符占比从 completion_tokens 里估算。"""
    total = think_chars + answer_chars
    if not total or not completion_tokens or not think_chars:
        return 0
    return int(completion_tokens * think_chars / total)


def apply_to_message(msg: dict, mode: str) -> tuple[int, int]:
    """就地把非流式 message 转成目标格式。返回 (思考字符数, 正文字符数)。"""
    if not isinstance(msg, dict):
        return 0, 0
    content = msg.get("content")
    think, answer = split_chunks(content)

    if mode == PASSTHROUGH or not isinstance(content, list):
        return len(think), len(answer)

    if mode == STRIP:
        msg["content"] = answer
    elif mode == THINK_TAGS:
        msg["content"] = (THINK_OPEN + think + THINK_CLOSE + answer) if think else answer
    else:  # REASONING_CONTENT
        msg["content"] = answer
        if think:
            msg["reasoning_content"] = think
            msg["reasoning"] = think
    return len(think), len(answer)


@dataclass
class StreamConverter:
    """流式思考转换的状态机。每个请求一个实例。

    需要状态是因为 think_tags 模式必须在思考开始时补 <think>、在切回正文时补 </think>,
    而这个切换点只能靠 delta.content 从 list 变回 str 来判定。
    """
    mode: str = REASONING_CONTENT
    think_chars: int = 0
    answer_chars: int = 0
    _opened: bool = field(default=False, repr=False)
    _closed: bool = field(default=False, repr=False)

    def feed(self, event: dict) -> None:
        """就地改写一条 SSE 事件。"""
        if not isinstance(event, dict):
            return
        for choice in event.get("choices") or []:
            if isinstance(choice, dict):
                self._convert_delta(choice.get("delta"))

    def _convert_delta(self, delta) -> None:
        if not isinstance(delta, dict) or "content" not in delta:
            return
        content = delta["content"]
        think, answer = split_chunks(content)
        self.think_chars += len(think)
        self.answer_chars += len(answer)

        if self.mode == PASSTHROUGH:
            return

        if self.mode == STRIP:
            delta["content"] = answer
            return

        if self.mode == THINK_TAGS:
            out = ""
            if think:
                if not self._opened:
                    out += THINK_OPEN
                    self._opened = True
                out += think
            if answer or (self._opened and not self._closed and not think):
                # 思考结束(增量切回纯文本)时补上闭合标签
                if self._opened and not self._closed:
                    out += THINK_CLOSE
                    self._closed = True
                out += answer
            delta["content"] = out
            return

        # REASONING_CONTENT
        delta["content"] = answer
        if think:
            delta["reasoning_content"] = think
            delta["reasoning"] = think

    def finalize(self) -> dict | None:
        """流结束时若 <think> 还没闭合, 返回一个补闭合标签的 delta, 否则 None。"""
        if self.mode == THINK_TAGS and self._opened and not self._closed:
            self._closed = True
            return {"content": THINK_CLOSE}
        return None

    def reasoning_tokens(self, completion_tokens: int) -> int:
        return reasoning_tokens_for(self.think_chars, self.answer_chars, completion_tokens)
