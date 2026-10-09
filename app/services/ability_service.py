"""学生能力雷达图：把错题与检测记录折算成六个能力维度的分数。

## 这个分数是什么、不是什么（先说实话）

它是**基于历史错题与检测记录的启发式评估**，不是考试分数，也不是对代码质量的
绝对测量。作用只有一个：把"我到底哪一项弱"从一堆错题里**看出形状**来。
所以界面上必须同时给出「依据 N 条记录」，让老师和学生自己判断这个形状可不可信；
一条记录都没有时，六个维度都是满分（满分在这里的含义是"暂未发现弱点"）。

## 六个维度

| 维度 | 看什么 | 证据 |
| --- | --- | --- |
| 准确性 | 逻辑与结果是否正确 | 逻辑错误、返回值/运算符/条件类知识点 |
| 健壮性 | 边界、空值与异常处理 | 运行时错误、越界/空指针/异常类知识点 |
| 规范性 | 语法与编码规范 | 语法错误、格式/缩进/命名风格类问题 |
| 可读性 | 命名、注释与结构是否清楚 | 命名/注释/结构/模块化类知识点 |
| 高效性 | 算法与复杂度意识 | 复杂度/性能/循环嵌套/重复计算类知识点 |
| 完备性 | 分支覆盖与代码完整度 | 语法不通过、缺分支/未实现、知识点标注不出来 |

## 打分公式（刻意做成"能手算"的简单形式）

    score = 100 / (1 + 权重和 / 10)      再夹到 [15, 100]

  · 一条"错误"级证据权重 3、警告级 1.5、提示级 0.75；错题按错误类型给 3 或 1.5
  · 权重和 0 → 100 分；权重和 10 → 50 分；权重和 20 → 33 分

为什么用这个形状而不是"每个维度固定扣 N 分"：
  · 它是单调递减的，错得越多分越低，不会出现"扣到 0 分再错就不动了"；
  · 它不会一两条错误就把某一项打到谷底 —— 这是学习工具，不是考试判卷；
  · 反过来说，**维度之间的相对高低才是雷达图要传达的信息**，
    所以公式只需要保证"证据多的那一项一定更低"。

## 一条证据只归到一个维度

按下面的顺序取**第一个**命中的维度，而不是"命中几个就摊几次"：

    完备性（语法不通过是硬伤）→ 规范性 → 可读性 → 高效性 → 健壮性 → 准确性

顺序是有讲究的：越"具体"的维度越靠前，兜底的「准确性」放最后。
一件证据被同时算进两个维度会让雷达图虚高（两边都扣，形状被抹平），
而顺序固定之后，同一批错题每次跑出来的图**完全一样**，可以对比不同时期。
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import utc_now
from app.models.code_check import CodeCheckRecord
from app.models.error_log import StudentErrorLog
from app.services.report_insight import RADAR_GUIDE
from app.services.tutor_service import UNKNOWN_KNOWLEDGE_POINT

logger = logging.getLogger(__name__)

# 打分公式里的除数：权重和等于它时正好 50 分。
# 调大 → 整体分数偏高（宽容），调小 → 更严厉。10 是看着教学场景定的：
# 一个知识点错两三次会明显看出来，但不会因为一次手滑就"不及格"。
WEIGHT_DIVISOR = 10

# 分数下限：雷达图上一个 0 分的尖角既难看也没什么信息量
# （"这一项完全不行"和"这一项很差"对学生的行动指导是一样的）。
MIN_SCORE = 15

# 各类证据的权重
WEIGHT_ERROR = 3.0        # 错题里的语法/逻辑/运行时错误、检测记录里的 error
WEIGHT_WARNING = 1.5      # 检测记录里的 warning（多为潜在风险）
WEIGHT_INFO = 0.75        # 检测记录里的 info（多为风格提示）
WEIGHT_STYLE_ERROR = 1.5  # 错题里的"规范问题"（比真错轻一档）

# 检测记录参与统计的条数上限：太多会把很久以前的风格提示也算进来，
# 雷达图变成"历史平均"而不是"最近的状态"。
CHECK_RECORD_LIMIT = 200

# 每个维度最多留几条证据样本，供界面展示"凭什么给我这个分"
MAX_SAMPLES = 3

# 每个维度最多列几条"拖后腿的知识点"（报告里做弱项细分用）
MAX_CONTRIBUTORS = 4


@dataclass(frozen=True)
class Dimension:
    """一个能力维度：键、中文名、一句话说明、以及判定用的关键词。"""

    key: str
    label: str
    hint: str
    keywords: tuple[str, ...]


# 判定顺序即"优先级"：一条证据归到第一个命中的维度。
#
# 关键词刻意用**具体的词**而不是宽泛的词：像"循环"既可能是边界问题也可能是
# 效率问题，把它放进「高效性」会让所有循环类错误都跑到效率那一项去；
# 这里只在出现"复杂度/性能/嵌套"这类明确的效率信号时才归给高效性。
DIMENSIONS: tuple[Dimension, ...] = (
    Dimension(
        key="completeness",
        label="完备性",
        hint="分支覆盖与代码完整度",
        # 「缩进」放在规范性而不是这里：缩进不对是风格问题，代码本身是完整的。
        keywords=("语法", "syntax", "未实现", "缺", "遗漏", "分支", "覆盖", "不完整",
                  "未定义", "括号", "缺少", "边界条件没考虑"),
    ),
    Dimension(
        key="conventions",
        label="规范性",
        hint="语法与编码规范",
        # 注意「命名」不在这里 —— 命名好不好是**可读性**问题，
        # 放在规范性里会让可读性那一项永远拿满分、雷达图少一个角。
        keywords=("规范", "格式", "风格", "缩进", "分号", "空行", "编码",
                  "大小写", "驼峰", "下划线", "命名规范"),
    ),
    Dimension(
        key="readability",
        label="可读性",
        hint="命名、注释与结构是否清楚",
        keywords=("可读", "注释", "命名", "变量名", "函数名", "结构", "模块化",
                  "拆分", "含义", "语义清晰", "理解", "难以阅读"),
    ),
    Dimension(
        key="efficiency",
        label="高效性",
        hint="算法与复杂度意识",
        keywords=("复杂度", "性能", "效率", "嵌套", "重复计算", "递归", "空间",
                  "算法选择", "优化", "遍历"),
    ),
    Dimension(
        key="robustness",
        label="健壮性",
        hint="边界、空值与异常处理",
        keywords=("边界", "越界", "下标", "索引", "空", "none", "null", "nil", "指针",
                  "溢出", "异常", "除零", "未初始化", "类型转换", "内存", "容错"),
    ),
    Dimension(
        key="accuracy",
        label="准确性",
        hint="逻辑与结果是否正确",
        # 兜底维度：前面的都没命中就归到这里
        keywords=("逻辑", "返回值", "运算符", "条件", "判断", "赋值", "表达式",
                  "类型", "结果", "计算", "正确"),
    ),
)

DIMENSION_INDEX = {item.key: item for item in DIMENSIONS}

# 错误类型 → 兜底维度（关键词一个都没命中时用）
ERROR_TYPE_FALLBACK = {
    "语法错误": "completeness",
    "规范问题": "conventions",
    "运行时错误": "robustness",
    "逻辑错误": "accuracy",
}


def classify(primary: str, error_type: str = "", secondary: str = "") -> str:
    """把一条证据归到一个维度。

    三级判定，顺序不能换：

      1. **知识点**（`primary`）—— 这是模型给出的标签，最权威，先看它；
      2. **AI 反馈正文**（`secondary`）—— 知识点没命中时才看正文；
      3. **错误类型**（`error_type`）—— 都没命中时按类型兜底，最后兜到「准确性」。

    为什么要把知识点和正文分开：正文是自然语言，很容易带上无关的词。
    实测过一条 `函数返回值` + "函数名叫 add 却做减法" 的错题 ——
    把两者拼在一起匹配时，正文里的"函数名"先命中了**可读性**，
    而它其实是典型的准确性错误。知识点单独判定就没有这个问题。

    另外匹配文本里**不包含 error_type**：拼进去的话每条「规范问题」都会
    因为字面带"规范"二字先命中规范性，可读性那一项永远是满分。
    """
    for source in (primary, secondary):
        if not source:
            continue
        haystack = source.lower()
        for dimension in DIMENSIONS:
            if any(word.lower() in haystack for word in dimension.keywords):
                return dimension.key
    if error_type in ERROR_TYPE_FALLBACK:
        return ERROR_TYPE_FALLBACK[error_type]
    # 完全无法归类时归到"准确性"，并由调用方计入 unclassified ——
    # 这类"说不清哪一项"的占比会如实报出来，不藏起来。
    return "accuracy"


def matches_any_keyword(text: str, key: str) -> bool:
    """文本里是否出现了某个维度的关键词（用于统计"兜底了多少条"）。"""
    haystack = (text or "").lower()
    return any(word.lower() in haystack for word in DIMENSION_INDEX[key].keywords)


def weight_for_error(error_type: str) -> float:
    """一条错题的权重。规范问题比真错轻一档。"""
    if error_type == "规范问题":
        return WEIGHT_STYLE_ERROR
    return WEIGHT_ERROR


def weight_for_severity(severity: str) -> float:
    """检测记录里一条问题的权重。"""
    if severity == "error":
        return WEIGHT_ERROR
    if severity == "warning":
        return WEIGHT_WARNING
    return WEIGHT_INFO


def score_from_weight(weight: float) -> int:
    """权重和 → 0-100 的分数。公式见模块头部。"""
    if weight <= 0:
        return 100
    score = 100.0 / (1.0 + weight / WEIGHT_DIVISOR)
    return int(max(MIN_SCORE, min(100, round(score))))


def _clip(text: str, limit: int = 40) -> str:
    cleaned = " ".join((text or "").split())
    return cleaned if len(cleaned) <= limit else cleaned[: limit - 1] + "…"


class AbilityService:
    """从错题本与检测记录算出六维能力分。"""

    @staticmethod
    async def build(
        session: AsyncSession,
        *,
        student_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, object]:
        """算一份能力画像。

        返回结构同时服务于三处：网页雷达图、报告弹窗、导出的 Word。
        每一项都带 `evidence_count` 与最多 3 条 `samples` —— 界面上要能回答
        "凭什么给我这个分"，否则这个雷达图就只是一张好看的图。

        `since` / `until` 是**左闭右开**区间。有 `until` 才能把"上一个等长区间"
        单独算出来做对比（见 `build_with_trend`），否则两次统计会把同一批
        记录算两遍。
        """
        weights: dict[str, float] = {item.key: 0.0 for item in DIMENSIONS}
        counts: dict[str, int] = {item.key: 0 for item in DIMENSIONS}
        samples: dict[str, list[str]] = {item.key: [] for item in DIMENSIONS}
        # 每个维度"被什么拖下去的"：{(知识点, 错误类型): 次数}
        contributors: dict[str, Counter[tuple[str, str]]] = {
            item.key: Counter() for item in DIMENSIONS
        }
        error_rows: list[StudentErrorLog] = []
        error_total = 0
        check_total = 0
        unclassified = 0

        # ---- 1. 错题本（主要来源：只记真正出错的那次）----
        condition = StudentErrorLog.student_id == student_id
        if since is not None:
            condition = condition & (StudentErrorLog.created_at >= since)
        if until is not None:
            condition = condition & (StudentErrorLog.created_at < until)
        stmt = (
            select(StudentErrorLog)
            .where(condition)
            .order_by(StudentErrorLog.created_at.desc(), StudentErrorLog.id.desc())
            .limit(500)
        )
        for item in (await session.execute(stmt)).scalars().all():
            error_total += 1
            error_rows.append(item)

            # 「知识点都标注不出来」本身就是信号：说明提交的片段太短、
            # 模型看不出上下文。这是**完备性**的问题（代码/上下文不完整），
            # 不是逻辑问题，所以单独记一笔而不走关键词匹配。
            if item.knowledge_point == UNKNOWN_KNOWLEDGE_POINT:
                weights["completeness"] += WEIGHT_WARNING
                counts["completeness"] += 1
                if len(samples["completeness"]) < MAX_SAMPLES:
                    samples["completeness"].append(
                        _clip(f"未能定位知识点：{item.ai_feedback}")
                    )
                unclassified += 1
                continue

            key = classify(item.knowledge_point, item.error_type, item.ai_feedback)
            # 关键词一个都没命中、靠错误类型兜底的，单独计数 ——
            # 这个数偏高就说明关键词表该补词了，而不是数据有问题。
            if key == "accuracy" and not (
                matches_any_keyword(item.knowledge_point, "accuracy")
                or matches_any_keyword(item.ai_feedback, "accuracy")
            ):
                unclassified += 1
            weights[key] += weight_for_error(item.error_type)
            counts[key] += 1
            # 记下"是哪一类问题把这个维度拖下去的"：知识点 + 错误类型。
            # 报告要能回答"完备性差是因为什么"，光有一个分数是答不上来的。
            contributors[key][(item.knowledge_point, item.error_type)] += 1
            if len(samples[key]) < MAX_SAMPLES:
                samples[key].append(_clip(f"{item.knowledge_point}：{item.ai_feedback}"))

        # ---- 2. 检测记录（补充来源：分类更细，且带 syntax_ok）----
        check_condition = CodeCheckRecord.id.isnot(None)
        if since is not None:
            check_condition = CodeCheckRecord.created_at >= since
        if until is not None:
            check_condition = check_condition & (CodeCheckRecord.created_at < until)
        check_stmt = (
            select(CodeCheckRecord)
            .where(check_condition)
            .order_by(CodeCheckRecord.created_at.desc(), CodeCheckRecord.id.desc())
            .limit(CHECK_RECORD_LIMIT)
        )
        for record in (await session.execute(check_stmt)).scalars().all():
            check_total += 1
            # 语法没过：直接记一笔完备性，它是最硬的信号
            if not record.syntax_ok:
                weights["completeness"] += WEIGHT_ERROR
                counts["completeness"] += 1
                if len(samples["completeness"]) < MAX_SAMPLES and record.syntax_error:
                    samples["completeness"].append(_clip(record.syntax_error))

            issues = _parse_issues(record.issues_json)
            if issues is None:
                # 老记录 / JSON 坏了：退化成用三个计数，权重取中位
                weights["accuracy"] += WEIGHT_WARNING * record.error_count
                weights["robustness"] += WEIGHT_WARNING * record.risk_count
                weights["conventions"] += WEIGHT_INFO * record.style_count
                counts["accuracy"] += record.error_count
                counts["robustness"] += record.risk_count
                counts["conventions"] += record.style_count
                continue

            for issue in issues:
                key = classify(
                    str(issue.get("title", "")), "", str(issue.get("detail", ""))
                )
                weights[key] += weight_for_severity(str(issue.get("severity", "info")))
                counts[key] += 1
                if len(samples[key]) < MAX_SAMPLES and issue.get("title"):
                    samples[key].append(_clip(str(issue["title"])))

        dimensions = [
            {
                "key": item.key,
                "label": item.label,
                "hint": item.hint,
                "score": score_from_weight(weights[item.key]),
                "weight": round(weights[item.key], 2),
                "evidence_count": counts[item.key],
                "samples": samples[item.key],
                # 「这个维度为什么低」：按次数倒序的知识点 + 错误类型，最多 4 条。
                # 报告里"完备性差是由于什么情况导致的"就靠它回答。
                "contributors": [
                    {"knowledge_point": point, "error_type": error_type, "count": count}
                    for (point, error_type), count in contributors[item.key].most_common(
                        MAX_CONTRIBUTORS
                    )
                ],
            }
            for item in DIMENSIONS
        ]

        # 最高分 / 最低分。用 min/max 而不是排序后取两端：分数打平时
        # min/max 返回**先出现**的那一个，而 dimensions 是按固定顺序生成的，
        # 所以同一批数据每次跑出来的"最弱 / 最强"完全一样 ——
        # 排序取 [-1] 会取到最后一个，那才是随机的（踩过）。
        weakest = min(dimensions, key=lambda d: d["score"])
        strongest = max(dimensions, key=lambda d: d["score"])
        spread = strongest["score"] - weakest["score"]

        return {
            "dimensions": dimensions,
            "error_total": error_total,
            "check_total": check_total,
            "evidence_total": error_total + check_total,
            "unclassified": unclassified,
            "weakest": weakest["key"],
            "strongest": strongest["key"],
            "spread": spread,
            "summary": _summary(
                dimensions=dimensions,
                evidence_total=error_total + check_total,
                error_total=error_total,
                check_total=check_total,
                weakest=weakest,
                strongest=strongest,
                spread=spread,
                unclassified=unclassified,
            ),
            # 侧栏那张卡片地方小，用短句；长句留给弹窗与 Word
            "headline": _headline(dimensions, weakest, error_total + check_total),
            # 「怎么读这张图」由后端给，前端与 Word 共用同一份文案 ——
            # 两处各写一遍迟早会不一致。
            "guide": RADAR_GUIDE,
            # 原始错题行交给报告做"反复出错的语句"分析（见 report_insight.py）。
            # 只在这一个方法里返回，不往 API 契约里带 —— 那是几 KB 的代码文本。
            "error_rows": error_rows,
        }

    @staticmethod
    async def build_with_trend(
        session: AsyncSession,
        *,
        student_id: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, object]:
        """当前区间 + **与上一个等长区间**的对比。

        只有当前分数时，学生看到"健壮性 45 分"并不知道自己是在变好还是变差。
        补上"较上一周期 +12"，这张图才从快照变成趋势 —— 而趋势才是学生真正
        关心的（努力了有没有用）。

        只在能确定区间长度时才做对比：`range=all` 没有"上一个周期"可言，
        这时候 delta 全是 null，界面不显示这一列（而不是编一个 0 出来）。
        """
        profile = await AbilityService.build(
            session, student_id=student_id, since=since, until=until
        )

        previous: dict[str, object] | None = None
        if since is not None:
            span = (until or utc_now()) - since
            previous = await AbilityService.build(
                session, student_id=student_id, since=since - span, until=since
            )

        old_scores = (
            {item["key"]: item["score"] for item in previous["dimensions"]}
            if previous
            else {}
        )
        # 上一周期**一条记录都没有**时不做对比。
        #
        # 不加这个判断的话，上一周期六项都是 100 分，本期每项都会显示成
        # 几十分的"退步"（实测截图里就是 -31 / -55 一排红字）——
        # 但学生上一周期根本没做过题，这个"退步"是假的。
        # 没有基线就如实写"—"，不编数字出来。
        comparable = previous is not None and previous["evidence_total"] > 0
        for item in profile["dimensions"]:
            if not comparable:
                item["delta"] = None
                item["delta_label"] = ""
                continue
            delta = item["score"] - old_scores.get(item["key"], item["score"])
            item["delta"] = delta
            item["delta_label"] = (
                f"+{delta}" if delta > 0 else (str(delta) if delta < 0 else "0")
            )

        profile["has_trend"] = comparable
        profile["previous_total"] = previous["evidence_total"] if previous else 0
        profile["trend_note"] = _trend_note(profile, previous, comparable)
        return profile


def _headline(
    dimensions: list[dict], weakest: dict, evidence_total: int
) -> str:
    """侧栏用的一句话：只说最弱那一项，不铺开。"""
    if evidence_total == 0:
        return "暂无可分析的记录，六项均为满分"
    return (
        f"最弱：{weakest['label']} {weakest['score']} 分"
        f"（依据 {evidence_total} 条记录）"
    )


def _trend_note(
    profile: dict[str, object],
    previous: dict[str, object] | None,
    comparable: bool,
) -> str:
    """把"进步最大 / 退步最大"讲成一句话。"""
    if previous is None:
        return ""
    # 上一周期一条记录都没有：谈不上"变化"，如实说明，不编一个数字出来
    if not comparable:
        return "上一周期没有可对比的记录，本次是第一个有数据的周期。"

    dimensions = [item for item in profile["dimensions"] if item.get("delta") is not None]
    if not dimensions:
        return ""

    improved = max(dimensions, key=lambda d: d["delta"])
    declined = min(dimensions, key=lambda d: d["delta"])
    parts: list[str] = []
    if improved["delta"] > 0:
        parts.append(f"进步最大的是「{improved['label']}」（+{improved['delta']} 分）")
    if declined["delta"] < 0:
        parts.append(f"需要留意「{declined['label']}」（{declined['delta']} 分）")
    if not parts:
        parts.append("六项与上一周期相比没有变化")
    return "；".join(parts) + "。"


def _parse_issues(raw: str | None) -> list[dict] | None:
    """解析检测记录里的 issues_json；坏了返回 None（调用方退化成用计数）。"""
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    return [item for item in data if isinstance(item, dict)]


def _summary(
    *,
    dimensions: list[dict],
    evidence_total: int,
    error_total: int,
    check_total: int,
    weakest: dict,
    strongest: dict,
    spread: int,
    unclassified: int,
) -> str:
    """给雷达图配一句人话结论。"""
    if evidence_total == 0:
        return (
            "还没有可分析的记录，六个维度都是满分。做几次「诊断 + 举一反三」"
            "或「AI 检测」之后，这张图才会反映你的真实情况。"
        )

    parts = [
        f"依据 {evidence_total} 条记录（错题 {error_total} 条、检测 {check_total} 次）得出。",
        f"目前最弱的是「{weakest['label']}」（{weakest['score']} 分，"
        f"{weakest['evidence_count']} 条相关记录），",
        f"最强的是「{strongest['label']}」（{strongest['score']} 分）。",
    ]
    if spread <= 8:
        parts.append("六项比较均衡，没有明显短板。")
    else:
        parts.append(f"最强与最弱相差 {spread} 分，先把最弱那一项补起来，收益最大。")

    flat = [item for item in dimensions if item["score"] <= 60]
    if len(flat) >= 3:
        parts.append(
            f"另有 {len(flat)} 项低于 60 分，说明错的类型比较分散，"
            "建议先集中练一类题目，别同时铺开。"
        )
    if unclassified:
        parts.append(
            f"（另有 {unclassified} 条记录无法判定到具体维度，已按兜底规则计入准确性。）"
        )
    return "".join(parts)


__all__ = [
    "DIMENSIONS",
    "MIN_SCORE",
    "WEIGHT_DIVISOR",
    "AbilityService",
    "Dimension",
    "classify",
    "matches_any_keyword",
    "score_from_weight",
]
