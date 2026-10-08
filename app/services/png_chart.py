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

# 画布尺寸。宽高都取偶数、且行字节数是 4 的倍数，压缩后体积更小。
WIDTH = 620
HEIGHT = 520
SCALE = 2  # 超采样倍数：先按 2 倍画再缩一半，等于免费拿到抗锯齿

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
    """
    size = max(len(dimensions), 3)
    canvas = Canvas(WIDTH * SCALE, HEIGHT * SCALE)
    cx = (WIDTH / 2) * SCALE
    # 半径取画布短边的 0.36：再大顶点的圆点会贴边，再小图就显得空。
    # cy 略微上移，给底部留出与顶部相当的白边（六边形最下方是个尖角）。
    cy = (HEIGHT / 2) * SCALE - 4 * SCALE
    radius = min(WIDTH, HEIGHT) * 0.36 * SCALE

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

    return canvas.downscale(SCALE).to_png()


__all__ = ["HEIGHT", "WIDTH", "Canvas", "render_radar_png"]
