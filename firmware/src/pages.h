// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
#pragma once
#include <Arduino_GFX_Library.h>

// Extra pages shown by placement orientation (face page stays in face.cpp).
// The clock orientation has two flip sides: 播客 on the right
// swipe and 当前播放 on the left, both drawn by drawPlayPage.
enum PageId : uint8_t { PAGE_FACE = 0, PAGE_CLOCK, PAGE_CALENDAR };

void clockSet(uint32_t localEpoch);   // seconds, timezone already applied
bool clockValid();
uint32_t clockEpoch();                // current local seconds, 0 if never set

void drawClockPage(Arduino_Canvas* c, uint32_t t, uint16_t accent);
void drawCalendarPage(Arduino_Canvas* c, uint32_t t, uint16_t accent);


// 播放页: the clock orientation's other flip side. One page,
// two sources — the Mac's "now playing" (host {"t":"np"}) and the board's own
// player (player.h) — with a badge saying which one is on screen.
enum PlaySrc : uint8_t { PSRC_NONE = 0, PSRC_MAC, PSRC_SD };

void drawPlayPage(Arduino_Canvas* c, uint32_t t, uint16_t accent, uint8_t src,
                  uint8_t volume, float volK);
// host {"t":"np"}: on=false clears it. pos/dur in seconds; the page
// interpolates pos locally from the arrival time while play is true.
void npSet(bool on, const char* title, const char* artist, const char* album,
           float pos, float dur, bool play, float rate, const char* app,
           const char* cover);
bool npOn();        // the Mac has something loaded (playing or paused)
bool npPlaying();
void npSnapshot(float* pos, float* dur, bool* play);   // interpolated, for {"t":"plst"}
// tap zones of the compact card's transport band (page frame): -1 prev/-15 s, +1 next/+15 s, 0 toggle
int playZoneAt(int px, int py);

// 钉住 chrome shared by the non-face pages (doc/06「钉住」, any page since
// 2026-09-22): the corner thumbtack the face wears (seat color while lit,
// grey after) and the 1.5 s pill 名牌 that answers the left key's hold.
// Both are drawn in the page's own rotation, last. k = 1 while the pill
// holds, fading to 0 over the last 400 ms; k = 0 draws nothing.
void drawPagePin(Arduino_Canvas* c, bool lit, uint16_t seatColor);
void drawPageBatt(Arduino_Canvas* c, int pct, uint32_t t);   // bottom-left, <20 % only
void drawPillToast(Arduino_Canvas* c, const char* label, float k, int cy = 368);

// 赛博黄历 (this orientation's home page; data pushed daily by host)
void drawAlmanacPage(Arduino_Canvas* c, uint32_t t);
bool almanacValid();

// shared anti-aliased CJK printing (almanac body font) for other screens,
// e.g. the post-voice send bubble on the face page. bg = what the text
// actually sits on (4bpp alpha blends against it). Returns end x / width.
int almanacPrint(Arduino_Canvas* c, int x, int baseline, const char* s,
                 uint16_t fg, uint16_t bg);
int almanacTextWidth(const char* s);
// same, 18 px face (English strings too wide for a fixed box)
void almanacFit(char* out, size_t n, const char* s, int maxW, bool small);   // one line + "…"
bool almanacHasAll(const char* s);        // every glyph drawable (flash or card)
int almanacPrintSmall(Arduino_Canvas* c, int x, int baseline, const char* s,
                      uint16_t fg, uint16_t bg);
int almanacTextWidthSmall(const char* s);
// tiny18 with letter tracking, on black (Claude 多会话 top row)
int almanacPrintSmallTrack(Arduino_Canvas* c, int x, int baseline, const char* s,
                           uint16_t fg, int track);
int almanacTextWidthSmallTrack(const char* s, int track);
void almanacFitSmallTrack(char* out, size_t n, const char* s, int maxW, int track);
bool almanacHasAllSmall(const char* s);   // tiny18 can draw every glyph (flash or card)
void almanacSet(const char* gz, const char* sx, const char* jc, const char* date,
                const char* yi0, const char* yi1, const char* ji0, const char* ji1,
                const char* qian, const char* dir, int sig,
                const char* cn, uint16_t color);
// 五份黄历 (2026-09-22): the host's line
// carries up to four alternate readings (`alt`); the page cycles pick 0 (the
// day's own draw) .. n with an up/down swipe. Only 宜/忌/签文 change — 干支,
// 属相, 执日, 方位 and the signal are the day's. A message is parsed as
// almanacAltClear -> almanacSet -> almanacAltAdd×n -> almanacCommit; commit
// keeps the pick only while every word is identical (a 10-min re-push of the
// same day must not move what the user is reading), else back to 0.
constexpr int ALMANAC_ALT_MAX = 4;
void almanacAltClear();
void almanacAltAdd(const char* yi0, const char* yi1, const char* ji0, const char* ji1,
                   const char* qian);
void almanacCommit();
int  almanacAltCount();
int  almanacPick();                 // 0 = own draw, 1..n = alternates
int  almanacPickStep(int dir);      // +1 / -1, wraps; returns the new pick
