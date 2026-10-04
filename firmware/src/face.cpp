// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
// AgentTouch face renderer — "E" style: pure-black bg + minimal white eyes.
// Emotion = eyelid cuts into the eye silhouette; the eye pair's BOTTOM edge
// is pinned at EYE_BOTTOM so state changes only move the upper lid.
// Motion (saccades / blink / breathing bob) sells the states; props (typing
// dots, "?", sweat) are accents only.
// Spec: design/EFaces.dc.html (states) + design/EWorkMotion.dc.html (timing).
#include "face.h"
#include "grokface.h"   // 第六套皮肤 grok: polygon eyes + shared blink clock
#include "pages.h"   // almanacPrint: shared AA CJK text (send bubble)
#include "i18n.h"
#include "pins.h"
#include "pet_fonts.h"
#include <math.h>

static const uint16_t BG      = 0x0000;                  // pure black = AMOLED off
static const uint16_t EYE     = C565(0xF4, 0xF6, 0xF8);
static const uint16_t GREYTXT = C565(0x8A, 0x91, 0x9A);
static const uint16_t GREYEYE = C565(0x6A, 0x74, 0x80);
static const uint16_t GREYDIM = C565(0x4A, 0x52, 0x5C);
static const uint16_t MICBAR  = C565(0xCF, 0xD6, 0xDD);
static const uint16_t SWEAT   = C565(0xDB, 0xE9, 0xF7);
static const uint16_t PANEL   = C565(0x14, 0x16, 0x1C);
static const uint16_t ZDARK   = C565(0x3F, 0x46, 0x50);
static const uint16_t ZLIGHT  = C565(0x5B, 0x64, 0x70);
static const uint16_t BATTRED = C565(0xFF, 0x5A, 0x4A);

// Eye pair geometry (round variant): centers symmetric about x=240,
// bottom edge fixed — lids press down from above.
static const int EYE_LX = 168, EYE_RX = 312;
static const int EYE_BOTTOM = 271;
static const int EYE_W = 92;

// Scale an RGB565 color by f (0..1)
static uint16_t dim565(uint16_t c, float f) {
  if (f > 1) f = 1; if (f < 0) f = 0;
  uint8_t r = ((c >> 11) & 0x1F) * f;
  uint8_t g = ((c >> 5) & 0x3F) * f;
  uint8_t b = (c & 0x1F) * f;
  return (r << 11) | (g << 5) | b;
}

// Tracked type in the u8g2 fonts (same helpers as pages.cpp): print with
// extra letterspacing / measure by invisible draw above the canvas.
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

static int trackWidth(Arduino_Canvas* c, const char* s, int track) {
  return trackPrint(c, 0, -200, s, track, BG);
}

// Symmetric pill eye, bottom pinned. Full-open (h==w) is a circle.
// Skins: eye geometry + head props vary, the state machinery doesn't.
static uint8_t s_skin = SKIN_CLASSIC;
static const char* const SKIN_NAMES[N_SKINS] = {"classic", "kitty", "robo",
                                                "bunny", "sprout", "grok"};
static const uint16_t PINK  = C565(0xFF, 0xA8, 0xC0);  // bunny inner ear
static const uint16_t GREEN = C565(0x3D, 0xDC, 0x84);  // sprout shoot
static void faceResetVariants();   // pill-skin expression pool, defined below
void faceSetSkin(uint8_t id) {
  // main re-asserts the seat's skin every frame, so only a real change may
  // reset grok's morph/cadence state (and the pill skins' variant pool).
  id %= N_SKINS;
  if (id == s_skin) return;
  s_skin = id;
  grokResetEngine();
  faceResetVariants();
}
uint8_t faceSkin()              { return s_skin; }
const char* faceSkinName(uint8_t id) { return SKIN_NAMES[id % N_SKINS]; }

// Robo pixel shine: a dark notch that slowly drifts around the visible eye
// box, with a glitch-hop to the far corner every ~4.2 s (pixels refreshing).
// Called from BOTH eye primitives so idle's lidded eyes keep it too.
// 眼神追声: the frame's eye offset, kept here so the robo
// shine (called from the primitives, no frame in hand) can act as a pupil.
static int8_t s_lookX = 0, s_lookY = 0;

static void drawRoboShine(Arduino_Canvas* c, int left, int top, int w, int h) {
  if (s_skin != SKIN_ROBO || w < 50 || h < 36) return;
  int s = h >= 56 ? 17 : 13;
  uint32_t tt = millis();
  int nx, ny;
  if (tt % 4200 < 160) {
    nx = left + w - s - 12;
    ny = top + h - s - 12;
  } else {
    nx = left + 12 + (int)(6 * sinf(tt * 0.0011f));
    ny = top + 10 + (int)(5 * sinf(tt * 0.0017f + 1.3f));
  }
  // While the pet is looking at a sound the notch stops being a corner
  // highlight and becomes a pupil: it drops to the eye's middle row and
  // slides toward the sound (user 2026-09-09: "the black pupil always sits
  // on the left" — with a corner highlight only the box shift showed).
  float k = fabsf(s_lookX) / 32.0f, ky = fabsf(s_lookY) / 10.0f;
  if (ky > k) k = ky;
  if (k > 1) k = 1;
  if (k > 0.01f) {
    int px = left + (w - s) / 2 + s_lookX * (w - s - 24) / 64;
    int py = top + (h - s) / 2 + s_lookY * (h - s - 16) / 20;
    nx += (int)((px - nx) * k);
    ny += (int)((py - ny) * k);
  }
  c->fillRoundRect(nx, ny, s, s, 4, BG);
}

static void drawEyePill(Arduino_Canvas* c, int cx, int w, int h, int dx, int dy,
                        uint16_t col) {
  if (h < 8) h = 8;
  // robo gets boxy eyes; everyone else keeps the round pill
  int r = min(w, h) / (s_skin == SKIN_ROBO ? 6 : 2);
  c->fillRoundRect(cx - w / 2 + dx, EYE_BOTTOM + dy - h, w, h, r, col);
  drawRoboShine(c, cx - w / 2 + dx, EYE_BOTTOM + dy - h, w, h);
}

// Eye pressed by a flat upper lid: keeps full width with a big-radius
// bottom (the mock's 10/32 corner mix), instead of a narrowing half-circle.
static void drawEyeLidded(Arduino_Canvas* c, int cx, int openH, int dy,
                          uint16_t col) {
  if (openH < 8) openH = 8;
  int bottom = EYE_BOTTOM + dy;
  int h = openH + 24;                  // headroom so the bottom radius survives
  int r = min(s_skin == SKIN_ROBO ? 12 : 32, min(EYE_W, h) / 2);
  c->fillRoundRect(cx - EYE_W / 2, bottom - h, EYE_W, h, r, col);
  c->fillRect(cx - EYE_W / 2 - 1, bottom - h - 1, EYE_W + 2, 25, BG);
  drawRoboShine(c, cx - EYE_W / 2, bottom - openH, EYE_W, openH);
}

// Rounded bar rotated by deg (for the droopy offline eyes).
static void drawRotatedPill(Arduino_Canvas* c, float cx, float cy, float len,
                            float thick, float deg, uint16_t col) {
  float a = deg * 0.0174533f, ca = cosf(a), sa = sinf(a);
  float half = (len - thick) / 2, r = thick / 2;
  float x0 = cx - half * ca, y0 = cy - half * sa;
  float x1 = cx + half * ca, y1 = cy + half * sa;
  float px = -sa * r, py = ca * r;
  c->fillTriangle((int)(x0 + px), (int)(y0 + py), (int)(x0 - px), (int)(y0 - py),
                  (int)(x1 + px), (int)(y1 + py), col);
  c->fillTriangle((int)(x0 - px), (int)(y0 - py), (int)(x1 - px), (int)(y1 - py),
                  (int)(x1 + px), (int)(y1 + py), col);
  c->fillCircle((int)x0, (int)y0, (int)r, col);
  c->fillCircle((int)x1, (int)y1, (int)r, col);
}

// Bubble label at (x, base): the 26 px face when it fits the text slot,
// else the 18 px one (English "Approve" is 120 px at 26 px; the slot right
// of the glyph is ~86 px). Chinese labels always fit, so the
// zh path draws exactly what it did.
static void bubbleLabel(Arduino_Canvas* c, int x, int base, const char* s,
                        uint16_t fg, uint16_t bg) {
  static const int SLOT_W = 86;
  if (almanacTextWidth(s) <= SLOT_W) almanacPrint(c, x, base, s, fg, bg);
  else   // 10 px further left: 84 px "Approve" then ends ~11 px inside the round end
    almanacPrintSmall(c, x - 10, base - 3, s, fg, bg);
}

