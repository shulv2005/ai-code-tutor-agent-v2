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


class ContributorItem(BaseModel):
    """把某个能力维度拖下去的具体问题（知识点 + 错误类型）。"""

    knowledge_point: str
    error_type: str
    count: int


class AbilityDimensionItem(BaseModel):
    """能力雷达图上的一个维度。"""

    key: str = Field(description="维度键：accuracy / robustness / conventions / …")
    label: str = Field(description="中文名，直接显示")
    hint: str = Field(description="一句话说明这个维度在看什么")
    score: int = Field(description="0-100 分；60 分以上算过关")
    weight: float = Field(description="该维度累计的证据权重（分数就是由它算出来的）")
    evidence_count: int = Field(description="归到这个维度的记录条数")
    samples: list[str] = Field(
        default_factory=list, description="最多 3 条证据样本，用来说明分数是怎么来的"
    )
    delta: int | None = Field(
        default=None,
        description="与上一个等长区间相比的分数变化；全部历史（range=all）时为 null",
    )
    delta_label: str = Field(default="", description="上面那个数带符号的写法，如 +12 / -8 / 0")
    contributors: list[ContributorItem] = Field(
        default_factory=list,
        description="这个维度为什么低：按次数倒序的知识点 + 错误类型（最多 4 条）",
    )


class AbilityProfile(BaseModel):
    """六维能力画像（雷达图的数据源）。

    刻意把 `evidence_total` 与 `unclassified` 一起返回：这个分数是启发式评估，
    界面上必须能说明"依据多少条记录"，否则雷达图就只是一张好看的图。
    """

    dimensions: list[AbilityDimensionItem] = Field(default_factory=list)
    error_total: int = Field(default=0, description="参与统计的错题条数")
    check_total: int = Field(default=0, description="参与统计的检测记录条数")
    evidence_total: int = Field(default=0, description="上面两者之和")
    unclassified: int = Field(default=0, description="关键词没命中、兜底归到准确性的条数")
    weakest: str = Field(default="", description="最低分维度的 key")
    strongest: str = Field(default="", description="最高分维度的 key")
    spread: int = Field(default=0, description="最高分与最低分之差")
    summary: str = Field(default="", description="配雷达图的一句完整结论（弹窗与 Word 用）")
    headline: str = Field(default="", description="只讲最弱那一项的短句（侧栏卡片用）")
    guide: str = Field(
        default="", description="「怎么读这张图」的说明文字，前后端与 Word 共用同一份"
    )
    has_trend: bool = Field(
        default=False, description="是否有可对比的上一个周期（range=all 时为 false）"
    )
    previous_total: int = Field(default=0, description="上一个周期的证据条数")
    trend_note: str = Field(default="", description="进步最大 / 需要留意 的一句话")


class InsightItem(BaseModel):
    """一个偏弱维度的诊断：分数、等级、成因、提升建议。"""

    key: str
    label: str
    hint: str
    score: int
    level: str = Field(description="优秀 / 良好 / 待加强 / 薄弱")
    evidence_count: int
    contributors: list[ContributorItem] = Field(default_factory=list)
    advice: str = Field(description="总结性提升建议（规则生成，不含具体题目）")


class InsightSection(BaseModel):
    """弱项细分与提升建议。只收录低于阈值的维度，按分数升序。"""

    items: list[InsightItem] = Field(default_factory=list)
    summary: str = ""
    threshold: int = Field(default=85, description="低于这个分数才会单独展开")


class RecurringItem(BaseModel):
    """一条"反复出错的语句"。"""

    line: str
    count: int = Field(description="在几条错题里出现过")
    knowledge_point: str = Field(description="这些错题属于哪一类知识点")
    group_records: int = Field(description="该类知识点一共几条错题")


class RecurringSection(BaseModel):
    """反复出错的语句。"""

    items: list[RecurringItem] = Field(default_factory=list)
    summary: str = ""
    min_records: int = Field(default=2, description="至少在几条错题里出现才算反复")


class LearningReportResponse(BaseModel):
    """学习报告全量数据（前端「查看报告」用的那个，与导出的 Word 同源）。"""

    student_id: str
    generated_at: datetime
    range_key: str = Field(description="all / 7 / 30 / 90 …")
    range_label: str = Field(description="区间的中文说法，直接显示")
    app_version: str | None = None
    usage: UsageSummaryResponse
    errors: ErrorReportSection
    ability: AbilityProfile = Field(
        default_factory=AbilityProfile, description="六维能力画像（雷达图）"
    )
    insights: InsightSection = Field(
        default_factory=InsightSection, description="弱项细分与提升建议（逐维度展开）"
    )
    recurring: RecurringSection = Field(
        default_factory=RecurringSection, description="反复出错的语句"
    )
    suggestions: list[str] = Field(default_factory=list, description="按固定规则生成的学习建议")


__all__ = [
    "AbilityDimensionItem",
    "AbilityProfile",
    "ContributorItem",
    "ErrorDetailItem",
    "ErrorReportSection",
    "ErrorTypeItem",
    "HeartbeatRequest",
    "HeartbeatResponse",
    "InsightItem",
    "InsightSection",
    "KnowledgePointItem",
    "LanguageItem",
    "LearningReportResponse",
    "RecurringItem",
    "RecurringSection",
    "UsageDailyItem",
    "UsageSessionItem",
    "UsageSummaryResponse",
]
