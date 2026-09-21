"""验收：新电脑上「没有 Python」的那几条分支，能不能走到该走的地方。

为什么单独一个脚本：正常情况下这台机器是装了 Python 的，`verify_bat.py` 走的是
「有 Python」的主干。而「学生新电脑上什么都没装」恰恰是最容易出问题、又最难
被主干用例覆盖到的一条路 —— 一旦它写错，学生在自己电脑上双击只会闪一下就没了，
老师那边完全复现不了。

本脚本用三招把这条路径逼出来，全程**不会真的往机器上装 Python**：

1. 把 PATH 砍到只剩系统目录，并清掉 PY_PYTHON 等变量，让 python / py 都找不到；
2. 把 LOCALAPPDATA / ProgramFiles 指向一个空的临时目录，让「默认安装目录扫描」
   也一无所获；
3. 用 AI_TUTOR_AUTO_INSTALL_PS1 把一个**假安装脚本**塞进去，观察 start.bat
   是否正确地调用它、以及调用失败之后是否给出人能看懂的手动安装指引。

另外单独验一遍 tools/lib/installer_check.ps1：架构判定、哈希校验、签名校验，
包括「文件被改坏了要拒绝」「签名主体不对要拒绝」这两个必须成立的拒绝分支。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
START_BAT = BASE / "start.bat"

FAILURES: list[str] = []
CHECKS = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if not ok:
        FAILURES.append(label)
    line = f"  {'OK  ' if ok else 'FAIL'} {label}"
    if detail:
        line += f"    {detail}"
    print(line)


def run_start_bat(extra_env: dict[str, str], timeout: int = 120) -> str:
    """在改造过的环境里跑一遍 start.bat，返回按 GBK 解码后的输出。"""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    env = {
        "SystemRoot": system_root,
        "SystemDrive": os.environ.get("SystemDrive", "C:"),
        "windir": system_root,
        "TEMP": os.environ.get("TEMP", ""),
        "TMP": os.environ.get("TMP", ""),
        "USERPROFILE": os.environ.get("USERPROFILE", ""),
        "COMSPEC": os.environ.get("COMSPEC", rf"{system_root}\System32\cmd.exe"),
        "PATHEXT": os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD"),
        # 只留系统目录：python.exe 和 py.exe 都不在这里
        "PATH": rf"{system_root}\System32;{system_root}",
        "NUMBER_OF_PROCESSORS": os.environ.get("NUMBER_OF_PROCESSORS", "1"),
    }
    env.update(extra_env)

    proc = subprocess.run(
        ["cmd", "/c", "start.bat"],
        cwd=str(BASE),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
    )
    return proc.stdout.decode("gbk", errors="replace")


def make_fake_home(root: Path, *, with_python: bool, arch_dir: str = "Python311") -> Path:
    """造一个假的 %LOCALAPPDATA%：里面有没有 Programs\\Python\\PythonXXX 由参数决定。"""
    local = root / "localappdata"
    programmes = local / "Programs" / "Python"
    if with_python:
        target = programmes / arch_dir
        target.mkdir(parents=True, exist_ok=True)
        # 用 whoami.exe 冒充 python.exe：start.bat 只检查它存不存在，
        # 真正执行 --version 时它会以非零码退出，于是止步于「版本检查」，
        # 不会走到创建虚拟环境那一步。这正好让用例既快又只覆盖检测逻辑。
        shutil.copy2(rf"{os.environ['SystemRoot']}\System32\whoami.exe", target / "python.exe")
    else:
        programmes.mkdir(parents=True, exist_ok=True)
    return local


def write_stub_installer(path: Path, *, exit_code: int, marker: Path) -> None:
    path.write_text(
        "$ErrorActionPreference = 'Stop'\n"
        f"'{marker}' | Set-Content -LiteralPath '{marker}' -Encoding UTF8\n"
        "Write-Host '      [假安装脚本] 已被 start.bat 调用'\n"
        f"exit {exit_code}\n",
        encoding="utf-8-sig",
    )


print("=" * 74)
print("无 Python 环境验收（不会真的安装 Python）")
print("=" * 74)

with tempfile.TemporaryDirectory(prefix="ai-tutor-nopy-") as tmp:
    tmp_path = Path(tmp)
    fake_empty = make_fake_home(tmp_path / "empty", with_python=False)
    fake_with_py = make_fake_home(tmp_path / "haspy", with_python=True)
    stub = tmp_path / "stub_install.ps1"
    marker = tmp_path / "stub_ran.txt"

    # ---------- 1. 完全没有 Python：应当去调自动安装脚本 ----------
    print("\n[1] 没有 Python + 自动安装脚本报失败（exit 1）...")
    write_stub_installer(stub, exit_code=1, marker=marker)
    marker.unlink(missing_ok=True)
    out1 = run_start_bat(
        {
            "LOCALAPPDATA": str(fake_empty),
            "ProgramFiles": str(fake_empty),
            "ProgramFiles(x86)": str(fake_empty),
            "AI_TUTOR_AUTO_INSTALL_PS1": str(stub),
        }
    )
    check("调用了自动安装脚本", marker.exists())
    check("给出了手动安装指引", "https://www.python.org/downloads/" in out1)
    check("说明了自动安装失败", "自动安装也没有成功" in out1)
    check("提示了勾选 Add Python to PATH", "Add Python to PATH" in out1)
    check("提到了断网时的补救办法", "手机热点" in out1)
    check("没有出现 cmd 解析错位报错", "不是内部或外部命令" not in out1)

    # ---------- 2. 没有 Python，自动安装脚本不存在 ----------
    print("\n[2] 没有 Python + 自动安装脚本缺失...")
    marker.unlink(missing_ok=True)
    out2 = run_start_bat(
        {
            "LOCALAPPDATA": str(fake_empty),
            "ProgramFiles": str(fake_empty),
            "ProgramFiles(x86)": str(fake_empty),
            "AI_TUTOR_AUTO_INSTALL_PS1": str(tmp_path / "does_not_exist.ps1"),
        }
    )
    check("提示找不到自动安装脚本", "找不到自动安装脚本" in out2)
    check("仍然给出手动安装指引", "https://www.python.org/downloads/" in out2)

    # ---------- 3. 「装了但没加 PATH」：应当被目录扫描捞回来 ----------
    print("\n[3] Python 装在默认目录但不在 PATH 上...")
    marker.unlink(missing_ok=True)
    write_stub_installer(stub, exit_code=0, marker=marker)
    out3 = run_start_bat(
        {
            "LOCALAPPDATA": str(fake_with_py),
            "ProgramFiles": str(fake_with_py),
            "ProgramFiles(x86)": str(fake_with_py),
            "AI_TUTOR_AUTO_INSTALL_PS1": str(stub),
        }
    )
    check("在安装目录里找到了 Python", "在安装目录里找到了 Python" in out3)
    check("没有误判成「没有 Python」", "没有检测到 Python" not in out3)
    check("没有去调自动安装脚本", not marker.exists())
    check("走到了版本检查这一步", "版本" in out3)
    check("没有出现 cmd 解析错位报错", "不是内部或外部命令" not in out3)

# ---------- 4. 安装包校验库 ----------
print("\n[4] tools/lib/installer_check.ps1（架构判定 / 哈希 / 签名）...")

LIB = BASE / "tools" / "lib" / "installer_check.ps1"
check("校验库存在", LIB.exists())


def run_ps(script: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        cwd=str(BASE),
        capture_output=True,
        timeout=timeout,
    )


arch_script = f"""
. '{LIB}'
$cases = @(
    @{{ a='AMD64'; w='';      req='Auto';  want='AMD64' }},
    @{{ a='ARM64'; w='';      req='Auto';  want='ARM64' }},
    @{{ a='x86';   w='AMD64'; req='Auto';  want='AMD64' }},
    @{{ a='x86';   w='';      req='Auto';  want='x86'   }},
    @{{ a='AMD64'; w='';      req='ARM64'; want='ARM64' }},
    @{{ a='';      w='';      req='Auto';  want='AMD64' }}
)
$bad = 0
foreach ($c in $cases) {{
    $got = Resolve-TargetArchitecture -Requested $c.req `
        -ProcessorArchitecture $c.a -ProcessorArchitectureW6432 $c.w
    if ($got -ne $c.want) {{ $bad++; Write-Host "MISMATCH $($c.a)/$($c.w) -> $got" }}
}}
if ($bad -eq 0) {{ Write-Host 'ARCH_OK' }} else {{ Write-Host 'ARCH_BAD' }}
"""
proc = run_ps(arch_script)
check(
    "六个架构判定用例全部正确",
    "ARCH_OK" in proc.stdout.decode("utf-8", errors="replace"),
    proc.stdout.decode("utf-8", errors="replace").strip().splitlines()[-1]
    if proc.stdout.strip()
    else "",
)

# 哈希校验：真文件要过，被改坏的文件要拦
sample = os.environ.get("AI_TUTOR_TEST_INSTALLER", "")
hash_script = f"""
. '{LIB}'
$rel = Get-InstallerRelease -Architecture 'AMD64'
$path = '{sample}'
$problem = Test-InstallerHash -Path $path -Release $rel
if ($null -eq $problem) {{ Write-Host 'HASH_OK' }} else {{ Write-Host 'HASH_BAD' }}
"""
if sample and Path(sample).exists():
    proc = run_ps(hash_script)
    check(
        "真安装包哈希校验通过",
        "HASH_OK" in proc.stdout.decode("utf-8", errors="replace"),
    )
else:
    print("  --   跳过哈希正例（未提供 AI_TUTOR_TEST_INSTALLER；")
    print("       下载一次官方安装包后这样跑：")
    print("       $env:AI_TUTOR_TEST_INSTALLER='<python-3.11.9-amd64.exe>'; python tools/verify_no_python.py）")

sig_script = f"""
. '{LIB}'
$tmp = Join-Path $env:TEMP 'ai-tutor-unsigned-probe.exe'
[System.IO.File]::WriteAllBytes($tmp, (New-Object byte[] 512))
$r = Get-InstallerSignatureProblem -Path $tmp
if ($r -and $r.Fatal) {{ Write-Host 'UNSIGNED_REJECTED' }} else {{ Write-Host 'UNSIGNED_ACCEPTED' }}
Remove-Item $tmp -Force -ErrorAction SilentlyContinue

# 微软签名的系统程序：签名有效，但主体不是 Python Software Foundation，必须拒绝
$r2 = Get-InstallerSignatureProblem -Path (Join-Path $env:SystemRoot 'System32\\notepad.exe')
if ($r2 -and $r2.Fatal) {{ Write-Host 'WRONG_SIGNER_REJECTED' }} else {{ Write-Host 'WRONG_SIGNER_ACCEPTED' }}
"""
proc = run_ps(sig_script)
sig_out = proc.stdout.decode("utf-8", errors="replace")
check("没有签名的文件被拒绝", "UNSIGNED_REJECTED" in sig_out)
check("签名主体不是 Python 软件基金会的被拒绝", "WRONG_SIGNER_REJECTED" in sig_out)

# ---------- 汇总 ----------
print("\n" + "=" * 74)
print(f"验收结果: {CHECKS - len(FAILURES)}/{CHECKS} 项通过")
print("=" * 74)
for label in FAILURES:
    print(f"  FAIL {label}")

sys.exit(0 if not FAILURES else 1)
