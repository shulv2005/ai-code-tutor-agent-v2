"""AI 代码导师 Agent：代码检测、注释生成、自动改错。

与项目里已有的「修复 Agent」的区别：
- 修复 Agent 面向的是**给开源项目提 PR**，输出 unified diff，简洁、工程化；
- 本模块面向的是**学生学编程**，要求学生看得懂，因此：
  · 检测要给出「为什么这是问题 + 怎么改」，而不是只报错；
  · 注释要通俗，讲清"这段在干什么"，而不是复述语法；
  · 改错要逐条列出「原来怎么写 / 改成什么 / 为什么」，而不是只给一份新代码。

三个功能都只依赖 `LLMClient` 协议，因此可以用假客户端离线测试整条链路。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.agents.extraction import extract_json_object as _extract_json_object
from app.core.config import Settings
from app.core.llm_client import LLMClient, LLMUsage
from app.core.trace import trace_span

logger = logging.getLogger(__name__)

PROMPT_VERSION = "tutor/v1"

# 「错题本 + 举一反三」诊断用的提示词版本，单独一个常量：
# 它和上面三个功能的提示词是**各自独立演进**的（诊断要求输出
# analysis + wrong_question_book 两段，结构完全不同），
# 混用一个版本号会让 Trace 里的"这次用的是哪版提示词"失去意义。
DIAGNOSE_PROMPT_VERSION = "tutor-diagnose/v1"

# 各语言的展示名，用于拼接提示词
_LANG_LABEL = {"python": "Python", "c": "C", "java": "Java",
               "javascript": "JavaScript", "go": "Go"}


def _lang_name(language: str) -> str:
    return _LANG_LABEL.get(language, language)


# ===========================================================================
# 一、代码检测
# ===========================================================================
CHECK_SYSTEM = """你是一位耐心细致的编程老师，正在帮学生检查代码。

你的任务：找出代码中的问题，并用学生能听懂的话告诉他怎么改。

检查范围（按重要性排序）：
1. **语法/编译错误**：写错的语法、类型不匹配、缺少分号等，这类问题最优先
2. **逻辑错误**：条件写反、循环边界错、变量用错、返回值不对
3. **运行时风险**：数组越界、空指针、除零、内存泄漏（C）、未关闭资源（Java）
4. **基础规范**：命名、缩进、魔法数字、重复代码

注意事项：
- 学生代码通常很短，不要把"没有写单元测试""没有写文档"这类当成错误
- 不要吹毛求疵地挑格式问题，重点放在真正影响程序正确性的地方
- 每条问题都要给出具体的修改建议，而不是泛泛而谈

只输出一个 JSON 对象，不要任何其他文字、不要用 markdown 代码块包裹。格式：
{
  "score": 85,
  "summary": "一句话总体评价",
  "issues": [
    {
      "line": 12,
      "severity": "error",
      "title": "除数为零会导致程序崩溃",
      "detail": "第 12 行 a / b 中的 b 可能是 0，运行时会抛出异常",
      "suggestion": "先判断 b 是否为 0：if (b == 0) { printf(\\"除数不能为0\\"); return -1; }"
    }
  ],
  "highlights": ["变量命名清晰易读", "缩进规范"]
}

规则：
- score 是 0-100 的整数，90+ 表示基本没问题，60 以下表示有严重错误
- severity 只能是 "error"（会出错）、"warning"（有隐患）、"info"（可以更好）
- 没有问题时代 issues 填空数组 []
- line 是问题所在的行号（从 1 开始）；说不准就填 null
- highlights 最多 3 条，没有就填空数组
"""


# ===========================================================================
# 二、生成注释
# ===========================================================================
COMMENT_SYSTEM = """你是一位编程老师，正在给学生的代码加注释，帮助他理解代码在做什么。

注释要求：
1. **用中文**，语言通俗，像老师讲给学生听
2. 解释「这段代码在干什么、为什么这么写」，而不是复述语法
   - 好："判断分数是否及格，60 分以上输出及格"
   - 差："if 语句，判断 score 是否大于等于 60"
