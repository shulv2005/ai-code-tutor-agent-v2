"""把学习报告渲染成 .docx —— 直接拼 OOXML，**不引入 python-docx 依赖**。

## 为什么手写而不用 python-docx（这是一个刻意的取舍）

python-docx 会连带把 lxml（一个几 MB 的编译扩展）装进来。本项目的核心卖点
是"双击 start.bat，什么都不用装"，运行期依赖一直压在 16 个；为了导出一份
表格文档就翻倍式地加依赖不划算。而 .docx 本质就是一个 zip + 几个 XML，
本报告用到的元素（段落、标题、表格、页脚页码）都是很基础的那几种，
手写的代码量可控，而且**生成结果可以用 Word 亲自打开验证**（tools/verify_report.py 就是这么做的）。

## 生成的包结构

    [Content_Types].xml
    _rels/.rels
    docProps/core.xml            标题 / 作者 / 生成时间
    docProps/app.xml             页数等统计（Word 会自己重算）
    word/document.xml            正文
    word/styles.xml              正文 / 标题 / 表格样式
    word/footer1.xml             页脚居中页码
    word/_rels/document.xml.rels 引用 styles.xml 与 footer1.xml

## 容易写错的几处（都踩过）

* `w:sectPr` 必须是 `w:body` 的**最后一个**子元素，否则 Word 直接报文档损坏；
* 表格必须先有 `w:tblGrid`，且每行单元格的宽度之和要与它一致，否则列宽会乱；
* 页脚里的页码要用 `PAGE` 域（`fldChar begin / instrText / fldChar end` 三段），
  只写一个 `w:instrText` 是不显示的；
* 文本里的 `&`、`<`、`>` 必须转义，否则 XML 解析失败。
"""

from __future__ import annotations

import io
import zipfile
from datetime import datetime
from typing import Any
from xml.sax.saxutils import escape

from app.services.png_chart import render_radar_png

# --- 页面与版式常量（单位：twips，1 英寸 = 1440 twips）-------------------------
PAGE_WIDTH = 11906  # A4 宽
PAGE_HEIGHT = 16838  # A4 高
MARGIN = 1440  # 上下左右页边距，约 2.54 cm
CONTENT_WIDTH = PAGE_WIDTH - MARGIN * 2  # 表格总宽，必须与各列宽之和一致

# 字号（单位：半磅）。22pt=44、16pt=32、14pt=28、12pt=24、10.5pt=21、9pt=18
SZ_TITLE = "44"
SZ_SUBTITLE = "24"
SZ_H1 = "32"
SZ_H2 = "28"
SZ_BODY = "24"
SZ_TABLE = "21"
SZ_SMALL = "18"

CN_FONT = "宋体"
EN_FONT = "Times New Roman"

NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
# 图片用的命名空间。wp: 必须在 w:document 上声明 —— 只在 <wp:inline> 里
# 写 xmlns:a / xmlns:pic 是不够的，wp: 前缀没人声明时 Word 直接报
# "文档可能已经损坏"（踩过一次，报错信息完全不提命名空间）。
NS_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"


def _t(text: Any) -> str:
    """转义并包成 `<w:t>`。空串也要留一个元素，否则 Word 里是空段落。"""
    body = escape("" if text is None else str(text))
    return f'<w:t xml:space="preserve">{body}</w:t>'


