"""启动前自检：把"缺库"翻译成一句能照着做的中文。

## 为什么要有这个脚本

`start.bat` 原来是用一行写死的 import 清单探测依赖：

    python -c "import fastapi, uvicorn, sqlalchemy, tree_sitter, ... "

这份清单**手写维护**，于是漏掉了 `greenlet` —— 而它恰恰是新电脑上最容易缺的那个：
SQLAlchemy 的异步引擎依赖 greenlet，但 sqlalchemy 对它的依赖带平台标记
（`platform_machine == "aarch64" or ... "x86_64" or ...`），在 ARM64 等机器上
pip 会直接跳过。结果是：

    sqlalchemy 装得上 → 探测清单里没有 greenlet → 判定"依赖已就绪" →
    启动 uvicorn → ModuleNotFoundError: No module named 'greenlet' → 一屏英文堆栈

学生看到的是英文 traceback，完全不知道该怎么办。

所以这里改成**读 requirements.txt**：清单加一项，自检自动跟着查，不会再漂移。
另外补一个真正建异步引擎的冒烟测试 —— 光 import 模块查不出 greenlet 那种
"装得上、import 也不报错、真用才炸"的问题。

## 用法

    python tools/preflight.py            # 打印人话报告（缺什么、怎么修）
    python tools/preflight.py --quiet    # 只给退出码，供 start.bat 判断要不要装依赖

退出码：0 = 一切就绪；1 = 有缺项（报告里已写明怎么修）。
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata as metadata
import re
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
REQUIREMENTS = BASE_DIR / "requirements.txt"

# 包名 -> 真实顶层模块名。绝大多数包名把 `-` 换成 `_` 就是模块名，
# 少数对不上（如 python-dotenv 的模块叫 dotenv）在这里点明。
# 优先用包元数据里的 top_level.txt，它才是权威来源；这个表只是兜底。
MODULE_ALIASES = {
    "python-dotenv": "dotenv",
    "python-multipart": "python_multipart",
    "uvicorn": "uvicorn",
    "tree-sitter": "tree_sitter",
}

REQUIRED_PYTHON = (3, 11)


def parse_requirements(path: Path) -> list[str]:
    """从 requirements.txt 里取出包名（去掉版本、extras、注释与空行）。"""
    names: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        # "uvicorn[standard]>=0.30.0" -> "uvicorn"
        name = re.split(r"[<>=!~\[\s;]", line, maxsplit=1)[0].strip()
        if name:
            names.append(name)
    return names


def candidate_modules(package: str) -> list[str]:
    """列出"这个包可能叫什么模块"，按可信度排序。

    不能只用包元数据的 top_level.txt：有些包（tree-sitter-python 之类）
    在那里列的是 `_binding` 这种**私有内部模块**，直接 import 必然失败，
    于是自检会把一个装得好好的环境误判成缺项。

    所以顺序是：包名规范化 → 已知别名 → 元数据里非下划线开头的模块。
    """
    ordered: list[str] = []
    normalized = package.replace("-", "_")
    ordered.append(normalized)
    if package in MODULE_ALIASES:
        ordered.append(MODULE_ALIASES[package])
    try:
        dist = metadata.distribution(package)
    except metadata.PackageNotFoundError:
        return []
    try:
        declared = dist.read_text("top_level.txt") or ""
    except Exception:  # noqa: BLE001 - 元数据格式不一时不该让自检崩掉
        declared = ""
    for line in declared.splitlines():
        name = line.strip()
        if name and not name.startswith("_") and name not in ordered:
            ordered.append(name)
    return ordered


def check_imports(packages: list[str]) -> list[tuple[str, str]]:
    """逐个包尝试 import，返回 [(包名, 原因)] 形式的缺项清单。

    一个包只要**有一个**候选模块能导入就算它在（同一个发行包里通常既有
    正式模块也有若干内部模块，全都要求导入成功会误报）。
    """
    missing: list[tuple[str, str]] = []
    for package in packages:
        candidates = candidate_modules(package)
        if not candidates:
            missing.append((package, "没有安装"))
            continue
        first_error: str | None = None
        for module in candidates:
            try:
                importlib.import_module(module)
            except Exception as exc:  # noqa: BLE001 - 任何导入失败都算候选不合格
                if first_error is None:
                    first_error = f"import {module} 失败：{exc}"
                continue
            break
        else:
            missing.append((package, first_error or "无法导入"))
    return missing


def check_async_engine() -> str | None:
    """真正建一次异步引擎 —— 这一步才能查出 greenlet。

    只 import sqlalchemy 是不够的：greenlet 是在**使用**异步引擎时才被导入的，
    所以缺了它，`import sqlalchemy` 照样成功（这正是问题难查的原因）。
    """
    try:
        from sqlalchemy.ext.asyncio import create_async_engine
    except Exception as exc:  # noqa: BLE001
        return f"导入 sqlalchemy 的异步接口失败：{exc}"

    path = Path(tempfile.mkdtemp()) / "preflight.db"
    try:
        engine = create_async_engine(f"sqlite+aiosqlite:///{path.as_posix()}")
    except Exception as exc:  # noqa: BLE001
        return f"创建异步引擎失败：{type(exc).__name__}: {exc}"

    import asyncio

    async def smoke() -> None:
        from sqlalchemy import text

        async with engine.connect() as connection:
            await connection.execute(text("select 1"))

    try:
        asyncio.run(smoke())
    except ModuleNotFoundError as exc:
        return (
            f"异步数据库引擎不可用：{exc}\n"
            "        这几乎总是缺 greenlet —— SQLAlchemy 的异步引擎靠它，"
            "但它对它的依赖带平台标记，\n"
            "        在部分机器上 pip 不会自动装上。"
        )
    except Exception as exc:  # noqa: BLE001
        return f"异步引擎冒烟测试失败：{type(exc).__name__}: {exc}"
    finally:
        import asyncio as _asyncio

        _asyncio.run(engine.dispose())
    return None


def check_tree_sitter() -> str | None:
    """语法包加载一次 —— 缺了它服务能跑，但 C/Java 的结构解析会静默失败。"""
    try:
        import tree_sitter_c
        import tree_sitter_java
        import tree_sitter_python
        from tree_sitter import Language, Parser
    except Exception as exc:  # noqa: BLE001
        return f"语法包导入失败：{exc}"

    try:
        parser = Parser(Language(tree_sitter_python.language()))
        tree = parser.parse(b"def add(a, b):\n    return a + b\n")
        if tree.root_node.has_error:
            return "Python 语法包解析异常（能导入但解析报错）"
        for module in (tree_sitter_c, tree_sitter_java):
            Language(module.language())
    except Exception as exc:  # noqa: BLE001
        return f"语法包使用失败：{type(exc).__name__}: {exc}"
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description="启动前依赖自检")
    parser.add_argument("--quiet", action="store_true", help="只给退出码，不打印报告")
    args = parser.parse_args()

    problems: list[str] = []

    if sys.version_info < REQUIRED_PYTHON:
        problems.append(
            f"Python 版本过低：当前 {sys.version.split()[0]}，"
            f"需要 {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]} 或更高"
        )

    packages = parse_requirements(REQUIREMENTS) if REQUIREMENTS.exists() else []
    if not packages:
        problems.append(f"读不到依赖清单：{REQUIREMENTS}")

    for package, reason in check_imports(packages):
        problems.append(f"{package}：{reason}")

    # 前两项过了才做冒烟测试：缺 sqlalchemy 时再建引擎只会报重复的错
    if not problems:
        for label, detail in (
            ("异步数据库引擎", check_async_engine()),
            ("多语言语法解析", check_tree_sitter()),
        ):
            if detail:
                problems.append(f"{label}：{detail}")

    if not problems:
        if not args.quiet:
            print(f"依赖自检通过（{len(packages)} 个包，Python {sys.version.split()[0]}）")
        return 0

    if not args.quiet:
        print()
        print("=" * 72)
        print("[错误] 依赖自检未通过，服务无法启动")
        print("=" * 72)
        for item in problems:
            print(f"  · {item}")
        print()
        print("  解决办法（在项目目录下执行）：")
        print("      .venv\\Scripts\\python -m pip install -r requirements.txt")
        print()
        print("  或者更省事：删掉项目下的 .venv 文件夹，重新双击 start.bat。")
        print("  start.bat 会自动建虚拟环境并把依赖装齐。")
        print("=" * 72)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
