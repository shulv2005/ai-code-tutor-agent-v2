"""Agent 层。

本包只剩「AI 代码导师」用到的东西：

- `tutor_agent`  代码检测 / 注释 / 改错 / 诊断（错题本 + 举一反三）
- `extraction`   LLM 自由文本 → JSON 对象（模型很少老实只输出 JSON）

原先这里还挂着「开源贡献端」的一整套 Agent（测试生成 / 修复 / 补丁 /
自动修复编排 / 检索上下文适配），已随该功能整体删除。

**不要再往这里堆再导出**：`__init__.py` 里 `import` 一堆东西会让
"谁依赖谁"变得完全看不出来——删功能时正是靠"有没有人 import 我"
来判断能不能删，再导出会把这条线索糊掉。调用方直接
`from app.agents.tutor_agent import TutorAgent` 就行。
"""

from app.agents.extraction import extract_json_object

__all__ = ["extract_json_object"]
