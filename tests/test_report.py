"""学习报告功能测试：使用时长记录 + 错题整改汇总 + 导出 Word。

覆盖四层：
  1. 时长累加：心跳把间隔截断后累加，迟到很久的心跳不会虚报学习时长；
  2. 聚合口径：区间过滤、按知识点 / 类型 / 语言分组、占比；
  3. 学习建议：纯规则生成，不依赖大模型（这条是硬要求——老师可能没配 Key）；
  4. 导出的 .docx：包结构、XML 合法性、内容对不对，以及**中文文件名**的响应头。

用 TestClient 跑真实 lifespan，因此"服务启动就记一条使用记录"也一并被覆盖。
"""

from __future__ import annotations

import asyncio
import io
import re
import time
import zipfile
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import unquote
from xml.etree import ElementTree as ET

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.main import create_app
from app.models.base import utc_now
from app.models.usage import UsageSession
from app.services.report_docx import render_learning_report
from app.services.report_service import ReportService
from app.services.tutor_service import TutorService
from app.services.usage_service import (
    MAX_HEARTBEAT_GAP_SECONDS,
    UsageService,
    accumulate,
    format_duration,
    parse_range,
)

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@asynccontextmanager
async def _db() -> AsyncIterator[AsyncSession]:
    """直连数据库的会话。

    结束时必须 dispose_engine()：引擎缓存在模块级全局变量里，
    不释放的话下一个用例会拿到**上一个用例的数据库**。
    """
    from app.core.database import dispose_engine, get_session, init_db

    await init_db()
    try:
        async with get_session() as session:
            yield session
    finally:
        await dispose_engine()


@pytest.fixture()
def client(sqlite_path: Path) -> Iterator[TestClient]:
    """带真实 lifespan 的测试客户端（启动时会写一条使用记录）。"""
    get_settings.cache_clear()
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


async def _add_usage(
    session: AsyncSession,
    *,
    student_id: str,
    started_minutes_ago: float,
    active_seconds: int,
) -> UsageSession:
    """直接造一条使用记录（用于统计断言，不必真等时间过去）。"""
    started = utc_now() - timedelta(minutes=started_minutes_ago)
    item = UsageSession(
        student_id=student_id,
        started_at=started,
        last_seen_at=started,
        active_seconds=active_seconds,
        pid=1234,
        app_version="0.1.0",
    )
    session.add(item)
    await session.commit()
    await session.refresh(item)
    return item


async def _add_error(
    session: AsyncSession,
    *,
    student_id: str,
    knowledge_point: str,
    error_type: str = "逻辑错误",
    language: str = "python",
    minutes_ago: float = 0,
    feedback: str = "AI 反馈内容",
) -> None:
    item = await TutorService.save_error(
        session,
        student_id=student_id,
        code_snippet="def add(a, b):\n    return a - b\n",
        language=language,
        error_type=error_type,
        knowledge_points=[knowledge_point, "附加"],
        ai_feedback=feedback,
    )
    assert item is not None
    # created_at 是写入时自动生成的，这里改成想要的时间点，方便测"区间过滤"
    item.created_at = utc_now() - timedelta(minutes=minutes_ago)
    await session.commit()


# ---------------------------------------------------------------------------
# 1. 时长累加
# ---------------------------------------------------------------------------
def test_accumulate_caps_single_gap() -> None:
    """单段心跳截断到 5 分钟——这是"合上笔记本两小时"不被算成学习的关键。"""
    assert accumulate(0, 60) == 60                     # 正常间隔原样累加
    assert accumulate(100, 7200) == 100 + MAX_HEARTBEAT_GAP_SECONDS
    assert accumulate(100, 0) == 100                   # 零间隔不加
    assert accumulate(100, -5) == 100                  # 时钟回拨不加（也不减）


def test_format_duration_reads_naturally() -> None:
    assert format_duration(0) == "0 秒"
    assert format_duration(45) == "45 秒"
    assert format_duration(90) == "1 分 30 秒"
    assert format_duration(3600) == "1 小时 0 分"
    assert format_duration(5220) == "1 小时 27 分"