// Post-voice send bubble: after hold-to-talk ends, tapping the pet presses
// Return on the Mac (main routes the tap; this only draws). ~6 s lifetime.
static void drawSendBubble(Arduino_Canvas* c, const FaceFrame& f) {
  float k = f.sendK;
  int w = 158, x = 240 - w / 2, y = 316, h = 50;
  uint16_t panel = dim565(PANEL, 0.6f + 0.4f * k);
  c->fillRoundRect(x, y, w, h, 25, panel);
  c->drawRoundRect(x, y, w, h, 25, dim565(PAGE_ACCENT, 0.8f * k));
  // return-arrow glyph: riser, shaft, left-pointing head
  uint16_t arr = dim565(PAGE_ACCENT, k);
  int ax = x + 34, ay = y + h / 2;
  c->fillRect(ax + 16, ay - 12, 3, 11, arr);
  c->fillRect(ax, ay - 2, 19, 3, arr);
  c->fillTriangle(ax - 7, ay, ax + 2, ay - 7, ax + 2, ay + 7, arr);
  bubbleLabel(c, x + 72, y + 34, tr(S_SEND), dim565(EYE, k), panel);
}

// needs_you decision bubble: while the selected agent waits on an approval,
// tap = approve, long-press = reject (main routes the touch; this only
// draws). The post-voice send bubble wins the slot when both are up.
static void drawApproveBubble(Arduino_Canvas* c, uint32_t t) {
  int w = 158, x = 240 - w / 2, y = 316, h = 50;
  float k = 0.75f + 0.25f * sinf(t * 6.2832f / 1800.0f);   // breathe with the dot
  c->fillRoundRect(x, y, w, h, 25, PANEL);
  c->drawRoundRect(x, y, w, h, 25, dim565(GREEN, k));
  uint16_t tick = dim565(GREEN, k);
  int ax = x + 38, ay = y + h / 2;
  drawRotatedPill(c, ax - 4, ay + 2, 17, 6, 45, tick);
  drawRotatedPill(c, ax + 7, ay - 1, 26, 6, -45, tick);
  bubbleLabel(c, x + 72, y + 34, tr(S_APPROVE), EYE, PANEL);
  const char* hint = tr(S_HOLD_REJECT);
  if (langEn())            // English hint: 18 px, centred
    almanacPrintSmall(c, 240 - almanacTextWidthSmall(hint) / 2, y + h + 28, hint,
                      dim565(GREYTXT, 0.85f), BG);
  else
    almanacPrint(c, 240 - almanacTextWidth(hint) / 2, y + h + 28, hint,
                 dim565(GREYTXT, 0.85f), BG);
}

// Head props per skin, drawn under the eyes so every state keeps its outfit.
static void drawSkinProps(Arduino_Canvas* c, uint32_t t) {
  switch (s_skin) {
    case SKIN_KITTY: {
      // two tilted triangle ears + three whiskers per cheek
      c->fillTriangle(EYE_LX - 44, 152, EYE_LX + 14, 148, EYE_LX - 22, 84, EYE);
      c->fillTriangle(EYE_RX + 44, 152, EYE_RX - 14, 148, EYE_RX + 22, 84, EYE);
      for (int i = 0; i < 3; i++) {
        int y = 234 + i * 18;
        c->drawLine(52, y + 6, 108, y, GREYTXT);
        c->drawLine(372, y, 428, y + 6, GREYTXT);
      }
      break;
    }
    // SKIN_ROBO has no head props: the whole robot lives in the boxy eyes
    // and their drifting pixel shine (drawn inside drawEyePill).
    case SKIN_BUNNY: {
      // one ear straight (pink pad), one folded with the tip flopping
      // outward — coordinates lifted from the 换装间 canvas mock
      drawRotatedPill(c, 158, 103, 148, 36, 82, EYE);
      drawRotatedPill(c, 158, 101, 96, 16, 82, PINK);
      drawRotatedPill(c, 318, 130, 92, 36, 98, EYE);
      drawRotatedPill(c, 332, 91, 62, 34, 168, EYE);
      drawRotatedPill(c, 332, 92, 40, 16, 168, PINK);
      break;
    }
    case SKIN_SPROUT: {
      // a green shoot: two stem segments in a slight S, two leaf pills
      drawRotatedPill(c, 239, 138, 34, 9, 96, GREEN);
      drawRotatedPill(c, 241, 116, 30, 9, 84, GREEN);
      drawRotatedPill(c, 214, 98, 48, 27, -35, GREEN);
      drawRotatedPill(c, 266, 98, 48, 27, 35, GREEN);
      break;
    }
  }
}

// Growth profile card (double-tap): level + XP bar + the three stats that
// feed it. Tap again to dismiss — skin picking moved to the settings card
// after playful pokes kept cycling skins by accident (user report, day 9).
static void drawProfile(Arduino_Canvas* c, const FaceFrame& f) {
  float k = f.profK;
  c->fillRoundRect(70, 300, 340, 162, 24, dim565(PANEL, 0.5f + 0.5f * k));
  c->drawRoundRect(70, 300, 340, 162, 24, dim565(GREYDIM, k));

  c->setTextSize(1);
  c->setFont(u8g2_font_fur25_tr);
  char lv[12];
  snprintf(lv, sizeof(lv), "LV %d", f.level);
  trackPrint(c, 100, 348, lv, 3, dim565(EYE, k));
  c->setFont(u8g2_font_fur17_tr);
  const char* sk = SKIN_NAMES[s_skin];
  trackPrint(c, 380 - trackWidth(c, sk, 3), 344, sk, 3,
             dim565(PAGE_ACCENT, k));

  // XP progress toward the next level
  c->fillRoundRect(100, 362, 280, 10, 5, dim565(ZDARK, k));
  int fw = 280 * f.xpPct / 100;
  if (fw > 6) c->fillRoundRect(100, 362, fw, 10, 5, dim565(PAGE_ACCENT, k));

  // three stat columns: label over value
  static const char* lbl[3] = {"done", "days", "pets"};
  uint16_t val[3] = {f.statDone, f.statDays, f.statPets};
  for (int i = 0; i < 3; i++) {
    int cx = 127 + i * 113;
    trackPrint(c, cx - trackWidth(c, lbl[i], 3) / 2, 408, lbl[i], 3,
               dim565(GREYTXT, k));
    char v[8];
    snprintf(v, sizeof(v), "%u", val[i]);
    trackPrint(c, cx - trackWidth(c, v, 3) / 2, 442, v, 3, dim565(EYE, k));
  }
  c->setFont((const GFXfont*)nullptr);
}

// Settings card (hold the face ~1.5 s): the current seat's skin picked by a
// direct tap on one of five mini-pet dots (no more blind cycling), three
// brightness levels, and a read-only status line. Sits in the bottom half so
// the pet previews its new outfit live above the card. Layout must match
// faceSettingsHit below — main routes the taps, this only draws.
// Tile order = the DEFAULT owners' seat order with the spare skin last, so
// the badges line up with the face page's agent dots out of the box;
// it is a fixed display order (swaps must never make tiles jump around).
// Six tiles since 2026-09-13 (grok): 50 px each on a 57 px pitch from x=110.
static const uint8_t SKIN_TILE_ORDER[N_SKINS] = {
    SKIN_CLASSIC, SKIN_ROBO, SKIN_KITTY, SKIN_SPROUT, SKIN_GROK, SKIN_BUNNY};
