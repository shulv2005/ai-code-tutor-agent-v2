"""「错题本 + 举一反三」功能测试。

覆盖四层：
  1. 提示词：System Prompt 是否逐字使用、User Prompt 模板拼接是否正确；
  2. Agent：模型输出的归一化（同义词、缺字段、解析失败）；
  3. 服务层：错题本的写入 / 回查 / 知识点聚合；
  4. API 全链路：/diagnose、/errors、兼容别名 /api/analyze。

大模型全部用可编排的假客户端替代，因此整条链路离线可测、结果确定。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.tutor_agent import (
    DIAGNOSE_SYSTEM,
    TutorAgent,
    build_diagnose_user_prompt,
    format_error_history,
    normalize_diagnose_payload,
    normalize_diagnose_status,
)
from app.api.deps import get_tutor_agent
from app.core.config import get_settings
from app.core.llm_client import LLMMessage, LLMResponse, LLMUsage
from app.main import create_app
from app.services.tutor_service import UNKNOWN_KNOWLEDGE_POINT, TutorService

PY_CODE = "def add(a, b):\n    return a - b\n"
OK_CODE = "def add(a, b):\n    return a + b\n"


# ---------------------------------------------------------------------------
# 假客户端：按提示词路由，并记录每次调用的 messages
# ---------------------------------------------------------------------------
class DiagnoseFakeLLM:
    """可编排的假 LLM 客户端（实现 `LLMClient` 协议）。"""

    def __init__(self, *replies: str) -> None:
        # 最后一个回复会被重复使用，方便"多轮调用"的用例
        self.replies = list(replies) or [""]
        self.calls: list[list[LLMMessage]] = []

    @property
    def model(self) -> str:
        return "fake-diagnose"

    @property
    def configured(self) -> bool:
        return True

    @property
    def last_user_content(self) -> str:
        for message in reversed(self.calls[-1]):
            if message.role == "user":
                return message.content
        return ""

    @property
    def last_system_content(self) -> str:
        for message in self.calls[-1]:
            if message.role == "system":
                return message.content
        return ""

    async def chat(self, messages: list[LLMMessage], **_: object) -> LLMResponse:
        self.calls.append(list(messages))
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        return LLMResponse(
            content=self.replies[index],
            model="fake-diagnose",
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=120, completion_tokens=80, total_tokens=200),
            latency_ms=1.0,
            attempts=1,
        )

    async def aclose(self) -> None:
        return None


def _reply(*, status: str, error_type: str, points: list[str], title: str = "运算符练习") -> str:
    """拼一段符合提示词契约的模型回复。"""
    return json.dumps(
        {
            "analysis": {
                "status": status,
                "error_type": error_type,
                "message": "函数名叫 add，却用了减号。" if status == "error" else "写得很规范，继续保持！",
                "knowledge_points": points,
            },
            "wrong_question_book": {
                "history_summary": "你最近在「函数返回值」上错了 2 次。",
                "new_exercise": {
                    "question_title": title,
                    "question_description": "实现 subtract(a, b) 返回两数之差。",
                    "hint": "注意参数的顺序，不要写反。",
                },
                "knowledge_explanation": "## 运算符\n\n```python\nreturn a + b\n```\n",
            },
        },
        ensure_ascii=False,
    )


ERROR_REPLY = _reply(status="error", error_type="逻辑错误", points=["函数返回值", "运算符使用"])
SUCCESS_REPLY = _reply(status="success", error_type="无错误", points=[])
UNPARSEABLE_REPLY = "这段代码看起来没问题，我就不输出 JSON 了。"


# ---------------------------------------------------------------------------
# 1. 提示词
# ---------------------------------------------------------------------------
def test_system_prompt_is_used_verbatim() -> None:
    """需求的 System Prompt 必须逐字使用，这里锁死几处关键约束。"""
    assert "# Role" in DIAGNOSE_SYSTEM
    assert "你是一个专业的 AI 代码导师与出题专家" in DIAGNOSE_SYSTEM
    assert '"status": "success" 或 "error"' in DIAGNOSE_SYSTEM
    assert '"history_summary"' in DIAGNOSE_SYSTEM
    assert '"knowledge_explanation"' in DIAGNOSE_SYSTEM
    # 两次"禁止 markdown 代码块"的强调不能被删掉
    assert DIAGNOSE_SYSTEM.count("```json") == 2
    # 四条 Constraints 齐全
    assert "# Constraints" in DIAGNOSE_SYSTEM
    for marker in (
        "1. 如果提交的代码完全正确",
        "2. 如果提交的代码有错误",
        "3. knowledge_explanation 必须通俗易懂",
        "4. 只能输出纯 JSON 字符串",
    ):
        assert marker in DIAGNOSE_SYSTEM, f"System Prompt 缺少约束：{marker}"


def test_user_prompt_template_contains_all_sections() -> None:
    prompt = build_diagnose_user_prompt(
        language="python", code=PY_CODE, history_errors_data="1. [2024-05-01] ……"
    )
    assert "【当前提交的代码】" in prompt
    assert "编程语言：Python" in prompt
    assert PY_CODE in prompt
    assert "【学生历史错题记录】" in prompt
    assert "1. [2024-05-01] ……" in prompt
    assert "【任务要求】" in prompt
    # 需求模板最后一条的强调句
    assert "只输出纯 JSON 字符串" in prompt


def test_format_error_history_empty_returns_hint() -> None:
    text = format_error_history([])
    assert "暂无历史错题记录" in text
    # 空历史时必须明确告诉模型"这是第一次"，否则它会自己编几条历史错题
    assert "第一次提交" in text


def test_format_error_history_renders_fields_and_numbers() -> None:
    from datetime import datetime

    records = [
        SimpleNamespace(
            created_at=datetime(2024, 5, 1, 10, 0),
            language="python",
            error_type="逻辑错误",
            knowledge_point="循环边界",
            code_snippet="for i in range(1, n):",
            ai_feedback="少遍历了最后一个元素",
        ),
        SimpleNamespace(
            created_at=datetime(2024, 5, 2, 10, 0),
            language="c",
            error_type="语法错误",
            knowledge_point="分号",
            code_snippet="int a = 1",
            ai_feedback="缺少分号",
        ),
    ]
    text = format_error_history(records)
    assert text.startswith("1. [2024-05-01]")
    assert "2. [2024-05-02]" in text
    assert "循环边界" in text and "分号" in text
    assert "AI 反馈：少遍历了最后一个元素" in text


def test_format_error_history_truncates_long_code() -> None:
    long_code = "x = 1\n" * 500  # 远超 1200 字符
    text = format_error_history(
        [
            SimpleNamespace(
                created_at=None,
                language="python",
                error_type="逻辑错误",
                knowledge_point="变量作用域",
                code_snippet=long_code,
                ai_feedback="反馈",
            )
        ]
    )
    # 截断后整体长度受控，且保留了"中间内容略"的提示
    assert "中间内容略" in text
    assert len(text) < len(long_code)


def test_format_error_history_handles_missing_attributes() -> None:
    """字段缺失时不能抛异常——错题本里历史的脏数据不该让整个诊断 500。"""
    text = format_error_history([SimpleNamespace()])
    assert "未分类错误" in text
    assert "时间未知" in text


def test_format_error_history_respects_limit() -> None:
    records = [
        SimpleNamespace(
            created_at=None, language="python", error_type="逻辑错误",
            knowledge_point=f"kp{i}", code_snippet="pass", ai_feedback="f",
        )
        for i in range(10)
    ]
    assert format_error_history(records, limit=3).count("AI 反馈") == 3


# ---------------------------------------------------------------------------
# 2. Agent 归一化
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw", ["success", "SUCCESS", "ok", "通过", "无错误", "correct", " Success "]
)
def test_normalize_status_success_synonyms(raw: str) -> None:
    assert normalize_diagnose_status(raw) == "success"


@pytest.mark.parametrize("raw", ["error", "语法错误", "", None, "failed", "未知值"])
def test_normalize_status_defaults_to_error(raw: object) -> None:
    """收敛不了一律按 error：宁多记一道题，不漏一道错题。"""
    assert normalize_diagnose_status(raw) == "error"


def test_normalize_payload_fills_missing_fields() -> None:
    payload = normalize_diagnose_payload({"analysis": {"status": "ok"}})
    assert payload["analysis"]["status"] == "success"
    assert payload["analysis"]["error_type"] == "无错误"
    assert payload["analysis"]["knowledge_points"] == []
    # 代码没问题时 wrong_question_book 也不能是空的（提示词 Constraint 1 要求照样出题）
    book = payload["wrong_question_book"]
    assert set(book["new_exercise"]) == {"question_title", "question_description", "hint"}
    assert all(value == "" for value in book["new_exercise"].values())


def test_normalize_payload_dedupes_knowledge_points() -> None:
    payload = normalize_diagnose_payload(
        {"analysis": {"status": "error", "knowledge_points": ["循环", "循环", "  ", "边界"]}}
    )
    assert payload["analysis"]["knowledge_points"] == ["循环", "边界"]


def test_normalize_payload_corrects_bad_error_type() -> None:
    payload = normalize_diagnose_payload({"analysis": {"status": "error"}})
    assert payload["analysis"]["error_type"] == "未分类错误"


async def test_agent_diagnose_uses_verbatim_system_prompt() -> None:
    fake = DiagnoseFakeLLM(ERROR_REPLY)
    agent = TutorAgent(get_settings(), fake)  # type: ignore[arg-type]

    result = await agent.diagnose(PY_CODE, filename="a.py", language="python", history_text="")

    assert fake.last_system_content == DIAGNOSE_SYSTEM
    assert result.parsed is True
    assert result.payload["analysis"]["status"] == "error"
    assert result.payload["analysis"]["knowledge_points"] == ["函数返回值", "运算符使用"]


async def test_agent_diagnose_marks_unparseable_output() -> None:
    """模型没返回 JSON 时必须如实标记，而不是伪造一个"无错误"结论。"""
    agent = TutorAgent(get_settings(), DiagnoseFakeLLM(UNPARSEABLE_REPLY))  # type: ignore[arg-type]

    result = await agent.diagnose(PY_CODE, filename="a.py", language="python", history_text="")

    assert result.parsed is False
    assert result.payload == {}
    assert result.warnings


async def test_agent_diagnose_warns_when_no_markdown_example() -> None:
    reply = _reply(status="error", error_type="逻辑错误", points=["循环"])
    payload = json.loads(reply)
    payload["wrong_question_book"]["knowledge_explanation"] = "循环要小心边界。"
    agent = TutorAgent(
        get_settings(), DiagnoseFakeLLM(json.dumps(payload, ensure_ascii=False))
    )  # type: ignore[arg-type]

    result = await agent.diagnose(PY_CODE, filename="a.py", language="python", history_text="")

    assert any("Markdown" in warning for warning in result.warnings)


# ---------------------------------------------------------------------------
# 3. 服务层
# ---------------------------------------------------------------------------
# 注意这里的 `_db()` 一定要在结束时 dispose_engine()。
# `app.core.database` 把引擎缓存在模块级全局变量里，而 `sqlite_path` 夹具
# 只清了 Settings 缓存、没有丢弃引擎；一旦某个用例直接调 init_db() 又不释放，
# 下一个用例拿到的就是**上一个用例的数据库**——症状是"表明明建了却查不到"，
# 且失败原因与本用例代码完全无关。通过 TestClient 的用例不受影响
# （lifespan 关闭时会 dispose），只有这种直连数据库的用例需要自己收尾。
@asynccontextmanager
async def _db() -> AsyncIterator[AsyncSession]:
    from app.core.database import dispose_engine, get_session, init_db

    await init_db()
    try:
        async with get_session() as session:
            yield session
    finally:
        await dispose_engine()


async def _save(session: AsyncSession, student_id: str, *, kp: str, error_type: str = "逻辑错误"):
    return await TutorService.save_error(
        session,
        student_id=student_id,
        code_snippet=PY_CODE,
        language="python",
        error_type=error_type,
        knowledge_points=[kp, "附加知识点"],
        ai_feedback="AI 反馈",
    )


async def test_service_save_and_query_error(sqlite_path: Path) -> None:
    async with _db() as session:
        saved = await _save(session, "S1", kp="循环边界")
        assert saved is not None
        assert saved.knowledge_point == "循环边界"
        # 完整知识点列表以 JSON 存下来，回读时能还原
        assert saved.knowledge_points() == ["循环边界", "附加知识点"]

        recent = await TutorService.recent_errors(session, student_id="S1", limit=5)
        assert [item.id for item in recent] == [saved.id]
        assert await TutorService.count_errors(session, student_id="S1") == 1


async def test_service_falls_back_to_placeholder_knowledge_point(sqlite_path: Path) -> None:
    """一个知识点都提取不出来时，用占位值而不是空串——空串会污染聚合统计。"""
    async with _db() as session:
        saved = await TutorService.save_error(
            session,
            student_id="S2",
            code_snippet=PY_CODE,
            language="python",
            error_type="逻辑错误",
            knowledge_points=["  ", ""],
            ai_feedback="AI 反馈",
        )
        assert saved is not None
        assert saved.knowledge_point == UNKNOWN_KNOWLEDGE_POINT
        assert saved.knowledge_points() == [UNKNOWN_KNOWLEDGE_POINT]


async def test_service_weak_points_aggregation(sqlite_path: Path) -> None:
    async with _db() as session:
        await _save(session, "S3", kp="循环边界")
        await _save(session, "S3", kp="循环边界")
        await _save(session, "S3", kp="数组下标")
        await _save(session, "OTHER", kp="别人的错题")

        weak = await TutorService.weak_points(session, student_id="S3", limit=5)

    assert weak[0] == ("循环边界", 2)
    assert ("数组下标", 1) in weak
    # 别的学生的错题不能混进聚合结果
    assert all(name != "别人的错题" for name, _ in weak)


async def test_service_recent_errors_is_per_student_and_newest_first(sqlite_path: Path) -> None:
    async with _db() as session:
        for index in range(3):
            await _save(session, "S4", kp=f"kp{index}")
        await _save(session, "S5", kp="别人的")

        recent = await TutorService.recent_errors(session, student_id="S4", limit=2)

    assert [item.knowledge_point for item in recent] == ["kp2", "kp1"]


async def test_service_delete_error_is_scoped_by_student(sqlite_path: Path) -> None:
    """删别人的错题必须失败——否则任何学生都能凭 id 清掉同班同学的错题。"""
    async with _db() as session:
        saved = await _save(session, "OWNER", kp="循环")
        assert saved is not None

        assert await TutorService.delete_error(
            session, error_id=saved.id, student_id="ATTACKER"
        ) is False
        assert await TutorService.count_errors(session, student_id="OWNER") == 1

        assert await TutorService.delete_error(
            session, error_id=saved.id, student_id="OWNER"
        ) is True
        assert await TutorService.count_errors(session, student_id="OWNER") == 0


# ---------------------------------------------------------------------------
# 4. API 全链路
# ---------------------------------------------------------------------------
@pytest.fixture()
def diagnose_app() -> Iterator[tuple[object, DiagnoseFakeLLM]]:
    """注入假 LLM 的应用 + 那个假客户端（用例要断言"提示词里带了历史错题"）。"""
    app = create_app()
    settings = get_settings()
    fake = DiagnoseFakeLLM(ERROR_REPLY)
    app.dependency_overrides[get_tutor_agent] = lambda: TutorAgent(settings, fake)  # type: ignore[arg-type]
    yield app, fake
    app.dependency_overrides.clear()


def _diagnose(client: TestClient, *, student_id: str = "S2024001", code: str = PY_CODE,
              filename: str = "demo.py") -> object:
    return client.post(
        "/api/v1/tutor/diagnose",
        data={"code": code, "filename": filename},
        headers={"X-Student-Id": student_id},
    )


def test_table_schema_has_required_columns(sqlite_path: Path, diagnose_app) -> None:
    """需求的 8 个字段必须真的落到表里（对着建表结果验证，而不是对着模型代码）。"""
    app, _ = diagnose_app
    with TestClient(app) as client:
        assert client.get("/api/v1/tutor/status").status_code == 200

    con = sqlite3.connect(sqlite_path)
    try:
        columns = {row[1] for row in con.execute("PRAGMA table_info(student_error_logs)")}
        indexes = {row[1] for row in con.execute("PRAGMA index_list(student_error_logs)")}
    finally:
        con.close()

    assert {
        "id", "student_id", "code_snippet", "language",
        "error_type", "knowledge_point", "ai_feedback", "created_at",
    } <= columns
    assert "ix_student_error_logs_student_created" in indexes


def test_diagnose_error_flow_logs_to_error_book(sqlite_path: Path, diagnose_app) -> None:
    app, _ = diagnose_app
    with TestClient(app) as client:
        response = _diagnose(client)
        errors = client.get(
            "/api/v1/tutor/errors", headers={"X-Student-Id": "S2024001"}
        ).json()

    assert response.status_code == 200, response.text
    body = response.json()

    # 上半屏
    assert body["analysis"]["status"] == "error"
    assert body["analysis"]["error_type"] == "逻辑错误"
    assert body["analysis"]["knowledge_points"] == ["函数返回值", "运算符使用"]
    assert body["analysis"]["message"]
    # 下半屏
    book = body["wrong_question_book"]
    assert book["history_summary"]
    assert book["new_exercise"]["question_title"] == "运算符练习"
    assert book["new_exercise"]["hint"]
    assert "```python" in book["knowledge_explanation"]

    # 归档
    assert body["logged"] is True
    assert body["error_log_id"]
    assert body["student_id"] == "S2024001"

    # 列表接口能查到同一条
    assert errors["total"] == 1
    assert errors["items"][0]["knowledge_point"] == "函数返回值"
    assert errors["items"][0]["code_snippet"] == PY_CODE
    assert errors["weak_points"][0] == {"knowledge_point": "函数返回值", "count": 1}


def test_diagnose_success_does_not_log(sqlite_path: Path, diagnose_app) -> None:
    """代码没问题时绝不能写错题本，否则「易错知识点」统计会被灌水。"""
    app, fake = diagnose_app
    fake.replies = [SUCCESS_REPLY]
    with TestClient(app) as client:
        body = _diagnose(client, code=OK_CODE).json()
        errors = client.get("/api/v1/tutor/errors", headers={"X-Student-Id": "S2024001"}).json()

    assert body["analysis"]["status"] == "success"
    assert body["logged"] is False
    assert body["error_log_id"] is None
    # 但新题目照样要给（提示词 Constraint 1）
    assert body["wrong_question_book"]["new_exercise"]["question_title"] == "运算符练习"
    assert errors["total"] == 0


def test_diagnose_feeds_history_into_prompt(sqlite_path: Path, diagnose_app) -> None:
    """第二次诊断时，提示词里必须带上第一次的错题。"""
    app, fake = diagnose_app
    with TestClient(app) as client:
        first = _diagnose(client).json()
        assert first["history_count"] == 0          # 第一次没有历史
        assert "暂无历史错题记录" in fake.last_user_content

        second = _diagnose(client).json()

    assert second["history_count"] == 1             # 第二次带上了刚才那条
    assert second["history_total"] == 2             # 响应里的总数包含本次新记的
    prompt = fake.last_user_content
    assert "函数返回值" in prompt                   # 历史错题的知识点进了提示词
    assert "当时的代码" in prompt                   # 历史错题的代码片段也进了提示词
    assert "函数名叫 add，却用了减号。" in prompt     # 历史错题的 AI 反馈进了提示词


def test_diagnose_isolates_students(sqlite_path: Path, diagnose_app) -> None:
    app, _ = diagnose_app
    with TestClient(app) as client:
        _diagnose(client, student_id="ALICE")
        _diagnose(client, student_id="BOB")
        _diagnose(client, student_id="ALICE")

        alice = client.get("/api/v1/tutor/errors", headers={"X-Student-Id": "ALICE"}).json()
        bob = client.get("/api/v1/tutor/errors", headers={"X-Student-Id": "BOB"}).json()

    assert alice["total"] == 2
    assert bob["total"] == 1
    assert all(item["student_id"] == "ALICE" for item in alice["items"])


def test_diagnose_without_identity_falls_back_to_local(sqlite_path: Path, diagnose_app) -> None:
    """不传任何身份 → 归到本机固定身份 local（页面不需要填学号）。"""
    app, _ = diagnose_app
    with TestClient(app) as client:
        response = client.post(
            "/api/v1/tutor/diagnose", data={"code": PY_CODE, "filename": "demo.py"}
        )
        errors = client.get("/api/v1/tutor/errors").json()

    assert response.status_code == 200, response.text
    assert response.json()["student_id"] == "local"
    # 不带身份也要能查回自己的错题本
    assert errors["student_id"] == "local"
    assert errors["total"] == 1


def test_diagnose_rejects_unknown_language(sqlite_path: Path, diagnose_app) -> None:
    app, _ = diagnose_app
    with TestClient(app) as client:
        response = _diagnose(client, filename="notes.txt")
    assert response.status_code == 400
    assert "语言" in response.json()["detail"]


def test_diagnose_returns_502_on_unparseable_model_output(
    sqlite_path: Path, diagnose_app
) -> None:
    """AI 跑偏时要让学生重试，而不是把解析失败伪装成"代码没问题"。"""
    app, fake = diagnose_app
    fake.replies = [UNPARSEABLE_REPLY]
    with TestClient(app) as client:
        response = _diagnose(client)
        errors = client.get("/api/v1/tutor/errors", headers={"X-Student-Id": "S2024001"}).json()

    assert response.status_code == 502
    assert "重试" in response.json()["detail"]
    assert errors["total"] == 0      # 关键：没有污染错题本


def test_error_book_tolerates_legacy_schema(sqlite_path: Path) -> None:
    """旧数据库里残留的列与表，不能影响新代码的读写。

    为什么值得单独测：这次删掉了 `student_error_logs.record_id` 列与
    `tutor_records` 表，但使用者手上的 `data/app.db` 是**旧结构**——
    SQLAlchemy 的 `create_all` 只建不改，既不会删表也不会删列。
    所以必须确认"旧库里多一列、多一张表"不会让插入与查询失败
    （否则用户升级后一打开页面就报错）。
    """
    import asyncio
    import sqlite3

    # 造一个"旧结构"：错题本表比新模型多一个 record_id 列，外加一张 tutor_records
    con = sqlite3.connect(sqlite_path)
    con.execute(
        """
        CREATE TABLE student_error_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id VARCHAR(64) NOT NULL,
            code_snippet TEXT NOT NULL,
            language VARCHAR(32) NOT NULL,
            filename VARCHAR(255),
            error_type VARCHAR(32) NOT NULL,
            knowledge_point VARCHAR(255) NOT NULL,
            knowledge_points_json TEXT,
            ai_feedback TEXT NOT NULL,
            source VARCHAR(16) NOT NULL DEFAULT 'diagnose',
            record_id INTEGER,
            model VARCHAR(128),
            trace_id VARCHAR(64),
            created_at DATETIME NOT NULL
        )
        """
    )
    con.execute("CREATE TABLE tutor_records (id INTEGER PRIMARY KEY, action VARCHAR(16))")
    con.commit()
    con.close()

    async def _roundtrip() -> list[str]:
        async with _db() as session:
            saved = await _save(session, "LEGACY", kp="循环边界")
            assert saved is not None
            recent = await TutorService.recent_errors(session, student_id="LEGACY", limit=5)
            return [item.knowledge_point for item in recent]

    assert asyncio.run(_roundtrip()) == ["循环边界"]


def test_error_book_works_without_identity(sqlite_path: Path, diagnose_app) -> None:
    """错题本接口不带任何身份参数也能用（单机版页面上没有学号输入框）。"""
    app, _ = diagnose_app
    with TestClient(app) as client:
        response = client.get("/api/v1/tutor/errors")

    assert response.status_code == 200
    assert response.json() == {
        "student_id": "local", "total": 0, "items": [], "weak_points": [],
    }


def test_delete_error_endpoint_scoped_by_student(sqlite_path: Path, diagnose_app) -> None:
    app, _ = diagnose_app
    with TestClient(app) as client:
        error_id = _diagnose(client).json()["error_log_id"]

        # 换个学号删不掉
        assert client.delete(
            f"/api/v1/tutor/errors/{error_id}", headers={"X-Student-Id": "OTHER"}
        ).status_code == 404
        # 本人可以删
        assert client.delete(
            f"/api/v1/tutor/errors/{error_id}", headers={"X-Student-Id": "S2024001"}
        ).status_code == 204
        assert client.delete(
            f"/api/v1/tutor/errors/{error_id}", headers={"X-Student-Id": "S2024001"}
        ).status_code == 404


# ---------------------------------------------------------------------------
# 5. 兼容别名 POST /api/analyze
# ---------------------------------------------------------------------------
def _alias_body(student_id: str = "ALIAS01", language: str = "python", code: str = PY_CODE) -> dict:
    return {"student_id": student_id, "language": language, "code_content": code}


def test_alias_endpoint_works_with_json_body(sqlite_path: Path, diagnose_app) -> None:
    app, _ = diagnose_app
    with TestClient(app) as client:
        response = client.post("/api/analyze", json=_alias_body())
        errors = client.get(
            "/api/v1/tutor/errors", headers={"X-Student-Id": "ALIAS01"}
        ).json()

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["analysis"]["status"] == "error"
    assert body["wrong_question_book"]["new_exercise"]["question_title"] == "运算符练习"
    # 关键：别名走的是同一套逻辑，错题本照样归档
    assert body["logged"] is True
    assert errors["total"] == 1


def test_alias_and_canonical_share_one_error_book(sqlite_path: Path, diagnose_app) -> None:
    """网页端与脚本端必须落到同一个错题本里，否则"结合历史"就断了。"""
    app, _ = diagnose_app
    with TestClient(app) as client:
        client.post("/api/analyze", json=_alias_body(student_id="SHARED"))
        client.post(
            "/api/v1/tutor/diagnose",
            data={"code": PY_CODE, "filename": "demo.py"},
            headers={"X-Student-Id": "SHARED"},
        )
        errors = client.get("/api/v1/tutor/errors", headers={"X-Student-Id": "SHARED"}).json()

    assert errors["total"] == 2


def test_alias_rejects_unsupported_language(sqlite_path: Path, diagnose_app) -> None:
    app, _ = diagnose_app
    with TestClient(app) as client:
        response = client.post("/api/analyze", json=_alias_body(language="rust"))
    assert response.status_code == 400
    assert "语言" in response.json()["detail"]


def test_alias_rejects_empty_code(sqlite_path: Path, diagnose_app) -> None:
    """空代码由 Pydantic 挡在 422，不会走到大模型。"""
    app, _ = diagnose_app
    with TestClient(app) as client:
        response = client.post("/api/analyze", json=_alias_body(code=""))
    assert response.status_code == 422


def test_alias_falls_back_to_python_for_missing_language(sqlite_path: Path, diagnose_app) -> None:
    app, _ = diagnose_app
    with TestClient(app) as client:
        body = {"student_id": "NOLANG", "code_content": PY_CODE}
        response = client.post("/api/analyze", json=body)
    assert response.status_code == 200, response.text
    assert response.json()["language"] == "python"


def test_openapi_exposes_diagnose_routes(sqlite_path: Path, diagnose_app) -> None:
    app, _ = diagnose_app
    with TestClient(app) as client:
        paths = client.get("/openapi.json").json()["paths"]

    for path in ("/api/analyze", "/api/v1/tutor/diagnose", "/api/v1/tutor/errors"):
        assert path in paths, f"缺少路由 {path}"
