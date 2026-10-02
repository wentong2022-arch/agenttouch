// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
// See jpegrom.h. TJpgDec in the S3 mask ROM is built with JD_FORMAT 0, so
// its output callback hands us RGB888 rectangles; we pack them to RGB565 as
// they arrive. The work pool must be plain internal RAM (the ROM code has no
// idea about PSRAM); the pixel buffer is PSRAM because 80 KB is too much to
// take from the heap the audio/BLE tasks share.
#include "jpegrom.h"
#include <Arduino.h>
#include <FS.h>
#include <esp_heap_caps.h>
#include "sdcard.h"
extern "C" {
#include "rom/tjpgd.h"
}

static const size_t JR_POOL = 8192;    // ChaN: ~3.1 KB for a baseline image

static uint16_t* gPix   = nullptr;     // COVER_MAX * COVER_MAX RGB565
static char      gPath[80] = "";       // path the cache holds (good or bad)
static bool      gOk    = false;
static int       gW = 0, gH = 0;

// ---- TJpgDec callbacks ----------------------------------------------------
static UINT jrIn(JDEC* jd, BYTE* buf, UINT nb) {
  File* f = (File*)jd->device;
  if (!f) return 0;
  if (buf) return (UINT)f->read(buf, nb);
  return f->seek(f->position() + nb) ? nb : 0;   // skip: no data wanted
}

static UINT jrOut(JDEC* jd, void* bitmap, JRECT* rect) {
  (void)jd;
  const uint8_t* s = (const uint8_t*)bitmap;
  for (int y = rect->top; y <= (int)rect->bottom; y++) {
    for (int x = rect->left; x <= (int)rect->right; x++) {
      uint8_t r = *s++, g = *s++, b = *s++;
      if ((unsigned)x < (unsigned)COVER_MAX && (unsigned)y < (unsigned)COVER_MAX)
        gPix[y * COVER_MAX + x] =
            (uint16_t)(((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3));
    }
  }
  return 1;                                       // 0 would abort the decode
}

// ---- decode ---------------------------------------------------------------
static bool decode(const char* path) {
  fs::FS* fs = sdFs();
  if (!fs) return false;
  File f = fs->open(path, FILE_READ);
  if (!f) return false;

  void* pool = malloc(JR_POOL);
  if (!gPix) gPix = (uint16_t*)heap_caps_malloc(
      (size_t)COVER_MAX * COVER_MAX * 2, MALLOC_CAP_SPIRAM);
  bool ok = false;
  JDEC jd;
  if (pool && gPix) {
    uint32_t t0 = millis();
    JRESULT r = jd_prepare(&jd, jrIn, pool, JR_POOL, &f);
    if (r == JDR_OK) {
      uint8_t sc = 0;                              // ROM build has JD_USE_SCALE
      while (sc < 3 && ((jd.width >> sc) > COVER_MAX || (jd.height >> sc) > COVER_MAX))
        sc++;
      gW = (int)(jd.width >> sc);
      gH = (int)(jd.height >> sc);
      if (gW > 0 && gH > 0 && gW <= COVER_MAX && gH <= COVER_MAX) {
        memset(gPix, 0, (size_t)COVER_MAX * COVER_MAX * 2);
        r = jd_decomp(&jd, jrOut, sc);
        ok = (r == JDR_OK);
      } else r = JDR_FMT3;
    }
    Serial.printf("cover: %s %dx%d scale %d -> %s (%lu ms)\n", path,
                  (int)jd.width, (int)jd.height, ok ? 0 : -1,
                  ok ? "ok" : "fail", (unsigned long)(millis() - t0));
    if (!ok) Serial.printf("cover: tjpgd rc %d\n", (int)r);
  }
  free(pool);
  f.close();
  return ok;
}

// The remembered failure has to be forgettable: the whole point of np_miss
// is that the host ships the cover AFTER the board first asked for it, and
// the path does not change when it lands.
void coverForget(const char* path) {
  if (path && strcmp(path, gPath) == 0) { gPath[0] = 0; gOk = false; }
}

CoverImg coverGet(const char* path) {
  CoverImg out = {nullptr, 0, 0};
  if (!path || path[0] != '/' || strlen(path) >= sizeof(gPath)) return out;
  if (strcmp(path, gPath) != 0) {                 // new path: decode once
    snprintf(gPath, sizeof(gPath), "%s", path);
    gOk = decode(path);
  }
  if (!gOk) return out;
  out.px = gPix; out.w = gW; out.h = gH;
  return out;
}
