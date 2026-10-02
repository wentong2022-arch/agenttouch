// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
// ES8311 playback path + synthesized chirps.
// The register sequences in codecInit() (ES8311) and micCodecInit() (ES7210)
// are derived from espressif/esp-bsp components/es8311 and components/es7210,
// Copyright (c) 2015-2026 Espressif Systems (Shanghai) CO LTD, Apache-2.0
// (THIRD_PARTY_LICENSES/esp-bsp-Apache-2.0.txt). Modified: resolved for one
// clock setup and written over Wire instead of the esp_codec_dev/i2c driver.
// Register names encode their addresses (REG0D = 0x0D); values below are the
// driver's sequence resolved for MCLK-from-pin 4.096 MHz, 16 kHz, 16-bit.
#include <Arduino.h>
#include <Wire.h>
#include <ESP_I2S.h>
#include <Preferences.h>
#include <math.h>
#include "pins.h"
#include "config.h"
#include "audio.h"
#include "player.h"

static const uint8_t ES8311_ADDR = 0x18;
static const int     SRATE       = 16000;

static I2SClass       i2s;
static QueueHandle_t  sndQ  = nullptr;
static bool           ready = false;
static Preferences    prefs;
static uint8_t        s_volume  = SOUND_VOLUME;   // 0..100, NVS-backed
static uint8_t        s_preMute = SOUND_VOLUME;   // restored by unmute
static bool           s_suppress = false;         // face-down hush

// The i2s driver allocates its channel context with MALLOC_CAP_DEFAULT, which
// lands in PSRAM on this heap layout; GDMA_ISR_IRAM_SAFE then rejects it as
// ISR user context. The prebuilt IDF can't be reconfigured, so these linker
// wraps (see platformio.ini) force default-caps allocations to internal RAM
// while audioInit() runs the i2s begin sequence.
static volatile bool s_forceInternal = false;
extern "C" {
void* __real_heap_caps_malloc(size_t size, uint32_t caps);
void* __real_heap_caps_calloc(size_t n, size_t size, uint32_t caps);
void* __wrap_heap_caps_malloc(size_t size, uint32_t caps) {
  if (s_forceInternal && !(caps & (MALLOC_CAP_INTERNAL | MALLOC_CAP_SPIRAM)))
    caps |= MALLOC_CAP_INTERNAL;
  return __real_heap_caps_malloc(size, caps);
}
void* __wrap_heap_caps_calloc(size_t n, size_t size, uint32_t caps) {
  if (s_forceInternal && !(caps & (MALLOC_CAP_INTERNAL | MALLOC_CAP_SPIRAM)))
    caps |= MALLOC_CAP_INTERNAL;
  return __real_heap_caps_calloc(n, size, caps);
}
}

