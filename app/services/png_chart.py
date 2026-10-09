"""把能力雷达图画成 PNG —— 纯标准库，**不依赖 Pillow / matplotlib**。

## 为什么要自己写 PNG 编码器

导出的 Word 报告里想放一张雷达图，就得往 docx 包里塞一张位图。
常规做法是 Pillow（`ImageDraw`）或 matplotlib，但两者都是几十 MB 的编译依赖，
而本项目的核心卖点是"双击 start.bat，什么都不用装"——运行期依赖一直压在 16 个。
PNG 的容器格式其实很薄：一个魔数 + 几个带 CRC 的块，像素数据用 zlib 压缩。
zlib 在标准库里，所以这件事完全可以自己做，代价是 100 行代码。

## 画法

不引图形库，所以自己实现两件最基础的事：

  · **直线**：Bresenham 整数算法（雷达图的网格、轴线、外框都是直线）；
  · **多边形填充**：扫描线 + 奇偶规则（数据多边形要半透明填充）。

半透明处理得很朴素：先把填充色按 alpha 混到白底上得到实色，
再直接写像素。雷达图的填充只压在白底和浅灰网格上，不需要真正的 alpha 合成。

## 关于字体

位图上要写"准确性 82"这样的中文标签，但**标准库画不了字**。
自己点阵化中文字形是不现实的（要带一份字库），所以这里只画图形，
文字交给 Word 用真正的文本排版（见 report_docx.py 里图旁边的能力明细表）。
这是刻意的分工：图负责"一眼看出形状"，表负责"精确读数"。
"""

from __future__ import annotations

import math
import struct
import zlib

from app.services import win_gdi_text

# 画布尺寸。宽高都取偶数、且行字节数是 4 的倍数，压缩后体积更小。
# 宽高比给足左右两边：六个轴标签里，左右两侧那两对是最占地方的。
WIDTH = 660
HEIGHT = 540
SCALE = 2  # 超采样倍数：先按 2 倍画再缩一半，等于免费拿到抗锯齿

# 轴标签的排布（单位是最终像素，绘制时会乘以 SCALE）
LABEL_RATIO = 1.25   # 标签中心到圆心的距离 / 半径
NAME_DY = 9          # 维度名相对标签中心上移多少
SCORE_DY = 11        # 分数相对标签中心下移多少
NAME_SIZE = 17
SCORE_SIZE = 15
LABEL_COLOR = (60, 70, 80)
SCORE_COLOR = (120, 132, 144)

# 雷达图本身的几何：半径占短边的比例、以及圆心相对画布中心的偏移。
# 抽成常量而不是散在函数里，是为了让**测试能用同一套坐标**去采样标签位置 ——
# 测试里手写一套估算坐标，改布局就会失效（第一版就是这么挂的）。
RADIUS_RATIO = 0.325
CENTER_DY = -4


def center() -> tuple[float, float]:
    """圆心（最终像素坐标）。"""
    return (WIDTH / 2, HEIGHT / 2 + CENTER_DY)


def base_radius() -> float:
    return min(WIDTH, HEIGHT) * RADIUS_RATIO


def point_at(index: int, count: int, ratio: float) -> tuple[float, float]:
    """第 index 根轴上、距圆心 `ratio` 倍半径处的坐标（最终像素）。"""
    angle = (math.pi * 2 * index) / count - math.pi / 2
    cx, cy = center()
    radius = base_radius() * ratio
    return (cx + math.cos(angle) * radius, cy + math.sin(angle) * radius)


def label_anchor(index: int, count: int) -> tuple[float, float]:
    """第 index 个轴标签的锚点（最终像素）—— 渲染与测试共用。"""
    return point_at(index, count, LABEL_RATIO)

# 颜色（R, G, B）
WHITE = (255, 255, 255)
GRID = (216, 224, 232)
AXIS = (222, 229, 236)
AREA_LINE = (47, 125, 143)
AREA_FILL = (47, 125, 143)
AREA_ALPHA = 0.20
WEAK = (201, 74, 74)
RING_LABEL = (150, 160, 170)


