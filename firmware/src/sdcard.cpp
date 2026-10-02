// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
#include "sdcard.h"
#include "pins.h"
#include <SPI.h>
#include <SD.h>
#include <SD_MMC.h>
#include "sd_diskio.h"   // raw sector access for the mount-failure autopsy
#include "diskio.h"
#include "driver/sdmmc_host.h"   // native-mode autopsy with real error codes
#include "sdmmc_cmd.h"
#include "esp_log.h"

// Two roads to the same slot: the S3's native SDMMC host in 1-bit mode over
// the GPIO matrix (CLK=SCK, CMD=MOSI, D0=MISO, DAT3=CS held high), and
// SD-SPI on SPI3 (the AMOLED QSPI owns SPI2). The first card we met (a
// no-name 32 GB, 2026-09-03) is deaf in SPI mode and fine in native mode,
// and native fails fast (~30 ms) when no card answers — so SDMMC goes first.
static SPIClass s_spi(HSPI);
static SdInfo   s_info = {};
static bool     s_spiUp = false;
static uint8_t  s_lastR1 = 0xFF;
static fs::FS*  s_fs = nullptr;      // whichever filesystem object mounted
static bool     s_mmc = false;

static void spiUp() {
  if (s_spiUp) return;
  pinMode(PIN_SD_MISO, INPUT_PULLUP);   // no card: read 0xFF, not 0x00 (fast fail)
  s_spi.begin(PIN_SD_SCK, PIN_SD_MISO, PIN_SD_MOSI, PIN_SD_CS);
  s_spiUp = true;
}
static void spiDown() {
  if (!s_spiUp) return;
  s_spi.end();
  s_spiUp = false;
}

// Bump a boot counter on the card and read it back: exercises mkdir, write,
// read in one go, and a later boot proves the card kept what we wrote.
static bool selfTest(fs::FS& f) {
  if (!f.exists("/agentpet") && !f.mkdir("/agentpet")) return false;
  uint32_t boots = 0;
  File fh = f.open("/agentpet/boot.txt", FILE_READ);
  if (fh) { boots = (uint32_t)fh.parseInt(); fh.close(); }
  boots++;
  fh = f.open("/agentpet/boot.txt", FILE_WRITE);
  if (!fh) return false;
  fh.printf("%lu\n", (unsigned long)boots);
  fh.close();
  fh = f.open("/agentpet/boot.txt", FILE_READ);
  if (!fh) return false;
  uint32_t back = (uint32_t)fh.parseInt();
  fh.close();
  s_info.boots = back;
  return back == boots;
}

static void listRoot(fs::FS& f) {
  File root = f.open("/");
  if (!root) return;
  int n = 0;
  for (File e = root.openNextFile(); e; e = root.openNextFile()) {
    if (n++ < 12)
      Serial.printf("sd:   %s%s  %lu\n", e.name(), e.isDirectory() ? "/" : "",
                    (unsigned long)e.size());
    e.close();
  }
  if (n > 12) Serial.printf("sd:   ... %d entries total\n", n);
  if (n == 0) Serial.println("sd:   (root is empty)");
  root.close();
}

static const char* const TYPE_NAMES[] = {"none", "MMC", "SD", "SDHC", "?"};

static void mounted(fs::FS& f, uint8_t type, uint64_t size, uint64_t total,
                    uint64_t used, uint32_t t0, const char* via) {
  s_fs = &f;
  s_info.mounted = true;
  s_info.mmc     = s_mmc;
  s_info.type    = type;
  s_info.sizeMB  = (uint32_t)(size >> 20);
  s_info.totalMB = (uint32_t)(total >> 20);
  s_info.usedMB  = (uint32_t)(used >> 20);
  s_info.rwOk    = selfTest(f);
  s_info.mountMs = millis() - t0;
  Serial.printf("sd: %s card %lu MB via %s, FAT %lu MB (%lu MB used), rw %s, "
                "boot #%lu, %lu ms\n",
                TYPE_NAMES[type > 4 ? 4 : type], (unsigned long)s_info.sizeMB, via,
                (unsigned long)s_info.totalMB, (unsigned long)s_info.usedMB,
                s_info.rwOk ? "ok" : "FAILED", (unsigned long)s_info.boots,
                (unsigned long)s_info.mountMs);
  listRoot(f);
}