static const int TILE_X0 = 110, TILE_PITCH = 57, TILE_Y = 304, TILE_SZ = 50;
static void drawSettingsCard(Arduino_Canvas* c, const FaceFrame& f) {
  float k = f.setK;
  uint16_t accent = AGENTS[f.agentIdx].color;
  c->fillRoundRect(24, 252, 432, 220, 24, dim565(PANEL, 0.5f + 0.5f * k));
  c->drawRoundRect(24, 252, 432, 220, 24, dim565(GREYDIM, k));

  // header: seat dot + name — which pet this card is dressing
  c->setTextSize(1);
  c->setFont(u8g2_font_fur17_tr);
  const char* name = AGENTS[f.agentIdx].name;
  int wn = trackWidth(c, name, 3);
  int hx = 240 - (wn + 18) / 2;
  c->fillCircle(hx + 4, 280, 5, dim565(accent, k));
  trackPrint(c, hx + 18, 286, name, 3, dim565(GREYTXT, k));

  // skin row: AA label + six wardrobe tiles. Each tile shows that skin's
  // mini EYE-PAIR (the big face's design language, not a blob with props);
  // the worn one gets a 2 px seat-color border, skins worn by other seats
  // carry their owner's color badge (tapping one swaps outfits).
  static const uint16_t TILE = C565(0x1E, 0x22, 0x2A);
  almanacPrint(c, 44, 342, tr(S_SKIN), dim565(GREYTXT, k), PANEL);
  for (int t = 0; t < N_SKINS; t++) {
    uint8_t i = SKIN_TILE_ORDER[t];
    int tx = TILE_X0 + t * TILE_PITCH, ty = TILE_Y;
    bool sel = i == s_skin;
    c->fillRoundRect(tx, ty, TILE_SZ, TILE_SZ, 10, dim565(TILE, k));
    if (sel) {
      c->drawRoundRect(tx, ty, TILE_SZ, TILE_SZ, 10, dim565(accent, k));
      c->drawRoundRect(tx + 1, ty + 1, TILE_SZ - 2, TILE_SZ - 2, 9,
                       dim565(accent, k));
    }
    int cx = tx + 25, cy = ty + 28;
    uint16_t body = dim565(EYE, (sel ? 1.0f : 0.62f) * k);
    if (i == SKIN_ROBO) {                        // boxy eyes + shine notch
      c->fillRoundRect(cx - 13, cy - 6, 12, 12, 3, body);
      c->fillRoundRect(cx + 1, cy - 6, 12, 12, 3, body);
      c->fillRect(cx - 10, cy - 3, 4, 4, dim565(TILE, k));
    } else if (i == SKIN_GROK) {                 // two small slanted eyes
      grokMiniEyes(c, cx, cy, body);
    } else {                                     // round pill eyes
      c->fillRoundRect(cx - 12, cy - 7, 9, 15, 4, body);
      c->fillRoundRect(cx + 3, cy - 7, 9, 15, 4, body);
    }
    switch (i) {
      case SKIN_KITTY:
        c->fillTriangle(cx - 15, cy - 7, cx - 4, cy - 11, cx - 13, cy - 21, body);
        c->fillTriangle(cx + 15, cy - 7, cx + 4, cy - 11, cx + 13, cy - 21, body);
        break;
      case SKIN_BUNNY:
        c->fillRoundRect(cx - 12, cy - 22, 6, 16, 3, body);   // upright ear
        c->fillRoundRect(cx + 3, cy - 19, 12, 6, 3, body);    // folded ear
        break;
      case SKIN_SPROUT:
        c->fillRect(cx - 1, cy - 20, 3, 8, dim565(GREEN, k));
        c->fillCircle(cx + 4, cy - 19, 4, dim565(GREEN, k));
        break;
    }
    for (int j = 0; j < N_AGENTS; j++) {         // owner badge (other seat)
      if (j != f.agentIdx && f.skinBy[j] == i) {
        c->fillCircle(tx + 45, ty + 9, 5, dim565(AGENTS[j].color, k));
        break;
      }
    }
  }

  // brightness row: AA label + three bars, tap to pick
  almanacPrint(c, 44, 414, tr(S_LIGHT), dim565(GREYTXT, k), PANEL);
  for (int i = 0; i < 3; i++) {
    int bx = 208 + i * 70, h = 12 + 8 * i;
    bool sel = i == f.setBright;
    c->fillRoundRect(bx, 420 - h, 40, h, 6,
                     dim565(sel ? EYE : GREYDIM, (sel ? 1.0f : 0.6f) * k));
  }

  // read-only status, two lines: link / IP / battery, then SD / firmware
  int sw = trackWidth(c, f.setStatus, 2);
  trackPrint(c, 240 - sw / 2, 442, f.setStatus, 2, dim565(GREYTXT, k));
  sw = trackWidth(c, f.setStatus2, 2);
  trackPrint(c, 240 - sw / 2, 464, f.setStatus2, 2, dim565(GREYTXT, 0.75f * k));
  c->setFont((const GFXfont*)nullptr);
}

// Claim card (variant A 底卡): the settings
// card's panel lowered to y=284 so the needs_you stare above stays whole.
// Only the green button says yes; the rest of the card is a no-op band.
static void drawClaimCard(Arduino_Canvas* c, const FaceFrame& f, uint32_t t) {
  static const uint16_t TILE = C565(0x1E, 0x22, 0x2A);
  float k = f.claimK;
  uint16_t panel = dim565(PANEL, 0.5f + 0.5f * k);
  c->fillRoundRect(24, 284, 432, 188, 24, panel);
  c->drawRoundRect(24, 284, 432, 188, 24, dim565(GREYDIM, k));
  const char* q = tr(S_CLAIM_Q);
  almanacPrint(c, 240 - almanacTextWidth(q) / 2, 320, q, dim565(GREYTXT, k), panel);
  if (f.claimName)
    almanacPrint(c, 240 - almanacTextWidth(f.claimName) / 2, 356, f.claimName, dim565(EYE, k), panel);
  if (f.claimSub)
    almanacPrintSmall(c, 240 - almanacTextWidthSmall(f.claimSub) / 2, 384, f.claimSub,
                      dim565(GREYTXT, k), panel);
  // button: tile + 2 px breathing green ring; accepted = solid green, black ink
  bool ok = f.claimDone;
  float br = ok ? 1.0f : 0.75f + 0.25f * sinf(t * 6.2832f / 1800.0f);
  uint16_t fill = ok ? dim565(GREEN, k) : dim565(TILE, k);
  c->fillRoundRect(120, 400, 240, 56, 28, fill);
  if (!ok) {
    c->drawRoundRect(120, 400, 240, 56, 28, dim565(GREEN, br * k));
    c->drawRoundRect(121, 401, 238, 54, 27, dim565(GREEN, br * k));
  }
  const char* lab = tr(ok ? S_CLAIM_DONE : S_CLAIM_OK);
  int gw = 30 + 14 + almanacTextWidth(lab), gx = 240 - gw / 2;
  uint16_t tick = ok ? BG : dim565(GREEN, br * k);
  drawRotatedPill(c, gx + 6, 430, 17, 6, 45, tick);
  drawRotatedPill(c, gx + 17, 427, 26, 6, -45, tick);
  almanacPrint(c, gx + 44, 437, lab, ok ? BG : dim565(EYE, k), fill);
  // 30 s countdown: drains right -> left; full green once accepted
  c->fillRoundRect(48, 460, 384, 6, 3, dim565(ZDARK, k));
  int w = ok ? 384 : (int)(384 * f.claimLeft);
  if (w > 6) c->fillRoundRect(48, 460, w, 6, 3, dim565(ok ? GREEN : GREYTXT, k));
}

int faceClaimHit(int x, int y) {
  if (x >= 112 && x <= 368 && y >= 392 && y <= 464) return 1;   // button + 8 px
  if (x >= 24 && x <= 456 && y >= 284 && y <= 472) return 0;
  return -1;
}

int faceSettingsHit(int x, int y) {
  if (x < 24 || x > 456 || y < 252 || y > 472) return -2;
  if (y >= 296 && y <= 362) {
    for (int t = 0; t < N_SKINS; t++)
      if (abs(x - (TILE_X0 + TILE_SZ / 2 + t * TILE_PITCH)) <= 28)
        return SKIN_TILE_ORDER[t];
  }
  if (y >= 380 && y <= 434) {
    for (int i = 0; i < 3; i++)
      if (abs(x - (228 + i * 70)) <= 34) return 10 + i;
  }
  return -1;
}

// 钉住 corner pin (doc/06): a thumbtack in the bottom-right corner — the one
// board entry for "only listen to my hand, not to gravity". Kept tiny (~20 px)
// and far from the eyes so the bare black face survives; the 64 px tap box
// around it lives in main (pinHit). Seat color for 10 s after locking (so the
// tap is unmistakable), then grey forever as the answer to "why is it not
// flipping pages?". Unpinned it only shows in the hand, faint.
static void drawPin(Arduino_Canvas* c, const FaceFrame& f) {
  uint16_t col = f.pinVis == 3 ? AGENTS[f.agentIdx].color
                               : dim565(GREYDIM, f.pinVis == 1 ? 0.6f : 1.0f);
  const int cx = 442, cy = 440;
  c->fillCircle(cx, cy - 6, 7, col);                 // head
  c->fillRect(cx - 9, cy - 1, 18, 4, col);           // collar
  drawRotatedPill(c, cx, cy + 8, 12, 4, 90, col);    // needle, straight down
}

