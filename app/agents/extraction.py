"""LLM 输出解析：从自由文本里抠出 JSON 对象。

为什么单独成模块：模型输出格式极其不稳定（带解释文字、带 Markdown 围栏、
被 max_tokens 截断），而"提取失败"是 Agent 链路上最常见的故障点，
必须独立可测。

四个 AI 功能（检测 / 改错 / 注释 / 诊断）都要求模型只输出 JSON，
但**没有一次能指望它真的照做**——所以这层兜底是刚需。

> 原先本模块还包含"把模型输出提取成可运行的 pytest 测试代码"那一套
> （`extract_python_code` / `python_test_functions` / `check_generated_test`），
> 那是给已删除的「开源贡献端 · 生成测试」用的，已一并移除。
"""

from __future__ import annotations

import json
import re
from typing import Any


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出 JSON 对象。

    模型经常不守规矩：套 markdown 代码块、前后加解释、甚至用单引号。
    因此按「直接解析 → 去代码块 → 取第一个 {...} 区间」三级降级尝试。

    参数:
        text: 模型返回的原始文本（可能夹着解释文字与代码围栏）。

    返回:
        解析出的 dict；完全解析不出来时返回 None（调用方据此走降级分支，
        而不是让整个请求失败）。诊断接口会把它映射成 502「请重试」，
        而**不会**当成"代码没有错误"——那是最危险的一种静默失败。
    """
    if not text or not text.strip():
        return None

    candidates: list[str] = [text.strip()]

    # 去掉 ```json ... ``` 包裹
    fenced = re.search(r"```(?:json)?\s*(?P<body>.*?)```", text, re.DOTALL)
    if fenced:
        candidates.append(fenced.group("body").strip())

    # 取第一个 { 到最后一个 } 之间的内容（容忍前后有解释文字）
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


__all__ = ["extract_json_object"]
