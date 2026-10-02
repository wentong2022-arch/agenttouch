// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
// On-board episode player — see player.h for the container and the threading.
#include "player.h"
#include "audio.h"
#include "sdcard.h"
#include <Arduino.h>
#include <ArduinoJson.h>
#include <string.h>

static const char* PL_DIR   = "/agentpet/audio";
static const char* PL_STATE = "/agentpet/audio/state.json";
static const uint32_t PL_RATE  = 16000;        // the I2S clock is fixed at this
static const uint32_t PL_HDR   = 16;           // "AGPTIMA1" + u32 rate + u32 blocks
static const uint32_t PL_BDATA = 8000;         // nibbles in a full block (1 s)
static const uint32_t PL_BSTR  = 4 + PL_BDATA; // block stride on the card

static const int PL_MAX = 64;
struct PlItem { char name[48]; };               // file name without the extension

static PlItem*  s_items = nullptr;              // PSRAM, sorted by name
static int      s_n     = 0;
static PlayerInfo gInfo = {};

// s_cur / s_blk are written by the loop (explicit skips) and by the audio
// task (auto-advance at the end of an episode); 32-bit stores are atomic and
// the two never race in practice — a skip lands, the stream notices at its
// next block. s_seekGen is what tells the stream "the loop moved you".
static volatile int      s_cur     = -1;        // index into s_items, -1 = nothing
static volatile uint32_t s_blk     = 0;         // block being played / to resume at
static volatile uint32_t s_seekGen = 0;         // bumped by every explicit seek
static volatile bool     s_want    = false;     // the episode should be sounding
static volatile bool     s_running = false;     // audioTask is inside the player loop

// --- audio task only ---
static File     s_file;
static uint32_t s_openGen = 0;                  // s_seekGen the open file was set up for
static int      s_openIdx = -1;
static uint32_t s_nblk    = 0;                  // blocks in the open file
static uint8_t* s_buf     = nullptr;            // PSRAM, one block of nibbles
static uint32_t s_bufSamp = 0, s_off = 0;       // samples in the block / next to decode
static ImaDec   s_ima     = {0, 0};

// --- loop only ---
static int      s_metaIdx = -2;                 // item gInfo describes
static uint32_t s_saveAt  = 0;

// ------------------------------------------------------------------ helpers
// Titles arrive as UTF-8; cutting one mid-character would leave the CJK
// renderer a broken glyph, so back up to a lead byte.
static void utf8Trunc(char* dst, size_t cap, const char* src) {
  size_t n = strlen(src);
  if (n >= cap) {
    n = cap - 1;
    while (n > 0 && (src[n] & 0xC0) == 0x80) n--;   // continuation byte: step back
  }
  memcpy(dst, src, n);
  dst[n] = 0;
}

static const char* baseName(const char* p) {
  const char* s = strrchr(p, '/');
  return s ? s + 1 : p;
}

static void plPath(char* out, size_t cap, const char* name, const char* ext) {
  snprintf(out, cap, "%s/%s%s", PL_DIR, name, ext);
}

// ------------------------------------------------------------------ scan
static void plInsert(const char* base) {
  if (s_n >= PL_MAX) return;
  int i = s_n;
  while (i > 0 && strcmp(s_items[i - 1].name, base) > 0) { s_items[i] = s_items[i - 1]; i--; }
  snprintf(s_items[i].name, sizeof(s_items[i].name), "%s", base);
  s_n++;
}

static int plFind(const char* base) {
  for (int i = 0; i < s_n; i++)
    if (strcmp(s_items[i].name, base) == 0) return i;
  return -1;
}

