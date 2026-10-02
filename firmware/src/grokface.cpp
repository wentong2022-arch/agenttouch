// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
// grok skin engine — see grokface.h for the shape of the thing.
#include "grokface.h"
#include "config.h"
#include "grok_eyes.h"   // GROK_EYES[25][2][96], 1/4 px screen coords
#include <math.h>

static const uint16_t GROK_WHITE = C565(0xF4, 0xF6, 0xF8);
static const uint16_t GROK_SLEEP = C565(0x8A, 0x91, 0x9A);   // off: dim grey
static const uint16_t GROK_GONE  = C565(0x6A, 0x74, 0x80);   // offline: greyer

// ---------------------------------------------------------------- state table
// Transcribed from Main.dc.html CFG (third revision, user-approved). Pool
// entries are GrokBot expression numbers = GROK_EYES indices; the seven round
// eyeball shapes (3 21 9 18 12 6 24) are deliberately absent.
struct GrokCfg {
  uint8_t  pool[3];
  uint8_t  nPool;
  uint16_t swMin, swMax;    // expression change cadence, ms
  uint16_t blMin, blMax;    // blink cadence, ms; 0 0 = never blinks
  uint16_t col;
  uint8_t  drift10;         // gaze drift amplitude x10
  uint8_t  dart;            // 1 = working saccade
  uint8_t  hop;             // 1 = done bounce
  int8_t   dy;              // whole-face y offset, px (needs_you clears the approve bubble)
};
static const GrokCfg GCFG[GK_N] = {
  /* off       */ {{13, 22, 4}, 3, 6000, 10000,    0,     0, GROK_SLEEP,  0, 0, 0,   0},
  /* idle      */ {{10,  1,19}, 3, 8000, 15000, 5000, 12000, GROK_WHITE, 10, 0, 0,   0},
  /* working   */ {{13,  4,22}, 3, 2200,  4000,    0,     0, GROK_WHITE,  4, 1, 0,   0},
  /* needs_you */ {{20,  1, 0}, 2, 1600,  2800, 4000,  7500, GROK_WHITE,  6, 0, 0, -56},
  /* done      */ {{ 2, 17,11}, 3, 1200,  2200,    0,     0, GROK_WHITE,  3, 0, 1,   0},
  /* listening */ {{ 0,  8, 0}, 2, 2500,  4500, 4500,  9000, GROK_WHITE,  5, 0, 0,   0},
  /* bored     */ {{ 5, 23,14}, 3, 6000, 12000, 7000, 15000, GROK_WHITE, 12, 0, 0,   0},
  /* surprised */ {{20,  1, 0}, 2,  900,  1600, 1200,  3000, GROK_WHITE,  0, 0, 0,   0},
  /* petting   */ {{ 2, 17, 0}, 2, 1500,  3000,    0,     0, GROK_WHITE,  2, 0, 0,   0},
  /* offline   */ {{16,  7, 0}, 2, 5000,  9000, 6000, 12000, GROK_GONE,   2, 0, 0,   0},
};

static uint32_t s_rng = 0;
static uint32_t rnd32() {
  if (!s_rng) s_rng = (millis() ^ 0xA5C3F17Bu) | 1u;
  s_rng = s_rng * 1664525u + 1013904223u;
  return s_rng >> 8;
}
static uint32_t rndMs(uint32_t a, uint32_t b) {
  return b <= a ? a : a + rnd32() % (b - a + 1);
}

// ------------------------------------------------------------------ blinking
// One scheduler for every skin: the五 pill skins press their upper lid, grok
// scales around the eye's centroid, but the rhythm and the curve are shared.
float petBlinkTick(uint8_t st, bool legacy, uint32_t t) {
  static uint8_t  last = 0xFF;
  static uint32_t nextAt = 0, startAt = 0;
  static bool     running = false;

  if (st >= GK_N) st = GK_IDLE;
  const GrokCfg& g = GCFG[st];
  uint16_t lo = g.blMin, hi = g.blMax;
  // the pill skins keep blinking while working (their eyes are tall enough
  // to show it); grok's working eyes are thin lines, so it does not.
  if (legacy && st == GK_WORKING) { lo = 3000; hi = 6000; }

  if (st != last) {   // entering a state: blink once soon, never look frozen
    last = st;
    nextAt = t + (lo ? rndMs(lo * 2 / 5, hi * 3 / 5) : 0);
  }
  // a state that never blinks still lets a blink already in flight finish
  if (!lo && !running) return 0.0f;

  if (!running && lo && (int32_t)(t - nextAt) >= 0) {
    running = true;
    startAt = t;
    nextAt = t + rndMs(lo, hi);
  }
  if (running) {
    int32_t p = (int32_t)(t - startAt);
    if (p >= 320) { running = false; return 0.0f; }
    return p < 134 ? p / 134.0f : 1.0f - (p - 134) / 186.0f;
  }
  return 0.0f;
}

