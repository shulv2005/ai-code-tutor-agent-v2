"""LLM 输出提取测试：这是 Agent 链路最常见的故障点。

模型被要求"只输出 JSON、不要 markdown 围栏"，但实际情况是：
套围栏的、前后加解释的、写半句就断的，全都会出现。
这四个功能（检测/改错/注释/诊断）都靠 `extract_json_object` 兜底，
所以它必须自己扛住这些脏输入，而不是让整个请求 500。
"""

from __future__ import annotations

from app.agents.extraction import extract_json_object


# ---------------------------------------------------------------------------
# 正常情况
# ---------------------------------------------------------------------------
def test_extract_plain_json() -> None:
    assert extract_json_object('{"a": 1}') == {"a": 1}


def test_extract_nested_json() -> None:
    text = '{"analysis": {"status": "error", "knowledge_points": ["循环"]}}'
    parsed = extract_json_object(text)
    assert parsed is not None
    assert parsed["analysis"]["status"] == "error"


# ---------------------------------------------------------------------------
# 模型不守规矩的三种典型写法
# ---------------------------------------------------------------------------
def test_extract_json_in_markdown_fence() -> None:
    """最常见的一种：把 JSON 套在 ```json 里。"""
    text = '好的，结果如下：\n```json\n{"a": 1, "b": [2]}\n```\n希望有帮助。'
    assert extract_json_object(text) == {"a": 1, "b": [2]}


def test_extract_json_with_bare_fence() -> None:
    """不带语言标记的裸围栏同样要能剥掉。"""
    assert extract_json_object('```\n{"ok": true}\n```') == {"ok": True}


def test_extract_json_with_surrounding_prose() -> None:
    text = '分析结果：{"score": 80} 以上。'
    assert extract_json_object(text) == {"score": 80}


def test_extract_prefers_whole_text_when_it_is_valid() -> None:
    """整段就是合法 JSON 时不能被"取第一个 {"的逻辑带偏。"""
    assert extract_json_object('{"a": {"b": 1}}') == {"a": {"b": 1}}


# ---------------------------------------------------------------------------
# 解析不出来时必须返回 None（调用方据此走降级分支）
# ---------------------------------------------------------------------------
def test_extract_returns_none_for_non_json() -> None:
    assert extract_json_object("这段代码没有问题") is None
    assert extract_json_object("") is None
    assert extract_json_object("   ") is None


def test_extract_returns_none_for_json_array() -> None:
    """只接受对象；数组说明模型没按格式来，当成解析失败处理。"""
    assert extract_json_object("[1, 2, 3]") is None


def test_extract_returns_none_for_truncated_json() -> None:
    """被 max_tokens 截断的输出：不能崩，只能返回 None 让学生重试。"""
    assert extract_json_object('{"analysis": {"status": "err') is None
