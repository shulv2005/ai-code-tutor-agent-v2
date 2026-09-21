"""学生身份约定：单机版只有一个身份，多个模块都要用到它。

## 为什么单独抽一个文件

`LOCAL_STUDENT_ID` / `STUDENT_HEADER` / `_resolve_student_id()` 原先都写在
`app/api/v1/endpoints/tutor.py` 里。用到它们的地方一多（report、usage、
app/main.py 启动时写使用记录）就出现了两个问题：

1. **main.py 得反过来 import 一个接口模块**。组装层依赖业务端点，方向是反的，
   项目的整合自检也把这条当成硬约束（`main.py` 只做组装，不碰具体端点）；
2. 别的端点模块为了拿一个常量，被迫把整个 tutor 端点（连同它的一堆依赖）
   拉进自己的导入链。

抽到这里之后，谁要身份就从这里取，端点模块之间不再互相 import。

## 为什么身份是固定值而不是"页面上填的学号"

本项目是"在自己电脑上跑"的单机版，一个浏览器就一个学生，错题本不需要区分人。
页面也不提供学号输入框——忘填就点不动按钮，而错题本本来就只服务这台机器上的人。

但**列与参数都保留**：`student_error_logs.student_id` 还在，
`X-Student-Id` 请求头与 `student_id` 查询参数也还认。这样：
  · 需求文档里 `POST /api/analyze` 的 `{student_id, ...}` 契约依然成立；
  · 一个脚本批量给一个班跑时，传了就会按人分开归档，不用改表也不用写迁移。
"""

from __future__ import annotations

# 单机版的固定身份。所有"没传身份"的请求都归到它名下。
LOCAL_STUDENT_ID = "local"

# 仍然接受这个请求头（可选）：传了就按传的归档，不传就是本地身份。
STUDENT_HEADER = "X-Student-Id"

# 身份字符串的最大长度，与 student_error_logs.student_id / usage_sessions.student_id
# 的列宽保持一致（两处都是 String(64)）。
MAX_STUDENT_ID_LENGTH = 64


def resolve_student_id(*candidates: str | None) -> str:
    """决定这次记录归到谁名下。

    单机版页面上没有"学号"这个输入框，所以正常情况下所有候选都是空的，
    返回固定身份 `local`。

    截断而不是报错：宁可存一个被截断的标识，也不要因为多打了一个空格或换行
    就让整个请求失败。
    """
    for value in candidates:
        text = (value or "").strip()
        if text:
            return text[:MAX_STUDENT_ID_LENGTH]
    return LOCAL_STUDENT_ID


__all__ = [
    "LOCAL_STUDENT_ID",
    "MAX_STUDENT_ID_LENGTH",
    "STUDENT_HEADER",
    "resolve_student_id",
]
