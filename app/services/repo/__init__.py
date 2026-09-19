"""多语言代码解析包（学生端「AI 检测 / 分析」用）。

| 模块 | 职责 |
| --- | --- |
| `language` | 扩展名 → 语言识别、tree-sitter 语法包加载、支持语言清单 |
| `python_parser` | Python 用标准库 `ast` 解析（比 tree-sitter 更准、自带错误行号） |
| `tree_sitter_parser` | 非 Python（C / Java / JS / Go）用 tree-sitter 解析 |
| `parser` | 统一门面：按语言挑引擎，返回 `ParsedFile` |
| `dto` | 解析结果的轻量数据结构（`ParsedFile` / `ParsedSymbol` …） |

原先这里还有 `git_service`（GitPython 克隆）与 `service`（克隆→解析→落库），
它们服务于「开源贡献端」的仓库解析，已随该功能一并删除。
现在这个包只剩"把一段代码解析成函数/类清单"这一件事——
学生端的 `/analyze` 与 AI 检测的本地语法检查都靠它。

**为什么不做再导出**：调用方直接
`from app.services.repo.parser import parse_source`。
包级再导出会让"谁依赖谁"变模糊，删功能时判断不了能不能删。
"""

__all__: list[str] = []