3. 在关键位置加注释：函数开头说明功能与参数、复杂逻辑前说明思路、
   容易看错的变量说明含义
4. **不要给每一行都加注释**，只在真正需要解释的地方加
5. 保留学生原有的代码和注释，只做加法
6. 不要修改代码逻辑，一个字都不要改

只输出一个 JSON 对象，不要任何其他文字、不要用 markdown 代码块包裹。格式：
{
  "commented_code": "加了注释的完整代码，换行用 \\n 表示",
  "summary": "用一句话说明你在哪些地方加了注释"
}
"""


# ===========================================================================
# 三、自动改错
# ===========================================================================
FIX_SYSTEM = """你是一位编程老师，正在帮学生改正代码里的错误。

要求：
1. 先找出代码里的错误，然后给出修正后的**完整代码**
2. 逐条列出你改了什么、为什么这么改，语言要让学生看得懂
3. 如果代码其实没有错误，**不要为了改而改**：
   had_error 填 false，fixed_code 原样返回学生代码，changes 填空数组
4. 只改真正有问题的地方，不要顺手重构、不要改学生的代码风格
5. 保持学生原有的注释

只输出一个 JSON 对象，不要任何其他文字、不要用 markdown 代码块包裹。格式：
{
  "had_error": true,
  "summary": "一句话说明主要问题",
  "fixed_code": "修正后的完整代码，换行用 \\n 表示",
  "changes": [
    {
      "line": 8,
      "original": "return a - b;",
      "fixed": "return a + b;",
      "reason": "函数名叫 add（相加），但这里用了减号，应该改成加号"
    }
  ]
}
"""


# ===========================================================================
# 四、错题本 + 举一反三（诊断）
# ===========================================================================
# 这是需求里**必须逐字使用**的 System Prompt。改动前请先确认需求文档，
# 不要顺手"优化"措辞——提示词里的每一处强调（尤其 Output Format 与
# Constraints 里的两次禁止 markdown 代码块）都是为了压住两类高频跑偏：
#   1. 模型习惯性地在 JSON 外面裹 ```json 围栏或加一句"以下是结果："；
#   2. 代码没出错时模型会偷懒不写 wrong_question_book。
# 前者由 `extract_json_object` 兜底，后者由下面的 normalize 逻辑兜底。
DIAGNOSE_SYSTEM = """# Role
你是一个专业的 AI 代码导师与出题专家。你的核心任务是帮助学生分析代码错误，并结合学生的历史错题数据库，进行针对性的查漏补缺和举一反三。

# Workflow
1. 代码诊断：精准分析用户提交的代码，找出语法错误、逻辑漏洞或不良编程习惯。
2. 知识点提取：将代码中的错误抽象为具体的编程知识点。
3. 错题本分析：结合用户提供的【历史错题记录】，总结学生的薄弱环节和易错规律。
4. 举一反三：根据当前的错误和历史的薄弱点，设计一道全新的编程练习题，并提供详细的知识点解析。

