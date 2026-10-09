"""错题本 + 举一反三验收：真实启动服务，用 HTTP 跑一遍完整链路。

两种模式（都会真的起 uvicorn、真的发请求、真的写 SQLite）：

  · 默认：验证**不依赖大模型**的那部分（任何人、任何机器都能跑）
        python tools/verify_diagnose.py
      覆盖：路由与参数校验、错题本接口、学号隔离、错误码。

  · 加 --ai：额外用 .env 里配好的真实模型跑完整诊断链路
        python tools/verify_diagnose.py --ai
      覆盖：真实返回的 JSON 结构、错题本归档、第二次诊断时
            "历史错题是否真的进了提示词"。

为什么不能只靠 pytest：
pytest 用假模型验证逻辑；本脚本验证的是"学生按教程点这个按钮，
从 HTTP 入参到数据库落库到底发生了什么"——配置、路由、序列化、
SQLite 建表一起对不对，是另一类问题。

前置：加 --ai 时需要在 .env 里配好 LLM__API_KEY（或对应的 LLM__MODELS__*__API_KEY），
      否则会看到"AI 功能尚未启用"的 503，这是预期行为而不是脚本坏了。
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
PORT = 8316
BASE = f"http://127.0.0.1:{PORT}"

# 身份标识：验收脚本用显式标识，方便断言"错题本确实按标识隔离"。
# 页面上没有学号输入框（单机版），但接口仍可选接受身份参数——
# 这条路径是给"一个脚本批量跑一个班"用的，顺手在这里验证它没坏。
STUDENT_A = "verify-A-2024001"
STUDENT_B = "verify-B-2024002"

# 一份**故意写错**的代码：函数叫 add 却做减法，并且循环少遍历一个元素。
# 两类问题都很典型，模型应当至少指出其中之一，并把它归到某个知识点。
BROKEN_PY = '''def add(a, b):
    return a - b


def total(scores):
    result = 0
    for i in range(len(scores) - 1):
        result += scores[i]
    return result
'''

# 一份正确的代码：用于验证 status=success 时**不写错题本**
GOOD_PY = '''def add(a, b):
    """返回两数之和。"""
    return a + b


def total(scores):
    """返回列表中所有元素之和。"""
    return sum(scores)
'''


def request(
    method: str, path: str, body: dict | None = None, headers: dict | None = None
) -> tuple[int, dict | str]:
    """发一个 HTTP 请求，返回 (状态码, 解析后的内容)。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
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


def post_json(path: str, body: dict, headers: dict | None = None) -> tuple[int, dict | str]:
    return request("POST", path, body, headers)


