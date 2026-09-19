"""SQLAlchemy ORM 模型包。

统一继承 `app.core.database.Base`。导入本包即完成元数据注册，
`init_db()` 会据此自动建表。

**新增模型时务必在此导出，否则 create_all 不会建表。**

本包目前只有「AI 代码导师（学生端）」相关的表：

| 表 | 用途 |
| --- | --- |
| `tutor_records` | 学习流水账：每次检测/注释/改错/诊断都记一条 |
| `student_error_logs` | 错题本：只记真正出错的，按知识点归档 |
| `code_check_records` | AI 自动检测（深度版）的历史 |
| `code_fix_records` | AI 改错（深度版）的历史 |
| `comment_records` | AI 注释生成的历史 |
| `classified_files` | 文件自动分类的台账 |
"""

from app.models.base import utc_now
from app.models.code_check import CodeCheckRecord
from app.models.code_fix import CodeFixRecord
from app.models.comment import CommentRecord
from app.models.error_log import StudentErrorLog
from app.models.file_record import ClassifiedFileRecord
from app.models.tutor import TutorAction, TutorRecord

__all__ = [
    "ClassifiedFileRecord",
    "CodeCheckRecord",
    "CodeFixRecord",
    "CommentRecord",
    "StudentErrorLog",
    "TutorAction",
    "TutorRecord",
    "utc_now",
]
