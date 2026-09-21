"""使用时长接口：/api/v1/usage

  POST /heartbeat   前端定时调用，累加"本次使用"的有效时长
  GET  /current     本次使用（当前进程）的时长，页面首屏用
  GET  /summary     某区间内使用时长汇总 + 每次明细

「点击 start.bat 记录使用时间」这件事发生在**服务启动时**（见 app/main.py
的 lifespan），不在接口里；接口只负责"续期"和"查询"。

## 关于 session_id

服务启动时会生成一条使用记录，id 存在 `app.state.usage_session_id`。
前端心跳不带任何参数就用它——单机版一台机器只有一个服务实例，
不需要客户端自己记 id。仍然保留请求体里的可选 `session_id`：
同一个进程被多个页面标签打开时，前端可以显式指定，行为更可预期。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, Query, Request

from app.api.deps import AppSettingsDep, DbSession
from app.core.identity import LOCAL_STUDENT_ID, STUDENT_HEADER, resolve_student_id
from app.models.usage import UsageSession
from app.schemas.report import (
    HeartbeatRequest,
    HeartbeatResponse,
    UsageDailyItem,
    UsageSessionItem,
    UsageSummaryResponse,
)
from app.services.usage_service import UsageService, format_duration, parse_range

logger = logging.getLogger(__name__)

router = APIRouter()

# 区间参数的中文说法，与 report_service 保持一致（这里只需给 usage 用）
RANGE_LABELS = {
    "all": "全部历史",
    "7": "最近 7 天",
    "30": "最近 30 天",
    "90": "最近 90 天",
}


def _current_session_id(request: Request) -> int | None:
    """取当前进程启动时写下的那条使用记录 id。"""
    return getattr(request.app.state, "usage_session_id", None)


@router.post(
    "/heartbeat",
    response_model=HeartbeatResponse,
    summary="使用时长心跳",
    description=(
        "前端页面每隔一段时间调用一次，把「本次使用」的有效时长往前推。\n\n"
        "只有真的开着学习页面才会累加——服务启动后无人使用的时间不计入，"
        "所以这个数字可以直接当作学习时长看。"
    ),
)
async def heartbeat(
    request: Request,
    session: DbSession,
    settings: AppSettingsDep,
    payload: HeartbeatRequest | None = None,
    student_id: str | None = Query(default=None, description="可选身份标识，默认本机 local"),
    x_student_id: str | None = Header(default=None, alias=STUDENT_HEADER),
) -> HeartbeatResponse:
    sid = resolve_student_id(student_id, x_student_id)
    session_id = payload.session_id if payload is not None else None
    if session_id is None:
        session_id = _current_session_id(request)

    if session_id is None:
        # 启动时那条记录没写成功（例如数据库只读）。不让前端反复报错，
        # 如实说明即可 —— 页面上的时长会一直显示 0。
        return HeartbeatResponse(message="本次使用记录不可用，使用时长未能统计")

    item = await UsageService.heartbeat(session, session_id=session_id, student_id=sid)
    if item is None:
        return HeartbeatResponse(message="本次使用记录不存在，使用时长未能统计")

    return HeartbeatResponse(
        session_id=item.id,
        active_seconds=item.active_seconds,
        active_text=format_duration(item.active_seconds),
        started_at=item.started_at,
    )


@router.get(
    "/current",
    response_model=HeartbeatResponse,
    summary="本次使用时长",
    description="页面首屏加载时读取一次，之后由 /heartbeat 推进。",
)
async def current_session(
    request: Request,
    session: DbSession,
    student_id: str | None = Query(default=None),
    x_student_id: str | None = Header(default=None, alias=STUDENT_HEADER),
) -> HeartbeatResponse:
    sid = resolve_student_id(student_id, x_student_id)
    session_id = _current_session_id(request)
    if session_id is None:
        return HeartbeatResponse(message="本次使用记录不可用，使用时长未能统计")

    record = await session.get(UsageSession, session_id)
    # student_id 一起校验：脚本显式传了别的身份时，不该看到本机那条记录
    if record is None or record.student_id != sid:
        return HeartbeatResponse(message="本次使用记录不存在，使用时长未能统计")
    return HeartbeatResponse(
        session_id=record.id,
        active_seconds=record.active_seconds,
        active_text=format_duration(record.active_seconds),
        started_at=record.started_at,
    )


@router.get(
    "/summary",
    response_model=UsageSummaryResponse,
    summary="使用时长汇总",
    description="区间内（默认最近 30 天）的总时长、使用天数、使用次数与每次明细。",
)
async def usage_summary(
    session: DbSession,
    range_value: str | None = Query(default="30", alias="range", description="7 / 30 / 90 / all"),
    student_id: str | None = Query(default=None),
    x_student_id: str | None = Header(default=None, alias=STUDENT_HEADER),
) -> UsageSummaryResponse:
    sid = resolve_student_id(student_id, x_student_id)
    since, range_key = parse_range(range_value)

    summary = await UsageService.summary(session, student_id=sid, since=since)
    sessions = await UsageService.list_sessions(
        session, student_id=sid, since=since, limit=100
    )
    return UsageSummaryResponse(
        student_id=sid,
        range_label=RANGE_LABELS.get(range_key, f"最近 {range_key} 天"),
        total_seconds=int(summary["total_seconds"]),
        total_text=format_duration(int(summary["total_seconds"])),
        session_count=int(summary["session_count"]),
        active_days=int(summary["active_days"]),
        average_seconds=int(summary["average_seconds"]),
        average_text=format_duration(int(summary["average_seconds"])),
        last_used_at=summary["last_used_at"],
        daily=[
            UsageDailyItem(
                date=item["date"],
                seconds=int(item["seconds"]),
                text=format_duration(int(item["seconds"])),
            )
            for item in summary["daily"]
        ],
        sessions=[
            UsageSessionItem(
                id=item.id,
                started_at=item.started_at,
                last_seen_at=item.last_seen_at,
                ended_at=item.ended_at,
                active_seconds=item.active_seconds,
                active_text=format_duration(item.active_seconds),
                wall_seconds=UsageService.wall_seconds(item),
            )
            for item in sessions
        ],
    )


__all__ = ["LOCAL_STUDENT_ID", "router"]
