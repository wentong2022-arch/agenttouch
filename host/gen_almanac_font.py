#!/usr/bin/env python3
# Copyright (c) 2026 yuwentong
# Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
"""Bakes anti-aliased CJK glyphs for the cyber-almanac page into
firmware/src/almanac_font.h (4-bit alpha bitmaps, blended on-device).

Why: the 314-PPI AMOLED made the 11 px cubic11 bitmap font look "low-res"
(user verdict 2026-08-29). Instead we rasterize real macOS fonts offline:
  - Songti SC Bold  22 px  -> header charset (sexagenary/zodiac/建除)
  - Songti SC Bold  34 px  -> the 宜 / 忌 seal characters
  - Hiragino Sans GB W6 22 px -> body charset (wordbank + ASCII + punctuation)

The body charset is parsed straight from agentpet_host.py's almanac section
plus ~/.agentpet/almanac.json (the hot-override wordbank), so RERUN THIS
after adding words, then rebuild the firmware:
  /usr/bin/python3 host/gen_almanac_font.py && cd firmware && pio run -t upload

SD mode (2026-09-03, 基A/基B): bake the FULL GB2312 set into binary .afn
files and push them onto the card — the board falls back to the card for any
glyph the flash tables lack, so adding words no longer needs a re-bake:
  /usr/bin/python3 host/gen_almanac_font.py --sd /tmp/afn
  curl "http://127.0.0.1:8788/sd/push?src=/tmp/afn/body26.afn&dst=/agentpet/fonts/body26.afn"
.afn layout: "AFN1", u16 ascent, u32 count, count x {u32 cp, u32 off} sorted
by cp, then the glyph blob (same record format as the flash tables).
"""
import struct
import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
HOST = HERE / "agentpet_host.py"
OUT = HERE.parent / "firmware" / "src" / "almanac_font.h"
OVERRIDE = Path.home() / ".agentpet" / "almanac.json"

def system_font(*paths):
    """First of these that exists. Songti lives in Supplemental/ on current
    macOS; the bare path only worked because Pillow searches the font dirs
    for a missing file's name."""
    return next((p for p in paths if Path(p).exists()), paths[0])


SONGTI = system_font("/System/Library/Fonts/Supplemental/Songti.ttc",
                     "/System/Library/Fonts/Songti.ttc")
HIRAGINO = system_font("/System/Library/Fonts/Hiragino Sans GB.ttc")
MENLO = system_font("/System/Library/Fonts/Menlo.ttc")
# Fixed strings the play pages draw in the 18 px face (baked into flash so the
# pages are legible even before the card font is in).
PLAY_UI_CHARS = ("正在播放播客第集共上没有的还卡节目里点什么就会出现这在设置页听贴一个或文件"
                 "网易云音乐浏览器苹果视频哔哩暂停继续下一曲上一曲秒剩余 ·Mac"
                 "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-:/.,()…")
ONLY = set()   # --sd <dir> --only tiny18 : bake just the named faces


def find_face(path, want_family, want_styles):
    found = {}
    for idx in range(24):
        try:
            f = ImageFont.truetype(path, 22, index=idx)
        except OSError:
            break
        fam, sty = f.getname()
        if want_family in fam and sty in want_styles:
            found.setdefault(sty, (idx, f"{fam} {sty}"))
    for sty in want_styles:                 # preference order, not file order
        if sty in found:
            return found[sty]
    raise SystemExit(f"no face like {want_family}/{want_styles} in {path}")