# Output Format (CRITICAL)
你必须严格输出合法的 JSON 格式，绝对不能包含 JSON 以外的任何文本（不要有 ```json 这样的 markdown 标记）。JSON 结构必须完全符合以下模板：

{
  "analysis": {
    "status": "success" 或 "error",
    "error_type": "语法错误" 或 "逻辑错误" 或 "无错误",
    "message": "针对当前提交代码的具体分析诊断结果。",
    "knowledge_points": ["本次错误涉及的知识点1", "知识点2"]
  },
  "wrong_question_book": {
    "history_summary": "结合数据库中的历史错题记录，总结学生的错误频率和易错薄弱点。",
    "new_exercise": {
      "question_title": "针对薄弱点设计的新题目名称",
      "question_description": "题目的详细描述，包含输入输出要求。",
      "hint": "解题思路提示，不要直接给出完整答案。"
    },
    "knowledge_explanation": "对相关知识点（包含当前错误和历史薄弱点）的详细通俗解析，必须包含 Markdown 格式的代码示例。"
  }
}

# Constraints
1. 如果提交的代码完全正确（status为success），analysis部分的message需给予鼓励，knowledge_points可以留空或写"代码规范"。但 wrong_question_book 部分依然需要根据【历史错题记录】出题。如果历史错题记录为空，则随机出一个进阶题目。
2. 如果提交的代码有错误（status为error），new_exercise 必须针对该错误知识点进行出题，不可偏离。
3. knowledge_explanation 必须通俗易懂，使用 Markdown 语法来排版代码。
4. 只能输出纯 JSON 字符串，禁止输出 markdown 的代码块标记（不要输出 ```json）。
"""

# 历史错题为空时，User Prompt 里放这句而不是留一片空白：
# 空白会让模型以为"这一段忘了填"，从而自己编造几条历史错题（真的会）；
# 写清楚"这是第一次提交"，它才会按 Constraint 1 随机出一道进阶题。
_NO_HISTORY_HINT = "（该学生暂无历史错题记录，这是他的第一次提交，请按「随机出一个进阶题目」处理）"


def format_error_history(records: Sequence[Any], *, limit: int | None = None) -> str:
    """把错题记录列表拍成一段纯文本，喂给 User Prompt 的【历史错题记录】段。

    为什么拍成文本而不是塞 JSON：模型对"编号列表 + 中文字段名"的遵循度
    明显高于嵌套 JSON，而且我们只关心它总结规律，不需要它逐字段解析。
    序号从 1 开始编号，模型在 history_summary 里可以引用"第 2 条"这类指代。

    参数:
        records: 错题记录（ORM 对象或任何带这些属性的对象皆可）。
        limit:   最多输出几条；None 表示全部（调用方负责只查需要的条数）。

    返回:
        形如 "1. [2024-05-01] 语言：Python｜错误类型：逻辑错误｜知识点：循环边界\\n
              当时的代码：...\\n AI 反馈：..." 的文本；没有记录时返回提示句。
    """
    items = list(records)[:limit] if limit is not None else list(records)
    if not items:
        return _NO_HISTORY_HINT

    blocks: list[str] = []
    for index, item in enumerate(items, start=1):
        # 用 getattr 兜底：这些字段在 ORM 模型上一定有，但测试里常传
        # SimpleNamespace / dict，少写一个 getattr 就会让整个诊断接口 500。
        created = getattr(item, "created_at", None)
        when = created.strftime("%Y-%m-%d") if hasattr(created, "strftime") else "时间未知"
        language = str(getattr(item, "language", "") or "未知")
        error_type = str(getattr(item, "error_type", "") or "未分类错误")
        knowledge_point = str(getattr(item, "knowledge_point", "") or "未标注知识点")
        code = str(getattr(item, "code_snippet", "") or "").strip()
        feedback = str(getattr(item, "ai_feedback", "") or "").strip()

        # 代码片段截断：一份作业几十上百行，5-10 条拼起来会挤爆上下文窗口，
        # 而总结"易错规律"只需要看到出错的那几行。前后各留 600 字符。
        if len(code) > 1200:
            code = code[:600] + "\n…（中间内容略）…\n" + code[-600:]

        blocks.append(
            f"{index}. [{when}] 语言：{language}｜错误类型：{error_type}"
            f"｜知识点：{knowledge_point}\n"
            f"   当时的代码：\n{code or '（无代码片段）'}\n"
            f"   AI 反馈：{feedback or '（无）'}"
        )
    return "\n\n".join(blocks)


def build_diagnose_user_prompt(
    *, language: str, code: str, history_errors_data: str
) -> str:
    """按需求给定的 User Prompt 模板拼接提示词。

    单独抽成函数（而不是写在 `TutorAgent.diagnose` 里）有两个好处：
      · 可以脱离大模型直接对拼接结果写断言，提示词是否退化一眼可见；
      · 模板里的占位符只有这一处，改格式不用翻业务逻辑。
    """
    return (
        "请根据以下信息，完成代码分析、错题本记录和举一反三出题任务。\n\n"
        "【当前提交的代码】\n"
        f"编程语言：{_lang_name(language)}\n"
        f"代码内容：\n{code}\n\n"
        "【学生历史错题记录】\n"
        f"{history_errors_data}\n\n"
        "【任务要求】\n"
        "1. 严格按照 System Prompt 中定义的 JSON 格式输出。\n"
        "2. 分析当前代码，给出诊断结果。\n"
        "3. 结合历史错题记录，总结学生的薄弱点。\n"
        "4. 针对薄弱点或当前错误，设计一道新题目，并给出知识点解析。\n"
        "5. 再次强调：只输出纯 JSON 字符串，不要输出任何多余的文本或 markdown 标记！\n"
    )


# status 字段的同义词收敛表。模型时不时会回 "OK" / "通过" / "correct"，
# 直接把原始值透给前端会让"是否记入错题本"的判断失效。
_SUCCESS_STATUS_TOKENS = {
    "success",
    "ok",
    "pass",
    "passed",
    "correct",
    "true",
    "no_error",
    "无错误",
    "通过",
    "正确",
    "没有问题",
}


def normalize_diagnose_status(raw: Any) -> str:
    """把模型给的 status 收敛成 "success" 或 "error"。

    收敛不了时**一律按 "error" 处理**。这是一处刻意的偏向：把没出错的代码
    误记成错题，学生顶多多做一道题；反过来把真错题漏掉，错题本就失去了意义。
    真正"完全没解析出 JSON"的情况在更上层用 `TutorResult.parsed` 拦掉，
    不会走到这里。
    """
    text = str(raw or "").strip().lower()
    if text in _SUCCESS_STATUS_TOKENS or text.startswith("success"):
        return "success"
    return "error"


def _clean_knowledge_points(raw: Any) -> list[str]:
    """知识点列表：去空、去重、保序（顺序本身是模型给出的重要性排序）。"""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text and text not in seen:
            seen.add(text)
            cleaned.append(text)
    return cleaned


def _as_text(raw: Any) -> str:
    """把任意值转成展示用文本；None 转空串，避免前端渲染出 "None"。"""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    return str(raw).strip()


def normalize_diagnose_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """把模型返回的 JSON 收敛成前端契约的形状（缺字段补默认值）。

    为什么必须归一化：提示词里写死了结构，但模型仍会小概率漏字段
    （尤其是代码没问题时偷懒省略 new_exercise）。与其让前端到处写
    `data.wrong_question_book?.new_exercise?.hint || ''`，不如在后端
    一次性补齐——前端只处理"有值但内容为空"这一种情况。
    """
    analysis_raw = payload.get("analysis")
    analysis_raw = analysis_raw if isinstance(analysis_raw, dict) else {}
    status = normalize_diagnose_status(analysis_raw.get("status"))
    knowledge_points = _clean_knowledge_points(analysis_raw.get("knowledge_points"))

    error_type = _as_text(analysis_raw.get("error_type"))
    if not error_type:
        error_type = "无错误" if status == "success" else "未分类错误"

    book_raw = payload.get("wrong_question_book")
    book_raw = book_raw if isinstance(book_raw, dict) else {}
    exercise_raw = book_raw.get("new_exercise")
    exercise_raw = exercise_raw if isinstance(exercise_raw, dict) else {}

    return {
        "analysis": {
            "status": status,
            "error_type": error_type,
            "message": _as_text(analysis_raw.get("message")),
            "knowledge_points": knowledge_points,
        },
        "wrong_question_book": {
            "history_summary": _as_text(book_raw.get("history_summary")),
            "new_exercise": {
                "question_title": _as_text(exercise_raw.get("question_title")),
                "question_description": _as_text(exercise_raw.get("question_description")),
                "hint": _as_text(exercise_raw.get("hint")),
            },
            "knowledge_explanation": _as_text(book_raw.get("knowledge_explanation")),
        },
    }



@dataclass(slots=True)
class TutorResult:
    """三个功能共用的返回结构。"""

    payload: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    usage: LLMUsage = field(default_factory=LLMUsage)
    duration_ms: float = 0.0
    warnings: list[str] = field(default_factory=list)
    # 模型输出是否真的解析成了 JSON 对象。
    # 为什么需要这个标志：`payload` 解析失败时是空字典 `{}`，而空字典
    # 与"模型返回了合法 JSON 但字段缺失"在调用方看来是一样的。诊断接口
    # 要靠它区分"代码没问题"和"AI 跑偏了"——前者可以正常出题，
    # 后者应该让学生重试，绝不能把一次解析失败当成"无错误"记进错题本。
    parsed: bool = False


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON 对象（实现已上移到 `app.agents.extraction`）。

    这里保留同名再导出：既有调用方（含测试）一直是从本模块 import 的，
    不动它们的导入路径，避免为了"整理目录"而制造无谓的改动。
    """
    return _extract_json_object(text)


