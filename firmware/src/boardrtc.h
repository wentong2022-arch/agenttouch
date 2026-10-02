// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
// PCF85063 real-time clock — RTC 启用 (2026-09-05).
// Hardware (schematic p1 "RTC" block): VDD = net VCC-RTC = AXP2101 pin 28
// RTCLDO. The PMU datasheet keeps RTCLDO on in its OFF state (~40 µA from
// the battery), so the clock survives long-press shutdown; only a fully
// unpowered board (battery dead/removed = PMU power-on reset) loses it.
// CLKOUT is unconnected, INT (GPIO13) unused for now, VBACKUP has only a cap.
// Convention: the chip holds LOCAL time (timezone already applied) — exactly
// what clockSet() takes; the whole firmware thinks in local seconds.
#pragma once
#include <Arduino.h>

struct RtcInfo {
  bool     present;      // chip answered on I2C
  bool     integrity;    // oscillator never stopped since it was last set (OS flag clear)
  bool     seeded;       // the soft clock was seeded from the chip at boot
  uint32_t bootEpoch;    // what the chip said at boot, 0 = nothing usable
  uint32_t lastSync;     // host local epoch at the last time push
  int32_t  lastDrift;    // chip - host at that push, seconds, before correction
  uint32_t syncs;        // host time pushes seen since boot
  uint32_t writes;       // how many of them rewrote the chip
};

bool rtcInit();                                   // probe; seeds clockSet() when the chip has a usable time
uint32_t rtcRead(bool* integrityOut = nullptr);   // local epoch from the chip, 0 if unusable
void rtcHostSync(uint32_t hostLocalEpoch);        // rewrite the chip when it is >=2 s off or lost integrity
const RtcInfo& rtcInfo();
void rtcFormat(uint32_t localEpoch, char* out, size_t n);   // "2026-09-05 11:20:33", "--" for 0
