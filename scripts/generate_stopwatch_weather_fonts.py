#!/usr/bin/env python3
"""Generate StopWatch's small, offline LVGL 9 weather fonts from the Noto CBINs.

No font converter, third-party Python package, or network access is required.
Run from any directory with Python 3.10+; --check verifies the committed output.
The extracted font data is covered by stopwatch_weather_fonts.LICENSE.txt beside
that output. The 16px subset includes printable ASCII; 20px/30px use the basic
fallback for characters not explicitly extracted, including punctuation.
"""

from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import struct
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FONT_COMPONENT = ROOT / "managed_components/78__xiaozhi-fonts"
OUTPUT = ROOT / "main/boards/m5stack/stopwatch/stopwatch_weather_fonts.c"
LICENSE = OUTPUT.with_suffix(".LICENSE.txt")

# Controlled offline vocabulary shared by Weather and Settings. Arbitrary city
# names use the service's deterministic ASCII fallback, not transient chat glyphs.
CONDITIONS = "晴晴夜多云雾雨雪雷雨未知"
CITIES = "北京上海广州深圳杭州成都重庆武汉南京西安香港台北"
SETTINGS_TEXT = "天气位置自动手动城市搜索确认取消返回选择最近常用更新配置已变化失败离线定位中加载忙重试保存成功"
TEXT_16 = "更新" + "".join(chr(cp) for cp in range(0x20, 0x7F))
TEXT_20 = (
    CONDITIONS + CITIES + SETTINGS_TEXT +
    "体感最高最低湿度风速逐时三日预报低温高温历史年龄未知已更新未加载"
    "星期一二四五六今天明天后天未来小时暂无降雨概率"
)
# Digits are extracted too: wide/negative magnitudes cannot safely use the
# shared Commissioner108 font, which has no minus glyph or fallback.
TEXT_30 = "逐时预报三日预报0123456789"
FALLBACK_TEXT = "".join(chr(cp) for cp in range(0x20, 0x7F)) + "°·，"


@dataclass(frozen=True)
class Glyph:
    codepoint: int
    advance: int  # CBIN and fmt_txt both store 1/16-pixel advances.
    width: int
    height: int
    offset_x: int
    offset_y: int
    bitmap: bytes


class CbinFont:
    """Read the component's legacy 32-bit CBIN layout, not host C structs.

    Layout matches managed_components/78__xiaozhi-fonts/scripts/glyph_provider.py.
    Unlike loading the whole common font at runtime, only selected glyph data is
    copied to the generated C file. Unsupported formats and missing data fail.
    """

    def __init__(self, path: Path):
        self.path = path
        self.data = path.read_bytes()
        self.line_height, self.base_line = self.unpack("<ii", 12)
        self.underline_position, self.underline_thickness = self.unpack("<bb", 21)
        (descriptor,) = self.unpack("<I", 24)
        bitmap, glyphs, cmaps = self.unpack("<III", descriptor)
        (flags,) = self.unpack("<H", descriptor + 18)
        (stride,) = self.unpack("<B", descriptor + 20)
        cmap_count = flags & 0x1FF
        bpp = (flags >> 9) & 0xF
        bitmap_format = (flags >> 14) & 3
        if bpp != 4 or bitmap_format != 0 or stride != 0:
            raise ValueError(
                f"{path}: expected plain, tightly packed 4bpp; "
                f"got bpp={bpp}, bitmap_format={bitmap_format}, stride={stride}"
            )
        if not 0 <= self.base_line < self.line_height:
            raise ValueError(f"{path}: invalid line metrics")
        self.bitmap_offset = descriptor + bitmap
        self.glyph_offset = descriptor + glyphs
        cmap_offset = descriptor + cmaps
        self.cmaps = []
        for index in range(cmap_count):
            start, length, first, unicode_list, glyph_list, count, kind = self.unpack(
                "<IHHIIHBx", cmap_offset + index * 20
            )
            if kind not in (0, 1, 2, 3):
                raise ValueError(f"{path}: unsupported cmap type {kind}")
            unicode_values = (
                self.unpack(f"<{count}H", cmap_offset + unicode_list)
                if kind in (1, 3)
                else ()
            )
            if tuple(sorted(set(unicode_values))) != unicode_values:
                raise ValueError(f"{path}: sparse cmap is not strictly sorted")
            offsets = ()
            if kind in (0, 1):
                offset_count, element = (length, "B") if kind == 0 else (count, "H")
                if not glyph_list:
                    raise ValueError(f"{path}: full cmap has no glyph-offset list")
                offsets = self.unpack(f"<{offset_count}{element}", cmap_offset + glyph_list)
            self.cmaps.append((start, length, first, kind, unicode_values, offsets))

    def unpack(self, fmt: str, offset: int) -> tuple:
        if offset < 0 or offset + struct.calcsize(fmt) > len(self.data):
            raise ValueError(f"{self.path}: truncated CBIN at byte {offset}")
        return struct.unpack_from(fmt, self.data, offset)

    def glyph_id(self, codepoint: int) -> int:
        for start, length, first, kind, unicode_values, offsets in self.cmaps:
            relative = codepoint - start
            if not 0 <= relative < length:
                continue
            if kind == 2:  # FORMAT0_TINY
                return first + relative
            if kind == 0:  # FORMAT0_FULL; zero beyond the first entry means absent.
                if offsets[relative] == 0 and relative != 0:
                    continue
                return first + offsets[relative]
            index = bisect.bisect_left(unicode_values, relative)
            if index < len(unicode_values) and unicode_values[index] == relative:
                return first + (index if kind == 3 else offsets[index])
        return 0

    def glyph(self, codepoint: int) -> Glyph:
        glyph_id = self.glyph_id(codepoint)
        if glyph_id == 0:
            raise ValueError(f"{self.path}: missing U+{codepoint:04X} {chr(codepoint)!r}")
        bitmap, advance, width, height, offset_x, offset_y = self.unpack(
            "<IIHHhh", self.glyph_offset + glyph_id * 16
        )
        # Keep the generated font compatible with both compact and LARGE fmt_txt.
        if advance > 0xFFF or max(width, height) > 0xFF:
            raise ValueError(f"{self.path}: glyph U+{codepoint:04X} exceeds compact metrics")
        if not (-128 <= offset_x <= 127 and -128 <= offset_y <= 127):
            raise ValueError(f"{self.path}: glyph U+{codepoint:04X} exceeds compact offsets")
        start = self.bitmap_offset + bitmap
        size = (width * height * 4 + 7) // 8
        if start < 0 or start + size > len(self.data):
            raise ValueError(f"{self.path}: truncated bitmap for U+{codepoint:04X}")
        return Glyph(codepoint, advance, width, height, offset_x, offset_y,
                     self.data[start:start + size])


