"""请求体与代码体积的上限。

## 为什么单独一个模块

这几个常量同时被**两端**用到：
  · `app/api/v1/endpoints/tutor.py` —— 业务层校验（文件路径与粘贴路径都要拦）；
  · `app/main.py` —— 在**解析请求体之前**按 Content-Length 先挡一道。

而 `app/main.py` 有一条硬约束：不许 import `app.api.v1.endpoints`（见
`tests/test_integration.py::test_main_is_a_thin_assembly_layer`）。
把常量放在这里，两边都能用，main 也仍然是"只做组装"。

## 为什么解析前还要再挡一道（这条是真踩出来的）

前端把代码放在 multipart 的**普通字段**里提交，而 Starlette 对非文件字段有
1MB 的硬上限；超过时它抛出的 `MultiPartException` 会被直接转成

    HTTPException(400, detail="Part exceeded maximum size of 1024KB.")

—— 一句英文内部错误原封不动显示给学生（真实截图就是这样）。
业务上的上限本来是 512KB、本来有中文提示，学生却看到了另一句话。

所以在 `main.py` 里按 Content-Length 提前拦：口径与 `MAX_CODE_BYTES` 对齐，
留一点 multipart 封装余量，保证学生看到的始终是同一句中文提示。
"""

from __future__ import annotations

# 单次提交的代码大小上限（业务口径）。
# 学生作业不会很大，超过基本是误传了文件；而且再大也超出模型的上下文，
# 交给模型也没用 —— 与其让它失败在模型那边，不如在这里拦下并说清楚。
MAX_CODE_BYTES = 512 * 1024

# 请求体上限 = 代码上限 + multipart 边界等封装开销的余量。
# 取得比 MAX_CODE_BYTES 宽松：让"略微超一点"的请求进到业务层，
# 由业务层给出精确的中文提示，而不是在这一层被笼统地拒掉。
MAX_AI_BODY_BYTES = MAX_CODE_BYTES + 256 * 1024

# 这些接口接受"把代码放在表单字段里"的提交方式，因此需要上面那道保护。
# 只覆盖 AI 接口：上传文件、项目库等接口的体积策略不一样，不该被这里顺手改掉。
AI_FORM_PATH_PREFIX = "/api/v1/tutor/"


def too_large_detail(size: int) -> str:
    """统一"代码太大"的提示文案。

    刻意写成一句**告诉学生怎么办**的话，而不是只报一个数字 ——
    他能做的通常是"只粘出问题的那一段"，而不是去改小文件。
    """
    return (
        f"代码太大（{max(size, 0) // 1024} KB），单次上限 {MAX_CODE_BYTES // 1024} KB。"
        "请只把出问题的那一段代码粘进来，或在本地把文件改小后重试。"
    )


__all__ = [
    "AI_FORM_PATH_PREFIX",
    "MAX_AI_BODY_BYTES",
    "MAX_CODE_BYTES",
    "too_large_detail",
]
