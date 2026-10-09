"""验收：拖拽 / 选择文件 / 粘贴进来的代码**不会在服务器上留下任何文件**。

## 这个脚本现在验什么

原先的学生端有一个「本地项目库」：拖进去的代码会真的写盘、按语言归档到
`c/` `java/` `python/`，再进文件列表管理。那套功能已经删除 —— 学生端最终形态是
**拖入或粘贴代码 → 交给 AI → 看结果与报告**，代码只存在于这次浏览里。

删掉"入库"之后，最需要盯住的反而变成一件事：**确认它真的没有偷偷写盘**。
所以这个脚本的核心是一条可证伪的断言：

    上传 / 粘贴代码前后，项目目录下的文件清单必须**完全一致**。

这条断言能真的失败：只要哪天有人在接口里加一句"顺手存一份到 data/"，
它马上就会红。这比"检查某个目录是空的"强得多 —— 后者在路径改名后就失效了。

另外保留原有两项静态检查（前端行为的"确定部分"）：
  · 占位提示与四个 AI 按钮的禁用条件
  · 四根分隔条与 500 行懒加载阈值

运行：python tools/verify_dragdrop.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
PORT = 8318
BASE = f"http://127.0.0.1:{PORT}"

PY_CODE = "def add(a, b):\n    return a + b\n\nprint(add(1, 2))\n"
C_CODE = "#include <stdio.h>\n\nint main(void) {\n    return 0;\n}\n"

# 清点文件时跳过的目录：它们体积大、且与"是否落了学生代码"无关
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache"}

checks: list[tuple[str, bool]] = []


def report(label: str, ok: bool, detail: str = "") -> None:
    """记一条验收结果并打印。"""
    checks.append((label, ok))
    print(f"  {'OK  ' if ok else 'FAIL'} {label}" + (f"  —— {detail}" if detail else ""))


def request(method: str, path: str, body: bytes | None = None, content_type: str | None = None):
    """发一个 HTTP 请求，返回 (状态码, 解析后的内容)。

    body 直接给字节，这样才能手工拼 multipart/form-data（不引入额外依赖）。
    """
    req = urllib.request.Request(f"{BASE}{path}", data=body, method=method)
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
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


def upload_form(filename: str, content: bytes) -> tuple[bytes, str]:
    """手工拼一个 multipart/form-data 请求体（模拟浏览器拖拽上传的文件）。"""
    boundary = f"----DSHDrag{uuid.uuid4().hex[:8]}"
    parts = [
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        content,
        b"\r\n",
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def form_field(name: str, value: str) -> tuple[bytes, str]:
    """拼一个只有文本字段的 multipart 请求体（模拟"粘贴代码"那条路）。"""
    boundary = f"----DSHPaste{uuid.uuid4().hex[:8]}"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
        f"{value}\r\n"
        f"--{boundary}--\r\n"
    ).encode()
    return body, f"multipart/form-data; boundary={boundary}"


def snapshot(root: Path) -> dict[str, int]:
    """清点项目目录下所有文件 -> {相对路径: 字节数}。

    比对前后两份快照，就能证明"上传没有在磁盘上留下东西"。
    用字节数而不是只比路径：覆盖写已有文件的情况也能被抓到。
    """
    result: dict[str, int] = {}
    for path in root.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file():
            try:
                result[path.relative_to(root).as_posix()] = path.stat().st_size
            except OSError:  # 文件刚好被别的进程删掉
                continue
    return result


def diff_snapshots(before: dict[str, int], after: dict[str, int]) -> dict[str, list[str]]:
    """给出两份快照的差异。"""
    return {
        "新增": sorted(set(after) - set(before)),
        "删除": sorted(set(before) - set(after)),
        "改动": sorted(k for k in set(before) & set(after) if before[k] != after[k]),
    }


def main() -> int:
    work = Path(tempfile.mkdtemp())
    env = {
        # env= 是**整个替换**环境，不是叠加：少了 PATH / SystemRoot，
        # Windows 上连 socket 都初始化不了（WinError 10106）。
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "DATABASE__SQLITE_PATH": str(work / "t.db"),
        "LLM__API_KEY": "",
        "APP__LOG_LEVEL": "WARNING",
        "PYTHONIOENCODING": "utf-8",
    }
    print("=" * 72)
    print("验收：拖拽 / 粘贴只预览，「不落盘」这条保证还成立吗")
    print("=" * 72)

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT),
         "--log-level", "warning"],
        cwd=str(BASE_DIR), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

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

        # ---------------- 场景 1：拖入一个文件 → 正常分析 ----------------
        before = snapshot(BASE_DIR)
        print()
        print("=" * 72)
        print("场景 1  拖一个 .py 进来 → 本地分析给出语言与规模")
        print("=" * 72)
        body, ctype = upload_form("drag_demo.py", PY_CODE.encode("utf-8"))
        code, data = request("POST", "/api/v1/tutor/analyze", body, ctype)
        ok = code == 200 and isinstance(data, dict)
        report("上传成功（200）", ok, f"状态码 {code}")
        if ok:
            report("语言识别正确", data.get("language") == "python",
                   f"识别为 {data.get('language')}")
            report("行数统计正确", data.get("line_count") == 4,
                   f"line_count={data.get('line_count')}")
            report("函数清单包含 add",
                   any(s.get("qualified_name") == "add" for s in data.get("symbols", [])))

        # ---------------- 场景 2：C 文件同样只预览 ----------------
        print()
        print("=" * 72)
        print("场景 2  拖一个 .c 进来 → 同样只预览")
        print("=" * 72)
        body, ctype = upload_form("drag_demo.c", C_CODE.encode("utf-8"))
        code, data = request("POST", "/api/v1/tutor/analyze", body, ctype)
        ok = code == 200 and isinstance(data, dict)
        report("上传成功（200）", ok, f"状态码 {code}")
        if ok:
            report("语言识别正确", data.get("language") == "c",
                   f"识别为 {data.get('language')}")

        # ---------------- 场景 3：粘贴代码（另一条入口）----------------
        print()
        print("=" * 72)
        print("场景 3  粘贴代码进来 → 与拖入走同一条路")
        print("=" * 72)
        body, ctype = form_field("code", PY_CODE)
        code, data = request("POST", "/api/v1/tutor/analyze", body, ctype)
        report("粘贴的代码也能分析", code == 200 and isinstance(data, dict),
               f"状态码 {code}")

        # ---------------- 场景 4：核心断言 —— 一个文件都没多 ----------------
        print()
        print("=" * 72)
        print("场景 4  ★ 核心保证：以上操作没有在磁盘上留下任何文件")
        print("=" * 72)
        after = snapshot(BASE_DIR)
        delta = diff_snapshots(before, after)
        detail = "；".join(f"{k} {v}" for k, v in delta.items() if v) or "磁盘完全没变"
        report("上传 / 粘贴前后项目目录文件清单完全一致",
               not any(delta.values()), detail)
        # 不能断言 data/library 不存在 —— 那是开发者之前留下的真实数据目录，
        # 磁盘上有残留是正常的。要断的是**后端不再有这套代码**（静态）
        # 以及**接口确实没了**（动态，见下一条）。
        lib_modules = [
            p for p in (BASE_DIR / "app").rglob("*.py")
            if p.name in {"library_service.py", "file_classifier.py",
                          "file_record_service.py", "library.py", "files.py"}
        ]
        report("后端不再有项目库 / 自动分类的模块文件",
               not lib_modules, f"仍有 {[p.name for p in lib_modules]}")
        report("已删除的项目库接口确实不存在了",
               request("GET", "/api/v1/library/scan")[0] == 404
               and request("GET", "/api/v1/files/list")[0] == 404)

        # ---------------- 静态检查：占位提示与拖拽绑定 ----------------
        print()
        print("=" * 72)
        print("场景 5  占位提示与拖拽绑定的静态检查")
        print("=" * 72)
        html = (BASE_DIR / "frontend" / "index.html").read_text(encoding="utf-8")
        css = (BASE_DIR / "frontend" / "css" / "style.css").read_text(encoding="utf-8")
        js = (BASE_DIR / "frontend" / "js" / "app.js").read_text(encoding="utf-8")

        report("HTML 里有 #code-placeholder 且 class=placeholder",
               'id="code-placeholder"' in html and 'class="placeholder"' in html)
        report("CSS 有 .placeholder 与 .placeholder.hidden",
               ".placeholder {" in css and ".placeholder.hidden" in css)
        report("JS 里有 hasCode 状态变量", "hasCode" in js)
        report("JS 用 classList 控制显隐（不直接改 display）",
               "classList.toggle('hidden'" in js)
        # 这条断言原先找的是 `state.aiAvailable && state.hasCode`——那个写法
        # 从**第一个提交**起就已经不存在了（setBusy 后来改用 aiReady() 收口），
        # 也就是说它一直在报假失败。这里改成检查真正的不变量：
        # 「可点条件里含 hasCode」+「四个按钮都由同一个 clickable 变量驱动」。
        four_buttons_share_condition = all(
            f"el.{name}.disabled = !clickable" in js
            for name in ("btnCheck", "btnComment", "btnFix", "btnDiagnose")
        )
        report("四个 AI 按钮在没有代码时禁用（可点条件含 hasCode）",
               "&& state.hasCode" in js and four_buttons_share_condition)
        report("两个拖拽区都绑定了处理器（① 与中间预览区）",
               "bindDropzone()" in js and "bindCodeDropzone()" in js)
        report("已删掉项目库拖拽区的绑定（bindLibraryDropzone 不再存在）",
               "bindLibraryDropzone" not in js)
        report("清空按钮存在（占位提示会重新出现）",
               'id="clearCodeBtn"' in html and "clearCode" in js)

        # ---------------- 静态检查：分隔条与懒加载 ----------------
        # 行为本身由 tools/check_frontend_logic.mjs 真跑 app.js 验证，
        # 这里只确认关键要素都在，两道检查互相补位。
        print()
        print("=" * 72)
        print("场景 6  分隔条与懒加载")
        print("=" * 72)
        report("HTML 里有四根分隔条",
               all(f'id="{name}"' in html for name in
                   ("resizerLeft", "resizerRight", "resizerCode", "resizerResultSplit")))
        # 数 role 之前要先去掉 HTML 注释：注释里也会提到 role="separator"，
        # 直接 count 会把说明文字也算成一根分隔条（这个坑刚踩过）
        html_without_comments = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL)
        separator_count = html_without_comments.count('role="separator"')
        report("分隔条带 role=separator（可聚焦、可键盘调）",
               separator_count == 4, f"实际 {separator_count} 根")
        report("CSS 里有 col-resize / row-resize 鼠标样式",
               "col-resize" in css and "row-resize" in css)
        report("CSS 用 CSS 变量做栅格（拖动只改一个变量）",
               "var(--sidebar-w)" in css and "var(--result-w)" in css)
        report("CSS 里中间栏最小宽度 400px", "--main-min: 400px" in css)
        report("JS 里最小宽度常量符合需求（200 / 400 / 280）",
               "sidebar: 200" in js and "main: 400" in js and "result: 280" in js)
        report("JS 有懒加载阈值 500 行与 requestIdleCallback",
               "LAZY_HIGHLIGHT_THRESHOLD = 500" in js and "requestIdleCallback" in js)
        report("JS 会在切换文件时取消未完成的高亮任务",
               "cancelPendingHighlight" in js)
        report("JS 会避开块注释中间切分（防止高亮串色）",
               "safeSplitPoint" in js)

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
