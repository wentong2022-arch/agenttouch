// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
// Orientation pages: clock / calendar / todo. Flat widget style: pure black,
// Logisoso type (u8g2, see pet_fonts.h), white primary / grey secondary /
// agent-accent details, one white rounded card with a flat analog dial.
// Time comes from the host ({"t":"time"}); a soft clock runs on millis().
#include "pages.h"
#include "config.h"
#include "pet_fonts.h"
#include "i18n.h"
#include <time.h>

uint8_t g_lang = LANG_ZH;   // i18n.h: set from NVS at boot and by host cfg

static const uint16_t BG      = 0x0000;
static const uint16_t WHITE   = 0xFFFF;
static const uint16_t GREYTXT = 0x7BEF;
static const uint16_t GREYDIM = 0x39E7;

// ---------------------------------------------------------------- soft clock
static uint32_t epochBase = 0;   // local seconds at baseMs
static uint32_t baseMs    = 0;

void clockSet(uint32_t localEpoch) { epochBase = localEpoch; baseMs = millis(); }
bool clockValid() { return epochBase != 0; }

static uint32_t clockNow() {
  return epochBase + (millis() - baseMs) / 1000;   // both wrap-safe unsigned
}

uint32_t clockEpoch() { return clockValid() ? clockNow() : 0; }

static void localTm(struct tm& out) {
  time_t tt = (time_t)clockNow();     // tz already applied -> use gmtime
  gmtime_r(&tt, &out);
}

// ---------------------------------------------------------------- helpers
// classic-font centered text (todo / no-clock states)
static void tc(Arduino_Canvas* c, int cx, int y, const char* s,
               uint8_t size, uint16_t col) {
  c->setTextSize(size);
  c->setTextColor(col);
  c->setCursor(cx - (int)strlen(s) * 3 * size, y);
  c->print(s);
}

// Small companion eyes so the todo page still reads as the pet.
static void miniEyes(Arduino_Canvas* c, uint32_t t, int cy) {
  float blink = 1.0f;
  uint32_t bp = t % 4600;
  if (bp > 4400) blink = fabsf((bp - 4400) / 200.0f - 0.5f) * 2.0f;
  int h = max(4, (int)(30 * blink));
  for (int s = -1; s <= 1; s += 2)
    c->fillRoundRect(240 + s * 38 - 15, cy + (30 - h) / 2, 30, h,
                     min(9, h / 2), WHITE);
}

