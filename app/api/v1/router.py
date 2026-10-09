"""API v1 路由聚合：所有业务模块在这里挂载。

本文件是"模块整合是否完整"的唯一入口——改这里就能看出后端对外提供什么，
`app/main.py` 只 include 本文件一次。

**当前只有学生端（AI 代码导师）的模块。**
原先还挂着 repositories / search / agent / sandbox 四个「开源贡献端」模块
（仓库解析、混合检索、测试生成与自动修复、Docker 沙箱），已随该功能删除。
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import (
    auth,
    check,
    comment,
    files,
    fix,
    health,
    library,
    models,
    report,
    tutor,
    usage,
)

api_router = APIRouter()
api_router.include_router(health.router, prefix="/health", tags=["health"])
# AI 代码导师：面向前端「学生代码学习」页面
# （检测 / 注释 / 改错 / 诊断（错题本 + 举一反三）+ 历史记录）
api_router.include_router(tutor.router, prefix="/tutor", tags=["tutor"])
# 使用时长：start.bat 启动时开一条，前端心跳累加，供学习报告统计
api_router.include_router(usage.router, prefix="/usage", tags=["usage"])
# 学习报告：错题明细 + 易错知识点汇总，可导出 Word 给老师
api_router.include_router(report.router, prefix="/report", tags=["report"])
# 本地项目库：原地扫描并按语言分类（只读，不移动文件）
api_router.include_router(library.router, prefix="/library", tags=["library"])
# 本地代码文件自动分类：会真正把文件归档到 c/ java/ python/ 目录
api_router.include_router(files.router, prefix="/files", tags=["files"])
# AI 自动检测：先本地语法检查（AST / Tree-sitter），再 AI 深度检测
api_router.include_router(check.router, prefix="/check", tags=["check"])
# 代码改错：本地分析 → AI 修正 → 本地复检，含对比学习用的历史记录
api_router.include_router(fix.router, prefix="/fix", tags=["fix"])
# 代码注释生成：三层中文注释 + 代码未被改动的复检
api_router.include_router(comment.router, prefix="/comment", tags=["comment"])
# API Key 会话：网页上填的 Key 只存内存，这里负责存取与清除
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
# 模型清单：前端下拉框的数据源（只报"有没有 Key"，不给 Key）
api_router.include_router(models.router, prefix="/models", tags=["models"])

__all__ = ["api_router"]