def _run(
    text: Any,
    *,
    size: str = SZ_BODY,
    bold: bool = False,
    color: str | None = None,
) -> str:
    """一个文本 run。中文字体走 `w:eastAsia`，西文走 `w:ascii`。"""
    props = [f'<w:rFonts w:ascii="{EN_FONT}" w:hAnsi="{EN_FONT}" w:eastAsia="{CN_FONT}"/>']
    if bold:
        props.append("<w:b/><w:bCs/>")
    if color:
        props.append(f'<w:color w:val="{color}"/>')
    props.append(f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>')
    return f"<w:r><w:rPr>{''.join(props)}</w:rPr>{_t(text)}</w:r>"


def _para(
    runs: str,
    *,
    align: str | None = None,
    before: int = 0,
    after: int = 80,
    line: int = 300,
    style: str | None = None,
    keep_next: bool = False,
) -> str:
    props = []
    if style:
        props.append(f'<w:pStyle w:val="{style}"/>')
    if keep_next:
        props.append("<w:keepNext/>")
    props.append(
        f'<w:spacing w:before="{before}" w:after="{after}" '
        f'w:line="{line}" w:lineRule="auto"/>'
    )
    if align:
        props.append(f'<w:jc w:val="{align}"/>')
    return f"<w:p><w:pPr>{''.join(props)}</w:pPr>{runs}</w:p>"


def _cell(
    content: str,
    *,
    width: int,
    fill: str | None = None,
    valign: str = "center",
) -> str:
    shade = f'<w:shd w:val="clear" w:color="auto" w:fill="{fill}"/>' if fill else ""
    props = (
        f'<w:tcPr><w:tcW w:w="{width}" w:type="dxa"/>{shade}'
        f'<w:vAlign w:val="{valign}"/></w:tcPr>'
    )
    return f"<w:tc>{props}{content}</w:tc>"


def _text_cell(
    text: Any,
    *,
    width: int,
    fill: str | None = None,
    bold: bool = False,
    align: str | None = None,
    size: str = SZ_TABLE,
) -> str:
    body = _para(
        _run(text, size=size, bold=bold),
        align=align,
        before=20,
        after=20,
        line=240,
    )
    return _cell(body, width=width, fill=fill)


def _table(rows: list[str], widths: list[int]) -> str:
    """拼一张表。`rows` 里每一项已经是若干 `<w:tc>` 拼起来的字符串。"""
    grid = "".join(f'<w:gridCol w:w="{w}"/>' for w in widths)
    borders = (
        "<w:tblBorders>"
        + "".join(
            f'<w:{edge} w:val="single" w:sz="4" w:space="0" w:color="9AA6B2"/>'
            for edge in ("top", "left", "bottom", "right", "insideH", "insideV")
        )
        + "</w:tblBorders>"
    )
    props = (
        f'<w:tblPr><w:tblW w:w="{sum(widths)}" w:type="dxa"/>'
        f'<w:tblLayout w:type="fixed"/>{borders}</w:tblPr>'
    )
    body = "".join(f"<w:tr>{row}</w:tr>" for row in rows)
    return f"<w:tbl>{props}<w:tblGrid>{grid}</w:tblGrid>{body}</w:tbl>"


def _heading(text: str, *, level: int = 1) -> str:
    style = "Heading1" if level == 1 else "Heading2"
    size = SZ_H1 if level == 1 else SZ_H2
    return _para(
        _run(text, size=size, bold=True, color="1F3864"),
        style=style,
        before=240 if level == 1 else 180,
        after=120,
        keep_next=True,
    )


# 图片在文档里的显示尺寸（EMU，1 厘米 = 360000 EMU）
IMG_WIDTH_CM = 10.5
EMU_PER_CM = 360000
IMAGE_REL_ID = "rId3"          # 与 DOCUMENT_RELS 里的图片关系对应
IMAGE_PART = "word/media/radar.png"


def _image_paragraph(png: bytes, *, alt: str, caption: str = "") -> str:
    """把一张 PNG 居中放进文档。

    ## 嵌入图片需要同时改四处（少一处 Word 就打不开或显示红叉）

      1. 包里写入 `word/media/radar.png`；
      2. `[Content_Types].xml` 加一条 `Default Extension="png"`；
      3. `word/_rels/document.xml.rels` 加一条 image 关系；
      4. 正文里用 `w:drawing` 引用那个 r:id，并给出 `wp:extent` 尺寸。

    尺寸必须按图片真实比例算：只给宽不给高，Word 会把图压扁。

    `w:docPr` 的 id 必须非 0 且在本文件内唯一，否则 Word 报"文档包含
    无法读取的内容"。
    """
    from app.services.png_chart import HEIGHT, WIDTH

    cx = int(IMG_WIDTH_CM * EMU_PER_CM)
    cy = int(cx * HEIGHT / WIDTH)
    drawing = (
        "<w:drawing><wp:inline distT=\"0\" distB=\"0\" distL=\"0\" distR=\"0\">"
        f'<wp:extent cx="{cx}" cy="{cy}"/>'
        f'<wp:effectExtent l="0" t="0" r="0" b="0"/>'
        f'<wp:docPr id="901" name="能力雷达图" descr="{escape(alt)}"/>'
        "<wp:cNvGraphicFramePr>"
        '<a:graphicFrameLocks xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
        ' noChangeAspect="1"/></wp:cNvGraphicFramePr>'
        '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">'
        '<pic:nvPicPr>'
        f'<pic:cNvPr id="901" name="radar.png" descr="{escape(alt)}"/>'
        "<pic:cNvPicPr/></pic:nvPicPr>"
        f'<pic:blipFill><a:blip r:embed="{IMAGE_REL_ID}"/><a:stretch><a:fillRect/>'
        "</a:stretch></pic:blipFill>"
        "<pic:spPr>"
        f'<a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
        "</pic:spPr></pic:pic></a:graphicData></a:graphic>"
        "</wp:inline></w:drawing>"
    )
    parts = [_para(f"<w:r>{drawing}</w:r>", align="center", after=60)]
    if caption:
        parts.append(
            _para(
                _run(caption, size=SZ_SMALL, color="7F7F7F"),
                align="center",
                after=180,
            )
        )
    return "".join(parts)


def _fmt_time(value: datetime | None) -> str:
    """统一按**本地时区**显示。库里存的是 UTC，老师要看的是自己手表上的时间。"""
    if value is None:
        return "—"
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _fmt_date(value: datetime | None) -> str:
    if value is None:
        return "—"
    return value.astimezone().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 各章节
# ---------------------------------------------------------------------------
def _section_basic(report: dict[str, Any]) -> str:
    widths = [2400, CONTENT_WIDTH - 2400]
    rows = [
        _text_cell("学生标识", width=widths[0], fill="EDF2FA", bold=True)
        + _text_cell(report["student_id"], width=widths[1]),
        _text_cell("统计区间", width=widths[0], fill="EDF2FA", bold=True)
        + _text_cell(report["range_label"], width=widths[1]),
        _text_cell("生成时间", width=widths[0], fill="EDF2FA", bold=True)
        + _text_cell(_fmt_time(report["generated_at"]), width=widths[1]),
        _text_cell("系统版本", width=widths[0], fill="EDF2FA", bold=True)
        + _text_cell(report["app_version"] or "—", width=widths[1]),
        _text_cell("数据来源", width=widths[0], fill="EDF2FA", bold=True)
        + _text_cell(
            "本机 SQLite 数据库中的使用记录与错题本，由系统自动汇总，未经人工修改。",
            width=widths[1],
        ),
    ]
    return _table(rows, widths)


def _section_usage(report: dict[str, Any]) -> str:
    usage = report["usage"]
    parts = [_heading("二、使用情况", level=1)]

    parts.append(
        _para(
            _run(
                f"统计区间内共使用 {usage['session_count']} 次，"
                f"涉及 {usage['active_days']} 天，"
                f"累计有效使用时长 {usage['total_text']}，"
                f"平均每次 {usage['average_text']}。"
            )
        )
    )
    if usage["last_used_at"] is not None:
        parts.append(
            _para(_run(f"最近一次使用：{_fmt_time(usage['last_used_at'])}", size=SZ_SMALL))
        )

    widths = [3200, CONTENT_WIDTH - 3200]
    rows = [
        _text_cell("累计有效使用时长", width=widths[0], fill="EDF2FA", bold=True)
        + _text_cell(usage["total_text"], width=widths[1], bold=True),
        _text_cell("使用天数", width=widths[0], fill="EDF2FA", bold=True)
        + _text_cell(f"{usage['active_days']} 天", width=widths[1]),
        _text_cell("使用次数", width=widths[0], fill="EDF2FA", bold=True)
        + _text_cell(f"{usage['session_count']} 次", width=widths[1]),
        _text_cell("平均每次时长", width=widths[0], fill="EDF2FA", bold=True)
        + _text_cell(usage["average_text"], width=widths[1]),
    ]
    parts.append(_table(rows, widths))
    parts.append(
        _para(
            _run(
                "说明：有效使用时长按页面心跳累加，只有学生真的开着学习页面才计入；"
                "服务启动后无人使用的时间不计。",
                size=SZ_SMALL,
                color="7F7F7F",
            ),
            after=160,
        )
    )

    # 按天分布
    if usage["daily"]:
        parts.append(_heading("（一）按天分布", level=2))
        widths = [3000, 3000, CONTENT_WIDTH - 6000]
        rows = [
            _text_cell("日期", width=widths[0], fill="D9E2F3", bold=True)
            + _text_cell("有效时长", width=widths[1], fill="D9E2F3", bold=True)
            + _text_cell("占区间总时长", width=widths[2], fill="D9E2F3", bold=True)
        ]
        for item in usage["daily"][:60]:
            seconds = int(item["seconds"])
            seconds_text = (
                f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分"
                if seconds >= 3600
                else f"{seconds // 60} 分 {seconds % 60} 秒"
            )
            share = (
                f"{seconds * 100 / usage['total_seconds']:.0f}%"
                if usage["total_seconds"]
                else "0%"
            )
            rows.append(
                _text_cell(item["date"], width=widths[0])
                + _text_cell(seconds_text, width=widths[1])
                + _text_cell(share, width=widths[2])
            )
        parts.append(_table(rows, widths))

    # 每次使用明细
    if usage["sessions"]:
        parts.append(_heading("（二）每次使用明细", level=2))
        widths = [800, 3200, 2500, CONTENT_WIDTH - 6500]
        rows = [
            _text_cell("序号", width=widths[0], fill="D9E2F3", bold=True, align="center")
            + _text_cell("开始时间", width=widths[1], fill="D9E2F3", bold=True)
            + _text_cell("有效时长", width=widths[2], fill="D9E2F3", bold=True)
            + _text_cell("服务运行时长", width=widths[3], fill="D9E2F3", bold=True)
        ]
        for index, item in enumerate(usage["sessions"], start=1):
            wall = int(item["wall_seconds"])
            wall_text = (
                f"{wall // 3600} 小时 {wall % 3600 // 60} 分"
                if wall >= 3600
                else f"{wall // 60} 分 {wall % 60} 秒"
            )
            rows.append(
                _text_cell(str(index), width=widths[0], align="center")
                + _text_cell(_fmt_time(item["started_at"]), width=widths[1])
                + _text_cell(item["active_text"], width=widths[2])
                + _text_cell(wall_text, width=widths[3])
            )
        parts.append(_table(rows, widths))

    return "".join(parts)


def _section_ability(report: dict[str, Any]) -> str:
    """一、能力雷达（六维）。

    图和表是配套的：图负责"一眼看出形状"，表负责"精确读数" ——
    自己写的 PNG 编码器画不了中文（标准库没有字体），所以图里只有图形，
    维度名与分数全部交给 Word 用真正的文本排版。
    """
    from app.services.png_chart import render_radar_png

    ability = report.get("ability") or {}
    dimensions = ability.get("dimensions") or []

    parts = [_heading("一、能力雷达（六维）", level=1)]
    parts.append(
        _para(
            _run(
                ability.get("summary")
                or "本区间没有可用于评估的记录，六个维度暂按满分显示。",
                size=SZ_SMALL,
                color="7F7F7F",
            ),
            after=160,
        )
    )

    parts.append(
        _image_paragraph(
            render_radar_png(dimensions, ability.get("weakest", "")),
            alt="六维能力雷达图",
            caption=(
                "图 1　学生能力雷达图（每圈 25 分；红色顶点为当前最弱的一项）"
            ),
        )
    )

    if not dimensions:
        return "".join(parts)

    widths = [CONTENT_WIDTH - 1600 - 1600 - 2400, 1600, 1600, 2400]
    rows = [
        _text_cell("能力维度", width=widths[0], fill="D9E2F3", bold=True)
        + _text_cell("得分", width=widths[1], fill="D9E2F3", bold=True, align="center")
        + _text_cell("依据", width=widths[2], fill="D9E2F3", bold=True, align="center")
        + _text_cell("该维度看什么", width=widths[3], fill="D9E2F3", bold=True)
    ]
    weakest_key = ability.get("weakest", "")
    for item in sorted(dimensions, key=lambda d: d["score"]):
        label = item["label"] + ("（最弱）" if item["key"] == weakest_key else "")
        rows.append(
            _text_cell(label, width=widths[0], bold=item["key"] == weakest_key)
            + _text_cell(f"{item['score']} 分", width=widths[1], align="center")
            + _text_cell(f"{item['evidence_count']} 条", width=widths[2], align="center")
            + _text_cell(item["hint"], width=widths[3])
        )
    parts.append(_table(rows, widths))

    # 说清楚这个分数是怎么来的 —— 教师看到"43 分"必须能知道依据是什么，
    # 否则这份报告就没法拿来跟学生谈。
    evidence = ability.get("evidence_total", 0)
    note = (
        f"评估依据：本区间内 {ability.get('error_total', 0)} 条错题记录、"
        f"{ability.get('check_total', 0)} 次代码检测，共 {evidence} 条证据。"
        "分数由证据的类别与严重程度折算（公式：100 / (1 + 权重和 / 10)，"
        "下限 15 分），用于横向比较各项强弱，不等同于考试分数。"
    )
    if ability.get("unclassified"):
        note += (
            f"其中 {ability['unclassified']} 条无法归到具体维度，"
            "按兜底规则计入了完备性或准确性。"
        )
    parts.append(_para(_run(note, size=SZ_SMALL, color="7F7F7F"), before=80))
    return "".join(parts)


def _section_errors(report: dict[str, Any]) -> str:
    errors = report["errors"]
    parts = [_heading("三、错题整改情况", level=1)]

    parts.append(
        _para(
            _run(
                f"统计区间内新增错题 {errors['total']} 条，"
                f"覆盖 {len(errors['weak_points'])} 个知识点、"
                f"{len(errors['by_language'])} 种语言。"
            )
        )
    )

    if not errors["total"]:
        parts.append(
            _para(
                _run(
                    "本区间没有新增错题记录。可能是这段时间没有提交过代码，"
                    "也没有做过诊断；请结合「使用情况」一节判断。",
                    color="7F7F7F",
                )
            )
        )
        return "".join(parts)

    # 易错知识点排行
    parts.append(_heading("（一）易错知识点排行", level=2))
    widths = [800, CONTENT_WIDTH - 800 - 1500 - 2400, 1500, 2400]
    rows = [
        _text_cell("排名", width=widths[0], fill="D9E2F3", bold=True, align="center")
        + _text_cell("知识点", width=widths[1], fill="D9E2F3", bold=True)
        + _text_cell("出错次数", width=widths[2], fill="D9E2F3", bold=True, align="center")
        + _text_cell("占全部错题", width=widths[3], fill="D9E2F3", bold=True, align="center")
    ]
    for index, item in enumerate(errors["weak_points"], start=1):
        rows.append(
            _text_cell(str(index), width=widths[0], align="center")
            + _text_cell(item["knowledge_point"], width=widths[1])
            + _text_cell(str(item["count"]), width=widths[2], align="center")
            + _text_cell(item["ratio"], width=widths[3], align="center")
        )
    parts.append(_table(rows, widths))

    # 错误类型 / 语言分布并排两张表（分开两张，Word 里更清楚）
    #
    # 注意这里有三层键名，别写混：`section_key` 是 errors 里的那一段，
    # `item_key` 是段里每一条的字段名（by_type 的条目里叫 error_type，
    # by_language 的条目里叫 language）。
    for title, section_key, item_key, label in (
        ("（二）错误类型分布", "by_type", "error_type", "错误类型"),
        ("（三）编程语言分布", "by_language", "language", "语言"),
    ):
        data = errors[section_key]
        if not data:
            continue
        parts.append(_heading(title, level=2))
        widths = [CONTENT_WIDTH - 2400 - 2400, 2400, 2400]
        rows = [
            _text_cell(label, width=widths[0], fill="D9E2F3", bold=True)
            + _text_cell("条数", width=widths[1], fill="D9E2F3", bold=True, align="center")
            + _text_cell("占比", width=widths[2], fill="D9E2F3", bold=True, align="center")
        ]
        for item in data:
            rows.append(
                _text_cell(item[item_key], width=widths[0])
                + _text_cell(str(item["count"]), width=widths[1], align="center")
                + _text_cell(item["ratio"], width=widths[2], align="center")
            )
        parts.append(_table(rows, widths))

    # 错题明细
    if errors["items"]:
        parts.append(_heading("（四）错题明细", level=2))
        if errors["listed"] < errors["total"]:
            parts.append(
                _para(
                    _run(
                        f"共 {errors['total']} 条，下表按时间倒序列出最近 {errors['listed']} 条。",
                        size=SZ_SMALL,
                        color="7F7F7F",
                    )
                )
            )
        widths = [1600, 1150, 2100, CONTENT_WIDTH - 4850]
        rows = [
            _text_cell("时间", width=widths[0], fill="D9E2F3", bold=True)
            + _text_cell("错误类型", width=widths[1], fill="D9E2F3", bold=True)
            + _text_cell("知识点", width=widths[2], fill="D9E2F3", bold=True)
            + _text_cell("问题说明（AI 诊断摘要）", width=widths[3], fill="D9E2F3", bold=True)
        ]
        for item in errors["items"]:
            points = "、".join(item["knowledge_points"][:3])
            rows.append(
                _text_cell(_fmt_time(item["created_at"]), width=widths[0], size=SZ_SMALL)
                + _text_cell(item["error_type"], width=widths[1], size=SZ_SMALL)
                + _text_cell(points, width=widths[2], size=SZ_SMALL)
                + _text_cell(item["ai_feedback_excerpt"], width=widths[3], size=SZ_SMALL)
            )
        parts.append(_table(rows, widths))

    return "".join(parts)


def _section_suggestions(report: dict[str, Any]) -> str:
    parts = [_heading("四、学习建议", level=1)]
    for index, tip in enumerate(report["suggestions"], start=1):
        parts.append(
            _para(_run(f"{index}. {tip}"), after=100)
        )
    parts.append(
        _para(
            _run(
                "以上建议由系统按固定规则根据区间内的数据生成（不依赖网络与任何大模型），"
                "供老师讲评时参考。",
                size=SZ_SMALL,
                color="7F7F7F",
            ),
            before=120,
        )
    )
    return "".join(parts)


# ---------------------------------------------------------------------------
# 包组装
#
# 这一节的 XML 用「短字符串相加」而不是整块三引号模板：OOXML 里有很多
# 一条就是一百多字符的 ContentType / Relationship 长 URL，整块模板会一路撑爆
# 行宽；拆开写还有个额外好处——部件名与类型集中成常量，加一个部件只改一处。
# ---------------------------------------------------------------------------
XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'

NS_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS_PKG_META = "http://schemas.openxmlformats.org/package/2006/relationships/metadata"
REL_OFFICE_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_OD = "application/vnd.openxmlformats-officedocument"
CT_DOCUMENT = f"{_OD}.wordprocessingml.document.main+xml"
CT_STYLES = f"{_OD}.wordprocessingml.styles+xml"
CT_FOOTER = f"{_OD}.wordprocessingml.footer+xml"
CT_CORE = "application/vnd.openxmlformats-package.core-properties+xml"
CT_APP = f"{_OD}.extended-properties+xml"


def _override(part: str, content_type: str) -> str:
    return f'<Override PartName="{part}" ContentType="{content_type}"/>'


def _relationship(rel_id: str, rel_type: str, target: str) -> str:
    return f'<Relationship Id="{rel_id}" Type="{rel_type}" Target="{target}"/>'


CONTENT_TYPES = (
    XML_DECL
    + f'<Types xmlns="{NS_CT}">'
    + '<Default Extension="rels" ContentType='
    + '"application/vnd.openxmlformats-package.relationships+xml"/>'
    + '<Default Extension="xml" ContentType="application/xml"/>'
    # 雷达图那张 PNG。少这一条 Word 会说"图片无法显示"。
    + '<Default Extension="png" ContentType="image/png"/>'
    + _override("/word/document.xml", CT_DOCUMENT)
    + _override("/word/styles.xml", CT_STYLES)
    + _override("/word/footer1.xml", CT_FOOTER)
    + _override("/docProps/core.xml", CT_CORE)
    + _override("/docProps/app.xml", CT_APP)
    + "</Types>"
)

ROOT_RELS = (
    XML_DECL
    + f'<Relationships xmlns="{NS_REL}">'
    + _relationship("rId1", f"{REL_OFFICE_DOC}/officeDocument", "word/document.xml")
    + _relationship("rId2", f"{NS_PKG_META}/core-properties", "docProps/core.xml")
    + _relationship("rId3", f"{REL_OFFICE_DOC}/extended-properties", "docProps/app.xml")
    + "</Relationships>"
)

DOCUMENT_RELS = (
    XML_DECL
    + f'<Relationships xmlns="{NS_REL}">'
    + _relationship("rId1", f"{REL_OFFICE_DOC}/styles", "styles.xml")
    + _relationship("rId2", f"{REL_OFFICE_DOC}/footer", "footer1.xml")
    # 雷达图。类型名是 image（不是 picture），Target 是包内相对路径。
    + _relationship(IMAGE_REL_ID, f"{REL_OFFICE_DOC}/image", "media/radar.png")
    + "</Relationships>"
)

_BASE_FONTS = (
    f'<w:rFonts w:ascii="{EN_FONT}" w:hAnsi="{EN_FONT}" '
    f'w:eastAsia="{CN_FONT}" w:cs="{EN_FONT}"/>'
)

STYLES = (
    XML_DECL
    + f'<w:styles xmlns:w="{NS_W}">'
    + "<w:docDefaults><w:rPrDefault><w:rPr>"
    + _BASE_FONTS
    + f'<w:sz w:val="{SZ_BODY}"/><w:szCs w:val="{SZ_BODY}"/>'
    + "</w:rPr></w:rPrDefault>"
    + '<w:pPrDefault><w:pPr><w:spacing w:after="80" '
    + 'w:line="300" w:lineRule="auto"/></w:pPr></w:pPrDefault>'
    + "</w:docDefaults>"
    + '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
    + '<w:name w:val="Normal"/><w:qFormat/></w:style>'
    + '<w:style w:type="paragraph" w:styleId="Heading1">'
    + '<w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:qFormat/>'
    + '<w:pPr><w:outlineLvl w:val="0"/></w:pPr>'
    + f'<w:rPr><w:b/><w:sz w:val="{SZ_H1}"/><w:szCs w:val="{SZ_H1}"/></w:rPr></w:style>'
    + '<w:style w:type="paragraph" w:styleId="Heading2">'
    + '<w:name w:val="heading 2"/><w:basedOn w:val="Normal"/><w:qFormat/>'
    + '<w:pPr><w:outlineLvl w:val="1"/></w:pPr>'
    + f'<w:rPr><w:b/><w:sz w:val="{SZ_H2}"/><w:szCs w:val="{SZ_H2}"/></w:rPr></w:style>'
    + "</w:styles>"
)

# 页脚：居中的「第 N 页」。页码必须写成 PAGE 域的三段式
# （begin / instrText / separate / end），只给一个 instrText 是不显示的。
_SMALL_RUN_PROPS = f'<w:rPr>{_BASE_FONTS}<w:sz w:val="{SZ_SMALL}"/></w:rPr>'


def _footer_run(text: str | None = None, *, field: str | None = None) -> str:
    if field is not None:
        return f'<w:r><w:instrText xml:space="preserve"> {field} </w:instrText></w:r>'
    return f"<w:r>{_SMALL_RUN_PROPS}<w:t xml:space=\"preserve\">{text}</w:t></w:r>"


def _fld_char(kind: str) -> str:
    return f'<w:r><w:fldChar w:fldCharType="{kind}"/></w:r>'


FOOTER = (
    XML_DECL
    + f'<w:ftr xmlns:w="{NS_W}">'
    + '<w:p><w:pPr><w:jc w:val="center"/></w:pPr>'
    + _footer_run("第 ")
    + _fld_char("begin")
    + _footer_run(field="PAGE")
    + _fld_char("separate")
    + f'<w:r>{_SMALL_RUN_PROPS}<w:t>1</w:t></w:r>'
    + _fld_char("end")
    + _footer_run(" 页")
    + "</w:p></w:ftr>"
)


def _document_xml(body: str) -> str:
    sect = (
        "<w:sectPr>"
        + '<w:footerReference w:type="default" r:id="rId2"/>'
        + f'<w:pgSz w:w="{PAGE_WIDTH}" w:h="{PAGE_HEIGHT}"/>'
        + f'<w:pgMar w:top="{MARGIN}" w:right="{MARGIN}" w:bottom="{MARGIN}" '
        + f'w:left="{MARGIN}" w:header="851" w:footer="992" w:gutter="0"/>'
        + "</w:sectPr>"
    )
    return (
        XML_DECL
        + f'<w:document xmlns:w="{NS_W}" xmlns:r="{NS_R}" xmlns:wp="{NS_WP}">'
        + f"<w:body>{body}{sect}</w:body></w:document>"
    )


def _core_xml(report: dict[str, Any]) -> str:
    stamp = report["generated_at"].astimezone().strftime("%Y-%m-%dT%H:%M:%SZ")
    title = escape(f"AI 代码导师学习报告（{report['student_id']}）")
    return (
        XML_DECL
        + '<cp:coreProperties xmlns:cp='
        + '"http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        + 'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        + 'xmlns:dcterms="http://purl.org/dc/terms/" '
        + 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        + f"<dc:title>{title}</dc:title>"
        + "<dc:creator>AI 代码导师系统</dc:creator>"
        + "<cp:lastModifiedBy>AI 代码导师系统</cp:lastModifiedBy>"
        + f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
        + f'<dcterms:modified xsi:type="dcterms:W3CDTF">{stamp}</dcterms:modified>'
        + "</cp:coreProperties>"
    )


def _app_xml() -> str:
    # 这些统计值 Word 打开时会自己重算，这里只给一个合法的初始值。
    return (
        XML_DECL
        + "<Properties xmlns="
        + '"http://schemas.openxmlformats.org/officeDocument/2006/extended-properties" '
        + 'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        + "<Application>AI 代码导师系统</Application>"
        + "<Company>TU0</Company>"
        + "<DocSecurity>0</DocSecurity><ScaleCrop>false</ScaleCrop>"
        + "<LinksUpToDate>false</LinksUpToDate><SharedDoc>false</SharedDoc>"
        + "<HyperlinksChanged>false</HyperlinksChanged>"
        + "<AppVersion>16.0000</AppVersion>"
        + "</Properties>"
    )


def render_learning_report(report: dict[str, Any]) -> bytes:
    """把 `ReportService.build()` 的结果渲染成 .docx 字节。"""
    body = "".join(
        [
            _para(
                _run("AI 代码导师 · 学习报告", size=SZ_TITLE, bold=True, color="1F3864"),
                align="center",
                after=60,
            ),
            _para(
                _run(
                    f"统计区间：{report['range_label']}　|　"
                    f"生成时间：{_fmt_date(report['generated_at'])}",
                    size=SZ_SUBTITLE,
                    color="595959",
                ),
                align="center",
                after=240,
            ),
            _section_basic(report),
            _section_ability(report),
            _section_usage(report),
            _section_errors(report),
            _section_suggestions(report),
            _para(
                _run(
                    "本报告由「AI 代码导师」系统自动生成，数据来自本机数据库，"
                    "供教师了解学生学习情况时参考。",
                    size=SZ_SMALL,
                    color="7F7F7F",
                ),
                align="center",
                before=240,
            ),
        ]
    )

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zout:
        zout.writestr("[Content_Types].xml", CONTENT_TYPES)
        zout.writestr("_rels/.rels", ROOT_RELS)
        zout.writestr("docProps/core.xml", _core_xml(report))
        zout.writestr("docProps/app.xml", _app_xml())
        zout.writestr("word/document.xml", _document_xml(body))
        zout.writestr("word/styles.xml", STYLES)
        zout.writestr("word/footer1.xml", FOOTER)
        zout.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS)
        # 雷达图。图片二进制直接写进包，不落磁盘临时文件。
        ability = report.get("ability") or {}
        zout.writestr(
            IMAGE_PART,
            render_radar_png(ability.get("dimensions") or [], ability.get("weakest", "")),
        )
    return buffer.getvalue()


__all__ = ["render_learning_report"]