def test_parse_range_is_tolerant() -> None:
    """区间参数写错不该报 422——老师可能手改地址栏。"""
    assert parse_range("all") == (None, "all")
    assert parse_range("0") == (None, "all")
    start, key = parse_range("7")
    assert key == "7" and start is not None
    assert parse_range("胡说八道")[1] == "30"          # 认不出来按 30 天
    assert parse_range("-3")[1] == "all"               # 负数当全部历史
    assert parse_range(None)[1] == "all"


def test_start_session_writes_row(sqlite_path: Path) -> None:

    async def run() -> None:
        async with _db() as session:
            session_id = await UsageService.start_session(
                session, student_id="local", app_version="0.1.0"
            )
            assert session_id is not None
            item = await session.get(UsageSession, session_id)
            assert item is not None
            assert item.student_id == "local"
            assert item.active_seconds == 0
            assert item.ended_at is None
            # 时间必须带 UTC 时区，否则前端会按本地时间解析而整体偏一个时区
            assert item.started_at.tzinfo is not None

    asyncio.run(run())


def test_heartbeat_accumulates_and_is_owner_scoped(sqlite_path: Path) -> None:

    async def run() -> None:
        async with _db() as session:
            session_id = await UsageService.start_session(session, student_id="local")
            assert session_id is not None

            # 把 last_seen 拨回 2 小时前，模拟"标签页被挂起了很久"
            item = await session.get(UsageSession, session_id)
            assert item is not None
            item.last_seen_at = utc_now() - timedelta(hours=2)
            await session.commit()

            updated = await UsageService.heartbeat(
                session, session_id=session_id, student_id="local"
            )
            assert updated is not None
            assert updated.active_seconds == MAX_HEARTBEAT_GAP_SECONDS

            # 换个人来发心跳：不该改到别人的记录
            stolen = await UsageService.heartbeat(
                session, session_id=session_id, student_id="别人"
            )
            assert stolen is None
            again = await session.get(UsageSession, session_id)
            assert again is not None
            assert again.active_seconds == MAX_HEARTBEAT_GAP_SECONDS

    asyncio.run(run())


def test_end_session_records_last_segment(sqlite_path: Path) -> None:

    async def run() -> None:
        async with _db() as session:
            session_id = await UsageService.start_session(session, student_id="local")
            assert session_id is not None
            await UsageService.end_session(session, session_id=session_id)
            item = await session.get(UsageSession, session_id)
            assert item is not None
            assert item.ended_at is not None
            # 刚刚开始就结束：有效时长为 0，但收尾时间必须有
            assert item.active_seconds >= 0

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 2. 汇总口径
# ---------------------------------------------------------------------------
def test_summary_totals_and_daily(sqlite_path: Path) -> None:

    async def run() -> None:
        async with _db() as session:
            await _add_usage(session, student_id="A", started_minutes_ago=60, active_seconds=600)
            await _add_usage(session, student_id="A", started_minutes_ago=120, active_seconds=300)
            await _add_usage(session, student_id="B", started_minutes_ago=30, active_seconds=999)

            summary = await UsageService.summary(session, student_id="A")
            assert summary["total_seconds"] == 900
            assert summary["session_count"] == 2
            assert summary["average_seconds"] == 450
            # 只算 A 的，B 的记录不能混进来
            assert "B" not in {item.student_id for item in summary["sessions"]}

    asyncio.run(run())


def test_group_by_field_rejects_unknown_column(sqlite_path: Path) -> None:
    """分组字段只接受白名单，避免"把列名拼进 SQL"这种注入形状的代码。"""

    async def run() -> None:
        async with _db() as session:
            with pytest.raises(ValueError):
                await TutorService.group_by_field(
                    session, student_id="A", field="ai_feedback; DROP TABLE"
                )

    asyncio.run(run())