class Canvas:
    """一张 RGB 画布。像素按行存成 bytearray，最后整体交给 PNG 编码。"""

    def __init__(self, width: int, height: int, background: tuple[int, int, int] = WHITE):
        self.width = width
        self.height = height
        self.rows = [bytearray(bytes(background) * width) for _ in range(height)]

    def blend(self, x: int, y: int, color: tuple[int, int, int], alpha: float = 1.0) -> None:
        """把一个像素按 alpha 混到画布上（越界直接丢弃）。"""
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        if alpha >= 1.0:
            row = self.rows[y]
            row[x * 3] = color[0]
            row[x * 3 + 1] = color[1]
            row[x * 3 + 2] = color[2]
            return
        if alpha <= 0.0:
            return
        row = self.rows[y]
        for channel in range(3):
            index = x * 3 + channel
            row[index] = int(row[index] * (1 - alpha) + color[channel] * alpha + 0.5)

    def line(
        self,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        color: tuple[int, int, int],
        width: int = 1,
        alpha: float = 1.0,
    ) -> None:
        """Bresenham 直线。`width` 是线宽（以像素为单位的方形笔头）。"""
        x0, y0, x1, y1 = int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))
        dx = abs(x1 - x0)
        dy = -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        half = width // 2
        while True:
            for ox in range(-half, width - half):
                for oy in range(-half, width - half):
                    self.blend(x0 + ox, y0 + oy, color, alpha)
            if x0 == x1 and y0 == y1:
                break
            err2 = 2 * err
            if err2 >= dy:
                err += dy
                x0 += sx
            if err2 <= dx:
                err += dx
                y0 += sy

    def polygon(
        self,
        points: list[tuple[float, float]],
        *,
        fill: tuple[int, int, int] | None = None,
        alpha: float = 1.0,
        outline: tuple[int, int, int] | None = None,
        outline_width: int = 1,
    ) -> None:
        """扫描线填充 + 可选描边。用奇偶规则，对凸多边形足够。"""
        if len(points) < 3:
            return
        if fill is not None:
            top = max(0, int(min(p[1] for p in points)))
            bottom = min(self.height - 1, int(max(p[1] for p in points)) + 1)
            for y in range(top, bottom + 1):
                scan = y + 0.5
                crossings: list[float] = []
                for index in range(len(points)):
                    x0, y0 = points[index]
                    x1, y1 = points[(index + 1) % len(points)]
                    if y0 == y1:
                        continue
                    lo, hi = (y0, y1) if y0 < y1 else (y1, y0)
                    if lo <= scan < hi:
                        t = (scan - y0) / (y1 - y0)
                        crossings.append(x0 + t * (x1 - x0))
                crossings.sort()
                for index in range(0, len(crossings) - 1, 2):
                    start = int(math.ceil(crossings[index] - 0.5))
                    end = int(crossings[index + 1] - 0.5)
                    for x in range(start, end + 1):
                        self.blend(x, y, fill, alpha)
        if outline is not None:
            for index in range(len(points)):
                x0, y0 = points[index]
                x1, y1 = points[(index + 1) % len(points)]
                self.line(x0, y0, x1, y1, outline, outline_width)

    def circle(
        self, cx: float, cy: float, radius: float, color: tuple[int, int, int]
    ) -> None:
        """实心圆。半径很小，逐像素判断距离就够了。"""
        r = int(math.ceil(radius))
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if dx * dx + dy * dy <= radius * radius:
                    self.blend(int(cx) + dx, int(cy) + dy, color)

    def downscale(self, factor: int) -> Canvas:
        """整数倍缩小（盒式滤波）。等于超采样抗锯齿。"""
        if factor <= 1:
            return self
        width = self.width // factor
        height = self.height // factor
        out = Canvas(width, height)
        area = factor * factor
        for y in range(height):
            out_row = out.rows[y]
            for x in range(width):
                total = [0, 0, 0]
                for sy in range(factor):
                    src = self.rows[y * factor + sy]
                    base = x * factor * 3
                    for sx in range(factor):
                        index = base + sx * 3
                        total[0] += src[index]
                        total[1] += src[index + 1]
                        total[2] += src[index + 2]
                out_row[x * 3] = total[0] // area
                out_row[x * 3 + 1] = total[1] // area
                out_row[x * 3 + 2] = total[2] // area
        return out

    def to_png(self) -> bytes:
        """编码成 PNG（8 位真彩、无滤波）。"""
        raw = bytearray()
        for row in self.rows:
            raw.append(0)  # 每行的滤波器类型：0 = None
            raw.extend(row)
        return _png(self.width, self.height, bytes(raw))

    def to_rgb(self) -> bytes:
        """导出成一整块 `width * height * 3` 的 RGB 字节（交给文字图层用）。"""
        return b"".join(bytes(row) for row in self.rows)

    def load_rgb(self, payload: bytes) -> None:
        """把一整块 RGB 字节装回画布（文字图层画完之后调）。"""
        expected = self.width * self.height * 3
        if len(payload) != expected:
            raise ValueError(f"像素数据长度不对：期望 {expected}，实际 {len(payload)}")
        stride = self.width * 3
        for y in range(self.height):
            self.rows[y][:] = payload[y * stride : (y + 1) * stride]


