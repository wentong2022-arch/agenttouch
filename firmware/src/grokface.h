// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
// Sixth skin "grok" (user-approved 2026-09-13 22:00):
// a pure-black face whose ONLY moving parts are two white polygon eyes.
//
//   shape   = GROK_EYES[expr][eye][96] — 48 points, 1/4 px screen coords
//   dynamic = per-state expression pool picked at random on a cadence,
//             point-by-point damped-spring morph between shapes,
//             per-state blink rhythm, slow gaze drift
//
#pragma once
#include <Arduino_GFX_Library.h>

// Visual state of the pet as the eye engines see it. Kept separate from
// AgentState because the face has more moods than the wire protocol does.
enum GrokSt : uint8_t {
  GK_OFF = 0, GK_IDLE, GK_WORKING, GK_NEEDS, GK_DONE, GK_LISTENING,
  GK_BORED, GK_SURPRISED, GK_PETTING, GK_OFFLINE, GK_N
};

// Blink scheduler shared by ALL skins: random
// per-state cadence instead of the old fixed 4.6 s tick, 320 ms curve (42 %
// closing + 58 % opening) and a 4 % floor. Call exactly once per frame.
// Returns 0 = wide open .. 1 = shut. legacy = one of the five pill skins
// (they blink while working; grok's working eyes are thin lines and don't).
float petBlinkTick(uint8_t st, bool legacy, uint32_t t);

// Draw grok's eye pair for this frame. Background and props stay with
// faceRender so every skin shares their positions.
void grokDrawEyes(Arduino_Canvas* c, uint8_t st, float blinkK,
                  int8_t lookX, int8_t lookY, uint32_t t);

// Drop the morph/cadence state (skin change, seat change).
void grokResetEngine();

// Expression-change cadence of a state, ms (GCFG swMin/swMax). Exported so
// the five pill skins' variant pool (face.cpp) can share the ONE table
// instead of copying the numbers — tuning a state's rhythm stays a one-line
// edit in GCFG. Read-only: it never touches the engine.
void petSwitchCadence(uint8_t st, uint16_t* lo, uint16_t* hi);

// Settings-card tile icon: two small slanted eyes, drawn around (cx, cy).
void grokMiniEyes(Arduino_Canvas* c, int cx, int cy, uint16_t col);
