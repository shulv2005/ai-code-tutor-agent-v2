"""使用时长服务：start.bat 启动时开一条，前端心跳累加，正常关闭时收尾。

## 两种时间口径（老师最关心的是第二个）

| 口径 | 怎么算 | 说明 |
| --- | --- | --- |
| 服务运行时长 | `last_seen_at` - `started_at` | 进程活着的时间，含无人使用的时段 |
| **有效使用时长** | `active_seconds`（心跳累加） | 只有页面真的开着才累加，报告以它为准 |

## 单段心跳为什么要截断

浏览器标签页被挂起（省电模式、合上笔记本盖）时定时器不执行，
恢复后第一个心跳的间隔可能是几个小时。若直接累加，这段"人不在"的时间
会被算成学习时长。所以每次只累加 `min(间隔, MAX_HEARTBEAT_GAP_SECONDS)`，
把这类误差封顶在 5 分钟——比"少算"更怕的是"多算"，老师会据此判断学生
是不是真的在用。

## 当前这次与历史各次

当前这次的有效秒数在数据库里是**单调递增**的，但两次心跳之间它是静止的。
前端的"本次已使用 xx:xx"因此采用「服务端值 + 本地自上次心跳以来的秒数」
显示，每次心跳回来再以服务端值为准纠偏，既跟手又不会漂。
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utc_now
from app.models.usage import UsageSession

logger = logging.getLogger(__name__)

# 单次心跳最多计入的秒数，见模块头部的说明。
MAX_HEARTBEAT_GAP_SECONDS = 300


def accumulate(previous_seconds: int, gap_seconds: float) -> int:
    """把一次心跳间隔截断后累加。间隔为负（时钟回拨）时按 0 处理。"""
    if gap_seconds <= 0:
        return previous_seconds
    return previous_seconds + int(min(gap_seconds, MAX_HEARTBEAT_GAP_SECONDS))


class UsageService:
    """使用时长读写与聚合。"""

    # -- 生命周期 ---------------------------------------------------------
    @staticmethod
    async def start_session(
        session: AsyncSession, *, student_id: str, app_version: str | None = None
    ) -> int | None:
        """服务启动时开一条使用记录，返回它的 id。

        写失败不让启动失败（返回 None）：使用统计是"锦上添花"的功能，
        不能因为它把整个服务拦在门外。
        """
        now = utc_now()
        item = UsageSession(
            student_id=student_id,
            started_at=now,
            last_seen_at=now,
            active_seconds=0,
            pid=os.getpid(),
            app_version=app_version,
        )
        try:
            session.add(item)
            await session.commit()
            await session.refresh(item)
        except Exception:  # noqa: BLE001 - 统计写不进去不该影响服务启动
            logger.warning("写入使用记录失败", exc_info=True)
            await session.rollback()
            return None
        logger.info("本次使用开始：session_id=%s student=%s", item.id, student_id)
        return item.id

    @staticmethod
    async def heartbeat(
        session: AsyncSession, *, session_id: int, student_id: str
    ) -> UsageSession | None:
        """前端定期调用：按截断后的间隔累加有效时长。

        只会更新**属于这个学生**的那条记录（student_id 一起作为过滤条件），
        避免脚本传错 id 时改到别人的使用记录。
        """
        item = await session.get(UsageSession, session_id)
        if item is None or item.student_id != student_id:
            return None

        now = utc_now()
        item.active_seconds = accumulate(
            item.active_seconds, (now - item.last_seen_at).total_seconds()
        )
        item.last_seen_at = now
        await session.commit()
        await session.refresh(item)
        return item

    @staticmethod
    async def end_session(session: AsyncSession, *, session_id: int) -> None:
        """正常关闭时收尾：把最后一段间隔也累加进去，再写 ended_at。

        拿不到（进程被强杀）不影响统计——历史各次读的是已累加的值。
        """
        try:
            item = await session.get(UsageSession, session_id)
            if item is None:
                return
            now = utc_now()
            item.active_seconds = accumulate(
                item.active_seconds, (now - item.last_seen_at).total_seconds()
            )
            item.last_seen_at = now
            item.ended_at = now
            await session.commit()
            logger.info(
                "本次使用结束：session_id=%s 有效时长 %s 秒",
                session_id,
                item.active_seconds,
            )
        except Exception:  # noqa: BLE001
            logger.warning("收尾使用记录失败", exc_info=True)

    # -- 查询 -------------------------------------------------------------
    @staticmethod
    def wall_seconds(item: UsageSession) -> int:
        """服务运行时长（从启动到现在/关闭）。仅作参考信息展示。"""
        end = item.ended_at or item.last_seen_at
        if end < item.started_at:
            return 0
        return int((end - item.started_at).total_seconds())

    @staticmethod
    async def list_sessions(
        session: AsyncSession,
        *,
        student_id: str,
        since: datetime | None = None,
        limit: int = 500,
    ) -> list[UsageSession]:
        """按时间**倒序**取使用记录（最近的在前面）。"""
        condition = UsageSession.student_id == student_id
        if since is not None:
            condition = condition & (UsageSession.started_at >= since)
        stmt = (
            select(UsageSession)
            .where(condition)
            .order_by(UsageSession.started_at.desc(), UsageSession.id.desc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    @staticmethod
    async def summary(
        session: AsyncSession,
        *,
        student_id: str,
        since: datetime | None = None,
    ) -> dict[str, object]:
        """使用时长汇总：总时长 / 天数 / 次数 / 最近一次 / 按天分布。

        「使用天数」按**本地日期**去重：跨零点的那次使用算两天，
        和数据里"哪几天来过"的直觉一致。
        """
        items = await UsageService.list_sessions(
            session, student_id=student_id, since=since, limit=1000
        )
        total = sum(item.active_seconds for item in items)

        days: dict[str, int] = {}
        for item in items:
            key = item.started_at.astimezone().date().isoformat()
            days[key] = days.get(key, 0) + item.active_seconds

        last = items[0] if items else None
        return {
            "total_seconds": total,
            "session_count": len(items),
            "active_days": len(days),
            "last_used_at": last.started_at if last else None,
            "average_seconds": int(total / len(items)) if items else 0,
            "daily": [
                {"date": key, "seconds": seconds}
                for key, seconds in sorted(days.items(), reverse=True)
            ],
            "sessions": items,
        }

    @staticmethod
    async def count_sessions(
        session: AsyncSession, *, student_id: str, since: datetime | None = None
    ) -> int:
        """一共用了几次。"""
        condition = UsageSession.student_id == student_id
        if since is not None:
            condition = condition & (UsageSession.started_at >= since)
        stmt = select(func.count(UsageSession.id)).where(condition)
        return int((await session.execute(stmt)).scalar_one())


def parse_range(value: str | None, *, now: datetime | None = None) -> tuple[datetime | None, str]:
    """把前端传来的区间参数翻译成起始时间。

    支持 `7` / `30` / `90` / `all`（天）。认不出来的值按 30 天处理，
    并且**不报错**——老师随手改一下地址栏不该看到一个 422。
    """
    moment = now or datetime.now(UTC)
    text = (value or "").strip().lower()
    if text in {"all", "0", ""}:
        return None, "all"
    try:
        days = int(text)
    except ValueError:
        days = 30
    if days <= 0:
        return None, "all"
    days = min(days, 3650)
    return moment - timedelta(days=days), str(days)


def today_local() -> date:
    """本地日期（报告落款用）。"""
    return datetime.now().astimezone().date()


def format_duration(seconds: int) -> str:
    """把秒数写成老师一眼能读的形式：`1 小时 23 分` / `12 分 30 秒` / `45 秒`。"""
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours} 小时 {minutes} 分"
    if minutes:
        return f"{minutes} 分 {secs} 秒"
    return f"{secs} 秒"


__all__ = [
    "MAX_HEARTBEAT_GAP_SECONDS",
    "UsageService",
    "accumulate",
    "format_duration",
    "parse_range",
    "today_local",
]
