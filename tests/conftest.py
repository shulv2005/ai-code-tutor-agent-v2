"""测试公共夹具：隔离数据库/数据目录与 Trace Sink，避免用例间互相污染。

本文件只保留「AI 代码导师（学生端）」需要的夹具。原先还有一整套服务于
「开源贡献端」的夹具（sample_repo 造本地 git 仓库、offline_clone 把 URL
解析替换成本地路径、repo_workspace、retrieval_env 配 hashing 嵌入…），
已随该功能删除。**连带的好处是测试不再需要 GitPython**。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.core.config import Settings, get_settings
from app.core.llm_client import LLMMessage, LLMResponse, LLMUsage, set_llm_client
from app.core.trace import MemoryTraceSink, TraceRecorder, set_trace_recorder

# ---------------------------------------------------------------------------
# 样例源码：覆盖 Python（常规/异步/嵌套/语法错误）、JS（导出/箭头函数/类）、Go
# ---------------------------------------------------------------------------
SAMPLE_PY = '''"""Sample module."""

import os
from typing import Any


def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


async def fetch(url: str, retries: int = 3) -> str:
    """Fetch a url."""
    if not url:
        raise ValueError("empty")
    for _ in range(retries):
        while True:
            break
    return url


class Service:
    """A service."""

    def __init__(self, name: str) -> None:
        self.name = name

    def run(self, flag: bool) -> bool:
        return flag and True
'''

SAMPLE_JS = """// Greet someone politely.
export function greet(name) { return `hi ${name}`; }

const double = (x) => x * 2;

class Widget {
  render() { return 1; }
}
"""

SAMPLE_GO = """package main

import "fmt"

// Server serves things.
type Server struct {
\tPort int
}

func (s *Server) Start() error {
\tfmt.Println(s.Port)
\treturn nil
}