def collect_charsets():
    src = HOST.read_text()
    sec = src[src.index("cyber almanac"):src.index("stretch reminder")]
    body = set()
    for lit in re.findall(r'"([^"]*)"', sec):
        body.update(lit)
    if OVERRIDE.exists():
        try:
            ov = json.loads(OVERRIDE.read_text())
            for key in ("yi", "ji", "qian"):
                for s in ov.get(key) or []:
                    body.update(s)
        except Exception as e:
            print("override skipped:", e, file=sys.stderr)
    body.update(chr(c) for c in range(0x20, 0x7F))          # ASCII
    body.update("「」·。，、！？；：…～—％")
    body.update("黄历同步中信号发送批准长按拒绝今日战报皮肤亮度")  # pages/face
    body.update("请在上")                                  # 「请在 Mac 上批准」 toast
    body.update("连到这台接已还没有主人现在跟着")          # claim card
    body = sorted(c for c in body if ord(c) >= 0x20)
    header = sorted(set("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥"
                        "鼠牛虎兔龙蛇马羊猴鸡狗猪建除满平定执破危成收开闭"
                        "属日 ·0123456789."
                        # clock cockpit page: 星期X + 十二时辰及别称
                        "星期一二三四五六时"
                        "夜半鸣平旦出食隅中昳晡入黄昏人"
                        # English clock page: weekday header
                        # + classical time-of-day names, all caps
                        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"))
    return header, body


def rasterize(font, px, chars):
    """-> (ascent, [(cp, w, h, adv, dx, dy_top, rows4bpp)])"""
    f = ImageFont.truetype(font[0], px, index=font[1])
    ascent, _ = f.getmetrics()
    out = []
    pad = px

    def render(ch):
        img = Image.new("L", (px * 3, px * 3), 0)
        ImageDraw.Draw(img).text((pad, pad), ch, font=f, fill=255)
        return img
    # what this face draws for a codepoint it lacks (U+0378 is unassigned):
    # any glyph that renders identically is absent and is left out, so the
    # board draws its own box instead of carrying thousands of .notdef copies
    notdef = render("\u0378")
    notdef_bytes = notdef.crop(notdef.getbbox()).tobytes() if notdef.getbbox() else None
    skipped = 0
    for ch in chars:
        img = render(ch)
        adv = max(1, round(f.getlength(ch)))
        bbox = img.getbbox()
        if bbox is None:                       # space etc.
            out.append((ord(ch), 0, 0, adv, 0, 0, b""))
            continue
        x0, y0, x1, y1 = bbox
        g = img.crop(bbox)
        if notdef_bytes is not None and g.tobytes() == notdef_bytes:
            skipped += 1
            continue
        w, h = g.size
        data = bytearray()
        pix = g.load()
        for y in range(h):
            for x in range(0, w, 2):
                a = pix[x, y] >> 4
                b = (pix[x + 1, y] >> 4) if x + 1 < w else 0
                data.append((a << 4) | b)
        out.append((ord(ch), w, h, adv, x0 - pad, y0 - pad, bytes(data)))
    if skipped:
        print(f"  ({skipped} codepoints not in this font, left out)")
    return ascent, out


def gb2312_chars():
    """Every GB2312 character (6763 hanzi + the symbol rows) as str."""
    out = set()
    for hi in range(0xA1, 0xF8):
        for lo in range(0xA1, 0xFF):
            try:
                out.add(bytes([hi, lo]).decode("gb2312"))
            except UnicodeDecodeError:
                pass
    return out


def wide_chars():
    """Beyond GB2312, for the faces that print whatever the Mac is playing
    (body26 = title, tiny18 = artist/album line): the whole CJK Unified
    block (GBK, Big5 and Japanese kanji all live there), kana, CJK
    punctuation, fullwidth forms, Latin-1 and general punctuation.
    2026-09-09: 蒨 in 叶蒨文 (GBK only) came out as a box."""
    out = set()
    for a, b in ((0x4E00, 0x9FFF), (0x3040, 0x30FF), (0x3000, 0x303F),
                 (0xFF00, 0xFFEF), (0x00A0, 0x00FF), (0x2010, 0x2027),
                 (0x2030, 0x205E), (0x2100, 0x214F), (0x2190, 0x21FF),
                 (0x2460, 0x24FF), (0x25A0, 0x27BF)):
        out.update(chr(c) for c in range(a, b + 1))
    return out


def emit_afn(path, ascent, glyphs):
    """Binary font file for the card (see module docstring)."""
    glyphs = sorted(glyphs, key=lambda g: g[0])
    table, blob = bytearray(), bytearray()
    for cp, w, h, adv, dx, dy, data in glyphs:
        table += struct.pack("<II", cp, len(blob))
        blob += bytes([w, h, adv & 0xFF, dx & 0xFF, dy & 0xFF]) + data
    with open(path, "wb") as fh:
        fh.write(b"AFN1" + struct.pack("<HI", ascent, len(glyphs)) + table + blob)
    return len(blob)


def main_sd(outdir):
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    st_idx, st_name = find_face(SONGTI, "Songti SC", ("Bold", "Black", "Regular"))
    hi_idx, hi_name = find_face(HIRAGINO, "Hiragino Sans GB", ("W6", "W3"))
    header_cs, body_cs = collect_charsets()
    full = gb2312_chars() | set(header_cs) | set(body_cs)
    full = sorted(c for c in full if ord(c) >= 0x20)
    wide = sorted(set(full) | wide_chars())
    print(f"SD charset: {len(full)} glyphs (almanac faces), {len(wide)} (media faces)")
    for name, font, idx, px in (("head26", SONGTI, st_idx, 26),
                                ("body26", HIRAGINO, hi_idx, 26),
                                ("quot24", HIRAGINO, hi_idx, 24),
                                ("tiny18", HIRAGINO, hi_idx, 18)):
        if ONLY and name not in ONLY:
            continue
        cs = wide if name in ("body26", "tiny18") else full
        a, g = rasterize((font, idx), px, cs)
        out = outdir / f"{name}.afn"
        n = emit_afn(out, a, g)
        print(f"wrote {out} ({out.stat().st_size // 1024} KB, glyph data {n // 1024} KB)")


def emit(name, ascent, glyphs, fh):
    cps, offs, blob = [], [], bytearray()
    for cp, w, h, adv, dx, dy, data in glyphs:
        cps.append(cp)
        offs.append(len(blob))
        blob += bytes([w, h, adv & 0xFF, dx & 0xFF, dy & 0xFF]) + data
    fh.write(f"#define {name}_ASCENT {ascent}\n")
    fh.write(f"#define {name}_COUNT {len(cps)}\n")
    fh.write(f"static const uint32_t {name}_CP[] = {{" +
             ",".join(str(c) for c in cps) + "};\n")
    fh.write(f"static const uint32_t {name}_OFF[] = {{" +
             ",".join(str(o) for o in offs) + "};\n")
    fh.write(f"static const uint8_t {name}_DATA[] = {{" +
             ",".join(str(b) for b in blob) + "};\n\n")
    return len(blob)


def main():
    st_idx, st_name = find_face(SONGTI, "Songti SC", ("Bold", "Black", "Regular"))
    hi_idx, hi_name = find_face(HIRAGINO, "Hiragino Sans GB", ("W6", "W3"))
    header_cs, body_cs = collect_charsets()
    print(f"header {st_name}: {len(header_cs)} glyphs; "
          f"body {hi_name}: {len(body_cs)} glyphs")
    with open(OUT, "w") as fh:
        fh.write("// AUTO-GENERATED by host/gen_almanac_font.py — do not edit.\n"
                 f"// header/seals: {st_name}; body: {hi_name}. 4bpp alpha,\n"
                 "// glyph record at OFF: w,h,adv,int8 dx,int8 dyTop, then\n"
                 "// ceil(w/2)*h bytes. Include from pages.cpp ONLY.\n"
                 "#pragma once\n#include <stdint.h>\n\n")
        total = 0
        a, g = rasterize((SONGTI, st_idx), 26, header_cs)
        total += emit("AFH", a, g, fh)                    # header, Songti 26
        a, g = rasterize((SONGTI, st_idx), 40, ["宜", "忌"])
        total += emit("AFS", a, g, fh)                    # seals, Songti 40
        a, g = rasterize((HIRAGINO, hi_idx), 26, body_cs)
        total += emit("AFB", a, g, fh)                    # body, Hiragino 26
        a, g = rasterize((HIRAGINO, hi_idx), 24, body_cs)
        total += emit("AFQ", a, g, fh)                    # omen line, Hiragino 24
        # play pages (compact media card): label + subtitle face.
        # Body charset plus the fixed UI strings, so the pages read without a
        # card; everything else (titles, show names) falls back to tiny18.afn.
        tiny_cs = sorted(set(body_cs) | set(PLAY_UI_CHARS))
        a, g = rasterize((HIRAGINO, hi_idx), 18, tiny_cs)
        total += emit("AFT", a, g, fh)                    # play label/sub, Hiragino 18
        # clock cockpit page: Menlo Bold digits
        mn_idx, mn_name = find_face(MENLO, "Menlo", ("Bold",))
        a, g = rasterize((MENLO, mn_idx), 96, sorted("0123456789:-"))
        total += emit("AFM", a, g, fh)                    # big time, Menlo 96
        a, g = rasterize((MENLO, mn_idx), 28, sorted("0123456789"))
        total += emit("AFN", a, g, fh)                    # seconds, Menlo 28
        a, g = rasterize((MENLO, mn_idx), 17, sorted("0123456789.:-"))
        total += emit("AFO", a, g, fh)                    # date + play-page times, Menlo 17
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB source, "
          f"{total // 1024} KB glyph data)")


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--sd":
        if "--only" in sys.argv:
            ONLY.update(sys.argv[sys.argv.index("--only") + 1].split(","))
        main_sd(sys.argv[2])
    else:
        main()