static const char* MO3[12]  = {"JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                               "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"};
static const char* MOF[12]  = {"JANUARY", "FEBRUARY", "MARCH", "APRIL",
                               "MAY", "JUNE", "JULY", "AUGUST", "SEPTEMBER",
                               "OCTOBER", "NOVEMBER", "DECEMBER"};
static const char* WD3[7]   = {"SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"};

// Print s with extra tracking between glyphs (letterspacing the u8g2 fonts
// don't do natively); returns the width drawn.
static int trackPrint(Arduino_Canvas* c, int x, int y, const char* s,
                      int track, uint16_t col) {
  c->setTextColor(col);
  int x0 = x;
  for (const char* p = s; *p; ++p) {
    c->setCursor(x, y);
    c->write((uint8_t)*p);
    x = c->getCursorX() + track;
  }
  return x - track - x0;
}

// Width of s at the current font: invisible draw above the canvas.
static int trackWidth(Arduino_Canvas* c, const char* s, int track) {
  return trackPrint(c, 0, -200, s, track, BG);
}

// ---------------------------------------------------------------- clock
// 圆环座舱 clock (user-picked 蓝环+青字 2026-08-29):
// implemented at the end of this file where the AA font machinery is visible.

// ---------------------------------------------------------------- calendar
static int daysInMonth(int year, int mon0) {
  static const int D[12] = {31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31};
  if (mon0 == 1 && (year % 4 == 0 && (year % 100 != 0 || year % 400 == 0)))
    return 29;
  return D[mon0];
}

void drawCalendarPage(Arduino_Canvas* c, uint32_t t, uint16_t accent) {
  c->fillScreen(BG);
  c->setTextSize(1);
  if (!clockValid()) {
    miniEyes(c, t, 180);
    tc(c, 240, 260, "WAITING FOR HOST", 2, GREYDIM);
    return;
  }
  struct tm tmv;
  localTm(tmv);

  // Direction B: centered tracked header "MONTH ▪ YEAR"
  c->setFont(u8g2_font_fur25_tr);
  char ys[6];
  snprintf(ys, sizeof(ys), "%d", 1900 + tmv.tm_year);
  int wm = trackWidth(c, MOF[tmv.tm_mon], 8);
  int wy = trackWidth(c, ys, 8);
  int hx = (480 - (wm + 36 + wy)) / 2;
  trackPrint(c, hx, 66, MOF[tmv.tm_mon], 8, GREYTXT);
  c->fillRect(hx + wm + 14, 50, 8, 8, accent);
  trackPrint(c, hx + wm + 36, 66, ys, 8, GREYDIM);

  // Monday-first weekday row, columns centered
  c->setFont(u8g2_font_fur17_tr);
  static const char WD[8] = "MTWTFSS";
  for (int i = 0; i < 7; i++) {
    char s[2] = {WD[i], 0};
    int cw = trackWidth(c, s, 0);
    trackPrint(c, 65 + i * 58 - cw / 2, 122, s, 0, GREYDIM);
  }

  int wcolToday = (tmv.tm_wday + 6) % 7;                    // 0 = Monday
  int wcolFirst = (wcolToday + 7 - (tmv.tm_mday - 1) % 7) % 7;
  int days = daysInMonth(1900 + tmv.tm_year, tmv.tm_mon);
  int col = wcolFirst, row = 0;
  for (int d = 1; d <= days; d++) {
    int cx = 65 + col * 58, base = 180 + row * 50;
    char s[4];
    snprintf(s, sizeof(s), "%d", d);
    int w = trackWidth(c, s, 0);
    uint16_t txt;
    if (d == tmv.tm_mday) {
      c->fillCircle(cx, base - 8, 23, accent);              // true circle
      txt = BG;
    } else {
      txt = col >= 5 ? GREYTXT : WHITE;                     // weekends dimmer
    }
    trackPrint(c, cx - w / 2, base, s, 0, txt);
    if (++col == 7) { col = 0; row++; }
  }
  c->setFont((const GFXfont*)nullptr);
}

// ---------------------------------------------------------------- cyber almanac
// 赛博黄历, 方向一「朱砂符纸」(design canvas artifact 29d853d2): gold
// sexagenary header, vermilion 宜 seal / indigo 忌 seal, signal+color param
// row, cyan omen line. Data arrives daily from the host ({"t":"almanac"});
// CJK text renders with Arduino_GFX's bundled cubic11 pixel font (俐方體,
// 10167 glyphs — the ONLY translation unit referencing it, keep it that way:
// the font header defines the array per-TU).
static const uint16_t AL_GOLD    = C565(0xE8, 0xC5, 0x6A);
static const uint16_t AL_GOLDDIM = C565(0x54, 0x47, 0x26);
static const uint16_t AL_RED     = C565(0xC1, 0x35, 0x2A);
static const uint16_t AL_SEALTXT = C565(0xF6, 0xEF, 0xE4);
static const uint16_t AL_INK     = C565(0x1C, 0x22, 0x30);
static const uint16_t AL_INKBRD  = C565(0x3A, 0x42, 0x56);
static const uint16_t AL_INKTXT  = C565(0x8F, 0xA0, 0xC0);
static const uint16_t AL_CYAN    = C565(0x3E, 0xE6, 0xD2);
static const uint16_t AL_WHITE   = C565(0xED, 0xEF, 0xF2);
static const uint16_t AL_GREY    = C565(0x9A, 0xA3, 0xB2);
static const uint16_t AL_DIM     = C565(0x6A, 0x74, 0x80);

static char alGz[16], alSx[16], alJc[16], alDate[8];
static char alYi[2][48], alJi[2][48], alQian[96], alDir[12], alCn[20];
static uint8_t  alSig   = 3;
static uint16_t alColor = AL_CYAN;
static bool     alValid = false;
// alternates + pick (pages.h 五份黄历); alHash fingerprints the whole text so
// commit can tell "same words again" from "new words".
struct AlAlt { char yi[2][48], ji[2][48], qian[96]; };
static AlAlt    alAlt[ALMANAC_ALT_MAX];
static uint8_t  alAltN = 0, alAltFill = 0;
static uint8_t  alPick = 0;
static uint32_t alHash = 0;
static const char* const AL_PICK_TAG[ALMANAC_ALT_MAX] = {"签二", "签三", "签四", "签五"};
static const char* pkYi(int i)  { return alPick ? alAlt[alPick - 1].yi[i] : alYi[i]; }
static const char* pkJi(int i)  { return alPick ? alAlt[alPick - 1].ji[i] : alJi[i]; }
static const char* pkQian()     { return alPick ? alAlt[alPick - 1].qian : alQian; }

void almanacSet(const char* gz, const char* sx, const char* jc, const char* date,
                const char* yi0, const char* yi1, const char* ji0, const char* ji1,
                const char* qian, const char* dir, int sig,
                const char* cn, uint16_t color) {
  strlcpy(alGz, gz, sizeof(alGz));
  strlcpy(alSx, sx, sizeof(alSx));
  strlcpy(alJc, jc, sizeof(alJc));
  strlcpy(alDate, date, sizeof(alDate));
  strlcpy(alYi[0], yi0, sizeof(alYi[0]));
  strlcpy(alYi[1], yi1, sizeof(alYi[1]));
  strlcpy(alJi[0], ji0, sizeof(alJi[0]));
  strlcpy(alJi[1], ji1, sizeof(alJi[1]));
  strlcpy(alQian, qian, sizeof(alQian));
  strlcpy(alDir, dir, sizeof(alDir));
  strlcpy(alCn, cn, sizeof(alCn));
  alSig = (uint8_t)((sig < 0) ? 0 : (sig > 4 ? 4 : sig));
  alColor = color;
  alValid = true;
}
bool almanacValid() { return alValid; }

void almanacAltClear() { alAltFill = 0; }
void almanacAltAdd(const char* yi0, const char* yi1, const char* ji0, const char* ji1,
                   const char* qian) {
  if (alAltFill >= ALMANAC_ALT_MAX) return;
  AlAlt& a = alAlt[alAltFill++];
  strlcpy(a.yi[0], yi0, sizeof(a.yi[0]));
  strlcpy(a.yi[1], yi1, sizeof(a.yi[1]));
  strlcpy(a.ji[0], ji0, sizeof(a.ji[0]));
  strlcpy(a.ji[1], ji1, sizeof(a.ji[1]));
  strlcpy(a.qian, qian, sizeof(a.qian));
}
static uint32_t alFnv(uint32_t h, const char* s) {
  for (; *s; s++) { h ^= (uint8_t)*s; h *= 16777619u; }
  return h ^ 0xFF;                                   // field separator
}
void almanacCommit() {
  alAltN = alAltFill;
  uint32_t h = 2166136261u;
  h = alFnv(h, alDate); h = alFnv(h, alYi[0]); h = alFnv(h, alYi[1]);
  h = alFnv(h, alJi[0]); h = alFnv(h, alJi[1]); h = alFnv(h, alQian);
  for (int i = 0; i < alAltN; i++) {
    h = alFnv(h, alAlt[i].yi[0]); h = alFnv(h, alAlt[i].yi[1]);
    h = alFnv(h, alAlt[i].ji[0]); h = alFnv(h, alAlt[i].ji[1]); h = alFnv(h, alAlt[i].qian);
  }
  h = alFnv(h, "n") + alAltN;
  if (h != alHash) alPick = 0;                       // new day / new words: back to the own draw
  alHash = h;
  if (alPick > alAltN) alPick = 0;
}
int almanacAltCount() { return alAltN; }
int almanacPick()     { return alPick; }
int almanacPickStep(int dir) {
  int n = alAltN + 1;
  if (n <= 1) return 0;
  alPick = (uint8_t)(((int)alPick + (dir >= 0 ? 1 : -1) + n) % n);
  return alPick;
}

// Anti-aliased CJK text: 4bpp glyphs baked offline from macOS fonts by
// host/gen_almanac_font.py (Songti SC Bold header/seals, Hiragino W6 body)
// and alpha-blended per pixel. Replaced the 11px cubic11 bitmap font, whose
// upscaled blocks read as "low-res" on this 314-PPI panel (user, 2026-08-29).
#include "almanac_font.h"
#include "sdfont.h"

// sd: which card font (sdfont.h) backs this face for glyphs missing in flash
struct AFont { const uint32_t* cp; const uint32_t* off; const uint8_t* data;
               int n; int ascent; int8_t sd; };
static const AFont AF_HEAD = {AFH_CP, AFH_OFF, AFH_DATA, AFH_COUNT, AFH_ASCENT, SDF_HEAD};
static const AFont AF_SEAL = {AFS_CP, AFS_OFF, AFS_DATA, AFS_COUNT, AFS_ASCENT, -1};
static const AFont AF_BODY = {AFB_CP, AFB_OFF, AFB_DATA, AFB_COUNT, AFB_ASCENT, SDF_BODY};
static const AFont AF_QUOT = {AFQ_CP, AFQ_OFF, AFQ_DATA, AFQ_COUNT, AFQ_ASCENT, SDF_QUOT};

static uint16_t blend565(uint16_t bg, uint16_t fg, uint8_t a) {   // a: 0..15
  int br = (bg >> 11) & 31, bgr = (bg >> 5) & 63, bb = bg & 31;
  int fr = (fg >> 11) & 31, fgr = (fg >> 5) & 63, fb = fg & 31;
  return (uint16_t)(((br + ((fr - br) * a) / 15) << 11) |
                    ((bgr + ((fgr - bgr) * a) / 15) << 5) |
                    (bb + ((fb - bb) * a) / 15));
}

static const uint8_t* afGlyph(const AFont& f, uint32_t cp) {
  int lo = 0, hi = f.n - 1;
  while (lo <= hi) {
    int mid = (lo + hi) / 2;
    if (f.cp[mid] == cp) return f.data + f.off[mid];
    if (f.cp[mid] < cp) lo = mid + 1; else hi = mid - 1;
  }
  return f.sd >= 0 ? sdFontGlyph((uint8_t)f.sd, cp) : nullptr;   // card fallback
}

static uint32_t afDecode(const char*& p) {   // tiny UTF-8 decoder (BMP only)
  uint8_t c = (uint8_t)*p++;
  if (c < 0x80) return c;
  if ((c >> 5) == 6) {
    uint32_t v = (uint32_t)(c & 31) << 6;
    return v | ((uint8_t)*p++ & 63);
  }
  if ((c >> 4) == 14) {
    uint32_t v = (uint32_t)(c & 15) << 12;
    v |= ((uint8_t)*p++ & 63) << 6;
    return v | ((uint8_t)*p++ & 63);
  }
  return '?';
}

// glyph record: w, h, adv, int8 dx, int8 dyTop, then ceil(w/2)*h 4bpp bytes.
// bg must match what the glyph sits on (page black / seal fill).
static int afChar(Arduino_Canvas* c, const AFont& f, int x, int base,
                  uint32_t cp, uint16_t fg, uint16_t bg) {
  const uint8_t* g = afGlyph(f, cp);
  if (!g) { c->drawRect(x + 2, base - 16, 15, 16, fg); return 19; }   // tofu
  int w = g[0], h = g[1], adv = g[2];
  int dx = (int8_t)g[3], dy = (int8_t)g[4];
  const uint8_t* d = g + 5;
  int top = base - f.ascent + dy, stride = (w + 1) / 2;
  for (int yy = 0; yy < h; yy++)
    for (int xx = 0; xx < w; xx++) {
      uint8_t b = d[yy * stride + xx / 2];
      uint8_t a = (xx & 1) ? (b & 15) : (b >> 4);
      if (a) c->drawPixel(x + dx + xx, top + yy,
                          a == 15 ? fg : blend565(bg, fg, a));
    }
  return adv;
}

static int afPrint(Arduino_Canvas* c, const AFont& f, int x, int base,
                   const char* s, uint16_t fg, uint16_t bg = BG) {
  const char* p = s;
  while (*p) x += afChar(c, f, x, base, afDecode(p), fg, bg);
  return x;
}

static int afWidth(const AFont& f, const char* s) {
  int w = 0;
  const char* p = s;
  while (*p) {
    const uint8_t* g = afGlyph(f, afDecode(p));
    w += g ? g[2] : 19;
  }
  return w;
}

// Center a string's ink box (not its advance/ascent box) on (cx, cy): the
// seal characters sat ~7 px low with a hand-picked baseline (user, 2026-09-03).
static void afPrintCenter(Arduino_Canvas* c, const AFont& f, int cx, int cy,
                          const char* s, uint16_t fg, uint16_t bg) {
  int x = 0, left = 1 << 20, right = -(1 << 20), top = 1 << 20, bottom = -(1 << 20);
  for (const char* p = s; *p;) {
    const uint8_t* g = afGlyph(f, afDecode(p));
    if (!g) { x += 19; continue; }
    int w = g[0], h = g[1], dx = (int8_t)g[3], dy = (int8_t)g[4];
    if (w && h) {
      left = min(left, x + dx); right = max(right, x + dx + w);
      top = min(top, dy - f.ascent); bottom = max(bottom, dy - f.ascent + h);
    }
    x += g[2];
  }
  if (right < left) return;
  int ox = cx - (left + right) / 2, base = cy - (top + bottom) / 2;
  afPrint(c, f, ox, base, s, fg, bg);
}

int almanacPrint(Arduino_Canvas* c, int x, int baseline, const char* s,
                 uint16_t fg, uint16_t bg) {
  return afPrint(c, AF_BODY, x, baseline, s, fg, bg);
}
int almanacTextWidth(const char* s) { return afWidth(AF_BODY, s); }

void drawAlmanacPage(Arduino_Canvas* c, uint32_t t) {
  (void)t;
  c->fillScreen(BG);
  if (!alValid) {
    afPrint(c, AF_BODY, (480 - afWidth(AF_BODY, "黄历同步中...")) / 2, 250,
            "黄历同步中...", AL_DIM);
    return;
  }

  // gold Songti header: 乙亥日 · 属马 · 平日, gregorian date right-aligned
  char head[64];
  snprintf(head, sizeof(head), "%s · %s · %s", alGz, alSx, alJc);
  afPrint(c, AF_HEAD, 34, 56, head, AL_GOLD);
  int dw = afWidth(AF_HEAD, alDate);
  afPrint(c, AF_HEAD, 446 - dw, 56, alDate, AL_DIM);
  if (alPick > 0) {   // 签二..签五: which reading this is (up/down swipe cycles them)
    const char* tag = AL_PICK_TAG[alPick - 1];
    afPrint(c, AF_HEAD, 446 - dw - 16 - afWidth(AF_HEAD, tag), 56, tag, AL_CYAN);
  }
  c->fillRect(34, 72, 180, 2, AL_GOLD);
  c->fillRect(214, 73, 232, 1, AL_GOLDDIM);

  // 宜 seal + entries
  c->fillRoundRect(34, 96, 62, 62, 10, AL_RED);
  afPrintCenter(c, AF_SEAL, 65, 127, "宜", AL_SEALTXT, AL_RED);
  afPrint(c, AF_BODY, 108, 124, pkYi(0), AL_WHITE);
  afPrint(c, AF_BODY, 108, 160, pkYi(1), AL_WHITE);

  // 忌 seal + entries
  c->fillRoundRect(34, 186, 62, 62, 10, AL_INK);
  c->drawRoundRect(34, 186, 62, 62, 10, AL_INKBRD);
  afPrintCenter(c, AF_SEAL, 65, 217, "忌", AL_INKTXT, AL_INK);
  afPrint(c, AF_BODY, 108, 214, pkJi(0), AL_GREY);
  afPrint(c, AF_BODY, 108, 250, pkJi(1), AL_GREY);

  // param row: "信号 东南 ▂▄▆█" left, color swatch + name right-aligned
  char sigTxt[32];
  snprintf(sigTxt, sizeof(sigTxt), "信号 %s", alDir);
  afPrint(c, AF_BODY, 34, 338, sigTxt, AL_DIM);
  int bx = 34 + afWidth(AF_BODY, sigTxt) + 14;
  for (int i = 0; i < 4; i++) {
    int h = 9 + 4 * i;
    c->fillRect(bx + i * 11, 338 - h, 7, h, i < alSig ? AL_CYAN : AL_INKBRD);
  }
  int nw = afWidth(AF_BODY, alCn);
  c->fillRoundRect(446 - nw - 28, 319, 20, 20, 4, alColor);
  afPrint(c, AF_BODY, 446 - nw, 338, alCn, AL_DIM);

  // omen line, centered, cyan (24 px: the longest omen + brackets must
  // still clear the margins — 26 px would kiss the bezel)
  char qbuf[112];
  snprintf(qbuf, sizeof(qbuf), "「%s」", pkQian());
  afPrint(c, AF_QUOT, (480 - afWidth(AF_QUOT, qbuf)) / 2, 410, qbuf, AL_CYAN);
}

// ---------------------------------------------------------------- clock v2
// 圆环座舱 (2026-08-29 定稿「蓝环+青字」): breathing
// minute ring (accent blue) + orbiting white second dot, wrapped around
// C-direction cockpit content — gold Songti sexagenary/weekday header,
// Menlo Bold time with cyan seconds, and the true-computed 时辰 ink panel.
static const AFont AF_TINY = {AFT_CP, AFT_OFF, AFT_DATA, AFT_COUNT, AFT_ASCENT, SDF_TINY};
static const AFont AF_MBIG = {AFM_CP, AFM_OFF, AFM_DATA, AFM_COUNT, AFM_ASCENT, -1};
static const AFont AF_MSEC = {AFN_CP, AFN_OFF, AFN_DATA, AFN_COUNT, AFN_ASCENT, -1};
static const AFont AF_MDAT = {AFO_CP, AFO_OFF, AFO_DATA, AFO_COUNT, AFO_ASCENT, -1};

static const uint16_t CK_TRACK  = C565(0x14, 0x17, 0x1C);   // ring groove
static const uint16_t CK_INK    = C565(0x12, 0x16, 0x1F);   // panel fill
static const uint16_t CK_DIMBAR = C565(0x1E, 0x4E, 0x48);   // unlit signal bar

static const char* const CK_WEEKDAY[7] = {"日", "一", "二", "三", "四", "五", "六"};
static const char* const CK_SHICHEN[12] = {   // double-hour names, 子 first
    "子时 · 夜半", "丑时 · 鸡鸣", "寅时 · 平旦", "卯时 · 日出",
    "辰时 · 食时", "巳时 · 隅中", "午时 · 日中", "未时 · 日昳",
    "申时 · 晡时", "酉时 · 日入", "戌时 · 黄昏", "亥时 · 人定"};

// afPrint with per-glyph tracking (the 96 px Menlo digits sit too loose at
// their natural advance; the mock uses about -6 px).
static int afPrintTrack(Arduino_Canvas* c, const AFont& f, int x, int base,
                        const char* s, uint16_t fg, int track) {
  const char* p = s;
  while (*p) x += afChar(c, f, x, base, afDecode(p), fg, BG) + track;
  return x - track;
}

static int afWidthTrack(const AFont& f, const char* s, int track) {
  int w = 0, n = 0;
  const char* p = s;
  while (*p) {
    const uint8_t* g = afGlyph(f, afDecode(p));
    w += g ? g[2] : 19;
    n++;
  }
  return n > 1 ? w + track * (n - 1) : w;
}

void drawClockPage(Arduino_Canvas* c, uint32_t t, uint16_t accent) {
  (void)t;
  c->fillScreen(BG);
  bool ok = clockValid();

  // ring groove is always on; progress + dot only once the clock is set
  c->fillArc(240, 240, 218, 214, 0, 360, CK_TRACK);

  if (!ok) {
    afPrintTrack(c, AF_MBIG, (480 - afWidthTrack(AF_MBIG, "--:--", -6)) / 2,
                 262, "--:--", GREYDIM, -6);
    c->setTextSize(1);
    c->setFont(u8g2_font_fur17_tr);
    int w = trackWidth(c, "WAITING FOR HOST", 3);
    trackPrint(c, (480 - w) / 2, 330, "WAITING FOR HOST", 3, GREYDIM);
    c->setFont((const GFXfont*)nullptr);
    return;
  }

  struct tm tmv;
  localTm(tmv);
  float msFrac = ((millis() - baseMs) % 1000) / 1000.0f;

  // minute-progress arc from 12 o'clock (angles clockwise, 0 deg = east)
  float sweep = ((tmv.tm_min * 60 + tmv.tm_sec) + msFrac) * 360.0f / 3600.0f;
  if (sweep > 1.0f) {
    if (sweep <= 90.0f) {
      c->fillArc(240, 240, 218, 214, 270, 270 + sweep, accent);
    } else {
      c->fillArc(240, 240, 218, 214, 270, 360, accent);
      c->fillArc(240, 240, 218, 214, 0, sweep - 90.0f, accent);
    }
  }
  // orbiting second dot rides on the ring
  float sa = (270.0f + (tmv.tm_sec + msFrac) * 6.0f) * 0.0174533f;
  c->fillCircle(240 + (int)(216 * cosf(sa)), 240 + (int)(216 * sinf(sa)), 7,
                WHITE);

  // gold header: sexagenary day (from the almanac push) + weekday + date
  char head[48];
  if (langEn())                        // English: full weekday, no 干支
    snprintf(head, sizeof(head), "%s", EN_WEEKDAY[tmv.tm_wday]);
  else if (almanacValid())
    snprintf(head, sizeof(head), "%s · 星期%s", alGz, CK_WEEKDAY[tmv.tm_wday]);
  else
    snprintf(head, sizeof(head), "星期%s", CK_WEEKDAY[tmv.tm_wday]);
  char ds[8];
  snprintf(ds, sizeof(ds), "%02d.%02d", tmv.tm_mon + 1, tmv.tm_mday);
  int hw = afWidth(AF_HEAD, head), dw = afWidthTrack(AF_MDAT, ds, 0);
  int hx = 240 - (hw + 12 + dw) / 2;
  afPrint(c, AF_HEAD, hx, 100, head, AL_GOLD);
  afPrintTrack(c, AF_MDAT, hx + hw + 12, 100, ds, AL_DIM, 0);
  c->fillRect(240 - 32, 112, 64, 2, AL_GOLD);

  // big Menlo time + cyan seconds on the shared baseline
  char ts[8], ss[4];
  snprintf(ts, sizeof(ts), "%02d:%02d", tmv.tm_hour, tmv.tm_min);
  snprintf(ss, sizeof(ss), "%02d", tmv.tm_sec);
  int tw = afWidthTrack(AF_MBIG, ts, -6);
  int sw = afWidthTrack(AF_MSEC, ss, 0);
  int tx = 240 - (tw + 12 + sw) / 2;
  afPrintTrack(c, AF_MBIG, tx, 262, ts, AL_WHITE, -6);
  afPrintTrack(c, AF_MSEC, tx + tw + 12, 262, ss, AL_CYAN, 0);

  // 时辰 ink panel with the deco signal bars (23-1 点 = 子时, two hours per)
  int shi = ((tmv.tm_hour + 1) / 2) % 12;
  if (langEn()) {
    // English two-line panel: gold Songti classical name over
    // a dim 18 px "HOUR OF THE …". The bars ride the upper row, right-aligned,
    // so the long second line runs underneath them: bars beside BOTH lines
    // made the widest panel 353 px, which pokes through the ring (inner edge
    // r = 214) at y 320..396; this way the widest is 288 px (ROOSTER) and
    // its rounded corners stay at r <= 207.
    const char* nm = EN_SHICHEN[shi];
    const char* an = EN_SHICHEN_ANIMAL[shi];
    int nw = afWidth(AF_HEAD, nm), aw = afWidth(AF_TINY, an);
    int inner = nw + 26 + 39 > aw ? nw + 26 + 39 : aw;
    int panelW = 24 + inner + 24, panelH = 76;
    int px = 240 - panelW / 2, py = 320;
    c->fillRoundRect(px, py, panelW, panelH, 14, CK_INK);
    c->drawRoundRect(px, py, panelW, panelH, 14, AL_INKBRD);
    afPrint(c, AF_HEAD, px + 24, py + 36, nm, AL_GOLD, CK_INK);
    afPrint(c, AF_TINY, px + 24, py + 62, an, AL_DIM, CK_INK);
    int bx = px + panelW - 24 - 39, bb = py + 36;
    for (int i = 0; i < 4; i++) {
      int h = 8 + 6 * i;
      c->fillRect(bx + i * 11, bb - h, 6, h, i < 3 ? AL_CYAN : CK_DIMBAR);
    }
    return;
  }
  const char* sc = CK_SHICHEN[shi];
  int scw = afWidth(AF_HEAD, sc);
  int panelW = 24 + scw + 26 + 39 + 24, panelH = 56;
  int px = 240 - panelW / 2, py = 330;
  c->fillRoundRect(px, py, panelW, panelH, 14, CK_INK);
  c->drawRoundRect(px, py, panelW, panelH, 14, AL_INKBRD);
  afPrint(c, AF_HEAD, px + 24, py + 38, sc, AL_GOLD, CK_INK);
  int bx = px + 24 + scw + 26, bb = py + panelH - 15;
  for (int i = 0; i < 4; i++) {
    int h = 8 + 6 * i;
    c->fillRect(bx + i * 11, bb - h, 6, h, i < 3 ? AL_CYAN : CK_DIMBAR);
  }
}

// ---------------------------------------------------------------- play page
// 播放页 ×2: the clock
// orientation's two flip sides share one compact media card. 当前播放 (left
// swipe) shows the Mac's track ({"t":"np"}); 播客 (right swipe) the board's
// own player (player.h). Square 480×480 layout, design px = device px:
//   card 448×224 at (16,20) r26  — label 18 px / 116 px artwork / two-line
//   26 px title / 18 px subtitle / 4 px slider with elapsed and -remaining
//   band y 264..424 — three 160×160 hit zones (prev|toggle|next; ±15 s on the
//   podcast page), glyphs at y=344, the middle one in an 88 px disc
//   bottom 56 px — the volume toast after an up/down swipe
#include "player.h"
#include "jpegrom.h"

// 18 px companion to almanacPrint, for English strings that overflow the
// 26 px face in a fixed box (face bubbles).
int almanacPrintSmall(Arduino_Canvas* c, int x, int baseline, const char* s,
                      uint16_t fg, uint16_t bg) {
  return afPrint(c, AF_TINY, x, baseline, s, fg, bg);
}
int almanacTextWidthSmall(const char* s) { return afWidth(AF_TINY, s); }

static const uint16_t PM_TRACK = C565(0x22, 0x26, 0x2E);   // slider groove
static const uint16_t PM_PLACE = C565(0x1A, 0x1F, 0x2A);   // artwork placeholder

enum { CARD_X = 16, CARD_Y = 20, CARD_W = 448, CARD_H = 224, CARD_R = 26,
       LABEL_BASE = 52, ART_X = 36, ART_Y = 66, ART_S = 116, ART_R = 18,
       TEXT_X = 168, TEXT_W = 276, SLIDER_X = 36, SLIDER_Y = 196, SLIDER_W = 408,
       TIME_BASE = 221, BAND_Y = 264, BAND_H = 160, GLYPH_CY = 344, PLAY_R = 44,
       VOL_Y = 435 };

// ---- Mac "now playing" state (host {"t":"np"}) ----------------------------
static bool  npHas = false, npIsPlaying = false;
static char  npTitle[128] = "", npArtist[128] = "", npAlbum[128] = "";
static char  npApp[32] = "", npCover[80] = "";
static float npPos = 0, npDur = 0, npRate = 1;
static uint32_t npAt = 0;          // millis() when pos was true

// Copy at most n-1 bytes, never splitting a UTF-8 sequence (host truncates
// by characters, but a BLE-shortened line must not leave half a glyph).
static void utf8Copy(char* dst, size_t n, const char* src) {
  if (!n) return;
  size_t len = strlen(src);
  if (len >= n) {
    len = n - 1;
    while (len && ((uint8_t)src[len] & 0xC0) == 0x80) len--;   // back to a lead byte
  }
  memcpy(dst, src, len);
  dst[len] = 0;
}
static void utf8Cat(char* dst, size_t n, const char* src) {
  size_t len = strlen(dst);
  if (len + 1 < n) utf8Copy(dst + len, n - len, src);
}

void npSet(bool on, const char* title, const char* artist, const char* album,
           float pos, float dur, bool play, float rate, const char* app,
           const char* cover) {
  npHas = on;
  if (!on) { npIsPlaying = false; npPos = npDur = 0; npCover[0] = 0; return; }
  utf8Copy(npTitle, sizeof(npTitle), title);
  utf8Copy(npArtist, sizeof(npArtist), artist);
  utf8Copy(npAlbum, sizeof(npAlbum), album);
  utf8Copy(npApp, sizeof(npApp), app);
  utf8Copy(npCover, sizeof(npCover), cover);
  npPos = pos < 0 ? 0 : pos;
  npDur = dur < 0 ? 0 : dur;
  npIsPlaying = play;
  npRate = (rate > 0.05f && rate < 8.0f) ? rate : 1.0f;
  npAt = millis();
}
bool npOn()      { return npHas; }
bool npPlaying() { return npHas && npIsPlaying; }

// Mac position between pushes: the host only speaks on change and every 15 s,
// so the slider runs locally from the last known point.
static float npPosNow() {
  float p = npPos;
  if (npIsPlaying) p += (float)(uint32_t)(millis() - npAt) / 1000.0f * npRate;
  if (npDur > 0 && p > npDur) p = npDur;
  return p < 0 ? 0 : p;
}
void npSnapshot(float* pos, float* dur, bool* play) {
  if (pos)  *pos  = npHas ? npPosNow() : 0.0f;
  if (dur)  *dur  = npHas ? npDur : 0.0f;
  if (play) *play = npHas && npIsPlaying;
}

// The app the Mac is playing in, in the label's language.
static const char* npAppName() {
  if (!strcmp(npApp, "netease"))  return tr(S_APP_NETEASE);
  if (!strcmp(npApp, "music"))    return tr(S_APP_MUSIC);
  if (!strcmp(npApp, "podcasts")) return tr(S_APP_PODCASTS);
  if (!strcmp(npApp, "chrome"))   return tr(S_APP_CHROME);
  if (!strcmp(npApp, "safari"))   return tr(S_APP_SAFARI);
  return npApp;
}

// ---- text fitting (cached: a card-backed glyph lookup is a microSD read) ---
// One line clipped to maxW with an ellipsis; single pass, remembers the last
// cut that still leaves room for the "…".
static void afFit(char* out, size_t n, const AFont& f, const char* s, int maxW) {
  if (n < 8) { if (n) out[0] = 0; return; }
  int ell = afWidth(f, "…"), w = 0;
  size_t cut = 0;
  const char* p = s;
  while (*p) {
    const uint8_t* g = afGlyph(f, afDecode(p));
    int adv = g ? g[2] : 19;
    if (w + adv > maxW) {
      if (cut > n - 4) cut = n - 4;
      memcpy(out, s, cut);
      out[cut] = 0;
      strlcat(out, "…", n);
      return;
    }
    w += adv;
    if (w + ell <= maxW) cut = (size_t)(p - s);
  }
  utf8Copy(out, n, s);
}

// Claim card (face.cpp): one line fitted to maxW in body26 / tiny18, and
// "can every glyph be drawn" (flash or card) for the ASCII-name fallback.
void almanacFit(char* out, size_t n, const char* s, int maxW, bool small) {
  afFit(out, n, small ? AF_TINY : AF_BODY, s, maxW);
}
bool almanacHasAll(const char* s) {
  for (const char* p = s; *p;) {
    uint32_t cp = afDecode(p);
    if (cp >= 0x80 && !afGlyph(AF_BODY, cp)) return false;
  }
  return true;
}

// Two lines: the first breaks where the next glyph would overflow (after the
// last space if the break falls inside a Latin word), the second is fitted
// with an ellipsis. Returns the number of lines used (1 or 2).
static int afWrap2(char* l1, char* l2, size_t n, const AFont& f, const char* s, int maxW) {
  int w = 0;
  const char* p = s;
  const char* brk = nullptr;          // where line 1 ends
  const char* lastSpace = nullptr;
  while (*p) {
    const char* q = p;
    uint32_t cp = afDecode(q);
    const uint8_t* g = afGlyph(f, cp);
    int adv = g ? g[2] : 19;
    if (w + adv > maxW) { brk = p; break; }
    w += adv;
    if (cp == ' ') lastSpace = q;      // break after the space
    p = q;
  }
  if (!brk) { utf8Copy(l1, n, s); l2[0] = 0; return 1; }
  if (lastSpace && (brk - s) > 8 && (brk - lastSpace) < 20) brk = lastSpace;
  size_t len = (size_t)(brk - s);
  if (len >= n) len = n - 1;
  memcpy(l1, s, len); l1[len] = 0;
  while (*brk == ' ') brk++;
  afFit(l2, n, f, brk, maxW);
  return 2;
}

// Per-slot cache: 0 title (two lines), 1 subtitle, 2 label, 3 hint.
static char pmKey[4][320], pmL1[4][320], pmL2[4][320];
static int  pmLines[4];
static void pmText(int slot, const AFont& f, const char* s, int maxW, bool two) {
  if (strcmp(pmKey[slot], s) == 0) return;
  utf8Copy(pmKey[slot], sizeof(pmKey[slot]), s);
  if (two) pmLines[slot] = afWrap2(pmL1[slot], pmL2[slot], sizeof(pmL1[slot]), f, s, maxW);
  else   { afFit(pmL1[slot], sizeof(pmL1[slot]), f, s, maxW); pmL2[slot][0] = 0; pmLines[slot] = 1; }
}

// ---- artwork: the 200 px card cover scaled once to 116 px, corners rounded
// into the card colour so the blit is a single rectangle -------------------
static uint16_t* pmArt = nullptr;
static char      pmArtKey[80] = "";
static bool      pmArtOk = false;

static inline bool pmCornerOut(int x, int y, int s, int r) {
  int cx = x < r ? r : (x >= s - r ? s - 1 - r : -1);
  int cy = y < r ? r : (y >= s - r ? s - 1 - r : -1);
  if (cx < 0 || cy < 0) return false;                 // not in a corner square
  int dx = x - cx, dy = y - cy;
  return dx * dx + dy * dy > r * r;
}

static const uint16_t* pmArtGet(const char* path) {
  if (!path || !path[0]) return nullptr;
  if (pmArtOk && strcmp(path, pmArtKey) == 0) return pmArt;
  CoverImg img = coverGet(path);                      // cached decode / cached failure
  if (!img.px || img.w <= 0 || img.h <= 0) { pmArtOk = false; return nullptr; }
  if (!pmArt) pmArt = (uint16_t*)heap_caps_malloc((size_t)ART_S * ART_S * 2, MALLOC_CAP_SPIRAM);
  if (!pmArt) return nullptr;
  for (int y = 0; y < ART_S; y++) {
    int sy = y * img.h / ART_S;
    for (int x = 0; x < ART_S; x++) {
      int sx = x * img.w / ART_S;
      pmArt[y * ART_S + x] = pmCornerOut(x, y, ART_S, ART_R)
                               ? AL_INK : img.px[(size_t)sy * COVER_MAX + sx];
    }
  }
  snprintf(pmArtKey, sizeof(pmArtKey), "%s", path);
  pmArtOk = true;
  return pmArt;
}

static void pmPlaceholder(Arduino_Canvas* c) {
  c->fillRoundRect(ART_X, ART_Y, ART_S, ART_S, ART_R, PM_PLACE);
  c->drawRoundRect(ART_X, ART_Y, ART_S, ART_S, ART_R, AL_INKBRD);
  int cx = ART_X + ART_S / 2, cy = ART_Y + ART_S / 2;   // a beamed pair of notes
  c->fillCircle(cx - 13, cy + 13, 7, AL_DIM);
  c->fillCircle(cx + 11, cy + 8, 7, AL_DIM);
  c->fillRect(cx - 8, cy - 14, 3, 27, AL_DIM);
  c->fillRect(cx + 16, cy - 19, 3, 27, AL_DIM);
  c->fillTriangle(cx - 8, cy - 14, cx + 19, cy - 19, cx + 19, cy - 14, AL_DIM);
  c->fillTriangle(cx - 8, cy - 14, cx - 8, cy - 9, cx + 19, cy - 14, AL_DIM);
}

// ---- glyphs, SF-style, drawn (never fonts) --------------------------------
static void pmPlayPause(Arduino_Canvas* c, int cx, int cy, bool playing, uint16_t col) {
  if (playing) {                                       // ▮▮ = "pause me"
    c->fillRoundRect(cx - 14, cy - 14, 10, 28, 2, col);
    c->fillRoundRect(cx + 4,  cy - 14, 10, 28, 2, col);
  } else {                                             // ▶ = "play me"
    c->fillTriangle(cx - 11, cy - 15, cx - 11, cy + 15, cx + 17, cy, col);
  }
}
static void pmSkip(Arduino_Canvas* c, int cx, int cy, bool fwd, uint16_t col) {
  if (fwd) {
    c->fillTriangle(cx - 17, cy - 11, cx - 17, cy + 11, cx - 2,  cy, col);
    c->fillTriangle(cx - 2,  cy - 11, cx - 2,  cy + 11, cx + 13, cy, col);
    c->fillRect(cx + 13, cy - 11, 4, 22, col);
  } else {
    c->fillTriangle(cx + 17, cy - 11, cx + 17, cy + 11, cx + 2,  cy, col);
    c->fillTriangle(cx + 2,  cy - 11, cx + 2,  cy + 11, cx - 13, cy, col);
    c->fillRect(cx - 17, cy - 11, 4, 22, col);
  }
}
// ±15 s: an open ring with an arrowhead at its start and "15" inside.
static void pmSeek15(Arduino_Canvas* c, int cx, int cy, bool fwd, uint16_t col) {
  if (fwd) {                                           // gap top-right, arrow clockwise
    c->fillArc(cx, cy, 17, 14, 300, 360, col);
    c->fillArc(cx, cy, 17, 14, 0, 250, col);
    c->fillTriangle(cx + 6, cy - 20, cx + 14, cy - 12, cx + 4, cy - 10, col);
  } else {
    c->fillArc(cx, cy, 17, 14, 290, 360, col);
    c->fillArc(cx, cy, 17, 14, 0, 240, col);
    c->fillTriangle(cx - 6, cy - 20, cx - 14, cy - 12, cx - 4, cy - 10, col);
  }
  int w = afWidth(AF_MDAT, "15");
  afPrint(c, AF_MDAT, cx - w / 2, cy + 6, "15", col);
}

static void pmFmtTime(char* out, size_t n, uint32_t sec) {
  if (sec >= 3600) snprintf(out, n, "%lu:%02lu:%02lu", (unsigned long)(sec / 3600),
                            (unsigned long)(sec / 60 % 60), (unsigned long)(sec % 60));
  else             snprintf(out, n, "%lu:%02lu", (unsigned long)(sec / 60), (unsigned long)(sec % 60));
}

// Volume toast in the bottom band (y 424..480 is empty by design): speaker,
// 236 px bar, level. Horizontal readout for an up/down gesture is the
// keyboard-volume-HUD convention (Mac, iOS landscape); the user preferred it
// here over a vertical bar crowding the transport band (2026-09-09).
static void pmVolume(Arduino_Canvas* c, uint8_t vol, float k) {
  if (k <= 0.02f) return;
  uint16_t col = vol ? AL_WHITE : AL_DIM;
  int sx = 88, cy = VOL_Y;
  c->fillRect(sx, cy - 4, 6, 8, col);
  c->fillTriangle(sx + 5, cy, sx + 14, cy - 9, sx + 14, cy + 9, col);
  if (vol) { c->fillArc(sx + 12, cy, 9, 7, 300, 360, col); c->fillArc(sx + 12, cy, 9, 7, 0, 60, col); }
  c->fillRoundRect(116, cy - 3, 236, 6, 3, PM_TRACK);
  int fill = 236 * vol / 100;
  if (fill > 0) c->fillRoundRect(116, cy - 3, fill < 6 ? 6 : fill, 6, 3, AL_WHITE);
  char num[4];
  snprintf(num, sizeof(num), "%u", vol);
  afPrint(c, AF_MDAT, 364, cy + 6, num, AL_GREY);
}

// ---- the page ----------------------------------------------------------------
void drawPlayPage(Arduino_Canvas* c, uint32_t t, uint16_t accent, uint8_t src,
                  uint8_t volume, float volK) {
  (void)t;
  c->fillScreen(BG);
  bool mac = src == PSRC_MAC;
  const PlayerInfo& pi = playerInfo();
  bool has = mac ? npHas : pi.hasItems;

  // card
  c->fillRoundRect(CARD_X, CARD_Y, CARD_W, CARD_H, CARD_R, AL_INK);
  c->drawRoundRect(CARD_X, CARD_Y, CARD_W, CARD_H, CARD_R, AL_INKBRD);

  // label
  char label[96];
  if (mac) {
    if (npHas && npApp[0]) snprintf(label, sizeof(label), tr(S_NP_LABEL_APP), npAppName());
    else                   snprintf(label, sizeof(label), "%s", tr(S_NP_LABEL));
  } else if (has) {
    snprintf(label, sizeof(label), tr(S_POD_LABEL_N), pi.index + 1, playerCount());
  } else {
    snprintf(label, sizeof(label), "%s", tr(S_POD_LABEL));
  }
  pmText(2, AF_TINY, label, CARD_W - 40, false);
  afPrint(c, AF_TINY, CARD_X + 20, LABEL_BASE, pmL1[2], AL_DIM, AL_INK);

  bool playing = false;
  if (!has) {
    // empty state: two lines in the middle of the card, glyphs dimmed
    const char* head = tr(mac ? S_NP_EMPTY : S_POD_EMPTY);
    const char* hint = tr(mac ? S_NP_HINT : S_POD_HINT);
    pmText(3, AF_TINY, hint, CARD_W - 56, true);
    int lines = pmLines[3];
    int base = lines == 2 ? 118 : 128;
    afPrint(c, AF_BODY, 240 - afWidth(AF_BODY, head) / 2, base, head, AL_GREY, AL_INK);
    afPrint(c, AF_TINY, 240 - afWidth(AF_TINY, pmL1[3]) / 2, base + 38, pmL1[3], AL_DIM, AL_INK);
    if (lines == 2)
      afPrint(c, AF_TINY, 240 - afWidth(AF_TINY, pmL2[3]) / 2, base + 62, pmL2[3], AL_DIM, AL_INK);
  } else {
    const char* title = mac ? npTitle : pi.title;
    const char* cover = mac ? npCover : pi.cover;
    playing = mac ? npIsPlaying : pi.playing;
    float pos = mac ? npPosNow() : (float)pi.posSec;
    float dur = mac ? npDur : (float)pi.durSec;

    char sub[288];
    sub[0] = 0;
    if (mac) {
      if (npArtist[0]) utf8Copy(sub, sizeof(sub), npArtist);
      if (npAlbum[0] && strcmp(npAlbum, npArtist) != 0) {
        if (sub[0]) utf8Cat(sub, sizeof(sub), " · ");
        utf8Cat(sub, sizeof(sub), npAlbum);
      }
      if (!sub[0]) utf8Copy(sub, sizeof(sub), npAppName());
    } else utf8Copy(sub, sizeof(sub), pi.show);

    // artwork (116, rounded) or the placeholder
    const uint16_t* art = cover[0] ? pmArtGet(cover) : nullptr;
    if (art) c->draw16bitRGBBitmap(ART_X, ART_Y, (uint16_t*)art, ART_S, ART_S);
    else     pmPlaceholder(c);

    // title (two lines) + subtitle, vertically centred beside the artwork
    pmText(0, AF_BODY, title, TEXT_W, true);
    pmText(1, AF_TINY, sub, TEXT_W, false);
    int lines = pmLines[0];
    int blockH = lines * 33 + (sub[0] ? 4 + 24 : 0);
    int top = ART_Y + (ART_S - blockH) / 2;
    afPrint(c, AF_BODY, TEXT_X, top + 26, pmL1[0], AL_WHITE, AL_INK);
    if (lines == 2) afPrint(c, AF_BODY, TEXT_X, top + 26 + 33, pmL2[0], AL_WHITE, AL_INK);
    if (sub[0]) afPrint(c, AF_TINY, TEXT_X, top + lines * 33 + 4 + 18, pmL1[1], AL_GREY, AL_INK);

    // slider + times
    c->fillRoundRect(SLIDER_X, SLIDER_Y, SLIDER_W, 4, 2, PM_TRACK);
    if (dur > 1.0f) {
      int fill = (int)(SLIDER_W * (pos / dur));
      if (fill > SLIDER_W) fill = SLIDER_W;
      if (fill >= 4) c->fillRoundRect(SLIDER_X, SLIDER_Y, fill, 4, 2, playing ? accent : AL_DIM);
    }
    char el[16], rm[18];
    uint32_t p = (uint32_t)(pos < 0 ? 0 : pos), d = (uint32_t)(dur < 0 ? 0 : dur);
    pmFmtTime(el, sizeof(el), p);
    rm[0] = '-';
    pmFmtTime(rm + 1, sizeof(rm) - 1, d > p ? d - p : 0);
    afPrint(c, AF_MDAT, SLIDER_X, TIME_BASE, el, AL_DIM, AL_INK);
    afPrint(c, AF_MDAT, SLIDER_X + SLIDER_W - afWidth(AF_MDAT, rm), TIME_BASE, rm, AL_DIM, AL_INK);
  }

  // transport band: three zones, glyph colour says whether they do anything
  uint16_t gcol = has ? AL_WHITE : AL_DIM;
  if (mac) { pmSkip(c, 80, GLYPH_CY, false, gcol); pmSkip(c, 400, GLYPH_CY, true, gcol); }
  else     { pmSeek15(c, 80, GLYPH_CY, false, gcol); pmSeek15(c, 400, GLYPH_CY, true, gcol); }
  c->fillCircle(240, GLYPH_CY, PLAY_R, AL_INK);
  c->drawCircle(240, GLYPH_CY, PLAY_R, AL_INKBRD);
  pmPlayPause(c, 240, GLYPH_CY, has && playing, gcol);

  pmVolume(c, volume, volK);
}

// ---- 钉住 chrome (pages.h) --------------------------------------------------
static uint16_t pgDim(uint16_t col, float f) {
  if (f >= 1.0f) return col;
  if (f <= 0.0f) return 0;
  int r = (int)(((col >> 11) & 0x1F) * f);
  int g = (int)(((col >> 5) & 0x3F) * f);
  int b = (int)((col & 0x1F) * f);
  return (uint16_t)((r << 11) | (g << 5) | b);
}

// Same geometry as face.cpp drawPin (bottom-right, ~20 px): head, collar,
// needle straight down. Grey = the AL_DIM the pages already use for
// secondary text, so it sits quietly on the clock and the almanac alike.
void drawPagePin(Arduino_Canvas* c, bool lit, uint16_t seatColor) {
  uint16_t col = lit ? seatColor : AL_DIM;
  const int cx = 442, cy = 440;
  c->fillCircle(cx, cy - 6, 7, col);
  c->fillRect(cx - 9, cy - 1, 18, 4, col);
  c->fillRoundRect(cx - 2, cy + 2, 4, 12, 2, col);
}

// Plain pill, centred on cy: each page hands in its own empty band (face and
// play pages 368 = above the volume bar / nameplate; clock 300 = between the
// digits and the 时辰 box; almanac 286 = between 忌 and the 信号 row), so the
// 1.5 s 名牌 never sits on a line of text.
void drawPillToast(Arduino_Canvas* c, const char* label, float k, int cy) {
  if (k <= 0.02f || !label || !*label) return;
  enum { PILL_H = 48 };
  int tw = almanacTextWidth(label);
  int w = tw + 64;
  int x = 240 - w / 2, y = cy - PILL_H / 2;
  uint16_t fill = pgDim(AL_INK, k);
  c->fillRoundRect(x, y, w, PILL_H, PILL_H / 2, fill);
  c->drawRoundRect(x, y, w, PILL_H, PILL_H / 2, pgDim(AL_INKBRD, k));
  almanacPrint(c, 240 - tw / 2, cy + 9, label, pgDim(AL_WHITE, k), fill);
}

// Which of the three zones a page-frame tap lands in: -1 left, +1 right,
// 0 middle or anywhere else on the page (the card is a toggle too).
int playZoneAt(int px, int py) {
  if (py < BAND_Y || py >= BAND_Y + BAND_H) return 0;
  if (px < 160) return -1;
  if (px >= 320) return +1;
  return 0;
}
