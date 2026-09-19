"""AI 代码导师接口：/api/v1/tutor

面向前端「学生代码学习」页面的能力：
  POST /analyze   上传文件或贴代码 -> 自动识别语言 + 解析结构（不依赖大模型）
  POST /check     AI 检测：评分 + 问题清单 + 做得好的地方
  POST /comment   AI 生成通俗中文注释
  POST /fix       AI 自动改错：修正后代码 + 逐条修改说明
  POST /diagnose  AI 诊断（错题本 + 举一反三）：诊断结论 + 新题目 + 知识点解析
  GET  /errors    错题本列表 + 易错知识点聚合
  DELETE /errors/{id}  删除一条错题

设计说明：
- `/analyze` 完全不依赖大模型，因此**没配置模型时前端依然可用**，
  能演示文件上传、语言分类、代码结构展示；几个 AI 按钮会给出明确提示。
- **除 `/diagnose` 外，几个 AI 接口都不落库**（不写任何表）：
  以前的 `tutor_records` 流水账只为页面左下角的「历史记录」列表服务，
  那个功能已删除，写入路径随之移除。
- `/diagnose` 额外做两件事（见该函数的 docstring）：
    1. 调用前先捞出**最近的历史错题**，拼进提示词；
    2. 判定有错时把这次错误写进**错题本**（student_error_logs）。
- **模型可以在页面上选**：AI 接口都接受可选的表单字段
  `model_id`（用哪个模型）与 `api_key`（用户自己填的 Key），
  另外认 `X-Session-Id` 请求头（Key 存在服务端内存会话里的时候用）。
  一个都不传时，就用后端 `.env` 里配好的默认模型——保证老用法完全不受影响。
- **学生身份**：本项目是单机版（自己电脑上跑），错题本不需要区分人，
  所有记录统一归档到固定身份 `local`（见 `LOCAL_STUDENT_ID`）。
  接口仍然可选接受 `X-Student-Id` 请求头或 `student_id` 字段——
  脚本按需求文档的 JSON 契约批量跑一个班时，传了就能按人分开记。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import (
    APIRouter,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
    status,
)

from app.agents.tutor_agent import TutorAgent, format_error_history
from app.api.deps import ApiKeyManagerDep, AppSettingsDep, DbSession, TutorAgentDep
from app.core.api_key_manager import SESSION_HEADER, ApiKeyManager
from app.core.config import Settings
from app.core.llm_client import (
    LLMClientConfig,
    LLMConfigError,
    LLMError,
    LLMTimeoutError,
    dynamic_llm_client,
)
from app.core.trace import current_trace_id
from app.schemas.diagnose import (
    DiagnoseAnalysis,
    DiagnoseResponse,
    ErrorLogListResponse,
    ErrorLogRead,
    KnowledgePointStat,
    NewExercise,
    WrongQuestionBook,
)
from app.schemas.tutor import (
    AnalyzeResponse,
    CheckIssue,
    CheckResponse,
    CodeChange,
    CodeSymbolRead,
    CommentResponse,
    FixResponse,
    TutorStatusResponse,
)
from app.services.repo.language import (
    LANGUAGE_LABELS,
    SUFFIX_TO_LANGUAGE,
    supported_languages,
)
from app.services.repo.parser import parse_source
from app.services.text_utils import count_text_lines
from app.services.tutor_service import TutorService

logger = logging.getLogger(__name__)

router = APIRouter()

# 单次上传的代码大小上限：学生作业不会很大，超过基本是误传了文件
MAX_CODE_BYTES = 512 * 1024

# 学生身份相关的两个常量。
#
# **页面不再要求填学号**：本项目是"在自己电脑上跑"的单机版，
# 一个浏览器就一个学生，错题本不需要区分人。因此所有记录都归档到
# `LOCAL_STUDENT_ID` 这个固定身份下。
#
# 为什么**保留**数据库的 student_id 列与这个可选参数、而不是把列删掉：
#   · 列留着，"以后要支持多个学生"只需要加回一个输入框，不用改表、不用写迁移；
#   · HTTP 参数留着，需求文档里的 `POST /api/analyze` JSON 契约
#     （`{student_id, language, code_content}`）就仍然成立，
#     脚本/第三方传了学号也能正常按人分开归档。
LOCAL_STUDENT_ID = "local"

# 仍然接受这个请求头（可选）：传了就按传的归档，不传就是本地身份。
STUDENT_HEADER = "X-Student-Id"

# 每次诊断喂给模型的历史错题条数（需求指定 5-10 条）。
# 取 8 是因为：少于 5 条总结不出规律，多于 10 条既费 token 又会让
# 模型把注意力分散到很久以前的错题上。
HISTORY_ERROR_LIMIT = 8

# 易错知识点聚合返回几条（前端用进度条展示，5 条正好一屏）
WEAK_POINT_LIMIT = 5



# ---------------------------------------------------------------------------
# 公共逻辑
# ---------------------------------------------------------------------------
def _resolve_language(filename: str) -> str:
    """按文件名后缀识别语言；不支持的扩展名返回 unknown。"""
    detected = None
    lowered = filename.lower()
    for suffix, language in SUFFIX_TO_LANGUAGE.items():
        if lowered.endswith(suffix):
            detected = language
            break
    return detected or "unknown"


def _resolve_student_id(*candidates: str | None) -> str:
    """决定这次记录归到谁名下。

    单机版页面上没有"学号"这个输入框，所以正常情况下所有候选都是空的，
    返回固定身份 `local`。

    但仍然保留候选参数：`POST /api/analyze` 这个兼容别名按需求文档的契约
    可以带 `student_id`，脚本批量给一个班跑的时候传进来就能按人分开归档——
    功能是免费保留的（表里本来就有这一列），只是页面上不再要求。
    """
    for value in candidates:
        text = (value or "").strip()
        if text:
            # 截断而不是报错：宁可存一个被截断的标识，
            # 也不要因为多打了一个空格/换行就让整个诊断失败。
            return text[:64]
    return LOCAL_STUDENT_ID


def _analyze(filename: str, code: str) -> AnalyzeResponse:
    """本地解析代码：识别语言、提取函数与类。不调用大模型。"""
    language = _resolve_language(filename)
    raw = code.encode("utf-8", errors="replace")

    notes: list[str] = []
    if language == "unknown":
        notes.append(
            "无法从文件名识别语言，目前支持：" + "、".join(sorted(SUFFIX_TO_LANGUAGE))
        )
        parsed_symbols: list[CodeSymbolRead] = []
        parse_error = None
        imports: list[str] = []
    else:
        parsed = parse_source(raw, filename, language)
        parsed_symbols = [
            CodeSymbolRead(
                name=symbol.name,
                qualified_name=symbol.qualified_name,
                kind=symbol.kind,
                start_line=symbol.start_line,
                end_line=symbol.end_line,
                signature=symbol.signature,
                complexity=symbol.complexity,
            )
            for symbol in parsed.symbols
        ]
        parse_error = parsed.parse_error
        imports = [item.module for item in parsed.imports]
        if parse_error:
            notes.append("代码存在语法错误，符号解析可能不完整，但仍可用于 AI 检测")

    return AnalyzeResponse(
        filename=filename,
        language=language,
        language_label=LANGUAGE_LABELS.get(language, language),
        line_count=count_text_lines(code),
        char_count=len(code),
        size_bytes=len(raw),
        parse_error=parse_error,
        symbols=parsed_symbols,
        imports=imports,
        notes=notes,
        trace_id=current_trace_id(),
    )


async def _read_code(
    session_file: UploadFile | None, code: str | None, filename: str | None
) -> tuple[str, str]:
    """从「上传文件」或「表单里的代码文本」中取出代码与文件名。"""
    if session_file is not None and session_file.filename:
        raw = await session_file.read()
        if len(raw) > MAX_CODE_BYTES:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail=f"文件过大（{len(raw) // 1024} KB），上限为 {MAX_CODE_BYTES // 1024} KB",
            )
        if not raw.strip():
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="上传的文件是空的")
        # 优先 utf-8，失败再退化，避免一个 GBK 编码的作业直接报错
        for encoding in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
            try:
                return raw.decode(encoding), session_file.filename
            except UnicodeDecodeError:
                continue
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="无法识别文件编码"
        )

    if code and code.strip():
        return code, (filename or "untitled.py")

    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="请上传代码文件，或在表单中提供 code 字段",
    )


def _ai_error(exc: Exception) -> HTTPException:
    """把 LLM 异常映射成前端能看懂的中文提示。"""
    if isinstance(exc, LLMConfigError):
        # 两类情况要分开说：
        #   1. 学生在页面上选了模型但没填 Key —— 用异常自带的那句话（更具体）；
        #   2. 后端 .env 一个 Key 都没配 —— 用下面这句（告诉老师/同学去哪配）。
        message = str(exc).strip()
        if "网页" in message:
            return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=message)
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI 功能尚未启用：请在页面「模型设置」里填入 API Key，"
            "或在后端 .env 中配置 LLM__API_KEY 后重启服务。",
        )
    if isinstance(exc, LLMTimeoutError):
        return HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="AI 模型响应超时，请稍后重试或换一段更短的代码。",
        )
    if isinstance(exc, LLMError):
        return HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"AI 调用失败：{exc}"
        )
    return HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(exc))


@asynccontextmanager
async def _active_agent(
    default_agent: TutorAgent,
    settings: Settings,
    manager: ApiKeyManager,
    session_id: str | None,
    model_id: str | None,
    api_key: str | None,
) -> AsyncIterator[TutorAgent]:
    """挑出这次请求真正要用的 Agent。

    三种情况：
      1. 三个参数一个都没传 → 直接用注入进来的默认 Agent（= `.env` 里配的模型）。
         这条路径同时也是**测试的注入点**（`dependency_overrides`）。
      2. 传了 `api_key`（页面本次提交带的）→ 现造一个客户端，用它。
      3. 只传了 `X-Session-Id` / `model_id` → 从内存会话里取出对应的 Key 再造。

    为什么用 `dynamic_llm_client`（用完即关）：用户的 Key 只该在这次请求里活着，
    不能跟着全局单例的连接池一直留在内存中。
    """
    if not (session_id or model_id or api_key):
        yield default_agent
        return

    # 会话 Key / 页面 Key / 后端配置 Key 的优先级由 ApiKeyManager 统一决定
    credential = manager.resolve(session_id, model_id, api_key)
    config = LLMClientConfig.from_llm_settings(
        settings.llm, credential.model_id, credential.api_key
    )
    logger.info(
        "本次请求使用模型=%s（Key 来源=%s，Key=%s）",
        config.model_name,
        credential.source,
        config.masked_key(),
    )
    async with dynamic_llm_client(config) as client:
        yield TutorAgent(settings, client)


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------
@router.get(
    "/status",
    response_model=TutorStatusResponse,
    summary="AI 导师模块状态",
    description="前端据此决定三个 AI 按钮是否可点击，以及提示学生配置模型。",
)
async def tutor_status(agent: TutorAgentDep) -> TutorStatusResponse:
    languages = supported_languages()
    return TutorStatusResponse(
        ai_available=agent.llm.configured,
        model=agent.llm.model,
        supported_languages={name: LANGUAGE_LABELS.get(name, name) for name in languages},
        max_upload_bytes=MAX_CODE_BYTES,
    )


# ---------------------------------------------------------------------------
# 分析（不依赖大模型）
# ---------------------------------------------------------------------------
@router.post(
    "/analyze",
    response_model=AnalyzeResponse,
    summary="分析代码（识别语言 + 解析结构）",
    description=(
        "支持两种提交方式：上传文件（multipart/form-data）或直接提交代码文本。"
        "本接口不调用大模型，因此在未配置 AI 时依然可用。"
    ),
)
async def analyze_code(
    session: DbSession,
    file: UploadFile | None = File(default=None, description="代码文件（.c/.java/.py 等）"),
    code: str | None = Form(default=None, description="也可以直接提交代码文本"),
    filename: str | None = Form(default=None, description="代码文本对应的文件名"),
) -> AnalyzeResponse:
    content, name = await _read_code(file, code, filename)
    # 纯本地解析：识别语言 + 提取函数/类，不落库、不调大模型
    return _analyze(name, content)


# ---------------------------------------------------------------------------
# AI 检测
# ---------------------------------------------------------------------------
@router.post(
    "/check",
    response_model=CheckResponse,
    summary="AI 检测代码",
    description="返回评分、问题清单（含修改建议）与做得好的地方。",
)
async def check_code(
    session: DbSession,
    agent: TutorAgentDep,
    settings: AppSettingsDep,
    manager: ApiKeyManagerDep,
    file: UploadFile | None = File(default=None),
    code: str | None = Form(default=None),
    filename: str | None = Form(default=None),
    model_id: str | None = Form(default=None, description="用哪个模型（页面下拉框选的）"),
    api_key: str | None = Form(default=None, description="用户自己填的 API Key（可空）"),
    x_session_id: str | None = Header(default=None, alias=SESSION_HEADER),
) -> CheckResponse:
    content, name = await _read_code(file, code, filename)
    language = _resolve_language(name)
    if language == "unknown":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无法识别代码语言，请使用 .c / .java / .py 等受支持的扩展名。",
        )

    try:
        async with _active_agent(
            agent, settings, manager, x_session_id, model_id, api_key
        ) as active:
            result = await active.check(content, filename=name, language=language)
    except Exception as exc:  # noqa: BLE001 - 统一映射为友好错误
        raise _ai_error(exc) from exc

    payload = result.payload
    issues = [
        CheckIssue(
            line=item.get("line") if isinstance(item.get("line"), int) else None,
            severity=item.get("severity", "info"),
            title=str(item.get("title", "")).strip() or "（未命名问题）",
            detail=str(item.get("detail", "")),
            suggestion=str(item.get("suggestion", "")),
        )
        for item in payload.get("issues", [])
    ]
    score = float(payload.get("score", 0.0))

    return CheckResponse(
        filename=name,
        language=language,
        score=score,
        level=TutorService.score_level(score),
        summary=str(payload.get("summary", "")),
        issues=issues,
        highlights=payload.get("highlights", []),
        model=result.model,
        trace_id=current_trace_id(),
    )


# ---------------------------------------------------------------------------
# 生成注释
# ---------------------------------------------------------------------------
@router.post(
    "/comment",
    response_model=CommentResponse,
    summary="生成中文注释",
    description="给学生代码加上通俗易懂的中文注释，帮助理解代码逻辑。",
)
async def comment_code(
    session: DbSession,
    agent: TutorAgentDep,
    settings: AppSettingsDep,
    manager: ApiKeyManagerDep,
    file: UploadFile | None = File(default=None),
    code: str | None = Form(default=None),
    filename: str | None = Form(default=None),
    model_id: str | None = Form(default=None, description="用哪个模型（页面下拉框选的）"),
    api_key: str | None = Form(default=None, description="用户自己填的 API Key（可空）"),
    x_session_id: str | None = Header(default=None, alias=SESSION_HEADER),
) -> CommentResponse:
    content, name = await _read_code(file, code, filename)
    language = _resolve_language(name)

    try:
        async with _active_agent(
            agent, settings, manager, x_session_id, model_id, api_key
        ) as active:
            result = await active.comment(content, filename=name, language=language)
    except Exception as exc:  # noqa: BLE001
        raise _ai_error(exc) from exc

    commented = result.payload.get("commented_code", content)
    summary = str(result.payload.get("summary", ""))

    return CommentResponse(
        filename=name,
        language=language,
        commented_code=commented,
        summary=summary,
        model=result.model,
        trace_id=current_trace_id(),
    )


# ---------------------------------------------------------------------------
# 自动改错
# ---------------------------------------------------------------------------
@router.post(
    "/fix",
    response_model=FixResponse,
    summary="AI 自动改错",
    description="找出代码错误，返回修正后的完整代码与逐条修改说明。",
)
async def fix_code(
    session: DbSession,
    agent: TutorAgentDep,
    settings: AppSettingsDep,
    manager: ApiKeyManagerDep,
    file: UploadFile | None = File(default=None),
    code: str | None = Form(default=None),
    filename: str | None = Form(default=None),
    model_id: str | None = Form(default=None, description="用哪个模型（页面下拉框选的）"),
    api_key: str | None = Form(default=None, description="用户自己填的 API Key（可空）"),
    x_session_id: str | None = Header(default=None, alias=SESSION_HEADER),
) -> FixResponse:
    content, name = await _read_code(file, code, filename)
    language = _resolve_language(name)

    try:
        async with _active_agent(
            agent, settings, manager, x_session_id, model_id, api_key
        ) as active:
            result = await active.fix(content, filename=name, language=language)
    except Exception as exc:  # noqa: BLE001
        raise _ai_error(exc) from exc

    payload = result.payload
    changes = [
        CodeChange(
            line=item.get("line") if isinstance(item.get("line"), int) else None,
            original=str(item.get("original", "")),
            fixed=str(item.get("fixed", "")),
            reason=str(item.get("reason", "")),
        )
        for item in payload.get("changes", [])
    ]
    fixed_code = payload.get("fixed_code", content)
    summary = str(payload.get("summary", ""))
    had_error = bool(payload.get("had_error", False))


    return FixResponse(
        filename=name,
        language=language,
        fixed_code=fixed_code,
        changes=changes,
        summary=summary,
        had_error=had_error,
        model=result.model,
        trace_id=current_trace_id(),
    )


# ---------------------------------------------------------------------------
# 诊断（错题本 + 举一反三）
# ---------------------------------------------------------------------------
@router.post(
    "/diagnose",
    response_model=DiagnoseResponse,
    summary="AI 诊断：错题本 + 举一反三",
    description=(
        "一次调用同时产出**上半屏的诊断结论**与**下半屏的错题本内容**。\n\n"
        "与 `/check` 的区别：`/check` 是「给这份代码打个分、列问题」，"
        "无状态、每次结果只跟代码有关；`/diagnose` 是「结合你以前的错题，"
        "看看你这次又错在哪、该补哪个知识点」，是**有状态**的——"
        "它会把最近的历史错题一起喂给模型，并在判定有错时归档进错题本。\n\n"
        "单机版不需要传身份：不传 `student_id` / `X-Student-Id` 时"
        "统一归档到固定身份 `local`。"
    ),
)
async def diagnose_code(
    session: DbSession,
    agent: TutorAgentDep,
    settings: AppSettingsDep,
    manager: ApiKeyManagerDep,
    file: UploadFile | None = File(default=None),
    code: str | None = Form(default=None),
    filename: str | None = Form(default=None),
    student_id: str | None = Form(
        default=None, description="可选身份标识；不传就是本机固定身份 local"
    ),
    model_id: str | None = Form(default=None, description="用哪个模型（页面下拉框选的）"),
    api_key: str | None = Form(default=None, description="用户自己填的 API Key（可空）"),
    x_session_id: str | None = Header(default=None, alias=SESSION_HEADER),
    x_student_id: str | None = Header(default=None, alias=STUDENT_HEADER),
) -> DiagnoseResponse:
    """完整流程：查历史错题 -> 拼提示词 -> 调模型 -> 写错题本 -> 返回。

    每一步的取舍都写在下面的注释里，这里先说三处最容易踩的地方：

    1. **历史错题必须在调用模型之前查**，而且是"上一次诊断留下的"，不含本次。
       如果先写库再查，模型就会看到一条还没被分析的"本次错误"，逻辑上自相矛盾。
    2. **只有 status == "error" 才写错题本**。代码没问题时也写，错题本会被
       "无错误"记录稀释，「易错知识点」统计直接失真。
    3. **模型没返回可解析 JSON 时返回 502，而不是当成"无错误"**。后者会让
       一次 AI 跑偏被伪装成"你的代码很好"，是这里最危险的一种静默失败。
    """
    content, name = await _read_code(file, code, filename)
    language = _resolve_language(name)
    if language == "unknown":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="无法识别代码语言，请使用 .c / .java / .py 等受支持的扩展名。",
        )
    # 身份：页面不传 → 落到本机固定身份 local；脚本传了 → 按传的归档
    sid = _resolve_student_id(student_id, x_student_id)

    # ---- 1. 查历史错题（本次调用要用它，所以必须在调模型之前）----
    history = await TutorService.recent_errors(
        session, student_id=sid, limit=HISTORY_ERROR_LIMIT
    )
    history_text = format_error_history(history)
    logger.info(
        "诊断开始：student=%s language=%s code=%d 字，历史错题 %d 条",
        sid,
        language,
        len(content),
        len(history),
    )

    # ---- 2. 拼提示词 + 调用大模型 ----
    try:
        async with _active_agent(
            agent, settings, manager, x_session_id, model_id, api_key
        ) as active:
            result = await active.diagnose(
                content, filename=name, language=language, history_text=history_text
            )
    except Exception as exc:  # noqa: BLE001 - 统一映射为友好错误
        raise _ai_error(exc) from exc

    # ---- 3. 解析失败就让学生重试，绝不伪造"无错误"结论 ----
    if not result.parsed:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                "AI 没有按约定格式返回结果（可能被截断或跑偏），请重试一次；"
                "若反复失败，换一段更短的代码或换一个模型试试。"
            ),
        )

    analysis_raw = result.payload["analysis"]
    book_raw = result.payload["wrong_question_book"]

    # ---- 4. 写流水账（无论对错都记，学生能回看"我诊断过哪些代码"）----

    # ---- 5. 判错 -> 写错题本 ----
    logged = False
    error_log_id: int | None = None
    if analysis_raw["status"] == "error":
        saved = await TutorService.save_error(
            session,
            student_id=sid,
            code_snippet=content,
            language=language,
            error_type=analysis_raw["error_type"],
            knowledge_points=analysis_raw["knowledge_points"],
            ai_feedback=analysis_raw["message"],
            filename=name,
            source="diagnose",
            model=result.model,
        )
        if saved is not None:
            logged = True
            error_log_id = saved.id
            logger.info(
                "已记入错题本：student=%s id=%s kp=%s", sid, saved.id, saved.knowledge_point
            )

    # ---- 6. 错题本统计：在写库**之后**再查，让返回值与前端刷新后看到的一致 ----
    # 若在写库前查，前端会同时拿到"共 5 条"（响应）和"共 6 条"（列表接口），
    # 学生一定会以为页面出 bug 了。
    total = await TutorService.count_errors(session, student_id=sid)
    weak = await TutorService.weak_points(session, student_id=sid, limit=WEAK_POINT_LIMIT)

    return DiagnoseResponse(
        student_id=sid,
        filename=name,
        language=language,
        language_label=LANGUAGE_LABELS.get(language, language),
        analysis=DiagnoseAnalysis(**analysis_raw),
        wrong_question_book=WrongQuestionBook(
            history_summary=book_raw["history_summary"],
            new_exercise=NewExercise(**book_raw["new_exercise"]),
            knowledge_explanation=book_raw["knowledge_explanation"],
        ),
        history_count=len(history),
        history_total=total,
        weak_points=[
            KnowledgePointStat(knowledge_point=point, count=count) for point, count in weak
        ],
        logged=logged,
        error_log_id=error_log_id,
        model=result.model,
        duration_ms=result.duration_ms,
        warnings=result.warnings,
        trace_id=current_trace_id(),
    )


# ---------------------------------------------------------------------------
# 错题本查询
# ---------------------------------------------------------------------------
@router.get(
    "/errors",
    response_model=ErrorLogListResponse,
    summary="错题本列表 + 易错知识点聚合",
    description=(
        "列出历史错题与易错知识点 Top N。\n\n"
        "单机版页面上没有学号输入框，所以前端调用时不带任何身份参数，"
        "后端会按固定的 `local` 身份返回这一台机器上的全部错题。"
    ),
)
async def list_errors(
    session: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    student_id: str | None = Query(
        default=None, description="可选身份标识；不传就是本机固定身份 local"
    ),
    x_student_id: str | None = Header(default=None, alias=STUDENT_HEADER),
) -> ErrorLogListResponse:
    sid = _resolve_student_id(student_id, x_student_id)
    items, total = await TutorService.list_errors(
        session, student_id=sid, limit=limit, offset=offset
    )
    weak = await TutorService.weak_points(session, student_id=sid, limit=WEAK_POINT_LIMIT)
    return ErrorLogListResponse(
        student_id=sid,
        total=total,
        items=[ErrorLogRead.model_validate(item) for item in items],
        weak_points=[KnowledgePointStat(knowledge_point=name, count=count) for name, count in weak],
    )


@router.delete(
    "/errors/{error_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除一条错题",
    description=(
        "只允许删除**本机身份**下的错题：身份标识会作为过滤条件一起查，"
        "脚本按人归档时删别人的会返回 404。"
    ),
)
async def delete_error(
    error_id: int,
    session: DbSession,
    student_id: str | None = Query(default=None),
    x_student_id: str | None = Header(default=None, alias=STUDENT_HEADER),
) -> None:
    sid = _resolve_student_id(student_id, x_student_id)
    if not await TutorService.delete_error(session, error_id=error_id, student_id=sid):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="错题不存在")
