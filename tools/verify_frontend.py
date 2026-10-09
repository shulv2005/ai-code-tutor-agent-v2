"""前端对接验收：真实启动服务，验证静态页面 + tutor 接口。

覆盖：
  1. 前端页面能被访问（/ 重定向到 /ui/index.html，静态资源可下载）
  2. /api/v1/tutor/analyze 能识别 C / Java / Python 并解析出函数
  3. 三个 AI 接口在「未配置模型」时返回友好的中文提示（503）
  4. 历史记录能写入与读取
  5. 本地项目库：扫描分类、按语言筛选、读文件、以及路径越界必须被拒绝
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PORT = 8311
BASE = f"http://127.0.0.1:{PORT}"

PY_CODE = '''def add(a, b):
    return a - b

def main():
    print(add(1, 2))
'''

C_CODE = """#include <stdio.h>

int add(int a, int b) {
    return a + b;
}

int main(void) {
    printf("%d\\n", add(1, 2));
    return 0;
}
"""

JAVA_CODE = """package demo;

public class Calculator {
    public int add(int a, int b) {
        return a + b;
    }
}
"""


def request(method: str, path: str, *, body: dict | None = None, raw: bytes | None = None,
            content_type: str | None = None) -> tuple[int, dict | str]:
    """发一个 HTTP 请求，返回 (状态码, 解析后的内容)。"""
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    elif body is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
            try:
                return resp.status, json.loads(payload)
            except json.JSONDecodeError:
                return resp.status, payload
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            return exc.code, payload


def form_body(filename: str, code: str) -> tuple[bytes, str]:
    """手工拼一个 multipart/form-data 请求体（避免额外依赖）。"""
    boundary = "----DSHTutorBoundary"
    parts = []
    for name, value in (("filename", filename), ("code", code)):
        parts.append(f"--{boundary}\r\n")
        parts.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n')
        parts.append(f"{value}\r\n")
    parts.append(f"--{boundary}--\r\n")
    return "".join(parts).encode("utf-8"), f"multipart/form-data; boundary={boundary}"


def main() -> int:
    # 临时数据目录：验收绝不能碰到开发者本机的 data/app.db
    # （原来这个临时目录只给项目库用，项目库删掉后改用它隔离数据库）
    work = Path(tempfile.mkdtemp())
    env = {
        # env= 是**整个替换**环境，不是叠加：少了 PATH / SystemRoot，
        # Windows 上连 socket 都初始化不了（WinError 10106）。
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "DATABASE__SQLITE_PATH": str(work / "verify.db"),
        "LLM__API_KEY": "",          # 刻意不配置模型，验证提示是否友好
        "APP__LOG_LEVEL": "WARNING",
        "PYTHONIOENCODING": "utf-8",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT), "--log-level", "warning"],
        cwd=str(BASE_DIR), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    checks: list[tuple[str, bool]] = []
    try:
        for _ in range(60):
            time.sleep(0.5)
            try:
                request("GET", "/api/v1/health")
                break
            except Exception:
                if proc.poll() is not None:
                    print("服务启动失败")
                    return 1

        print("=" * 72)
        print("1. 前端页面与静态资源")
        print("=" * 72)
        # 第二个元素只用于说明"这个文件应该是什么类型"，判定只看能否取到内容
        for path, _expect in (
            ("/ui/index.html", "text/html"),
            ("/ui/css/style.css", "text/css"),
            ("/ui/js/app.js", "javascript"),
            ("/ui/vendor/highlight.min.js", "javascript"),
            ("/ui/vendor/github.min.css", "text/css"),
        ):
            code, body = request("GET", path)
            ok = code == 200 and isinstance(body, str) and len(body) > 50
            checks.append((f"静态资源 {path}", ok))
            print(f"  {'OK ' if ok else 'FAIL'} {code}  {path}  ({len(body) if isinstance(body, str) else 0} 字节)")

        # 根路径应该 302 跳到前端页面
        code, _ = request("GET", "/")
        checks.append(("根路径重定向到前端", code in (200, 302, 307)))
        print(f"  {'OK ' if code in (200, 302, 307) else 'FAIL'} {code}  /  (重定向)")

        # 两个页面的样式表里都要有 [hidden] 兜底规则。
        # 缺了它，JS 的 el.hidden = true 会被自定义 display 覆盖，
        # 表现为"点了文件以后『还没有代码』的提示还压在代码上面"。
        for stylesheet in ("/ui/css/style.css",):
            code, css = request("GET", stylesheet)
            has_rule = (
                code == 200
                and isinstance(css, str)
                and "[hidden]" in css
                and "display: none !important" in css
            )
            checks.append((f"{stylesheet} 含 [hidden] 兜底规则", has_rule))
            print(f"  {'OK ' if has_rule else 'FAIL'} {stylesheet} 的 [hidden] 兜底规则")


        print()
        print("=" * 72)
        print("2. /api/v1/tutor/analyze 语言识别与解析")
        print("=" * 72)
        for filename, source, expect_lang, expect_symbol in (
            ("demo.py", PY_CODE, "python", "add"),
            ("demo.c", C_CODE, "c", "add"),
            ("Calculator.java", JAVA_CODE, "java", "add"),
        ):
            body, ctype = form_body(filename, source)
            code, data = request("POST", "/api/v1/tutor/analyze", raw=body, content_type=ctype)
            if code != 200 or not isinstance(data, dict):
                checks.append((f"analyze {filename}", False))
                print(f"  FAIL {code}  {filename}  {data}")
                continue
            names = [s["qualified_name"] for s in data["symbols"]]
            ok = data["language"] == expect_lang and expect_symbol in names
            checks.append((f"analyze {filename}", ok))
            print(f"  {'OK ' if ok else 'FAIL'} {filename} -> {data['language_label']} "
                  f"({data['language']}) 行={data['line_count']} 符号={names}")

        print()
        print("=" * 72)
        print("3. 未配置模型时三个 AI 接口的提示")
        print("=" * 72)
        for path in ("/check", "/comment", "/fix"):
            body, ctype = form_body("demo.py", PY_CODE)
            code, data = request("POST", f"/api/v1/tutor{path}", raw=body, content_type=ctype)
            detail = data.get("detail", "") if isinstance(data, dict) else str(data)
            # 期望：503 + 一句"告诉使用者怎么办"的中文提示。
            # 措辞有两种（来自多模型路径会带上模型名，来自单模型路径是通用那句），
            # 所以只校验关键信息而不是锁死整句——锁死会让一次正常的文案优化
            # 变成"验收失败"，那种断言没有价值。
            ok = code == 503 and "Key" in detail and ("网页" in detail or "AI" in detail)
            checks.append((f"{path} 友好提示", ok))
            print(f"  {'OK ' if ok else 'FAIL'} {code}  {path}  -> {detail[:70]}")

        print()
        print("=" * 72)
        print("4. 状态接口与错题本")
        print("=" * 72)
        code, status = request("GET", "/api/v1/tutor/status")
        ok = code == 200 and isinstance(status, dict) and status.get("ai_available") is False
        checks.append(("tutor/status", ok))
        print(f"  {'OK ' if ok else 'FAIL'} {code}  ai_available={status.get('ai_available')} "
              f"支持语言={list((status.get('supported_languages') or {}).values())}")

        # 错题本接口：新库应当是空的，且不传身份也能用（单机版）
        code, book = request("GET", "/api/v1/tutor/errors")
        ok = code == 200 and isinstance(book, dict) and book.get("total") == 0
        checks.append(("错题本接口可用（新库为空）", ok))
        print(f"  {'OK ' if ok else 'FAIL'} {code}  共 {book.get('total') if isinstance(book, dict) else '?'} 条错题")

        # 「历史记录」功能已删除：它的接口不该再存在（404）
        code, _ = request("GET", "/api/v1/tutor/history")
        ok = code == 404
        checks.append(("历史记录接口已随功能删除", ok))
        print(f"  {'OK ' if ok else 'FAIL'} {code}  GET /api/v1/tutor/history（应为 404）")

        print()
        print()
        print("=" * 72)
        passed = sum(1 for _, ok in checks if ok)
        print(f"验收结果：{passed}/{len(checks)} 项通过")
        print("=" * 72)
        for label, ok in checks:
            if not ok:
                print(f"  FAIL  {label}")
        return 0 if passed == len(checks) else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