def test_report_filters_by_range(sqlite_path: Path) -> None:

    async def run() -> None:
        async with _db() as session:
            await _add_error(session, student_id="A", knowledge_point="循环边界", minutes_ago=10)
            await _add_error(session, student_id="A", knowledge_point="循环边界", minutes_ago=20)
            await _add_error(session, student_id="A", knowledge_point="数组下标", minutes_ago=10)
            # 100 天前的错题：只应出现在"全部历史"里
            await _add_error(session, student_id="A", knowledge_point="旧知识点", minutes_ago=100 * 1440)

            recent = await ReportService.build(session, student_id="A", range_value="30")
            assert recent["errors"]["total"] == 3
            assert "旧知识点" not in {i["knowledge_point"] for i in recent["errors"]["items"]}

            all_time = await ReportService.build(session, student_id="A", range_value="all")
            assert all_time["errors"]["total"] == 4

    asyncio.run(run())


def test_report_groups_and_ratios(sqlite_path: Path) -> None:

    async def run() -> None:
        async with _db() as session:
            for _ in range(3):
                await _add_error(session, student_id="A", knowledge_point="循环边界")
            await _add_error(session, student_id="A", knowledge_point="数组下标",
                             error_type="运行时错误", language="c")

            report = await ReportService.build(session, student_id="A", range_value="all")
            weak = report["errors"]["weak_points"]
            assert weak[0]["knowledge_point"] == "循环边界"
            assert weak[0]["count"] == 3
            assert weak[0]["ratio"] == "75%"          # 3/4
            assert {i["error_type"] for i in report["errors"]["by_type"]} == {
                "逻辑错误", "运行时错误"
            }
            assert {i["language"] for i in report["errors"]["by_language"]} == {"python", "c"}

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 3. 学习建议（纯规则，不调大模型）
# ---------------------------------------------------------------------------
def test_suggestions_without_any_data(sqlite_path: Path) -> None:

    async def run() -> None:
        async with _db() as session:
            report = await ReportService.build(session, student_id="空", range_value="all")
            assert report["usage"]["session_count"] == 0
            assert any("没有使用记录" in tip for tip in report["suggestions"])

    asyncio.run(run())


def test_suggestions_name_the_weakest_point(sqlite_path: Path) -> None:

    async def run() -> None:
        async with _db() as session:
            for _ in range(4):
                await _add_error(session, student_id="A", knowledge_point="循环边界")
            await _add_usage(session, student_id="A", started_minutes_ago=30, active_seconds=1800)

            report = await ReportService.build(session, student_id="A", range_value="all")
            joined = "".join(report["suggestions"])
            assert "循环边界" in joined
            assert "4 次" in joined

    asyncio.run(run())


def test_suggestions_warn_on_low_usage(sqlite_path: Path) -> None:

    async def run() -> None:
        async with _db() as session:
            await _add_usage(session, student_id="A", started_minutes_ago=5, active_seconds=120)
            report = await ReportService.build(session, student_id="A", range_value="all")
            assert any("低于 30 分钟" in tip for tip in report["suggestions"])

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 4. 导出的 Word
# ---------------------------------------------------------------------------
def _sample_report() -> dict:
    now = utc_now()
    return {
        "student_id": "local",
        "generated_at": now,
        "range_key": "all",
        "range_label": "全部历史",
        "app_version": "0.1.0",
        "usage": {
            "total_seconds": 5220,
            "total_text": "1 小时 27 分",
            "session_count": 1,
            "active_days": 1,
            "average_seconds": 5220,
            "average_text": "1 小时 27 分",
            "last_used_at": now,
            "daily": [{"date": "2026-09-21", "seconds": 5220}],
            "sessions": [
                {
                    "id": 1,
                    "started_at": now,
                    "last_seen_at": now,
                    "ended_at": None,
                    "active_seconds": 5220,
                    "active_text": "1 小时 27 分",
                    "wall_seconds": 5400,
                }
            ],
        },
        "errors": {
            "total": 2,
            "listed": 2,
            "weak_points": [{"knowledge_point": "循环边界", "count": 2, "ratio": "100%"}],
            "by_type": [{"error_type": "逻辑错误", "count": 2, "ratio": "100%"}],
            "by_language": [{"language": "python", "count": 2, "ratio": "100%"}],
            "items": [
                {
                    "id": 1,
                    "created_at": now,
                    "language": "python",
                    "filename": "homework1.py",
                    "error_type": "逻辑错误",
                    "knowledge_point": "循环边界",
                    "knowledge_points": ["循环边界", "累加"],
                    "ai_feedback": "range 少遍历一个元素",
                    "ai_feedback_excerpt": "range 少遍历一个元素",
                    "code_excerpt": "for i in range(len(x)-1)",
                    "line_count": 4,
                }
            ],
        },
        "suggestions": ["「循环边界」累计错 2 次，建议重做。"],
    }