def generate_font(size: int, font: CbinFont, text: str) -> list[str]:
    codepoints = sorted(set(map(ord, text)))
    if not codepoints or codepoints[-1] - codepoints[0] >= 0xFFFF:
        raise ValueError("weather charset must fit one sparse 16-bit cmap")
    glyphs = [font.glyph(cp) for cp in codepoints]
    prefix = f"weather_{size}"
    lines = [
        f"/* {size}px: {len(glyphs)} glyphs; all advances and offsets preserved from CBIN. */",
        f"static LV_ATTRIBUTE_LARGE_CONST const uint8_t {prefix}_bitmap[] = {{",
        "    // clang-format off",
    ]
    for glyph in glyphs:
        lines.append(f"    /* U+{glyph.codepoint:04X} {chr(glyph.codepoint)} */")
        for start in range(0, len(glyph.bitmap), 12):
            lines.append("    " + ", ".join(f"0x{byte:02x}" for byte in glyph.bitmap[start:start + 12]) + ",")
    lines.extend([
        "    // clang-format on",
        "};",
        "",
        f"static const lv_font_fmt_txt_glyph_dsc_t {prefix}_glyphs[] = {{",
        "    {0}, /* Glyph ID zero is reserved. */",
    ])
    bitmap_index = 0
    for glyph in glyphs:
        lines.extend([
            "    {",
            f"        .bitmap_index = {bitmap_index},",
            f"        .adv_w = {glyph.advance},",
            f"        .box_w = {glyph.width},",
            f"        .box_h = {glyph.height},",
            f"        .ofs_x = {glyph.offset_x},",
            f"        .ofs_y = {glyph.offset_y},",
            f"    }}, /* U+{glyph.codepoint:04X} {chr(glyph.codepoint)} */",
        ])
        bitmap_index += len(glyph.bitmap)
    if bitmap_index > 0xFFFFF:
        raise ValueError("weather font bitmap exceeds compact fmt_txt capacity")
    lines.extend([
        "};",
        "",
        f"static const uint16_t {prefix}_unicode[] = {{",
        "    // clang-format off",
    ])
    for start in range(0, len(codepoints), 10):
        lines.append("    " + ", ".join(f"0x{cp - codepoints[0]:04x}" for cp in codepoints[start:start + 10]) + ",")
    lines.extend([
        "    // clang-format on",
        "};",
        "",
        f"static const lv_font_fmt_txt_cmap_t {prefix}_cmaps[] = {{{{",
        f"    .range_start = 0x{codepoints[0]:04x},",
        f"    .range_length = {codepoints[-1] - codepoints[0] + 1},",
        "    .glyph_id_start = 1,",
        f"    .unicode_list = {prefix}_unicode,",
        "    .glyph_id_ofs_list = NULL,",
        f"    .list_length = {len(codepoints)},",
        "    .type = LV_FONT_FMT_TXT_CMAP_SPARSE_TINY,",
        "}};",
        "",
        f"static const lv_font_fmt_txt_dsc_t {prefix}_dsc = {{",
        f"    .glyph_bitmap = {prefix}_bitmap,",
        f"    .glyph_dsc = {prefix}_glyphs,",
        f"    .cmaps = {prefix}_cmaps,",
        "    .kern_dsc = NULL,",
        "    .kern_scale = 0,",
        "    .cmap_num = 1,",
        "    .bpp = 4,",
        "    .kern_classes = 0,",
        "    .bitmap_format = LV_FONT_FMT_TXT_PLAIN,",
        "    .stride = 0,",
        "};",
        "",
        f"const lv_font_t font_stopwatch_weather_{size} = {{",
        "    .get_glyph_dsc = lv_font_get_glyph_dsc_fmt_txt,",
        "    .get_glyph_bitmap = lv_font_get_bitmap_fmt_txt,",
        f"    .line_height = {font.line_height},",
        f"    .base_line = {font.base_line},",
        "    .subpx = LV_FONT_SUBPX_NONE,",
        "    .kerning = LV_FONT_KERNING_NONE,",
        "    .static_bitmap = 1,",
        f"    .underline_position = {font.underline_position},",
        f"    .underline_thickness = {font.underline_thickness},",
        f"    .dsc = &{prefix}_dsc,",
        ("    .fallback = NULL," if size == 16
         else f"    .fallback = &font_noto_sans_basic_{size}_4,"),
        "};",
        "",
    ])
    return lines