// Volume overlay: phone-style vertical bar on the right edge — ten segments
// filling bottom-up (matches the swipe direction), speaker glyph underneath.
// Muted = red slash, all segments dark. Momentary (1.5 s), summoned by a
// vertical swipe / left-key hold / host set.
static void drawVolume(Arduino_Canvas* c, const FaceFrame& f) {
  float k = f.volK;
  bool muted = f.volume == 0;
  c->fillRoundRect(392, 78, 56, 324, 18, dim565(PANEL, k));
  int fillN = (f.volume + 9) / 10;
  for (int i = 0; i < 10; i++) {          // i=0 is the bottom segment
    bool on = !muted && i < fillN;
    c->fillRoundRect(408, 330 - i * 26, 24, 16, 5,
                     dim565(on ? EYE : GREYDIM, (on ? 1.0f : 0.55f) * k));
  }
  int sx = 409, sy = 375;                 // speaker glyph below the stack
  uint16_t col = dim565(muted ? GREYDIM : EYE, k);
  c->fillRect(sx, sy - 6, 7, 12, col);
  c->fillTriangle(sx + 5, sy, sx + 17, sy - 12, sx + 17, sy + 12, col);
  if (muted)
    drawRotatedPill(c, sx + 9, sy, 38, 6, -45, dim565(BATTRED, k));
}

static float ease(float k) { return k * k * (3 - 2 * k); }

// Working saccade: glance left, hold, glance right, hold, back (3.8 s loop).
static int dartOffset(uint32_t t) {
  float p = (t % 3800) / 3800.0f;
  if (p < 0.18f) return (int)(-14 * ease(p / 0.18f));
  if (p < 0.38f) return -14;
  if (p < 0.52f) return (int)(-14 + 26 * ease((p - 0.38f) / 0.14f));
  if (p < 0.72f) return 12;
  if (p < 0.88f) return (int)(12 - 12 * ease((p - 0.72f) / 0.16f));
  return 0;
}

static const char* stateLabel(const FaceFrame& f) {
  if (f.offline)   return "Link Lost";
  if (f.listening) return "Listening";
  switch (f.st) {
    case ST_WORKING:   return "Working";
    case ST_NEEDS_YOU: return "Needs You";
    case ST_DONE:      return "Done";
    case ST_IDLE:      return "Standby";
    default:           return "Sleeping";
  }
}

// Direction-1 toast (user-picked): the face stays clean; a small bottom line
// "● claude · working" plus one mini dot per seat fades in on changes, and
// stays put while the selected agent needs you. The dots get their own fade
// level so they can stay pinned while a background agent is active without
// pinning the (selected-agent) nameplate text alongside them.
static void drawToast(Arduino_Canvas* c, const FaceFrame& f, uint32_t t,
                      float kText, float kDots) {
  if (kText > 0.03f) {
    uint16_t accent = AGENTS[f.agentIdx].color;
    const char* name = AGENTS[f.agentIdx].name;
    const char* st = stateLabel(f);

    c->setTextSize(1);
    c->setFont(u8g2_font_fur17_tr);
    int wn = trackWidth(c, name, 3), ws = trackWidth(c, st, 3);
    int x = 240 - (8 + 10 + wn + 9 + 6 + 9 + ws) / 2;
    c->fillCircle(x + 4, 418, 4, dim565(accent, kText));
    x += 18;
    trackPrint(c, x, 424, name, 3, dim565(GREYTXT, kText));
    x += wn + 9;
    c->fillCircle(x + 3, 418, 3, dim565(GREYDIM, kText));
    x += 15;
    trackPrint(c, x, 424, st, 3, dim565(GREYTXT, kText));
    c->setFont((const GFXfont*)nullptr);
  }

  // mini seat dots — the board's only seat roster, so 隐藏 = 当前 off 就不画
  // applies here: a seat whose app is not running this
  // instant is left out and the rest re-center. Fixed index order always: a
  // seat that comes back lands in its own place, it is a filter, never a
  // reorder. Two ways all five stay on screen — every seat off (host down,
  // or nothing installed: the picture must not collapse to nothing) and the
  // 「显示离线席位」 setting. The selected seat is always drawn even when off:
  // its pet is filling the whole screen, and a ring with no dot under it
  // would be a hole (host-pushed selects are obeyed unconditionally, so this
  // does happen — reverse follow onto an app the host still calls off).
  // Selected = ring, working = breathing, needs = blink.
  bool allOff = true;
  for (int i = 0; i < N_AGENTS && allOff; i++)
    if (f.agentStates[i] != ST_OFF) allOff = false;
  bool showAll = f.showOff || allOff;
  bool vis[N_AGENTS];
  int nvis = 0;
  for (int i = 0; i < N_AGENTS; i++) {
    vis[i] = showAll || f.agentStates[i] != ST_OFF || i == f.agentIdx;
    if (vis[i]) nvis++;
  }
  float k = kDots;
  int slot = 0;
  for (int i = 0; i < N_AGENTS; i++) {
    if (!vis[i]) continue;
    float lvl;
    switch (f.agentStates[i]) {
      case ST_WORKING:   lvl = 0.75f + 0.25f * sinf(t * 6.2832f / 1800.0f); break;
      case ST_NEEDS_YOU: lvl = (t / 300) % 2 ? 1.0f : 0.3f; break;
      case ST_DONE:      lvl = 0.9f;  break;
      // idle/off lifted 2026-09-05 (user: "only the orange one looks lit"):
      // 45 % of a saturated blue or purple on the black AMOLED reads as off.
      // Working still breathes above, needs still blinks, so state survives.
      case ST_IDLE:      lvl = 0.62f; break;
      default:           lvl = 0.28f; break;
    }
    // centered on 240, 28 px apart, however many are VISIBLE (five land on
    // 184..296, four on 198..282, a lone seat sits dead center on 240)
    int dx = 240 - (nvis - 1) * 14 + slot * 28;
    slot++;
    c->fillCircle(dx, 454, 5, dim565(AGENTS[i].color, lvl * k));
    if (i == f.agentIdx)
      c->drawCircle(dx, 454, 9, dim565(AGENTS[i].color, 0.65f * k));
  }
}

// Battery appears only when low; blinks below 10%. While the 多会话 row is up
// its › (tip x 452, y 39..54 with the stroke) owns the corner, so the icon
// steps down 36 px under it; the row's backing never reaches
// past x 424 (titles are capped at 300 px), so x stays.
static void drawLowBatt(Arduino_Canvas* c, const FaceFrame& f, uint32_t t,
                        bool rowUp) {
  if (f.battPct < 0 || f.battPct >= 20) return;
  if (f.battPct < 10 && (t / 500) % 2) return;
  int y = rowUp ? 64 : 28;
  c->drawRoundRect(432, y, 26, 13, 3, GREYDIM);
  c->fillRect(458, y + 4, 3, 6, GREYDIM);
  int fw = 20 * f.battPct / 20;
  c->fillRoundRect(435, y + 3, max(2, fw), 7, 2, BATTRED);
}

static void drawZz(Arduino_Canvas* c, uint32_t t) {
  float p = (t % 3600) / 3600.0f;
  int rise = (int)(p * 32);
  c->setTextSize(4);
  c->setTextColor(dim565(ZLIGHT, 1.0f - 0.5f * p));
  c->setCursor(346, 132 - rise);
  c->print("z");
  c->setTextSize(3);
  c->setTextColor(dim565(ZDARK, 1.0f - 0.5f * p));
  c->setCursor(378, 100 - rise - rise / 2);
  c->print("z");
}

// Typing indicator: three dots pulsing in sequence (1.3 s, 220 ms stagger).
static void drawTypingDots(Arduino_Canvas* c, uint32_t t, int dy) {
  for (int i = 0; i < 3; i++) {
    uint32_t ph = (t + 13000 - i * 220) % 1300;
    float lvl = 0.25f;
    int lift = 0;
    if (ph < 650) {
      float k = sinf(3.1416f * ph / 650.0f);
      lvl = 0.25f + 0.75f * k;
      lift = (int)(4 * k);
    }
    c->fillCircle(212 + i * 28, 322 + dy - lift, 7, dim565(EYE, lvl));
  }
}