def test_docx_package_structure_is_complete() -> None:
    """Word 只认完整包：少一个部件就会弹"文件已损坏"。"""
    payload = render_learning_report(_sample_report())
    assert payload[:2] == b"PK"           # zip 魔数

    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert zf.testzip() is None
        names = set(zf.namelist())
        for part in (
            "[Content_Types].xml",
            "_rels/.rels",
            "word/document.xml",
            "word/styles.xml",
            "word/footer1.xml",
            "word/_rels/document.xml.rels",
            "docProps/core.xml",
            "docProps/app.xml",
        ):
            assert part in names, f"缺少部件 {part}"

        # 每个 XML 部件都要能解析
        for name in names:
            if name.endswith(".xml") or name.endswith(".rels"):
                ET.fromstring(zf.read(name))


def test_docx_contains_the_actual_data() -> None:
    payload = render_learning_report(_sample_report())
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        root = ET.fromstring(zf.read("word/document.xml"))

    blob = "\n".join(
        "".join(node.text or "" for node in para.iter(f"{W}t"))
        for para in root.iter(f"{W}p")
    )
    assert "学习报告" in blob
    assert "一、使用情况" in blob
    assert "二、错题整改情况" in blob
    assert "三、学习建议" in blob
    assert "1 小时 27 分" in blob
    assert "循环边界" in blob
    assert "range 少遍历一个元素" in blob
    # 表格：基本信息 / 使用汇总 / 按天 / 每次明细 / 知识点 / 类型 / 语言 / 错题明细
    assert len(list(root.iter(f"{W}tbl"))) >= 8


def test_docx_escapes_special_characters() -> None:
    """代码片段里出现 < > & 时不能把 XML 弄坏。"""
    report = _sample_report()
    report["errors"]["items"][0]["ai_feedback_excerpt"] = 'if (a < b) && (c > d) "引号"'
    report["errors"]["items"][0]["knowledge_points"] = ["指针 <-> 数组"]
    payload = render_learning_report(report)

    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        xml = zf.read("word/document.xml")
    root = ET.fromstring(xml)             # 解析不过就说明转义漏了
    blob = "".join(node.text or "" for node in root.iter(f"{W}t"))
    assert "<" in blob and "&" in blob    # 转义后回读还是原字符


def test_docx_handles_empty_report() -> None:
    """一条数据都没有时也必须能导出，而不是抛异常。"""
    report = _sample_report()
    report["usage"]["sessions"] = []
    report["usage"]["daily"] = []
    report["errors"] = {
        "total": 0, "listed": 0, "weak_points": [], "by_type": [],
        "by_language": [], "items": [],
    }
    payload = render_learning_report(report)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        assert zf.testzip() is None


# ---------------------------------------------------------------------------
# 5. HTTP 接口
# ---------------------------------------------------------------------------
def test_usage_current_creates_session_on_startup(client: TestClient) -> None:
    """「点击 start.bat 记录使用时间」：服务一起来就该有记录。"""
    data = client.get("/api/v1/usage/current").json()
    assert data["session_id"] is not None
    assert data["active_seconds"] == 0
    assert data["started_at"].endswith("Z")       # 必须带时区


