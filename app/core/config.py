"""配置管理：基于 pydantic-settings，支持 .env 与环境变量覆盖。

约定：
- 嵌套配置用双下划线分隔，例如 APP__DEBUG、DOCKER__TIMEOUT_SECONDS。
- 通过 get_settings() 获取单例；测试中可调用 get_settings.cache_clear() 重置。
- 大模型支持"单模型"与"多模型"两种写法，多模型用
  `LLM__MODELS__<ID>__<字段>`，详见 LLMSettings 的类注释。
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# app/core/config.py -> app/core -> app -> 项目根目录
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]


def split_csv_list(value: object) -> object:
    """把环境变量里的列表写法统一成 list[str]。

    兼容：'a,b,c' / 'a, b' / '["a","b"]' -> ['a','b','c']

    为什么必须显式处理：pydantic-settings 默认会用 JSON 解析 list 型字段的原始值，
    `FOO=a,b` 会直接抛 SettingsError 让应用起不来。因此所有 list 型字段都要
    标 `Annotated[list[str], NoDecode]` 并挂本函数作为 before 校验器。
    """
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("["):  # 兼容 JSON 数组写法
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, list):
                return [str(item).strip() for item in parsed if str(item).strip()]
        return [item.strip() for item in text.split(",") if item.strip()]
    return value


class AppSettings(BaseModel):
    """应用自身的运行参数（供 FastAPI / uvicorn 使用）。"""

    name: str = "ai-code-tutor-agent"
    version: str = "0.1.0"
    environment: Literal["local", "dev", "staging", "prod"] = "local"
    debug: bool = True
    host: str = "127.0.0.1"
    port: int = 8000
    api_v1_prefix: str = "/api/v1"
    log_level: str = "INFO"
    # 注意：NoDecode 必须保留。pydantic-settings 默认会把 list 型字段的原始值
    # 当作 JSON 解析，导致 `APP__CORS_ORIGINS=a,b` 这种逗号写法直接报
    # SettingsError 并使应用启动失败；NoDecode 会跳过该步，交给下面的
    # _split_origins 校验器处理，从而同时兼容 `a,b` 与 `["a","b"]` 两种写法。
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"]
    )
    trace_header: str = "X-Trace-Id"

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """兼容多种写法：'a,b,c' / 'a, b' / '["a","b"]' -> ['a','b','c']。"""
        return split_csv_list(value)

    @field_validator("api_v1_prefix")
    @classmethod
    def _normalize_prefix(cls, value: str) -> str:
        return value if value.startswith("/") else f"/{value}"


class DatabaseSettings(BaseModel):
    """数据库配置：开发期 SQLite，生产期切换 PostgreSQL。"""

    sqlite_path: Path = PROJECT_ROOT / "data" / "app.db"
    # 非空时优先生效，例如 postgresql+asyncpg://user:pwd@host:5432/agent
    url_override: str = ""
    echo: bool = False

    @property
    def url(self) -> str:
        """返回 SQLAlchemy 异步连接串。"""
        if self.url_override:
            return self.url_override
        if str(self.sqlite_path) == ":memory:":
            return "sqlite+aiosqlite:///:memory:"
        path = self.sqlite_path.expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        return f"sqlite+aiosqlite:///{path.as_posix()}"

    def ensure_directory(self) -> None:
        """确保 SQLite 文件所在目录存在（内存库跳过）。"""
        if self.url_override or str(self.sqlite_path) == ":memory:":
            return
        path = self.sqlite_path.expanduser()
        if not path.is_absolute():
            path = PROJECT_ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# 多模型配置：提供商默认地址与"本地模型"名单
# ---------------------------------------------------------------------------
# 常见 OpenAI 兼容服务商的默认 API 地址。
# 作用：同学在 .env 里只写了 provider 而没写 BASE_URL 时，自动补上，
# 少填一项就少一个填错的机会。地址都带 /v1（Ollama 的兼容层也是 /v1）。
PROVIDER_DEFAULT_BASE_URLS: dict[str, str] = {
    "deepseek": "https://api.deepseek.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "openai": "https://api.openai.com/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "zhipu": "https://open.bigmodel.cn/api/paas/v4",
    "siliconflow": "https://api.siliconflow.cn/v1",
    "ollama": "http://127.0.0.1:11434/v1",
    "vllm": "http://127.0.0.1:8001/v1",
    "lmstudio": "http://127.0.0.1:1234/v1",
}

# 本地部署的提供商：跑在自己电脑上，不需要 API Key。
# 名单里的模型在网页上不会强制要求填 Key（学生没网也能用本地模型演示）。
LOCAL_PROVIDERS: frozenset[str] = frozenset({"ollama", "vllm", "lmstudio", "local"})


class LLMModelSettings(BaseModel):
    """**一个**可选模型的配置（对应 `.env` 里的一组 `LLM__MODELS__<ID>__*`）。

    设计原则：能自动推出来的就不要让人填。
    比如写了 `PROVIDER=deepseek` 就不用再写 BASE_URL；
    写了 `PROVIDER=ollama` 就不要求填 API Key。
    """

    # 网页下拉框里显示的名字，例如「DeepSeek 官方」「通义千问」。留空则用 model_name
    label: str = ""
    # 提供商标识：deepseek / qwen / openai / ollama …（大小写不敏感）
    provider: str = "openai-compatible"
    # 真正发给服务商的模型名，例如 deepseek-chat、qwen-plus、qwen2.5-coder:7b
    model_name: str = ""
    # API 地址；留空则按 provider 自动补（见 PROVIDER_DEFAULT_BASE_URLS）
    base_url: str = ""
    # 默认 API Key（可选）。留空表示"用网页上输入的"或"用全局兜底 Key"
    api_key: SecretStr = SecretStr("")
    # 也可以让 Key 从**另一个环境变量**读（例如 OPENAI_API_KEY），
    # 这样密钥不必重复写进 .env 的这一段
    api_key_env: str = ""
    # 网页上的说明文字，例如「需要自己申请 Key，注册送额度」
    description: str = ""
    # 是否必须填 Key。None = 按 provider 自动判断（本地模型不需要）
    requires_api_key: bool | None = None
    # 关掉后网页上不显示这个模型（临时不想用某个模型时用，比删配置安全）
    enabled: bool = True
    # 以下三项留空（None）表示跟随全局 LLM__* 的默认值
    timeout_seconds: int | None = None
    max_tokens: int | None = None
    temperature: float | None = None

    # ---- 规整与推导 ----
    @field_validator("provider")
    @classmethod
    def _normalize_provider(cls, value: str) -> str:
        """提供商标识统一小写去空格，避免 DeepSeek / deepseek 被当成两个。"""
        return (value or "openai-compatible").strip().lower()

    @field_validator("label", "model_name", "base_url", "api_key_env", "description")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        """顺手去掉首尾空格：`.env` 里 `= ` 后面多打一个空格是很常见的。"""
        return (value or "").strip()

    @model_validator(mode="after")
    def _fill_defaults(self) -> LLMModelSettings:
        """补齐能自动推出来的字段，并做基本校验。"""
        if not self.model_name:
            raise ValueError("模型缺少 MODEL_NAME（例如 LLM__MODELS__DEEPSEEK__MODEL_NAME）")

        # 地址：没写就按 provider 查表；查不到就要求显式填写
        if not self.base_url:
            self.base_url = PROVIDER_DEFAULT_BASE_URLS.get(self.provider, "")
        if not self.base_url:
            raise ValueError(
                f"provider={self.provider} 没有内置默认地址，请显式填写 BASE_URL"
            )
        self.base_url = self.base_url.rstrip("/")

        # 是否需要 Key：没显式指定就按 provider / 地址判断
        if self.requires_api_key is None:
            self.requires_api_key = not self.is_local
        return self

    # ---- 展示用属性 ----
    @property
    def display_name(self) -> str:
        """网页上显示的名字：优先 LABEL，其次模型名。"""
        return self.label or self.model_name

    @property
    def is_local(self) -> bool:
        """是不是本地部署的模型（本地模型不需要 Key，也不该走公网）。"""
        if self.provider in LOCAL_PROVIDERS:
            return True
        # 地址指向本机时也当成本地模型（同学常把 vLLM 跑在 127.0.0.1:其他端口）
        host = self.base_url.lower()
        return any(mark in host for mark in ("127.0.0.1", "localhost", "0.0.0.0"))

    def to_public_dict(
        self, *, id: str, is_default: bool = False, has_default_key: bool = False
    ) -> dict[str, object]:
        """转成"可以安全发给前端"的字典——**绝不包含 API Key**。

        Args:
            id: 模型 ID（前端提交回来时用它）。
            is_default: 是不是默认模型（前端可以把它预选上）。
            has_default_key: 后端是否已有可用 Key（只回答"有没有"，不给值）。
        """
        return {
            "id": id,
            "label": self.display_name,
            "provider": self.provider,
            "model_name": self.model_name,
            "base_url": self.base_url,
            "description": self.description,
            "is_local": self.is_local,
            "requires_api_key": bool(self.requires_api_key),
            "has_default_key": has_default_key,
            "is_default": is_default,
        }


class LLMSettings(BaseModel):
    """大模型配置：默认走 OpenAI 兼容协议，便于接 DeepSeek-Coder / CodeQwen。

    支持两种写法，可以同时用：

    1. **单模型（旧写法，仍然兼容）**：`LLM__BASE_URL` / `LLM__MODEL` / `LLM__API_KEY`。
    2. **多模型（新写法）**：在 `.env` 里按模型各写一组

       ```ini
       LLM__MODELS__DEEPSEEK__LABEL=DeepSeek
       LLM__MODELS__DEEPSEEK__MODEL_NAME=deepseek-chat
       LLM__MODELS__DEEPSEEK__BASE_URL=https://api.deepseek.com/v1
       LLM__MODELS__DEEPSEEK__API_KEY=
       LLM__DEFAULT_MODEL=deepseek
       ```

       `LLM__MODELS__<ID>__*` 里的 `<ID>` 是模型标识（小写字母数字，如 deepseek / qwen / ollama），
       网页上传回来的就是这个 ID。

    两者同时存在时**以多模型为准**；但上面这几个单模型字段仍然有用：
    它们是"全局默认值"，多模型里没写的项（超时、max_tokens、temperature）会从这里继承，
    而 `LLM__API_KEY` 还兼任"某个模型没单独配 Key 时的兜底 Key"（详见 `effective_api_key`）。
    """

    provider: str = "openai-compatible"
    base_url: str = "https://api.deepseek.com/v1"
    api_key: SecretStr = SecretStr("")
    # 实测（2026-02，本机网络）：DeepSeek 会把请求的模型名统一路由到当前主力模型，
    # 请求 deepseek-coder / deepseek-chat 都返回 model=deepseek-flash。
    # 所以这里用官方通用的 deepseek-chat；换其它服务商时按它的文档填。
    model: str = "deepseek-chat"
    timeout_seconds: int = 120
    max_tokens: int = 4096
    temperature: float = 0.2
    # 429 / 5xx / 超时的重试次数与退避基数（指数退避）
    max_retries: int = 2
    retry_backoff_seconds: float = 1.0
    # 单次请求附加头（部分网关需要）
    extra_headers: dict[str, str] = Field(default_factory=dict)

    # ------------------------------------------------------------------
    # 多模型
    # ------------------------------------------------------------------
    # 模型表：键是模型 ID（`.env` 里 `LLM__MODELS__<ID>__*` 的小写形式）
    models: dict[str, LLMModelSettings] = Field(default_factory=dict)
    # 默认用哪个模型（写模型 ID）。留空 = 用列表里的第一个；
    # 一个多模型都没配时，自动用上面的单模型字段拼一个 id 为 default 的模型。
    default_model: str = ""

    # ---- 校验：把用户写的大小写、空格、末尾斜杠都规整掉 ----
    @field_validator("provider")
    @classmethod
    def _normalize_provider(cls, value: str) -> str:
        return (value or "openai-compatible").strip().lower()

    @field_validator("base_url")
    @classmethod
    def _normalize_base_url(cls, value: str) -> str:
        """去掉末尾斜杠：拼接 `/chat/completions` 时才不会出现 `//`。"""
        return (value or "").strip().rstrip("/")

    @field_validator("models")
    @classmethod
    def _normalize_model_ids(
        cls, value: dict[str, LLMModelSettings]
    ) -> dict[str, LLMModelSettings]:
        """把模型 ID 规整成小写并去空格，同时挡掉非法 ID。

        为什么要规整：`.env` 里写 `LLM__MODELS__DeepSeek__*` 或 `LLM__MODELS__deepseek__*`
        都应该能用；前端提交的 id 也必须能对上，否则"选了模型却报找不到"。
        """
        normalized: dict[str, LLMModelSettings] = {}
        for raw_id, model in (value or {}).items():
            model_id = str(raw_id).strip().lower()
            if not model_id:
                raise ValueError("模型 ID 不能为空（写成 LLM__MODELS__<ID>__* 的形式）")
            # 只允许 ASCII：这个 ID 会长在环境变量名里（LLM__MODELS__<ID>__*），
            # 也可能被前端放进 URL 查询串，用中文/符号会带来一堆编码问题。
            # 注意不能只用 isalnum()：中文、日文等字符在 Python 里也算是 alnum。
            if not all(ch.isascii() and (ch.isalnum() or ch in "-_") for ch in model_id):
                raise ValueError(
                    f"模型 ID「{raw_id}」只允许 ASCII 字母、数字、下划线和连字符"
                    "（建议用小写英文，如 deepseek / qwen / ollama）"
                )
            normalized[model_id] = model
        return normalized

    @model_validator(mode="after")
    def _check_default_model(self) -> LLMSettings:
        """默认模型的 ID 必须真实存在，否则就直接报错。

        这类配置错误如果不当场拦下，会拖到"学生点了检测却没反应"的时候才暴露，
        排查成本高得多——启动时失败反而更好定位。
        """
        if self.default_model and self.default_model.strip().lower() not in self.model_table:
            raise ValueError(
                f"LLM__DEFAULT_MODEL={self.default_model} 在模型列表里不存在，"
                f"可选：{sorted(self.model_table) or '（一个都没配，用的是单模型写法）'}"
            )
        self.default_model = self.default_model.strip().lower()
        return self

    # ---- 模型表：多模型为空时，用单模型字段兜底拼一个，保证老配置照样能跑 ----
    @property
    def model_table(self) -> dict[str, LLMModelSettings]:
        """返回"当前真正可用的模型表"（永远不会是空的）。"""
        if self.models:
            return self.models
        # 单模型写法：包装成一个 id 为 default 的模型，让上层逻辑只有一套
        return {
            "default": LLMModelSettings(
                label=self.model,
                provider=self.provider,
                model_name=self.model,
                base_url=self.base_url,
                api_key=self.api_key,
                description="来自单模型配置（LLM__MODEL / LLM__BASE_URL / LLM__API_KEY）",
            )
        }

    @property
    def default_model_id(self) -> str:
        """默认使用的模型 ID（`LLM__DEFAULT_MODEL` 留空时取第一个）。"""
        table = self.model_table
        if self.default_model in table:
            return self.default_model
        return next(iter(table))

    @property
    def is_configured(self) -> bool:
        """是否配置了可用的 API Key（旧的单模型判断，保留给已有调用方）。"""
        return bool(self.api_key.get_secret_value().strip())

    @property
    def has_local_model(self) -> bool:
        """是否有本地部署的模型（本地模型不需要 Key，没网也能演示）。"""
        return any(model.is_local for model in self.model_table.values())

    def get_model(self, model_id: str | None = None) -> LLMModelSettings:
        """按 ID 取模型配置；ID 为空时取默认模型。

        Raises:
            KeyError: 指定的模型不存在（调用方应转成 400，并提示可选值）。
        """
        table = self.model_table
        wanted = (model_id or "").strip().lower() or self.default_model_id
        if wanted not in table:
            raise KeyError(f"没有名为 {model_id} 的模型，可选：{sorted(table)}")
        return table[wanted]

    def effective_api_key(self, model: LLMModelSettings, override: str = "") -> str:
        """算出某个模型这次调用真正要用的 API Key。

        优先级（从高到低）：
          1. `override`：用户在网页上现场输入的 Key —— 只用于本次请求，不落盘；
          2. 模型自己配的 `LLM__MODELS__<ID>__API_KEY`；
          3. 模型指定的 `API_KEY_ENV`（Key 放在别的环境变量里，不必写进 .env）；
          4. 全局 `LLM__API_KEY` 兜底 —— 方便"只配了一个 Key"的同学直接用上新模型。

        第 4 条**只对云端模型生效**：本地模型（Ollama / vLLM 等）不吃全局 Key，
        免得把云端的密钥顺手发给本机的服务，也免得前端误以为"这个模型已经配好了"。

        另外提醒一句：把"全局 Key"发给任意云端模型时，若该 Key 只对某个服务商有效，
        别的模型会返回 401 —— 这时给那个模型单独配一个 KEY 即可（页面上的报错会说清）。
        """
        override = (override or "").strip()
        if override:
            return override

        own = model.api_key.get_secret_value().strip()
        if own:
            return own

        if model.api_key_env:
            from_env = os.environ.get(model.api_key_env.strip(), "").strip()
            if from_env:
                return from_env

        if model.is_local:
            return ""
        return self.api_key.get_secret_value().strip()

    def resolved_settings(self, model_id: str | None = None, api_key: str = "") -> LLMSettings:
        """按模型 ID 生成一份"可以直接交给 LLM 客户端用"的单模型配置。

        这是多模型与现有代码之间的适配层：`OpenAICompatibleClient` 只认
        `base_url / model / api_key` 这几个字段，这里把它们换成所选模型的值，
        其余（超时、重试、extra_headers）保持全局默认，于是**客户端一行都不用改**。

        Args:
            model_id: 模型 ID；为空用默认模型。
            api_key: 用户在网页上输入的 Key（可空，空则按 effective_api_key 的规则取）。
        """
        model = self.get_model(model_id)
        return self.model_copy(
            update={
                "provider": model.provider,
                "base_url": model.base_url,
                "model": model.model_name,
                "api_key": SecretStr(self.effective_api_key(model, api_key)),
                "timeout_seconds": model.timeout_seconds or self.timeout_seconds,
                "max_tokens": model.max_tokens or self.max_tokens,
                "temperature": (
                    self.temperature if model.temperature is None else model.temperature
                ),
            }
        )

    def public_models(self) -> list[dict[str, object]]:
        """给前端用的模型清单：**不含任何 API Key**，只说明"能不能直接用"。

        Returns:
            形如 `[{"id": "deepseek", "label": "DeepSeek", "has_default_key": True, ...}]`
        """
        default_id = self.default_model_id
        options: list[dict[str, object]] = []
        for model_id, model in self.model_table.items():
            if not model.enabled:
                continue
            options.append(
                model.to_public_dict(
                    id=model_id,
                    is_default=model_id == default_id,
                    # 只看"后端有没有现成的 Key"，具体值绝不外传
                    has_default_key=bool(self.effective_api_key(model)),
                )
            )
        return options



class SecuritySettings(BaseModel):
    """安全相关配置：网页上输入的 API Key 怎么临时保管。

    背景：项目原本的 Key 只能写在 `.env` 里（一个服务一个模型）。现在支持
    同学在网页上选模型、填自己的 Key，这些 Key 只放在**进程内存**里，
    不落库、不进日志（详见 `app/core/api_key_manager.py` 的说明）。
    这里配的就是那份内存保管箱的行为。
    """

    # 网页上输入的 Key 多久没用就自动清掉（秒）。默认 30 分钟。
    # 每次使用都会顺延，所以是"无操作 30 分钟后失效"。
    api_key_ttl_seconds: int = 30 * 60
    # 内存里最多同时保留多少个会话。set_key 是无需登录就能调的接口，
    # 不设上限的话有人循环调用就能把内存吃满；超过上限会淘汰最久没用的会话。
    max_sessions: int = 500
    # 是否允许前端传 Key。默认允许（这是本功能的目的）；
    # 想强制"只能用 .env 里配好的 Key"时设 false，接口会直接拒绝 set_key。
    allow_client_key: bool = True

    @field_validator("api_key_ttl_seconds", "max_sessions")
    @classmethod
    def _positive_security_values(cls, value: int) -> int:
        """有效期与会话上限都必须是正数，否则保管箱会立刻清空或直接拒绝服务。"""
        if value <= 0:
            raise ValueError("安全配置里的有效期与会话上限必须为正数")
        return value


class TracingSettings(BaseModel):
    """Agent Trace 配置。"""

    enabled: bool = True
    sink: Literal["log", "memory"] = "log"
    capture_input: bool = True
    capture_output: bool = True
    max_payload_chars: int = 2000
    memory_buffer_size: int = 1000


class Settings(BaseSettings):
    """全局配置根对象。"""

    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        env_nested_delimiter="__",
        extra="ignore",
        case_sensitive=False,
    )

    app: AppSettings = Field(default_factory=AppSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    tracing: TracingSettings = Field(default_factory=TracingSettings)

    def ensure_directories(self) -> None:
        """一次性创建运行期需要的本地目录。"""
        self.database.ensure_directory()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """获取全局配置单例（带缓存）。"""
    return Settings()


def get_available_models(settings: Settings | None = None) -> list[dict[str, object]]:
    """列出当前配置里所有"可选用"的模型。

    这是给前端下拉框用的接口数据，因此有一条硬规矩：
    **返回值里绝不能出现 API Key**，只回答"后端有没有现成的 Key"（`has_default_key`）。
    用户在网页上输入的 Key 属于"这次请求"的临时数据，不参与这份清单。

    Args:
        settings: 可传入一份自定义配置（测试用）；不传则用全局单例。

    Returns:
        每个模型一个字典，字段如下：

        | 字段 | 说明 |
        | --- | --- |
        | `id` | 模型 ID（前端选完把它传回来，如 `deepseek`） |
        | `label` | 网页上显示的名字，如 `DeepSeek 官方` |
        | `provider` | 提供商标识，如 `deepseek` / `qwen` / `ollama` |
        | `model_name` | 发给服务商的模型名，如 `deepseek-chat` |
        | `base_url` | API 地址 |
        | `description` | 给学生的说明文字 |
        | `is_local` | 是否本地模型（本地模型不需要 Key） |
        | `requires_api_key` | 是否必须填 Key |
        | `has_default_key` | 后端是否已配好 Key（只给布尔值，不给 Key 本身） |
        | `is_default` | 是否是默认模型（前端可以预选它） |
    """
    current = settings or get_settings()
    return current.llm.public_models()