// Sweat easter egg: after 3 min of continuous work, a drop slides down
// every ~25 s (design: RoboEyes-style, not a permanent prop).
static void drawSweat(Arduino_Canvas* c, uint32_t t, uint32_t workT) {
  if ((int32_t)(t - workT) < 180000) return;
  uint32_t ph = (t - workT) % 25000;
  if (ph > 2200) return;
  int y = 130 + (int)(24.0f * ph / 2200.0f);
  float fade = ph > 1600 ? 1.0f - (ph - 1600) / 600.0f : 1.0f;
  uint16_t col = dim565(SWEAT, fade);
  c->fillTriangle(361, y, 355, y + 8, 367, y + 8, col);
  c->fillCircle(361, y + 10, 7, col);
}

// Which mood the eyes are in, in the SAME precedence as the draw chain below
// — the blink clock and (for grok) the expression pool both key off it.
// dizzy/eating/burp/stretch have no entry of their own: those four keep their
// hand-drawn eyes on every skin, so they borrow a cadence.
static uint8_t visualState(const FaceFrame& f) {
  if (f.dizzy)     return GK_IDLE;
  if (f.surprised) return GK_SURPRISED;
  if (f.petting)   return GK_PETTING;
  if (f.eating)    return GK_IDLE;
  if (f.burp)      return GK_DONE;
  if (f.listening) return GK_LISTENING;
  if (f.offline)   return GK_OFFLINE;
  if (f.stretch)   return GK_DONE;
  if (f.bored)     return GK_BORED;
  switch (f.st) {
    case ST_OFF:       return GK_OFF;
    case ST_WORKING:   return GK_WORKING;
    case ST_NEEDS_YOU: return GK_NEEDS;
    case ST_DONE:      return GK_DONE;
    default:           return GK_IDLE;
  }
}

// ------------------------------------------- expression pool (pill skins)
// grok has 2–4 faces per state; the five pill skins used to have
// exactly one. Same idea here, built out of the EXISTING primitives: only
// numbers change (open height, width, baseline, drift/hop amplitude), and a
// switch eases over 180 ms instead of popping. The cadence comes from the one
// table in grokface.cpp (petSwitchCadence) so the two engines can never drift
// apart. Blink, 眼神追声, robo shine, typing dots, sweat, "?" and the bubbles
// all stack on top exactly as before; ST_OFF and stretch keep their single
// face on purpose (sleeping / take-a-break must stay still).
struct EyeVar {
  float wL, hL;   // left eye: pill width / open height
  float wR, hR;   // right eye
  float dy;       // baseline offset from EYE_BOTTOM (done's smile arcs: -25)
  float aux;      // bored = drift amplitude | done = hop | needs = "?"-sync
  float pill;     // done only: 0 = smile arc, 1 = wide "laugh" pill
};

static uint32_t s_vrng = 0;
static uint32_t vrnd() {
  if (!s_vrng) s_vrng = (millis() ^ 0x1F35C9A7u) | 1u;
  s_vrng = s_vrng * 1664525u + 1013904223u;
  return s_vrng >> 8;
}

static uint8_t nVariants(uint8_t st) {
  switch (st) {
    case GK_IDLE: case GK_WORKING: case GK_NEEDS:
    case GK_DONE: case GK_BORED:  return 3;
    default:                      return 1;   // off/listening/surprised/…
  }
}

// V0 = exactly what the face looked like before the pool existed.
// Variant table = lead's 2026-09-19 spec; the "which eye" coin is flipped
// at pick time so the asymmetric versions do not always lean the same way.
static EyeVar variantOf(uint8_t st, uint8_t v) {
  bool left = (vrnd() & 1) != 0;
  switch (st) {
    case GK_IDLE:                                        // lidded, EYE_W wide
      if (v == 1) return {EYE_W, left ? 46.f : 34.f,     // 不对称
                          EYE_W, left ? 34.f : 46.f, 0, 0, 0};
      if (v == 2) return {EYE_W, 30, EYE_W, 30, 0, 0, 0};          // 慢眯
      return {EYE_W, 42, EYE_W, 42, 0, 0, 0};
    case GK_WORKING:                                     // pill + saccade
      if (v == 1) return {EYE_W - 12, 70, EYE_W - 12, 70, 0, 0, 0};  // 专注
      if (v == 2) return {EYE_W, left ? 40.f : 58.f,                 // 琢磨
                          EYE_W, left ? 58.f : 40.f, 0, 0, 0};
      return {EYE_W, 58, EYE_W, 58, 0, 0, 0};
    case GK_NEEDS:                                       // wide stare
      if (v == 1) return {96, 96, 96, 96, 0, 4, 0};      // aux = ride the "?"
      if (v == 2) return {106, 110, 106, 110, -4, 0, 0}; // 略高，仍瞪大
      return {106, 106, 106, 106, 0, 0, 0};
    case GK_DONE:                                        // arc: w = 2*rx, h = ry
      if (v == 1) return {96, 32, 96, 32, -25, 6, 0};
      if (v == 2) return {106, 80, 106, 80, 0, 0, 1};    // 笑开：药丸，无 hop
      return {84, 28, 84, 28, -25, 10, 0};
    case GK_BORED:                                       // heavy lids + drift
      if (v == 1) return {EYE_W, left ? 8.f : 30.f,      // 一眼闭
                          EYE_W, left ? 30.f : 8.f, 0, 10, 0};
      if (v == 2) return {EYE_W, 34, EYE_W, 34, 0, 4, 0};  // 几乎不动
      return {EYE_W, 34, EYE_W, 34, 0, 10, 0};
  }
  return {EYE_W, 42, EYE_W, 42, 0, 0, 0};
}

static EyeVar   s_vDisp = {EYE_W, 42, EYE_W, 42, 0, 0, 0};
static EyeVar   s_vSrc  = {EYE_W, 42, EYE_W, 42, 0, 0, 0};
static EyeVar   s_vDst  = {EYE_W, 42, EYE_W, 42, 0, 0, 0};
static uint8_t  s_vIdx = 0, s_vState = 0xFF;
static uint32_t s_vSwitchAt = 0, s_vMorphAt = 0;

static void faceResetVariants() { s_vState = 0xFF; }

// ms until the next variant. needs_you gets a floor of 3 s: the approve
// bubble is a target the finger aims at, the face above it must stay calm.
static uint32_t vCadence(uint8_t st) {
  uint16_t lo = 6000, hi = 12000;
  petSwitchCadence(st, &lo, &hi);
  if (!lo || hi < lo) { lo = 6000; hi = 12000; }
  if (st == GK_NEEDS) {
    if (lo < 3000) lo = 3000;
    if (hi < lo + 1200) hi = lo + 1200;
  }
  return lo + (hi > lo ? vrnd() % (hi - lo + 1) : 0);
}

static float vlerp(float a, float b, float k) { return a + (b - a) * k; }

static const EyeVar& varTick(uint8_t st, uint32_t t) {
  if (st != s_vState) {               // entering a state: start on V0, no morph
    s_vState = st;
    s_vIdx = 0;
    s_vDisp = s_vSrc = s_vDst = variantOf(st, 0);
    s_vMorphAt = t - 180;
    s_vSwitchAt = t + vCadence(st);
  } else if (nVariants(st) > 1 && (int32_t)(t - s_vSwitchAt) >= 0) {
    uint8_t n = nVariants(st), v;
    do { v = (uint8_t)(vrnd() % n); } while (v == s_vIdx);   // never twice
    s_vIdx = v;
    s_vSrc = s_vDisp;
    s_vDst = variantOf(st, v);
    s_vMorphAt = t;
    s_vSwitchAt = t + vCadence(st);
  }
  int32_t p = (int32_t)(t - s_vMorphAt);
  if (p < 180) {
    float k = ease(p / 180.0f);
    s_vDisp.wL   = vlerp(s_vSrc.wL,   s_vDst.wL,   k);
    s_vDisp.hL   = vlerp(s_vSrc.hL,   s_vDst.hL,   k);
    s_vDisp.wR   = vlerp(s_vSrc.wR,   s_vDst.wR,   k);
    s_vDisp.hR   = vlerp(s_vSrc.hR,   s_vDst.hR,   k);
    s_vDisp.dy   = vlerp(s_vSrc.dy,   s_vDst.dy,   k);
    s_vDisp.aux  = vlerp(s_vSrc.aux,  s_vDst.aux,  k);
    s_vDisp.pill = vlerp(s_vSrc.pill, s_vDst.pill, k);
  } else {
    s_vDisp = s_vDst;
  }
  return s_vDisp;
}