func helper(x int) int {
\treturn x + 1
}
"""

BROKEN_PY = "def broken(:\n    pass\n"


@pytest.fixture()
def sqlite_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """把数据库指向临时文件，并清空配置缓存。"""
    db_path = tmp_path / "test.db"
    monkeypatch.setenv("DATABASE__SQLITE_PATH", str(db_path))
    monkeypatch.setenv("APP__LOG_LEVEL", "WARNING")
    get_settings.cache_clear()
    yield db_path
    get_settings.cache_clear()


@pytest.fixture()
def memory_recorder() -> Iterator[tuple[TraceRecorder, MemoryTraceSink]]:
    """提供内存 Trace Sink，便于断言记录内容。"""
    sink = MemoryTraceSink(maxlen=100)
    recorder = TraceRecorder([sink])
    set_trace_recorder(recorder)
    yield recorder, sink
    set_trace_recorder(None)


@pytest.fixture()
def sample_tree(tmp_path: Path) -> Path:
    """造一棵**纯文件系统**的样例代码树，供解析器的遍历测试使用。

    刻意不建 git 仓库：需要 git 的只有"克隆远程仓库"那条链路，
    它属于已删除的开源贡献端。解析器只关心"目录里有哪些源码文件"，
    用普通目录就够，测试也因此不再依赖 GitPython。
    """
    root = tmp_path / "sample_src"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
    (root / "pkg" / "core.py").write_text(SAMPLE_PY, encoding="utf-8")
    (root / "pkg" / "broken.py").write_text(BROKEN_PY, encoding="utf-8")
    (root / "web.js").write_text(SAMPLE_JS, encoding="utf-8")
    (root / "main.go").write_text(SAMPLE_GO, encoding="utf-8")
    # 非源码文件与排除目录，用于验证遍历剪枝
    (root / "README.md").write_text("# sample\n", encoding="utf-8")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "dep.js").write_text("function dep() {}\n", encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def _isolate_runtime_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """默认把所有运行期数据目录隔离到临时目录（autouse，作用于所有测试）。

    为什么必须这么做：这些配置的默认值都指向仓库里的 `data/`，
    凡是忘了显式覆盖的测试都会读写它。后果有两层：
    1. 污染开发者的工作目录（跑一次测试就多出一堆数据库文件）；
    2. **更隐蔽的是让测试之间通过共享状态互相影响**，且失败原因与代码无关：
       残留的 `data/app.db` 里若有历史记录，用例断言的"总数为 N"就会莫名其妙地不对。

    显式覆盖这些变量的夹具（sqlite_path 等）在本夹具之后执行，仍然生效。
    """
    monkeypatch.setenv("DATABASE__SQLITE_PATH", str(tmp_path / "_data" / "app.db"))
    # 强制清空 API Key：开发机上 .env 里可能配了真实的付费 Key，
    # 万一某个用例漏了注入假客户端，就会真的发请求出去——既花钱又让结果不可复现。
    # 需要"已配置"状态的用例自己再 setenv 覆盖即可（monkeypatch 在夹具之后执行）。
    monkeypatch.setenv("LLM__API_KEY", "")
    # 干脆让用例**完全不读开发机的 .env**：那是"部署配置"，不是测试输入。
    # 踩过一次：本机 .env 里加了多模型配置（LLM__MODELS__*）之后，
    # `test_models_list_endpoint` 断言的"模型清单正好是 deepseek + ollama"
    # 就被多出来的 qwen 顶掉了——失败原因与代码无关，纯粹是本机配置不同。
    # 用例需要什么配置，就自己用 `Settings(_env_file=None)` 或 monkeypatch.setenv 注入。
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 假 LLM 客户端：本机没有可用的模型端点，用它让整条 Agent 链路离线可测
# ---------------------------------------------------------------------------
class FakeLLMClient:
    """可编排响应的假 LLM 客户端。

    实现 `LLMClient` 协议，行为完全确定：
    - `responses` 按调用顺序依次返回；最后一个会被重复使用（便于测重试）。
    - 也可传一个 `callable(messages) -> str` 动态生成响应。
    - 记录每次调用的 messages，便于断言提示词内容。
    """

    def __init__(
        self,
        responses: list[str] | str,
        *,
        model: str = "fake-model",
        fail_with: Exception | None = None,
    ) -> None:
        if isinstance(responses, str):
            responses = [responses]
        self._responses = list(responses)
        self._model = model
        self._fail_with = fail_with
        self.calls: list[list[LLMMessage]] = []

    @property
    def model(self) -> str:
        return self._model

    @property
    def configured(self) -> bool:
        return True

    @property
    def call_count(self) -> int:
        return len(self.calls)

    @property
    def last_messages(self) -> list[LLMMessage]:
        return self.calls[-1] if self.calls else []

    @property
    def last_user_content(self) -> str:
        for message in reversed(self.last_messages):
            if message.role == "user":
                return message.content
        return ""

    async def chat(self, messages: list[LLMMessage], **_: object) -> LLMResponse:
        self.calls.append(list(messages))
        if self._fail_with is not None:
            raise self._fail_with

        index = min(len(self.calls) - 1, len(self._responses) - 1)
        content = self._responses[index]
        return LLMResponse(
            content=content,
            model=self._model,
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150),
            latency_ms=1.0,
            attempts=1,
        )

    async def aclose(self) -> None:
        return None


# 一段格式规范、可直接运行的生成结果，供假客户端复用
FAKE_GENERATED_TEST = '''```python
import pytest

from pkg.core import add


def test_add_positive_numbers():
    assert add(1, 2) == 3


@pytest.mark.parametrize("a, b, expected", [(0, 0, 0), (-1, 1, 0)])
def test_add_edge_cases(a, b, expected):
    assert add(a, b) == expected
```
'''


@pytest.fixture()
def fake_llm() -> FakeLLMClient:
    """默认返回可运行测试的假客户端。"""
    return FakeLLMClient(FAKE_GENERATED_TEST)


@pytest.fixture()
def llm_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """注入假 LLM 客户端，并清空配置缓存。"""
    monkeypatch.setenv("APP__LOG_LEVEL", "WARNING")
    get_settings.cache_clear()
    yield tmp_path
    set_llm_client(None)
    get_settings.cache_clear()
