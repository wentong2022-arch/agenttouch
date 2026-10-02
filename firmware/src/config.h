// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
#pragma once
#include <stdint.h>

// ---- Wi-Fi / Host (Mac) ----
// Public defaults: no factory network and no fixed Mac. A new board learns
// both from the Mac's settings page (Wi-Fi over BLE, owner claim).
// A developer's own board can bake them in via config.local.h (gitignored),
// which defines exactly these four:
//   WIFI_SSID / WIFI_PASS         factory network, always known, never stored
//   HOST_MDNS_NAME                Mac's mDNS name without ".local"
//   HOST_FALLBACK_IP              used when mDNS fails and there is no owner
#if __has_include("config.local.h")
#include "config.local.h"
#endif
#ifndef WIFI_SSID
#define WIFI_SSID   ""
#define WIFI_PASS   ""
#endif
#ifndef HOST_MDNS_NAME
#define HOST_MDNS_NAME    ""
#endif
#ifndef HOST_FALLBACK_IP
#define HOST_FALLBACK_IP  ""
#endif
#define HOST_PORT         8737
static constexpr bool kFactoryWifi = sizeof(WIFI_SSID) > 1;   // "" is 1 byte (the NUL)

// ---- Touch mapping (MADCTL 0xA0 rotates the panel 90° CCW) ----
// screen_x = raw_y ; screen_y = LCD_H-1 - raw_x   (flip flags below if wrong)
#define TOUCH_SWAP_XY   1
#define TOUCH_MIRROR_X  1   // x was mirrored; exposed by swipe direction (2026-08-27)
#define TOUCH_MIRROR_Y  0   // was 1 with the swipe handlers' y sign written
                            // backwards to compensate — absolute y ran
                            // upside-down for a week until the settings
                            // card's hit test needed it for real (day 9)
// Horizontal travel (px) that turns a touch into a swipe; anything shorter
// stays a tap. Swipes must also be mostly horizontal (|dx| > 2|dy|).
#define SWIPE_MIN_PX    80
// Vertical (volume) swipes on the face: shorter travel and a looser 1.5:1
// straightness — misfiring ±10 costs nothing, while in-hand tremor adds
// sideways drift that the 2:1 test kept rejecting (2026-09-02).
#define VSWIPE_MIN_PX   60

// ---- Agents ----
enum AgentState : uint8_t {
  ST_OFF = 0,      // not running -> pet sleeps
  ST_IDLE,         // running, quiet -> drowsy
  ST_WORKING,      // actively doing things
  ST_NEEDS_YOU,    // waiting for user input
  ST_DONE,         // finished recently
};

struct AgentDef {
  const char* id;      // wire id (matches host)
  const char* name;    // shown on screen
  uint16_t    color;   // accent (RGB565)
};

// RGB565 helpers: RGB(r,g,b) at compile time
#define C565(r,g,b) (uint16_t)((((r)&0xF8)<<8) | (((g)&0xFC)<<3) | ((b)>>3))

static const AgentDef AGENTS[] = {
  { "claude",    "Claude",   C565(0xFF, 0x6B, 0x5A) },  // coral
  { "codex",     "Codex",    C565(0x50, 0x96, 0xFF) },  // blue
  { "qoder",     "Qoder",    C565(0x3D, 0xDC, 0x84) },  // green
  { "qoderwork", "QwenWork", C565(0xA7, 0x78, 0xFF) },  // purple (通义紫, 千问办公)
  // 2026-09-13 五席: host 把 qoder 拆成 qoder (Qoder CN IDE) + forest
  // (Qoder CN.app 桌面版 Qoder Forest). 新席位只能追加在末尾——
  // host 的 report 消息 w[]/d[] 按本表下标生成, 插中间会把千问的分钟数
  // 记到别人头上.
  { "forest",    "Forest",   C565(0xF2, 0xA3, 0x3C) },  // amber 琥珀 (候选, 真机四灯并排看过再定)
};
static const int N_AGENTS = sizeof(AGENTS) / sizeof(AGENTS[0]);

// A background agent flipping to needs_you steals the big face (the overview
// dots always hint at the others). Set 0 to keep focus fully manual.
#define AUTO_FOCUS_NEEDS_YOU 1

// ---- Orientation pages ----
// Page shown at each IMU orientation sector (90° steps; see pages.h).
// The sector-to-physical mapping depends on how the IMU is mounted: rotate
// the board, watch serial "orient: n", and swap entries here if a direction
// lands on the wrong page.
// Physical edges (screen facing you, keys up; doc/01 last section): left
// side = microSD slot + MIC2 hole, right side = speaker grille + MIC1 hole,
// bottom = USB-C. The IMU's X points right, so sector 1 = right side down,
// sector 3 = left side down.
// 2026-09-09 swap (user): the clock strip — the side that plays podcasts —
// moved to sector 3 so the speaker and the default mic (MIC1) face up; the
// almanac took sector 1 (a few seconds of TTS on tap; dictation there
// auto-picks MIC2, see micSessionStart). Sector 2 is keys-down, unplaceable,
// excluded in imuPoll — that entry is a never-used placeholder.
#define PAGES_BY_ORIENT {PAGE_FACE, PAGE_CALENDAR, PAGE_FACE, PAGE_CLOCK}

// Idle dimming (battery): full while any agent works / needs you or on
// recent activity; else step down after 8 / 20 minutes. "Full" is picked on
// the settings card (3 levels, NVS "bright"); DIM/LOW stay fixed below it.
#define BRIGHT_LEVELS {110, 180, 245}
#define BRIGHT_FULL 180
#define BRIGHT_DIM  100
#define BRIGHT_LOW  40

#define FW_VERSION "v1.1"
#define FW_BUILD   __DATE__ " " __TIME__   // hello "build": tells an OTA apart from the old image

// Clock/calendar pages use one fixed accent — user picked blue over the
// per-agent color (no red on these pages).
#define PAGE_ACCENT C565(0x50, 0x96, 0xFF)

// ---- Sounds (ES8311 + speaker) ----
#define SOUND_ENABLED 1
#define SOUND_VOLUME  70    // default DAC volume 0..100 (first boot only —
                            // runtime value lives in NVS: swipe up/down on the
                            // face page, left-key hold = mute, {"t":"sound","vol":N})

// ---- Microphone (ES7210 dual-mic ADC, mic-blackhole branch) ----
// Capture runs full duplex on the playback I2S port. Frames of MIC_FRAME_MS
// mono 16 kHz go out as IMA ADPCM {"t":"mic"} lines over TCP while the right
// key is held (and the host enabled it) or a host {"t":"mic","on":1} test.
#define MIC_ENABLED   1
#define MIC_GAIN      10    // ES7210 PGA 0..14 = 0,3,6..33,34.5,36,37.5 dB (10 = 30 dB, esp-bsp default)
#define MIC_FRAME_MS  40    // 640 samples -> 320 B ADPCM per line (~470 B on the wire, 25 lines/s)