// grok draws its eyes from the polygon engine; the five pill skins fall
// through to their own primitives. This frame's blink, 0 = open .. 1 = shut.
static float s_blinkK = 0;
static bool grokEyes(Arduino_Canvas* c, uint8_t gst, const FaceFrame& f,
                     uint32_t t) {
  if (s_skin != SKIN_GROK) return false;
  grokDrawEyes(c, gst, s_blinkK, f.lookX, f.lookY, t);
  return true;
}

// Toast fade from the age of the last surfacing: 250 ms in, hold, the last
// 400 ms of a 3 s life out (the hold — nudge / needs_you — is the caller's).
static float toastAgeK(int32_t age) {
  if (age < 0 || age >= 3000) return 0.0f;
  if (age < 250)  return age / 250.0f;
  if (age > 2600) return (3000 - age) / 400.0f;
  return 1.0f;
}

// ---- Claude 多会话 top row -------------------------------------------------
// Design: Board.dc.html drawTopRow style A + drawChevrons
// One centred line at baseline 52: the current session's title + 12 px + a
// dim 「n/N」, on a pure-black rounded backing (8 / 6 px padding, r 8) so the
// bunny's ear tip passing behind it cannot eat the words — the same rule on
// all six skins. The backing is opaque the moment the row shows (no alpha on
// this canvas): the ear is cut for the 250 ms fade-in rather than fading.
// ‹ › at x 28 / 452 mark the two tap halves. A change rolls only what changed:
// old text slides 14 px out and fades, new text comes in from the other side,
// 260 ms smoothstep; +1 = up (right half / higher index), -1 = down.
static const uint16_t SR_TITLE = C565(0xC9, 0xCF, 0xD6);
static const uint16_t SR_DIM   = C565(0x6E, 0x76, 0x80);
static const int SR_BASE = 52, SR_GAP = 12, SR_TRACK = 1;
static const int SR_ROLL_MS = 260, SR_SHIFT = 14;
static char     s_srTitle[72] = "", s_srCount[8] = "";   // what the row drew last
static char     s_srFromTitle[72] = "", s_srFromCount[8] = "";   // rolling out
static uint32_t s_srRollAt = 0;
static int8_t   s_srDir = 1;
static bool     s_srRolling = false, s_srTitleRolls = false, s_srCountRolls = false;
static float    s_srK = 0;          // k the row was drawn with (0 = not drawn)
static uint32_t s_srAt = 0;         // millis() of that frame

// The age guard only has to outlive one slow loop pass (a first card-glyph
// load, a file push's 250 ms drain): touchPoll runs before the frame, and the
// caller already checks that the face is the page on screen.
bool faceSessRowShown() {
  return s_srK > 0.03f && (int32_t)(millis() - s_srAt) < 1000;
}

// one 2.5 px round-capped stroke between two points
static void srStroke(Arduino_Canvas* c, float x0, float y0, float x1, float y1,
                     uint16_t col) {
  const float th = 2.5f;
  float dx = x1 - x0, dy = y1 - y0;
  drawRotatedPill(c, (x0 + x1) / 2, (y0 + y1) / 2, sqrtf(dx * dx + dy * dy) + th,
                  th, atan2f(dy, dx) * 57.29578f, col);
}

// ‹ (dir -1, tip at x 28) or › (dir +1, tip at x 452): half-height 7, 5 wide,
// centred on the title's x-height (y 46), outside the backing
static void srChevron(Arduino_Canvas* c, int dir, uint16_t col) {
  const float y = 46, h = 7, w = 5;
  float tip = dir < 0 ? 28 : 452, open = tip - dir * w;
  srStroke(c, open, y - h, tip, y, col);
  srStroke(c, tip, y, open, y + h, col);
}

static void drawSessRow(Arduino_Canvas* c, const FaceFrame& f, uint32_t t, float k) {
  char count[8];
  snprintf(count, sizeof(count), "%u/%u", (unsigned)(f.sessCur + 1), (unsigned)f.sessN);
  const char* title = f.sessTitle ? f.sessTitle : "";
  // a change since the last drawn frame starts a roll; a fresh row (first
  // frame after it was impossible) just appears
  bool tCh = s_srTitle[0] && strcmp(title, s_srTitle) != 0;
  bool cCh = s_srCount[0] && strcmp(count, s_srCount) != 0;
  if (tCh || cCh) {
    strlcpy(s_srFromTitle, s_srTitle, sizeof(s_srFromTitle));
    strlcpy(s_srFromCount, s_srCount, sizeof(s_srFromCount));
    s_srRollAt = t;
    s_srDir = f.sessDir < 0 ? -1 : 1;
    s_srTitleRolls = tCh;
    s_srCountRolls = cCh;
    s_srRolling = true;
  }
  strlcpy(s_srTitle, title, sizeof(s_srTitle));
  strlcpy(s_srCount, count, sizeof(s_srCount));

  int wt = almanacTextWidthSmallTrack(title, SR_TRACK);
  int wc = almanacTextWidthSmallTrack(count, SR_TRACK);
  int x0 = 240 - (wt + SR_GAP + wc) / 2;
  uint16_t titleCol = dim565(SR_TITLE, k);
  uint16_t cBase = f.sessQueue ? GREEN : SR_DIM;
  int32_t age = (int32_t)(t - s_srRollAt);
  if (s_srRolling && age >= SR_ROLL_MS) s_srRolling = false;

  if (s_srRolling) {
    float e = ease(age / (float)SR_ROLL_MS);
    // the old line leaves from ITS OWN centred position, so a long old title
    // never shows under the new counter
    int wtO = almanacTextWidthSmallTrack(s_srFromTitle, SR_TRACK);
    int wcO = almanacTextWidthSmallTrack(s_srFromCount, SR_TRACK);
    int x0O = 240 - (wtO + SR_GAP + wcO) / 2;
    // backing grows to y 20..72 and the wider of the two lines while both move
    int bw = max(wt + SR_GAP + wc, wtO + SR_GAP + wcO);
    c->fillRoundRect(240 - bw / 2 - 8, 20, bw + 16, 52, 8, BG);
    int yOld = SR_BASE - (int)lroundf(SR_SHIFT * e) * s_srDir;
    int yNew = SR_BASE + (int)lroundf(SR_SHIFT * (1 - e)) * s_srDir;
    float kOld = k * (1 - e), kNew = k * e;
    if (s_srTitleRolls) {
      almanacPrintSmallTrack(c, x0O, yOld, s_srFromTitle, dim565(SR_TITLE, kOld), SR_TRACK);
      almanacPrintSmallTrack(c, x0, yNew, title, dim565(SR_TITLE, kNew), SR_TRACK);
    } else {
      almanacPrintSmallTrack(c, x0, SR_BASE, title, titleCol, SR_TRACK);
    }
    if (s_srCountRolls) {
      almanacPrintSmallTrack(c, x0O + wtO + SR_GAP, yOld, s_srFromCount, dim565(cBase, kOld), SR_TRACK);
      almanacPrintSmallTrack(c, x0 + wt + SR_GAP, yNew, count, dim565(cBase, kNew), SR_TRACK);
    } else {
      almanacPrintSmallTrack(c, x0 + wt + SR_GAP, SR_BASE, count, dim565(cBase, k), SR_TRACK);
    }
  } else {
    c->fillRoundRect(x0 - 8, SR_BASE - 24, wt + SR_GAP + wc + 16, 36, 8, BG);
    almanacPrintSmallTrack(c, x0, SR_BASE, title, titleCol, SR_TRACK);
    almanacPrintSmallTrack(c, x0 + wt + SR_GAP, SR_BASE, count, dim565(cBase, k), SR_TRACK);
  }
  // the pressed one lights up for 300 ms (main times it)
  uint16_t dimC = dim565(SR_DIM, 0.9f * k), litC = dim565(EYE, k);
  srChevron(c, -1, f.sessLit < 0 ? litC : dimC);
  srChevron(c, +1, f.sessLit > 0 ? litC : dimC);
}