def _png(width: int, height: int, raw: bytes) -> bytes:
    """最小 PNG 容器：魔数 + IHDR + IDAT + IEND，每块都带 CRC32。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    # IHDR: 宽 高 位深8 颜色类型2(RGB) 压缩0 滤波0 隔行0
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def render_radar_png(dimensions: list[dict], weakest: str = "") -> bytes:
    """画六维雷达图，返回 PNG 字节。

    `dimensions` 就是 `AbilityService.build()` 里那一串，每项要有
    `label` 与 `score`。空列表时返回一张只带外框和提示的空图 ——
    Word 里宁可放一张"还没有数据"的图，也不要一个破图或者干脆缺图。

    **六个轴上都标出「维度名 + 分数」**（`win_gdi_text` 用 Windows GDI 画中文）。
    没有文字的雷达图是读不懂的：六个角凹凹凸凸，但不知道哪个角是哪一项，
    还得来回对照旁边的表格。画不了字时自动降级成没有文字的图（见 win_gdi_text）。
    """
    size = max(len(dimensions), 3)
    canvas = Canvas(WIDTH * SCALE, HEIGHT * SCALE)
    cx, cy = center()
    cx *= SCALE
    cy *= SCALE
    # 半径取短边的 0.325：剩下的地方要留给轴标签。
    # 原来是 0.36，加上文字之后左右两侧的标签会被裁掉（实测）。
    radius = base_radius() * SCALE

    def at(index: int, ratio: float) -> tuple[float, float]:
        angle = (math.pi * 2 * index) / size - math.pi / 2
        return (cx + math.cos(angle) * radius * ratio, cy + math.sin(angle) * radius * ratio)

    if not dimensions:
        canvas.polygon(
            [at(i, 1) for i in range(size)],
            outline=GRID,
            outline_width=2 * SCALE,
        )
        return canvas.downscale(SCALE).to_png()

    # ---- 网格：4 圈 ----
    for ring in range(4, 0, -1):
        canvas.polygon(
            [at(i, ring / 4) for i in range(size)],
            outline=GRID,
            outline_width=1 * SCALE,
        )

    # ---- 轴线 ----
    for index in range(size):
        x, y = at(index, 1)
        canvas.line(cx, cy, x, y, AXIS, width=1 * SCALE)

    # ---- 数据多边形 ----
    area = [
        at(index, max(0.0, min(1.0, item.get("score", 0) / 100)))
        for index, item in enumerate(dimensions)
    ]
    canvas.polygon(
        area, fill=AREA_FILL, alpha=AREA_ALPHA, outline=AREA_LINE, outline_width=2 * SCALE
    )

    # ---- 顶点 ----
    for index, item in enumerate(dimensions):
        x, y = area[index]
        is_weak = item.get("key") == weakest
        canvas.circle(x, y, (5 if is_weak else 3.4) * SCALE, WEAK if is_weak else AREA_LINE)

    # ---- 六轴标签：维度名 + 分数 ----
    _draw_axis_labels(canvas, dimensions, at, weakest)

    return canvas.downscale(SCALE).to_png()


def _draw_axis_labels(canvas: Canvas, dimensions: list[dict], at, weakest: str) -> None:
    """在六个轴的外侧写「维度名 / 分数」两行。

    对齐方式按方位决定（右侧的靠左排、左侧的靠右排），这样文字始终朝图形
    **外面**长，不会压到雷达图上。左右两侧的标签因此也不会戳出画布 ——
    半径之所以从 0.36 收到 0.325，就是给这两行文字让位。
    """
    if not win_gdi_text.available() or canvas.width // SCALE != WIDTH:
        return

    labels: list[win_gdi_text.Label] = []
    for index, item in enumerate(dimensions):
        angle = (math.pi * 2 * index) / len(dimensions) - math.pi / 2
        anchor_x, anchor_y = at(index, LABEL_RATIO)
        cos = math.cos(angle)
        align = "left" if cos > 0.3 else ("right" if cos < -0.3 else "center")
        is_weak = item.get("key") == weakest

        labels.append(
            win_gdi_text.Label(
                text=str(item.get("label", "")),
                x=int(anchor_x),
                y=int(anchor_y - NAME_DY * SCALE),
                size=NAME_SIZE * SCALE,
                align=align,
                color=WEAK if is_weak else LABEL_COLOR,
            )
        )
        labels.append(
            win_gdi_text.Label(
                text=str(item.get("score", "")),
                x=int(anchor_x),
                y=int(anchor_y + SCORE_DY * SCALE),
                size=SCORE_SIZE * SCALE,
                align=align,
                color=WEAK if is_weak else SCORE_COLOR,
            )
        )

    painted = win_gdi_text.paint(canvas.width, canvas.height, canvas.to_rgb(), labels)
    if painted is not None:
        canvas.load_rgb(painted)


__all__ = [
    "HEIGHT",
    "LABEL_RATIO",
    "WIDTH",
    "Canvas",
    "base_radius",
    "center",
    "label_anchor",
    "point_at",
    "render_radar_png",
]