// Raw SD-SPI bring-up at 400 kHz, bypassing the library, one command at a
// time with every reply printed: CMD0 (idle) -> CMD8 (v2 voltage check) ->
// ACMD41 loop (leave idle) -> CMD58 (OCR: SDHC?). Whatever step goes dark is
// the fault line. Returns the CMD0 R1 (0x01 = a card is there and talking).
static uint8_t rawCmd(uint8_t idx, uint32_t arg, uint8_t crc, uint32_t* r7) {
  digitalWrite(PIN_SD_CS, LOW);
  s_spi.transfer(0xFF);
  s_spi.transfer(0x40 | idx);
  s_spi.transfer(arg >> 24); s_spi.transfer(arg >> 16);
  s_spi.transfer(arg >> 8);  s_spi.transfer(arg);
  s_spi.transfer(crc);
  uint8_t r1 = 0xFF;
  for (int i = 0; i < 10 && (r1 & 0x80); i++) r1 = s_spi.transfer(0xFF);
  if (r7) {
    *r7 = 0;
    for (int i = 0; i < 4; i++) *r7 = (*r7 << 8) | s_spi.transfer(0xFF);
  }
  s_spi.transfer(0xFF);
  digitalWrite(PIN_SD_CS, HIGH);
  s_spi.transfer(0xFF);
  return r1;
}
static uint8_t probeInit() {
  pinMode(PIN_SD_CS, OUTPUT);
  digitalWrite(PIN_SD_CS, HIGH);
  s_spi.beginTransaction(SPISettings(400000, MSBFIRST, SPI_MODE0));
  for (int i = 0; i < 12; i++) s_spi.transfer(0xFF);   // >=74 clocks, CS high
  uint32_t r7 = 0, ocr = 0;
  uint8_t r0 = rawCmd(0, 0, 0x95, nullptr);
  uint8_t r8 = 0xFF, r41 = 0xFF, r58 = 0xFF;
  uint32_t ms = 0;
  if (r0 == 0x01) {
    r8 = rawCmd(8, 0x1AA, 0x87, &r7);
    uint32_t t0 = millis();
    do {
      rawCmd(55, 0, 0x01, nullptr);
      r41 = rawCmd(41, r8 == 0x01 ? 0x40000000 : 0, 0x01, nullptr);
    } while (r41 == 0x01 && (int32_t)(millis() - t0) < 1000);
    ms = millis() - t0;
    r58 = rawCmd(58, 0, 0xFD, &ocr);
  }
  s_spi.endTransaction();
  Serial.printf("sd: probe CMD0=%02X CMD8=%02X/%03lX ACMD41=%02X (%lu ms) "
                "CMD58=%02X OCR=%08lX%s\n", r0, r8, (unsigned long)(r7 & 0xFFF),
                r41, (unsigned long)ms, r58, (unsigned long)ocr,
                (r58 == 0 && (ocr & (1u << 30))) ? " SDHC/SDXC" : "");
  return r0;
}

// Last resort before blaming the slot: some cards want far more than the
// spec's 74 wake-up clocks, or a slower clock. 6 rounds of 4096 clocks at
// 100 kHz with CS high, then CMD0, 50 ms apart. Returns the first CMD0 R1
// that was not 0xFF (or 0xFF if the card never spoke).
static uint8_t deepProbe() {
  pinMode(PIN_SD_CS, OUTPUT);
  digitalWrite(PIN_SD_CS, HIGH);
  uint8_t best = 0xFF;
  for (int round = 0; round < 6 && best == 0xFF; round++) {
    s_spi.beginTransaction(SPISettings(100000, MSBFIRST, SPI_MODE0));
    for (int i = 0; i < 512; i++) s_spi.transfer(0xFF);
    best = rawCmd(0, 0, 0x95, nullptr);
    s_spi.endTransaction();
    delay(50);
  }
  Serial.printf("sd: deep probe (6x4096 clk @100k) CMD0=%02X\n", best);
  return best;
}