void faceRender(Arduino_Canvas* c, const FaceFrame& f, uint32_t t) {
  const AgentDef& A = AGENTS[f.agentIdx];
  uint16_t accent = A.color;
  s_lookX = f.lookX; s_lookY = f.lookY;   // for the robo pupil (drawRoboShine)

  // toast surfaces on: agent switch, any state change, offline flip
  static uint8_t lastIdx = 0xFF;
  static uint32_t switchT = 0;
  static uint8_t prevSt[N_AGENTS];
  static bool prevStInit = false;
  if (!prevStInit) { memset(prevSt, 255, sizeof(prevSt)); prevStInit = true; }
  static bool prevOff = false;
  bool resurface = false;
  if (f.agentIdx != lastIdx) {
    lastIdx = f.agentIdx;
    resurface = true;
    faceResetVariants();     // new seat, new pet: start its pool on V0
  }
  if (memcmp(prevSt, f.agentStates, sizeof(prevSt)) != 0) {
    memcpy(prevSt, f.agentStates, sizeof(prevSt));
    resurface = true;
  }
  if (f.offline != prevOff) { prevOff = f.offline; resurface = true; }
  // toast pinned on a nudge (tap / pick-up / PWR tap) or while the selected
  // agent needs you (with ≥2 Claude sessions: the CURRENT session; main
  // swaps it into agentStates[agentIdx])
  bool hold = f.nudge ||
              (!f.offline && !f.listening &&
               f.agentStates[f.agentIdx] == ST_NEEDS_YOU);
  // the current session (or the count) moved — the session row gets
  // its 3 s again, but CONTINUING from where its fade is: a tap on a row that
  // is showing must not dip it to black and fade it back in. The snap-to-zero
  // restart above stays for everything else, so one session = today's face.
  static uint16_t lastSessGen = 0;
  bool sessMoved = f.sessGen != lastSessGen;
  lastSessGen = f.sessGen;
  if (sessMoved && f.sessN >= 2) {
    float kNow = hold ? 1.0f : toastAgeK((int32_t)(t - switchT));
    switchT = kNow >= 1.0f ? t - 250 : kNow > 0.03f ? t - (uint32_t)(kNow * 250) : t;
  } else if (resurface) {
    switchT = t;
  }
  if (f.sessN < 2) {         // no row possible: the next one appears, never rolls in
    s_srTitle[0] = s_srCount[0] = 0;
    s_srRolling = false;
  }

  // track "working continuously since" for the sweat easter egg
  static bool wasWorking = false;
  static uint32_t workT = 0;
  bool working = !f.offline && !f.listening && !f.surprised && f.st == ST_WORKING;
  if (working && !wasWorking) workT = t;
  wasWorking = working;

  c->fillScreen(BG);
  // toast: fade in 250 ms, hold, fade out over the last 400 ms; pinned
  // on a nudge (tap / pick-up / PWR tap) or while the selected agent needs
  // you (`hold`, above). The dots alone also stay pinned while a background
  // agent is working/needs/done — a Qoder task must stay visible from the
  // claude seat, not flash for 3 s. The session row shares `k`.
  float toastK;
  {
    bool bgActive = false;
    if (!f.offline && !f.listening)
      for (int i = 0; i < N_AGENTS; i++)
        if (i != f.agentIdx && (f.agentStates[i] == ST_WORKING ||
                                f.agentStates[i] == ST_NEEDS_YOU ||
                                f.agentStates[i] == ST_DONE))
          bgActive = true;
    float k = hold ? 1.0f : toastAgeK((int32_t)(t - switchT));
    float kDots = (hold || bgActive) ? 1.0f : k;
    if (k > 0.03f || kDots > 0.03f) drawToast(c, f, t, k, kDots);
    toastK = k;
  }
  // Claude 多会话 top row: drawn after the eyes (below), decided here
  // so the low-battery icon can step out of its way
  bool rowUp = f.sessN >= 2 && toastK > 0.03f;
  drawLowBatt(c, f, t, rowUp);
  drawSkinProps(c, t);

  // blink on the per-state cadence table (grokface.cpp)
  // instead of the old fixed 4.6 s tick: 320 ms, 42 % closing +
  // 58 % opening, 4 % left open. The pill skins still press the upper lid —
  // only the rhythm and the curve are shared with grok.
  uint8_t gst = visualState(f);
  s_blinkK = petBlinkTick(gst, s_skin != SKIN_GROK, t);
  float blink = 1.0f - 0.96f * s_blinkK;
  // pill skins' expression pool, same cadence table as grok's (ticked every
  // frame so the timeline keeps running under grok / the hand-drawn moods)
  const EyeVar& V = varTick(gst, t);

  if (f.dizzy) {                                     // shaken: counter-spinning spirals
    int wob = (int)(6 * sinf(t * 0.02f));
    for (int e = 0; e < 2; e++) {
      int cx = (e ? EYE_RX : EYE_LX) + wob;
      float rot = (e ? -0.5f : 0.5f) * t;            // opposite spins
      for (int s = 0; s < 3; s++) {
        float a0 = rot + s * 120;
        c->fillArc(cx, 235, 36 - s * 11, 29 - s * 11, a0, a0 + 255, EYE);
      }
    }
  } else if (f.surprised) {                          // picked up: wide ovals + o
    if (!grokEyes(c, GK_SURPRISED, f, t)) {          // grok: no mouth, per spec
      drawEyePill(c, EYE_LX, 100, 114, 0, 0, EYE);
      drawEyePill(c, EYE_RX, 100, 114, 0, 0, EYE);
      c->fillCircle(240, 324, 12, EYE);
      c->fillCircle(240, 324, 7, BG);
    }
  } else if (f.petting) {                            // stroked: blissful arcs + blush
    int bob = (int)(3 * sinf(t * 0.008f));
    if (grokEyes(c, GK_PETTING, f, t)) bob = 0;      // the engine has its own drift
    else {
      c->fillArc(EYE_LX, 250 + bob, 46, 30, 180, 360, EYE);
      c->fillArc(EYE_RX, 250 + bob, 46, 30, 180, 360, EYE);
    }
    c->fillCircle(EYE_LX - 66, 272 + bob, 11, dim565(BATTRED, 0.45f));
    c->fillCircle(EYE_RX + 66, 272 + bob, 11, dim565(BATTRED, 0.45f));
  } else if (f.eating) {                             // charger in: nom nom nom
    drawEyePill(c, EYE_LX, EYE_W, (int)(EYE_W * blink), 0, 0, EYE);
    drawEyePill(c, EYE_RX, EYE_W, (int)(EYE_W * blink), 0, 0, EYE);
    int mh = 10 + (int)(16 * fabsf(sinf(t * 0.009f)));
    c->fillRoundRect(216, 316, 48, mh, min(10, mh / 2), EYE);
  } else if (f.burp) {                               // charge done: satisfied "o"
    c->fillArc(EYE_LX, 246, 42, 28, 180, 360, EYE);
    c->fillArc(EYE_RX, 246, 42, 28, 180, 360, EYE);
    int r = 12 + (int)(4 * sinf(t * 0.02f));
    c->fillCircle(240, 330, r, EYE);
    c->fillCircle(240, 330, r - 5, BG);
  } else if (f.listening) {                          // full-open + voice bars
    if (!grokEyes(c, GK_LISTENING, f, t)) {
      drawEyePill(c, EYE_LX, EYE_W, (int)(EYE_W * blink), f.lookX, f.lookY, EYE);
      drawEyePill(c, EYE_RX, EYE_W, (int)(EYE_W * blink), f.lookX, f.lookY, EYE);
    }
    static const int base[5] = {12, 24, 34, 22, 12};
    for (int i = 0; i < 5; i++) {
      int h = base[i] + (int)(8 * sinf(t * 0.012f + i * 1.1f));
      if (h < 6) h = 6;
      c->fillRoundRect(204 + i * 16, 333 - h / 2, 8, h, 4, MICBAR);
    }
  } else if (f.offline) {                            // droopy grey eyes + broken link
    if (!grokEyes(c, GK_OFFLINE, f, t)) {
      drawRotatedPill(c, EYE_LX, 242, 84, 34, -8, GREYEYE);
      drawRotatedPill(c, EYE_RX, 242, 84, 34, 8, GREYEYE);
    }
    c->fillRoundRect(212, 300, 20, 6, 3, GREYDIM);
    c->fillRoundRect(248, 308, 20, 6, 3, GREYDIM);
  } else if (f.stretch) {                            // "take a break" nudge
    int hop = -(int)(6 * sinf(3.1416f * (t % 1600) / 1600.0f));
    c->fillArc(EYE_LX, 246 + hop, 42, 28, 180, 360, EYE);
    c->fillArc(EYE_RX, 246 + hop, 42, 28, 180, 360, EYE);
    c->setTextSize(1);
    c->setFont(u8g2_font_fur17_tr);
    int w = trackWidth(c, "break time ~", 3);
    trackPrint(c, 240 - w / 2, 340, "break time ~", 3, GREYTXT);
    c->setFont((const GFXfont*)nullptr);
  } else if (f.bored) {                              // lonely: heavy lids, slow drift
    if (!grokEyes(c, GK_BORED, f, t)) {
      int drift = (int)(V.aux * sinf(t * 0.0012f));
      drawEyeLidded(c, EYE_LX + drift, (int)(V.hL * blink), (int)V.dy, EYE);
      drawEyeLidded(c, EYE_RX + drift, (int)(V.hR * blink), (int)V.dy, EYE);
    }
    c->setTextSize(3);
    c->setTextColor(GREYTXT);
    c->setCursor(352, 140);
    c->print("...");
  } else switch (f.st) {
    case ST_OFF: {                                   // sleeping: flat bars + zz
      if (!grokEyes(c, GK_OFF, f, t)) {
        int breathe = (int)(4 * sinf(t * 0.0013f));
        c->fillRoundRect(136, 230 + breathe, 64, 10, 5, EYE);
        c->fillRoundRect(280, 230 + breathe, 64, 10, 5, EYE);
      }
      drawZz(c, t);
      break;
    }
    case ST_IDLE: {                                  // drowsy half-lid, slow wave
      if (!grokEyes(c, GK_IDLE, f, t)) {
        float wave = 3 * sinf(t * 0.0012f);          // slow breathing, kept
        int dy = f.lookY + (int)V.dy;
        drawEyeLidded(c, EYE_LX + f.lookX, (int)((V.hL + wave) * blink), dy, EYE);
        drawEyeLidded(c, EYE_RX + f.lookX, (int)((V.hR + wave) * blink), dy, EYE);
      }
      break;
    }
    case ST_WORKING: {                               // squint + saccade + dots
      int dart = dartOffset(t);
      int bob = (int)(4 * sinf(3.1416f * (t % 4800) / 4800.0f));
      if (grokEyes(c, GK_WORKING, f, t)) bob = 0;    // engine owns the saccade
      else {
        int dy = bob + f.lookY + (int)V.dy;
        drawEyePill(c, EYE_LX, (int)V.wL, (int)(V.hL * blink), dart + f.lookX, dy, EYE);
        drawEyePill(c, EYE_RX, (int)V.wR, (int)(V.hR * blink), dart + f.lookX, dy, EYE);
      }
      drawTypingDots(c, t, bob);
      drawSweat(c, t, workT);
      break;
    }
    case ST_NEEDS_YOU: {                             // wide stare + bouncing "?"
      if (!grokEyes(c, GK_NEEDS, f, t)) {
        // V1 rides the bouncing "?" (aux = 4 px of lift at its peak)
        int dy = f.lookY + (int)V.dy - (int)(V.aux * fabsf(sinf(t * 0.005f)));
        drawEyePill(c, EYE_LX, (int)V.wL, (int)(V.hL * blink), f.lookX, dy, EYE);
        drawEyePill(c, EYE_RX, (int)V.wR, (int)(V.hR * blink), f.lookX, dy, EYE);
      }
      int qy = 100 - (int)(8 * fabsf(sinf(t * 0.005f)));
      c->setTextSize(5);
      c->setTextColor(EYE);
      c->setCursor(362, qy);
      c->print("?");
      if (f.sendK <= 0.03f && f.claimK <= 0.03f && !f.hideBubble) drawApproveBubble(c, t);
      break;
    }
    case ST_DONE: {                                  // happy closed arcs, bouncing
      if (!grokEyes(c, GK_DONE, f, t)) {             // engine owns grok's hop
        int hop = -(int)(V.aux * sinf(3.1416f * (t % 2000) / 2000.0f));
        int dy = (int)V.dy + hop;                    // arcs sit 25 px higher
        if (V.pill >= 0.5f) {                        // V2「笑开」: wide pill
          drawEyePill(c, EYE_LX, (int)V.wL, (int)(V.hL * blink), 0, dy, EYE);
          drawEyePill(c, EYE_RX, (int)V.wR, (int)(V.hR * blink), 0, dy, EYE);
        } else {                                     // V0/V1: smile arcs
          c->fillArc(EYE_LX, EYE_BOTTOM + dy, (int)(V.wL / 2), (int)V.hL, 180, 360, EYE);
          c->fillArc(EYE_RX, EYE_BOTTOM + dy, (int)(V.wR / 2), (int)V.hR, 180, 360, EYE);
        }
      }
      break;
    }
  }

  // Claude 多会话 top row: after the head props and eyes so its black
  // backing covers the bunny's ear tip; same k as the bottom line
  if (rowUp) drawSessRow(c, f, t, toastK);
  s_srK = rowUp ? toastK : 0.0f;
  s_srAt = millis();

  if (f.pinVis) drawPin(c, f);
  if (f.volK > 0.03f) drawVolume(c, f);
  if (f.sendK > 0.03f) drawSendBubble(c, f);
  if (f.profK > 0.03f) drawProfile(c, f);
  if (f.setK > 0.03f) drawSettingsCard(c, f);
  if (f.claimK > 0.03f) drawClaimCard(c, f, t);   // last: it is modal
}

