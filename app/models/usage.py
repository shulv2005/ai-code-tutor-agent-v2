"""使用时长记录的 ORM 模型。

面向需求「点击 start.bat 记录使用时间」：**服务进程启动的那一刻**就是学生
开始使用的那一刻（学生就是双击 start.bat 把服务跑起来的），所以这个时间
在应用 lifespan 的 startup 阶段落一条记录。

## 为什么需要「心跳」而不是只记开始和结束

只记 `started_at` / `ended_at` 有两个绕不开的问题：

1. **结束时间经常拿不到**。学生直接关掉命令行窗口、或者断电、或者任务管理器
   结束进程时，uvicorn 的 shutdown 钩子根本不会执行，`ended_at` 永远是空。
   这样算出来的时长不是"用了多久"，而是"直到老师来问为止"。
2. **服务开着不等于在用**。学生双击完 start.bat 就去吃饭，窗口开一下午，
   按进程存活时长算会得出一个荒唐的数字。

所以改成：前端页面每隔一段时间调一次心跳接口刷新 `last_seen_at`，
**有效时长 = 累计心跳间隔**（`active_seconds`）。只有页面真的开着才算在用。

## 为什么要单独存 `active_seconds`，而不是每次现算 `last_seen - started`

因为浏览器标签页会被挂起（省电模式、长时间切到后台、合上笔记本盖）。
挂起期间定时器不执行，恢复后第一个心跳的间隔可能是两小时——现算的话
这两小时会被算成"在用"。改成每次心跳**累加**一段（单段最多计 5 分钟，
见 `MAX_HEARTBEAT_GAP_SECONDS`），这类"假使用"最多只多算 5 分钟。
`started_at` 仍然保留，用来回答"哪一天来的、来了几次"。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import UTCDateTime, utc_now


class UsageSession(Base):
    """一次使用（= 一次 start.bat 启动的服务进程）。"""

    __tablename__ = "usage_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # 与错题本同一套身份约定：单机版固定 `local`。
    student_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)

    # 服务启动时间 = 学生双击 start.bat 的时间
    started_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now, index=True, nullable=False
    )

    # 最后一次收到前端心跳的时间
    last_seen_at: Mapped[datetime] = mapped_column(
        UTCDateTime, default=utc_now, nullable=False
    )

    # 累计有效秒数：每次心跳把「距上次心跳的间隔」截断后累加进来。
    # 报告的"使用时长"读的就是这一列——它是单调递增的，不会因为一次
    # 迟到的请求而突然暴涨。
    active_seconds: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # 正常关闭时写入；被杀进程 / 断电时为 NULL（统计时不依赖它）
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)

    # 进程号：同一台机器上重复启动时，便于把"哪一条是当前这次"认出来
    pid: Mapped[int | None] = mapped_column(Integer, default=None)

    # 版本号：将来统计口径变了，可以按版本区分老数据
    app_version: Mapped[str | None] = mapped_column(String(32), default=None)

    __table_args__ = (
        # 主力查询：「某学生最近 N 次使用」——按学生过滤 + 时间倒序
        Index("ix_usage_sessions_student_started", "student_id", "started_at"),
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试辅助
        return (
            f"<UsageSession id={self.id} student={self.student_id} "
            f"started={self.started_at:%Y-%m-%d %H:%M:%S}>"
        )


__all__ = ["UsageSession"]
