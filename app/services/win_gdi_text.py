"""把中文文字画进位图 —— 走 Windows GDI，**不引入 Pillow 之类的图像库**。

## 为什么需要它

导出的 Word 报告里有一张手绘的雷达图（`png_chart.py`）。那张图原本只有图形、
没有文字：标准库画不了字，手写 TrueType 光栅化又要带一份字库，不划算 ——
于是当时的做法是"图看形状、表读数值"，把维度名交给旁边的表格。

结果是**图本身读不懂**：六个角凹凹凸凸，但不知道哪个角是哪一项。
现在需要在六个轴上直接标出"健壮性 45"这样的文字。

## 为什么用 ctypes 调 GDI 而不是装 Pillow

Pillow 是个几 MB 的编译依赖，而本项目的卖点是"双击 start.bat 什么都不用装"
（运行期依赖一直压在 16 个）。Windows 自带的 GDI 本来就有完整的中文字体与
抗锯齿渲染，用 ctypes 调 `gdi32`/`user32` 就能拿到 —— ctypes 是标准库。

## 它是可选的

**画不了字绝不能影响报告生成**：这个模块在非 Windows、GDI 调用失败、
或者画完之后一个像素都没变（字体缺字）时一律返回 `None`，
调用方照旧输出没有文字的图。报告"少几个标签"可以接受，"生成不出来"不行。
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# 中文字体优先级：微软雅黑在 Win7 以上都有；后面两个是更老的兜底。
# CreateFontW 在字体不存在时不会失败，只会替换成别的字体，所以多给几个候选。
FONT_CANDIDATES = ("Microsoft YaHei", "SimHei", "SimSun")

# GDI 常量
_ANTIALIASED_QUALITY = 4
_DEFAULT_CHARSET = 1
_OUT_TT_PRECIS = 4
_CLIP_DEFAULT_PRECIS = 0
_DEFAULT_PITCH = 0
_FW_NORMAL = 400
_TRANSPARENT = 1
_DT_SINGLELINE = 0x00000020
_DT_CENTER = 0x00000001
_DT_RIGHT = 0x00000002
_DT_VCENTER = 0x00000004
_DT_NOCLIP = 0x00010000
_DT_CALCRECT = 0x00000400
_BI_RGB = 0


@dataclass(frozen=True)
class Label:
    """一段要画的文字。

    `x` / `y` 是**锚点**：`align` 决定文字相对锚点怎么摆
    （left = 锚点在左端，center = 锚点居中，right = 锚点在右端）。
    `size` 是字号（像素）。
    """

    text: str
    x: int
    y: int
    size: int
    align: str = "center"
    color: tuple[int, int, int] = (60, 70, 80)


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class _RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


def available() -> bool:
    """当前环境能不能用 GDI 画字（非 Windows 一律为 False）。"""
    return sys.platform == "win32"


def _load_libraries():
    """加载 gdi32 / user32 并声明用到的函数签名。

    签名必须显式声明：64 位下句柄是 8 字节，靠 ctypes 自己猜会把返回值截断，
    表现是"什么都没画出来"却不报错 —— 这类问题极难排查。
    """
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC

    gdi32.CreateDIBSection.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(_BITMAPINFO),
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
        wintypes.HANDLE,
        wintypes.DWORD,
    ]
    gdi32.CreateDIBSection.restype = wintypes.HBITMAP

    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ

    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL

    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL

    gdi32.CreateFontW.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPCWSTR,
    ]
    gdi32.CreateFontW.restype = wintypes.HFONT

    gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
    gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
    gdi32.SetTextColor.restype = wintypes.COLORREF

    user32.DrawTextW.argtypes = [
        wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
        ctypes.POINTER(_RECT), wintypes.UINT,
    ]
    user32.DrawTextW.restype = ctypes.c_int

    return gdi32, user32


def paint(
    width: int,
    height: int,
    rgb: bytes,
    labels: list[Label],
    *,
    font_family: str | None = None,
) -> bytes | None:
    """在 RGB 位图上画若干段文字，返回新的 RGB 字节；画不了时返回 None。

    `rgb` 是自上而下的 `width * height * 3` 字节。内部会转成 32 位 DIB
    （GDI 只认 32 位真彩顺手），画完再转回 24 位 RGB。

    返回 None 表示"没画成"：调用方应当继续用原来那张没有文字的图，
    而不是让整个报告生成失败。
    """
    if not available() or not labels or width <= 0 or height <= 0:
        return None
    if len(rgb) != width * height * 3:
        logger.warning("文字图层尺寸不对：期望 %s 字节", width * height * 3)
        return None

    try:
        gdi32, user32 = _load_libraries()
    except OSError:  # pragma: no cover - 只有非 Windows 会走到
        return None

    hdc = None
    bitmap = None
    try:
        hdc = gdi32.CreateCompatibleDC(None)
        if not hdc:
            return None

        info = _BITMAPINFO()
        header = info.bmiHeader
        header.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        header.biWidth = width
        # 负高度 = 自上而下，与我们的行序一致，省一次翻转
        header.biHeight = -height
        header.biPlanes = 1
        header.biBitCount = 32
        header.biCompression = _BI_RGB

        bits = ctypes.c_void_p()
        bitmap = gdi32.CreateDIBSection(
            hdc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0
        )
        if not bitmap or not bits:
            return None

        # 把自己画好的图形拷进 DIB，后面 GDI 会把文字叠在它上面。
        #
        # 通道顺序是这里最容易搞错的地方：GDI 的 32 位 DIB 是 **BGRA**
        # （字节序 B,G,R,A），而我们的画布是 RGB。两边必须显式换过来 ——
        # 第一版按 RGB 直接对应，结果 GDI 把红色写成了字节 0，我读回来当成 R，
        # 于是"最弱项标红"变成了紫蓝色（实测采样到 (74,74,201) 才发现）。
        size = width * height * 4
        buffer = ctypes.create_string_buffer(size)
        source = memoryview(rgb)
        target = memoryview(buffer).cast("B")
        target[0::4] = source[2::3]   # B
        target[1::4] = source[1::3]   # G
        target[2::4] = source[0::3]   # R
        target[3::4] = b"\xff" * (width * height)
        ctypes.memmove(bits, buffer, size)

        gdi32.SelectObject(hdc, bitmap)
        gdi32.SetBkMode(hdc, _TRANSPARENT)

        for label in labels:
            font = _create_font(gdi32, label.size, font_family)
            if not font:
                continue
            previous = gdi32.SelectObject(hdc, font)
            try:
                rectangle = _measure(user32, hdc, label)
                gdi32.SetTextColor(hdc, _colorref(label.color))
                user32.DrawTextW(
                    hdc, label.text, -1, ctypes.byref(rectangle),
                    _DT_SINGLELINE | _DT_VCENTER | _DT_NOCLIP,
                )
            finally:
                gdi32.SelectObject(hdc, previous)
                gdi32.DeleteObject(font)

        ctypes.memmove(buffer, bits, size)
        out = memoryview(buffer).cast("B")
        result = bytearray(width * height * 3)
        result[0::3] = out[2::4]   # R
        result[1::3] = out[1::4]   # G
        result[2::3] = out[0::4]   # B

        # 自检：一个像素都没变说明字体缺字或绘制失败，这时候宁可不要文字图层
        if bytes(result) == rgb:
            logger.info("GDI 没有改动任何像素，跳过文字图层")
            return None
        return bytes(result)
    except Exception:  # noqa: BLE001 - 画不出字不该让报告生成失败
        logger.warning("GDI 文字渲染失败，改用无文字的图", exc_info=True)
        return None
    finally:
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if hdc:
            gdi32.DeleteDC(hdc)


def _create_font(gdi32, size: int, family: str | None):
    """建一个字体对象。负高度 = 按字符高度算（而不是单元格高度）。"""
    for name in ((family,) if family else ()) + FONT_CANDIDATES:
        font = gdi32.CreateFontW(
            -abs(size), 0, 0, 0, _FW_NORMAL, 0, 0, 0,
            _DEFAULT_CHARSET, _OUT_TT_PRECIS, _CLIP_DEFAULT_PRECIS,
            _ANTIALIASED_QUALITY, _DEFAULT_PITCH, name,
        )
        if font:
            return font
    return None


def _measure(user32, hdc, label: Label) -> _RECT:
    """按对齐方式算出文字的绘制矩形。

    先用 DT_CALCRECT 量出文字实际占多大 —— 中文与数字宽度差很多，
    写死宽度会让"规范性 100"这种长标签压到别的东西上。
    """
    rectangle = _RECT(0, 0, 0, 0)
    user32.DrawTextW(
        hdc, label.text, -1, ctypes.byref(rectangle),
        _DT_SINGLELINE | _DT_CALCRECT,
    )
    text_width = rectangle.right - rectangle.left
    text_height = rectangle.bottom - rectangle.top

    if label.align == "left":
        left = label.x
    elif label.align == "right":
        left = label.x - text_width
    else:
        left = label.x - text_width // 2
    return _RECT(left, label.y - text_height // 2, left + text_width, label.y + text_height // 2)


def _colorref(color: tuple[int, int, int]) -> int:
    """RGB -> COLORREF（GDI 是 0x00BBGGRR，与常见顺序相反）。"""
    red, green, blue = color
    return (blue << 16) | (green << 8) | red


__all__ = ["FONT_CANDIDATES", "Label", "available", "paint"]
