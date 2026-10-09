"""新电脑模拟：把项目复制到一个干净目录，从零建虚拟环境并跑起来。

这一步是**真的**在验证"新电脑能不能用"，而不是在开发者机器上假装一下：

  1. 复制项目（排除 .venv / .git / data / 各种缓存），确保没有可复用的环境；
  2. 新建一个空虚拟环境；
  3. 只按 requirements.txt 装依赖（学生双击 start.bat 走的就是这条路）；
  4. 跑自检、起服务、打 /health 与真实业务接口。

另外专门做一次 **greenlet 缺失**的复现：把它卸掉再自检，
确认能报出中文原因而不是甩一屏英文堆栈。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
EXCLUDE = {".venv", ".git", "data", "__pycache__", ".ruff_cache", ".pytest_cache", "node_modules"}

# 先证明"要复制的就是这个项目"再去复制。
# 这不是洁癖：脚本第一版放在项目根目录、SRC 写成 parent.parent，
# 而 parent.parent 是**桌面** —— 于是把桌面整个拷进了临时目录，里面有密钥文件。
# 宁可这里断言失败，也不要静默复制错目录。
REQUIRED_MARKERS = ("requirements.txt", "app/main.py", "pyproject.toml")


def log(message: str) -> None:
    print(message, flush=True)


def assert_source_is_the_project() -> None:
    missing = [name for name in REQUIRED_MARKERS if not (SRC / name).is_file()]
    if missing:
        raise SystemExit(
            f"拒绝执行：{SRC} 看起来不是本项目（缺 {missing}）。\n"
            "请从项目根目录运行：python tools\\cleanroom_check.py"
        )
    # 项目根目录下不该有桌面常见的那种杂物
    suspicious = [
        p.name for p in SRC.iterdir()
        if p.is_file() and p.suffix.lower() in {".lnk", ".7z", ".reg"}
    ]
    if suspicious:
        raise SystemExit(f"拒绝执行：{SRC} 里出现了非项目文件 {suspicious}，疑似桌面目录。")
    log(f"[0] 源目录确认：{SRC}")


def copy_project(dest: Path) -> None:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {name for name in names if name in EXCLUDE}

    shutil.copytree(SRC, dest, ignore=ignore, dirs_exist_ok=True)
    log(f"[1] 已复制项目 -> {dest}")


def run(cmd: list[str], cwd: Path, *, label: str, timeout: int = 1800) -> subprocess.CompletedProcess:
    log(f"    $ {' '.join(cmd[:4])} ...  ({label})")
    return subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=timeout,
    )


def http(url: str, timeout: int = 10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return None, str(exc)


def main() -> int:
    assert_source_is_the_project()
    work = Path(tempfile.mkdtemp(prefix="cleanroom_")) / "ai-code-tutor-agent"
    copy_project(work)

    venv = work / ".venv"
    python = venv / "Scripts" / "python.exe"
    if os.name != "nt":
        python = venv / "bin" / "python"

    log("[2] 新建空虚拟环境")
    result = run([sys.executable, "-m", "venv", str(venv)], work, label="python -m venv")
    if result.returncode != 0:
        log(result.stdout[-2000:] + result.stderr[-2000:])
        return 1

    log("[3] 只按 requirements.txt 安装依赖（这一步最慢，几分钟）")
    result = run(
        [str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check",
         "-r", "requirements.txt"],
        work, label="pip install -r requirements.txt",
    )
    if result.returncode != 0:
        log("    安装失败：")
        log(result.stdout[-3000:] + result.stderr[-3000:])
        return 1
    log("    安装完成")

    # ---- 4. 自检 ----
    log("[4] 跑依赖自检")
    result = run([str(python), "tools/preflight.py"], work, label="preflight")
    log("    " + result.stdout.strip().replace("\n", "\n    "))
    if result.returncode != 0:
        log("    ✗ 自检未通过，清洁环境装不齐依赖")
        return 1
    log("    ✓ 自检通过")

    # ---- 5. 真正起服务 ----
    log("[5] 启动服务并打接口")
    env = dict(os.environ)
    env.update({
        "DATABASE__SQLITE_PATH": str(work / "data" / "app.db"),
        "LLM__API_KEY": "",
        "PYTHONIOENCODING": "utf-8",
    })
    proc = subprocess.Popen(
        [str(python), "-m", "uvicorn", "app.main:app", "--port", "8517", "--log-level", "warning"],
        cwd=str(work), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
        errors="replace",
    )
    try:
        base = "http://127.0.0.1:8517"
        ready = False
        for _ in range(60):
            time.sleep(0.5)
            status, _body = http(f"{base}/api/v1/health")
            if status == 200:
                ready = True
                break
            if proc.poll() is not None:
                break
        if not ready:
            log("    ✗ 服务没起来")
            proc.kill()
            log((proc.stdout.read() or "")[-3000:])
            return 1

        checks: list[tuple[str, bool, str]] = []
        status, body = http(f"{base}/api/v1/health")
        checks.append(("GET /health", status == 200, f"{status}"))

        status, body = http(f"{base}/ui/index.html")
        checks.append(("GET /ui/index.html", status == 200 and len(body) > 500, f"{status} {len(body)}B"))

        status, body = http(f"{base}/api/v1/report/summary?range=all")
        ok = status == 200
        dims = ""
        if ok:
            data = json.loads(body)
            dims = f"能力维度 {len(data['ability']['dimensions'])} 个"
            ok = len(data["ability"]["dimensions"]) == 6
        checks.append(("GET /report/summary（要建异步引擎）", ok, f"{status} {dims}"))

        # 真的解析一次代码：这一步会用到 tree-sitter
        status, body = http(f"{base}/api/v1/tutor/status")
        checks.append(("GET /tutor/status", status == 200, f"{status}"))

        log("")
        for label, ok, detail in checks:
            log(f"    {'OK  ' if ok else 'FAIL'} {label}  —— {detail}")
        failed = [label for label, ok, _ in checks if not ok]

        # ---- 6. 复现 greenlet 缺失，确认自检能报出来 ----
        log("")
        log("[6] 复现 greenlet 缺失（模拟 ARM64 上 pip 跳过它的情形）")
        run([str(python), "-m", "pip", "uninstall", "-y", "-q", "greenlet"], work, label="卸载 greenlet")
        result = run([str(python), "tools/preflight.py"], work, label="preflight（缺 greenlet）")
        caught = result.returncode != 0 and "greenlet" in result.stdout
        log(f"    {'OK  ' if caught else 'FAIL'} 自检报出了 greenlet 缺失（返回码 {result.returncode}）")
        if caught:
            for line in result.stdout.splitlines():
                if "greenlet" in line or "解决办法" in line:
                    log("      " + line.strip())
        if not caught:
            failed.append("自检没能报出 greenlet 缺失")
            log("    完整输出：")
            log(result.stdout[-1500:])

        # ---- 7. 走一遍"学生重新双击 start.bat"的修复路径 ----
        # 光能报错还不够：start.bat 发现自检失败后会再跑一次
        # `pip install -r requirements.txt`。这里验证那一步真的能把 greenlet 装回来，
        # 否则学生照着提示做还是修不好。
        log("")
        log("[7] 模拟 start.bat 的修复动作：重新安装依赖")
        result = run(
            [str(python), "-m", "pip", "install", "-q", "--disable-pip-version-check",
             "-r", "requirements.txt"],
            work, label="pip install -r requirements.txt（修复）",
        )
        repaired = result.returncode == 0
        if repaired:
            result = run([str(python), "tools/preflight.py"], work, label="preflight（修复后）")
            repaired = result.returncode == 0
        log(f"    {'OK  ' if repaired else 'FAIL'} 重装依赖后自检恢复通过")
        if not repaired:
            failed.append("重新安装依赖没能修好 greenlet 缺失")
            log(result.stdout[-1500:])

        log("")
        log("=" * 72)
        if failed:
            log(f"清洁环境验证未通过：{failed}")
        else:
            log("清洁环境验证通过：新电脑上从零安装可以正常启动并工作")
        log(f"（测试目录保留在 {work}，可手动删除）")
        log("=" * 72)
        return 1 if failed else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
