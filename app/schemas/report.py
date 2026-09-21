"""学习报告与使用时长接口的请求 / 响应契约。

把契约单独放一个文件（而不是散在 endpoint 里），目的是让前端有**一份可引用的
形状**：`tools/verify_report.py` 与前端契约检查都直接读这里的模型，
接口字段改了而文档没改时能被发现。
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class HeartbeatRequest(BaseModel):
    """心跳请求体。

    整个请求体都是可选的——单机版只有一个服务实例，前端不传时后端就用
    自己启动时记下的那条记录 id。留着 `session_id` 是为了让"同一个进程被
    多个标签页打开"这类情况可以由前端显式指定。
    """

    session_id: int | None = Field(default=None, description="可选：显式指定本次使用的记录 ID")


class HeartbeatResponse(BaseModel):
    """心跳响应：前端拿它刷新"本次已使用 xx:xx"。"""

    session_id: int | None = Field(default=None, description="本次使用的记录 ID；写库失败时为 null")
    active_seconds: int = Field(default=0, description="本次累计有效使用秒数（服务端口径）")
    active_text: str = Field(default="0 秒", description="上面那个秒数的中文写法")
    started_at: datetime | None = Field(default=None, description="本次使用开始时间（UTC）")
    message: str = Field(default="", description="写库失败时的说明；正常为空")


class UsageDailyItem(BaseModel):
    """按天汇总的一项。"""

    date: str = Field(description="本地日期 YYYY-MM-DD")
    seconds: int = Field(description="当天有效使用秒数")
    text: str = Field(description="当天有效使用时长（中文写法）")


class UsageSessionItem(BaseModel):
    """一次使用的明细。"""

    id: int
    started_at: datetime
    last_seen_at: datetime
    ended_at: datetime | None = None
    active_seconds: int
    active_text: str
    wall_seconds: int = Field(description="服务运行时长（含无人使用的时段），仅供参考")


class UsageSummaryResponse(BaseModel):
    """使用时长汇总。"""

    student_id: str
    range_label: str
    total_seconds: int
    total_text: str
    session_count: int
    active_days: int
    average_seconds: int
    average_text: str
    last_used_at: datetime | None = None
    daily: list[UsageDailyItem] = Field(default_factory=list)
    sessions: list[UsageSessionItem] = Field(default_factory=list)


class KnowledgePointItem(BaseModel):
    knowledge_point: str
    count: int
    ratio: str = Field(description="占区间内全部错题的百分比，已带 % 号")


class ErrorTypeItem(BaseModel):
    error_type: str
    count: int
    ratio: str


class LanguageItem(BaseModel):
    language: str
    count: int
    ratio: str


class ErrorDetailItem(BaseModel):
    """错题明细里的一条。"""

    id: int
    created_at: datetime
    language: str
    filename: str
    error_type: str
    knowledge_point: str
    knowledge_points: list[str] = Field(default_factory=list)
    ai_feedback: str
    ai_feedback_excerpt: str
    code_excerpt: str
    line_count: int


class ErrorReportSection(BaseModel):
    """错题整改情况。"""

    total: int
    listed: int
    weak_points: list[KnowledgePointItem] = Field(default_factory=list)
    by_type: list[ErrorTypeItem] = Field(default_factory=list)
    by_language: list[LanguageItem] = Field(default_factory=list)
    items: list[ErrorDetailItem] = Field(default_factory=list)


class LearningReportResponse(BaseModel):
    """学习报告全量数据（前端「查看报告」用的那个，与导出的 Word 同源）。"""

    student_id: str
    generated_at: datetime
    range_key: str = Field(description="all / 7 / 30 / 90 …")
    range_label: str = Field(description="区间的中文说法，直接显示")
    app_version: str | None = None
    usage: UsageSummaryResponse
    errors: ErrorReportSection
    suggestions: list[str] = Field(default_factory=list, description="按固定规则生成的学习建议")


__all__ = [
    "ErrorDetailItem",
    "ErrorReportSection",
    "ErrorTypeItem",
    "HeartbeatRequest",
    "HeartbeatResponse",
    "KnowledgePointItem",
    "LanguageItem",
    "LearningReportResponse",
    "UsageDailyItem",
    "UsageSessionItem",
    "UsageSummaryResponse",
]