// Only entries with BOTH .ima and .json count: the host pushes the sidecar
// first, so a half-arrived episode never shows up in the list.
static void plScan() {
  s_n = 0;
  fs::FS* fs = sdFs();
  if (!fs || !s_items) return;
  File dir = fs->open(PL_DIR);
  if (!dir) return;
  if (!dir.isDirectory()) { dir.close(); return; }
  for (File e = dir.openNextFile(); e; e = dir.openNextFile()) {
    if (e.isDirectory()) { e.close(); continue; }
    char fn[64];                                 // e.name() dies with the File
    snprintf(fn, sizeof(fn), "%s", baseName(e.name()));
    e.close();
    size_t len = strlen(fn);
    if (len < 5 || len - 4 >= sizeof(s_items[0].name)) continue;
    if (strcasecmp(fn + len - 4, ".ima") != 0) continue;
    char base[48];
    memcpy(base, fn, len - 4);
    base[len - 4] = 0;
    char side[128];
    plPath(side, sizeof(side), base, ".json");
    if (!fs->exists(side)) continue;
    plInsert(base);
  }
  dir.close();
}

// /agentpet/audio/state.json -> where we stopped last time.
static void plRestore() {
  s_cur = s_n ? 0 : -1;
  s_blk = 0;
  fs::FS* fs = sdFs();
  if (!fs || !s_n) return;
  File f = fs->open(PL_STATE, FILE_READ);
  if (!f) return;
  JsonDocument d;
  bool ok = deserializeJson(d, f) == DeserializationError::Ok;
  f.close();
  if (!ok) return;
  int i = plFind(d["id"] | "");
  if (i >= 0) { s_cur = i; s_blk = d["blk"] | 0u; }
}

// The .ima header, read straight (used for the duration when the sidecar has
// no "dur" — never while the stream owns the file).
static uint32_t plBlocksOf(const char* name) {
  fs::FS* fs = sdFs();
  if (!fs) return 0;
  char p[128];
  plPath(p, sizeof(p), name, ".ima");
  File f = fs->open(p, FILE_READ);
  if (!f) return 0;
  uint8_t h[PL_HDR];
  uint32_t n = 0;
  if (f.read(h, PL_HDR) == (int)PL_HDR && memcmp(h, "AGPTIMA1", 8) == 0) {
    uint32_t rate = h[8] | (h[9] << 8) | (h[10] << 16) | ((uint32_t)h[11] << 24);
    if (rate == PL_RATE)
      n = h[12] | (h[13] << 8) | (h[14] << 16) | ((uint32_t)h[15] << 24);
  }
  f.close();
  return n;
}

static void plLoadMeta() {
  s_metaIdx = s_cur;
  gInfo.title[0] = gInfo.show[0] = gInfo.cover[0] = 0;
  gInfo.durSec = 0;
  if (s_cur < 0 || s_cur >= s_n) return;
  char name[48];
  snprintf(name, sizeof(name), "%s", s_items[s_cur].name);
  utf8Trunc(gInfo.title, sizeof(gInfo.title), name);   // fallback title
  fs::FS* fs = sdFs();
  if (!fs) return;
  char p[128];
  plPath(p, sizeof(p), name, ".json");
  File f = fs->open(p, FILE_READ);
  if (f) {
    JsonDocument d;
    if (deserializeJson(d, f) == DeserializationError::Ok) {
      const char* ti = d["title"] | "";
      if (ti[0]) utf8Trunc(gInfo.title, sizeof(gInfo.title), ti);
      utf8Trunc(gInfo.show,  sizeof(gInfo.show),  d["show"]  | "");
      utf8Trunc(gInfo.cover, sizeof(gInfo.cover), d["cover"] | "");
      gInfo.durSec = (uint32_t)(d["dur"] | 0);
    }
    f.close();
  }
  if (!gInfo.durSec && !s_running) gInfo.durSec = plBlocksOf(name);
}

// ------------------------------------------------------------------ api
void playerSaveState() {
  fs::FS* fs = sdFs();
  s_saveAt = millis();
  if (!fs || s_cur < 0 || s_cur >= s_n) return;
  File f = fs->open(PL_STATE, FILE_WRITE);
  if (!f) return;
  f.printf("{\"id\":\"%s\",\"blk\":%lu}\n", s_items[s_cur].name, (unsigned long)s_blk);
  f.close();
}

// Stop the stream and wait for the audio task to actually leave it, so the
// loop can close the File / reshuffle the list underneath it. Bounded: one
// decode chunk is 16 ms, a slow card read a few tens more.
static bool plSuspend() {
  bool was = s_want;
  s_want = false;
  uint32_t t0 = millis();
  while (s_running && (int32_t)(millis() - t0) < 400) delay(2);
  return was;
}

