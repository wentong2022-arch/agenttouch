// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
#pragma once
#include <Arduino_GFX_Library.h>
#include "config.h"

// Everything the renderer needs for one frame.
struct FaceFrame {
  uint8_t    agentIdx;        // which pet fills the screen
  AgentState st;              // its state
  bool       offline;         // true = no host connection (overrides st visuals)
  bool       listening;       // voice key held -> listening face
  bool       surprised;       // just picked up
  bool       nudge;           // user asked "what's up" -> pin the toast
  bool       dizzy;           // just got a good shake -> spiral eyes
  bool       petting;         // being stroked -> blissful arcs + blush
  bool       eating;          // charger plugged in -> nom nom
  bool       burp;            // charge done -> satisfied burp
  bool       bored;           // lonely: everyone idle, nobody plays with me
  bool       stretch;         // host says the human worked too long
  int8_t     lookX, lookY;    // 眼神追声: eye offset toward the last sound, page-frame px
  uint8_t    agentStates[N_AGENTS];  // all agents, for the mini status dots
  bool       showOff;         // 「显示离线席位」: draw off seats too
  int        battPct;         // 0..100, -1 = unknown
  bool       wifiUp;
  bool       hostUp;
  uint8_t    volume;          // 0..100 (0 = muted)
  float      volK;            // volume overlay opacity, 0 = hidden
  float      sendK;           // post-voice "↵ 发送" bubble opacity, 0 = hidden
  // growth profile card (double-tap; tap again to dismiss)
  float      profK;           // card opacity, 0 = hidden
  uint8_t    level;
  uint8_t    xpPct;           // progress toward the next level, 0..100
  uint16_t   statDone;        // agent done events = feedings
  uint16_t   statDays;        // distinct days powered on together
  uint16_t   statPets;        // petting sessions received
  // 钉住 corner pin (doc/06): 0 = hidden, 1 = faint grey (in hand, unpinned
  // — findable without breaking the bare desk face), 2 = grey (pinned, past
  // the highlight), 3 = seat color (just pinned)
  uint8_t    pinVis;
  // settings card (hold the face ~1.5 s): skin picker + brightness + status
  float      setK;            // card opacity, 0 = hidden
  uint8_t    setBright;       // brightness level 0..2
  uint8_t    skinBy[N_AGENTS];  // every seat's outfit (owner badges on the card)
  char       setStatus[40];   // read-only line 1: link / IP / battery
  char       setStatus2[40];  // read-only line 2: SD card / firmware
  // claim card: a Mac asks
  // to become the owner; tap the green button = yes
  float      claimK;          // card opacity, 0 = hidden
  float      claimLeft;       // countdown bar, 1 -> 0 over 30 s
  bool       claimDone;       // accepted: solid green 「已连接」
  const char* claimName;      // fitted Mac name (body26)
  const char* claimSub;       // 「现在跟着…」/「还没有主人」, fitted (tiny18)
};

// Skins: same soul, different body. Affects eye shape + head props everywhere
// (boot animation included). Persisted by main in NVS.
// Lineup user-picked from the 换装间 canvas (artifact d568a8cf, 2026-08-29):
// robo = F 像素高光眼, bunny = C 折耳, sprout promoted from the bench.
// grok (2026-09-13) is the odd one out: no pill eyes, no head props — a pure
// black face with two 48-point polygon eyes driven by grokface.cpp.
enum SkinId : uint8_t { SKIN_CLASSIC = 0, SKIN_KITTY, SKIN_ROBO, SKIN_BUNNY,
                        SKIN_SPROUT, SKIN_GROK };
static const uint8_t N_SKINS = 6;
void    faceSetSkin(uint8_t id);
uint8_t faceSkin();
const char* faceSkinName(uint8_t id);   // "classic".."grok", the host-side spelling

void faceRender(Arduino_Canvas* c, const FaceFrame& f, uint32_t t);
void faceBootAnim(Arduino_Canvas* c);   // blocking wake-up, once from setup()

// Settings-card tap routing (main owns the touch, face owns the layout):
// 0..5 = skin dot, 10..12 = brightness level, -1 = inside card (no target),
// -2 = outside the card (dismiss).
int faceSettingsHit(int x, int y);
// Claim card, in the face page's VIEW frame: 1 = the connect button,
// 0 = elsewhere on the card (no-op), -1 = outside the card.
int faceClaimHit(int x, int y);