def generate(component: Path) -> str:
    if not LICENSE.is_file():
        raise ValueError(f"font redistribution requires {LICENSE}")
    # All basic profiles share this charset. Verify punctuation and ASCII rather
    # than accidentally depending on a server-pushed glyph for fixed demo copy.
    basic = json.loads((component / "charsets/basic.json").read_text(encoding="utf-8"))
    missing = sorted(set(map(ord, FALLBACK_TEXT)) - set(basic["codepoints"]))
    if missing:
        raise ValueError("basic fallback is missing " + ", ".join(f"U+{cp:04X}" for cp in missing))
    lines = [
        "/*",
        " * Generated by scripts/generate_stopwatch_weather_fonts.py; do not edit by hand.",
        " * Offline subsets of Noto Sans SC, from 78/xiaozhi-fonts common CBINs.",
        " * Copyright 2014-2021 Adobe (http://www.adobe.com/),",
        " * with Reserved Font Name 'Source'. Licensed under SIL OFL 1.1.",
        " * See stopwatch_weather_fonts.LICENSE.txt for provenance and full terms.",
    ]
    fonts = []
    for size, text in ((16, TEXT_16), (20, TEXT_20), (30, TEXT_30)):
        path = component / f"cbin/font_noto_sans_common_{size}_4.bin"
        font = CbinFont(path)
        fonts.append((size, font, text))
        lines.append(f" * {path.name} SHA-256:")
        lines.append(f" * {hashlib.sha256(font.data).hexdigest()}")
    lines.extend([
        " */",
        "",
        '#include "sdkconfig.h"',
        "",
        "#if CONFIG_STOPWATCH_LOCAL_APPS",
        "",
        '#include "lvgl.h"',
        "",
        "LV_FONT_DECLARE(font_noto_sans_basic_20_4)",
        "LV_FONT_DECLARE(font_noto_sans_basic_30_4)",
        "",
    ])
    for size, font, text in fonts:
        lines.extend(generate_font(size, font, text))
    lines.append("#endif  // CONFIG_STOPWATCH_LOCAL_APPS")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--font-component", type=Path, default=FONT_COMPONENT,
                        help="existing 78/xiaozhi-fonts component directory")
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--check", action="store_true", help="check output without writing it")
    args = parser.parse_args()
    try:
        generated = generate(args.font_component)
        if args.check:
            if not args.output.is_file() or args.output.read_bytes() != generated.encode("utf-8"):
                parser.exit(1, f"{args.output} is stale; rerun {Path(__file__).name}\n")
            print("StopWatch weather fonts are reproducible and all fixed glyphs are covered.")
        else:
            args.output.write_bytes(generated.encode("utf-8"))
            print(f"Generated {args.output}")
    except (OSError, ValueError, KeyError, struct.error) as error:
        parser.exit(1, f"{error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
