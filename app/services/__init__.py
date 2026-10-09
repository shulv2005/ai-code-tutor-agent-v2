"""服务层：学生端（AI 代码导师）的领域能力。

按功能分：

| 模块 | 职责 |
| --- | --- |
| `tutor_service` | 学习流水账 + **错题本**的读写与知识点聚合 |
| `code_checker` / `code_check_service` | 本地静态检查 + AI 深度检测 |
| `code_fixer` / `code_fix_service` | 本地分析 → AI 修正 → 本地复检 |
| `comment_generator` / `comment_service` | 三层中文注释 + 未改动复检 |
| `repo/` | 多语言代码解析（只保留解析，Git 克隆已删除） |
"""