// ------------------------------------------------------------- morph engine
static float    s_disp[2][96];      // what is on screen right now, 1/4 px
static float    s_src[2][96];       // frame the current morph started from
static int8_t   s_cur = -1;         // expression being morphed toward
static float    s_pos = 1, s_vel = 0;
static uint8_t  s_state = 0xFF;
static uint32_t s_nextSwitch = 0, s_lastMs = 0;

// Read-only view of the cadence table for the pill skins' variant pool.
void petSwitchCadence(uint8_t st, uint16_t* lo, uint16_t* hi) {
  if (st >= GK_N) st = GK_IDLE;
  *lo = GCFG[st].swMin;
  *hi = GCFG[st].swMax;
}

void grokResetEngine() {
  s_cur = -1;
  s_state = 0xFF;
  s_pos = 1;
  s_vel = 0;
}

static void setExpr(uint8_t i, uint32_t t) {
  if ((int8_t)i == s_cur) return;
  if (s_cur < 0) {                     // cold start: pop straight to the shape
    for (int e = 0; e < 2; e++)
      for (int k = 0; k < 96; k++) s_disp[e][k] = s_src[e][k] = GROK_EYES[i][e][k];
    s_cur = (int8_t)i;
    s_pos = 1; s_vel = 0;
    return;
  }
  for (int e = 0; e < 2; e++)
    for (int k = 0; k < 96; k++) s_src[e][k] = s_disp[e][k];
  s_cur = (int8_t)i;
  s_pos = 0; s_vel = 0;
}

// ------------------------------------------------------------ polygon filler
// Arduino_GFX has no fillPolygon: even-odd scanline fill, one drawFastHLine
// per span (the canvas clips and handles rotation for us).
static void fillPoly(Arduino_Canvas* c, const float* X, const float* Y, int n,
                     uint16_t col) {
  float ymin = Y[0], ymax = Y[0];
  for (int i = 1; i < n; i++) {
    if (Y[i] < ymin) ymin = Y[i];
    if (Y[i] > ymax) ymax = Y[i];
  }
  int y0 = (int)ceilf(ymin - 0.5f), y1 = (int)floorf(ymax - 0.5f);
  if (y0 < 0) y0 = 0;
  if (y1 > 479) y1 = 479;
  for (int y = y0; y <= y1; y++) {
    float yc = y + 0.5f;
    float xs[12];
    int m = 0;
    for (int i = 0, j = n - 1; i < n; j = i++) {
      float yi = Y[i], yj = Y[j];
      if ((yi <= yc && yj > yc) || (yj <= yc && yi > yc)) {
        if (m < 12) xs[m++] = X[i] + (yc - yi) * (X[j] - X[i]) / (yj - yi);
      }
    }
    for (int a = 1; a < m; a++) {          // insertion sort, m is 2..6 here
      float v = xs[a];
      int b = a - 1;
      while (b >= 0 && xs[b] > v) { xs[b + 1] = xs[b]; b--; }
      xs[b + 1] = v;
    }
    for (int a = 0; a + 1 < m; a += 2) {
      int xl = (int)ceilf(xs[a] - 0.5f), xr = (int)floorf(xs[a + 1] - 0.5f);
      if (xl < 0) xl = 0;
      if (xr > 479) xr = 479;
      if (xr >= xl) c->drawFastHLine(xl, y, xr - xl + 1, col);
    }
  }
}

