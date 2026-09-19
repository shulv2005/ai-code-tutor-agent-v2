"""学生错题本的 ORM 模型。

面向「错题本 + 举一反三」功能：每次 AI 诊断判定代码**有错**时，把这次错误
按知识点归档到某个学生名下，供后续诊断时回查历史、聚合薄弱环节。

与 `tutor_records` 的分工（刻意分两张表，不要合并）：

| 表 | 记什么 | 读法 |
| --- | --- | --- |
| `tutor_records` | **流水账**：学生每点一次按钮就一条，好的坏的都记 | 「我最近看过哪些代码」 |
| `student_error_logs` | **错题本**：只记真正出错的那次，字段面向复习 | 「我老是错在哪」 |

合并成一张表的代价：查「最近 5-10 条错题」必须按状态过滤流水账，
历史列表还会被毫无信息量的「无错误」记录稀释；而错题本要按知识点聚合，
在流水账上做聚合等于每次都在全表上扫。所以分开建。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.repository import utc_now


class StudentErrorLog(Base):
    """一条错题记录。

    字段与学生档案的对应关系（需求指定 8 个字段，这里全部具备，
    另外补了几个工程上必需的）：

        必需：id / student_id / code_snippet / language /
              error_type / knowledge_point / ai_feedback / created_at
        补充：knowledge_points_json（一次可能涉及多个知识点，
              knowledge_point 存"主知识点"用于聚合与索引，
              完整列表存 JSON 供展示，避免再建一张关联表）
              filename / source / record_id / model / trace_id
    """

    __tablename__ = "student_error_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # ---------- 谁错的 ----------
    # 用字符串而不是外键：本项目目前没有用户表（学生端不做登录），
    # 学号由前端输入框提供。将来接入统一认证时，这里换成 user_id 外键即可，
    # 字符串列可以直接容纳 UUID / 学号两种形态。
    student_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)

    # ---------- 错在什么代码上 ----------
    code_snippet: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    filename: Mapped[str | None] = mapped_column(String(255), default=None)

    # ---------- 错在哪 ----------
    # 语法错误 / 逻辑错误 / 运行时错误 / 规范问题 …（由模型判定，此处只存字符串）
    error_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    # 主知识点：取模型返回 knowledge_points 的第一条，用于「易错知识点 Top N」聚合
    knowledge_point: Mapped[str] = mapped_column(String(255), index=True, nullable=False)
    # 完整知识点列表的 JSON 字符串；解析失败时前端退回只显示 knowledge_point
    knowledge_points_json: Mapped[str | None] = mapped_column(Text, default=None)

    # ---------- AI 说了什么 ----------
    ai_feedback: Mapped[str] = mapped_column(Text, nullable=False)

    # ---------- 工程字段 ----------
    # 这条错题是哪次诊断产生的：diagnose（错题本接口）/ check（旧的 AI 检测）
    source: Mapped[str] = mapped_column(String(16), default="diagnose", nullable=False)
    # 关联的流水账记录，便于"从错题跳回当时那份代码"
    record_id: Mapped[int | None] = mapped_column(Integer, default=None)
    model: Mapped[str | None] = mapped_column(String(128), default=None)
    trace_id: Mapped[str | None] = mapped_column(String(64), default=None)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, index=True, nullable=False
    )

    __table_args__ = (
        # 主力查询：「某学生最近 N 条错题」——按学生过滤 + 时间倒序，
        # 这条组合索引能让它走索引扫描而不是全表排序。
        Index("ix_student_error_logs_student_created", "student_id", "created_at"),
        # 次要查询：「某学生最常错的知识点」——按学生 + 知识点分组计数。
        Index("ix_student_error_logs_student_kp", "student_id", "knowledge_point"),
    )

    def knowledge_points(self) -> list[str]:
        """把 JSON 字符串还原成知识点列表；坏了就退回 [主知识点]。"""
        import json

        if not self.knowledge_points_json:
            return [self.knowledge_point]
        try:
            items = json.loads(self.knowledge_points_json)
        except (TypeError, ValueError):
            return [self.knowledge_point]
        if not isinstance(items, list):
            return [self.knowledge_point]
        cleaned = [str(item).strip() for item in items if str(item).strip()]
        return cleaned or [self.knowledge_point]

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"<StudentErrorLog id={self.id} student={self.student_id} "
            f"type={self.error_type} kp={self.knowledge_point}>"
        )


__all__ = ["StudentErrorLog"]