class TutorAgent:
    """AI 代码导师 Agent（检测 / 注释 / 改错）。"""

    # 类名以 Tutor 开头，不会被 pytest 误认成测试类；仍显式声明一次更保险
    __test__ = False

    def __init__(self, settings: Settings, llm: LLMClient) -> None:
        self._settings = settings
        self._llm = llm

    @property
    def llm(self) -> LLMClient:
        return self._llm

    # -- 共用调用 ---------------------------------------------------------
    async def _ask(
        self, system: str, user: str, *, kind: str, prompt_version: str = PROMPT_VERSION
    ) -> TutorResult:
        """发起一次调用并解析 JSON，带 Trace 记录。"""
        started = time.perf_counter()
        with trace_span(
            "agent.tutor",
            kind="agent",
            payload={"kind": kind, "prompt_version": prompt_version, "code_chars": len(user)},
            metadata={"model": self._llm.model},
        ) as span:
            from app.core.llm_client import LLMMessage

            response = await self._llm.chat(
                [
                    LLMMessage(role="system", content=system),
                    LLMMessage(role="user", content=user),
                ]
            )
            warnings: list[str] = []
            payload = extract_json_object(response.content)
            if payload is None:
                warnings.append("模型返回的内容不是合法 JSON，已按空结果处理")
                logger.warning("导师 Agent 输出解析失败，前 200 字：%s", response.content[:200])
                payload = {}

            duration = (time.perf_counter() - started) * 1000
            span.set_metadata(parsed=bool(payload), duration_ms=round(duration, 1))
            span.set_output({"keys": sorted(payload.keys())})

            return TutorResult(
                payload=payload,
                model=response.model or self._llm.model,
                usage=response.usage,
                duration_ms=duration,
                warnings=warnings,
                parsed=bool(payload),
            )

    # -- 1. 检测 ----------------------------------------------------------
    async def check(self, code: str, *, filename: str, language: str) -> TutorResult:
        """检查代码质量，返回评分、问题列表与亮点。"""
        user = (
            f"请检查下面这段 {_lang_name(language)} 代码（文件名：{filename}）。\n\n"
            f"```{language}\n{code}\n```"
        )
        result = await self._ask(CHECK_SYSTEM, user, kind="check")

        # 归一化：分数收敛到 0-100，缺失字段补默认值，避免前端拿到脏数据
        payload = result.payload
        try:
            score = float(payload.get("score", 0))
        except (TypeError, ValueError):
            score = 0.0
            result.warnings.append("模型给的评分不是数字，已置为 0")
        payload["score"] = max(0.0, min(100.0, score))

        issues = payload.get("issues")
        payload["issues"] = [item for item in issues if isinstance(item, dict)] if isinstance(
            issues, list
        ) else []
        for issue in payload["issues"]:
            issue.setdefault("severity", "info")
            if issue["severity"] not in ("error", "warning", "info"):
                issue["severity"] = "info"

        highlights = payload.get("highlights")
        payload["highlights"] = (
            [str(item) for item in highlights if str(item).strip()]
            if isinstance(highlights, list)
            else []
        )
        payload.setdefault("summary", "")
        return result

    # -- 2. 生成注释 ------------------------------------------------------
    async def comment(self, code: str, *, filename: str, language: str) -> TutorResult:
        """为代码生成通俗的中文注释。"""
        user = (
            f"请给下面这段 {_lang_name(language)} 代码加上通俗的中文注释"
            f"（文件名：{filename}）。\n\n```{language}\n{code}\n```"
        )
        result = await self._ask(COMMENT_SYSTEM, user, kind="comment")

        commented = result.payload.get("commented_code")
        if not isinstance(commented, str) or not commented.strip():
            # 模型没给出可用代码时，退回原代码并告警，前端据此提示学生重试
            result.warnings.append("模型没有返回带注释的代码，已回退为原代码")
            result.payload["commented_code"] = code
        result.payload.setdefault("summary", "")
        return result

    # -- 3. 自动改错 ------------------------------------------------------
    async def fix(self, code: str, *, filename: str, language: str) -> TutorResult:
        """找出错误并给出修正后的代码与逐条说明。"""
        user = (
            f"请检查并修正下面这段 {_lang_name(language)} 代码"
            f"（文件名：{filename}）。\n\n```{language}\n{code}\n```"
        )
        result = await self._ask(FIX_SYSTEM, user, kind="fix")

        payload = result.payload
        fixed = payload.get("fixed_code")
        if not isinstance(fixed, str) or not fixed.strip():
            result.warnings.append("模型没有返回修正后的代码，已回退为原代码")
            payload["fixed_code"] = code
        payload["had_error"] = bool(payload.get("had_error", False))

        changes = payload.get("changes")
        payload["changes"] = (
            [item for item in changes if isinstance(item, dict)]
            if isinstance(changes, list)
            else []
        )
        payload.setdefault("summary", "")
        return result

    # -- 4. 错题本 + 举一反三（诊断）---------------------------------------
    async def diagnose(
        self, code: str, *, filename: str, language: str, history_text: str = ""
    ) -> TutorResult:
        """一次调用同时产出「诊断结论」与「新题目 + 知识点解析」。

        参数:
            code:         学生本次提交的代码。
            filename:     文件名，仅用于提示词里给学生一点上下文。
            language:     语言标识（python / c / java …）。
            history_text: **已格式化好**的历史错题文本，见 `format_error_history`。
                          传空串表示该学生没有历史错题，模型会按提示词
                          Constraint 1 随机出一道进阶题。

        返回:
            `TutorResult`，其 `payload` 已归一化为
            `{"analysis": {...}, "wrong_question_book": {...}}`；
            `parsed=False` 表示模型压根没返回 JSON（调用方应让用户重试，
            而不能把这当成"代码没错误"）。
        """
        user = build_diagnose_user_prompt(
            language=language,
            code=code,
            history_errors_data=history_text or _NO_HISTORY_HINT,
        )
        result = await self._ask(
            DIAGNOSE_SYSTEM, user, kind="diagnose", prompt_version=DIAGNOSE_PROMPT_VERSION
        )
        if not result.parsed:
            # 没解析出 JSON 就原样返回，不要归一化出一个假的"无错误"结论
            return result

        result.payload = normalize_diagnose_payload(result.payload)
        # 提示词要求 knowledge_explanation 必须含 Markdown 代码示例。
        # 模型偶尔会忘记，这里只记一条 warning 供前端提示，**不替它编内容**。
        explanation = result.payload["wrong_question_book"]["knowledge_explanation"]
        if "```" not in explanation:
            result.warnings.append("AI 返回的知识点解析里没有 Markdown 代码示例")
        return result


__all__ = [
    "DIAGNOSE_PROMPT_VERSION",
    "DIAGNOSE_SYSTEM",
    "PROMPT_VERSION",
    "TutorAgent",
    "TutorResult",
    "build_diagnose_user_prompt",
    "extract_json_object",
    "format_error_history",
    "normalize_diagnose_payload",
    "normalize_diagnose_status",
]