// ----------------------------------------------------------------- the frame
void grokDrawEyes(Arduino_Canvas* c, uint8_t st, float blinkK,
                  int8_t lookX, int8_t lookY, uint32_t t) {
  if (st >= GK_N) st = GK_IDLE;
  const GrokCfg& g = GCFG[st];

  if (st != s_state) {                 // enter: first shape of the pool
    s_state = st;
    setExpr(g.pool[0], t);
    s_nextSwitch = t + rndMs(g.swMin, g.swMax);
  } else if (g.nPool > 1 && (int32_t)(t - s_nextSwitch) >= 0) {
    uint8_t i;
    do { i = g.pool[rnd32() % g.nPool]; } while ((int8_t)i == s_cur);
    setExpr(i, t);
    s_nextSwitch = t + rndMs(g.swMin, g.swMax);
  }

  // damped spring on the 0..1 morph progress (w = 2 pi 3.5 Hz, zeta = 0.72,
  // ~280 ms with a little overshoot = the "spring" of it). Sub-stepped at
  // 16 ms: plain Euler at one 50 ms step diverges for this w.
  int32_t dtMs = (int32_t)(t - s_lastMs);
  s_lastMs = t;
  if (dtMs < 0 || dtMs > 50) dtMs = 50;
  if (s_pos < 0.999f || fabsf(s_vel) > 0.001f) {
    const float w = 21.9911f, z = 0.72f;
    int steps = dtMs / 16 + 1;
    float h = (dtMs / 1000.0f) / steps;
    for (int i = 0; i < steps; i++) {
      float acc = w * w * (1.0f - s_pos) - 2 * z * w * s_vel;
      s_vel += acc * h;
      s_pos += s_vel * h;
    }
    if (s_pos > 0.999f && fabsf(s_vel) < 0.05f) { s_pos = 1; s_vel = 0; }
    const int16_t* tg0 = GROK_EYES[s_cur][0];
    const int16_t* tg1 = GROK_EYES[s_cur][1];
    for (int k = 0; k < 96; k++) {
      s_disp[0][k] = s_src[0][k] + (tg0[k] - s_src[0][k]) * s_pos;
      s_disp[1][k] = s_src[1][k] + (tg1[k] - s_src[1][k]) * s_pos;
    }
  }

  // gaze: two slow sines + the working saccade + the done hop,
  // with 眼神追声 (lookX/lookY) added straight on top.
  float drift = g.drift10 / 10.0f, s = t / 1000.0f;
  float gx = drift * (14 * sinf(s * 0.37f) + 6 * sinf(s * 0.91f));
  float gy = drift * 8 * sinf(s * 0.53f + 1.3f);
  if (g.dart) {
    float ph = (t % 2400) / 2400.0f;
    gx += ph < 0.12f ? -18.0f : (ph < 0.30f ? 14.0f : 0.0f);
  }
  if (g.hop) gy -= 10 * fabsf(sinf(3.14159265f * (t % 2000) / 2000.0f));
  gx += lookX;
  gy += lookY + g.dy;   // needs_you: 20 spans y 202-366, bubble starts 316 (真机 2026-09-13)

  float sy = 1.0f - 0.96f * blinkK;      // blink = scale about the centroid
  for (int e = 0; e < 2; e++) {
    const float* p = s_disp[e];
    float cy = 0;
    for (int k = 1; k < 96; k += 2) cy += p[k];
    cy /= 48.0f;
    float X[48], Y[48];
    for (int k = 0; k < 48; k++) {
      X[k] = p[k * 2] * 0.25f + gx;
      Y[k] = (cy + (p[k * 2 + 1] - cy) * sy) * 0.25f + gy;
    }
    fillPoly(c, X, Y, 48, g.col);
  }
}

// Settings-card tile icon (SettingsCard.dc.html, sixth tile): two small
// slanted eyes, points relative to the tile's icon center.
void grokMiniEyes(Arduino_Canvas* c, int cx, int cy, uint16_t col) {
  static const int8_t L[6][2] = {{-11,-10}, {-4,-12}, {0,-6}, {-3,8}, {-10,10}, {-14,4}};
  static const int8_t R[6][2] = {{5,-14}, {12,-15}, {15,-9}, {13,4}, {6,6}, {2,0}};
  float X[6], Y[6];
  for (int e = 0; e < 2; e++) {
    const int8_t (*P)[2] = e ? R : L;
    for (int i = 0; i < 6; i++) { X[i] = cx + P[i][0]; Y[i] = cy + P[i][1]; }
    fillPoly(c, X, Y, 6, col);
  }
}