def main() -> int:
    use_ai = "--ai" in sys.argv
    work = Path(tempfile.mkdtemp())

    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "DATABASE__SQLITE_PATH": str(work / "diagnose.db"),
        "RETRIEVAL__INDEX_DIR": str(work / "index"),
        "REPOSITORY__WORKSPACE_DIR": str(work / "repos"),
        "DOCKER__WORKSPACE_DIR": str(work / "ws"),
        "APP__LOG_LEVEL": "WARNING",
        "PYTHONIOENCODING": "utf-8",
    }

    print("=" * 72)
    if use_ai:
        print("模式：完整链路（读 .env 里的 LLM__* 配置，会真的调用大模型）")
    else:
        # 刻意不配模型：验证"没有 Key 时接口给出明确提示而不是 500"
        env["LLM__API_KEY"] = ""
        print("模式：接口与参数校验（未配置模型，验证降级路径）")
    print("=" * 72)

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PORT),
         "--log-level", "warning"],
        cwd=str(BASE_DIR), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    checks: list[tuple[str, bool]] = []

    def record(label: str, ok: bool, detail: str = "") -> None:
        checks.append((label, ok))
        print(f"  {'OK  ' if ok else 'FAIL'} {label}" + (f"  —— {detail}" if detail else ""))

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

        # ---------------- 1. 路由与 OpenAPI ----------------
        print()
        print("=" * 72)
        print("1. 路由注册（规范接口 + 兼容别名）")
        print("=" * 72)
        code, spec = request("GET", "/openapi.json")
        paths = spec["paths"] if isinstance(spec, dict) else {}
        for path in ("/api/v1/tutor/diagnose", "/api/v1/tutor/errors",
                     "/api/v1/tutor/errors/{error_id}", "/api/analyze"):
            record(f"路由已注册：{path}", path in paths)
        params = paths.get("/api/v1/tutor/diagnose", {}).get("post", {})
        record("POST /diagnose 声明了请求体（multipart）",
               "requestBody" in params)
        record("POST /api/analyze 声明了 JSON 请求体（需求文档的契约）",
               "requestBody" in paths.get("/api/analyze", {}).get("post", {}))

        # ---------------- 2. 数据库表 ----------------
        print()
        print("=" * 72)
        print("2. 错题本建表（需求点名的 8 个字段）")
        print("=" * 72)
        import sqlite3

        con = sqlite3.connect(work / "diagnose.db")
        try:
            columns = {row[1] for row in con.execute("PRAGMA table_info(student_error_logs)")}
            indexes = {row[1] for row in con.execute("PRAGMA index_list(student_error_logs)")}
        finally:
            con.close()
        required = {"id", "student_id", "code_snippet", "language",
                    "error_type", "knowledge_point", "ai_feedback", "created_at"}
        missing = sorted(required - columns)
        record("student_error_logs 表已建出且 8 个必需字段齐全",
               not missing, f"缺：{missing}" if missing else f"共 {len(columns)} 列")
        record("按「学生 + 时间」的组合索引存在（最近 N 条错题走索引）",
               "ix_student_error_logs_student_created" in indexes)

        # ---------------- 3. 参数校验（不需要模型） ----------------
        print()
        print("=" * 72)
        print("3. 参数校验：身份 / 语言 / 空代码")
        print("=" * 72)
        code, body = request("GET", "/api/v1/tutor/errors")
        record("不传任何身份查错题本 → 200 且归到固定身份 local（页面不填学号）",
               code == 200 and isinstance(body, dict) and body.get("student_id") == "local",
               f"HTTP {code} student_id={body.get('student_id') if isinstance(body, dict) else '?'}")

        code, body = post_json("/api/analyze", {
            "student_id": STUDENT_A, "language": "rust", "code_content": BROKEN_PY,
        })
        record("别名接口传不支持的语言 → 400", code == 400, f"HTTP {code}")

        code, body = post_json("/api/analyze", {
            "student_id": STUDENT_A, "language": "python", "code_content": "",
        })
        record("别名接口传空代码 → 422（被 Pydantic 挡在模型之前）",
               code == 422, f"HTTP {code}")

        code, body = request("GET", f"/api/v1/tutor/errors?student_id={STUDENT_A}")
        record("新学号的错题本是空的（total=0）",
               code == 200 and isinstance(body, dict) and body.get("total") == 0,
               f"HTTP {code} total={body.get('total') if isinstance(body, dict) else '?'}")

        if not use_ai:
            # ---------------- 4. 未配置模型时的降级 ----------------
            print()
            print("=" * 72)
            print("4. 未配置模型：给出可读的中文提示，而不是 500")
            print("=" * 72)
            code, body = post_json("/api/analyze", {
                "student_id": STUDENT_A, "language": "python", "code_content": BROKEN_PY,
            })
            detail = body.get("detail", "") if isinstance(body, dict) else str(body)
            record("诊断返回 503（AI 未启用）", code == 503, f"HTTP {code}")
            record("提示里说明了去哪里配 Key",
                   "API Key" in detail or "LLM__API_KEY" in detail, detail[:70])
        else:
            # ---------------- 4. 真实模型：完整诊断链路 ----------------
            print()
            print("=" * 72)
            print("4. 真实模型 · 第一次诊断（有错误的代码）")
            print("=" * 72)
            code, first = post_json("/api/analyze", {
                "student_id": STUDENT_A, "language": "python",
                "code_content": BROKEN_PY, "filename": "homework.py",
            })
            if code != 200:
                record("第一次诊断返回 200", False,
                       f"HTTP {code}：{str(first)[:160]}")
            else:
                assert isinstance(first, dict)
                analysis = first.get("analysis", {})
                book = first.get("wrong_question_book", {})
                exercise = book.get("new_exercise", {})

                print(f"       结论：{analysis.get('status')} / {analysis.get('error_type')}")
                print(f"       诊断：{str(analysis.get('message'))[:100]}")
                print(f"       知识点：{'、'.join(analysis.get('knowledge_points') or [])}")
                print(f"       历史总结：{str(book.get('history_summary'))[:100]}")
                print(f"       新题目：{exercise.get('question_title')}")
                print(f"       解析长度：{len(str(book.get('knowledge_explanation')))} 字")

                # 上下两半屏的字段一个都不能少，否则前端会渲染出空白块
                record("上半屏 analysis 四个字段齐全",
                       all(key in analysis for key in
                           ("status", "error_type", "message", "knowledge_points")))
                record("下半屏 wrong_question_book 三个字段齐全",
                       all(key in book for key in
                           ("history_summary", "new_exercise", "knowledge_explanation")))
                record("新题目包含 标题/描述/提示",
                       all(exercise.get(key) for key in
                           ("question_title", "question_description", "hint")))
                record("代码有错时 status=error（不是把错误当成通过）",
                       analysis.get("status") == "error", str(analysis.get("status")))
                record("提取出了至少一个知识点",
                       len(analysis.get("knowledge_points") or []) >= 1)
                record("知识点解析含 Markdown 代码示例（前端要按 MD 渲染）",
                       "```" in str(book.get("knowledge_explanation")))
                record("这次错误已归档进错题本（logged=true）",
                       first.get("logged") is True, str(first.get("logged")))
                record("第一次诊断没有历史错题可参考（history_count=0）",
                       first.get("history_count") == 0, str(first.get("history_count")))

                # ---- 错题本接口要能查到刚归档的这条 ----
                code, book_body = request(
                    "GET", f"/api/v1/tutor/errors?student_id={STUDENT_A}")
                assert isinstance(book_body, dict), book_body
                items = book_body.get("items", [])
                record("错题本接口查到了刚归档的错题",
                       book_body.get("total") == 1 and len(items) == 1,
                       f"total={book_body.get('total')}")
                if items:
                    record("错题记录里存下了原始代码（复习时要看「当时怎么写的」）",
                           bool(items[0].get("code_snippet")))
                    record("易错知识点聚合有数据（下半屏的条形图靠它）",
                           len(book_body.get("weak_points") or []) >= 1,
                           str(book_body.get("weak_points"))[:80])

                # ---- 身份隔离（可选能力）：另一个标识看不到 A 的错题 ----
                code, other = request(
                    "GET", f"/api/v1/tutor/errors?student_id={STUDENT_B}")
                assert isinstance(other, dict), other
                record("换个身份标识查 → 看不到别人的错题（接口仍支持按人归档）",
                       other.get("total") == 0, f"B total={other.get('total')}")

                # ---- 删别人的错题必须失败 ----
                if items:
                    code, _ = request(
                        "DELETE", f"/api/v1/tutor/errors/{items[0]['id']}"
                                  f"?student_id={STUDENT_B}")
                    record("拿别人的身份标识删这条错题 → 404（删不掉）",
                           code == 404, f"HTTP {code}")

                # ---------------- 5. 第二次诊断：历史错题要进提示词 ----------------
                print()
                print("=" * 72)
                print("5. 真实模型 · 第二次诊断（同样有错，验证「结合历史错题」）")
                print("=" * 72)
                code, second = post_json("/api/analyze", {
                    "student_id": STUDENT_A, "language": "python",
                    "code_content": BROKEN_PY, "filename": "homework2.py",
                })
                if code != 200:
                    record("第二次诊断返回 200", False, f"HTTP {code}：{str(second)[:160]}")
                else:
                    assert isinstance(second, dict)
                    record("第二次诊断带上了历史错题（history_count>=1）",
                           second.get("history_count", 0) >= 1,
                           f"history_count={second.get('history_count')}")
                    record("错题本总数已累积到 2 条",
                           second.get("history_total") == 2,
                           f"history_total={second.get('history_total')}")
                    summary = str(second.get("wrong_question_book", {}).get("history_summary"))
                    record("历史总结这次言之有物（提到了历史/规律，而不是空话）",
                           len(summary) >= 10, summary[:80])

                # ---------------- 6. 代码正确时不应写错题本 ----------------
                print()
                print("=" * 72)
                print("6. 真实模型 · 正确代码：出题但不记错题")
                print("=" * 72)
                code, good = post_json("/api/analyze", {
                    "student_id": STUDENT_B, "language": "python",
                    "code_content": GOOD_PY, "filename": "good.py",
                })
                if code != 200:
                    record("正确代码的诊断返回 200", False, f"HTTP {code}：{str(good)[:160]}")
                else:
                    assert isinstance(good, dict)
                    record("正确代码 → status=success 且 logged=false",
                           good.get("analysis", {}).get("status") == "success"
                           and good.get("logged") is False,
                           f"status={good.get('analysis', {}).get('status')} "
                           f"logged={good.get('logged')}")
                    record("即使代码没错，依然给出了新题目（提示词 Constraint 1）",
                           bool(good.get("wrong_question_book", {})
                                .get("new_exercise", {}).get("question_title")))
                    code, b_book = request(
                        "GET", f"/api/v1/tutor/errors?student_id={STUDENT_B}")
                    assert isinstance(b_book, dict), b_book
                    record("正确代码没有污染错题本（B 的错题数仍为 0）",
                           b_book.get("total") == 0, f"total={b_book.get('total')}")

        # ---------------- 汇总 ----------------
        print()
        print("=" * 72)
        passed = sum(1 for _, ok in checks if ok)
        print(f"验收结果：{passed}/{len(checks)} 项通过")
        print("=" * 72)
        for label, ok in checks:
            print(f"  {'OK  ' if ok else 'FAIL'} {label}")
        return 0 if passed == len(checks) else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
