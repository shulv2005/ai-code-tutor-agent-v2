"""AI 代码导师的记录服务：历史记录的读写与评分等级换算。

与 Agent 分开的原因：Agent 只负责「问模型、拿结果」，
持久化与查询属于服务层职责，分开后 Agent 可以脱离数据库单独测试。

本模块管两张表：
  · `tutor_records`      —— 流水账（每次操作都记）
  · `student_error_logs` —— 错题本（只记出错的，面向复习）
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.error_log import StudentErrorLog
from app.models.tutor import TutorRecord
from app.services.text_utils import count_text_lines

logger = logging.getLogger(__name__)

# 错题本里"知识点的兜底值"。模型偶尔给不出知识点，这时如果存空串，
# 「易错知识点 Top N」的聚合结果里就会出现一条空白项，学生看不懂。
UNKNOWN_KNOWLEDGE_POINT = "未标注知识点"


class TutorService:
    """学生代码学习记录的服务层。"""

    # -- 评分等级 ---------------------------------------------------------
    @staticmethod
    def score_level(score: float) -> str:
        """把 0-100 的分数换算成学生看得懂的等级。"""
        if score >= 90:
            return "优秀"
        if score >= 75:
            return "良好"
        if score >= 60:
            return "及格"
        return "待改进"

    # -- 写入 -------------------------------------------------------------
    @staticmethod
    async def record(
        session: AsyncSession,
        *,
        filename: str,
        language: str,
        code: str,
        action: str,
        summary: str | None = None,
        result_json: str | None = None,
        score: float | None = None,
        model: str | None = None,
        duration_ms: float | None = None,
    ) -> TutorRecord:
        """写入一条学习记录。

        刻意不让异常冒泡：记录失败不应该让学生的检测结果丢失，
        因此写失败时只告警，仍返回一个未落库的对象。
        """
        from app.core.trace import current_trace_id

        item = TutorRecord(
            filename=filename,
            language=language,
            code=code,
            line_count=count_text_lines(code),
            action=action,
            score=score,
            summary=summary,
            result_json=result_json,
            model=model,
            duration_ms=duration_ms,
            trace_id=current_trace_id(),
        )
        try:
            session.add(item)
            await session.commit()
            await session.refresh(item)
        except Exception:  # noqa: BLE001 - 记不上不该影响主流程
            logger.warning("写入导师记录失败", exc_info=True)
            await session.rollback()
        return item

    # -- 查询 -------------------------------------------------------------
    @staticmethod
    async def list_records(
        session: AsyncSession,
        *,
        limit: int = 20,
        offset: int = 0,
        filename: str | None = None,
        action: str | None = None,
    ) -> tuple[list[TutorRecord], int]:
        """分页查询历史记录，按时间倒序。返回 (记录列表, 总数)。"""
        conditions = []
        if filename:
            conditions.append(TutorRecord.filename == filename)
        if action:
            conditions.append(TutorRecord.action == action)

        count_stmt = select(func.count(TutorRecord.id))
        list_stmt = select(TutorRecord).order_by(TutorRecord.created_at.desc())
        for condition in conditions:
            count_stmt = count_stmt.where(condition)
            list_stmt = list_stmt.where(condition)

        total = (await session.execute(count_stmt)).scalar_one()
        rows = (
            await session.execute(list_stmt.limit(limit).offset(offset))
        ).scalars().all()
        return list(rows), total

    @staticmethod
    async def get_record(session: AsyncSession, record_id: int) -> TutorRecord | None:
        """按主键取一条记录。"""
        return await session.get(TutorRecord, record_id)

    @staticmethod
    async def delete_record(session: AsyncSession, record_id: int) -> bool:
        """删除一条记录；不存在时返回 False。"""
        result = await session.execute(
            delete(TutorRecord).where(TutorRecord.id == record_id)
        )
        await session.commit()
        return bool(result.rowcount)

    # =======================================================================
    # 错题本（student_error_logs）
    # =======================================================================
    @staticmethod
    async def save_error(
        session: AsyncSession,
        *,
        student_id: str,
        code_snippet: str,
        language: str,
        error_type: str,
        knowledge_points: list[str],
        ai_feedback: str,
        filename: str | None = None,
        source: str = "diagnose",
        record_id: int | None = None,
        model: str | None = None,
    ) -> StudentErrorLog | None:
        """把一个错误写进错题本。

        与 `record()` 一样**不让异常冒泡**：写错题本失败不应该让学生
        连诊断结果都看不到。失败时返回 None，调用方据此把响应里的
        `logged` 置为 False。

        参数:
            knowledge_points: AI 提取的全部知识点。列表第一条会作为
                「主知识点」单独存一列，用于按知识点聚合统计
                （在 JSON 列上做 GROUP BY 需要数据库支持 JSON 函数，
                SQLite / MySQL 的写法还不一样，不如冗余一列）。
        """
        from app.core.trace import current_trace_id

        points = [str(item).strip() for item in knowledge_points if str(item).strip()]
        primary = points[0] if points else UNKNOWN_KNOWLEDGE_POINT

        item = StudentErrorLog(
            student_id=student_id,
            code_snippet=code_snippet,
            language=language,
            filename=filename,
            error_type=error_type or "未分类错误",
            knowledge_point=primary,
            knowledge_points_json=json.dumps(points, ensure_ascii=False),
            ai_feedback=ai_feedback,
            source=source,
            record_id=record_id,
            model=model,
            trace_id=current_trace_id(),
        )
        try:
            session.add(item)
            await session.commit()
            await session.refresh(item)
        except Exception:  # noqa: BLE001 - 记不上不该影响主流程
            logger.warning("写入错题本失败", exc_info=True)
            await session.rollback()
            return None
        return item

    @staticmethod
    async def recent_errors(
        session: AsyncSession, *, student_id: str, limit: int = 8
    ) -> list[StudentErrorLog]:
        """取某学生最近的 N 条错题，按时间**倒序**（最新的在前）。

        倒序而不是正序：喂给模型时最新的错误更能代表"当前卡在哪"，
        而且 prompt 里一旦超长被截断，先丢掉的应该是很久以前的错题。
        """
        stmt = (
            select(StudentErrorLog)
            .where(StudentErrorLog.student_id == student_id)
            .order_by(StudentErrorLog.created_at.desc(), StudentErrorLog.id.desc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    async def count_errors(session: AsyncSession, *, student_id: str) -> int:
        """某学生错题本里的总条数（用于前端显示"共 N 条"）。"""
        stmt = select(func.count(StudentErrorLog.id)).where(
            StudentErrorLog.student_id == student_id
        )
        return int((await session.execute(stmt)).scalar_one())

    @staticmethod
    async def weak_points(
        session: AsyncSession, *, student_id: str, limit: int = 5
    ) -> list[tuple[str, int]]:
        """聚合易错知识点，返回 [(知识点, 次数)]，按次数倒序。

        这是错题本相对流水账的核心价值：学生看到的不是"我错了 20 次"，
        而是"循环边界错了 6 次、数组下标错了 4 次"。
        """
        stmt = (
            select(StudentErrorLog.knowledge_point, func.count(StudentErrorLog.id))
            .where(StudentErrorLog.student_id == student_id)
            .group_by(StudentErrorLog.knowledge_point)
            .order_by(func.count(StudentErrorLog.id).desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()
        return [(str(name), int(count)) for name, count in rows]

    @staticmethod
    async def list_errors(
        session: AsyncSession,
        *,
        student_id: str,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[StudentErrorLog], int]:
        """分页列出某学生的错题，按时间倒序。返回 (列表, 总数)。"""
        condition = StudentErrorLog.student_id == student_id
        total = int(
            (
                await session.execute(
                    select(func.count(StudentErrorLog.id)).where(condition)
                )
            ).scalar_one()
        )
        stmt = (
            select(StudentErrorLog)
            .where(condition)
            .order_by(StudentErrorLog.created_at.desc(), StudentErrorLog.id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        return rows, total

    @staticmethod
    async def delete_error(
        session: AsyncSession, *, error_id: int, student_id: str | None = None
    ) -> bool:
        """删除一条错题。

        `student_id` 传了就会一起作为过滤条件——**多租户场景下这是必须的**，
        否则任何学生都能凭 id 删掉别人的错题。前端总会传，默认 None
        只是为了兼容管理端"按 id 直接删"的用法。
        """
        condition = StudentErrorLog.id == error_id
        if student_id is not None:
            condition = condition & (StudentErrorLog.student_id == student_id)
        result = await session.execute(delete(StudentErrorLog).where(condition))
        await session.commit()
        return bool(result.rowcount)


__all__ = ["TutorService"]
