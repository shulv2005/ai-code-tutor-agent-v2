"""学习报告接口：/api/v1/report

  GET /summary   报告数据（使用时长 + 错题整改 + 学习建议）
  GET /word      导出 Word 学习报告，浏览器直接下载

## 导出为什么放在后端生成

需求是「导出的 Word 从浏览器里直接下载」。前端当然可以用 JS 拼一个 .docx
再从 Blob 下载，但那样得把整份文档的排版逻辑搬到前端，而且要引入一个
几 MB 的前端库；后端本来就有全部数据，用标准库拼一份真正的 .docx
（见 app/services/report_docx.py，零新增依赖）走 `Content-Disposition`
返回，浏览器行为与点击普通下载链接完全一致。

## 文件名为什么要写两个

`filename=` 只能放 ASCII，中文会被浏览器截断或乱码（老 IE/Edge 会直接丢名字）。
按 RFC 5987 / 6266 再加一个 `filename*=UTF-8''<百分号编码>`，
现代浏览器优先用它，于是中文文件名能完整保留。
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from fastapi import APIRouter, Header, Query, Response

from app.api.deps import AppSettingsDep, DbSession
from app.core.identity import STUDENT_HEADER, resolve_student_id
from app.schemas.report import (
    AbilityDimensionItem,
    AbilityProfile,
    ContributorItem,
    ErrorDetailItem,
    ErrorReportSection,
    ErrorTypeItem,
    InsightItem,
    InsightSection,
    KnowledgePointItem,
    LanguageItem,
    LearningReportResponse,
    RecurringItem,
    RecurringSection,
    UsageDailyItem,
    UsageSessionItem,
    UsageSummaryResponse,
)
from app.services.report_service import ReportService
from app.services.usage_service import format_duration

logger = logging.getLogger(__name__)

router = APIRouter()

# Word 文档的标准 MIME 类型（.docx，不是老的 .doc）
DOCX_MEDIA_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


def _to_response(report: dict) -> LearningReportResponse:
    """把服务层返回的 dict 收敛成 Pydantic 模型。

    刻意在这里做显式转换而不是 `model_validate(report)`：服务层的结构
    与对外契约是两件事，显式转换能让"新增了一个内部字段但忘了对外暴露"
    这类问题在类型检查阶段就暴露出来。
    """
    usage = report["usage"]
    errors = report["errors"]
    return LearningReportResponse(
        student_id=report["student_id"],
        generated_at=report["generated_at"],
        range_key=report["range_key"],
        range_label=report["range_label"],
        app_version=report["app_version"],
        usage=UsageSummaryResponse(
            student_id=report["student_id"],
            range_label=report["range_label"],
            total_seconds=usage["total_seconds"],
            total_text=usage["total_text"],
            session_count=usage["session_count"],
            active_days=usage["active_days"],
            average_seconds=usage["average_seconds"],
            average_text=usage["average_text"],
            last_used_at=usage["last_used_at"],
            daily=[
                UsageDailyItem(
                    date=item["date"],
                    seconds=item["seconds"],
                    text=format_duration(item["seconds"]),
                )
                for item in usage["daily"]
            ],
            sessions=[
                UsageSessionItem(
                    id=item["id"],
                    started_at=item["started_at"],
                    last_seen_at=item["last_seen_at"],
                    ended_at=item["ended_at"],
                    active_seconds=item["active_seconds"],
                    active_text=item["active_text"],
                    wall_seconds=item["wall_seconds"],
                )
                for item in usage["sessions"]
            ],
        ),
        errors=ErrorReportSection(
            total=errors["total"],
            listed=errors["listed"],
            weak_points=[
                KnowledgePointItem(**item) for item in errors["weak_points"]
            ],
            by_type=[ErrorTypeItem(**item) for item in errors["by_type"]],
            by_language=[LanguageItem(**item) for item in errors["by_language"]],
            items=[ErrorDetailItem(**item) for item in errors["items"]],
        ),
        ability=_ability_from(report.get("ability")),
        insights=_insights_from(report.get("insights")),
        recurring=_recurring_from(report.get("recurring")),
        suggestions=list(report["suggestions"]),
    )


def _ability_from(raw: dict | None) -> AbilityProfile:
    """把能力画像的 dict 收敛成 Pydantic 模型（数据缺失时返回空画像）。"""
    if not raw:
        return AbilityProfile()
    return AbilityProfile(
        dimensions=[
            AbilityDimensionItem(
                key=item["key"],
                label=item["label"],
                hint=item["hint"],
                score=int(item["score"]),
                weight=float(item["weight"]),
                evidence_count=int(item["evidence_count"]),
                samples=list(item.get("samples", [])),
                delta=None if item.get("delta") is None else int(item["delta"]),
                delta_label=str(item.get("delta_label", "")),
                contributors=[
                    ContributorItem(
                        knowledge_point=str(one["knowledge_point"]),
                        error_type=str(one["error_type"]),
                        count=int(one["count"]),
                    )
                    for one in item.get("contributors", [])
                ],
            )
            for item in raw.get("dimensions", [])
        ],
        error_total=int(raw.get("error_total", 0)),
        check_total=int(raw.get("check_total", 0)),
        evidence_total=int(raw.get("evidence_total", 0)),
        unclassified=int(raw.get("unclassified", 0)),
        weakest=str(raw.get("weakest", "")),
        strongest=str(raw.get("strongest", "")),
        spread=int(raw.get("spread", 0)),
        summary=str(raw.get("summary", "")),
        headline=str(raw.get("headline", "")),
        guide=str(raw.get("guide", "")),
        has_trend=bool(raw.get("has_trend", False)),
        previous_total=int(raw.get("previous_total", 0)),
        trend_note=str(raw.get("trend_note", "")),
    )


def _insights_from(raw: dict | None) -> InsightSection:
    """弱项细分与提升建议。"""
    if not raw:
        return InsightSection()
    return InsightSection(
        items=[
            InsightItem(
                key=str(item["key"]),
                label=str(item["label"]),
                hint=str(item["hint"]),
                score=int(item["score"]),
                level=str(item["level"]),
                evidence_count=int(item["evidence_count"]),
                contributors=[
                    ContributorItem(
                        knowledge_point=str(one["knowledge_point"]),
                        error_type=str(one["error_type"]),
                        count=int(one["count"]),
                    )
                    for one in item.get("contributors", [])
                ],
                advice=str(item.get("advice", "")),
            )
            for item in raw.get("items", [])
        ],
        summary=str(raw.get("summary", "")),
        threshold=int(raw.get("threshold", 0)),
    )


def _recurring_from(raw: dict | None) -> RecurringSection:
    """反复出错的语句。"""
    if not raw:
        return RecurringSection()
    return RecurringSection(
        items=[
            RecurringItem(
                line=str(item["line"]),
                count=int(item["count"]),
                knowledge_point=str(item["knowledge_point"]),
                group_records=int(item["group_records"]),
            )
            for item in raw.get("items", [])
        ],
        summary=str(raw.get("summary", "")),
        min_records=int(raw.get("min_records", 0)),
    )


@router.get(
    "/summary",
    response_model=LearningReportResponse,
    summary="学习报告数据",
    description=(
        "汇总某个区间内的**使用时长**与**错题整改情况**，并给出按固定规则生成的"
        "学习建议。\n\n"
        "与 `/report/word` 同源：页面上看到的就是导出 Word 里的内容。"
    ),
)
async def report_summary(
    session: DbSession,
    settings: AppSettingsDep,
    range_value: str | None = Query(
        default="30", alias="range", description="7 / 30 / 90 / all（默认 30 天）"
    ),
    student_id: str | None = Query(default=None, description="可选身份标识，默认本机 local"),
    x_student_id: str | None = Header(default=None, alias=STUDENT_HEADER),
) -> LearningReportResponse:
    sid = resolve_student_id(student_id, x_student_id)
    report = await ReportService.build(
        session,
        student_id=sid,
        range_value=range_value,
        app_version=settings.app.version,
    )
    return _to_response(report)


@router.get(
    "/word",
    summary="导出 Word 学习报告（浏览器直接下载）",
    description=(
        "生成一份 .docx 并作为附件返回，浏览器会直接弹出下载。\n\n"
        "文档包含：基本信息、使用情况（总时长 / 按天 / 每次明细）、"
        "错题整改情况（易错知识点排行 / 错误类型与语言分布 / 错题明细）、"
        "以及按规则生成的学习建议。**不需要联网、不需要配置模型 Key**。"
    ),
    response_class=Response,
    responses={
        200: {
            "content": {DOCX_MEDIA_TYPE: {}},
            "description": "Word 文档（作为附件下载）",
        }
    },
)
async def report_word(
    session: DbSession,
    settings: AppSettingsDep,
    range_value: str | None = Query(
        default="30", alias="range", description="7 / 30 / 90 / all（默认 30 天）"
    ),
    student_id: str | None = Query(default=None),
    x_student_id: str | None = Header(default=None, alias=STUDENT_HEADER),
) -> Response:
    sid = resolve_student_id(student_id, x_student_id)
    payload, filename = await ReportService.build_docx(
        session,
        student_id=sid,
        range_value=range_value,
        app_version=settings.app.version,
    )
    logger.info("导出学习报告：student=%s 文件名=%s %d 字节", sid, filename, len(payload))
    return Response(
        content=payload,
        media_type=DOCX_MEDIA_TYPE,
        headers={
            # ASCII 兜底（老浏览器）+ RFC 5987 中文文件名（现代浏览器优先用它）
            "Content-Disposition": (
                "attachment; filename=\"learning-report.docx\"; "
                f"filename*=UTF-8''{quote(filename)}"
            ),
            "Content-Length": str(len(payload)),
            # 让前端在 fetch 场景下也能读到文件名（跨域时不加这个读不到）
            "Access-Control-Expose-Headers": "Content-Disposition",
            "Cache-Control": "no-store",
        },
    )


__all__ = ["DOCX_MEDIA_TYPE", "router"]
