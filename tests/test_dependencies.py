"""依赖清单与启动前自检的测试。

这一组用例存在的理由很具体：**"新电脑上跑不起来"**。
现场报的是

    ModuleNotFoundError: No module named 'greenlet'

根因是三层一起漏：

  1. `greenlet` 没写进 requirements.txt（指望 sqlalchemy 带，但它的依赖带
     平台标记，ARM64 等机器上 pip 会跳过）；
  2. start.bat 的依赖探测清单是**手写**的，里面没有 greenlet；
  3. 于是判定"依赖已就绪"直接跳过安装，uvicorn 甩出一屏英文堆栈。

所以这里分别钉住这三层：清单里有它、探测清单不再手写（由 preflight 读清单）、
preflight 真的能查出"装得上但用不了"的情况。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from app.core.config import PROJECT_ROOT
from tools import preflight

REQUIREMENTS = PROJECT_ROOT / "requirements.txt"
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
START_BAT_SRC = PROJECT_ROOT / "tools" / "bat_src" / "start.bat.utf8"


def _requirement_names() -> set[str]:
    return {name.lower() for name in preflight.parse_requirements(REQUIREMENTS)}


# --------------------------------------------------------------- 清单完整性
def test_greenlet_is_declared_explicitly() -> None:
    """greenlet 必须写在清单里，不能指望 sqlalchemy 带。

    这是本次故障的根因：sqlalchemy 对 greenlet 的依赖带平台标记
    （platform_machine == "x86_64" / "aarch64" / ...），在不在名单里的机器上
    pip 会跳过它。SQLAlchemy 装得上、import 也不报错，直到真正建异步引擎才炸。
    """
    assert "greenlet" in _requirement_names(), "requirements.txt 里没有 greenlet"
    assert "greenlet" in PYPROJECT.read_text(encoding="utf-8").lower()


def test_every_critical_import_is_declared() -> None:
    """代码真正 import 的第三方库都要在清单里，否则新电脑装不齐。

    这一条挡的是"本地能跑是因为 .venv 里恰好有"，换台机器就缺。
    """
    required = {
        "fastapi", "uvicorn", "pydantic", "pydantic-settings", "python-dotenv",
        "python-multipart", "sqlalchemy", "aiosqlite", "greenlet", "httpx",
        "openai", "tree-sitter", "tree-sitter-python", "tree-sitter-javascript",
        "tree-sitter-go", "tree-sitter-c", "tree-sitter-java",
    }
    missing = sorted(required - _requirement_names())
    assert not missing, f"requirements.txt 缺少：{missing}"


def test_requirements_and_pyproject_declare_the_same_packages() -> None:
    """两份清单必须一致，否则 `pip install -r` 与 `pip install -e .` 装出不同环境。

    只比 `[project] dependencies`（不含 dev extras）：用 tomllib 读，
    别用正则扫全文 —— 正则会把 pytest/mypy 那些开发依赖也算进来，
    还会漏掉 `uvicorn[standard]` 这种带 extras 的写法（第一版两个错都犯了）。
    """
    import tomllib

    pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    declared = {
        re.split(r"[><=\[]", entry)[0].strip().lower()
        for entry in pyproject["project"]["dependencies"]
    }
    requirements = _requirement_names()
    assert requirements == declared, (
        f"只在 requirements.txt：{sorted(requirements - declared)}；"
        f"只在 pyproject.toml：{sorted(declared - requirements)}"
    )


# --------------------------------------------------------------- 解析逻辑
def test_parse_requirements_handles_extras_and_comments() -> None:
    """`uvicorn[standard]>=0.30.0` 要解析成 `uvicorn`，注释与空行要跳过。"""
    names = preflight.parse_requirements(REQUIREMENTS)
    assert "uvicorn" in names
    assert not any("[" in name or ">" in name for name in names)
    assert "greenlet" in names


def test_parse_requirements_on_a_synthetic_file(tmp_path: Path) -> None:
    sample = tmp_path / "requirements.txt"
    sample.write_text(
        "# 注释行\n"
        "\n"
        "fastapi>=0.115.0\n"
        "uvicorn[standard]>=0.30.0   # 行尾注释\n"
        "python-multipart>=0.0.9\n"
        "-r other.txt\n",
        encoding="utf-8",
    )
    assert preflight.parse_requirements(sample) == [
        "fastapi", "uvicorn", "python-multipart",
    ]


def test_candidate_modules_prefers_the_public_module() -> None:
    """tree-sitter-* 的 top_level.txt 里列了 `_binding` 这种私有模块。

    直接 import 它必然失败 —— 如果自检把候选模块全都要求导入成功，
    一个装得好好的环境会被误判成"缺依赖"（第一版就是这么误报的）。
    所以候选里必须是公开模块在前，且下划线开头的要被排除。
    """
    candidates = preflight.candidate_modules("tree-sitter-c")
    assert candidates[0] == "tree_sitter_c"
    assert not any(name.startswith("_") for name in candidates)


def test_check_imports_reports_missing_package() -> None:
    """不存在的包要如实报"没有安装"，而不是抛异常。"""
    missing = preflight.check_imports(["definitely-not-a-real-package-xyz"])
    assert len(missing) == 1
    package, reason = missing[0]
    assert package == "definitely-not-a-real-package-xyz"
    assert "没有安装" in reason


def test_check_imports_passes_for_installed_packages() -> None:
    assert preflight.check_imports(["fastapi", "sqlalchemy", "greenlet"]) == []


# --------------------------------------------------------------- 冒烟测试
def test_async_engine_smoke_test_passes_in_this_environment() -> None:
    """本机环境必须能建起异步引擎 —— 缺 greenlet 时这条会给出中文原因。

    注意它测的是 check_async_engine() 的返回值：环境正常时应当是 None。
    真正缺库的机器上这条会红，而报出来的不是英文堆栈，是一句能照着做的说明。
    """
    assert preflight.check_async_engine() is None


def test_tree_sitter_smoke_test_passes_in_this_environment() -> None:
    assert preflight.check_tree_sitter() is None


# --------------------------------------------------------------- start.bat
def test_start_bat_uses_the_preflight_script_instead_of_a_hand_written_list() -> None:
    """start.bat 的依赖探测必须走 preflight，不能再手写 import 清单。

    手写清单漏掉 greenlet 就是这次故障的直接原因：清单里没有它 →
    判定"依赖已就绪" → 跳过安装 → 一屏英文堆栈。
    """
    text = START_BAT_SRC.read_text(encoding="utf-8")
    assert 'set "PREFLIGHT=tools\\preflight.py"' in text
    assert 'python "%PREFLIGHT%" --quiet' in text
    # 不该再出现那种一整行 import 的探测
    assert not re.search(r'^python -c "import fastapi', text, re.M), (
        "start.bat 还在用手写的 import 清单探测依赖"
    )


def test_preflight_script_is_shipped() -> None:
    """start.bat 会调用它，文件必须在。"""
    assert (PROJECT_ROOT / "tools" / "preflight.py").is_file()


@pytest.mark.parametrize("name", ["start.bat", "stop.bat"])
def test_generated_bat_exists(name: str) -> None:
    assert (PROJECT_ROOT / name).is_file()