// ------------------------------------------------------------------ codec
static bool wr(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(ES8311_ADDR);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

static uint8_t rd(uint8_t reg) {
  Wire.beginTransmission(ES8311_ADDR);
  Wire.write(reg);
  Wire.endTransmission(false);
  Wire.requestFrom(ES8311_ADDR, (uint8_t)1);
  return Wire.available() ? Wire.read() : 0;
}

// Reg 0x32: 0.5 dB per step, 0x00 = -95.5 dB, 0xBF = 0 dB, above that is
// DIGITAL GAIN up to +32 dB — hard clipping ("scratchy" static at max, found
// on device 2026-08-28). So cap at 0 dB and map 1..100 linearly in dB over
// a 50 dB span (91+vol): 100 -> 0 dB clean maximum, 70 -> -15 dB.
static uint8_t volReg(uint8_t vol) {
  return vol == 0 ? 0 : (uint8_t)(91 + vol);
}

static bool codecInit() {
  if (!wr(0x00, 0x1F)) return false;   // reset
  delay(20);
  wr(0x00, 0x00);
  wr(0x00, 0x80);                      // power on, slave mode
  // clocks: MCLK from pin (4.096 MHz = 256 * 16 kHz), all clocks on
  wr(0x01, 0x3F);
  wr(0x02, rd(0x02) & 0x07);           // pre_div 1, pre_multi 1x
  wr(0x03, 0x10);                      // single speed, adc_osr
  wr(0x04, 0x10);                      // dac_osr
  wr(0x05, 0x00);                      // adc/dac div 1
  wr(0x06, (rd(0x06) & 0xE0) | 0x03);  // bclk div 4, sclk not inverted
  wr(0x07, rd(0x07) & 0xC0);           // lrck_h
  wr(0x08, 0xFF);                      // lrck_l
  // format: I2S slave, 16-bit in/out
  wr(0x00, rd(0x00) & 0xBF);
  wr(0x09, 0x0C);
  wr(0x0A, 0x0C);
  // analog power-up (playback path)
  wr(0x0D, 0x01);
  wr(0x0E, 0x02);
  wr(0x12, 0x00);                      // power up DAC
  wr(0x13, 0x10);                      // enable HP drive output
  wr(0x1C, 0x6A);
  wr(0x37, 0x08);                      // bypass DAC equalizer
  wr(0x32, volReg(s_volume));          // DAC volume
  return true;
}

// ------------------------------------------------------------------ synth
// One phase-continuous sine sweep with click-free attack/release edges
// (edges shrink for very short notes so fast bendy blips keep their volume).
static void sweep(float f0, float f1, int ms, float amp) {
  static int16_t buf[256 * 2];
  const int total = SRATE * ms / 1000;
  static float phase = 0;
  const int edge = min(120, total / 3);
  int done = 0;
  while (done < total) {
    int n = min(256, total - done);
    for (int i = 0; i < n; i++) {
      float k = (float)(done + i) / total;
      float f = f0 + (f1 - f0) * k;
      phase += 2.0f * 3.14159265f * f / SRATE;
      float env = 1.0f;
      int at = done + i, left = total - at;
      if (at < edge)   env = (float)at / edge;
      if (left < edge) env = min(env, (float)left / edge);
      int16_t s = (int16_t)(sinf(phase) * amp * 32767.0f * env);
      buf[i * 2] = buf[i * 2 + 1] = s;
    }
    i2s.write((uint8_t*)buf, n * 4);
    done += n;
  }
}

static void silence(int ms);

// Otto/Zowi-style stepped exponential bend: short notes climbing (or falling)
// by `prop` per step, with tiny gaps — the granular warble that makes those
// robots sound alive. Frequencies/timings below come from OttoDIYLib's
// sing() table, softened by our sine timbre (theirs is a square buzzer).
static void bend(float f0, float f1, float prop, int noteMs, int gapMs,
                 float amp) {
  if (f0 < f1)
    for (float f = f0; f < f1; f *= prop) { sweep(f, f, noteMs, amp); silence(gapMs); }
  else
    for (float f = f0; f > f1; f /= prop) { sweep(f, f, noteMs, amp); silence(gapMs); }
}

static void silence(int ms) {
  static const int16_t z[64] = {0};
  int total = SRATE * ms / 1000;
  for (int done = 0; done < total; done += 16)
    i2s.write((uint8_t*)z, min(16, total - done) * 4);
}

// Emotion vocabulary adapted from OttoDIYLib sing() — community-proven
// pitch contours: rising = positive/greeting, falling = rest/negative,
// up-then-down = laugh, sweep-plus-staccato = "uh-oh, look at me".
static void renderSound(uint8_t id) {
  switch (id) {
    case SND_BOOT:                        // S_connection: hello!
      sweep(659, 659, 50, 0.30f);  silence(30);
      sweep(1319, 1319, 55, 0.30f); silence(25);
      sweep(1760, 1760, 60, 0.30f);
      break;
    case SND_BYE:                         // S_disconnection: bye-bye
      sweep(659, 659, 50, 0.30f);  silence(30);
      sweep(1760, 1760, 55, 0.30f); silence(25);
      sweep(1319, 1319, 50, 0.30f);
      break;
    case SND_SELECT:                      // S_buttonPushed: crisp double blip
      bend(1319, 1568, 1.03f, 20, 2, 0.26f); silence(30);
      bend(1319, 2349, 1.04f, 10, 2, 0.26f);
      break;
    case SND_NEEDS:                       // S_OhOoh: rise + beep-beep-beep
      bend(880, 2000, 1.04f, 8, 3, 0.32f);
      silence(200);
      for (int i = 0; i < 10; i++) { sweep(988, 988, 15, 0.32f); silence(25); }
      break;
    case SND_DONE:                        // S_happy: up-down laugh
      bend(1500, 2500, 1.05f, 20, 8, 0.28f);
      bend(2499, 1500, 1.05f, 25, 8, 0.28f);
      break;
    case SND_VOICE_ON:                    // S_mode1: rising "listening?"
      bend(1319, 1760, 1.02f, 30, 10, 0.26f);
      break;
    case SND_VOICE_OFF:                   // inverse of voice_on
      bend(1760, 1319, 1.02f, 30, 10, 0.26f);
      break;
    case SND_SURPRISE:                    // S_surprise: quick gasp on pickup
      bend(800, 2150, 1.02f, 10, 1, 0.28f);
      bend(2149, 800, 1.03f, 7, 1, 0.28f);
      break;
    case SND_TICK:                        // volume feedback: one clean blip
      sweep(1568, 1568, 28, 0.30f);
      break;
    case SND_PURR:                        // cat-purr rumble: slow low wobble
      for (int i = 0; i < 3; i++) {
        sweep(196, 147, 70, 0.20f);
        sweep(147, 196, 70, 0.20f);
      }
      break;
    case SND_NOM:                         // om-nom-nom: three chewy low blips
      for (int i = 0; i < 3; i++) {
        sweep(392, 294, 60, 0.26f);
        silence(45);
      }
      break;
    case SND_BURP:                        // stepped falling belch + "excuse me"
      bend(320, 140, 1.06f, 18, 3, 0.30f);
      silence(180);
      sweep(1319, 1568, 45, 0.18f);
      break;
    case SND_DIZZY:                       // drunken siren wobbling down
      sweep(600, 900, 80, 0.26f);
      sweep(900, 480, 95, 0.26f);
      sweep(480, 760, 80, 0.24f);
      sweep(760, 350, 110, 0.22f);
      break;
    case SND_SIGH:                        // soft falling two-tone sigh
      sweep(880, 660, 130, 0.16f);
      silence(70);
      sweep(660, 494, 180, 0.13f);
      break;
  }
}

// ------------------------------------------------------------------ speech
// One PSRAM clip buffer, filled from the net loop, played by audioTask.
// 2026-09-05: playback starts once SPEAK_START bytes have
// landed and then keeps pace with the network (the link is faster than the
// 32 KB/s real-time rate) instead of waiting for the whole clip; the wire
// format may be IMA ADPCM (4:1), decoded here on the fly.
static const uint32_t SPEAK_MAX   = 1200 * 1024;  // ~38 s of 16 kHz speech
static const uint32_t SPEAK_START = 8 * 1024;     // 0.25 s in the buffer before we start
static int16_t*          s_pcm     = nullptr;     // lazily ps_malloc'd
static volatile uint32_t s_pcmWant = 0;           // expected PCM bytes of the clip in flight (0 = none)
static volatile uint32_t s_pcmFill = 0;           // PCM bytes landed so far
static volatile bool     s_pcmBusy = false;       // player queued or running
static uint8_t           s_fmt     = 0;           // 0 pcm, 1 ima adpcm
static int               s_imaPred = 0, s_imaIdx = 0;
static uint32_t          s_t0 = 0, s_tStart = 0, s_tDone = 0;
static SpeakStats        s_stats;
static volatile bool     s_statsPending = false;

static const int8_t  IMA_IDX[16] = {-1, -1, -1, -1, 2, 4, 6, 8, -1, -1, -1, -1, 2, 4, 6, 8};
static const int16_t IMA_STEP[89] = {
  7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
  50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
  253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
  1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
  3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
  11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
  32767};

// State lives in the caller: the TTS clip decodes on the loop task while the
// episode player decodes on the audio task, and the two must not share a
// predictor.
int16_t imaDecodeStep(ImaDec& st, uint8_t nib) {
  int step = IMA_STEP[st.idx];
  int diff = step >> 3;
  if (nib & 1) diff += step >> 2;
  if (nib & 2) diff += step >> 1;
  if (nib & 4) diff += step;
  if (nib & 8) st.pred -= diff; else st.pred += diff;
  if (st.pred > 32767) st.pred = 32767; else if (st.pred < -32768) st.pred = -32768;
  st.idx += IMA_IDX[nib];
  if (st.idx < 0) st.idx = 0; else if (st.idx > 88) st.idx = 88;
  return (int16_t)st.pred;
}

static inline int16_t imaDecode(uint8_t nib) {
  ImaDec st = {s_imaPred, s_imaIdx};
  int16_t v = imaDecodeStep(st, nib);
  s_imaPred = st.pred; s_imaIdx = st.idx;
  return v;
}

bool audioSpeakBegin(uint32_t bytes, uint8_t fmt) {
  if (!ready || s_pcmBusy || bytes == 0 || bytes > SPEAK_MAX) return false;
  if (!s_pcm) s_pcm = (int16_t*)ps_malloc(SPEAK_MAX);
  if (!s_pcm) return false;
  s_fmt = fmt; s_imaPred = 0; s_imaIdx = 0;
  s_pcmFill = 0;
  s_t0 = millis(); s_tStart = 0; s_tDone = 0;
  s_pcmWant = bytes & ~1u;             // whole int16 samples; set last, it arms audioSpeakData
  return true;
}

static void speakKick() {              // enough data (or all of it): queue the player once
  if (s_pcmBusy) return;
  if (s_volume <= 0 || s_suppress) {   // muted: swallow the clip once it is complete
    if (s_pcmFill >= s_pcmWant) s_pcmWant = 0;
    return;
  }
  s_pcmBusy = true;
  s_tStart = millis();
  uint8_t id = SND_SPEAK;
  if (xQueueSend(sndQ, &id, 0) != pdTRUE) { s_pcmBusy = false; s_pcmWant = 0; }
}

void audioSpeakData(const uint8_t* data, uint32_t n) {
  if (!s_pcmWant) return;
  uint32_t room = s_pcmWant - s_pcmFill;
  uint8_t* dst = (uint8_t*)s_pcm + s_pcmFill;
  if (s_fmt == 1) {                    // IMA ADPCM: one wire byte -> two samples, low nibble first
    int16_t* o = (int16_t*)dst;
    uint32_t outB = 0;
    for (uint32_t i = 0; i < n && outB + 4 <= room; i++) {
      o[0] = imaDecode(data[i] & 0x0F);
      o[1] = imaDecode(data[i] >> 4);
      o += 2; outB += 4;
    }
    n = outB;
  } else {
    if (n > room) n = room;
    memcpy(dst, data, n);
  }
  s_pcmFill += n;                      // publish after the bytes are in place
  if (s_pcmFill >= s_pcmWant) s_tDone = millis();
  if (s_pcmFill >= s_pcmWant || s_pcmFill >= SPEAK_START) speakKick();
}

bool audioSpeakTakeStats(SpeakStats& out) {
  if (!s_statsPending) return false;
  out = s_stats;
  s_statsPending = false;
  return true;
}

static void playPcm() {
  static int16_t out[256 * 2];         // mono -> interleaved stereo
  const int16_t* src = s_pcm;
  uint32_t total = s_pcmWant / 2, done = 0;
  uint32_t idleSince = millis();
  bool underrun = false;
  while (done < total) {
    uint32_t avail = s_pcmFill / 2;
    if (done >= avail) {               // caught up with the network: wait for more
      if ((int32_t)(millis() - idleSince) > 1500) { underrun = true; break; }
      vTaskDelay(pdMS_TO_TICKS(5));
      continue;
    }
    idleSince = millis();
    uint32_t c = min((uint32_t)256, avail - done);
    for (uint32_t i = 0; i < c; i++) out[i * 2] = out[i * 2 + 1] = src[done + i];
    i2s.write((uint8_t*)out, c * 4);
    done += c;
  }
  uint32_t now = millis();
  s_stats = {done * 2, s_tStart - s_t0, (s_tDone ? s_tDone : now) - s_t0,
             now - s_tStart, s_fmt, underrun};
  s_statsPending = true;
  s_pcmWant = 0;
  s_pcmBusy = false;
}

// ------------------------------------------------------------------ player
// The episode player (player.cpp) reads and decodes; this side only lays the
// mono samples out as stereo and watches the queue. 256 samples = 16 ms, so a
// chirp or a TTS clip waits at most that long — and the player resumes from
// the exact sample it stopped on, mid-block, when audioTask hands it back.
static volatile bool s_plQueued = false;

void audioPlayerKick() {
  if (!ready || s_plQueued) return;
  uint8_t id = SND_PLAYER;
  s_plQueued = true;
  if (xQueueSend(sndQ, &id, 0) != pdTRUE) s_plQueued = false;
}

static void playPlayer() {
  static int16_t mono[256], out[256 * 2];
  playerRunSet(true);
  for (;;) {
    if (uxQueueMessagesWaiting(sndQ) > 0) break;   // 让位: something else wants the speaker
    uint32_t n = playerPull(mono, 256);
    if (!n) break;
    for (uint32_t i = 0; i < n; i++) out[i * 2] = out[i * 2 + 1] = mono[i];
    i2s.write((uint8_t*)out, n * 4);
  }
  playerRunSet(false);
}

static volatile bool     s_paOn    = false;   // amp enabled (something is sounding)
static volatile uint32_t s_paOffAt = 0;       // millis() the amp last went quiet
bool audioOutputActive() { return s_paOn || (int32_t)(millis() - s_paOffAt) < 300; }

static void audioTask(void*) {
  uint8_t id;
  bool paOn = false;
  for (;;) {
    if (!xQueueReceive(sndQ, &id, portMAX_DELAY)) continue;
    if (!paOn) {
      digitalWrite(PIN_PA_CTRL, HIGH);   // PA on only while playing (no idle hiss)
      paOn = true; s_paOn = true;
      silence(20);                       // let the amp settle, avoids the pop
    }
    do {
      if      (id == SND_PLAYER) { s_plQueued = false; playPlayer(); }
      else if (id == SND_SPEAK)  playPcm();
      else                       renderSound(id);
    } while (xQueueReceive(sndQ, &id, 0));
    // The episode is still what you are listening to: put it back behind the
    // chirps that just cut in, with the PA left on so there is no pop.
    if (playerWantPlay()) {
      uint8_t p = SND_PLAYER;
      s_plQueued = true;
      if (xQueueSend(sndQ, &p, 0) == pdTRUE) continue;
      s_plQueued = false;
    }
    silence(30);
    digitalWrite(PIN_PA_CTRL, LOW);
    paOn = false; s_paOn = false; s_paOffAt = millis();
  }
}

// ------------------------------------------------------------------ microphone
// ES7210 4-channel ADC (two MEMS mics on ADC1/ADC2 here), I2S slave on the
// playback port. Register sequence derived from espressif/esp-bsp
// components/es7210 (es7210_config_codec, Apache-2.0, see the top of this
// file), clock coefficients for MCLK 4.096 MHz / 16 kHz.
#if MIC_ENABLED
static uint8_t  s_micAddr   = 0;                 // 0x40..0x43 by AD0/AD1 straps, 0 = not found
static bool     s_micReady  = false;
static volatile bool s_micOn = false;
static volatile uint8_t s_micSkip = 0;           // frames to discard after start (stale DMA)
static uint8_t  s_micCh     = 0;                 // 0 L, 1 R, 2 average
static const int MIC_SAMPLES = SRATE * MIC_FRAME_MS / 1000;   // 640
static const int MIC_RING    = 32;               // 1.28 s of frames in PSRAM
static MicFrame* s_micRing   = nullptr;
static volatile uint16_t s_micHead = 0, s_micTail = 0;   // SPSC: task writes head, loop reads tail
static portMUX_TYPE s_micMux = portMUX_INITIALIZER_UNLOCKED;

// ---- 眼神追声 -------------------------------------------
// Plain cross-correlation of the two mics over ±GAZE_MAXL samples with a
// parabolic peak fit. The mics sit ≈ 39 mm apart → at most ~1.8 samples of
// delay at 16 kHz, so the answer is coarse (left / centre / right with some
// gradation) — all a pair of eyes needs. Gated by frame level, by peak
// quality (no peak on the search edge, normalized peak ≥ minConf) and by
// the board's own speaker (it must not stare at itself). ~4.4k MACs per
// 40 ms frame on the mic task, nothing on the loop.
static volatile bool s_gazeOn  = true;
static volatile bool s_gazeDbg = false;
static int      s_gazeMinRms   = 150;            // frame RMS gate, 16-bit units
static float    s_gazeFloor    = 0;              // slow EMA of frame RMS (room noise)
static float    s_gazeMinConf  = 0.55f;          // normalized peak gate
static GazeEst  s_gaze         = {};
static const int   GAZE_HIST   = 24;
static GazeSample  s_gazeHist[GAZE_HIST];
static volatile uint8_t s_gazeHistW = 0;        // next write slot (ring)
static const int   GAZE_MAXL   = 3;
static const float GAZE_LAG_FS = 1.8f;           // d·fs/c: samples of delay at 90°

static void gazeFeed(const int16_t* rx, uint32_t nowMs) {
  const int N = MIC_SAMPLES;
  int64_t el = 0, er = 0;
  for (int i = 0; i < N; i++) {
    int32_t l = rx[i * 2], r = rx[i * 2 + 1];
    el += l * l; er += r * r;
  }
  float rmsL = sqrtf((float)el / N), rmsR = sqrtf((float)er / N);
  float rms = rmsL > rmsR ? rmsL : rmsR;
  // Noise floor: a slow EMA (rises ~6 s, falls ~2 s at 25 fps) so a fan or a
  // TV in the room is not "a sound" — the two mics hear steady room noise
  // almost identically, so the correlation gate alone would pass it with
  // conf ≈ 1. A burst must clear the fixed gate AND 2.5× the floor.
  s_gazeFloor += (rms - s_gazeFloor) * (rms < s_gazeFloor ? 0.02f : 0.006f);
  s_gaze.floor = (uint16_t)s_gazeFloor;
  if (rms < s_gazeMinRms || rms < 2.5f * s_gazeFloor) return;
  if (audioOutputActive()) return;
  // r[k] = Σ L[i]·R[i+k]; R lagging L (MIC1 heard it first) peaks at k > 0
  int64_t acc[2 * GAZE_MAXL + 1];
  for (int k = -GAZE_MAXL; k <= GAZE_MAXL; k++) {
    int64_t s = 0;
    for (int i = GAZE_MAXL; i < N - GAZE_MAXL; i++)
      s += (int32_t)rx[i * 2] * (int32_t)rx[(i + k) * 2 + 1];
    acc[k + GAZE_MAXL] = s;
  }
  int best = 0;
  for (int j = 1; j < 2 * GAZE_MAXL + 1; j++) if (acc[j] > acc[best]) best = j;
  if (best == 0 || best == 2 * GAZE_MAXL) return;   // peak on the edge: not a clean delay
  float norm = sqrtf((float)el * (float)er);
  if (norm <= 0) return;
  float conf = (float)acc[best] / norm;
  if (conf < s_gazeMinConf) return;
  float y0 = (float)acc[best - 1], y1 = (float)acc[best], y2 = (float)acc[best + 1];
  float den = y0 - 2 * y1 + y2;
  float delta = den < 0 ? 0.5f * (y0 - y2) / den : 0;
  if (delta > 0.5f) delta = 0.5f; else if (delta < -0.5f) delta = -0.5f;
  float lag = (float)(best - GAZE_MAXL) + delta;
  float az = lag / GAZE_LAG_FS;
  if (az > 1) az = 1; else if (az < -1) az = -1;
  bool fresh = (int32_t)(nowMs - s_gaze.ms) > 400;   // a new burst snaps, a running one smooths
  s_gaze.az = fresh ? az : 0.6f * s_gaze.az + 0.4f * az;
  s_gaze.conf = conf; s_gaze.lag = lag; s_gaze.rms = (uint16_t)rms;
  s_gaze.ms = nowMs; s_gaze.n++;
  s_gazeHist[s_gazeHistW] = GazeSample{az, (uint16_t)rms, nowMs};   // raw per-frame az, not the smoothed one
  s_gazeHistW = (s_gazeHistW + 1) % GAZE_HIST;
  if (s_gazeDbg) {
    static uint32_t lastDbg = 0;
    if ((int32_t)(nowMs - lastDbg) > 250) {
      lastDbg = nowMs;
      Serial.printf("gaze: lag %+.2f az %+.2f conf %.2f rms %d (L %d R %d)\n",
                    lag, s_gaze.az, conf, (int)rms, (int)rmsL, (int)rmsR);
    }
  }
}
static MicStats s_micStats;
static uint16_t s_micSeq = 0;
static int      s_encPred = 0, s_encIdx = 0;     // IMA encoder state (reset per start)

static bool micWr(uint8_t reg, uint8_t val) {
  Wire.beginTransmission(s_micAddr);
  Wire.write(reg);
  Wire.write(val);
  return Wire.endTransmission() == 0;
}

static bool micCodecInit() {
  for (uint8_t a = 0x40; a <= 0x43; a++) {       // probe the strap-selected address
    Wire.beginTransmission(a);
    if (Wire.endTransmission() == 0) { s_micAddr = a; break; }
  }
  if (!s_micAddr) return false;
  if (!micWr(0x00, 0xFF)) return false;          // software reset
  micWr(0x00, 0x32);
  micWr(0x09, 0x30);                             // power-up timing
  micWr(0x0A, 0x30);
  micWr(0x23, 0x2A);                             // HPF ADC1/2
  micWr(0x22, 0x0A);
  micWr(0x21, 0x2A);                             // HPF ADC3/4
  micWr(0x20, 0x0A);
  micWr(0x11, 0x60);                             // 16-bit, standard I2S
  micWr(0x12, 0x00);                             // no TDM: ADC1/ADC2 on SDOUT as L/R
  micWr(0x40, 0xC3);                             // analog power + VMID
  micWr(0x41, 0x70);                             // mic bias 2.87 V
  micWr(0x42, 0x70);
  for (uint8_t r = 0x43; r <= 0x46; r++) micWr(r, (MIC_GAIN & 0x0F) | 0x10);   // PGA gain
  for (uint8_t r = 0x47; r <= 0x4A; r++) micWr(r, 0x08);                       // mic power
  // clocks for MCLK 4.096 MHz / LRCK 16 kHz (esp-bsp coeff table row):
  // osr 0x20, adc_div 1 | doubler<<6 | dll<<7, lrck div 0x0100
  micWr(0x07, 0x20);
  micWr(0x02, 0x01 | (1 << 6) | (1 << 7));
  micWr(0x04, 0x01);
  micWr(0x05, 0x00);
  micWr(0x06, 0x04);                             // DLL power down
  micWr(0x4B, 0x0F);                             // bias + ADC + PGA power, ch 1-2
  micWr(0x4C, 0x0F);                             // ch 3-4
  micWr(0x00, 0x71);                             // enable
  micWr(0x00, 0x41);
  return true;
}

static inline uint8_t imaEncode(int16_t s) {
  int step = IMA_STEP[s_encIdx];
  int diff = s - s_encPred;
  uint8_t nib = 0;
  if (diff < 0) { nib = 8; diff = -diff; }
  int d = step >> 3;
  if (diff >= step)      { nib |= 4; diff -= step;      d += step; }
  if (diff >= step >> 1) { nib |= 2; diff -= step >> 1; d += step >> 1; }
  if (diff >= step >> 2) { nib |= 1;                    d += step >> 2; }
  s_encPred += (nib & 8) ? -d : d;
  if (s_encPred > 32767) s_encPred = 32767; else if (s_encPred < -32768) s_encPred = -32768;
  s_encIdx += IMA_IDX[nib];
  if (s_encIdx < 0) s_encIdx = 0; else if (s_encIdx > 88) s_encIdx = 88;
  return nib;
}

static void micTask(void*) {
  static int16_t rx[MIC_SAMPLES * 2];            // interleaved L/R, 2560 B
  for (;;) {
    size_t got = i2s.readBytes((char*)rx, sizeof(rx));   // blocks on DMA; keeps it drained
    if (got != sizeof(rx)) { delay(1); continue; }
    if (s_gazeOn) gazeFeed(rx, millis());        // 眼神追声 listens whether or not we stream
    if (!s_micOn) continue;
    if (s_micSkip) { s_micSkip--; continue; }
    uint16_t head = s_micHead, next = (head + 1) % MIC_RING;
    MicFrame* f = &s_micRing[head];
    int16_t pkL = 0, pkR = 0, pk = 0;
    uint32_t sq = 0;
    for (int i = 0; i < MIC_SAMPLES; i++) {
      int16_t l = rx[i * 2], r = rx[i * 2 + 1];
      int al = l < 0 ? -l : l, ar = r < 0 ? -r : r;
      if (al > pkL) pkL = al;
      if (ar > pkR) pkR = ar;
      int16_t m = s_micCh == 0 ? l : s_micCh == 1 ? r : (int16_t)(((int)l + r) / 2);
      int am = m < 0 ? -m : m;
      if (am > pk) pk = am;
      sq += (uint32_t)((int)m * m) >> 8;
      uint8_t nib = imaEncode(m);
      if (i & 1) f->d[i >> 1] |= nib << 4; else f->d[i >> 1] = nib;
    }
    f->seq = s_micSeq++; f->peak = pk; f->ms = millis();
    s_micStats.peakL = pkL; s_micStats.peakR = pkR;
    s_micStats.rms = (uint16_t)sqrtf((float)sq * 256.0f / MIC_SAMPLES);
    if (next == s_micTail) { s_micStats.dropped++; continue; }   // ring full: drop this frame
    s_micStats.frames++;
    portENTER_CRITICAL(&s_micMux);
    s_micHead = next;
    portEXIT_CRITICAL(&s_micMux);
  }
}

static void micInit() {
  s_micRing = (MicFrame*)heap_caps_malloc(sizeof(MicFrame) * MIC_RING, MALLOC_CAP_SPIRAM);
  if (!s_micRing) { Serial.println("mic: no PSRAM for ring"); return; }
  if (!micCodecInit()) { Serial.println("mic: es7210 not found on I2C 0x40-0x43"); return; }
  xTaskCreatePinnedToCore(micTask, "mic", 6144, nullptr, 2, nullptr, 0);
  s_micReady = true;
  Serial.printf("mic: es7210 @0x%02X up, %d ms frames, gain step %d\n", s_micAddr, MIC_FRAME_MS, MIC_GAIN);
}

bool audioMicAvailable() { return s_micReady; }
bool audioMicActive()    { return s_micReady && s_micOn; }
void audioMicChannel(uint8_t ch) { s_micCh = ch > 2 ? 0 : ch; }

void audioMicStart() {
  if (!s_micReady || s_micOn) return;
  portENTER_CRITICAL(&s_micMux);
  s_micHead = s_micTail = 0;
  portEXIT_CRITICAL(&s_micMux);
  s_micStats = MicStats{}; s_micStats.ch = s_micCh;
  s_micSeq = 0; s_encPred = 0; s_encIdx = 0;
  s_micSkip = 2;                                 // whatever DMA buffered before the press
  s_micOn = true;
}

void audioMicStop() { s_micOn = false; }

bool audioMicPop(MicFrame& out) {
  if (!s_micRing) return false;
  uint16_t tail = s_micTail;
  if (tail == s_micHead) return false;
  memcpy(&out, &s_micRing[tail], sizeof(MicFrame));
  portENTER_CRITICAL(&s_micMux);
  s_micTail = (tail + 1) % MIC_RING;
  portEXIT_CRITICAL(&s_micMux);
  return true;
}

MicStats audioMicStats() { MicStats st = s_micStats; st.ch = s_micCh; return st; }
GazeEst audioGaze()               { return s_gaze; }
int audioGazeHist(GazeSample* out, int max) {
  int n = 0;
  uint8_t w = s_gazeHistW;
  for (int i = 1; i <= GAZE_HIST && n < max; i++) {
    const GazeSample& s = s_gazeHist[(w + GAZE_HIST - i) % GAZE_HIST];
    if (!s.ms) break;
    out[n++] = s;
  }
  return n;
}
void    audioGazeEnable(bool on)  { s_gazeOn = on; }
bool    audioGazeEnabled()        { return s_gazeOn; }
void    audioGazeTune(int minRms, float minConf) {
  if (minRms >= 0) s_gazeMinRms = minRms;
  if (minConf >= 0) s_gazeMinConf = minConf;
}
void    audioGazeDebug(bool on)   { s_gazeDbg = on; }
#else
bool audioMicAvailable() { return false; }
bool audioMicActive()    { return false; }
void audioMicChannel(uint8_t) {}
void audioMicStart() {}
void audioMicStop() {}
bool audioMicPop(MicFrame&) { return false; }
MicStats audioMicStats() { return MicStats{}; }
GazeEst audioGaze()               { return GazeEst{}; }
int     audioGazeHist(GazeSample*, int) { return 0; }
void    audioGazeEnable(bool)     {}
bool    audioGazeEnabled()        { return false; }
void    audioGazeTune(int, float) {}
void    audioGazeDebug(bool)      {}
#endif

// ------------------------------------------------------------------ api
bool audioInit() {
#if !SOUND_ENABLED
  return false;
#endif
  prefs.begin("agentpet", false);
  s_volume  = prefs.getUChar("vol", SOUND_VOLUME);
  s_preMute = prefs.getUChar("pvol", SOUND_VOLUME);
  pinMode(PIN_PA_CTRL, OUTPUT);
  digitalWrite(PIN_PA_CTRL, LOW);
#if MIC_ENABLED
  // full duplex: the ES7210 hangs off the same BCLK/LRCK/MCLK, so its RX
  // channel must be created together with playback TX on this one port
  i2s.setPins(PIN_I2S_BCLK, PIN_I2S_LRCK, PIN_I2S_DOUT, PIN_I2S_DIN, PIN_I2S_MCLK);
#else
  i2s.setPins(PIN_I2S_BCLK, PIN_I2S_LRCK, PIN_I2S_DOUT, -1, PIN_I2S_MCLK);
#endif
  s_forceInternal = true;                // see the heap_caps wraps above
  bool ok = i2s.begin(I2S_MODE_STD, SRATE, I2S_DATA_BIT_WIDTH_16BIT,
                      I2S_SLOT_MODE_STEREO);
  s_forceInternal = false;
  if (!ok) {
    Serial.println("audio: i2s begin FAILED");
    return false;
  }
  if (!codecInit()) {
    Serial.println("audio: es8311 not responding");
    return false;
  }
  sndQ = xQueueCreate(6, 1);
  xTaskCreatePinnedToCore(audioTask, "audio", 8192, nullptr, 1, nullptr, 0);   // +SD reads: the player streams from the card on this task
  ready = true;
  Serial.println("audio: up");
#if MIC_ENABLED
  micInit();
#endif
  return true;
}

void audioPlay(uint8_t id) {
  if (ready && s_volume > 0 && !s_suppress) xQueueSend(sndQ, &id, 0);
}

void audioSuppress(bool on) { s_suppress = on; }

void audioSetVolume(int vol) {
  if (vol < 0) vol = 0;
  if (vol > 100) vol = 100;
  if (vol > 0) s_preMute = (uint8_t)vol;
  s_volume = (uint8_t)vol;
  if (ready) wr(0x32, volReg(s_volume));
  prefs.putUChar("vol", s_volume);
  prefs.putUChar("pvol", s_preMute);
}

uint8_t audioVolume() { return s_volume; }
bool    audioMuted()  { return s_volume == 0; }

void audioToggleMute() {
  audioSetVolume(s_volume == 0 ? (s_preMute ? s_preMute : SOUND_VOLUME) : 0);
}

int soundIdByName(const char* name) {
  static const char* N[] = {"boot", "bye", "select", "needs", "done",
                            "voice_on", "voice_off", "surprise", "tick",
                            "purr", "nom", "burp", "dizzy", "sigh"};
  for (int i = 0; i < 14; i++)
    if (strcmp(name, N[i]) == 0) return i;
  return -1;
}
