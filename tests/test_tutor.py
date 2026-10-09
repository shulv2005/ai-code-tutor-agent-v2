"""AI 代码导师模块测试：JSON 解析、三个 Agent 能力、API 全链路。

大模型用可注入的假客户端替代，因此整条链路离线可测。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.agents.tutor_agent import TutorAgent, extract_json_object
from app.api.deps import get_tutor_agent
from app.core.config import get_settings
from app.main import create_app
from app.services.tutor_service import TutorService
from tests.conftest import FakeLLMClient

PY_CODE = "def add(a, b):\n    return a - b\n"
C_CODE = "int add(int a, int b) {\n    return a + b;\n}\n"
JAVA_CODE = "public class A {\n    int f() { return 1; }\n}\n"

CHECK_REPLY = json.dumps(
    {
        "score": 72,
        "summary": "整体结构清楚，但有一个逻辑错误。",
        "issues": [
            {
                "line": 2,
                "severity": "error",
                "title": "函数名是 add 却做了减法",
                "detail": "函数叫 add（相加），但返回的是 a - b。",
                "suggestion": "改成 return a + b",
            },
            {"line": None, "severity": "警告", "title": "缺少类型注解"},
        ],
        "highlights": ["命名清晰", "缩进规范"],
    },
    ensure_ascii=False,
)

COMMENT_REPLY = json.dumps(
    {
        "commented_code": 'def add(a, b):\n    """把两个数相加。"""\n    return a + b\n',
        "summary": "在函数开头加了功能说明",
    },
    ensure_ascii=False,
)

FIX_REPLY = json.dumps(
    {
        "had_error": True,
        "summary": "函数实现与命名不符",
        "fixed_code": "def add(a, b):\n    return a + b\n",
        "changes": [
            {"line": 2, "original": "return a - b", "fixed": "return a + b",
             "reason": "add 应该是相加"},
        ],
    },
    ensure_ascii=False,
)

NO_ERROR_REPLY = json.dumps(
    {"had_error": False, "summary": "没有发现问题", "fixed_code": PY_CODE, "changes": []},
    ensure_ascii=False,
)


# ---------------------------------------------------------------------------
# JSON 解析的健壮性
# ---------------------------------------------------------------------------
def test_extract_plain_json() -> None:
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_json_in_markdown_fence() -> None:
    """模型经常不守规矩，把 JSON 套在 ```json 里。"""
    text = '好的，结果如下：\n```json\n{"a": 1, "b": [2]}\n```\n希望有帮助。'
    assert extract_json_object(text) == {"a": 1, "b": [2]}


def test_extract_json_with_surrounding_prose() -> None:
    text = '分析结果：{"score": 80} 以上。'
    assert extract_json_object(text) == {"score": 80}


def test_extract_returns_none_for_non_json() -> None:
    assert extract_json_object("这段代码没有问题") is None
    assert extract_json_object("") is None


def test_extract_returns_none_for_json_array() -> None:
    """只接受对象；数组说明模型没按格式来。"""
    assert extract_json_object("[1, 2, 3]") is None


# ---------------------------------------------------------------------------
# Agent 层
# ---------------------------------------------------------------------------
def _agent(reply: str) -> TutorAgent:
    return TutorAgent(get_settings(), FakeLLMClient(reply))


async def test_agent_check_normalizes_fields() -> None:
    result = await _agent(CHECK_REPLY).check(PY_CODE, filename="a.py", language="python")
    payload = result.payload

    assert payload["score"] == 72
    assert len(payload["issues"]) == 2
    assert payload["highlights"] == ["命名清晰", "缩进规范"]
    # 非法的 severity 要被纠正成 info，避免前端拿到未知值
    assert payload["issues"][1]["severity"] == "info"


async def test_agent_check_clamps_score() -> None:
    """模型给出越界分数时要收敛到 0-100。"""
    reply = json.dumps({"score": 380, "issues": [], "summary": ""})
    result = await _agent(reply).check(PY_CODE, filename="a.py", language="python")
    assert result.payload["score"] == 100.0


async def test_agent_check_handles_bad_score() -> None:
    reply = json.dumps({"score": "优秀", "issues": [], "summary": ""})
    result = await _agent(reply).check(PY_CODE, filename="a.py", language="python")
    assert result.payload["score"] == 0.0
    assert result.warnings


async def test_agent_check_warns_on_unparsable_output() -> None:
    result = await _agent("我不会返回 JSON").check(PY_CODE, filename="a.py", language="python")
    assert result.payload["score"] == 0.0
    assert result.payload["issues"] == []
    assert any("JSON" in w for w in result.warnings)


async def test_agent_comment_falls_back_to_original() -> None:
    """模型没给出注释代码时，退回原代码并告警，而不是让前端拿到空内容。"""
    reply = json.dumps({"summary": "失败了"})
    result = await _agent(reply).comment(PY_CODE, filename="a.py", language="python")
    assert result.payload["commented_code"] == PY_CODE
    assert result.warnings


async def test_agent_fix_returns_code_and_changes() -> None:
    result = await _agent(FIX_REPLY).fix(PY_CODE, filename="a.py", language="python")
    assert result.payload["had_error"] is True
    assert "return a + b" in result.payload["fixed_code"]
    assert result.payload["changes"][0]["reason"] == "add 应该是相加"


async def test_agent_fix_without_error() -> None:
    result = await _agent(NO_ERROR_REPLY).fix(PY_CODE, filename="a.py", language="python")
    assert result.payload["had_error"] is False
    assert result.payload["changes"] == []


async def test_agent_prompt_contains_code_and_language() -> None:
    fake = FakeLLMClient(CHECK_REPLY)
    agent = TutorAgent(get_settings(), fake)
    await agent.check(C_CODE, filename="a.c", language="c")

    user = fake.last_user_content
    assert "return a + b" in user
    assert "a.c" in user
    # 应把语言告诉模型，否则可能按 Python 习惯点评 C 代码
    assert "C" in user


# ---------------------------------------------------------------------------
# 服务层
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("score", "level"),
    [(95, "优秀"), (80, "良好"), (65, "及格"), (30, "待改进"), (90, "优秀"), (60, "及格")],
)
def test_score_level(score: float, level: str) -> None:
    assert TutorService.score_level(score) == level


# ---------------------------------------------------------------------------
# API 层
# ---------------------------------------------------------------------------
class TutorFakeLLM(FakeLLMClient):
    """按系统提示词内容路由到对应回复的假客户端。

    不能按调用顺序返回：每个测试通常只调一个接口，
    顺序式客户端会让 /comment 收到检测的回复、/fix 收到注释的回复。
    按提示词里的特征字段（issues / commented_code / fixed_code）路由才可靠。
    """

    def __init__(self) -> None:
        super().__init__([CHECK_REPLY])
        self.calls_by_kind: dict[str, int] = {"check": 0, "comment": 0, "fix": 0}

    async def chat(self, messages, **kwargs):  # type: ignore[no-untyped-def]
        from app.core.llm_client import LLMResponse, LLMUsage

        system = next((m.content for m in messages if m.role == "system"), "")
        if "commented_code" in system:
            kind, content = "comment", COMMENT_REPLY
        elif "fixed_code" in system:
            kind, content = "fix", FIX_REPLY
        else:
            kind, content = "check", CHECK_REPLY

        self.calls.append(list(messages))
        self.calls_by_kind[kind] += 1
        return LLMResponse(
            content=content,
            model="fake-tutor",
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=100, completion_tokens=60, total_tokens=160),
            latency_ms=1.0,
        )


@pytest.fixture()
def tutor_app() -> Iterator[object]:
    """注入假 LLM 的应用。

    假客户端在 lambda 外面创建一次并共用；若写在 lambda 里，
    每次解析依赖都会新建一个，各接口拿到的回复会互相串。
    """
    app = create_app()
    settings = get_settings()
    fake = TutorFakeLLM()
    app.dependency_overrides[get_tutor_agent] = lambda: TutorAgent(settings, fake)
    yield app
    app.dependency_overrides.clear()


def _form(filename: str, code: str) -> dict:
    return {"filename": filename, "code": code}


def test_analyze_detects_language_and_symbols(sqlite_path: Path, tutor_app: object) -> None:
    with TestClient(tutor_app) as client:  # type: ignore[arg-type]
        response = client.post("/api/v1/tutor/analyze", data=_form("demo.py", PY_CODE))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["language"] == "python"
    assert body["language_label"] == "Python"
    assert body["line_count"] == 2
    assert [s["qualified_name"] for s in body["symbols"]] == ["add"]


@pytest.mark.parametrize(
    ("filename", "code", "language"),
    [("a.py", PY_CODE, "python"), ("a.c", C_CODE, "c"), ("A.java", JAVA_CODE, "java")],
)
def test_analyze_supports_all_target_languages(
    sqlite_path: Path, tutor_app: object, filename: str, code: str, language: str
) -> None:
    with TestClient(tutor_app) as client:  # type: ignore[arg-type]
        body = client.post("/api/v1/tutor/analyze", data=_form(filename, code)).json()
    assert body["language"] == language
    assert body["symbols"]


def test_analyze_unknown_extension(sqlite_path: Path, tutor_app: object) -> None:
    with TestClient(tutor_app) as client:  # type: ignore[arg-type]
        body = client.post("/api/v1/tutor/analyze", data=_form("notes.txt", "hello")).json()
    assert body["language"] == "unknown"
    assert body["symbols"] == []
    assert body["notes"]


def test_analyze_reports_syntax_error(sqlite_path: Path, tutor_app: object) -> None:
    with TestClient(tutor_app) as client:  # type: ignore[arg-type]
        body = client.post("/api/v1/tutor/analyze", data=_form("bad.py", "def f(:\n")).json()
    assert body["parse_error"] is not None


def test_analyze_rejects_empty_input(sqlite_path: Path, tutor_app: object) -> None:
    with TestClient(tutor_app) as client:  # type: ignore[arg-type]
        response = client.post("/api/v1/tutor/analyze", data={"filename": "a.py", "code": "   "})
    assert response.status_code == 400


def test_check_endpoint(sqlite_path: Path, tutor_app: object) -> None:
    with TestClient(tutor_app) as client:  # type: ignore[arg-type]
        response = client.post("/api/v1/tutor/check", data=_form("demo.py", PY_CODE))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["score"] == 72
    # 72 分落在 [60, 75) 区间，按阈值应为「及格」
    assert body["level"] == "及格"
    assert len(body["issues"]) == 2
    assert body["issues"][0]["severity"] == "error"
    assert body["issues"][0]["line"] == 2
    assert body["highlights"] == ["命名清晰", "缩进规范"]


def test_check_rejects_unknown_language(sqlite_path: Path, tutor_app: object) -> None:
    with TestClient(tutor_app) as client:  # type: ignore[arg-type]
        response = client.post("/api/v1/tutor/check", data=_form("notes.txt", "hello"))
    assert response.status_code == 400
    assert "语言" in response.json()["detail"]


def test_comment_endpoint(sqlite_path: Path, tutor_app: object) -> None:
    with TestClient(tutor_app) as client:  # type: ignore[arg-type]
        body = client.post("/api/v1/tutor/comment", data=_form("demo.py", PY_CODE)).json()

    assert "把两个数相加" in body["commented_code"]
    assert body["summary"]


def test_fix_endpoint(sqlite_path: Path, tutor_app: object) -> None:
    with TestClient(tutor_app) as client:  # type: ignore[arg-type]
        body = client.post("/api/v1/tutor/fix", data=_form("demo.py", PY_CODE)).json()

    assert body["had_error"] is True
    assert "return a + b" in body["fixed_code"]
    assert body["changes"][0]["reason"] == "add 应该是相加"
    assert body["changes"][0]["line"] == 2


def test_ai_endpoints_do_not_touch_the_database(sqlite_path: Path, tutor_app: object) -> None:
    """四个 AI 接口都不落库。

    原先每次操作都会往 `tutor_records` 流水账里写一行，供页面左下角的
    「历史记录」列表读取。该功能已删除，写入路径也一并移除——
    这里守住"没有偷偷写回去"，免得有人日后又把表加回来。
    """
    async def _table_names() -> set[str]:
        from app.core.database import get_session

        async with get_session() as session:
            from sqlalchemy import text

            rows = await session.execute(
                text("SELECT name FROM sqlite_master WHERE type='table'")
            )
            return {row[0] for row in rows}

    with TestClient(tutor_app) as client:  # type: ignore[arg-type]
        for action in ("analyze", "check", "comment", "fix"):
            response = client.post(f"/api/v1/tutor/{action}", data=_form("demo.py", PY_CODE))
            assert response.status_code == 200, response.text
        # 流水账表与它的记录 ID 都不该再出现
        assert "record_id" not in response.json()

    import asyncio

    tables = asyncio.run(_table_names())
    assert "tutor_records" not in tables, f"流水账表不该再建出来: {sorted(tables)}"


def test_status_endpoint_reports_ai_available(sqlite_path: Path, tutor_app: object) -> None:
    with TestClient(tutor_app) as client:  # type: ignore[arg-type]
        body = client.get("/api/v1/tutor/status").json()

    # 假客户端自称已配置
    assert body["ai_available"] is True
    assert "python" in body["supported_languages"]
    assert "c" in body["supported_languages"]
    assert "java" in body["supported_languages"]


def test_ai_endpoints_return_503_without_key(
    sqlite_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未配置模型时给出中文提示，而不是 500。"""
    monkeypatch.setenv("LLM__API_KEY", "")
    get_settings.cache_clear()
    from app.core.llm_client import set_llm_client

    set_llm_client(None)
    app = create_app()
    try:
        with TestClient(app) as client:
            for path in ("check", "comment", "fix"):
                response = client.post(
                    f"/api/v1/tutor/{path}", data=_form("demo.py", PY_CODE)
                )
                assert response.status_code == 503
                assert "LLM__API_KEY" in response.json()["detail"]
    finally:
        get_settings.cache_clear()


def test_frontend_is_served(sqlite_path: Path, tutor_app: object) -> None:
    """前端页面与静态资源应能被访问，根路径重定向过去。"""
    with TestClient(tutor_app) as client:  # type: ignore[arg-type]
        page = client.get("/ui/index.html")
        root = client.get("/", follow_redirects=False)
        css = client.get("/ui/css/style.css")
        js = client.get("/ui/js/app.js")

    assert page.status_code == 200
    assert "AI 代码导师" in page.text
    assert root.status_code in (302, 307)
    assert css.status_code == 200
    assert js.status_code == 200


def test_openapi_exposes_tutor_routes(sqlite_path: Path, tutor_app: object) -> None:
    with TestClient(tutor_app) as client:  # type: ignore[arg-type]
        paths = client.get("/openapi.json").json()["paths"]

    for path in ("/api/v1/tutor/analyze", "/api/v1/tutor/check",
                 "/api/v1/tutor/comment", "/api/v1/tutor/fix",
                 "/api/v1/tutor/diagnose", "/api/v1/tutor/errors",
                 "/api/v1/tutor/status"):
        assert path in paths
    # 历史记录功能已删除，这三条路由不该再出现
    assert "/api/v1/tutor/history" not in paths


# ---------------------------------------------------------------------------
# 代码体积上限：两条提交路径都必须拦，而且必须是中文提示
# ---------------------------------------------------------------------------
def test_pasted_code_over_limit_gets_a_chinese_413(
    sqlite_path: Path, tutor_app: object
) -> None:
    """粘贴的代码超标时给中文 413，而不是 multipart 解析器的英文内部错误。

    真实反馈就是这样来的：学生粘了一大段代码点「生成注释」，界面上显示
    "生成失败：Part exceeded maximum size of 1024KB." ——
    那是 Starlette 解析 multipart 普通字段时抛的，跟"代码太大"这件事
    完全对不上，学生不知道该干什么。

    业务上限本来是 512KB 且早有中文提示，但**只挡了上传文件那条路**，
    粘贴这条路既绕过上限、又会在超过 1MB 时先撞上 Starlette。
    """
    from app.core.limits import MAX_CODE_BYTES

    code = "x = 1\n" * ((MAX_CODE_BYTES + 4096) // 6)
    with TestClient(tutor_app) as client:  # type: ignore[arg-type]
        response = client.post(
            "/api/v1/tutor/comment",
            files={"code": (None, code), "filename": (None, "big.py")},
        )
    assert response.status_code == 413
    detail = response.json()["detail"]
    assert "代码太大" in detail
    assert "上限" in detail
    # 关键：不能把第三方库的英文原文漏给学生
    assert "exceeded maximum size" not in detail


def test_pasted_code_just_under_limit_is_accepted(
    sqlite_path: Path, tutor_app: object
) -> None:
    """正好在限内的代码不能被误拦（否则正常作业全用不了）。"""
    from app.core.limits import MAX_CODE_BYTES

    code = "x = 1\n" * ((MAX_CODE_BYTES - 4096) // 6)
    with TestClient(tutor_app) as client:  # type: ignore[arg-type]
        response = client.post(
            "/api/v1/tutor/comment",
            files={"code": (None, code), "filename": (None, "ok.py")},
        )
    # 体积检查放行（后面因为假模型正常工作，返回 200）
    assert response.status_code == 200, response.text


def test_uploaded_file_over_limit_keeps_the_chinese_413(
    sqlite_path: Path, tutor_app: object
) -> None:
    """上传文件那条路的中文提示不能被改坏（两条路现在共用同一句文案）。"""
    from app.core.limits import MAX_CODE_BYTES

    payload = b"x = 1\n" * ((MAX_CODE_BYTES + 4096) // 6)
    with TestClient(tutor_app) as client:  # type: ignore[arg-type]
        response = client.post(
            "/api/v1/tutor/comment",
            files={"file": ("big.py", payload, "text/x-python")},
        )
    assert response.status_code == 413
    assert "代码太大" in response.json()["detail"]


def test_oversized_body_is_rejected_before_parsing(
    sqlite_path: Path, tutor_app: object
) -> None:
    """超过请求体上限的提交，要在 Starlette 解析之前就被挡掉。

    这条防的是"漏网"：只要请求体大到会被 multipart 解析器拒绝，
    学生就会看到那句英文。所以解析前必须按 Content-Length 先拦一道。
    """
    from app.core.limits import MAX_AI_BODY_BYTES

    with TestClient(tutor_app) as client:  # type: ignore[arg-type]
        response = client.post(
            "/api/v1/tutor/comment",
            files={
                "code": (None, "x" * (MAX_AI_BODY_BYTES + 1024)),
                "filename": (None, "huge.py"),
            },
        )
    assert response.status_code == 413
    detail = response.json()["detail"]
    assert "代码太大" in detail
    assert "exceeded maximum size" not in detail


def test_limits_are_shared_between_main_and_endpoints() -> None:
    """上限常量只有一个来源，两处引用同一份，不会各写一个数字。"""
    from app.core import limits
    from app.main import create_app  # noqa: F401  （导入即验证没有循环依赖）

    assert limits.MAX_AI_BODY_BYTES > limits.MAX_CODE_BYTES
    assert limits.AI_FORM_PATH_PREFIX.startswith("/api/")
    # 提示文案里必须带上"怎么办"，而不只是报一个数字
    message = limits.too_large_detail(2 * 1024 * 1024)
    assert "2" in message and "上限" in message and "粘" in message
