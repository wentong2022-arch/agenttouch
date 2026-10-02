// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
#include "sdfont.h"
#include "sdcard.h"
#include <FS.h>
#include <esp_heap_caps.h>

// .afn: "AFN1", u16 ascent, u32 count, count x {u32 cp, u32 off} sorted by
// cp, then the glyph blob — records are w,h,adv,int8 dx,int8 dyTop + 4bpp
// rows, byte-identical to the flash tables so afChar draws them as-is.
static const char* const PATHS[SDF_N] = {
  "/agentpet/fonts/head26.afn", "/agentpet/fonts/body26.afn", "/agentpet/fonts/quot24.afn",
  "/agentpet/fonts/tiny18.afn"};
static const char* const NAMES[SDF_N] = {"head26", "body26", "quot24", "tiny18"};

struct Face {
  File      f;
  uint32_t  n = 0;
  uint32_t* tab = nullptr;     // PSRAM: cp0, off0, cp1, off1, ...
  uint32_t  blob = 0;          // file offset of the glyph data
};
static Face s_face[SDF_N];

// Direct-mapped glyph cache in PSRAM: 512 slots x 512 B. A 26 px glyph is
// ≤ 5 + 13*26 = 343 B. Collisions just evict; on-screen text is small.
static const int      N_SLOTS = 512, SLOT_SZ = 512;
struct Slot { uint8_t face; uint8_t used; uint32_t cp; };
static Slot*    s_slot = nullptr;
static uint8_t* s_pool = nullptr;
static bool     s_firstLogged = false;

static void faceClose(Face& fc) {
  if (fc.f) fc.f.close();
  if (fc.tab) { free(fc.tab); fc.tab = nullptr; }
  fc.n = 0;
}

static bool faceOpen(Face& fc, const char* path, const char* name) {
  fs::FS* fs = sdFs();
  if (!fs) return false;
  fc.f = fs->open(path, FILE_READ);
  if (!fc.f) return false;
  uint8_t hdr[10];
  if (fc.f.read(hdr, 10) != 10 || memcmp(hdr, "AFN1", 4) != 0) { faceClose(fc); return false; }
  uint32_t n = (uint32_t)hdr[6] | ((uint32_t)hdr[7] << 8) | ((uint32_t)hdr[8] << 16) | ((uint32_t)hdr[9] << 24);
  if (!n || n > 65536) { faceClose(fc); return false; }
  fc.tab = (uint32_t*)heap_caps_malloc(n * 8, MALLOC_CAP_SPIRAM);
  if (!fc.tab) { faceClose(fc); return false; }
  if (fc.f.read((uint8_t*)fc.tab, n * 8) != n * 8) { faceClose(fc); return false; }
  fc.n = n;
  fc.blob = 10 + n * 8;
  Serial.printf("sdfont: %s %lu glyphs (%lu KB)\n", name, (unsigned long)n,
                (unsigned long)(fc.f.size() >> 10));
  return true;
}

void sdFontLoadAll() {
  if (!s_slot) {
    s_slot = (Slot*)heap_caps_calloc(N_SLOTS, sizeof(Slot), MALLOC_CAP_SPIRAM);
    s_pool = (uint8_t*)heap_caps_malloc((size_t)N_SLOTS * SLOT_SZ, MALLOC_CAP_SPIRAM);
  }
  if (s_slot) memset(s_slot, 0, N_SLOTS * sizeof(Slot));   // old records may be stale
  for (int i = 0; i < SDF_N; i++) {
    faceClose(s_face[i]);
    faceOpen(s_face[i], PATHS[i], NAMES[i]);
  }
}

bool sdFontReady(uint8_t face) { return face < SDF_N && s_face[face].n > 0; }

bool sdFontIsFontPath(const char* path) {
  return strncmp(path, "/agentpet/fonts/", 16) == 0;
}

const uint8_t* sdFontGlyph(uint8_t face, uint32_t cp) {
  if (face >= SDF_N || !s_face[face].n || !s_slot || !s_pool) return nullptr;
  Face& fc = s_face[face];
  int idx = (int)((cp * 2654435761u) ^ ((uint32_t)face << 20)) & (N_SLOTS - 1);
  Slot& sl = s_slot[idx];
  uint8_t* rec = s_pool + (size_t)idx * SLOT_SZ;
  if (sl.used && sl.face == face && sl.cp == cp) return rec;
  // binary search the index
  int lo = 0, hi = (int)fc.n - 1, hit = -1;
  while (lo <= hi) {
    int mid = (lo + hi) / 2;
    uint32_t c = fc.tab[mid * 2];
    if (c == cp) { hit = mid; break; }
    if (c < cp) lo = mid + 1; else hi = mid - 1;
  }
  if (hit < 0) return nullptr;
  if (!fc.f.seek(fc.blob + fc.tab[hit * 2 + 1])) return nullptr;
  if (fc.f.read(rec, 5) != 5) return nullptr;
  size_t body = (size_t)((rec[0] + 1) / 2) * rec[1];
  if (5 + body > (size_t)SLOT_SZ) return nullptr;           // would not fit a slot
  if (body && fc.f.read(rec + 5, body) != body) return nullptr;
  sl.used = 1; sl.face = face; sl.cp = cp;
  if (!s_firstLogged) {
    s_firstLogged = true;
    Serial.printf("sdfont: first card glyph U+%04lX from %s\n", (unsigned long)cp, NAMES[face]);
  }
  return rec;
}
