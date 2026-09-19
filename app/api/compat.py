"""兼容别名接口：`POST /api/analyze`

需求文档里给的接口路径是 `POST /api/analyze`，请求体是最朴素的
`{ student_id, language, code_content }`（JSON）。而本项目内部约定的是
「业务接口都在 `/api/v1/<模块>/` 下，提交方式与其它导师接口一致（multipart 表单）」。

这里同时提供两条路径，各自服务一类调用方：

| 路径 | 提交方式 | 给谁用 |
| --- | --- | --- |
| `POST /api/v1/tutor/diagnose` | multipart 表单 | **学生端页面**（与 `/check`、`/fix` 写法一致） |
| `POST /api/analyze` | JSON | 校外脚本、Postman 调试、按需求文档对接的第三方 |

关键设计：别名**不重新实现一遍业务逻辑**，而是直接调用规范接口那个函数。
原因是这两条路径一旦各写一份，迟早会漂移——最常见的后果是"网页上好好的、
用脚本调就少了错题本归档"，而且这种差异不会报错。
FastAPI 的路径函数本身就是普通 Python 函数，直接 await 调用即可复用全部逻辑。

一个已知的取舍：别名只接受 `language`（需求就是这么定的），不接受 `filename`，
所以这里要按语言反推一个文件名（后端的语言识别一直是**按后缀**做的，
见 `_resolve_language`）。传了 `filename` 时以它为准——显式给的信息优先。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Header

from app.api.deps import ApiKeyManagerDep, AppSettingsDep, DbSession, TutorAgentDep
from app.api.v1.endpoints import tutor as tutor_endpoints
from app.core.api_key_manager import SESSION_HEADER
from app.schemas.diagnose import DiagnoseRequest, DiagnoseResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# 语言标识 -> 探测用后缀。只列本项目支持的语言；
# 未收录的语言会拼出一个不会被任何后缀匹配到的文件名，
# 从而走进"无法识别代码语言"的 400 分支，给出明确提示而不是猜测。
_LANGUAGE_SUFFIX: dict[str, str] = {
    "python": ".py",
    "c": ".c",
    "java": ".java",
    "javascript": ".js",
    "go": ".go",
}


def filename_for_language(language: str, filename: str | None = None) -> str:
    """给别名接口拼一个文件名，供后端识别语言。"""
    if filename and filename.strip():
        return filename.strip()
    key = (language or "").strip().lower()
    return f"submitted{_LANGUAGE_SUFFIX.get(key, '.' + key if key else '.txt')}"


@router.post(
    "/analyze",
    response_model=DiagnoseResponse,
    tags=["tutor"],
    summary="诊断（兼容别名，等价于 POST /api/v1/tutor/diagnose）",
    description=(
        "按需求文档的约定接受 JSON 请求体：`{ student_id, language, code_content }`。\n\n"
        "内部直接复用 `POST /api/v1/tutor/diagnose` 的实现，"
        "因此错题本归档、历史错题拼提示词等行为与网页端**完全一致**。\n\n"
        "返回体与规范接口相同：上半屏 `analysis` + 下半屏 `wrong_question_book`。"
    ),
)
async def analyze_alias(
    payload: DiagnoseRequest,
    session: DbSession,
    agent: TutorAgentDep,
    settings: AppSettingsDep,
    manager: ApiKeyManagerDep,
    x_session_id: str | None = Header(default=None, alias=SESSION_HEADER),
    x_student_id: str | None = Header(default=None, alias=tutor_endpoints.STUDENT_HEADER),
) -> DiagnoseResponse:
    filename = filename_for_language(payload.language, payload.filename)
    logger.debug("兼容别名 /api/analyze 转调 /api/v1/tutor/diagnose：%s", filename)

    # 直接调用规范接口的函数本体：所有参数都是显式 Python 实参，
    # 不经过 FastAPI 的表单解析，因此 file=None 是安全的（该参数有默认值）。
    return await tutor_endpoints.diagnose_code(
        session=session,
        agent=agent,
        settings=settings,
        manager=manager,
        file=None,
        code=payload.code_content,
        filename=filename,
        student_id=payload.student_id,
        model_id=None,
        api_key=None,
        x_session_id=x_session_id,
        x_student_id=x_student_id,
    )


__all__ = ["filename_for_language", "router"]