def test_usage_heartbeat_endpoint(client: TestClient) -> None:
    first = client.get("/api/v1/usage/current").json()
    response = client.post("/api/v1/usage/heartbeat", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == first["session_id"]
    assert body["active_seconds"] >= 0

    # 显式指定一个不存在的 session_id：如实返回提示，而不是 500
    missing = client.post("/api/v1/usage/heartbeat", json={"session_id": 999999})
    assert missing.status_code == 200
    assert missing.json()["message"]


def test_report_summary_endpoint(client: TestClient) -> None:
    response = client.get("/api/v1/report/summary?range=all")
    assert response.status_code == 200
    body = response.json()
    assert body["student_id"] == "local"
    assert body["range_label"] == "全部历史"
    assert body["usage"]["session_count"] >= 1     # 启动时那条
    assert body["suggestions"]                       # 一定有建议（哪怕只有一条兜底）

    # 区间参数写错也不报错
    assert client.get("/api/v1/report/summary?range=乱写").status_code == 200


def test_report_word_endpoint_serves_docx_download(client: TestClient) -> None:
    response = client.get("/api/v1/report/word?range=all")
    assert response.status_code == 200
    assert (
        response.headers["content-type"]
        == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert response.content[:2] == b"PK"

    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment;")
    assert "filename*=UTF-8''" in disposition
    # 中文文件名要能完整还原（RFC 5987 百分号编码）
    assert "学习报告" in unquote(disposition)
    assert "local" in unquote(disposition)


def test_report_endpoints_are_registered() -> None:
    """接口清单是给前端与文档对齐用的，少挂一个模块必须能被发现。"""
    paths = create_app().openapi()["paths"]
    for path in (
        "/api/v1/usage/heartbeat",
        "/api/v1/usage/current",
        "/api/v1/usage/summary",
        "/api/v1/report/summary",
        "/api/v1/report/word",
    ):
        assert path in paths, f"接口没注册：{path}"


def test_startup_survives_unwritable_usage_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """使用记录写不进去时服务仍要能起来（统计是锦上添花，不是生命线）。

    这条不是假想：教室机器的磁盘满了、`app.db` 被别的程序锁住、
    或者只读共享目录，都会让写入失败。此时学生要的是"还能做题"，
    而不是"服务起不来"。
    """
    from app.core.database import dispose_engine
    from app.services import usage_service

    async def boom(*_args: object, **_kwargs: object) -> int | None:
        raise RuntimeError("数据库坏了")

    monkeypatch.setattr(usage_service.UsageService, "start_session", boom)
    monkeypatch.setenv("DATABASE__SQLITE_PATH", str(tmp_path / "broken.db"))
    get_settings.cache_clear()
    asyncio.run(dispose_engine())

    with TestClient(create_app()) as test_client:
        assert test_client.get("/api/v1/health").status_code == 200
        # 时长统计不可用，但接口本身不能 500
        data = test_client.get("/api/v1/usage/current").json()
        assert data["session_id"] is None
        assert data["message"]
        assert test_client.get("/api/v1/report/summary").status_code == 200

    get_settings.cache_clear()


def test_report_generated_at_is_timezone_aware(client: TestClient) -> None:
    """生成时间必须带 Z —— 否则前端按本地时间解析，会整体偏一个时区。"""
    body = client.get("/api/v1/report/summary").json()
    assert body["generated_at"].endswith("Z")
    assert datetime.fromisoformat(body["generated_at"].replace("Z", "+00:00")).tzinfo is not None


# ---------------------------------------------------------------------------
# 6. 前端静态文件的缓存策略与导出入口
# ---------------------------------------------------------------------------
def test_frontend_files_are_never_cached(client: TestClient) -> None:
    """前端页面与资源必须 no-store，绝不能走浏览器缓存。

    这条是踩坑之后加的，而且是**踩了两次**：StaticFiles 只给 ETag /
    Last-Modified、不给任何 Cache-Control，浏览器于是按启发式规则自己决定
    新鲜期，在有效期内连请求都不发。后果是新版本加了「导出学习报告」按钮，
    服务端返回的 HTML 里明明有，学生刷新后浏览器渲染的还是旧页面 ——
    看起来就是"功能没做"。

    第一版改成 no-cache（"用之前先验证"），对**新存下的**缓存有效，
    但挡不住加这个头之前就已经存在的条目，用户会被历史缓存一直卡住。
    所以最终用 no-store：前端资源永远不缓存。
    代价只是本机 localhost 上多传几百 KB，换来"改了页面就一定看得到"。

    API 响应不受影响：业务接口不该被浏览器缓存，但也不该被这条中间件
    顺手改掉语义。
    """
    for path in ("/ui/index.html", "/ui/js/app.js", "/ui/css/style.css"):
        response = client.get(path)
        assert response.status_code == 200, path
        assert "no-store" in response.headers.get("cache-control", ""), path

    # 根路径的 307 跳转也必须 no-store，否则它本身会被缓存住，
    # 后面的时间戳参数就永远追不上
    redirect = client.get("/", follow_redirects=False)
    assert redirect.status_code == 307
    assert "no-store" in redirect.headers.get("cache-control", "")
    assert redirect.headers["location"].startswith("/ui/index.html?t=")

    # API 响应不带这个头
    assert "no-store" not in client.get("/api/v1/health").headers.get("cache-control", "")


def test_root_redirect_carries_a_cache_buster(client: TestClient) -> None:
    """每次打开根路径都应拿到一个**新的**地址。

    start.bat 打开的就是根地址。带上时间戳之后，无论浏览器里存着多老的
    缓存，这一次打开必然是新页面 —— "双击 start.bat 却看到旧界面"这类问题
    从入口就断掉了。
    """
    first = client.get("/", follow_redirects=False).headers["location"]
    time.sleep(0.01)
    second = client.get("/", follow_redirects=False).headers["location"]
    assert first.startswith("/ui/index.html?t=")
    assert first != second, "两次打开拿到同一个地址，说明没有起到绕开缓存的作用"


def test_frontend_page_contains_export_entry(client: TestClient) -> None:
    """页面上必须真的能看到导出入口。

    需求是「导出的 Word 从浏览器里直接下载」，所以入口得是带 download 的
    <a>；而且必须**同时**出现在顶栏 —— 左栏是纵向滚动区，卡片会被文件列表
    顶到看不见，只放那里等于没做。
    """
    html = client.get("/ui/index.html").text
    for element_id in ("topbarExport", "reportDownload"):
        assert f'id="{element_id}"' in html, element_id
        match = re.search(rf'<a\b[^>]*id="{element_id}"[^>]*>', html)
        assert match is not None, element_id
        tag = match.group(0)
        assert "download" in tag, element_id
        assert "/api/v1/report/word" in tag, element_id

    # 「本次已使用」也要在常驻区域（顶栏）能看到，否则计时同样没人看见
    assert 'id="topbarUsage"' in html


def test_action_buttons_have_no_icons(client: TestClient) -> None:
    """四个功能按钮前不带图标，且配色统一。

    真实反馈："AI 检测 / 生成注释 / 自动改错 / 诊断+举一反三，它们的功能前
    不带图标，界面需要简洁"。所以：
      · 按钮里不能再出现 emoji 或 <span class="btn__icon">；
      · 四个按钮不能各配一种高饱和色（原先蓝/紫/橙/绿四块），
        只保留主操作强调色，其余统一描边。
    """
    html = client.get("/ui/index.html").text

    for button_id in ("btnCheck", "btnComment", "btnFix", "btnDiagnose"):
        match = re.search(rf'<button\b[^>]*id="{button_id}"[^>]*>(.*?)</button>', html, re.S)
        assert match is not None, button_id
        inner = match.group(1)
        assert "btn__icon" not in inner, f"{button_id} 里还有图标 span"
        # 按钮文字里不能再有 emoji（U+1F300 以上的字符）
        assert not any(ord(ch) > 0x1F000 for ch in inner), f"{button_id} 里还有 emoji：{inner!r}"

    assert "btn__icon" not in html, "页面里还留着 btn__icon"
    # 次要语义色按钮已随配色统一一起删除，不该再出现在页面上
    for modifier in ("btn--accent", "btn--warn", "btn--ok"):
        assert modifier not in html, f"{modifier} 又回到页面上了"
