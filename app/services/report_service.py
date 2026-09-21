"""学习报告：把「使用时长」和「错题整改情况」拼成一份老师能直接看的材料。

## 这个服务刻意不做的事

1. **不调用大模型**。老师可能根本不知道什么是 API Key，也可能在没配 Key 的
   机器上导出报告。报告必须 100% 离线可生成，所以「学习建议」是一套**规则**
   （见 `_suggestions`），而不是让模型写一段话。
2. **不做数据加工上的猜测**。没有的字段就是 0 / 空表，不编。
   老师拿这份材料是要跟学生谈话的，编出来的数字比没有数字更糟糕。

## 报告里有什么

资源层（原始数据）-> 由 `build()` 汇总成三段：
  · **使用情况**：总有效时长、使用天数、使用次数、每次明细
  · **错题整改**：区间内错题总数、按知识点 / 错误类型 / 语言分布、逐条明细
  · **学习建议**：按上面的数据套规则生成的、可执行的短句

`to_docx_bytes()` 负责把这份结构渲染成 Word（实现在 report_docx.py）。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utc_now
from app.services.tutor_service import UNKNOWN_KNOWLEDGE_POINT, TutorService
from app.services.usage_service import UsageService, format_duration, parse_range

logger = logging.getLogger(__name__)

# 报告里逐条列出的错题上限。老师要的是"看清最近错在哪"，
# 不是把 200 条错题全打出来（那样没人会看）。
REPORT_ERROR_LIMIT = 50

# 报告里逐条列出的使用记录上限
REPORT_SESSION_LIMIT = 50

# 知识点榜列几条
REPORT_WEAK_LIMIT = 10

# 建议规则用到的阈值，集中放这里，避免散落在判断里
SUGGEST_LOW_USAGE_MINUTES = 30
SUGGEST_REPEAT_COUNT = 3
SUGGEST_DOMINANT_RATIO = 0.5

# 报告区间的中文说法
RANGE_LABELS = {
    "all": "全部历史",
    "7": "最近 7 天",
    "30": "最近 30 天",
    "90": "最近 90 天",
}


def _excerpt(text: str, limit: int) -> str:
    """把长文本压成一行摘要（表格单元格里放不下多行大段文字）。"""
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "…"


def _ratio(count: int, total: int) -> str:
    if total <= 0:
        return "0%"
    return f"{count * 100 / total:.0f}%"


def _suggestions(
    *,
    total_seconds: int,
    session_count: int,
    error_total: int,
    weak: list[tuple[str, int]],
    by_type: list[tuple[str, int]],
    by_language: list[tuple[str, int]],
) -> list[str]:
    """按固定规则生成学习建议。

    每条建议都必须是**可执行的**（"把循环边界那 3 道题重做一遍"），
    而不是"继续加油"这类废话——老师会拿着它跟学生谈。
    """
    tips: list[str] = []

    if session_count == 0:
        tips.append(
            "本区间没有使用记录。请确认学生是否已双击 start.bat 启动过系统，"
            "或把报告区间调成「全部历史」再看看。"
        )
    elif total_seconds < SUGGEST_LOW_USAGE_MINUTES * 60:
        tips.append(
            f"本区间有效使用时长只有 {format_duration(total_seconds)}，"
            f"低于 {SUGGEST_LOW_USAGE_MINUTES} 分钟。建议先保证每次上机都完整跑一遍"
            "「诊断 + 举一反三」，而不是只把系统打开。"
        )

    if error_total == 0 and session_count > 0:
        tips.append(
            "本区间没有新增错题。可以让该学生尝试难度更高一档的题目，"
            "或把之前错得最多的知识点用新题目复测一次。"
        )

    if weak:
        top_point, top_count = weak[0]
        if top_point != UNKNOWN_KNOWLEDGE_POINT and top_count >= SUGGEST_REPEAT_COUNT:
            tips.append(
                f"「{top_point}」累计错 {top_count} 次，是最集中的薄弱点。"
                f"建议把错题本里这个知识点的 {top_count} 道题重做一遍，"
                "并在下一次提交时特别说明这次是怎么避免的。"
            )
        elif top_point != UNKNOWN_KNOWLEDGE_POINT:
            tips.append(
                f"目前最集中的知识点是「{top_point}」（{top_count} 次），"
                "样本还少，可以再观察一段时间。"
            )
        if top_point == UNKNOWN_KNOWLEDGE_POINT:
            tips.append(
                "有部分错题没能归到具体知识点上。这通常说明代码片段太短，"
                "模型看不出上下文；建议提交完整的函数或整个文件。"
            )

    if error_total and by_type:
        top_type, top_count = by_type[0]
        if top_count / error_total >= SUGGEST_DOMINANT_RATIO:
            tips.append(
                f"错题里「{top_type}」占了 {_ratio(top_count, error_total)}，"
                "是当前的主要失分类型，适合作为下一阶段专项训练的方向。"
            )

    if len(by_language) >= 2:
        names = "、".join(name for name, _ in by_language[:3])
        tips.append(
            f"练习过的语言有 {len(by_language)} 种（{names}）。"
            "若目标是单语言能力提升，建议一段时间内集中练一种。"
        )

    if not tips:
        tips.append("各项数据都在正常范围内，按当前节奏继续练习即可。")

    return tips


class ReportService:
    """学习报告的数据汇总与渲染入口。"""

    @staticmethod
    async def build(
        session: AsyncSession,
        *,
        student_id: str,
        range_value: str | None = "30",
        app_version: str | None = None,
    ) -> dict[str, Any]:
        """汇总一份学习报告（纯数据，不含渲染）。"""
        since, range_key = parse_range(range_value)
        now = utc_now()

        usage = await UsageService.summary(session, student_id=student_id, since=since)
        sessions = await UsageService.list_sessions(
            session, student_id=student_id, since=since, limit=REPORT_SESSION_LIMIT
        )

        error_total = await TutorService.count_errors_since(
            session, student_id=student_id, since=since
        )
        weak = await TutorService.weak_points(
            session, student_id=student_id, limit=REPORT_WEAK_LIMIT, since=since
        )
        by_type = await TutorService.group_by_field(
            session, student_id=student_id, field="error_type", since=since, limit=10
        )
        by_language = await TutorService.group_by_field(
            session, student_id=student_id, field="language", since=since, limit=10
        )
        errors = await TutorService.list_errors_since(
            session, student_id=student_id, since=since, limit=REPORT_ERROR_LIMIT
        )

        total_seconds = int(usage["total_seconds"])
        session_count = int(usage["session_count"])

        report: dict[str, Any] = {
            "student_id": student_id,
            "generated_at": now,
            "range_key": range_key,
            "range_label": RANGE_LABELS.get(range_key, f"最近 {range_key} 天"),
            "app_version": app_version,
            "usage": {
                "total_seconds": total_seconds,
                "total_text": format_duration(total_seconds),
                "session_count": session_count,
                "active_days": int(usage["active_days"]),
                "average_seconds": int(usage["average_seconds"]),
                "average_text": format_duration(int(usage["average_seconds"])),
                "last_used_at": usage["last_used_at"],
                "daily": list(usage["daily"]),
                "sessions": [
                    {
                        "id": item.id,
                        "started_at": item.started_at,
                        "last_seen_at": item.last_seen_at,
                        "ended_at": item.ended_at,
                        "active_seconds": item.active_seconds,
                        "active_text": format_duration(item.active_seconds),
                        "wall_seconds": UsageService.wall_seconds(item),
                    }
                    for item in sessions
                ],
                "session_total": len(sessions),
            },
            "errors": {
                "total": error_total,
                "listed": len(errors),
                "weak_points": [
                    {
                        "knowledge_point": name,
                        "count": count,
                        "ratio": _ratio(count, error_total),
                    }
                    for name, count in weak
                ],
                "by_type": [
                    {"error_type": name, "count": count, "ratio": _ratio(count, error_total)}
                    for name, count in by_type
                ],
                "by_language": [
                    {"language": name, "count": count, "ratio": _ratio(count, error_total)}
                    for name, count in by_language
                ],
                "items": [
                    {
                        "id": item.id,
                        "created_at": item.created_at,
                        "language": item.language,
                        "filename": item.filename or "",
                        "error_type": item.error_type,
                        "knowledge_point": item.knowledge_point,
                        "knowledge_points": item.knowledge_points(),
                        "ai_feedback": item.ai_feedback,
                        "ai_feedback_excerpt": _excerpt(item.ai_feedback, 120),
                        "code_excerpt": _excerpt(item.code_snippet, 90),
                        "line_count": len(item.code_snippet.splitlines()),
                    }
                    for item in errors
                ],
            },
        }
        report["suggestions"] = _suggestions(
            total_seconds=total_seconds,
            session_count=session_count,
            error_total=error_total,
            weak=weak,
            by_type=by_type,
            by_language=by_language,
        )
        logger.info(
            "生成学习报告：student=%s range=%s 使用 %s 秒 / 错题 %s 条",
            student_id,
            range_key,
            total_seconds,
            error_total,
        )
        return report

    @staticmethod
    async def build_docx(
        session: AsyncSession,
        *,
        student_id: str,
        range_value: str | None = "30",
        app_version: str | None = None,
    ) -> tuple[bytes, str]:
        """汇总并渲染成 Word，返回 (文件字节, 建议的文件名)。"""
        from app.services.report_docx import render_learning_report

        report = await ReportService.build(
            session,
            student_id=student_id,
            range_value=range_value,
            app_version=app_version,
        )
        payload = render_learning_report(report)
        return payload, ReportService.filename(report)

    @staticmethod
    def filename(report: dict[str, Any]) -> str:
        """给浏览器下载用的文件名。

        带上学生标识与生成时刻：老师一次收几十份时，光看文件名就能分辨，
        不会出现"下载(3).docx"这种谁也认不出的东西。
        """
        stamp = report["generated_at"].astimezone().strftime("%Y%m%d-%H%M")
        safe_student = "".join(
            ch for ch in str(report["student_id"]) if ch.isalnum() or ch in "-_"
        ) or "student"
        return f"AI代码导师学习报告_{safe_student}_{stamp}.docx"


__all__ = ["REPORT_ERROR_LIMIT", "ReportService"]