// Wake-up sequence, blocking (~1.7 s), run once from setup() while SND_BOOT
// chirps: closed lids fade in, one sleepy half-blink, eyes pop open with a
// little overshoot, then a quick left-right glance. Mirrors the BYE shutdown.
// Skin-aware since 2026-09-19: the pill skins wear their head props from the
// moment the eyes are lit, and grok plays the same keyframes through its own
// polygon engine (h -> blink amount, dx -> gaze) instead of drawing pills.
void faceBootAnim(Arduino_Canvas* c) {
  struct Key { int16_t t; int16_t h; int16_t dx; float br; };
  static const Key K[] = {
    {   0,  10,   0, 0.0f },   // closed line fades in from black
    { 300,  10,   0, 1.0f },
    { 550,  34,   0, 1.0f },   // sleepy half open...
    { 700,  12,   0, 1.0f },   // ...and droops shut again
    { 950, 104,   0, 1.0f },   // pop open with overshoot
    {1100,  92,   0, 1.0f },
    {1250,  92, -16, 1.0f },   // glance left
    {1450,  92,  16, 1.0f },   // glance right
    {1650,  92,   0, 1.0f },
  };
  const int N = sizeof(K) / sizeof(K[0]);
  const bool grok = s_skin == SKIN_GROK;
  if (grok) grokResetEngine();     // never inherit a previous morph
  uint32_t t0 = millis();
  for (;;) {
    uint32_t now = millis();
    int32_t t = (int32_t)(now - t0);
    if (t > K[N - 1].t) break;
    int i = 1;
    while (i < N - 1 && K[i].t < t) i++;
    float k = ease((float)(t - K[i - 1].t) / (K[i].t - K[i - 1].t));
    int h    = K[i - 1].h  + (int)((K[i].h  - K[i - 1].h)  * k);
    int dx   = K[i - 1].dx + (int)((K[i].dx - K[i - 1].dx) * k);
    float br = K[i - 1].br + (K[i].br - K[i - 1].br) * k;
    c->fillScreen(BG);
    if (grok) {
      // same timeline, grok's vocabulary: lid height -> blink amount
      // (10 = shut, >= 92 = wide open, the 104 overshoot clamps to open),
      // glance -> the engine's gaze offset in screen px.
      float blinkK = (92.0f - h) / 82.0f;
      if (blinkK < 0) blinkK = 0;
      if (blinkK > 1) blinkK = 1;
      grokDrawEyes(c, GK_IDLE, blinkK, (int8_t)dx, 0, now);
    } else {
      if (t >= 300) drawSkinProps(c, now);   // props once the eyes are lit
      uint16_t col = dim565(EYE, br);
      drawEyePill(c, EYE_LX, EYE_W, h, dx, 0, col);
      drawEyePill(c, EYE_RX, EYE_W, h, dx, 0, col);
    }
    c->flush();
    delay(33);
  }
  if (grok) grokResetEngine();     // and do not leak it into the first frame
}