bool playerInit() {
  if (!s_items) s_items = (PlItem*)ps_malloc(sizeof(PlItem) * PL_MAX);
  // Internal RAM for the block: a PSRAM destination makes the SD driver
  // bounce every sector through its own buffer, and the read has to finish
  // inside the ~90 ms the I2S DMA holds.
  if (!s_buf) s_buf = (uint8_t*)heap_caps_malloc(PL_BDATA, MALLOC_CAP_DMA);
  if (!s_buf) s_buf = (uint8_t*)ps_malloc(PL_BDATA);
  memset(&gInfo, 0, sizeof(gInfo));
  s_cur = -1; s_blk = 0; s_want = false;
  s_metaIdx = -2;
  if (!s_items || !s_buf) { Serial.println("player: no PSRAM"); return false; }
  plScan();
  plRestore();
  plLoadMeta();
  gInfo.hasItems = s_n > 0;
  Serial.printf("player: %d episode(s) on card, resume %s blk %lu\n", s_n,
                s_cur >= 0 ? s_items[s_cur].name : "-", (unsigned long)s_blk);
  return s_n > 0;
}

// Same scan, but the episode you are listening to keeps playing across it —
// the host pushes a new file while an old one is running all the time.
void playerRescan() {
  if (!s_items) { playerInit(); return; }
  char keep[48];
  keep[0] = 0;
  if (s_cur >= 0 && s_cur < s_n) snprintf(keep, sizeof(keep), "%s", s_items[s_cur].name);
  bool was = plSuspend();
  if (s_file) { s_file.close(); }
  s_openIdx = -1;
  plScan();
  int i = keep[0] ? plFind(keep) : -1;
  if (i >= 0) {
    s_cur = i;
    s_seekGen++;                       // the list moved: make the stream re-open
    if (was) { s_want = true; audioPlayerKick(); }
  } else {
    plRestore();
    s_seekGen++;
  }
  gInfo.hasItems = s_n > 0;
  plLoadMeta();
  Serial.printf("player: rescan -> %d episode(s)%s\n", s_n, was && i >= 0 ? ", still playing" : "");
}

void playerToggle() {
  if (s_n <= 0) return;
  if (s_want) { playerPause(); return; }
  if (s_cur < 0) { s_cur = 0; s_blk = 0; }
  if (s_blk && s_blk >= gInfo.durSec && gInfo.durSec) s_blk = 0;   // finished: from the top
  s_seekGen++;
  s_want = true;
  audioPlayerKick();
}

void playerPause() {
  if (!s_want) return;
  plSuspend();
  playerSaveState();
}

static void plSkip(int delta) {
  if (s_n <= 0) return;
  bool was = plSuspend();
  int nx = (s_cur < 0 ? 0 : s_cur) + delta;
  if (nx < 0)    nx = 0;
  if (nx >= s_n) nx = s_n - 1;
  s_cur = nx;
  s_blk = 0;
  s_seekGen++;
  plLoadMeta();
  if (was) { s_want = true; audioPlayerKick(); }
  else     playerSaveState();
}

void playerNext() { plSkip(+1); }
void playerPrev() { plSkip(-1); }

// ±15 s from the compact card's side zones. Blocks are 1 s, so this is just
// a block move; the stream notices the bumped seekGen at its next pull and
// re-seeks inside the open file (no suspend needed for a seek).
void playerSeek(int32_t dsec) {
  if (s_n <= 0 || s_cur < 0) return;
  int32_t blk = (int32_t)s_blk + dsec;
  if (blk < 0) blk = 0;
  uint32_t total = gInfo.durSec ? gInfo.durSec : (s_openIdx == s_cur ? s_nblk : 0);
  if (total && blk >= (int32_t)total) blk = (int32_t)total - 1;
  s_blk = (uint32_t)blk;
  s_seekGen++;
  if (!s_want) playerSaveState();
}

bool playerPlaying() { return s_want; }
bool playerWantPlay() { return s_want; }
void playerRunSet(bool on) { s_running = on; }
int  playerCount() { return s_n; }

