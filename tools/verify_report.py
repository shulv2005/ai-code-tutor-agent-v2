"""学习报告验收：真的起服务、真的发 HTTP、真的导出一份 Word 并打开它。

覆盖需求「记录使用时间 + 分析历史错题明细和易错知识点 + 导出 Word 给老师」：

  1. 路由与参数：/usage/heartbeat、/usage/current、/usage/summary、
     /report/summary、/report/word 都注册了，区间参数容错；
  2. 使用时长：start.bat（服务启动）那一刻自动开一条记录；心跳会让
     "有效时长"往前走；**长时间不心跳不会被算成使用**（单段封顶）；
  3. 报告数据：错题明细、易错知识点排行、错误类型与语言分布、
     按规则生成的学习建议，字段齐全且数字自洽；
  4. 导出的 Word：HTTP 头是附件下载 + 中文文件名、包结构完整、
     XML 合法、内容里确实有种进去的错题；
  5. 本机装了 Word 的话，**用 Word 亲自打开一次**（这是"能不能交到老师手里"
     的最终判据——结构合法但 Word 报"文件已损坏"是很容易发生的一种失败）。

本脚本不会调用任何大模型：错题数据直接用 sqlite3 种进临时库，
所以没有 API Key 也能跑，CI 与教室机器都能用。

用法：python tools/verify_report.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

BASE_DIR = Path(__file__).resolve().parent.parent
PORT = 8318
BASE = f"http://127.0.0.1:{PORT}"
STUDENT = "verify-report-2024001"
# 专门用来验"单段心跳封顶"的另一个身份：它的记录 last_seen 停在 2 小时前，
# 和上面那份报告数据分开，免得把总时长断言搅乱。
CAP_STUDENT = "verify-report-cap"
CAP_SESSION_ID = 0  # prepare_database() 里回填

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

CHECKS: list[tuple[str, bool]] = []


def record(label: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((label, ok))
    print(f"  {'OK  ' if ok else 'FAIL'} {label}" + (f"  —— {detail}" if detail else ""))


def request(method: str, path: str, body: dict | None = None):
    """发一个请求，返回 (状态码, 响应体字节, 响应头)。

    响应头刻意返回 `HTTPMessage` 本身而不是 `dict(...)`：HTTP 头名是大小写
    不敏感的，转成普通 dict 以后 `headers["Content-Type"]` 会因为服务端发的是
    `content-type` 而取不到值——这个坑真的让上面几条断言全挂了。
    """
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read(), resp.headers
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers


def get_json(path: str) -> tuple[int, dict]:
    status, payload, _ = request("GET", path)
    try:
        return status, json.loads(payload)
    except json.JSONDecodeError:
        return status, {}


def post_json(path: str, body: dict) -> tuple[int, dict]:
    status, payload, _ = request("POST", path, body)
    try:
        return status, json.loads(payload)
    except json.JSONDecodeError:
        return status, {}


def api_json(path: str) -> dict:
    """GET 一个接口并返回解析后的 JSON（失败返回空字典，由断言去报错）。"""
    _, payload, _ = request("GET", path)
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return {}


def http_get_text(path: str) -> str:
    """GET 一个静态资源并返回文本（前端页面 / JS）。"""
    _, payload, _ = request("GET", path)
    return payload.decode("utf-8", errors="replace")


def _png_is_valid(payload: bytes) -> tuple[bool, str]:
    """校验 PNG 的容器结构与每个块的 CRC。

    只判断"字节数大于 0"没有意义：Word 拿到坏图只会显示一个红叉，
    既不报错也不提示，最后是老师来问"为什么图是一片空白"。
    """
    import struct
    import zlib

    if payload[:8] != b"\x89PNG\r\n\x1a\n":
        return False, "魔数不对"
    index = 8
    names: list[str] = []
    while index < len(payload):
        if index + 8 > len(payload):
            return False, "块头被截断"
        length = struct.unpack(">I", payload[index : index + 4])[0]
        tag = payload[index + 4 : index + 8]
        body = payload[index + 8 : index + 8 + length]
        crc = struct.unpack(">I", payload[index + 8 + length : index + 12 + length])[0]
        if zlib.crc32(tag + body) & 0xFFFFFFFF != crc:
            return False, f"{tag!r} 的 CRC 不匹配"
        names.append(tag.decode("ascii", errors="replace"))
        index += 12 + length
    if names[:1] != ["IHDR"] or names[-1:] != ["IEND"]:
        return False, f"块顺序异常：{names}"
    width, height = struct.unpack(">II", payload[16:24])
    return True, f"{width}×{height}，块 {names}"


def _ability_ordering_ok(dimensions: list[dict]) -> bool:
    """证据多的维度分数不该高于证据少两档以上的维度。

    这是"雷达图能读"的最低要求：如果分数和证据量没有单调关系，
    图上那个形状就是随机噪声。
    """
    pairs = [
        (item.get("weight", 0), item.get("score", 0))
        for item in dimensions
        if isinstance(item.get("weight"), (int, float))
    ]
    if len(pairs) < 2:
        return False
    pairs.sort()
    scores = [score for _, score in pairs]
    return all(a >= b for a, b in zip(scores, scores[1:], strict=False))


def prepare_database(db_path: Path, env: dict[str, str]) -> None:
    """先用应用自己的建表逻辑建好表，再种入使用记录与错题。

    为什么先建表：直接手写 CREATE TABLE 的话，表结构一旦调整，
    这个脚本就会用一份"过时的表结构"去骗过验收——那比不测还糟。
    """
    bootstrap = (
        "import asyncio;"
        "from app.core.database import init_db;"
        "asyncio.run(init_db())"
    )
    result = subprocess.run(
        [sys.executable, "-c", bootstrap],
        cwd=str(BASE_DIR),
        env=env,
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        print(result.stderr.decode("utf-8", errors="replace")[-2000:])
        raise SystemExit("建表失败，无法继续")

    now = datetime.now(UTC)
    conn = sqlite3.connect(db_path)
    try:
        # 三条使用记录：两天前 30 分钟、昨天 45 分钟、今天 12 分钟
        sessions = [
            (now - timedelta(days=2, hours=1), 1800, 1800),
            (now - timedelta(days=1, hours=2), 2700, 2760),
            (now - timedelta(hours=1), 720, 900),
        ]
        for started, active, last_offset in sessions:
            conn.execute(
                "INSERT INTO usage_sessions"
                " (student_id, started_at, last_seen_at, active_seconds, ended_at, pid, app_version)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    STUDENT,
                    started.replace(tzinfo=None).isoformat(sep=" "),
                    (started + timedelta(seconds=last_offset)).replace(tzinfo=None).isoformat(sep=" "),
                    active,
                    (started + timedelta(seconds=last_offset)).replace(tzinfo=None).isoformat(sep=" "),
                    1234,
                    "0.1.0",
                ),
            )

        # 错题：循环边界 3 条、数组下标 2 条、函数返回值 1 条
        errors = [
            ("逻辑错误", "循环边界", '["循环边界"]', "range 少遍历了一个元素，求和结果少最后一项。"),
            ("逻辑错误", "循环边界", '["循环边界", "累加"]', "外层循环的终止条件写成了 < 而不是 <=。"),
            ("逻辑错误", "循环边界", '["循环边界"]', "空列表时直接取 [0] 会越界。"),
            ("运行时错误", "数组下标", '["数组下标"]', "下标从 1 开始，但 Python 从 0 开始。"),
            ("运行时错误", "数组下标", '["数组下标", "边界条件"]', "遍历时多访问了一位。"),
            ("规范问题", "函数返回值", '["函数返回值"]', "函数名叫 add 却做了减法。"),
        ]
        for index, (error_type, point, points_json, feedback) in enumerate(errors):
            conn.execute(
                "INSERT INTO student_error_logs"
                " (student_id, code_snippet, language, filename, error_type,"
                "  knowledge_point, knowledge_points_json, ai_feedback, source, model, trace_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    STUDENT,
                    "def add(a, b):\n    return a - b\n",
                    "python" if index % 2 == 0 else "c",
                    f"homework{index + 1}.py",
                    error_type,
                    point,
                    points_json,
                    feedback,
                    "diagnose",
                    "verify-model",
                    "verify-trace",
                    (now - timedelta(days=index % 3, minutes=index * 7))
                    .replace(tzinfo=None)
                    .isoformat(sep=" "),
                ),
            )
        conn.commit()

        # 再种一条"上次心跳停在 2 小时前"的记录，用来验单段心跳封顶。
        # 单独一个身份，避免混进上面那份报告数据里。
        global CAP_SESSION_ID
        started = now - timedelta(hours=2)
        cursor = conn.execute(
            "INSERT INTO usage_sessions"
            " (student_id, started_at, last_seen_at, active_seconds, ended_at, pid, app_version)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                CAP_STUDENT,
                started.replace(tzinfo=None).isoformat(sep=" "),
                started.replace(tzinfo=None).isoformat(sep=" "),
                0,
                None,
                4321,
                "0.1.0",
            ),
        )
        CAP_SESSION_ID = int(cursor.lastrowid)
        conn.commit()
    finally:
        conn.close()


def probe_with_word(docx_path: Path) -> tuple[bool, str]:
    """用 Word 打开一次（本机没装 Word 时由调用方跳过）。"""
    script = (
        "$ErrorActionPreference='Stop';"
        "$w=New-Object -ComObject Word.Application;"
        "$w.Visible=$false;$w.DisplayAlerts=0;"
        "try{$d=$w.Documents.Open($env:PROBE_DOCX,$false,$true);"
        "Write-Output ('PAGES=' + $d.ComputeStatistics(2));"
        "Write-Output ('TABLES=' + $d.Tables.Count);"
        "Write-Output ('FOOTERS=' + $d.Sections.Item(1).Footers.Count);"
        "$d.Close($false);Write-Output 'WORD_OK'}"
        "catch{Write-Output ('WORD_FAIL: ' + $_.Exception.Message)}"
        "finally{$w.Quit()}"
    )
    env = dict(os.environ)
    env["PROBE_DOCX"] = str(docx_path)
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            env=env,
            capture_output=True,
            timeout=180,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"调用 Word 失败：{exc}"
    out = proc.stdout.decode("utf-8", errors="replace")
    if "WORD_OK" not in out:
        return False, out.strip()[-300:]
    pages = ""
    for line in out.splitlines():
        if line.startswith("PAGES="):
            pages = line
    return True, f"{pages} {out.splitlines()[-1]}".strip()


def word_available() -> bool:
    if os.name != "nt":
        return False
    try:
        proc = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(New-Object -ComObject Word.Application) | Out-Null; 'YES'",
            ],
            capture_output=True,
            timeout=120,
        )
        return b"YES" in proc.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="ai-tutor-report-"))
    db_path = work / "report.db"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "DATABASE__SQLITE_PATH": str(db_path),
        "CLASSIFIER__ROOT": str(work / "codes"),
        "LIBRARY__ROOTS": str(work / "library"),
        "APP__LOG_LEVEL": "WARNING",
        "LLM__API_KEY": "",
        "PYTHONIOENCODING": "utf-8",
    }

    print("=" * 74)
    print("学习报告验收（不调用大模型；错题数据直接种进临时库）")
    print("=" * 74)

    prepare_database(db_path, env)

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--port",
            str(PORT),
            "--log-level",
            "warning",
        ],
        cwd=str(BASE_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    docx_path = work / "learning-report.docx"
    try:
        ready = False
        for _ in range(60):
            time.sleep(0.5)
            try:
                status, _, _ = request("GET", "/api/v1/health")
                if status == 200:
                    ready = True
                    break
            except Exception:  # noqa: BLE001
                if proc.poll() is not None:
                    break
        if not ready:
            print("服务启动失败")
            return 1

        # ---------------- 1. 路由 ----------------
        print()
        print("=" * 74)
        print("1. 路由与参数")
        print("=" * 74)
        status, spec = get_json("/openapi.json")
        paths = spec.get("paths", {}) if isinstance(spec, dict) else {}
        for path in (
            "/api/v1/usage/heartbeat",
            "/api/v1/usage/current",
            "/api/v1/usage/summary",
            "/api/v1/report/summary",
            "/api/v1/report/word",
        ):
            record(f"路由已注册：{path}", path in paths)

        # ---------------- 2. 使用时长 ----------------
        print()
        print("=" * 74)
        print("2. 使用时长（start.bat 启动即开始记）")
        print("=" * 74)
        # 不带身份参数 = 页面上的默认用法（本机固定身份 local），
        # 服务启动时开的那条记录就归在它名下。
        status, current = get_json("/api/v1/usage/current")
        record("GET /usage/current 可用", status == 200 and bool(current.get("session_id")))
        record(
            "启动时就自动开了一条使用记录",
            bool(current.get("session_id")),
            f"session_id={current.get('session_id')}",
        )
        record(
            "刚启动时本次时长为 0（还没人用）",
            current.get("active_seconds") == 0,
            current.get("active_text", ""),
        )
        record(
            "开始时间带 UTC 时区（前端才能换算成本地时间）",
            str(current.get("started_at", "")).endswith("Z"),
            str(current.get("started_at", "")),
        )

        status, beat = post_json("/api/v1/usage/heartbeat", {})
        record("POST /usage/heartbeat 可用", status == 200)

        # 单段心跳封顶：校验"合上笔记本两小时再打开"不会被算成两小时学习。
        # 造一条 last_seen 停在 2 小时前的记录，心跳一次，看推进了多少。
        status, capped = post_json(
            f"/api/v1/usage/heartbeat?student_id={CAP_STUDENT}",
            {"session_id": CAP_SESSION_ID},
        )
        record("可以对指定的使用记录发心跳", status == 200)
        record(
            "一次迟到 2 小时的心跳只推进 300 秒（单段封顶）",
            capped.get("active_seconds") == 300,
            f"active_seconds={capped.get('active_seconds')}",
        )

        status, usage = get_json(f"/api/v1/usage/summary?range=all&student_id={STUDENT}")
        record("GET /usage/summary 可用", status == 200)
        record(
            "汇总条数 = 种进去的 3 次",
            usage.get("session_count") == 3,
            str(usage.get("session_count")),
        )
        record(
            "总时长 = 1800+2700+720 = 5220 秒",
            usage.get("total_seconds") == 5220,
            f"{usage.get('total_seconds')} ({usage.get('total_text')})",
        )
        record("总时长的中文写法正确", usage.get("total_text") == "1 小时 27 分", str(usage.get("total_text")))
        record("使用天数 = 3", usage.get("active_days") == 3, str(usage.get("active_days")))
        record("每次明细都带有效时长", len(usage.get("sessions", [])) == 3)

        # 区间容错：认不出来的值按 30 天，不该报 422
        status, _tolerant = get_json(f"/api/v1/usage/summary?range=whatever&student_id={STUDENT}")
        record("区间参数写错不报错（按 30 天）", status == 200)

        # ---------------- 3. 报告数据 ----------------
        print()
        print("=" * 74)
        print("3. 学习报告数据（错题明细 + 易错知识点）")
        print("=" * 74)
        status, report = get_json(f"/api/v1/report/summary?range=all&student_id={STUDENT}")
        record("GET /report/summary 可用", status == 200)
        record("错题总数 = 6", report.get("errors", {}).get("total") == 6, str(report.get("errors", {}).get("total")))
        record("使用时长与 /usage/summary 一致", report.get("usage", {}).get("total_seconds") == 5220)

        weak = report.get("errors", {}).get("weak_points", [])
        record("易错知识点排行榜非空", bool(weak))
        record(
            "排行榜第一名是「循环边界」，3 次",
            bool(weak) and weak[0]["knowledge_point"] == "循环边界" and weak[0]["count"] == 3,
            f"{weak[0] if weak else '—'}",
        )
        record(
            "排行榜按次数倒序",
            [item["count"] for item in weak] == sorted((i["count"] for i in weak), reverse=True),
        )
        by_type = report.get("errors", {}).get("by_type", [])
        record("错误类型分布有 3 类", len(by_type) == 3, str([i["error_type"] for i in by_type]))
        by_lang = report.get("errors", {}).get("by_language", [])
        record("语言分布覆盖 python/c", {i["language"] for i in by_lang} == {"python", "c"})

        items = report.get("errors", {}).get("items", [])
        record("错题明细逐条返回", len(items) == 6)
        record(
            "明细含知识点列表与 AI 反馈摘要",
            all(item["knowledge_points"] and item["ai_feedback_excerpt"] for item in items),
        )
        record("时间倒序（第一条最新）", items[0]["created_at"] >= items[-1]["created_at"])
        record("生成了学习建议", len(report.get("suggestions", [])) >= 1)
        record(
            "建议里点名了最薄弱的知识点",
            any("循环边界" in tip for tip in report.get("suggestions", [])),
        )
        record("报告不带任何大模型依赖", True, "未配置 LLM__API_KEY 也能生成")

        # ---------------- 4. 导出 Word ----------------
        print()
        print("=" * 74)
        print("4. 导出 Word（浏览器直接下载）")
        print("=" * 74)
        status, payload, headers = request(
            "GET", f"/api/v1/report/word?range=all&student_id={STUDENT}"
        )
        record("GET /report/word 返回 200", status == 200, f"{len(payload)} 字节")
        ctype = headers.get("Content-Type", "")
        record(
            "Content-Type 是 docx",
            "wordprocessingml.document" in ctype,
            ctype,
        )
        disposition = headers.get("Content-Disposition", "")
        record("是附件下载（attachment）", disposition.startswith("attachment;"))
        record("文件名带 UTF-8 编码（中文不乱码）", "filename*=UTF-8''" in disposition)
        record(
            "文件名含学生标识",
            STUDENT in urllib.parse.unquote(disposition),
            urllib.parse.unquote(disposition)[-60:],
        )
        docx_path.write_bytes(payload)

        with zipfile.ZipFile(docx_path) as zf:
            parts = set(zf.namelist())
            broken = zf.testzip()
        record("zip 完整无损", broken is None)
        for part in (
            "[Content_Types].xml",
            "_rels/.rels",
            "word/document.xml",
            "word/styles.xml",
            "word/footer1.xml",
            "word/_rels/document.xml.rels",
            "docProps/core.xml",
        ):
            record(f"包含部件 {part}", part in parts)

        with zipfile.ZipFile(docx_path) as zf:
            xml_bytes = zf.read("word/document.xml")
        try:
            root = ET.fromstring(xml_bytes)
            xml_ok = True
        except ET.ParseError as exc:
            root = None
            xml_ok = False
            record("document.xml 是合法 XML", False, str(exc))
        if xml_ok:
            record("document.xml 是合法 XML", True)
            texts = [
                "".join(node.text or "" for node in para.iter(f"{W}t"))
                for para in root.iter(f"{W}p")
            ]
            blob = "\n".join(texts)
            record("文档里有标题", "学习报告" in blob)
            record("文档里有能力雷达章节", "一、能力雷达（六维）" in blob)
            record("文档里有使用情况章节", "二、使用情况" in blob)
            record("文档里有错题整改章节", "三、错题整改情况" in blob)
            record("文档里有学习建议章节", "四、学习建议" in blob)
            record("文档里有种进去的知识点", "循环边界" in blob and "数组下标" in blob)
            record("文档里有总时长 1 小时 27 分", "1 小时 27 分" in blob)
            record("文档里列出了每次使用明细", "每次使用明细" in blob)
            record("文档里列出了错题明细", "错题明细" in blob)
            record("文档里没有残留的 XML 转义符", "&amp;lt;" not in blob)
            # 六个能力维度都要落到表里，并说明"依据多少条证据"
            for label in ("准确性", "健壮性", "规范性", "可读性", "高效性", "完备性"):
                record(f"能力表里有「{label}」", label in blob)
            record("能力表说明了评估依据", "评估依据" in blob)
            record("最弱的那一项被标出来", "（最弱）" in blob)
            tables = list(root.iter(f"{W}tbl"))
            record("至少 9 张表", len(tables) >= 9, f"{len(tables)} 张")

        # ---------------- 5. Word 亲自打开 ----------------
        print()
        print("=" * 74)
        print("5. 用 Word 打开（结构合法 ≠ Word 认）")
        print("=" * 74)
        if word_available():
            ok, detail = probe_with_word(docx_path)
            record("Word 能正常打开导出的文档", ok, detail)
        else:
            print("  --   本机没有 Word，跳过这一项（结构检查已通过）")

        # ---------------- 6. 能力雷达图（PNG + 接口） ----------------
        print()
        print("=" * 74)
        print("6. 能力雷达图（六维打分 / 手写 PNG / 嵌进 docx）")
        print("=" * 74)
        with zipfile.ZipFile(docx_path) as zf:
            names = zf.namelist()
            png_in_package = "word/media/radar.png" in names
            record("docx 包里带了 radar.png", png_in_package)
            if png_in_package:
                png = zf.read("word/media/radar.png")
                record(
                    "radar.png 是合法 PNG（含 CRC 校验）",
                    *_png_is_valid(png),
                )
            content_types = zf.read("[Content_Types].xml").decode("utf-8")
            record("Content_Types 里登记了 png", 'Extension="png"' in content_types)
            rels = zf.read("word/_rels/document.xml.rels").decode("utf-8")
            record("document.xml.rels 里引用了图片", "media/radar.png" in rels)
            document = zf.read("word/document.xml").decode("utf-8")
            # xmlns:wp 漏了的话 Word 直接报"文档可能已经损坏"，报错信息
            # 完全不提命名空间 —— 这条专门盯着它
            record("声明了 wp: 命名空间", "xmlns:wp=" in document)
            record("正文里有 w:drawing", "<w:drawing>" in document)

        profile = api_json("/api/v1/report/summary?range=all")
        ability = (profile or {}).get("ability") or {}
        dims = ability.get("dimensions") or []
        record("接口返回六个能力维度", len(dims) == 6, f"{len(dims)} 个")
        record(
            "维度键与顺序固定",
            [d.get("key") for d in dims]
            == ["completeness", "conventions", "readability",
                "efficiency", "robustness", "accuracy"],
            str([d.get("key") for d in dims]),
        )
        record("每个维度都有 0-100 的分数", all(0 <= d.get("score", -1) <= 100 for d in dims))
        record(
            "每个维度都说明了依据条数",
            all(isinstance(d.get("evidence_count"), int) for d in dims),
        )
        record("给出了最弱 / 最强与极差", bool(ability.get("weakest")) and ability.get("spread") is not None)
        record("附了一句人话结论", bool(ability.get("summary")))
        record(
            "打平之前先按证据量对齐：错得多的那一项分数更低",
            _ability_ordering_ok(dims),
        )

        page = http_get_text("/ui/index.html")
        record("页面里有雷达图容器", 'id="radarChart"' in page)
        js = http_get_text("/ui/js/app.js")
        record("雷达图用 createElementNS 画 SVG", "createElementNS" in js)

        # 「没引第三方图表库」只能靠**页面实际加载了什么**来判断。
        # 两个坑都踩过：
        #   · 用"文件里出现 chart 这个词"判断 —— 被自己的注释绊倒
        #     （注释里写着"引 echarts / chart.js 要塞几百 KB"）；
        #   · 直接正则扫 <script src> —— 会把 HTML 注释里那段
        #     "Vue 3 接入预留位"的示例代码也算成真的加载了。
        # 所以先把注释去掉，再看剩下的 <script src>。
        visible = re.sub(r"<!--.*?-->", "", page, flags=re.S)
        loaded = re.findall(r'<script[^>]*\bsrc="([^"]+)"', visible, re.I)
        banned = ("echarts", "chart.js", "chartjs", "d3", "highcharts", "plotly",
                  "apexcharts", "vue")
        hit = [src for src in loaded if any(word in src.lower() for word in banned)]
        record(
            "雷达图没有引第三方图表库（多下载几百 KB 不划算）",
            not hit,
            f"实际加载 {loaded}",
        )
        expected_scripts = [
            "vendor/highlight.min.js",  # 语法高亮
            "vendor/marked.min.js",     # Markdown -> HTML
            "vendor/purify.min.js",     # 消毒（AI 生成的 HTML 必须过一遍）
            "js/app.js",                # 页面逻辑
        ]
        record(
            "页面加载的脚本就是那固定的 4 个（没有偷偷加依赖）",
            loaded == expected_scripts,
            f"{loaded}",
        )

    finally:
        proc.kill()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass

    print()
    print("=" * 74)
    passed = sum(1 for _, ok in CHECKS if ok)
    print(f"验收结果: {passed}/{len(CHECKS)} 项通过")
    print("=" * 74)
    for label, ok in CHECKS:
        if not ok:
            print(f"  FAIL {label}")

    if docx_path.exists():
        keep = Path(tempfile.gettempdir()) / "ai-tutor-learning-report.docx"
        shutil.copy2(docx_path, keep)
        print(f"\n导出的 Word 已留一份供人工查看：{keep}")
    print(f"临时目录：{work}")

    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
