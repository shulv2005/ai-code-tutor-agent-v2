"""AI 代码导师的服务层：错题本的读写、知识点聚合，以及评分等级换算。

与 Agent 分开的原因：Agent 只负责「问模型、拿结果」，
持久化与查询属于服务层职责，分开后 Agent 可以脱离数据库单独测试。

原先这里还管一张 `tutor_records` 流水账（每次操作都记一条）。它的
**唯一读取方**是页面左下角的「历史记录」列表，那个功能已删除，
所以这条写入路径也一并移除——留着只会写一堆谁也读不到的行。

错题本（`student_error_logs`）不受影响：它自己就存了代码、知识点与 AI 反馈，
不需要靠流水账才能工作。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.error_log import StudentErrorLog

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
        session: AsyncSession,
        *,
        student_id: str,
        limit: int = 5,
        since: datetime | None = None,
    ) -> list[tuple[str, int]]:
        """聚合易错知识点，返回 [(知识点, 次数)]，按次数倒序。

        这是错题本相对流水账的核心价值：学生看到的不是"我错了 20 次"，
        而是"循环边界错了 6 次、数组下标错了 4 次"。

        `since` 用于学习报告的"最近 N 天"口径；不传就是全部历史
        （前端错题本卡片要的正是全部历史）。
        """
        condition = StudentErrorLog.student_id == student_id
        if since is not None:
            condition = condition & (StudentErrorLog.created_at >= since)
        stmt = (
            select(StudentErrorLog.knowledge_point, func.count(StudentErrorLog.id))
            .where(condition)
            .group_by(StudentErrorLog.knowledge_point)
            .order_by(func.count(StudentErrorLog.id).desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()
        return [(str(name), int(count)) for name, count in rows]

    @staticmethod
    async def count_errors_since(
        session: AsyncSession, *, student_id: str, since: datetime | None = None
    ) -> int:
        """某个时间区间内的错题条数（学习报告用）。"""
        condition = StudentErrorLog.student_id == student_id
        if since is not None:
            condition = condition & (StudentErrorLog.created_at >= since)
        stmt = select(func.count(StudentErrorLog.id)).where(condition)
        return int((await session.execute(stmt)).scalar_one())

    @staticmethod
    async def group_by_field(
        session: AsyncSession,
        *,
        student_id: str,
        field: str,
        since: datetime | None = None,
        limit: int = 10,
    ) -> list[tuple[str, int]]:
        """按某个字段分组计数，次数倒序。用于「错误类型分布」「语言分布」。

        `field` 只接受白名单里的列名。**不把它拼进 SQL 字符串**，
        而是映射到 ORM 列对象——否则这里就是一个注入点
        （哪怕目前调用方都是写死的，也不该留下这种形状的代码）。
        """
        columns = {
            "error_type": StudentErrorLog.error_type,
            "language": StudentErrorLog.language,
            "knowledge_point": StudentErrorLog.knowledge_point,
        }
        if field not in columns:
            raise ValueError(f"不支持的分组字段：{field}")
        column = columns[field]

        condition = StudentErrorLog.student_id == student_id
        if since is not None:
            condition = condition & (StudentErrorLog.created_at >= since)
        stmt = (
            select(column, func.count(StudentErrorLog.id))
            .where(condition)
            .group_by(column)
            .order_by(func.count(StudentErrorLog.id).desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).all()
        return [(str(name), int(count)) for name, count in rows]

    @staticmethod
    async def list_errors_since(
        session: AsyncSession,
        *,
        student_id: str,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[StudentErrorLog]:
        """按时间倒序取区间内的错题明细（学习报告的明细表用）。"""
        condition = StudentErrorLog.student_id == student_id
        if since is not None:
            condition = condition & (StudentErrorLog.created_at >= since)
        stmt = (
            select(StudentErrorLog)
            .where(condition)
            .order_by(StudentErrorLog.created_at.desc(), StudentErrorLog.id.desc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

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