// The card answered CMD0 but FatFs would not mount it: read sector 0 (and
// the first partition's boot sector) and say what is actually on the card —
// exFAT / GPT / FAT32 — so the fix (reformat FAT32) is a fact, not a guess.
static uint32_t le32(const uint8_t* p) {
  return (uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}
static void autopsy() {
  uint8_t pdrv = sdcard_init(PIN_SD_CS, &s_spi, 20000000);
  if (pdrv == 0xFF) { Serial.println("sd: autopsy: no drive slot"); return; }
  if (disk_initialize(pdrv) & STA_NOINIT) {
    Serial.println("sd: autopsy: card init failed");
    sdcard_uninit(pdrv);
    return;
  }
  uint8_t ty = (uint8_t)sdcard_type(pdrv);
  uint32_t sectors = sdcard_num_sectors(pdrv);
  Serial.printf("sd: autopsy: %s card, %lu MB raw\n", TYPE_NAMES[ty > 4 ? 4 : ty],
                (unsigned long)(sectors / 2048));
  uint8_t* b = (uint8_t*)malloc(512);
  if (b && sd_read_raw(pdrv, b, 0)) {
    uint32_t partStart = 0;
    if (!memcmp(b + 3, "EXFAT   ", 8)) {
      Serial.println("sd: autopsy: sector 0 is a raw exFAT volume (no partition table)");
    } else if (b[510] == 0x55 && b[511] == 0xAA && (b[0x1C2] || b[0x1D2])) {
      for (int i = 0; i < 4; i++) {
        const uint8_t* e = b + 0x1BE + 16 * i;
        if (!e[4]) continue;
        Serial.printf("sd: autopsy: MBR part %d type 0x%02X start %lu size %lu MB\n",
                      i, e[4], (unsigned long)le32(e + 8),
                      (unsigned long)(le32(e + 12) / 2048));
        if (!partStart) partStart = le32(e + 8);
      }
      if (b[0x1C2] == 0xEE && sd_read_raw(pdrv, b, 2)) {   // GPT: entry 0
        partStart = le32(b + 32);
        Serial.printf("sd: autopsy: GPT, first partition starts LBA %lu\n",
                      (unsigned long)partStart);
      }
    } else if (b[510] == 0x55 && b[511] == 0xAA) {
      Serial.println("sd: autopsy: sector 0 looks like a boot sector (no MBR)");
    } else {
      Serial.println("sd: autopsy: sector 0 has no signature — blank / unformatted card");
    }
    if (partStart && sd_read_raw(pdrv, b, partStart)) {
      char oem[9]; memcpy(oem, b + 3, 8); oem[8] = 0;
      const char* fs = !memcmp(oem, "EXFAT", 5) ? "exFAT"
                     : !memcmp(oem, "NTFS", 4) ? "NTFS"
                     : !memcmp(b + 82, "FAT32", 5) ? "FAT32"
                     : !memcmp(b + 54, "FAT", 3) ? "FAT12/16" : "unknown";
      Serial.printf("sd: autopsy: boot sector OEM \"%s\" -> %s\n", oem, fs);
    }
  } else Serial.println("sd: autopsy: sector 0 read failed");
  free(b);
  sdcard_uninit(pdrv);
}

// SD_MMC.begin() hides the IDF error codes; redo the bring-up by hand at
// probing speed (400 kHz) and print what sdmmc_card_init actually returned
// (ESP_ERR_TIMEOUT = no card answers on CMD, crc = wiring/contact, ...).
static void quietIdf() {
  esp_log_level_set("sdmmc_common", ESP_LOG_NONE);
  esp_log_level_set("vfs_fat_sdmmc", ESP_LOG_NONE);
}
static void mmcAutopsy() {
  esp_log_level_set("sdmmc_cmd", ESP_LOG_VERBOSE);
  esp_log_level_set("sdmmc_sd",  ESP_LOG_VERBOSE);
  esp_log_level_set("sdmmc_req", ESP_LOG_VERBOSE);
  sdmmc_host_t host = SDMMC_HOST_DEFAULT();
  host.flags = SDMMC_HOST_FLAG_1BIT;
  host.max_freq_khz = SDMMC_FREQ_PROBING;
  sdmmc_slot_config_t slot = SDMMC_SLOT_CONFIG_DEFAULT();
  slot.clk = (gpio_num_t)PIN_SD_SCK;  slot.cmd = (gpio_num_t)PIN_SD_MOSI;
  slot.d0  = (gpio_num_t)PIN_SD_MISO;
  slot.d1 = slot.d2 = slot.d3 = GPIO_NUM_NC;
  slot.width = 1;
  slot.flags |= SDMMC_SLOT_FLAG_INTERNAL_PULLUP;
  esp_err_t e1 = sdmmc_host_init();
  esp_err_t e2 = sdmmc_host_init_slot(SDMMC_HOST_SLOT_1, &slot);
  sdmmc_card_t* card = (sdmmc_card_t*)calloc(1, sizeof(sdmmc_card_t));
  esp_err_t e3 = card ? sdmmc_card_init(&host, card) : ESP_ERR_NO_MEM;
  Serial.printf("sd: sdmmc autopsy: host_init=%s slot=%s card_init=%s\n",
                esp_err_to_name(e1), esp_err_to_name(e2), esp_err_to_name(e3));
  if (e3 == ESP_OK) {
    Serial.printf("sd: sdmmc autopsy: name \"%s\" %lu MB%s ocr=%08lX\n",
                  card->cid.name,
                  (unsigned long)(((uint64_t)card->csd.capacity * card->csd.sector_size) >> 20),
                  (card->ocr & (1u << 30)) ? " SDHC/SDXC" : "",
                  (unsigned long)card->ocr);
  }
  free(card);
  sdmmc_host_deinit();
  quietIdf();
}

// Native SD protocol, 1 data line, through the GPIO matrix. DAT3 (our CS
// pin) must sit high or the card would drop into SPI mode on the first CMD0.
static bool mountMmc(bool quiet) {
  spiDown();                                    // hand GPIO1/2/3 to the SDMMC host
  pinMode(PIN_SD_CS, OUTPUT);
  digitalWrite(PIN_SD_CS, HIGH);
  SD_MMC.setPins(PIN_SD_SCK, PIN_SD_MOSI, PIN_SD_MISO);   // clk, cmd, d0
  uint32_t t0 = millis();
  bool ok = SD_MMC.begin("/sd", true, false, SDMMC_FREQ_DEFAULT, 8);
  if (!ok) {
    SD_MMC.end();
    if (!quiet) {
      Serial.printf("sd: sdmmc 1-bit mount failed (%lu ms)\n",
                    (unsigned long)(millis() - t0));
      mmcAutopsy();
    }
    spiUp();                                    // back to SPI for the hot-plug watch
    return false;
  }
  s_mmc = true;
  mounted(SD_MMC, (uint8_t)SD_MMC.cardType(), SD_MMC.cardSize(),
          SD_MMC.totalBytes(), SD_MMC.usedBytes(), t0, "sdmmc 1-bit");
  return true;
}

// Drop a card that is no longer answering (pulled out, swapped) so the next
// attempt starts clean. Keeps the boot counter for the log line.
static void unmount() {
  if (!s_info.mounted) return;
  if (s_mmc) SD_MMC.end(); else SD.end();
  uint32_t boots = s_info.boots;
  s_info = SdInfo{};
  s_info.boots = boots;
  s_fs = nullptr;
  s_mmc = false;
  spiUp();
  Serial.println("sd: card gone, unmounted");
}

bool sdInit(bool deep) {
  static bool quieted = false;
  if (!quieted) { quietIdf(); quieted = true; }
  if (s_info.mounted) {                         // still the same card?
    File f = s_fs->open("/agentpet/boot.txt", FILE_READ);
    bool alive = (bool)f;
    if (f) f.close();
    if (alive) return true;
    unmount();
  }
  spiUp();
  if (mountMmc(false)) return true;             // native 1-bit: fast, and what picky cards want
  uint32_t t0 = millis();
  // SD-SPI fallback. 20 MHz through the GPIO matrix is comfortable; the
  // driver identifies the card at 400 kHz first and caps at 25 MHz anyway.
  if (SD.begin(PIN_SD_CS, s_spi, 20000000, "/sd", 8)) {
    mounted(SD, (uint8_t)SD.cardType(), SD.cardSize(), SD.totalBytes(),
            SD.usedBytes(), t0, "spi");
    return true;
  }
  uint8_t r1 = probeInit();
  s_lastR1 = r1;
  Serial.printf("sd: spi mount failed (%lu ms), CMD0 R1=0x%02X -> %s\n",
                (unsigned long)(millis() - t0), r1,
                r1 == 0x01 ? "card answers, filesystem not FAT? (exFAT/64GB+)"
                : r1 == 0xFF ? "no answer: card absent / not seated / no SPI mode"
                             : "odd reply, card in a strange state");
  if (r1 == 0x01) autopsy();   // raw sectors: what is really on the card
  if (deep && r1 == 0xFF && deepProbe() == 0x01) {   // slow waker? one more go
    t0 = millis();
    if (SD.begin(PIN_SD_CS, s_spi, 20000000, "/sd", 8)) {
      mounted(SD, (uint8_t)SD.cardType(), SD.cardSize(), SD.totalBytes(),
              SD.usedBytes(), t0, "spi (slow wake)");
      return true;
    }
  }
  return false;
}

const SdInfo& sdInfo() { return s_info; }
fs::FS* sdFs() { return s_info.mounted ? s_fs : nullptr; }

bool sdMkdirs(const char* filePath) {
  if (!s_fs) return false;
  char buf[128];
  snprintf(buf, sizeof(buf), "%s", filePath);
  for (char* p = buf + 1; *p; p++) {
    if (*p != '/') continue;
    *p = 0;
    if (!s_fs->exists(buf) && !s_fs->mkdir(buf)) return false;
    *p = '/';
  }
  return true;
}
uint8_t sdLastR1() { return s_lastR1; }

// Hot-plug watch while unmounted: a lone SPI CMD0 every 3 s (~1 ms, silent),
// and a quiet native SDMMC attempt every 10 s (~30 ms when nothing answers)
// for cards that, like our first one, never speak SPI.
bool sdPoll() {
  if (s_info.mounted || !s_spiUp) return false;
  static uint32_t lastSpi = 0, lastMmc = 0;
  uint32_t now = millis();
  if ((int32_t)(now - lastMmc) >= 10000) {
    lastMmc = now;
    if (mountMmc(true)) return true;
  }
  if ((int32_t)(now - lastSpi) < 3000) return false;
  lastSpi = now;
  pinMode(PIN_SD_CS, OUTPUT);
  digitalWrite(PIN_SD_CS, HIGH);
  s_spi.beginTransaction(SPISettings(400000, MSBFIRST, SPI_MODE0));
  for (int i = 0; i < 12; i++) s_spi.transfer(0xFF);
  uint8_t r0 = rawCmd(0, 0, 0x95, nullptr);
  s_spi.endTransaction();
  if (r0 != 0x01) return false;
  Serial.println("sd: card answered CMD0, mounting");
  return sdInit(false);
}

static void fmtMB(char* out, size_t n, uint32_t mb) {
  if (mb >= 1024) snprintf(out, n, "%lu.%luG", (unsigned long)(mb / 1024),
                           (unsigned long)((mb % 1024) * 10 / 1024));
  else            snprintf(out, n, "%luM", (unsigned long)mb);
}

void sdStatus(char* out, size_t n) {
  if (!s_info.mounted) { snprintf(out, n, "no SD"); return; }
  char sz[12], us[12];
  fmtMB(sz, sizeof(sz), s_info.sizeMB);
  fmtMB(us, sizeof(us), s_info.usedMB);
  snprintf(out, n, "SD %s%s %s used", sz, s_info.rwOk ? "" : " ro?", us);
}
