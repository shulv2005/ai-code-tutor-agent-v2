"""SQLAlchemy ORM 模型包。

统一继承 `app.core.database.Base`。导入本包即完成元数据注册，
`init_db()` 会据此自动建表。

**新增模型时务必在此导出，否则 create_all 不会建表。**

本包目前只有「AI 代码导师（学生端）」相关的表：

| 表 | 用途 |
| --- | --- |
| `student_error_logs` | 错题本：每次诊断判定有错时归档一条，按知识点聚合 |
| `usage_sessions` | 使用时长：每次双击 start.bat 起一条，前端心跳续期 |
| `code_check_records` | AI 自动检测（深度版）的历史 |
| `code_fix_records` | AI 改错（深度版）的历史 |
| `comment_records` | AI 注释生成的历史 |
| `classified_files` | 文件自动分类的台账 |

原先还有一张 `tutor_records`（页面每次操作都记一条流水账），它唯一的读取方是
左下角的「历史记录」列表。该功能已删除，表与模型也一并移除。
"""

from app.models.base import utc_now
from app.models.code_check import CodeCheckRecord
from app.models.code_fix import CodeFixRecord
from app.models.comment import CommentRecord
from app.models.error_log import StudentErrorLog
from app.models.usage import UsageSession

__all__ = [
    "CodeCheckRecord",
    "CodeFixRecord",
    "CommentRecord",
    "StudentErrorLog",
    "UsageSession",
    "utc_now",
]
