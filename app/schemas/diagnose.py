"""「错题本 + 举一反三」功能的 API 契约。

这个模块定义的 JSON 结构与 System Prompt 里约定的输出模板**一一对应**：

    {
      "analysis":            -> DiagnoseAnalysis
      "wrong_question_book": -> WrongQuestionBook
    }

前端拿到后，上半屏渲染 `analysis`、下半屏渲染 `wrong_question_book`，
不需要再做任何字段映射——接口契约与提示词契约保持一致，
可以少掉一整类"模型改了字段名、前端没跟上"的低级 bug。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# 模型判定的两种结果。用 Literal 而不是 str：模型偶尔会返回 "ok" / "pass"
# 之类的近义词，交给归一化逻辑收敛，收敛不了的按 error 处理（宁可多记错题，
# 也不要漏掉——错题本漏记比多记更影响复习）。
DiagnoseStatusLiteral = Literal["success", "error"]


# ---------------------------------------------------------------------------
# 上半屏：当前代码的诊断结果
# ---------------------------------------------------------------------------
class DiagnoseAnalysis(BaseModel):
    """本轮诊断结论，对应 System Prompt 里的 `analysis` 段。"""

    status: DiagnoseStatusLiteral = Field(description="success=代码没问题；error=有错")
    error_type: str = Field(default="无错误", description="语法错误 / 逻辑错误 / 无错误 …")
    message: str = Field(default="", description="针对本次代码的具体诊断")
    knowledge_points: list[str] = Field(
        default_factory=list, description="本次错误涉及的知识点"
    )


# ---------------------------------------------------------------------------
# 下半屏：错题本 & 举一反三
# ---------------------------------------------------------------------------
class NewExercise(BaseModel):
    """AI 针对薄弱点设计的新题目。"""

    question_title: str = Field(default="", description="题目名称")
    question_description: str = Field(default="", description="题目详细描述，含输入输出要求")
    hint: str = Field(default="", description="解题思路提示（不含完整答案）")


class WrongQuestionBook(BaseModel):
    """错题本 / 举一反三，对应 System Prompt 里的 `wrong_question_book` 段。"""

    history_summary: str = Field(
        default="", description="结合历史错题总结出的错误频率与薄弱点"
    )
    new_exercise: NewExercise = Field(default_factory=NewExercise)
    knowledge_explanation: str = Field(
        default="", description="知识点详细解析，**含 Markdown 代码示例**，前端需按 MD 渲染"
    )


# ---------------------------------------------------------------------------
# 薄弱点聚合：错题本按知识点统计的结果
# ---------------------------------------------------------------------------
class KnowledgePointStat(BaseModel):
    """某个知识点错了几次。"""

    knowledge_point: str
    count: int = Field(ge=1)


class ErrorLogRead(BaseModel):
    """错题本里的一条错题。"""

    model_config = ConfigDict(from_attributes=True)

    id: int
    student_id: str
    filename: str | None = None
    language: str
    error_type: str
    knowledge_point: str
    ai_feedback: str
    code_snippet: str
    created_at: datetime


class ErrorLogListResponse(BaseModel):
    """错题本列表：某学生的错题 + 按知识点的聚合统计。"""

    student_id: str
    total: int
    items: list[ErrorLogRead] = Field(default_factory=list)
    weak_points: list[KnowledgePointStat] = Field(
        default_factory=list, description="易错知识点 Top N（按次数倒序）"
    )


# ---------------------------------------------------------------------------
# 完整响应
# ---------------------------------------------------------------------------
class DiagnoseResponse(BaseModel):
    """`POST /api/v1/tutor/diagnose` 的返回体：上半屏 + 下半屏一次给全。"""

    student_id: str
    filename: str
    language: str
    language_label: str

    analysis: DiagnoseAnalysis
    wrong_question_book: WrongQuestionBook

    # 本次喂给模型的错题条数（history_count）与错题本总条数（history_total）。
    # 分开返回是为了让前端能显示"本次参考了 5 条历史错题（共 12 条）"，
    # 学生能直观看到"AI 确实看过我的错题本"，而不是凭空出题。
    history_count: int = 0
    history_total: int = 0
    weak_points: list[KnowledgePointStat] = Field(default_factory=list)

    # 这次错误有没有写进错题本（status=success 时不写，因此为 false）
    logged: bool = False
    error_log_id: int | None = None

    model: str = ""
    duration_ms: float | None = None
    warnings: list[str] = Field(default_factory=list)
    trace_id: str | None = None


# ---------------------------------------------------------------------------
# POST /api/analyze（兼容别名）的请求体
# ---------------------------------------------------------------------------
class DiagnoseRequest(BaseModel):
    """信里约定的请求体：`{ student_id, language, code_content }`。

    只被 `POST /api/analyze` 这个兼容别名使用；规范接口
    `POST /api/v1/tutor/diagnose` 走的是与其余导师接口一致的
    multipart 表单（`code` / `filename`），
    这样前端不用为这一个按钮单独写一套提交逻辑。

    `language` 与 `student_id` 都可省略：
      · 不传 language 按 Python 处理（学生最常用的语言）；
      · 不传 student_id 归到本机固定身份 `local`（单机版页面上没有学号输入框）。
    """

    student_id: str = Field(
        default="local",
        max_length=64,
        description="可选身份标识；不传就是本机固定身份 local",
    )
    language: str = Field(default="python", description="语言标识：python / c / java …")
    code_content: str = Field(min_length=1, description="要诊断的代码")
    filename: str | None = Field(default=None, description="可选：原始文件名")


__all__ = [
    "DiagnoseAnalysis",
    "DiagnoseRequest",
    "DiagnoseResponse",
    "DiagnoseStatusLiteral",
    "ErrorLogListResponse",
    "ErrorLogRead",
    "KnowledgePointStat",
    "NewExercise",
    "WrongQuestionBook",
]
