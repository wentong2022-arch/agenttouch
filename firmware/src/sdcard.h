// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
// microSD over SPI — 基A of the SD roadmap.
// Mount at boot, expose card facts for the settings card / hello, and run a
// tiny read-back self-test so "mounted" really means "FAT read+write works".
#pragma once
#include <Arduino.h>
#include <FS.h>

struct SdInfo {
  bool     mounted;
  uint8_t  type;      // sdcard_type_t: 0 none, 1 MMC, 2 SD, 3 SDHC/SDXC
  uint32_t sizeMB;    // raw card capacity
  uint32_t totalMB;   // FAT volume size
  uint32_t usedMB;
  bool     rwOk;      // /agentpet/boot.txt written and read back
  uint32_t boots;     // boot counter kept on the card (proof it persists)
  uint32_t mountMs;   // how long the mount + self-test took
  bool     mmc;       // mounted through the native SDMMC host (else SD-SPI)
};

bool sdInit(bool deep = false);       // (re)mount; verifies a mounted card is still there;
                                      // deep = also run the slow long-clock SPI probe
bool sdPoll();                        // call from loop(): hot-plug watch, true once when a card mounts
const SdInfo& sdInfo();
uint8_t sdLastR1();                   // CMD0 reply of the last failed mount (0xFF = silent)
void sdStatus(char* out, size_t n);   // "SD 29G · 3M used" / "no SD" / "SD ro?"
fs::FS* sdFs();                       // mounted filesystem, nullptr when no card
bool sdMkdirs(const char* filePath);  // create every parent directory of a file path