const PlayerInfo& playerInfo() {
  gInfo.hasItems = s_n > 0;
  gInfo.playing  = s_want;
  gInfo.posSec   = s_blk;
  gInfo.index    = s_cur;
  return gInfo;
}

// Metadata follows the stream's own auto-advance, and the resume point goes
// down every 10 s so a yanked battery costs at most that.
void playerPoll() {
  if (s_metaIdx != s_cur) plLoadMeta();
  if (s_want && (int32_t)(millis() - s_saveAt) > 10000) playerSaveState();
}

// ------------------------------------------------------------------ stream
// Everything below runs on the audio task.
static bool plOpen() {
  if (s_file) s_file.close();
  s_openIdx = -1;
  s_nblk = 0; s_bufSamp = 0; s_off = 0;
  fs::FS* fs = sdFs();
  if (!fs || s_cur < 0 || s_cur >= s_n) return false;
  char p[128];
  plPath(p, sizeof(p), s_items[s_cur].name, ".ima");
  s_file = fs->open(p, FILE_READ);
  if (!s_file) return false;
  uint8_t h[PL_HDR];
  if (s_file.read(h, PL_HDR) != (int)PL_HDR || memcmp(h, "AGPTIMA1", 8) != 0) {
    s_file.close();
    Serial.printf("player: %s is not a container\n", p);
    return false;
  }
  uint32_t rate = h[8] | (h[9] << 8) | (h[10] << 16) | ((uint32_t)h[11] << 24);
  if (rate != PL_RATE) {
    s_file.close();
    Serial.printf("player: %s is %lu Hz, need %lu\n", p, (unsigned long)rate,
                  (unsigned long)PL_RATE);
    return false;
  }
  s_nblk = h[12] | (h[13] << 8) | (h[14] << 16) | ((uint32_t)h[15] << 24);
  s_openIdx = s_cur;
  s_openGen = s_seekGen;
  return true;
}

// Load block s_blk. false = past the end (or a bad read).
static bool plBlock() {
  if (!s_file || s_blk >= s_nblk) return false;
  uint32_t want = PL_HDR + s_blk * PL_BSTR;
  if (s_file.position() != want && !s_file.seek(want)) return false;
  uint8_t bh[4];
  if (s_file.read(bh, 4) != 4) return false;
  int n = s_file.read(s_buf, PL_BDATA);          // the last block may be short
  if (n <= 0) return false;
  s_ima.pred = (int16_t)(bh[0] | (bh[1] << 8));
  s_ima.idx  = bh[2];
  if (s_ima.idx > 88) s_ima.idx = 88;
  s_bufSamp = (uint32_t)n * 2;
  s_off = 0;
  return true;
}

uint32_t playerPull(int16_t* out, uint32_t maxSamples) {
  if (!s_want || s_n <= 0 || !s_buf) return 0;
  if (s_openIdx != s_cur || s_openGen != s_seekGen) {
    while (!plOpen()) {                          // skip files that aren't containers
      if (s_cur < 0 || s_cur + 1 >= s_n) { s_want = false; return 0; }
      s_cur = s_cur + 1;
      s_blk = 0;
      s_openGen = s_seekGen;
    }
    if (s_blk >= s_nblk) s_blk = 0;              // stale resume point
  }
  while (s_off >= s_bufSamp) {                   // this block is spent
    if (!plBlock()) {
      if (s_cur + 1 < s_n) {                     // roll into the next episode
        s_cur = s_cur + 1;
        s_blk = 0;
        s_openGen = s_seekGen;
        if (plOpen()) continue;
      }
      s_want = false;                            // last one: stop where we are
      return 0;
    }
  }
  uint32_t n = s_bufSamp - s_off;
  if (n > maxSamples) n = maxSamples;
  for (uint32_t i = 0; i < n; i++) {
    uint32_t s = s_off + i;
    uint8_t b = s_buf[s >> 1];
    out[i] = imaDecodeStep(s_ima, (s & 1) ? (uint8_t)(b >> 4) : (uint8_t)(b & 0x0F));
  }
  s_off += n;
  if (s_off >= s_bufSamp) s_blk = s_blk + 1;     // position advances a whole second at a time
  return n;
}
