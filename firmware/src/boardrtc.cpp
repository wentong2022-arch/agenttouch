// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
#include "boardrtc.h"
#include <Wire.h>
#include <SensorPCF85063.hpp>
#include <time.h>
#include "pins.h"
#include "pages.h"

static SensorPCF85063 rtc;
static RtcInfo info;

// Anything earlier is "never set": after a power-on reset the chip reads
// 2000-01-01 (the driver adds 2000 to its two-digit year).
static const uint32_t RTC_MIN_EPOCH = 1767225600u;   // 2026-01-01 00:00:00

// Howard Hinnant's days_from_civil: calendar fields -> seconds, no timezone.
static uint32_t civilToEpoch(int y, unsigned m, unsigned d,
                             unsigned hh, unsigned mm, unsigned ss) {
  y -= m <= 2;
  const int      era = (y >= 0 ? y : y - 399) / 400;
  const unsigned yoe = (unsigned)(y - era * 400);
  const unsigned doy = (153 * (m + (m > 2 ? -3 : 9)) + 2) / 5 + d - 1;
  const unsigned doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
  const int64_t days = (int64_t)era * 146097 + (int64_t)doe - 719468;
  return (uint32_t)(days * 86400 + hh * 3600 + mm * 60 + ss);
}

void rtcFormat(uint32_t ep, char* out, size_t n) {
  if (!ep) { snprintf(out, n, "--"); return; }
  time_t tt = (time_t)ep;
  struct tm tmv;
  gmtime_r(&tt, &tmv);                   // local seconds -> fields (no tz twice)
  snprintf(out, n, "%04d-%02d-%02d %02d:%02d:%02d", tmv.tm_year + 1900,
           tmv.tm_mon + 1, tmv.tm_mday, tmv.tm_hour, tmv.tm_min, tmv.tm_sec);
}

uint32_t rtcRead(bool* integrityOut) {
  if (!info.present) { if (integrityOut) *integrityOut = false; return 0; }
  bool integ = rtc.isClockIntegrityGuaranteed();   // seconds register OS bit clear
  if (integrityOut) *integrityOut = integ;
  if (!integ) return 0;
  RTC_DateTime dt = rtc.getDateTime();
  if (dt.getMonth() < 1 || dt.getMonth() > 12 || dt.getDay() < 1 || dt.getDay() > 31 ||
      dt.getHour() > 23 || dt.getMinute() > 59 || dt.getSecond() > 59) return 0;
  uint32_t ep = civilToEpoch(dt.getYear(), dt.getMonth(), dt.getDay(),
                             dt.getHour(), dt.getMinute(), dt.getSecond());
  return ep >= RTC_MIN_EPOCH ? ep : 0;
}

bool rtcInit() {
  info = RtcInfo{};
  info.present = rtc.begin(Wire, PIN_I2C_SDA, PIN_I2C_SCL);
  if (!info.present) { Serial.println("rtc: PCF85063 not found"); return false; }
  rtc.setClockOutput(SensorPCF85063::CLK_LOW);   // CLKOUT pin floats here: stop the 32 kHz toggle
  bool integ = false;
  info.bootEpoch = rtcRead(&integ);
  info.integrity = integ;
  char buf[24];
  rtcFormat(info.bootEpoch, buf, sizeof(buf));
  if (info.bootEpoch) {
    clockSet(info.bootEpoch);
    info.seeded = true;
    Serial.printf("rtc: clock seeded from chip: %s\n", buf);
  } else {
    Serial.printf("rtc: no usable time (integrity %s), waiting for host\n",
                  integ ? "ok" : "lost");
  }
  return true;
}

void rtcHostSync(uint32_t hostLocal) {
  if (!info.present || !hostLocal) return;
  bool integ = false;
  uint32_t chip = rtcRead(&integ);
  int32_t drift = chip ? (int32_t)(chip - hostLocal) : 0;
  info.syncs++;
  info.lastSync = hostLocal;
  info.lastDrift = drift;
  info.integrity = integ;
  if (chip && drift > -2 && drift < 2) return;    // close enough; a write restarts the sub-second counter
  time_t tt = (time_t)hostLocal;
  struct tm tmv;
  gmtime_r(&tt, &tmv);
  rtc.setDateTime(RTC_DateTime(tmv));             // writes seconds bit 7 = 0: integrity flag cleared
  info.writes++;
  info.integrity = true;
  char a[24], b[24];
  rtcFormat(hostLocal, a, sizeof(a));
  rtcFormat(chip, b, sizeof(b));
  Serial.printf("rtc: set %s (chip had %s, drift %ld s)\n", a, b, (long)drift);
}

const RtcInfo& rtcInfo() { return info; }
