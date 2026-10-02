// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
// On-board audio player (wave 2).
//
// The card holds episodes as /agentpet/audio/<name>.ima — a block container
// the host writes (16 B header "AGPTIMA1" + u32 LE rate + u32 LE block count,
// then blocks of 4 B state header + 8000 B IMA ADPCM nibbles = 1 s each) —
// plus a sidecar <name>.json with the title/show/duration/cover. Playback
// runs inside the existing audioTask (audio.cpp): one speaker, one queue, so
// a chirp or a TTS clip simply gets the speaker for as long as it needs and
// the episode picks up where it left off.
//
// Threading: everything below runs on the main loop EXCEPT the three calls
// marked "audio task" — those are the streaming side and touch the open File.
#pragma once
#include <stdint.h>

struct PlayerInfo {
  bool     hasItems;    // the card holds at least one playable episode
  bool     playing;     // the board's own speaker is sounding an episode
  char     title[96];
  char     show[64];    // podcast / album name (the play page subtitle)
  uint32_t posSec, durSec;
  char     cover[48];   // /agentpet/covers/<hash12>.jpg, "" = none
  int      index;       // 0-based position of the current episode in the list (-1 = none)
};

bool playerInit();          // scan the card + restore the resume point
void playerRescan();        // host {"t":"pl"}: /agentpet/audio/ changed
void playerToggle();
void playerNext();
void playerPrev();
void playerPause();
void playerSeek(int32_t dsec);   // ±seconds inside the current episode (block granularity, clamped)
bool playerPlaying();
const PlayerInfo& playerInfo();

// Wave 2 additions.
int  playerCount();         // episodes on the card ({"t":"plst"} n)
void playerSaveState();     // write /agentpet/audio/state.json now
void playerPoll();          // from loop(): metadata refresh, auto-advance, 10 s resume writes

// ---- audio task side -----------------------------------------------------
bool     playerWantPlay();                            // the episode should be sounding
void     playerRunSet(bool on);                       // audioTask entered/left the player loop
uint32_t playerPull(int16_t* out, uint32_t maxSamples);  // decoded mono samples, 0 = stop
