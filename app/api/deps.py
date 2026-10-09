"""FastAPI 依赖注入：数据库会话、全局配置与领域服务。

这里只保留学生端（AI 代码导师）需要的依赖。
原先还有 `RepoServiceDep` / `RetrievalServiceDep` / `SandboxRunnerDep` /
`TestAgentDep` / `FixAgentDep` / `AutoFixPlannerDep` 六个，属于「开源贡献端」，
已随该功能删除。

**删它们顺带解决了一个隐蔽的耦合**：`DbSession` 也定义在本文件里，
只要本文件还 import 检索/沙箱，那么**每一个**学生端接口
（哪怕只是读一次错题本）都会在导入时把 faiss、docker 这些重依赖拉进来。
依赖注入的"聚合文件"很容易变成这种隐形桥梁。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.tutor_agent import TutorAgent
from app.core.api_key_manager import ApiKeyManager
from app.core.api_key_manager import get_api_key_manager as _get_api_key_manager
from app.core.config import Settings, get_settings
from app.core.database import get_db
from app.core.llm_client import get_llm_client
from app.services.code_checker import CodeChecker
from app.services.code_fixer import CodeFixer
from app.services.comment_generator import CommentGenerator
from app.services.file_classifier import FileClassifier
from app.services.library_service import LibraryService

DbSession = Annotated[AsyncSession, Depends(get_db)]
AppSettingsDep = Annotated[Settings, Depends(get_settings)]


def get_api_key_manager() -> ApiKeyManager:
    """API Key 内存保管箱（进程内单例）。

    为什么必须是单例：Key 就存在这个对象的内存字典里，
    按请求新建的话"刚存进去的 Key"下一次请求就找不到了。

    测试里可以用 `reset_api_key_manager()` 清空，避免用例之间互相影响。
    """
    return _get_api_key_manager()


ApiKeyManagerDep = Annotated[ApiKeyManager, Depends(get_api_key_manager)]


def get_tutor_agent(settings: AppSettingsDep) -> TutorAgent:
    """AI 代码导师 Agent（检测 / 注释 / 改错 / 诊断）。

    与其它几个 AI 服务共用同一个进程级 LLM 客户端（httpx 连接池复用）；
    测试可用 `app.dependency_overrides[get_tutor_agent]` 注入假客户端。
    """
    return TutorAgent(settings, get_llm_client())


TutorAgentDep = Annotated[TutorAgent, Depends(get_tutor_agent)]


def get_library(settings: AppSettingsDep) -> LibraryService:
    """本地项目库服务。

    服务本身无状态（只持有配置），按请求构造即可；
    从 settings 现取现用，测试里覆盖 LIBRARY__ROOTS 后立刻生效。
    """
    return LibraryService(settings.library)


LibraryServiceDep = Annotated[LibraryService, Depends(get_library)]


def get_file_classifier(settings: AppSettingsDep) -> FileClassifier:
    """本地代码文件分类器（扫描 + 按语言归档）。

    无状态（只持有配置），按请求构造即可；从 settings 现取现用，
    测试里覆盖 CLASSIFIER__ROOT 后立刻生效。
    """
    return FileClassifier(settings.classifier)


FileClassifierDep = Annotated[FileClassifier, Depends(get_file_classifier)]


def get_code_checker(settings: AppSettingsDep) -> CodeChecker:
    """AI 自动检测器（本地静态检查 + 大模型深度检测）。

    与其它 Agent 共用同一个进程级 LLM 客户端（连接池复用）；
    测试可用 `app.dependency_overrides[get_code_checker]` 注入假客户端。
    """
    return CodeChecker(settings, get_llm_client())


CodeCheckerDep = Annotated[CodeChecker, Depends(get_code_checker)]


def get_code_fixer(settings: AppSettingsDep) -> CodeFixer:
    """代码改错器（本地分析 + AI 修正 + 本地复检）。

    内部复用 `CodeChecker` 的本地检查能力，保证「检测」与「改错」对
    "什么算问题"的判断完全一致；两者共用同一个进程级 LLM 客户端。
    测试可用 `app.dependency_overrides[get_code_fixer]` 注入假客户端。
    """
    return CodeFixer(settings, get_llm_client())


CodeFixerDep = Annotated[CodeFixer, Depends(get_code_fixer)]


def get_comment_generator(settings: AppSettingsDep) -> CommentGenerator:
    """代码注释生成器（本地分析 + AI 写注释 + 本地复检）。

    内部复用 `CodeChecker` 的本地解析能力，保证三个学习类功能
    （检测/改错/注释）对同一份代码的结构理解一致。
    测试可用 `app.dependency_overrides[get_comment_generator]` 注入假客户端。
    """
    return CommentGenerator(settings, get_llm_client())


CommentGeneratorDep = Annotated[CommentGenerator, Depends(get_comment_generator)]


__all__ = [
    "ApiKeyManagerDep",
    "AppSettingsDep",
    "CodeCheckerDep",
    "CodeFixerDep",
    "CommentGeneratorDep",
    "DbSession",
    "FileClassifierDep",
    "LibraryServiceDep",
    "TutorAgentDep",
    "get_api_key_manager",
    "get_code_checker",
    "get_code_fixer",
    "get_comment_generator",
    "get_file_classifier",
    "get_library",
    "get_tutor_agent",
]
