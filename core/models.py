# -*- coding: utf-8 -*-
"""模型注册表：从上游同步模型清单、维护别名、按能力路由。

上游 /v1/models 返回的每个模型都带 capabilities（completion_chat / reasoning / vision /
function_calling / ocr / moderation / audio 等）、max_context_length 和 aliases，这些信息
决定了本层能否安全地转发 reasoning_effort 之类的参数，所以值得缓存下来而不是每次现查。

自定义别名单独存，上游同步不会覆盖。典型用途是让写死了模型名的客户端能直接接进来，
比如把 gpt-4o 指到 mistral-large-latest。
"""
import json
import time
from dataclasses import dataclass, field

CHAT_CAPABILITY = "completion_chat"
SYNC_INTERVAL = 1800.0


@dataclass(slots=True)
class ModelInfo:
    id: str
    name: str = ""
    description: str = ""
    owned_by: str = "mistralai"
    created: int = 0
    max_context_length: int = 0
    default_temperature: float | None = None
    capabilities: dict = field(default_factory=dict)
    aliases: list = field(default_factory=list)
    deprecation: str | None = None

    @property
    def supports_reasoning(self) -> bool:
        return bool(self.capabilities.get("reasoning"))

    @property
    def supports_chat(self) -> bool:
        return bool(self.capabilities.get(CHAT_CAPABILITY))

    @property
    def supports_embeddings(self) -> bool:
        # 上游没有单独的 embedding 能力位, 用命名约定 + 无 chat 能力来判定
        return not self.supports_chat and "embed" in self.id

    @property
    def supports_moderation(self) -> bool:
        return bool(self.capabilities.get("moderation")) or "moderation" in self.id

    def to_openai(self) -> dict:
        """OpenAI /v1/models 形态, 附带本层的扩展字段(客户端会忽略未知键)。"""
        return {
            "id": self.id,
            "object": "model",
            "created": self.created or int(time.time()),
            "owned_by": self.owned_by,
            "context_window": self.max_context_length,
            "max_context_length": self.max_context_length,
            "capabilities": self.capabilities,
            "description": self.description,
            "deprecation": self.deprecation,
        }


class ModelRegistry:
    def __init__(self, store=None):
        self.store = store
        self.models: dict[str, ModelInfo] = {}
        self.custom_aliases: dict[str, str] = {}
        self.synced_at: float = 0.0
        self._index: dict[str, str] = {}

    # ---------- 持久化 ----------

    def load(self) -> None:
        if not self.store:
            return
        raw = self.store.get_meta("models")
        if raw:
            try:
                data = json.loads(raw)
                self.update(data.get("models") or [], persist=False)
                self.synced_at = data.get("synced_at", 0.0)
            except (json.JSONDecodeError, TypeError, KeyError):
                pass
        raw_alias = self.store.get_meta("model_aliases")
        if raw_alias:
            try:
                self.custom_aliases = {str(k): str(v)
                                       for k, v in json.loads(raw_alias).items()}
            except (json.JSONDecodeError, TypeError, AttributeError):
                self.custom_aliases = {}
        self._reindex()

    def _persist_models(self, raw: list) -> None:
        if self.store:
            self.store.set_meta("models", json.dumps(
                {"synced_at": self.synced_at, "models": raw}, ensure_ascii=False))

    def _persist_aliases(self) -> None:
        if self.store:
            self.store.set_meta("model_aliases",
                                json.dumps(self.custom_aliases, ensure_ascii=False))

    # ---------- 同步 ----------

    def update(self, raw_models: list, persist: bool = True) -> int:
        models = {}
        for raw in raw_models or []:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            models[raw["id"]] = ModelInfo(
                id=raw["id"],
                name=raw.get("name") or raw["id"],
                description=raw.get("description") or "",
                owned_by=raw.get("owned_by") or "mistralai",
                created=int(raw.get("created") or 0),
                max_context_length=int(raw.get("max_context_length") or 0),
                default_temperature=raw.get("default_model_temperature"),
                capabilities=raw.get("capabilities") or {},
                aliases=list(raw.get("aliases") or []),
                deprecation=raw.get("deprecation"))
        if not models:
            return 0
        self.models = models
        self.synced_at = time.time()
        self._reindex()
        if persist:
            self._persist_models(raw_models)
        return len(models)

    def stale(self) -> bool:
        return time.time() - self.synced_at > SYNC_INTERVAL

    def _reindex(self) -> None:
        index: dict[str, str] = {}
        for model in self.models.values():
            index[model.id.lower()] = model.id
            for alias in model.aliases:
                index.setdefault(str(alias).lower(), model.id)
        for alias, target in self.custom_aliases.items():
            index[alias.lower()] = target
        self._index = index

    # ---------- 查询 ----------

    def resolve(self, name: str) -> ModelInfo | None:
        if not name:
            return None
        target = self._index.get(name.strip().lower())
        if target:
            return self.models.get(target) or ModelInfo(id=target)
        return None

    def resolve_id(self, name: str) -> str:
        """解析成上游真实模型 id。解析不出来就原样透传, 让上游去报错。"""
        info = self.resolve(name)
        return info.id if info else name

    def list_openai(self, capability: str | None = CHAT_CAPABILITY) -> list[dict]:
        out = []
        for model in self.models.values():
            if capability and not model.capabilities.get(capability):
                continue
            out.append(model.to_openai())
        for alias, target in sorted(self.custom_aliases.items()):
            info = self.models.get(target)
            if not info or (capability and not info.capabilities.get(capability)):
                continue
            entry = info.to_openai()
            entry.update({"id": alias, "root": target, "alias_of": target})
            out.append(entry)
        out.sort(key=lambda m: m["id"])
        return out

    # ---------- 别名管理 ----------

    def set_alias(self, alias: str, target: str) -> None:
        alias = (alias or "").strip()
        target = (target or "").strip()
        if not alias or not target:
            raise ValueError("alias 和 target 都不能为空")
        if alias.lower() in {m.lower() for m in self.models}:
            raise ValueError(f"{alias} 是上游真实模型名, 不能当别名")
        self.custom_aliases[alias] = target
        self._reindex()
        self._persist_aliases()

    def remove_alias(self, alias: str) -> bool:
        if alias in self.custom_aliases:
            del self.custom_aliases[alias]
            self._reindex()
            self._persist_aliases()
            return True
        return False
