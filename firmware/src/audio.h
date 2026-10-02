// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
#pragma once
#include <stdint.h>

// Cute synthesized chirps through the ES8311 codec + speaker.
enum SoundId : uint8_t {
  SND_BOOT = 0,   // hello chirp on power-up
  SND_BYE,        // falling tone before shutdown
  SND_SELECT,     // tiny blip on manual agent switch
  SND_NEEDS,      // "chirp-chirp?" — an agent wants you
  SND_DONE,       // ding-dong — an agent finished
  SND_VOICE_ON,   // rising blip when hold-to-talk starts
  SND_VOICE_OFF,  // falling blip when it ends
  SND_SURPRISE,   // gasp when picked up
  SND_TICK,       // single short blip (volume-change feedback)
  SND_PURR,       // low rumble loop while being petted
  SND_NOM,        // chewy blips when the charger goes in ("feeding")
  SND_BURP,       // charge-done: satisfied belch + tiny "excuse me"
  SND_DIZZY,      // drunken siren after a good shake
  SND_SIGH,       // soft falling sigh (bored / stretch reminder)
};

bool audioInit();               // I2C codec setup + I2S + player task
void audioPlay(uint8_t id);     // queue a sound (non-blocking, drops if full)
int  soundIdByName(const char* name);   // -1 if unknown ({"t":"sound"} msgs)

// Runtime volume (0..100). Persisted to NVS; 0 = muted (sounds are dropped
// and the PA never fires). Mute toggle restores the last non-zero volume.
void    audioSetVolume(int vol);
uint8_t audioVolume();
bool    audioMuted();
void    audioToggleMute();

// Temporary hush that does NOT touch the NVS volume/mute state — used while
// the pet lies face-down asleep. Queued sounds are simply dropped.
void audioSuppress(bool on);

// TTS speech: the host streams one 16 kHz mono
// s16le PCM clip over TCP ({"t":"speak","len":N} header, then base64
// {"t":"pcm","d":...} chunks ≤1024 raw bytes). The clip lands in a single
// PSRAM buffer; playback starts once SPEAK_START bytes are in (early start,
// 2026-09-05) and keeps pace with the network. Wi-Fi only; BLE hasn't the
// bandwidth. fmt 0 = s16le PCM, 1 = IMA ADPCM (4:1, low nibble first, state
// reset per clip) — `bytes` is always the DECODED PCM size.
// A 1.5 s underrun ends the clip early.
constexpr uint8_t SND_SPEAK = 100;     // internal queue id, not a chirp
bool audioSpeakBegin(uint32_t bytes, uint8_t fmt = 0);  // false = busy/oversize/audio down
void audioSpeakData(const uint8_t* data, uint32_t n);   // append a chunk (wire bytes)
struct SpeakStats { uint32_t bytes, startMs, rxMs, playMs; uint8_t fmt; bool underrun; };
bool audioSpeakTakeStats(SpeakStats& out);              // true once per finished clip

// On-board episode player (player.h). It shares this one queue
// and this one speaker: the player loop gives the speaker up the moment a
// chirp or a TTS clip is queued behind it, and audioTask hands it back
// afterwards, so nothing ever mixes. The IMA decoder is exported because the
// player carries its own state (it decodes on the audio task while a TTS clip
// may still be filling on the loop task).
constexpr uint8_t SND_PLAYER = 101;    // internal queue id, not a chirp
void audioPlayerKick();                // (re)queue the player; no-op if already queued/running
struct ImaDec { int pred, idx; };
int16_t imaDecodeStep(ImaDec& st, uint8_t nib);

// Microphone capture: ES7210 -> I2S RX, full duplex with
// playback. Frames of MIC_FRAME_MS mono 16 kHz, IMA ADPCM 4:1 (low nibble
// first, encoder state reset by audioMicStart — the same wire format as TTS
// clips, reversed). The mic task fills a PSRAM ring; the main loop drains it
// with audioMicPop and writes {"t":"mic"} lines to the TCP host.
constexpr int MIC_FRAME_BYTES = 16000 * 40 / 1000 / 2;   // 320 (MIC_FRAME_MS = 40)
struct MicFrame { uint16_t seq; int16_t peak; uint32_t ms; uint8_t d[MIC_FRAME_BYTES]; };   // ms = millis() at capture
struct MicStats { uint32_t frames, dropped; int16_t peakL, peakR; uint16_t rms; uint8_t ch; };
bool     audioMicAvailable();          // ES7210 answered on I2C and the RX channel is up
void     audioMicStart();              // start filling the ring (stale DMA data is skipped)
void     audioMicStop();
bool     audioMicActive();
void     audioMicChannel(uint8_t ch);  // 0 = left slot (ADC1), 1 = right (ADC2), 2 = average
bool     audioMicPop(MicFrame& out);   // false = ring empty
MicStats audioMicStats();              // counters since the last start; peaks of the latest frame

// 眼神追声: the mic task cross-correlates the two mics on
// every 40 ms frame, streaming or not, and keeps the latest confident
// direction. Only a number leaves the mic task — no audio goes anywhere.
struct GazeEst {
  float    az;      // -1..+1, + = toward MIC1 (the board's right edge, keys up)
  float    conf;    // normalized correlation peak of that estimate, 0..1
  float    lag;     // interpolated inter-mic delay in samples (+ = MIC1 heard it first)
  uint16_t rms;     // frame level that produced it (16-bit units)
  uint16_t floor;   // slow noise-floor estimate the gate compares against
  uint32_t ms;      // millis() of the last confident estimate
  uint32_t n;       // confident estimates since boot
};
GazeEst audioGaze();
struct GazeSample { float az; uint16_t rms; uint32_t ms; };
int     audioGazeHist(GazeSample* out, int max);    // newest first, confident frames only
void    audioGazeEnable(bool on);
bool    audioGazeEnabled();
void    audioGazeTune(int minRms, float minConf);   // negative = keep
void    audioGazeDebug(bool on);                    // serial line per confident frame (≤4/s)
bool    audioOutputActive();                        // PA on, or off < 300 ms ago (own speaker)
