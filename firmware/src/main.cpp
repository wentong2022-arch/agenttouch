// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
// AgentTouch — desk pet showing the live status of AI coding agents.
// Board: Waveshare ESP32-S3-Touch-AMOLED-2.16.
// Talks newline-delimited JSON over TCP to the Mac host service (host/).
#include <Arduino.h>
#include <Wire.h>
#include <WiFi.h>
#include <ESPmDNS.h>
#include <Arduino_GFX_Library.h>
#include <ArduinoJson.h>
#include <XPowersLib.h>
#include <SensorQMI8658.hpp>
#include <TouchDrvCSTXXX.hpp>
#include <Preferences.h>
#include <math.h>
#include <mbedtls/base64.h>
#include <lwip/sockets.h>
#include <errno.h>
#include "pins.h"
#include "config.h"
#include "face.h"
#include "pages.h"
#include "audio.h"
#include "ble.h"
#include "sdcard.h"
#include "sdfont.h"
#include "boardrtc.h"
#include "player.h"
#include "jpegrom.h"
#include "i18n.h"
#include "esp_rom_crc.h"
#include <Update.h>
#include <esp_ota_ops.h>
#include <esp_system.h>
#include <esp_timer.h>
#include <esp_heap_caps.h>
#include <esp_task_wdt.h>

// ---------- task watchdog ----------
// Added 2026-09-19 alongside an attempt to make the middle (PWRON) key the
// hold-to-talk key, which needed the PMU's long-press hard cut off. That cut
// turned out to be EFUSE-locked and the key went back to IO18, so the hard
// cut is still there as the physical emergency stop (6 s). The TWDT stays
// anyway: a wedged loop now reboots itself instead of waiting for a hand.
// loopTask must check in every WDT_TIMEOUT_S seconds or the chip panics and
// reboots (reset reason shows up in the hello line).
//
// 15 s, not the core's default 5: loop() has a few legitimate multi-second
// stalls (SD-OTA flash write ~7.6 s is the longest) and a false reboot in
// the middle of one of those would be far worse than a late one. Every such
// path either feeds the dog itself (wdtFeed) or unsubscribes (powerOff).
//
// Arduino-ESP32 3.1 already brings the TWDT up at boot
// (CONFIG_ESP_TASK_WDT_INIT=y, 5 s, panic, CPU0 idle subscribed), so
// esp_task_wdt_init() here answers ESP_ERR_INVALID_STATE and we reconfigure
// the running one instead. Both orders are handled so the build does not
// depend on that Kconfig staying the way it is.
static const uint32_t WDT_TIMEOUT_S = 15;
static bool wdtOn = false;                 // false until setup subscribes us

static inline void wdtFeed() { if (wdtOn) esp_task_wdt_reset(); }

static void wdtBegin() {
  esp_task_wdt_config_t cfg = {};
  cfg.timeout_ms     = WDT_TIMEOUT_S * 1000;
  cfg.idle_core_mask = 1 << 0;     // as the core config had it (loopTask is core 1)
  cfg.trigger_panic  = true;
  esp_err_t e = esp_task_wdt_init(&cfg);
  if (e == ESP_ERR_INVALID_STATE) e = esp_task_wdt_reconfigure(&cfg);
  if (e != ESP_OK) { Serial.printf("wdt: config failed (%d)\n", (int)e); return; }
  if ((e = esp_task_wdt_add(nullptr)) != ESP_OK) {
    Serial.printf("wdt: subscribe failed (%d)\n", (int)e);
    return;
  }
  wdtOn = true;
  esp_task_wdt_reset();
  Serial.printf("wdt: loop watched, %lu s -> panic\n", (unsigned long)WDT_TIMEOUT_S);
}

// Leaving a path that will never come back (powerOff's park loop): drop the
// subscription instead of feeding, so the dog cannot drag us back up.
static void wdtLeave() {
  if (!wdtOn) return;
  wdtOn = false;
  esp_task_wdt_delete(nullptr);
}

// ---------- hardware ----------
static Arduino_DataBus* bus;
static Arduino_CO5300*  gfx;
static Arduino_Canvas*  canvas;
static XPowersPMU       pmu;
static SensorQMI8658    qmi;
static TouchDrvCST92xx  tp;
static bool pmuOk = false, imuOk = false, tpOk = false;

// ---------- app state ----------
static uint8_t  agentStates[N_AGENTS] = {ST_OFF};   // ST_OFF == 0: the rest zero-init
static int      selected  = 0;
static bool     hostUp    = false;
static bool     listening = false;
static uint32_t nudgeUntil = 0;   // pin the status toast until this millis()
static uint32_t volShowUntil = 0; // volume overlay visible until this millis()
static uint32_t surprisedUntil  = 0;
static uint32_t dizzyUntil   = 0; // shaken -> spiral eyes
static uint32_t petUntil     = 0; // being stroked (refreshed per stroke)
static uint32_t eatUntil     = 0; // charger plugged -> nom nom
static uint32_t burpUntil    = 0; // charge finished -> burp
static uint32_t boredUntil   = 0; // lonely sigh episode
static uint32_t stretchUntil = 0; // host-sent "take a break"
static bool     flipped      = false;  // face-down = sleep (dark + hushed)

// Growth stats (NVS ns "petgrow"): agent done events feed the pet, petting
// and days together add up to XP -> level. Double-tap = profile card;
// tapping the open card cycles the skin.
static Preferences growPrefs;
static uint32_t statDone = 0, statDays = 0, statPets = 0;
static uint32_t lastDayNum = 0;          // local epoch / 86400, 0 = never
// One pet per seat (user idea 2026-08-29): swiping agents swaps the whole
// character, so each agent is recognizable at a glance. NVS keys skin0..N-1
// (the fifth seat just adds "skin4" — stored by index, nothing to migrate).
static uint8_t skinBy[N_AGENTS] = {SKIN_CLASSIC, SKIN_ROBO, SKIN_KITTY,
                                   SKIN_SPROUT,
                                   SKIN_BUNNY};  // lead swaps to SKIN_GROK at merge
static uint32_t profUntil = 0;
static uint32_t lastTapAt = 0;
// Settings card (hold the face ~1.5 s): skin picker for the current seat,
// brightness level (NVS "bright"), read-only status line. Owns the touch
// while visible; refreshed to 12 s on each interaction.
static uint32_t setCardUntil = 0;
static const uint8_t BRIGHT_LVLS[3] = BRIGHT_LEVELS;
static uint8_t brightLvl = 1;
static uint8_t powState = 0;   // 0 = on battery, 1 = charging, 2 = USB + full
// "拿起时显示" binding (doc/06 screen-sovereignty rules): what the pet shows
// while confirmed in-hand. Host pushes {"t":"cfg","pickup":"face"} from
// config.json pickup_page; cached in NVS "pickup" so it works without the
// host too. BIND_SMART once guessed intent from agent states (working/done
// pulled the face in); doc/06 retired the guessing — needs_you is the only
// state allowed to grab the screen, so smart is now an alias of follow and
// the value survives only for config/NVS compatibility.
enum : uint8_t { BIND_FOLLOW = 0, BIND_FACE, BIND_CLOCK, BIND_REPORT,
                 BIND_ALMANAC, BIND_SMART, N_BINDS };
static uint8_t pickupBind = BIND_FOLLOW;
static bool    summonFace = false;        // shake in hand = summon the pet
// 钉住 (doc/06, 2026-09-05): the user's explicit "only listen to my hand,
// not to gravity" — the pet page is locked (desk flips, pickup and in-hand
// turns no longer change it) while still rotating upright. Outranks the
// pickup binding; needs_you may still grab the screen and face-down still
// sleeps. Toggled by the corner pin (or /test/pin), remembered in NVS "pin".
static bool     pinned = false;
// 2026-09-22 (user): the pin holds whichever placement page was on screen
// when it was set — the face, the clock strip or the almanac strip — not
// only the face. The strip's own left/right swipes stay free (the pin says
// "not gravity", never "not my finger"); NVS "pin" = 0 off, else page + 1
// (the pre-09-22 value 1 reads as the face, as before).
static uint8_t  pinnedPage = PAGE_FACE;
static uint32_t pinLitUntil = 0;          // seat-color highlight, then grey
static const uint32_t PIN_LIT_MS = 10000;
static uint32_t toastUntil = 0;           // 1.5 s pill 名牌 (钉住 / 解除钉住 / 命运自有回应)
static char     toastText[64] = "";   // 64: English 名牌 are longer
// set by the host's mac_approve toast: on the face page that pill takes the
// approve bubble's place (the bubble is what could not be honoured), instead
// of half-covering it and its hint at the default y (device shot 2026-10-04)
static bool     toastOnBubble = false;
static void showToast(const char* s, uint32_t ms = 1500) {
  strlcpy(toastText, s, sizeof(toastText));
  toastUntil = millis() + ms;
  toastOnBubble = false;
}
// Low-battery 名牌 (2026-10-04, user: the face-only corner icon was never
// seen): on battery, the first <20 % reading shows it, the first <10 % one
// again, and while it stays low it comes back every 10 min (<20 %) / 5 min
// (<10 %) — user asked for the repeat the same day. 0 = none shown yet,
// 1 = the <20 % one, 2 = the <10 % one; re-armed by the charger or by the
// reading climbing 3 points back over a threshold.
static uint8_t  lowBattWarned = 0;
static uint32_t lowBattToastAt = 0;
// debug: /test/raw?j={"t":"batt","pct":15,"s":60} fakes the gauge for the
// corner icons and the 名牌 ONLY — never the <=5 % shutdown or the sigh;
// "rep":20 shortens the repeat to 20 s while the fake lasts
static int      battFakePct = -1;
static uint32_t battFakeUntil = 0, battFakeRepMs = 0;
static bool battFakeOn() {
  if (battFakePct >= 0 && (int32_t)(battFakeUntil - millis()) <= 0) battFakePct = -1;
  return battFakePct >= 0;
}
// voice_style (host cfg "voice": chirp|tts, NVS "vtts"): with TTS on and the
// TCP link up the HOST speaks fresh needs_you/done (and falls back to pushing
// the chirp if synthesis fails) — so the board keeps quiet for those two.
static bool voiceTts = false;

static void saveSkin(int agent) {
  char key[8];
  snprintf(key, sizeof(key), "skin%d", agent);
  growPrefs.putUChar(key, skinBy[agent]);
}

// Exclusive wardrobe (user decision 2026-08-30): every seat wears a DISTINCT
// skin — matching outfits would defeat seat recognition at a glance. Wanting
// a skin another seat wears swaps the two outfits. Returns true if changed.
// `seat` may be any seat, not just the shown one (Mac settings page):
// the face only redraws when the shown seat's outfit changed. Every
// change is reported to the host (sendSkins) so the page shows who wears what.
static void sendSkins();
static bool wearSkinFor(int seat, uint8_t want) {
  want %= N_SKINS;
  if (seat < 0 || seat >= N_AGENTS) return false;
  if (want == skinBy[seat]) return false;
  for (int i = 0; i < N_AGENTS; i++) {
    if (i != seat && skinBy[i] == want) {
      skinBy[i] = skinBy[seat];
      saveSkin(i);
      break;
    }
  }
  skinBy[seat] = want;
  faceSetSkin(skinBy[selected]);   // the shown seat's outfit, whichever seat moved
  saveSkin(seat);
  sendSkins();
  return true;
}
static bool wearSkin(uint8_t want) { return wearSkinFor(selected, want); }
static bool almanacView = true;   // almanac is this orientation's HOME page
                                  // (user promotion 2026-08-29); left-swipe
                                  // shows the calendar, right-swipe returns
// Clock orientation is a three-page strip with the clock in the middle:
// 播客 ←右滑← 钟表 →左滑→ 当前播放, no wrap-around.
enum ClockView : uint8_t { CV_POD = 0, CV_CLOCK, CV_NP };   // 播客 ←右滑← 钟表 →左滑→ 当前播放
static uint8_t clockView = CV_CLOCK;
// Which source the play page is showing, and the double-tap override that
// beats the automatic answer until that answer itself changes.
// Debug view lock (host {"t":"view"}), so /test/shot can photograph a page
// the board is not physically turned to. Zero = off.
static uint8_t  viewPg = PAGE_FACE, viewSub = 0;
static uint32_t viewUntil = 0;
static uint8_t  viewSavedCv = CV_CLOCK;   // flip-side views to put back afterwards
static bool     viewSavedAlm = true;

// Voice remote: after hold-to-talk ends a "↵ 发送" bubble arms for 6 s —
// tap = host presses Return. (Shake-to-undo existed briefly and was retired
// same day: the gesture felt bad in acceptance.)
static uint32_t sendUntil = 0;
// 桌面页说话落 Mac 光标: hold-to-talk started on a page that
// is NOT the pet's face — clock, either play page, almanac, calendar — so the
// dictation is meant for whatever the human is looking at on the Mac, not for
// the seat the pet happens to sit on. Latched at PRESS and reused by the
// "↵ 发送" bubble: one utterance is one destination, so the Return that ends
// it must land where the words did, even though the bubble is tapped later
// (on the face page) and the seat may have moved meanwhile.
static bool voiceToFront = false;
static char voiceSid[9] = "";          // the session that utterance went to
static uint32_t demoStart       = 0;   // != 0 while the 8-face demo runs
static int8_t   demoFace        = -1;  // -1 = full tour, else one fixed face
static uint32_t lastRx = 0, lastPing = 0, lastStateReq = 0;

// Orientation pages + idle dimming
static uint8_t  orient = 0;                    // IMU sector, 90° steps
static const uint8_t PAGE_FOR_ORIENT[4] = PAGES_BY_ORIENT;
// What the render loop actually drew last frame (page + the sector it was
// rotated for). The touch layer keys gesture semantics off THIS, not off
// PAGE_FOR_ORIENT[orient]: with the doc/06 needs_you screen-grab and the
// in-hand bindings, the displayed page can differ from the placement page —
// a tap must approve on the face that is really on screen.
static uint8_t  shownPage = PAGE_FACE;
static uint8_t  shownRot  = 0;
static uint32_t lastActive = 0;
static uint8_t  curBright  = BRIGHT_FULL;
static void noteActivity() { lastActive = millis(); }

// 钉住 on/off, from the corner pin or the host (/test/pin). Persisted right
// away so a power cycle keeps the lock.
static void setPinned(bool on, uint8_t page = PAGE_FACE) {
  pinned = on;
  if (on) pinnedPage = page;
  pinLitUntil = on ? millis() + PIN_LIT_MS : 0;
  showToast(tr(on ? S_PINNED : S_UNPINNED));
  growPrefs.putUChar("pin", on ? (uint8_t)(page + 1) : 0);
  audioPlay(on ? SND_SELECT : SND_TICK);
  noteActivity();
  Serial.printf("pin: %s page %u\n", on ? "on" : "off", on ? page : 0);
}

static WiFiClient sock;

// ---------- helpers ----------
static int agentIdxById(const char* id) {
  for (int i = 0; i < N_AGENTS; i++)
    if (strcmp(AGENTS[i].id, id) == 0) return i;
  return -1;
}

// ---- 遥测 -------------------------------------------
// A board reboot used to be silent: the host only saw the link drop and come
// back, so "the Mac's Bluetooth hiccuped" and "the board panicked" read the
// same in the log. Uptime + reset reason ride on the hello, the heap numbers
// on the keepalive ping, and a week-long soak answers both questions from
// the log alone. Pure function so the wire vocabulary lives in one place;
// anything the IDF adds later lands in "other" rather than leaking a number.
static const char* resetReasonName(esp_reset_reason_t r) {
  switch (r) {
    case ESP_RST_POWERON:   return "poweron";
    case ESP_RST_SW:        return "sw";
    case ESP_RST_PANIC:     return "panic";
    case ESP_RST_INT_WDT:                      // all three watchdogs read
    case ESP_RST_TASK_WDT:                     // the same to the host
    case ESP_RST_WDT:       return "wdt";
    case ESP_RST_BROWNOUT:  return "brownout";
    case ESP_RST_DEEPSLEEP: return "deepsleep";
    default:                return "other";     // UNKNOWN/EXT/SDIO/USB/JTAG...
  }
}
// Seconds since boot. esp_timer is 64-bit, so a soak past millis()'s 49-day
// wrap still reports a growing number.
static uint32_t uptimeSec() {
  return (uint32_t)(esp_timer_get_time() / 1000000LL);
}

// ---- 隐藏 = 当前 off 就不画, S3 版 --------------------
// This board is one-pet-per-seat full-screen switching, not a card list, so
// the rule lands on the switching sequence: swiping / cycling skips a seat
// that is off RIGHT NOW (not "never seen running" — that was sticky and kept
// a quit app in the way). Only this instant counts, so a seat that comes
// back is reachable again at its own fixed index; it is a filter, never a
// reorder. Host-pushed selects ignore all of this (reverse follow must land
// where the Mac is) and so does the needs_you screen grab.
// 设置项「显示离线席位」: host cfg {"show_off":0|1}, NVS "showoff",
// default hidden. Turning it on switches every rule in this block off at once
// — no skipping, no auto re-select, five dots always — which is exactly what
// the setting promises ("永远五张"). One flag, one place, so there is no state
// where half the rules apply.
static bool showOffSeats = false;
static bool anySeatOn() {
  for (int i = 0; i < N_AGENTS; i++)
    if (agentStates[i] != ST_OFF) return true;
  return false;
}
// step +1 = next index, -1 = previous. With every seat off nothing is
// skipped: all five stay reachable and the picture does not collapse (the
// host being down puts us here too). With exactly one seat on it returns
// that seat — including when it IS the current one, so a swipe becomes a
// no-op instead of landing on a dead app.
static int nextSeat(int from, int step) {
  int adv = step > 0 ? 1 : N_AGENTS - 1;
  int n = (from + adv) % N_AGENTS;
  if (showOffSeats || !anySeatOn()) return n;
  for (int k = 0; k < N_AGENTS; k++) {
    if (agentStates[n] != ST_OFF) return n;
    n = (n + adv) % N_AGENTS;
  }
  return n;                                    // unreachable: anySeatOn() held
}

// ---- the one door to the TCP socket ---------------------------------------
// Newline-delimited JSON on one byte stream has a single framing rule: every
// write ends in '\n' and a started line is always finished. Three writers
// (keepalive ping, mic batches, screenshot chunks) used to frame for
// themselves; a hand-counted length in one of them (2026-09-05 21:00) lost a
// '\n' and glued pings onto the screenshot header. Nothing else touches sock:
//   tcpWriteLine(line, wait, park)        one JSON line, '\n' appended HERE
//   tcpWriteLines(buf, len, wait, park)   pre-framed batch, must end in '\n'
// Never blocks the loop (WiFiClient::print retried for up to ~10 s and froze
// the face, 2026-09-05 20:18): MSG_DONTWAIT plus a bounded wait; when lwIP
// still has no room the remainder is parked in txPend and completed by
// tcpPump() on later loop passes, so the line ends exactly where it should.
// Only a line that has not started may be dropped (park=false, counted).
static char*    txPend = nullptr;            // PSRAM, TX_PEND_SZ
static size_t   txPendLen = 0, txPendOff = 0;
static uint32_t tcpDropped = 0;
static const size_t TX_PEND_SZ = 8192;       // >= largest single write (shot batch 4 x ~1990 B)

static bool tcpPump() {                      // true while a remainder is still parked
  if (!txPendLen) return false;
  int fd = sock.fd();
  if (fd < 0) { txPendLen = txPendOff = 0; return false; }
  while (txPendOff < txPendLen) {
    int n = send(fd, txPend + txPendOff, txPendLen - txPendOff, MSG_DONTWAIT);
    if (n > 0) { txPendOff += n; continue; }
    if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) return true;
    txPendLen = txPendOff = 0;               // socket broke; netPoll notices
    return false;
  }
  txPendLen = txPendOff = 0;
  return false;
}

static bool tcpWriteLines(const char* buf, size_t len, uint32_t maxWaitMs, bool park) {
  int fd = sock.fd();
  if (fd < 0 || !len) return false;
  if (buf[len - 1] != '\n') {                // the one rule, enforced at the door
    Serial.println("tcp: unframed write refused");
    tcpDropped++; return false;
  }
  if (!txPend) txPend = (char*)heap_caps_malloc(TX_PEND_SZ, MALLOC_CAP_SPIRAM);
  uint32_t t0 = millis();
  while (tcpPump()) {                        // finish the parked remainder first, in order
    if ((int32_t)(millis() - t0) > (int32_t)maxWaitMs && !park) { tcpDropped++; return false; }
    delay(5);                                // a parking writer waits: framing before latency
  }
  size_t off = 0;
  while (off < len) {
    int n = send(fd, buf + off, len - off, MSG_DONTWAIT);
    if (n > 0) { off += n; continue; }
    if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
      if ((int32_t)(millis() - t0) <= (int32_t)maxWaitMs) { delay(5); continue; }
      size_t rem = len - off;
      if (off == 0 && !park) { tcpDropped++; return false; }            // nothing started: skip it
      if (!txPend || rem > TX_PEND_SZ) { tcpDropped++; return false; }  // cannot happen for our sizes
      memcpy(txPend, buf + off, rem); txPendLen = rem; txPendOff = 0;
      return true;                           // tcpPump completes it
    }
    tcpDropped++; return false;              // socket error; the link code notices
  }
  return true;
}

static bool tcpWriteLine(const String& line, uint32_t maxWaitMs = 300, bool park = true) {
  String out = line; out += '\n';            // the '\n' lives here and nowhere else
  return tcpWriteLines(out.c_str(), out.length(), maxWaitMs, park);
}

static bool rxFromSerial = false;   // true while handleLine() runs a USB-CDC line

static void sendJson(const JsonDocument& doc) {
  String out;
  serializeJson(doc, out);
  if (rxFromSerial) {            // a reply to a USB-CDC line goes back the way it came
    Serial.println(out);
    return;
  }
  if (bleConnected()) {
    bleSendLine(out);            // BLE preferred when the bridge is attached
  } else if (sock.connected()) {
    tcpWriteLine(out, 300);      // events matter: bounded wait, then parked, never 10 s
  }
}

// Who wears what: {"claude":"classic",...}. Rides on both hellos and is sent
// again after every change (card, host message), see wearSkinFor.
static void addSkins(JsonDocument& d) {
  JsonObject o = d["skins"].to<JsonObject>();
  for (int i = 0; i < N_AGENTS; i++) o[AGENTS[i].id] = faceSkinName(skinBy[i]);
}
static void sendSkins() {
  JsonDocument d;
  d["t"] = "skins";
  addSkins(d);
  sendJson(d);
}

static void sendEvent(const char* type, const char* key = nullptr,
                      const char* val = nullptr) {
  JsonDocument d;
  d["t"] = type;
  if (key) d[key] = val;
  sendJson(d);
}

// Host wire names of AgentState, shared by {"t":"state"} and {"t":"sess"}.
static uint8_t stateByName(const char* s) {
  if (!strcmp(s, "idle"))      return ST_IDLE;
  if (!strcmp(s, "working"))   return ST_WORKING;
  if (!strcmp(s, "needs_you")) return ST_NEEDS_YOU;
  if (!strcmp(s, "done"))      return ST_DONE;
  return ST_OFF;
}

// ---- Claude 多会话 ----
// Several Claude Code terminals at once: the host pushes the seat's session
// list ({"t":"sess","a":"claude","cur":id8,"list":[{id,ti,dir,st}…]}, start
// order, ≤ 8) and the face shows ONE of them — "what you see on the board is
// what you act on". A top row names it (title + 「n/N」, face.cpp) and the
// top band's halves step through the list; approve / reject / dictation /
// the send bubble's Return carry its id ("sid") so the host lands them in
// that terminal tab. Fewer than two sessions = no table = today's board to
// the pixel. One table, owned by the seat it came for (only Claude sends one
// today; the wire is per seat so Codex / Qoder can reuse it later). Forgotten
// when the host link drops or the owner Mac changes: the host re-sends it.
static const int SESS_MAX = 8;
struct SessItem {
  char    id[9];              // session_id, first 8 chars
  char    ti[65];             // Claude's ai-title, ≤ 64 bytes (may be "")
  char    dir[33];            // folder name, host already made it unique
  uint8_t st;                 // AgentState of THIS session
};
static SessItem sessList[SESS_MAX];
static char     sessLabel[SESS_MAX][72];   // what the row prints, fitted to 300 px
static bool     sessLabelCard = false;     // tiny18.afn was ready when labelled
static uint8_t  sessN = 0, sessCur = 0;
static int8_t   sessSeat = -1;             // AGENTS index the table belongs to
static int8_t   sessDir = 1;               // roll direction of the latest change
static uint16_t sessGen = 0;               // bumps when the current one / the count moves
static int8_t   sessLit = 0;               // chevron lit after a tap: -1 ‹ / +1 ›
static uint32_t sessLitUntil = 0;
static uint32_t sessTapAt = 0;             // last top-band tap (local-wins window)
static bool     sessTapped = false;        // sessTapAt is meaningful

static bool sessActive() { return sessN >= 2 && sessSeat == selected; }
// The selected seat's state as the face sees it: the current session's while
// the row exists, else the seat's merged state. The needs_you screen grab,
// the seat auto-switch and the needs / done chirps stay on agentStates[]
// (host-merged, "any session waits") on purpose.
static uint8_t sessEffState() {
  return sessActive() ? sessList[sessCur].st : agentStates[selected];
}
// counter turns green: the current session waits AND another one does too
static bool sessQueued() {
  if (!sessActive() || sessList[sessCur].st != ST_NEEDS_YOU) return false;
  for (int i = 0; i < sessN; i++)
    if (i != sessCur && sessList[i].st == ST_NEEDS_YOU) return true;
  return false;
}
static int sessFind(const char* id) {
  if (!id || !*id) return -1;
  for (int i = 0; i < sessN; i++)
    if (!strcmp(sessList[i].id, id)) return i;
  return -1;
}
static void sessClear() {
  sessN = 0; sessCur = 0; sessSeat = -1;
  sessTapped = false;
}

// Copy at most n-1 bytes without splitting a UTF-8 sequence.
static void sessCopy(char* dst, size_t n, const char* src) {
  size_t len = strlen(src);
  if (len >= n) {
    len = n - 1;
    while (len && ((uint8_t)src[len] & 0xC0) == 0x80) len--;
  }
  memcpy(dst, src, len);
  dst[len] = 0;
}

// What the row calls a session: its title minus a leading
// 「✳」/「*」 (the Warp tab spinner) and spaces; no title -> the folder. Without
// the card's tiny18.afn always the folder — a title would be tofu boxes —
// and a title tiny18 cannot draw (an emoji, a glyph missing from the card)
// falls back the same way. Fitted once here, not per frame: the first lookup
// of a card glyph is a microSD read.
static void sessRelabel() {
  sessLabelCard = sdFontReady(SDF_TINY);
  for (int i = 0; i < sessN; i++) {
    const char* ti = sessList[i].ti;
    for (;;) {
      if (*ti == ' ' || *ti == '*') { ti++; continue; }
      if (!strncmp(ti, "\xE2\x9C\xB3", 3)) { ti += 3; continue; }   // ✳ U+2733
      break;
    }
    const char* dir = sessList[i].dir;
    const char* s = ti;
    if (!*ti || !sessLabelCard || !almanacHasAllSmall(ti)) s = *dir ? dir : ti;
    if (!*s) s = sessList[i].id;
    almanacFitSmallTrack(sessLabel[i], sizeof(sessLabel[i]), s, 300, 1);
  }
}

// host {"t":"sess"}: the whole list every time (~200 ms coalesced on its side)
static void sessHandle(JsonDocument& d) {
  int seat = agentIdxById(d["a"] | "");
  if (seat < 0) return;
  JsonArrayConst arr = d["list"].as<JsonArrayConst>();
  int n = (int)arr.size();
  if (n > SESS_MAX) n = SESS_MAX;
  if (n < 2) {                         // 0 / 1 session: no row, today's board
    if (sessSeat == seat) sessClear();
    return;
  }
  // the current one before this message, to tell what moved
  char oldId[9] = "";
  bool had = sessN >= 2 && sessSeat == seat;
  int oldN = had ? sessN : 0, oldIdx = had ? sessCur : 0;
  if (had) strlcpy(oldId, sessList[sessCur].id, sizeof(oldId));
  int i = 0;
  for (JsonObjectConst o : arr) {
    if (i >= n) break;
    SessItem& it = sessList[i++];
    sessCopy(it.id, sizeof(it.id), o["id"] | "");
    sessCopy(it.ti, sizeof(it.ti), o["ti"] | "");
    sessCopy(it.dir, sizeof(it.dir), o["dir"] | "");
    it.st = stateByName(o["st"] | "off");
  }
  sessN = (uint8_t)n;
  sessSeat = (int8_t)seat;
  // A tap wins over a push that crossed it on the wire: for 600 ms after
  // the last top-band tap a different `cur` from the host is the host not
  // having seen the tap yet (it answers our sess_sel), so keep ours — as
  // long as ours is still in the list. After that the host decides.
  int idx = -1;
  if (had && sessTapped && (int32_t)(millis() - sessTapAt) < 600) idx = sessFind(oldId);
  if (idx < 0) idx = sessFind(d["cur"] | "");
  if (idx < 0) idx = sessFind(oldId);   // host named none of its own: stay put
  if (idx < 0) idx = 0;
  sessCur = (uint8_t)idx;
  bool curMoved = strcmp(oldId, sessList[idx].id) != 0;
  if (curMoved || n != oldN) {
    // host-driven moves roll by index (higher = up, lower = down), measured
    // from where the old current sits in the NEW list; a count-only change
    // (a session started / ended elsewhere) rolls up, as on the canvas
    int from = sessFind(oldId);
    if (from < 0) from = oldIdx;
    sessDir = (curMoved && idx < from) ? -1 : 1;
    sessGen++;                         // face.cpp: the row gets its 3 s again
  }
  sessRelabel();
}

// Top band tap (dir -1 = left half = previous, +1 = right half = next, wraps):
// the board switches at once and tells the host, which follows on the Mac
// after the hand has been still for 1.5 s.
static void sessStep(int dir) {
  if (!sessActive()) return;
  sessCur = (uint8_t)((sessCur + (dir < 0 ? sessN - 1 : 1)) % sessN);
  sessDir = dir < 0 ? -1 : 1;
  sessLit = sessDir;
  sessLitUntil = millis() + 300;
  sessTapAt = millis();
  sessTapped = true;
  sessGen++;
  JsonDocument d;
  d["t"] = "sess_sel"; d["a"] = AGENTS[sessSeat].id; d["id"] = sessList[sessCur].id;
  sendJson(d);
  audioPlay(SND_TICK);
}

// 隐藏 = 当前 off 就不画: the seat we are sitting on
// went off while others are still up, so move to the first non-off seat in
// fixed index order — otherwise the pet stares at a quit app that swiping
// can no longer reach. Silent, and src:"auto" tells the host this was
// nobody's intent: retarget dictation, but no `open -a` resurrecting the app
// it just quit and no board-just-acted lockout on reverse follow.
// Self-limiting: afterwards the seat is non-off, so a repeat call does
// nothing. All seats off = nothing to move to, we stay. Called from the
// state message and from the cfg that turns 「显示离线席位」 back off (the
// selected seat may have gone off while the setting was hiding this rule).
static void autoReselectIfOff() {
  if (showOffSeats) return;
  if (agentStates[selected] != ST_OFF || !anySeatOn()) return;
  for (int i = 0; i < N_AGENTS; i++) {
    if (agentStates[i] == ST_OFF) continue;
    selected = i;
    JsonDocument r;
    r["t"] = "select"; r["agent"] = AGENTS[i].id; r["src"] = "auto";
    sendJson(r);
    break;
  }
}

// ---- 播放页 ×2: the clock orientation's flip sides -------
// 当前播放 (left swipe) acts on the Mac through the host, 播客 (right swipe)
// on the board's own player. Nothing is inferred: the page IS the source.
static uint8_t pageSrc() {
  return clockView == CV_NP ? PSRC_MAC : clockView == CV_POD ? PSRC_SD : PSRC_NONE;
}
// zone -1 = left (prev / -15 s), 0 = middle or the card (play-pause), +1 = right
static void playAct(uint8_t src, int zone) {
  if (src == PSRC_MAC) {
    sendEvent("media", "cmd", zone < 0 ? "prev" : zone > 0 ? "next" : "toggle");
  } else if (src == PSRC_SD) {
    if (zone < 0)      playerSeek(-15);
    else if (zone > 0) playerSeek(+15);
    else               playerToggle();
  }
}
// Native touch point -> the page frame the clock-orientation pages were
// drawn in (canvas rotation (4-shownRot)&3; same table as vdx/vdy above,
// device-validated for the two-sided pages).
static void pageXY(int x, int y, int& px, int& py) {
  switch ((4 - shownRot) & 3) {
    // Canvas rotations 1 and 3 are exactly 180° apart, so this row is the
    // point reflection of the device-validated case 3 below. The clock
    // strip moved here on 2026-09-09 (PAGES_BY_ORIENT); the old `py = x`
    // had never run — the play page lived at rotation 3 until then.
    case 1:  px = 479 - y; py = 479 - x; break;
    case 2:  px = 479 - x; py = 479 - y; break;
    case 3:  px = y;       py = x;       break;   // observed on device 2026-09-09 morning (user-validated zones)
    default: px = x;       py = y;       break;
  }
}

// ---- microphone streaming (mic-blackhole branch) ----
// The mic task (audio.cpp) fills a ring of IMA ADPCM frames; they leave as
// {"t":"mic","seq","p","d"} lines (25/s × ~470 B) on one of two links, picked
// once per session: the TCP socket when it is up (main loop drains the ring,
// batches of 4, non-blocking send), else BLE (a sender task on core 0 pushes
// each line as MTU-sized notifies — measured 20 KB/s at the default 30 ms
// interval, 33 KB/s at the 15 ms we request for the session).
// Capture starts on the right-key press when the host enabled it (cfg mic:1 =
// host voice_source "board") or on a host {"t":"mic","on":1} test, and
// {"t":"micstat"} closes each session. {"t":"mic","link":"auto|tcp|ble"}
// forces the pick for tests (BLE with Wi-Fi still up).
static bool     micOnTalk   = false;   // host cfg: right key also streams the board mic
static bool     micWasOn    = false;   // a session ran and its stat is still owed
enum MicLink : uint8_t { MICLINK_NONE = 0, MICLINK_TCP = 1, MICLINK_BLE = 2 };
static MicLink  micLink      = MICLINK_NONE;   // this session's link
static MicLink  micLinkForce = MICLINK_NONE;   // host override (NONE = auto)
// Mic by placement (2026-09-09): each side edge carries one mic hole —
// right edge = MIC1 = ES7210 ADC1 = slot 0, left edge = MIC2 = ADC2 = slot 1
// (doc/01 last section). Sector 1 rests on the right edge (the almanac
// since the swap), so take the left mic there; everywhere else MIC1 is
// clear (face page: both up; in hand nothing is covered). Desk `orient`,
// not live gravity, on purpose. A host slot pick ({"t":"mic","ch":0..2},
// /test/mic/ch) wins until ch 3 hands it back to auto.
static bool     micChManual  = false;
static inline uint8_t micAutoCh() { return orient == 1 ? 1 : 0; }

// 眼神追声: the mic task's direction estimate (audio.cpp)
// drives an eye offset on the face page. NVS "gaze" = on/off; a host
// {"t":"gaze","az":x,"ms":n} forces a look for screenshots.
static bool     gazeOn         = true;
static float    gazeForceAz    = 0;
static uint32_t gazeForceUntil = 0;
static float    lookAz         = 0;    // smoothed, board axis (+ = MIC1 side)
static volatile bool micBleTaskUp = false;     // sender task alive (owns the ring while set)
static volatile uint32_t micLinesTx  = 0;
static volatile uint32_t micTxErr    = 0;      // batches lost to a broken socket / lines BLE gave up on
static volatile uint32_t micStale    = 0;      // frames older than MIC_STALE_MS thrown away (link stalled)
static uint32_t micLoopLast = 0, micLoopMax = 0, micLoopSum = 0, micLoopN = 0;   // drain interval = ring dwell
static const int MIC_LINE   = 80 + (MIC_FRAME_BYTES + 2) / 3 * 4 + 8;   // header + base64 (428) + tail
static const int MIC_BUFSZ  = MIC_LINE * 4;                             // four lines per TCP write
static const int32_t MIC_STALE_MS = 1500;                               // older than this = not worth sending
static char*    micBuf      = nullptr;

// BLE sender: one line per frame, whole line under the tx lock so the loop's
// own JSON (pings, events) can't splice into it. ENOMEM = the radio hasn't
// drained the mbuf pool yet; wait 2 ms and retry, give a line up after 300 ms
// (link dead or hopeless — the frame is stale by then anyway). Exits once the
// session ended and the ring is empty; micDrain then reports micstat.
static void micBleTask(void*) {
  char* line = (char*)heap_caps_malloc(MIC_LINE, MALLOC_CAP_SPIRAM);
  size_t chunk = (size_t)max(20, bleLinkInfo().mtu - 3);
  MicFrame f;
  while (line) {
    if (!audioMicPop(f)) {
      if (!audioMicActive()) break;                // session over, ring drained
      delay(5); continue;
    }
    if ((int32_t)(millis() - f.ms) > MIC_STALE_MS) { micStale++; continue; }
    if (!bleConnected()) { micTxErr++; continue; }
    int used = snprintf(line, 80, "{\"t\":\"mic\",\"seq\":%u,\"p\":%d,\"d\":\"", f.seq, f.peak);
    size_t olen = 0;
    mbedtls_base64_encode((unsigned char*)line + used, MIC_LINE - used - 4, &olen, f.d, MIC_FRAME_BYTES);
    used += olen;
    memcpy(line + used, "\"}\n", 3); used += 3;
    size_t off = 0; uint32_t t0 = millis();
    bleTxLock();
    while (off < (size_t)used) {
      off += bleSendRaw((const uint8_t*)line + off, used - off, chunk);
      if (off < (size_t)used) {
        if ((int32_t)(millis() - t0) > 300 || !bleConnected()) { micTxErr++; break; }
        delay(2);
      }
    }
    bleTxUnlock();
    if (off == (size_t)used) micLinesTx++;
  }
  free(line);
  micBleTaskUp = false;
  vTaskDelete(nullptr);
}

static void micSessionStart() {
  if (audioMicActive()) return;
  // Default order flipped to BLE first (2026-09-05 evening, user decision):
  // measured faster (sound→host 275 ms vs 440 over TCP), no router in the
  // path, same route at the desk and outdoors. TCP remains the fallback.
  MicLink pick = micLinkForce;
  if (pick == MICLINK_NONE) pick = bleConnected() ? MICLINK_BLE : (sock.connected() ? MICLINK_TCP : MICLINK_NONE);
  if (pick == MICLINK_BLE && !bleConnected()) pick = sock.connected() ? MICLINK_TCP : MICLINK_NONE;
  if (pick == MICLINK_TCP && !sock.connected()) pick = bleConnected() ? MICLINK_BLE : MICLINK_NONE;
  micLink = pick;
  if (!micChManual) audioMicChannel(micAutoCh());
  audioMicStart();
  if (pick == MICLINK_BLE && !micBleTaskUp) {
    bleRequestInterval(12);                        // 15 ms for the session (macOS honours it)
    micBleTaskUp = true;
    xTaskCreatePinnedToCore(micBleTask, "micble", 6144, nullptr, 1, nullptr, 0);
  }
  Serial.printf("mic: session on %s%s\n", pick == MICLINK_BLE ? "ble" : pick == MICLINK_TCP ? "tcp" : "NO LINK",
                micLinkForce ? " (forced)" : "");
}

static void micDrain() {
  if (!audioMicActive() && !micWasOn) return;
  if (!micBuf) micBuf = (char*)heap_caps_malloc(MIC_BUFSZ, MALLOC_CAP_SPIRAM);
  uint32_t now = millis();
  if (micLoopLast) {
    uint32_t dt = now - micLoopLast;
    if (dt > micLoopMax) micLoopMax = dt;
    micLoopSum += dt; micLoopN++;
  }
  micLoopLast = now;
  bool tcp = micLink == MICLINK_TCP && sock.connected() && micBuf;
  if (tcp && tcpPump()) {                          // a remainder is still parked: leave the ring alone
    if (audioMicActive()) micWasOn = true;
    return;
  }
  MicFrame f;
  int used = 0, batched = 0;
  while (!micBleTaskUp && audioMicPop(f)) {        // BLE sessions: the sender task owns the ring
    if (!tcp) continue;                            // no host: just empty the ring
    if ((int32_t)(now - f.ms) > MIC_STALE_MS) { micStale++; continue; }   // link stalled: stay live
    used += snprintf(micBuf + used, 80, "{\"t\":\"mic\",\"seq\":%u,\"p\":%d,\"d\":\"", f.seq, f.peak);
    size_t olen = 0;
    mbedtls_base64_encode((unsigned char*)micBuf + used, MIC_BUFSZ - used - 4, &olen, f.d, MIC_FRAME_BYTES);
    used += olen;
    memcpy(micBuf + used, "\"}\n", 3); used += 3;
    micLinesTx++;
    if (++batched == 4) break;                     // one batch per loop; the rest waits a frame
  }
  if (used) tcpWriteLines(micBuf, used, 0, true);   // no room = parked whole, continued by tcpPump
  if (audioMicActive()) { micWasOn = true; return; }
  if (micWasOn && !txPendLen && !micBleTaskUp) {   // session over, ring / batch / BLE task drained
    micWasOn = false;
    if (micLink == MICLINK_BLE) bleRequestInterval(24);   // back to macOS' 30 ms default
    MicStats st = audioMicStats();
    JsonDocument d;
    d["t"] = "micstat"; d["frames"] = st.frames; d["dropped"] = st.dropped;
    d["link"] = micLink == MICLINK_BLE ? "ble" : micLink == MICLINK_TCP ? "tcp" : "none";
    d["lines"] = micLinesTx; d["stale"] = micStale; d["txerr"] = micTxErr; d["tcp_dropped"] = tcpDropped;
    d["peakL"] = st.peakL; d["peakR"] = st.peakR;
    d["rms"] = st.rms; d["ch"] = st.ch; d["rssi"] = WiFi.RSSI();
    d["loop_avg_ms"] = micLoopN ? micLoopSum / micLoopN : 0; d["loop_max_ms"] = micLoopMax;
    sendJson(d);
    Serial.printf("mic: %lu frames, %lu dropped, %lu stale, %lu lines, rssi %d, loop avg %lu max %lu\n",
                  (unsigned long)st.frames, (unsigned long)st.dropped, (unsigned long)micStale,
                  (unsigned long)micLinesTx, WiFi.RSSI(),
                  (unsigned long)(micLoopN ? micLoopSum / micLoopN : 0), (unsigned long)micLoopMax);
    micLinesTx = micStale = micTxErr = 0;
    micLoopLast = micLoopMax = micLoopSum = micLoopN = 0;
  }
}
// ---- BLE bandwidth probe ({"t":"bletest"}) ----------------
// Question to settle before choosing a codec for out-of-Wi-Fi dictation: how
// many bytes/s does the NUS notify pipe really carry to the Mac? A separate
// task (core 0, beside the NimBLE host) floods fixed-length JSON lines
// {"t":"bt","s":000123,"d":"xxx…"} for `sec` seconds; the host counts what
// arrives (/test/ble/bw). Params: len = line bytes incl. '\n' (470 ≈ one
// 40 ms ADPCM frame line), chunk = notify size (0 = MTU-3), gap = ms pause
// after each line (0 = flood, the stack's ENOMEM is the only throttle),
// itvl = connection interval to request first (1.25 ms units, 0 = leave).
struct BleTest {
  volatile bool run = false, done = false;
  int sec = 3, len = 470, chunk = 0, gap = 0;
  uint32_t lines = 0, bytes = 0, retries = 0, ms = 0, waitMs = 0;
  uint32_t loopLast = 0, loopMax = 0;          // main-loop dwell while the task floods
};
static BleTest bt;

static void bleTestTask(void*) {
  size_t len = (size_t)max(40, min(bt.len, 2000));
  size_t chunk = bt.chunk > 0 ? (size_t)bt.chunk : (size_t)max(20, bleLinkInfo().mtu - 3);
  uint8_t* line = (uint8_t*)malloc(len + 1);
  if (!line) { bt.run = false; bt.done = true; vTaskDelete(nullptr); return; }
  memset(line, 'x', len);
  line[len - 2] = '"'; line[len - 1] = '}'; line[len] = '\n';   // {"t":"bt","s":NNNNNN,"d":"xxx…"}\n
  uint32_t t0 = millis(), seq = 0;
  while (bt.run && bleConnected() && (int32_t)(millis() - t0) < bt.sec * 1000) {
    int n = snprintf((char*)line, len, "{\"t\":\"bt\",\"s\":%6lu,\"d\":\"", (unsigned long)seq);   // %6lu: leading zeros are not JSON
    line[n] = 'x';                                  // snprintf's NUL back to filler
    size_t off = 0;
    bleTxLock();                                    // whole line or nothing between other senders
    while (off < len + 1 && bt.run && bleConnected()) {
      size_t got = bleSendRaw(line + off, len + 1 - off, chunk);
      off += got;
      if (off < len + 1) {                          // mbufs full: let the radio drain
        bt.retries++;
        uint32_t w0 = millis(); delay(2); bt.waitMs += millis() - w0;
      }
    }
    bleTxUnlock();
    if (off < len + 1) break;                       // link went away mid-line
    seq++; bt.lines++; bt.bytes += len + 1;
    if (bt.gap > 0) delay(bt.gap);
  }
  bt.ms = millis() - t0;
  free(line);
  bt.run = false; bt.done = true;
  vTaskDelete(nullptr);
}

static void bleTestStart(int sec, int len, int chunk, int gap, int itvl) {
  if (bt.run) return;
  bt = BleTest();
  bt.sec = max(1, min(sec, 30)); bt.len = len; bt.chunk = chunk; bt.gap = max(0, gap);
  if (itvl > 0) { bleRequestInterval((uint16_t)itvl); delay(300); }   // let the update land first
  bt.run = true; bt.loopLast = millis();
  xTaskCreatePinnedToCore(bleTestTask, "bletest", 4096, nullptr, 1, nullptr, 0);
}

static void bleTestPoll() {                         // main loop: dwell stats + summary line
  if (bt.run) {
    uint32_t now = millis(), dt = now - bt.loopLast;
    bt.loopLast = now;
    if (dt > bt.loopMax) bt.loopMax = dt;
    return;
  }
  if (!bt.done) return;
  bt.done = false;
  BleLinkInfo li = bleLinkInfo();
  JsonDocument d;
  d["t"] = "btend"; d["lines"] = bt.lines; d["bytes"] = bt.bytes; d["ms"] = bt.ms;
  d["retries"] = bt.retries; d["wait_ms"] = bt.waitMs; d["len"] = bt.len; d["chunk"] = bt.chunk;
  d["mtu"] = li.mtu; d["itvl_ms"] = li.itvl * 1.25f; d["latency"] = li.latency;
  d["loop_max_ms"] = bt.loopMax; d["rssi"] = WiFi.RSSI(); d["tx_dropped"] = bleTxDropped();
  d["bps"] = bt.ms ? (uint32_t)((uint64_t)bt.bytes * 1000 / bt.ms) : 0;
  sendJson(d);
  Serial.printf("bletest: %lu lines %lu B in %lu ms = %lu B/s, %lu retries (%lu ms waiting), mtu %u itvl %.2f ms, loop max %lu\n",
                (unsigned long)bt.lines, (unsigned long)bt.bytes, (unsigned long)bt.ms,
                (unsigned long)(bt.ms ? bt.bytes * 1000 / bt.ms : 0), (unsigned long)bt.retries,
                (unsigned long)bt.waitMs, li.mtu, li.itvl * 1.25f, (unsigned long)bt.loopMax);
}
// ---------- networking ----------
// ---- host -> card file push (基B), TCP only ------------------------------
// {"t":"fbeg","id",path,size,crc} -> .part opened; {"t":"fdat","id","seq","d"}
// base64 ≤1024 raw bytes each, in order; {"t":"fend","id"} -> size+CRC32
// checked, .part renamed over the target. Every step answers {"t":"fack"}.
static void sdFileArrived(const char* path);   // reacts to freshly pushed files
static File     xfFile;
static int      xfId = -1;
static uint32_t xfSize = 0, xfGot = 0, xfCrc = 0, xfWant = 0, xfSeq = 0, xfT0 = 0;
static char     xfPath[96];
// Chunks are staged in PSRAM and hit the card 64 KB at a time: a cheap card
// spends ~20 ms per write command, so 1 KB writes crawled at ~45 KB/s.
static const size_t XF_BUF = 65536;
static uint8_t* xfBuf = nullptr;
static size_t   xfFill = 0;
// transfer profiling (reported in the final fack): where does the time go?
static uint32_t xfIters = 0, xfMaxL = 0, xfIterL = 0, xfHandleUs = 0, xfWriteUs = 0,
                xfGapMs = 0, xfGaps = 0, xfLastPoll = 0, xfAvailMax = 0;
static uint32_t xfLastData = 0, xfWaits = 0;
static bool     xfNull = false;          // dst "/dev/null": measure the pipe, skip decode+write
static bool     xfSerial = false;        // transfer arrived over USB-CDC: no flow control there, so every fdat is acked back on the serial line (host/sdpush_usb.py paces on them)

static bool xfFlush() {
  if (!xfFill) return true;
  uint32_t t = micros();
  bool ok = xfFile.write(xfBuf, xfFill) == xfFill;
  xfWriteUs += micros() - t;
  xfFill = 0;
  return ok;
}

static void xfReply(int id, bool ok, const char* why = nullptr) {
  JsonDocument r;
  r["t"] = "fack"; r["id"] = id; r["ok"] = ok;
  if (why) r["why"] = why;
  if (ok && xfGot) {
    r["bytes"] = xfGot; r["ms"] = millis() - xfT0;
    r["iters"] = xfIters; r["maxl"] = xfMaxL; r["avail"] = xfAvailMax;
    r["hus"] = xfSeq ? xfHandleUs / xfSeq : 0; r["wms"] = xfWriteUs / 1000;
    r["gap"] = xfGaps ? xfGapMs / xfGaps : 0; r["waits"] = xfWaits;
  }
  sendJson(r);
}
static void xfAbort(const char* why) {
  if (xfFile) xfFile.close();
  xfFill = 0;
  if (sdFs()) {
    char tmp[104];
    snprintf(tmp, sizeof(tmp), "%s.part", xfPath);
    sdFs()->remove(tmp);
  }
  if (xfId >= 0) xfReply(xfId, false, why);
  xfId = -1;
}

// {"t": "fdat", "id": N, "seq": N, "d": "<base64>"} parsed by hand: the
// generic ArduinoJson path cost ~3.3 ms per 1 KB chunk, this is well under 1.
static long xfJsonInt(const char* line, const char* key) {
  const char* p = strstr(line, key);
  if (!p) return -1;
  p += strlen(key);
  while (*p == ':' || *p == ' ') p++;
  return atol(p);
}
static void handleFdat(const char* line) {
  if (xfId < 0 || xfJsonInt(line, "\"id\"") != xfId) return;
  if ((uint32_t)xfJsonInt(line, "\"seq\"") != xfSeq) { xfAbort("seq"); return; }
  const char* d = strstr(line, "\"d\"");
  if (!d) { xfAbort("nod"); return; }
  d += 3;
  while (*d == ':' || *d == ' ') d++;
  if (*d != '"') { xfAbort("nod"); return; }
  d++;
  const char* e = strchr(d, '"');
  if (!e) { xfAbort("nod"); return; }
  uint32_t tH = micros();
  xfIterL++;
  if (xfNull) {
    xfGot += 1024; xfSeq++; xfLastData = millis();
    if (xfSerial) Serial.printf("{\"t\":\"fack\",\"id\":%d,\"seq\":%lu}\n", xfId, (unsigned long)(xfSeq - 1));
    return;
  }
  static uint8_t dec[1032];
  size_t n = 0;
  if (mbedtls_base64_decode(dec, sizeof(dec), &n, (const uint8_t*)d, e - d) != 0) {
    xfAbort("b64"); return;
  }
  if (xfFill + n > XF_BUF && !xfFlush()) { xfAbort("write"); return; }
  memcpy(xfBuf + xfFill, dec, n);
  xfFill += n;
  xfCrc = esp_rom_crc32_le(xfCrc, dec, n);
  xfGot += n; xfSeq++;
  xfLastData = millis();
  xfHandleUs += micros() - tH;
  // USB-CDC has no flow control: the per-chunk ack is what keeps the sender
  // under the 4352 B RX queue (TCP transfers never see this line)
  if (xfSerial) Serial.printf("{\"t\":\"fack\",\"id\":%d,\"seq\":%lu}\n", xfId, (unsigned long)(xfSeq - 1));
}

// ---- 基C: one JSON record per day on the card ----------------------------
// /agentpet/days/YYYY-MM-DD.json = today's leaderboard (host report) + the
// pet's growth counters. Rewritten at most every 5 min (so a day's file is
// at worst 5 min stale at midnight), and at once when the date changes.
// "w"/"d" carry one column per AGENTS entry in table order, so the array
// length follows N_AGENTS (4 before 2026-09-13, 5 after). The board never
// reads these files back — they are an archive for the host — so readers
// must size off the array they find and treat missing trailing columns as
// 0 (an old four-column day predates the forest seat). A day whose file was
// written by the four-seat build is simply rewritten with five columns the
// next time this runs; no in-place migration.
static void sdDayRecord(const int* w, const int* dn, bool force) {
  static uint32_t lastWrite = 0;
  static char lastDate[11] = "";
  fs::FS* fs = sdFs();
  uint32_t t = clockEpoch();
  if (!fs || !t) return;
  time_t tt = (time_t)t;
  struct tm tmv;
  gmtime_r(&tt, &tmv);                     // local seconds -> fields
  char date[11];
  snprintf(date, sizeof(date), "%04d-%02d-%02d", tmv.tm_year + 1900, tmv.tm_mon + 1, tmv.tm_mday);
  bool newDay = strcmp(date, lastDate) != 0;
  if (!force && !newDay && (int32_t)(millis() - lastWrite) < 5 * 60000) return;
  char path[48];
  snprintf(path, sizeof(path), "/agentpet/days/%s.json", date);
  sdMkdirs(path);
  File f = fs->open(path, FILE_WRITE);
  if (!f) return;
  uint32_t xp = statDone * 10 + statPets * 2 + statDays * 80;
  f.printf("{\"date\":\"%s\",\"w\":[", date);
  for (int i = 0; i < N_AGENTS; i++) { if (i) f.print(','); f.printf("%d", w[i]); }
  f.print("],\"d\":[");
  for (int i = 0; i < N_AGENTS; i++) { if (i) f.print(','); f.printf("%d", dn[i]); }
  f.printf("],\"done\":%lu,\"days\":%lu,\"pets\":%lu,\"xp\":%lu}\n",
           (unsigned long)statDone, (unsigned long)statDays, (unsigned long)statPets,
           (unsigned long)xp);
  f.close();
  lastWrite = millis();
  snprintf(lastDate, sizeof(lastDate), "%s", date);
  Serial.printf("sd: day record %s\n", path);
}

// ---- screenshot: canvas -> host, TCP only ---------------------------------
// Closes the "Claude can't see the face it drew" loop.
// {"t":"shot","id":N,"s":S} -> {"t":"sbeg",id,w,h,n,step}
//                             + n × {"t":"sdat",id,seq,d}   (base64, ≤1440 raw B)
//                             + {"t":"sfin",id,ok,ms,bytes,w,h}
// S = downsample step: 1 = full 480×480 (460 KB), 2 = 240×240 box-averaged
// (115 KB, the default — enough to judge a coordinate). Pixels are the raw
// framebuffer words, RGB565 little-endian, rows top to bottom: exactly what
// the panel was last flushed with (rotation is only set around draws).
// Synchronous on the loop task: the face freezes ~3 s at S=2 and the frame
// cannot change underneath us. The request may arrive over BLE; the reply
// never goes there (NUS is 100 B/packet) — no TCP means sfin why:"no tcp".
// (rxFromSerial is defined up by sendJson: replies to a USB-CDC line go back on it)

static void sendShot(int id, int step) {
  JsonDocument r;
  r["t"] = "sfin"; r["id"] = id; r["ok"] = false;
  // The reply goes back the way the request came: USB-CDC (host/shot.py —
  // works with no network at all, the office case) or the TCP socket. Never BLE.
  // one emitter for both channels: serial writes straight, TCP goes through
  // the door (bulk transfer: generous 3 s wait per batch, remainder parked)
  bool haveOut = rxFromSerial || sock.connected();
  auto emit = [&](const char* p, size_t n) -> bool {
    if (rxFromSerial) return Serial.write((const uint8_t*)p, n) == n;
    return tcpWriteLines(p, n, 3000, true);
  };
  const char* why = nullptr;
  if (!haveOut)                       why = "no tcp";
  else if (xfId >= 0)                 why = "busy";     // a file push owns the loop
  else if (!canvas->getFramebuffer()) why = "no fb";
  if (why) {
    r["why"] = why;
    if (haveOut) { String f; serializeJson(r, f); f += '\n'; emit(f.c_str(), f.length()); } else sendJson(r);
    return;
  }

  if (step < 1) step = 1;
  if (step > 8) step = 8;
  const int w = LCD_W / step, h = LCD_H / step;
  const uint32_t total = (uint32_t)w * h * 2;
  uint16_t* snap = (uint16_t*)heap_caps_malloc(total, MALLOC_CAP_SPIRAM);
  if (!snap) { r["why"] = "psram"; String f; serializeJson(r, f); f += '\n'; emit(f.c_str(), f.length()); return; }
  uint32_t t0 = millis();

  const uint16_t* fb = canvas->getFramebuffer();
  if (step == 1) {
    memcpy(snap, fb, total);
  } else {                                      // box average over step×step
    const int n = step * step;
    for (int y = 0; y < h; y++) {
      for (int x = 0; x < w; x++) {
        uint32_t rs = 0, gs = 0, bs = 0;
        for (int dy = 0; dy < step; dy++) {
          const uint16_t* row = fb + (uint32_t)(y * step + dy) * LCD_W + x * step;
          for (int dx = 0; dx < step; dx++) {
            uint16_t p = row[dx];
            rs += (p >> 11) & 31; gs += (p >> 5) & 63; bs += p & 31;
          }
        }
        snap[y * w + x] = (uint16_t)(((rs / n) << 11) | ((gs / n) << 5) | (bs / n));
      }
    }
  }

  // Lines are hand-built (no JsonDocument churn per chunk) and batched four
  // to a write so lwIP emits full segments — same trick as host push_file().
  const int RAW = 1440, B64 = RAW / 3 * 4;      // 1920 base64 chars per line
  const int LINE = 64 + B64;                    // header + data + "\"}\n"
  const int nChunks = (total + RAW - 1) / RAW;
  char* buf = (char*)heap_caps_malloc(LINE * 4 + 16, MALLOC_CAP_SPIRAM);
  bool ok = buf != nullptr;
  if (ok) {
    JsonDocument b;
    b["t"] = "sbeg"; b["id"] = id; b["w"] = w; b["h"] = h; b["n"] = nChunks; b["step"] = step;
    String hdr; serializeJson(b, hdr); hdr += '\n';
    ok = emit(hdr.c_str(), hdr.length());
  }
  int used = 0;
  const uint8_t* p = (const uint8_t*)snap;
  for (int seq = 0; ok && seq < nChunks; seq++) {
    uint32_t off = (uint32_t)seq * RAW;
    uint32_t n = total - off < (uint32_t)RAW ? total - off : RAW;
    used += snprintf(buf + used, 64, "{\"t\":\"sdat\",\"id\":%d,\"seq\":%d,\"d\":\"", id, seq);
    size_t olen = 0;
    mbedtls_base64_encode((unsigned char*)buf + used, B64 + 4, &olen, p + off, n);
    used += olen;
    memcpy(buf + used, "\"}\n", 3); used += 3;
    if ((seq & 3) == 3 || seq == nChunks - 1) {
      ok = emit(buf, used);
      used = 0;
      wdtFeed();      // 480×480 = 320 batches, each allowed to wait 3 s on lwIP
    }
  }
  free(buf);
  free(snap);
  uint32_t ms = millis() - t0;
  r["ok"] = ok; r["ms"] = ms; r["bytes"] = total; r["w"] = w; r["h"] = h;
  if (!ok) r["why"] = "tcp write";
  String fin; serializeJson(r, fin); fin += '\n';
  emit(fin.c_str(), fin.length());              // same channel as the data: keeps order
  Serial.printf("shot: %dx%d %u B %s in %u ms\n", w, h, (unsigned)total, ok ? "ok" : "FAIL", (unsigned)ms);
}

// ---------- SD-OTA ----------
// {"t":"ota",id,path,size,crc}: flash <path> from the card into the other app
// slot (default_16MB.csv has app0/app1) and reboot into it. The host pushed
// the file over the fbeg/fdat channel first (CRC-verified on write); the CRC
// is run again while streaming it into Update, so a stale or half-written
// file can never be booted. {"t":"ota",id,rollback:true}: boot the other
// slot (the previous firmware) — the manual undo, no bootloader rollback
// config needed. A USB flash (pio upload) always lands in app0 and boots it.
static void otaScreen(const char* msg) {
  canvas->fillScreen(0);
  canvas->fillRoundRect(103, 200, 110, 10, 5, 0xFFFF);   // closed eyes, like powerOff
  canvas->fillRoundRect(267, 200, 110, 10, 5, 0xFFFF);
  canvas->setTextSize(3);
  canvas->setTextColor(0x7BEF);
  canvas->setCursor(240 - (int)strlen(msg) * 9, 300);
  canvas->print(msg);
  canvas->flush();
}

static void otaRun(int id, const char* path, uint32_t size, uint32_t wantCrc) {
  JsonDocument r;
  r["t"] = "ota"; r["id"] = id; r["ok"] = false;
  const char* why = nullptr;
  fs::FS* fs = sdFs();
  File f;
  if (xfId >= 0)                 why = "busy";
  else if (!fs)                  why = "no sd";
  else if (!size)                why = "size 0";
  else if (!(f = fs->open(path, FILE_READ))) why = "open";
  else if (f.size() != size)     why = "size";
  if (why) { if (f) f.close(); r["why"] = why; sendJson(r); return; }
  otaScreen("UPDATING");
  uint32_t t0 = millis();
  const size_t BLK = 16384;
  uint8_t* buf = (uint8_t*)ps_malloc(BLK);
  uint32_t crc = 0, got = 0;
  if (!buf) why = "mem";
  else if (!Update.begin(size, U_FLASH)) why = Update.errorString();
  while (!why && got < size) {
    int n = f.read(buf, BLK);
    if (n <= 0) { why = "short read"; break; }
    crc = esp_rom_crc32_le(crc, buf, n);
    if (Update.write(buf, n) != (size_t)n) { why = Update.errorString(); break; }
    got += n;
    wdtFeed();      // ~1.5 MB of flash erase+write, ~7.6 s with no loop pass
  }
  f.close();
  if (buf) free(buf);
  if (!why && crc != wantCrc) why = "crc";
  if (why) Update.abort();
  else if (!Update.end(true)) why = Update.errorString();
  r["ms"] = millis() - t0; r["bytes"] = got;
  if (why) {
    r["why"] = why;
    sendJson(r);
    Serial.printf("ota: FAILED (%s) after %lu B\n", why, (unsigned long)got);
    return;                                    // the face loop repaints on the next frame
  }
  const esp_partition_t* boot = esp_ota_get_boot_partition();
  r["ok"] = true; r["part"] = boot ? boot->label : "?";
  sendJson(r);
  Serial.printf("ota: ok %lu B in %lu ms -> %s, restarting\n", (unsigned long)got,
                (unsigned long)(millis() - t0), boot ? boot->label : "?");
  otaScreen("REBOOT");
  delay(500);                                  // let the reply leave over TCP/BLE
  ESP.restart();
}

static void otaRollback(int id) {
  JsonDocument r;
  r["t"] = "ota"; r["id"] = id; r["ok"] = false;
  const esp_partition_t* run = esp_ota_get_running_partition();
  const esp_partition_t* other = esp_ota_get_next_update_partition(run);
  esp_app_desc_t desc;
  if (!other || other == run)                                         r["why"] = "no other slot";
  else if (esp_ota_get_partition_description(other, &desc) != ESP_OK) r["why"] = "other slot empty";
  else if (esp_ota_set_boot_partition(other) != ESP_OK)               r["why"] = "set boot";
  else { r["ok"] = true; r["part"] = other->label; r["ver"] = desc.version; }
  sendJson(r);
  if (!(r["ok"] | false)) return;
  Serial.printf("ota: rollback -> %s (%s), restarting\n", other->label, desc.version);
  otaScreen("ROLLBACK");
  delay(500);
  ESP.restart();
}

static int spkId = 0;                 // id of the TTS clip in flight (speak header)

// The host names the cover file it believes is on the card. If it is not
// there, ask for it — once per path, because the play page would otherwise
// re-ask 30 times a second. Cleared when that very file lands (sdFileArrived),
// so a cover deleted later can be fetched again.
static char npMissAsked[80] = "";
static void npCoverCheck(const char* path) {
  if (!path || path[0] != '/') return;
  if (strcmp(path, npMissAsked) == 0) return;
  fs::FS* fs = sdFs();
  if (!fs || fs->exists(path)) return;
  snprintf(npMissAsked, sizeof(npMissAsked), "%s", path);
  sendEvent("np_miss", "cover", path);
}

static bool btnPwrArmed();   // defined with the Btn table below (debug 2026-09-19)
static uint32_t pmuKeyEdges = 0;      // debug: PEK edges seen since boot
static uint64_t pmuIrqLast = 0;       // debug: last non-zero IRQ status word
static uint32_t pmuIrqPolls = 0;      // debug: how many times we polled
static bool btnPwrDown();
// ---------- known Wi-Fi networks ----------
// The Mac's settings page adds networks over BLE ({"t":"wifi","op":"add"});
// they live in NVS "wifinet" (s0..s3 / p0..p3, newest first). config.h's
// WIFI_SSID is the factory entry, always known, never stored, never removed
// (only when config.local.h sets one: kFactoryWifi; the public build has none).
// Connecting = scan, then join the strongest known network in range, so a
// board carried to another place picks that place's network by itself.
// Passwords never leave the board: replies carry SSIDs and signal only.
static const int WIFI_NETS_MAX = 4;
static Preferences wifiPrefs;
static String   wifiSsid[WIFI_NETS_MAX], wifiPass[WIFI_NETS_MAX];
static int      wifiN = 0;
static int16_t  wifiSeen[WIFI_NETS_MAX + 1];   // last scan's RSSI per net (last slot = factory), 0 = not seen
static bool     wifiScanning = false;
static uint32_t wifiScanAt = 0;                // when the last scan started
static int      wifiReplyId = -1;              // a list reply owed once the scan lands (-1 = none)
// What the last scan saw, strongest first (the page offers these as one-tap
// names: proves the board can hear it, i.e. it is 2.4 GHz and in range).
static const int WIFI_NEAR_MAX = 10;
static String   wifiNear[WIFI_NEAR_MAX];
static int16_t  wifiNearRssi[WIFI_NEAR_MAX];
static int      wifiNearN = 0;

static void wifiLoad() {
  wifiPrefs.begin("wifinet", false);
  wifiN = 0;
  for (int i = 0; i < WIFI_NETS_MAX; i++) {
    char k[4];
    snprintf(k, sizeof k, "s%d", i);
    String sid = wifiPrefs.getString(k, "");
    if (!sid.length()) continue;
    snprintf(k, sizeof k, "p%d", i);
    wifiSsid[wifiN] = sid;
    wifiPass[wifiN] = wifiPrefs.getString(k, "");
    wifiN++;
  }
}

static void wifiSave() {
  for (int i = 0; i < WIFI_NETS_MAX; i++) {
    char ks[4], kp[4];
    snprintf(ks, sizeof ks, "s%d", i);
    snprintf(kp, sizeof kp, "p%d", i);
    if (i < wifiN) { wifiPrefs.putString(ks, wifiSsid[i]); wifiPrefs.putString(kp, wifiPass[i]); }
    else if (wifiPrefs.isKey(ks)) { wifiPrefs.remove(ks); wifiPrefs.remove(kp); }
  }
}

// Kick off an async scan; wifiPoll() finishes it. Harmless while one runs.
static void wifiStartScan() {
  if (wifiScanning) return;
  if (WiFi.getMode() == WIFI_OFF) WiFi.mode(WIFI_STA);
  wifiScanAt = millis();               // also on failure: retry in 30 s, not every pass
  if (WiFi.scanNetworks(true, false, false, 120) == WIFI_SCAN_FAILED) return;
  wifiScanning = true;
}

static void wifiReply(int id, bool ok, const char* why) {
  JsonDocument r;
  r["t"] = "wifi"; r["id"] = id; r["ok"] = ok;
  if (why) r["why"] = why;
  JsonArray a = r["nets"].to<JsonArray>();
  for (int i = 0; i < wifiN + (kFactoryWifi ? 1 : 0); i++) {
    bool fac = i == wifiN;
    JsonObject o = a.add<JsonObject>();
    o["s"] = fac ? WIFI_SSID : wifiSsid[i].c_str();
    if (fac) o["fac"] = 1;
    o["seen"] = wifiSeen[fac ? WIFI_NETS_MAX : i];
  }
  bool up = WiFi.status() == WL_CONNECTED;
  if (up) { r["cur"] = WiFi.SSID(); r["rssi"] = WiFi.RSSI(); r["ip"] = WiFi.localIP().toString(); }
  r["scan"] = wifiScanning ? 1 : 0;
  JsonArray nr = r["near"].to<JsonArray>();
  for (int i = 0; i < wifiNearN; i++) {
    JsonObject o = nr.add<JsonObject>();
    o["s"] = wifiNear[i]; o["r"] = wifiNearRssi[i];
  }
  sendJson(r);
}

static void wifiHandle(JsonDocument& d) {
  const char* op = d["op"] | "list";
  int id = d["id"] | 0;
  String sid = d["s"] | "";
  if (strcmp(op, "add") == 0) {
    String pw = d["p"] | "";
    if (!sid.length() || sid.length() > 32) { wifiReply(id, false, "ssid"); return; }
    if (pw.length() && (pw.length() < 8 || pw.length() > 63)) { wifiReply(id, false, "pass"); return; }
    if (kFactoryWifi && sid == WIFI_SSID) { wifiReply(id, false, "factory"); return; }
    // newest first: drop any old copy, shift down, the oldest falls off the end
    int keep = 0;
    String ns[WIFI_NETS_MAX], np[WIFI_NETS_MAX];
    ns[keep] = sid; np[keep] = pw; keep++;
    for (int i = 0; i < wifiN && keep < WIFI_NETS_MAX; i++)
      if (wifiSsid[i] != sid) { ns[keep] = wifiSsid[i]; np[keep] = wifiPass[i]; keep++; }
    for (int i = 0; i < WIFI_NETS_MAX; i++) { wifiSsid[i] = ns[i]; wifiPass[i] = np[i]; wifiSeen[i] = 0; }
    wifiN = keep;
    wifiSave();
    Serial.printf("wifi: saved \"%s\" (%d known)\n", sid.c_str(), wifiN + (kFactoryWifi ? 1 : 0));
    // not online (or asked to switch): go find it now instead of at the next probe
    if (WiFi.status() != WL_CONNECTED || (d["join"] | 0)) {
      WiFi.disconnect(false);
      wifiStartScan();
    }
    wifiReply(id, true, nullptr);
  } else if (strcmp(op, "rm") == 0) {
    int at = -1;
    for (int i = 0; i < wifiN; i++) if (wifiSsid[i] == sid) at = i;
    if (at < 0) { wifiReply(id, false, kFactoryWifi && sid == WIFI_SSID ? "factory" : "unknown"); return; }
    for (int i = at; i < wifiN - 1; i++) { wifiSsid[i] = wifiSsid[i + 1]; wifiPass[i] = wifiPass[i + 1]; wifiSeen[i] = wifiSeen[i + 1]; }
    wifiN--;
    wifiSsid[wifiN] = ""; wifiPass[wifiN] = ""; wifiSeen[wifiN] = 0;
    wifiSave();
    Serial.printf("wifi: forgot \"%s\"\n", sid.c_str());
    wifiReply(id, true, nullptr);
  } else if (strcmp(op, "scan") == 0) {   // fresh "seen" column for the page; answers when done
    if (WiFi.status() != WL_CONNECTED || !wifiScanning) wifiStartScan();
    if (wifiScanning) wifiReplyId = id; else wifiReply(id, true, nullptr);
  } else {
    wifiReply(id, true, nullptr);
  }
}

// ---- Owner Mac ----
// NVS "owner": the Mac this board follows. Its Wi-Fi TCP goes there (mDNS
// first, then the last IP it told us), and while it is set only that Mac's
// BLE link carries app traffic. Empty = no owner: the first host that says
// hostinfo takes it (upgrade and first boot need no clicks), TCP falls back
// to config.h. Changing hands is always explicit — a claim from the new Mac;
// an absent owner is never timed out (用户 09-26 定).
static Preferences ownerPrefs;
static String ownerId, ownerName, ownerMdns, ownerIp;
static uint16_t ownerPort = HOST_PORT;
static uint16_t curBleLink = BLE_NO_LINK;   // BLE link the line in handleLine came from

static uint32_t ownerDigest(const String& id) {   // FNV-1a 32, same as host owner_digest()
  uint32_t h = 2166136261u;
  for (size_t i = 0; i < id.length(); i++) { h ^= (uint8_t)id[i]; h *= 16777619u; }
  return h ? h : 1;                               // 0 means "no owner" on the air
}

static void ownerLoad() {
  ownerPrefs.begin("owner", false);
  ownerId   = ownerPrefs.getString("id", "");
  ownerName = ownerPrefs.getString("name", "");
  ownerMdns = ownerPrefs.getString("mdns", "");
  ownerIp   = ownerPrefs.getString("ip", "");
  ownerPort = ownerPrefs.getUShort("port", HOST_PORT);
  bleSetOwner(ownerId.length() > 0, ownerId.length() ? ownerDigest(ownerId) : 0);
  Serial.printf("owner: %s\n", ownerId.length() ? ownerName.c_str() : "(none)");
}

static void ownerReply(JsonDocument& r) {   // back the way the hostinfo/claim came
  if (curBleLink != BLE_NO_LINK) { String s; serializeJson(r, s); bleSendLineTo(curBleLink, s); }
  else sendJson(r);
}

// Take `d` as the owner (the tap said yes, or it already was the owner and is
// only refreshing its address). Replies on curBleLink / the line's own path.
static void ownerApply(JsonDocument& d, bool claim) {
  String id = d["id"] | "";
  bool changed = id != ownerId;
  ownerId = id;
  ownerName = String(d["name"] | "").substring(0, 64);
  ownerMdns = String(d["mdns"] | "").substring(0, 64);
  const char* ip = d["ip"] | "";
  if (*ip) ownerIp = ip;
  ownerPort = d["port"] | (int)HOST_PORT;
  ownerPrefs.putString("id", ownerId); ownerPrefs.putString("name", ownerName);
  ownerPrefs.putString("mdns", ownerMdns); ownerPrefs.putString("ip", ownerIp);
  ownerPrefs.putUShort("port", ownerPort);
  bleSetOwner(true, ownerDigest(ownerId));
  if (curBleLink != BLE_NO_LINK) bleTrust(curBleLink);
  JsonDocument r;
  r["t"] = "owner"; r["mine"] = true; r["id"] = ownerId; r["name"] = ownerName; r["claimed"] = changed;
  ownerReply(r);
  if (!changed) return;
  Serial.printf("owner: now %s (%s)\n", ownerName.c_str(), claim ? "claim" : "first host");
  sessClear();                          // the old Mac's terminals are not this one's
  JsonDocument rel;
  rel["t"] = "released"; rel["by"] = ownerName; rel["id"] = ownerId;
  String rs; serializeJson(rel, rs);
  bleKickOthers(curBleLink, rs, 800);
  // The TCP peer is the old owner unless this very line came over it.
  if (curBleLink != BLE_NO_LINK && !rxFromSerial && sock.connected()) {
    tcpWriteLine(rs, 300);
    sock.stop();                        // netPoll reconnects to the new owner in ~3 s
  }
}

// ---- Claim card ----
// A new owner — the first one or a takeover — only counts after a tap on the
// board, so a colleague's Mac can't take the board from across the office.
// One prompt at a time; 30 s without a tap = declined. A Mac whose automatic
// first-owner hostinfo was declined is not asked again for 10 min (its host
// reconnects every scan); an explicit claim always asks.
static const uint32_t CLAIM_ASK_MS = 30000, CLAIM_REASK_MS = 600000;
static bool     claimOn = false;         // prompt up, waiting for the tap
static uint32_t claimAt = 0, claimUntil = 0, claimResAt = 0;
static bool     claimAccepted = false, claimIsClaim = false;
static uint16_t claimLink = BLE_NO_LINK; // BLE_NO_LINK = asked over TCP
static JsonDocument claimDoc;
static char     claimName[96], claimSub[128];
static String   declinedId;
static uint32_t declinedAt = 0;

static bool claimShowing() {             // card on screen: pending or its 1.4 s outro
  if (claimOn) return true;
  if (!claimResAt) return false;
  return (int32_t)(millis() - claimResAt) < (claimAccepted ? 1400 : 400);
}

static void claimReply(JsonDocument& r) {    // to the Mac that asked, wherever it is
  if (claimLink != BLE_NO_LINK) {
    if (!bleLinked(claimLink)) return;
    String s; serializeJson(r, s); bleSendLineTo(claimLink, s);
  } else sendJson(r);
}

static void claimResolve(bool yes, const char* why) {
  if (!claimOn) return;
  claimOn = false;
  claimResAt = millis(); claimAccepted = yes;
  if (claimLink != BLE_NO_LINK) bleHold(claimLink, false);
  if (yes) {
    uint16_t save = curBleLink;
    curBleLink = claimLink;
    ownerApply(claimDoc, claimIsClaim);
    curBleLink = save;
    audioPlay(SND_DONE);
  } else {
    declinedId = claimDoc["id"] | ""; declinedAt = millis();
    JsonDocument r;
    r["t"] = "owner"; r["mine"] = false; r["declined"] = true; r["why"] = why;
    r["id"] = ownerId; r["name"] = ownerName;
    claimReply(r);
    if (claimLink != BLE_NO_LINK && !bleIsTrusted(claimLink)) bleKick(claimLink, 1500);   // never the owner's own link
    audioPlay(SND_TICK);
  }
  Serial.printf("claim: %s (%s)\n", yes ? "accepted" : "declined", why);
}

static void claimTick() {                  // loop: timeout, asker gone
  if (!claimOn) return;
  if ((int32_t)(millis() - claimUntil) >= 0) claimResolve(false, "timeout");
  else if (claimLink != BLE_NO_LINK && !bleLinked(claimLink)) {
    claimOn = false; claimResAt = 0;       // the Mac left: drop the card quietly
    Serial.println("claim: asker disconnected");
  }
}

// {"t":"hostinfo"|"claim","id","name","mdns","ip","port"}. hostinfo = "I am
// here" (every link-up); claim = "follow me from now on".
static void ownerHandle(JsonDocument& d, bool claim) {
  String id = d["id"] | "";
  if (id.length() == 0 || id.length() > 64) return;
  JsonDocument r;
  r["t"] = "owner"; r["id"] = ownerId; r["name"] = ownerName;
  if (ownerId.length() && id == ownerId) { ownerApply(d, false); return; }   // the owner, refreshing
  bool ask = claim || ownerId.length() == 0;
  const char* no = nullptr;
  if (!ask) no = "owned";
  else if (!claim && id == declinedId && (int32_t)(millis() - declinedAt) < (int32_t)CLAIM_REASK_MS) no = "declined";
  else if (flipped) no = "asleep";         // face down: nobody can see the card
  else if (claimOn && id != String(claimDoc["id"] | "")) no = "busy";
  if (no) {
    r["mine"] = false; r["why"] = no;
    ownerReply(r);
    if (curBleLink != BLE_NO_LINK && !bleIsTrusted(curBleLink)) bleKick(curBleLink, 1500);   // let the reply land
    return;
  }
  uint32_t now = millis();
  if (!claimOn) {                          // a fresh prompt (a repeat just re-reports)
    claimOn = true; claimAt = now; claimUntil = now + CLAIM_ASK_MS; claimResAt = 0;
    claimDoc = d; claimIsClaim = claim; claimLink = curBleLink;
    // Mac name: its display name, or the ASCII Bonjour name when a glyph is
    // missing from every font the board has (a cardless board, a rare hanzi).
    const char* nm = d["name"] | "";
    const char* md = d["mdns"] | "";
    almanacFit(claimName, sizeof(claimName), (almanacHasAll(nm) || !*md) ? nm : md, 384, false);
    if (ownerId.length()) {
      char who[96];
      almanacFit(who, sizeof(who), almanacHasAll(ownerName.c_str()) || !ownerMdns.length()
                                   ? ownerName.c_str() : ownerMdns.c_str(), 280, true);
      snprintf(claimSub, sizeof(claimSub), tr(S_CLAIM_FROM), who);
    } else {
      strlcpy(claimSub, tr(S_CLAIM_NEW), sizeof(claimSub));
    }
    setCardUntil = profUntil = sendUntil = 0;    // the card owns the face now
    audioPlay(SND_NEEDS);
    Serial.printf("claim: asking for %s\n", nm);
  }
  if (claimLink != BLE_NO_LINK) bleHold(claimLink, true);
  r["mine"] = false; r["pending"] = true;
  r["sec"] = (int)((claimUntil - now) / 1000);
  ownerReply(r);
}

static void handleLine(const String& line) {
  bool visitor = curBleLink != BLE_NO_LINK && !bleIsTrusted(curBleLink);
  if (line.startsWith("{\"t\": \"fdat\"") || line.startsWith("{\"t\":\"fdat\"")) {
    if (visitor) return;
    handleFdat(line.c_str());
    return;
  }
  JsonDocument d;
  if (deserializeJson(d, line)) return;
  const char* t = d["t"] | "";
  if (strcmp(t, "hostinfo") == 0 || strcmp(t, "claim") == 0) {
    ownerHandle(d, t[0] == 'c');
    return;
  }
  if (visitor) return;       // not the owner's link: only hostinfo/claim get through
  lastRx = millis();
  if (strcmp(t, "state") == 0) {
    uint8_t prev[N_AGENTS];
    memcpy(prev, agentStates, sizeof(prev));
    JsonObjectConst ag = d["agents"].as<JsonObjectConst>();
    for (JsonPairConst kv : ag) {
      int idx = agentIdxById(kv.key().c_str());
      if (idx < 0) continue;
      agentStates[idx] = stateByName(kv.value() | "off");
    }
    if (memcmp(prev, agentStates, sizeof(prev)) != 0) noteActivity();
    // chirp on fresh needs_you / done from any agent (needs wins the push)
    bool fresh_needs = false, fresh_done = false;
    for (int i = 0; i < N_AGENTS; i++) {
      if (agentStates[i] != prev[i]) {
        if (agentStates[i] == ST_NEEDS_YOU) fresh_needs = true;
        if (agentStates[i] == ST_DONE) {
          fresh_done = true;
          statDone++;                    // every finished task feeds the pet
          growPrefs.putUInt("done", statDone);
        }
      }
    }
    // with TTS voice on and TCP up the host speaks these moments instead
    // (it pushes the chirp itself if synthesis fails — never silent)
    bool ttsWill = voiceTts && sock.connected();
    if (fresh_needs) {
      if (!ttsWill) audioPlay(SND_NEEDS);
      setCardUntil = 0;      // approval takes the stage over the settings card
    } else if (fresh_done && !ttsWill) audioPlay(SND_DONE);
#if AUTO_FOCUS_NEEDS_YOU
    // A background agent just flipped to needs_you: steal the big face.
    // Edge-triggered, so the user can still switch away manually afterwards.
    // Relay (2026-09-02): when the selected seat's request is answered and
    // another seat is still waiting, hand over to it — that seat raised its
    // hand while the first card was up and never got its own edge. A seat
    // the user deliberately swiped away from stays ignored (no edge here).
    // Asleep (face-down) the pet neither switches nor raises Mac windows.
    if (!listening && !flipped && agentStates[selected] != ST_NEEDS_YOU) {
      bool relay = prev[selected] == ST_NEEDS_YOU;
      for (int i = 0; i < N_AGENTS; i++) {
        if (i != selected && agentStates[i] == ST_NEEDS_YOU &&
            (relay || prev[i] != ST_NEEDS_YOU)) {
          selected = i;
          sendEvent("select", "agent", AGENTS[i].id);
          if (relay && !ttsWill) audioPlay(SND_NEEDS);   // "next one's up"
          break;
        }
      }
    }
#endif
    // Runs after the needs_you grab above, which keeps its priority (it
    // always lands on a non-off seat, so this is then a no-op).
    autoReselectIfOff();
  } else if (strcmp(t, "sound") == 0) {
    if (d["vol"].is<int>()) {            // remote volume set (host /test/volume)
      audioSetVolume(d["vol"].as<int>());
      volShowUntil = millis() + 1500;
      audioPlay(SND_TICK);
    } else {
      int id = soundIdByName(d["name"] | "");
      if (id >= 0) audioPlay((uint8_t)id);
    }
  } else if (strcmp(t, "speak") == 0) {     // TTS clip header (host, TCP only)
    spkId = d["id"] | 0;                      // echoed in the "spk" timing report
    const char* fmt = d["fmt"] | "pcm";       // "pcm" | "ima" (ADPCM 4:1)
    if (!audioSpeakBegin(d["len"] | 0u, strcmp(fmt, "ima") == 0 ? 1 : 0))
      Serial.println("speak: rejected (busy/oversize/audio down)");
  } else if (strcmp(t, "pcm") == 0) {       // base64 chunk of the clip
    const char* b64 = d["d"] | "";
    static uint8_t dec[1032];               // host sends ≤1024 raw bytes/chunk
    size_t n = 0;
    if (mbedtls_base64_decode(dec, sizeof(dec), &n,
                              (const uint8_t*)b64, strlen(b64)) == 0 && n)
      audioSpeakData(dec, (uint32_t)n);
  } else if (strcmp(t, "stretch") == 0) {   // host: human worked 90+ min straight
    stretchUntil = millis() + 10000;
    audioPlay(SND_SIGH);
    noteActivity();
  } else if (strcmp(t, "select") == 0) {    // host: Mac front app changed -> follow it (doc/06 最后意图获胜)
    int i = agentIdxById(d["agent"] | "");
    if (i >= 0 && i != selected) {
      selected = i;                          // silent: the user is at the Mac, not looking for a chirp
      noteActivity();
    }
    JsonDocument r;                          // echo so the host can confirm; src tells it not to re-follow
    r["t"] = "select"; r["agent"] = AGENTS[selected].id; r["src"] = "host";
    sendJson(r);
  } else if (strcmp(t, "np") == 0) {        // Mac "now playing"
    bool on = (d["on"] | 0) != 0;
    const char* cover = d["cover"] | "";
    npSet(on, d["title"] | "", d["artist"] | "", d["album"] | "",
          d["pos"] | 0.0f, d["dur"] | 0.0f, (d["play"] | 0) != 0,
          d["rate"] | 1.0f, d["app"] | "", cover);
    if (on) npCoverCheck(cover);
  } else if (strcmp(t, "pl") == 0) {        // host: /agentpet/audio/ changed
    playerRescan();
  } else if (strcmp(t, "play") == 0) {      // host debug (/test/play/<cmd>[&src=mac]): drive a page
    // Same helpers the tap zones run. Default target is the board player;
    // "src":"mac" routes to the Mac (that is the user's player — mind it).
    const char* cmd = d["cmd"] | "status";
    const char* sv  = d["src"] | "sd";
    uint8_t src = strcmp(sv, "mac") == 0 ? PSRC_MAC : PSRC_SD;
    if      (!strcmp(cmd, "toggle")) playAct(src, 0);
    else if (!strcmp(cmd, "next"))   { if (src == PSRC_SD) playerNext(); else playAct(src, +1); }
    else if (!strcmp(cmd, "prev"))   { if (src == PSRC_SD) playerPrev(); else playAct(src, -1); }
    else if (!strcmp(cmd, "fwd"))    playAct(src, +1);
    else if (!strcmp(cmd, "back"))   playAct(src, -1);
    else if (!strcmp(cmd, "pause"))  { if (src == PSRC_SD) playerPause(); }
    float pos = 0, dur = 0; bool playing = false;
    if (src == PSRC_MAC) npSnapshot(&pos, &dur, &playing);
    else {
      pos = (float)playerInfo().posSec; dur = (float)playerInfo().durSec;
      playing = playerInfo().playing;
    }
    JsonDocument r;
    r["t"] = "plst"; r["id"] = d["id"] | 0; r["cmd"] = cmd;
    r["pos"] = pos; r["dur"] = dur; r["play"] = playing ? 1 : 0;
    r["n"] = playerCount(); r["i"] = playerInfo().index;
    r["src"] = src == PSRC_MAC ? "mac" : "sd";
    r["page"] = clockView == CV_NP ? "np" : clockView == CV_POD ? "pod" : "clock";
    sendJson(r);
  } else if (strcmp(t, "view") == 0) {      // host debug (/test/view/<p>): force a page for s seconds
    const char* pv = d["p"] | "";
    int sec = d["s"] | 20;
    uint8_t pg = 255, sub = 0;
    if      (!strcmp(pv, "face"))     { pg = PAGE_FACE; }
    else if (!strcmp(pv, "clock"))    { pg = PAGE_CLOCK;    sub = CV_CLOCK; }
    else if (!strcmp(pv, "np") || !strcmp(pv, "play")) { pg = PAGE_CLOCK; sub = CV_NP; }
    else if (!strcmp(pv, "pod"))      { pg = PAGE_CLOCK;    sub = CV_POD; }
    else if (!strcmp(pv, "almanac"))  { pg = PAGE_CALENDAR; sub = 1; }
    else if (!strcmp(pv, "calendar")) { pg = PAGE_CALENDAR; sub = 0; }
    bool ok = pg != 255;
    if (ok && sec > 0) {
      if ((int32_t)(viewUntil - millis()) <= 0) {   // first lock: remember where the user was
        viewSavedCv = clockView; viewSavedAlm = almanacView;
      }
      viewPg = pg; viewSub = sub;
      viewUntil = millis() + (uint32_t)sec * 1000;
    } else {
      viewUntil = 0;                        // s = 0 (or an unknown page): release
    }
    noteActivity();                         // never photograph a dimmed screen
    JsonDocument r;
    r["t"] = "view"; r["id"] = d["id"] | 0; r["p"] = pv; r["ok"] = ok; r["s"] = sec;
    sendJson(r);
  } else if (strcmp(t, "wifi") == 0) {     // Mac settings page: board Wi-Fi list
    wifiHandle(d);
  } else if (strcmp(t, "sess") == 0) {      // Claude 多会话: the seat's session list
    sessHandle(d);
  } else if (strcmp(t, "toast") == 0) {     // host one-liner on the pill 名牌
    // mac_approve: an approve for a session the host cannot reach tab-exactly
    // (app-level terminal with ≥2 sessions) — it typed nothing, the human
    // answers on the Mac. Unknown kinds are ignored (newer host, older board).
    const char* k = d["k"] | "";
    if (!strcmp(k, "mac_approve")) {
      showToast(tr(S_APPROVE_ON_MAC), 2000);
      toastOnBubble = true;
    }
  } else if (strcmp(t, "batt") == 0) {      // debug via /test/raw: fake gauge {"pct":15,"s":60}; pct<0 ends it
    int p = d["pct"] | -1;
    uint32_t s = d["s"] | 30;
    battFakePct = p > 100 ? 100 : p;
    battFakeUntil = millis() + (s > 600 ? 600 : s) * 1000;
    battFakeRepMs = (uint32_t)(d["rep"] | 0) * 1000;
    lowBattWarned = 0;                     // so the faked level shows its 名牌
    Serial.printf("batt fake: %d for %us\n", battFakePct, (unsigned)s);
  } else if (strcmp(t, "nudge") == 0) {     // host debug (/test/nudge): status toast + seat dots for 3 s, for /test/shot
    nudgeUntil = millis() + 3000;
  } else if (strcmp(t, "pin") == 0) {       // host debug (/test/pin/0|1): 钉住
    setPinned((d["on"] | 0) != 0, shownPage);   // pins whatever is on screen, like the key
    JsonDocument r;
    r["t"] = "pin"; r["on"] = pinned ? 1 : 0; r["id"] = d["id"] | 0;
    r["pg"] = pinned ? (pinnedPage == PAGE_CLOCK ? "clock" : pinnedPage == PAGE_CALENDAR ? "almanac" : "face") : "";
    sendJson(r);
  } else if (strcmp(t, "profile") == 0) {   // host debug: show the growth card
    profUntil = millis() + 6000;
    noteActivity();
  } else if (strcmp(t, "skin") == 0) {      // host: skin a seat (default = the shown one)
    int seat = selected;
    const char* ag = d["agent"] | "";        // Mac settings page names the seat
    if (*ag) for (int i = 0; i < N_AGENTS; i++) if (strcmp(ag, AGENTS[i].id) == 0) seat = i;
    bool changed = wearSkinFor(seat, (uint8_t)(d["id"] | 0));   // same swap rule as the card
    if (!changed) sendSkins();               // the page still wants the truth back
    if (seat == selected) profUntil = millis() + 6000;   // show the card so the name is visible
    noteActivity();
  } else if (strcmp(t, "almanac") == 0) {   // daily cyber-almanac from host
    const char* ch = d["ch"] | "#3EE6D2";
    uint32_t v = strtoul(ch[0] == '#' ? ch + 1 : ch, nullptr, 16);
    uint16_t col = (uint16_t)(((v >> 8) & 0xF800) | ((v >> 5) & 0x07E0) |
                              ((v >> 3) & 0x001F));
    almanacAltClear();
    almanacSet(d["gz"] | "", d["sx"] | "", d["jc"] | "", d["date"] | "",
               d["yi"][0] | "", d["yi"][1] | "", d["ji"][0] | "", d["ji"][1] | "",
               d["qian"] | "", d["dir"] | "", d["sig"] | 3, d["cn"] | "", col);
    // 五份黄历 (pages.h): up to four alternates ride along; an old host or the
    // card file of an old host has none and the page simply has nothing to cycle
    for (JsonObjectConst a : d["alt"].as<JsonArrayConst>())
      almanacAltAdd(a["yi"][0] | "", a["yi"][1] | "", a["ji"][0] | "", a["ji"][1] | "",
                    a["qian"] | "");
    almanacCommit();
  } else if (strcmp(t, "report") == 0) {    // daily leaderboard from host
    int w[N_AGENTS] = {0}, dn[N_AGENTS] = {0};
    for (int i = 0; i < N_AGENTS; i++) {
      w[i] = d["w"][i] | 0;
      dn[i] = d["d"][i] | 0;
    }
    sdDayRecord(w, dn, d["force"] | false);   // the on-board 战报 page retired 2026-09-09 (user); the archive stays
  } else if (strcmp(t, "fbeg") == 0) {      // file push: open <path>.part
    int id = d["id"] | 0;
    const char* path = d["path"] | "";
    // A transfer already running owns the card (the host serialises pushes;
    // this is the board-side backstop). Only a transfer that has gone silent
    // for 10 s is dead rather than busy.
    if (xfId >= 0) {
      if ((int32_t)(millis() - xfLastData) < 10000) { xfReply(id, false, "busy"); return; }
      xfAbort("stale");
    }
    xfNull = strcmp(path, "/dev/null") == 0;
    if (xfNull) {                              // pipe benchmark, nothing touches the card
      xfId = id; xfSize = d["size"] | 0u; xfGot = 0; xfSeq = 0; xfT0 = millis();
      xfIters = xfMaxL = xfIterL = xfHandleUs = xfWriteUs = xfGapMs = xfGaps = xfAvailMax = xfWaits = 0;
      xfLastPoll = 0; xfLastData = millis();
      xfSerial = rxFromSerial;
      xfReply(id, true);
      return;
    }
    fs::FS* fs = sdFs();
    if (!fs) { xfReply(id, false, "no sd"); return; }
    if (path[0] != '/' || strlen(path) >= sizeof(xfPath)) { xfReply(id, false, "path"); return; }
    snprintf(xfPath, sizeof(xfPath), "%s", path);
    sdMkdirs(xfPath);
    char tmp[104];
    snprintf(tmp, sizeof(tmp), "%s.part", xfPath);
    fs->remove(tmp);
    xfFile = fs->open(tmp, FILE_WRITE);
    if (!xfFile) { xfReply(id, false, "open"); return; }
    if (!xfBuf) xfBuf = (uint8_t*)heap_caps_malloc(XF_BUF, MALLOC_CAP_SPIRAM);
    if (!xfBuf) { xfFile.close(); xfReply(id, false, "mem"); return; }
    xfId = id; xfSize = d["size"] | 0u;
    xfWant = strtoul(d["crc"] | "0", nullptr, 16);
    xfGot = 0; xfCrc = 0; xfSeq = 0; xfFill = 0; xfT0 = millis();
    xfSerial = rxFromSerial;
    xfIters = xfMaxL = xfIterL = xfHandleUs = xfWriteUs = xfGapMs = xfGaps = xfAvailMax = xfWaits = 0;
    xfLastPoll = 0; xfLastData = millis();
    xfReply(id, true);
  } else if (strcmp(t, "fend") == 0) {      // file push: verify + commit
    int id = d["id"] | 0;
    if (xfId < 0 || id != xfId) { xfReply(id, false, "no transfer"); return; }
    if (xfNull) { xfReply(xfId, true); xfId = -1; xfNull = false; return; }
    bool flushed = xfFlush();
    xfFile.close();
    fs::FS* fs = sdFs();
    char tmp[104];
    snprintf(tmp, sizeof(tmp), "%s.part", xfPath);
    const char* why = nullptr;
    if (!flushed) why = "write";
    else if (xfGot != xfSize) why = "size";
    else if (xfCrc != xfWant) why = "crc";
    else { fs->remove(xfPath); if (!fs->rename(tmp, xfPath)) why = "rename"; }
    if (why) fs->remove(tmp);
    Serial.printf("xfer: %s %lu B %s (%lu ms)\n", xfPath, (unsigned long)xfGot,
                  why ? why : "ok", (unsigned long)(millis() - xfT0));
    xfReply(xfId, why == nullptr, why);
    if (!why) sdFileArrived(xfPath);
    xfId = -1;
  } else if (strcmp(t, "frm") == 0) {       // delete a file or an empty directory
    JsonDocument r;
    r["t"] = "frm"; r["id"] = d["id"] | 0;
    fs::FS* fs = sdFs();
    const char* path = d["path"] | "";
    bool ok = false;
    if (fs && path[0] == '/' && strcmp(path, "/") != 0 && strcmp(path, "/agentpet") != 0) {
      File f = fs->open(path);
      bool dir = f && f.isDirectory();
      if (f) f.close();
      ok = dir ? fs->rmdir(path) : fs->remove(path);
    }
    r["ok"] = ok;
    sendJson(r);
  } else if (strcmp(t, "fls") == 0) {       // list a directory (first 40 entries)
    JsonDocument r;
    r["t"] = "fls"; r["id"] = d["id"] | 0;
    const char* path = d["path"] | "/";
    r["path"] = path;
    JsonArray e = r["e"].to<JsonArray>();
    fs::FS* fs = sdFs();
    File dir = fs ? fs->open(path) : File();
    if (!dir || !dir.isDirectory()) r["why"] = fs ? "not a dir" : "no sd";
    else {
      int n = 0;
      for (File f = dir.openNextFile(); f; f = dir.openNextFile()) {
        if (n++ < 40) {
          JsonArray row = e.add<JsonArray>();
          row.add(f.name()); row.add(f.size()); row.add(f.isDirectory());
        }
        f.close();
      }
      r["n"] = n;
    }
    if (dir) dir.close();
    sendJson(r);
  } else if (strcmp(t, "fcat") == 0) {      // first ≤768 bytes of a file, base64
    JsonDocument r;
    r["t"] = "fcat"; r["id"] = d["id"] | 0;
    fs::FS* fs = sdFs();
    const char* path = d["path"] | "";
    File f = fs ? fs->open(path, FILE_READ) : File();
    if (!f) r["why"] = fs ? "open" : "no sd";
    else {
      static uint8_t raw[768];
      size_t n = f.read(raw, sizeof(raw));
      r["size"] = f.size();
      f.close();
      static char b64[1040];
      size_t olen = 0;
      mbedtls_base64_encode((unsigned char*)b64, sizeof(b64), &olen, raw, n);
      b64[olen] = 0;
      r["d"] = b64;
    }
    sendJson(r);
  } else if (strcmp(t, "shot") == 0) {      // screenshot of the canvas -> host (TCP)
    sendShot(d["id"] | 0, d["s"] | 2);
  } else if (strcmp(t, "pmu") == 0) {       // debug 2026-09-19: raw AXP2101 register peek/poke over USB serial
    JsonDocument r;
    r["t"] = "pmu";
    int reg = d["reg"] | -1;
    if (reg >= 0 && reg <= 0xFF) {
      if (d["val"].is<int>()) pmu.writeRegister((uint8_t)reg, (uint8_t)(d["val"].as<int>()));
      r["reg"] = reg; r["val"] = pmu.readRegister((uint8_t)reg);
    }
    r["r22"] = pmu.readRegister(0x22); r["r25"] = pmu.readRegister(0x25); r["r27"] = pmu.readRegister(0x27);
    sendJson(r);
  } else if (strcmp(t, "btn") == 0) {       // debug 2026-09-19: raw key levels + arming
    JsonDocument r;
    r["t"] = "btn";
    r["pwr"] = digitalRead(PIN_KEY_PWR); r["pwr_armed"] = btnPwrArmed(); r["pwr_down"] = btnPwrDown();
    r["io18"] = digitalRead(PIN_KEY_IO18); r["boot"] = digitalRead(PIN_KEY_BOOT);
    r["pek_edges"] = pmuKeyEdges; r["irq_last"] = (uint32_t)pmuIrqLast;
    r["irq_polls"] = pmuIrqPolls; r["irq_now"] = (uint32_t)pmu.getIrqStatus();
    sendJson(r);
  } else if (strcmp(t, "ping") == 0) {      // host RTT probe (/test/rtt): answer on the same link
    JsonDocument r;
    r["t"] = "pong"; r["id"] = d["id"] | 0;
    sendJson(r);
  } else if (strcmp(t, "bletest") == 0) {   // BLE bandwidth probe, see bleTestTask
    JsonDocument r;
    BleLinkInfo li = bleLinkInfo();
    r["t"] = "btack"; r["busy"] = bt.run; r["mtu"] = li.mtu; r["itvl_ms"] = li.itvl * 1.25f;
    sendJson(r);                             // ack first: the flood must not splice into it
    bleTestStart(d["sec"] | 3, d["len"] | 470, d["chunk"] | 0, d["gap"] | 0, d["itvl"] | 0);
  } else if (strcmp(t, "mic") == 0) {       // host mic test: {"on":0|1} stream, {"ch":0|1|2} slot pick
    if (d["ch"].is<int>()) {                     // 0 MIC1 / 1 MIC2 / 2 average; 3 = back to auto by placement
      int c = d["ch"].as<int>();
      micChManual = c >= 0 && c <= 2;
      audioMicChannel(micChManual ? (uint8_t)c : micAutoCh());
    }
    if (d["link"].is<const char*>()) {             // test override, NVS-free: gone on reboot
      const char* l = d["link"];
      micLinkForce = strcmp(l, "ble") == 0 ? MICLINK_BLE : strcmp(l, "tcp") == 0 ? MICLINK_TCP : MICLINK_NONE;
    }
    if (d["on"].is<int>()) {
      if (d["on"].as<int>()) micSessionStart(); else audioMicStop();
    }
    JsonDocument r;
    r["t"] = "mic"; r["ok"] = audioMicAvailable(); r["on"] = audioMicActive();
    r["ch"] = audioMicStats().ch; r["tcp"] = sock.connected(); r["ble"] = bleConnected(); r["rssi"] = WiFi.RSSI();
    r["link"] = micLink == MICLINK_BLE ? "ble" : micLink == MICLINK_TCP ? "tcp" : "none";
    r["force"] = micLinkForce == MICLINK_BLE ? "ble" : micLinkForce == MICLINK_TCP ? "tcp" : "auto";
    sendJson(r);
  } else if (strcmp(t, "gaze") == 0) {      // 眼神追声: query / on-off / force / tune / dbg
    if (d["on"].is<int>()) {
      gazeOn = d["on"].as<int>() != 0;
      audioGazeEnable(gazeOn);
      growPrefs.putUChar("gaze", gazeOn ? 1 : 0);
      Serial.printf("gaze: %s\n", gazeOn ? "on" : "off");
    }
    if (d["az"].is<float>()) {                 // forced look (screenshots): az -1..1 for ms
      gazeForceAz = d["az"].as<float>();
      gazeForceUntil = millis() + (uint32_t)(d["ms"] | 3000);
    }
    if (d["min"].is<int>() || d["minc"].is<float>())
      audioGazeTune(d["min"] | -1, d["minc"] | -1.0f);
    if (d["dbg"].is<int>()) audioGazeDebug(d["dbg"].as<int>() != 0);
    GazeEst g = audioGaze();
    JsonDocument r;
    r["t"] = "gaze";
    if (d["id"].is<int>()) r["id"] = d["id"].as<int>();
    r["on"] = gazeOn; r["az"] = g.az; r["conf"] = g.conf; r["lag"] = g.lag;
    r["rms"] = g.rms; r["floor"] = g.floor; r["n"] = g.n;
    r["age"] = g.n ? (int32_t)(millis() - g.ms) : -1;
    r["look"] = lookAz; r["force"] = (int32_t)(gazeForceUntil - millis()) > 0;
    r["spk"] = audioOutputActive();
    {   // last confident frames, newest first: [age_ms, az, rms] — clap left, then right, then read this
      GazeSample hs[24];
      int hn = audioGazeHist(hs, 24);
      JsonArray h = r["hist"].to<JsonArray>();
      for (int i = 0; i < hn; i++) {
        JsonArray e = h.add<JsonArray>();
        e.add((int32_t)(millis() - hs[i].ms)); e.add((int)lroundf(hs[i].az * 100)); e.add(hs[i].rms);
      }
    }
    sendJson(r);
  } else if (strcmp(t, "sd?") == 0) {       // host asks about the card: (re)try + report
    bool ok = sdInit(true);          // re-verifies a mounted card, deep-probes a silent slot
    wdtFeed();                       // deep probe = SPI retries with delay(50)
    JsonDocument r;
    r["t"] = "sd"; r["ok"] = ok;
    r["mb"] = sdInfo().sizeMB; r["rw"] = sdInfo().rwOk;
    r["used"] = sdInfo().usedMB; r["boots"] = sdInfo().boots;
    if (ok) r["via"] = sdInfo().mmc ? "sdmmc" : "spi";
    if (!ok) r["r1"] = sdLastR1();
    sendJson(r);
  } else if (strcmp(t, "ota") == 0) {       // SD-OTA: flash <path> from the card, or roll back
    int id = d["id"] | 0;
    if (d["rollback"] | false) otaRollback(id);
    else otaRun(id, d["path"] | "", d["size"] | 0u, strtoul(d["crc"] | "0", nullptr, 16));
  } else if (strcmp(t, "rtc?") == 0) {      // host asks the hardware clock (/test/rtc)
    const RtcInfo& ri = rtcInfo();
    bool integ = false;
    uint32_t chip = rtcRead(&integ);
    JsonDocument r;
    r["t"] = "rtc"; r["ok"] = ri.present; r["integ"] = integ;
    r["chip"] = chip; r["clock"] = clockEpoch(); r["boot"] = ri.bootEpoch;
    r["seeded"] = ri.seeded; r["drift"] = ri.lastDrift;
    r["syncs"] = ri.syncs; r["writes"] = ri.writes; r["up"] = millis() / 1000;
    sendJson(r);
  } else if (strcmp(t, "cfg") == 0) {       // host-pushed preferences
    if (d["mic_link"].is<const char*>()) {   // dictation link preference: ble (default) | tcp | auto
      const char* l = d["mic_link"];
      micLinkForce = strcmp(l, "tcp") == 0 ? MICLINK_TCP : strcmp(l, "ble") == 0 ? MICLINK_BLE : MICLINK_NONE;
      Serial.printf("cfg: mic link -> %s\n", l);
    }
    const char* p = d["pickup"] | "";
    uint8_t v = 255;
    if      (!strcmp(p, "follow"))  v = BIND_FOLLOW;
    else if (!strcmp(p, "face"))    v = BIND_FACE;
    else if (!strcmp(p, "clock"))   v = BIND_CLOCK;
    else if (!strcmp(p, "report"))  v = BIND_CLOCK;     // 战报页已下线：老配置落到钟表
    else if (!strcmp(p, "almanac")) v = BIND_ALMANAC;
    else if (!strcmp(p, "smart"))   v = BIND_SMART;
    if (v != 255 && v != pickupBind) {
      pickupBind = v;
      growPrefs.putUChar("pickup", v);
      Serial.printf("cfg: pickup binding -> %s\n", p);
    }
    if (d["mic"].is<int>()) {          // voice_source "board": right key streams the mic too
      micOnTalk = d["mic"].as<int>() != 0;
      Serial.printf("cfg: mic on talk -> %d (es7210 %s)\n", micOnTalk, audioMicAvailable() ? "up" : "absent");
    }
    const char* vs = d["voice"] | "";
    if (*vs) {
      bool tts = strcmp(vs, "tts") == 0;
      if (tts != voiceTts) {
        voiceTts = tts;
        growPrefs.putUChar("vtts", tts ? 1 : 0);
        Serial.printf("cfg: voice style -> %s\n", vs);
      }
    }
    {   // 「显示离线席位」: 1 = 永远五席, 0 = 隐藏当前 off 的 (default).
        // A missing key changes nothing, so an older host never resets it.
        // bool accepted next to int: JSON true/false must not read as absent.
      JsonVariantConst so = d["show_off"];
      if (so.is<int>() || so.is<bool>()) {
        bool on = so.as<bool>();
        if (on != showOffSeats) {
          showOffSeats = on;
          growPrefs.putUChar("showoff", on ? 1 : 0);
          // turning it back off re-applies the hiding rules right away: the
          // seat under us may have quit while they were suspended
          autoReselectIfOff();
          Serial.printf("cfg: show off seats -> %d\n", on ? 1 : 0);
        }
      }
    }
    {   // 语言两档: "zh" | "en". A missing key changes nothing, so an
        // older host never resets it. Every page reads tr() per frame, so the
        // next frame is already in the new language.
      const char* lg = d["lang"] | "";
      uint8_t want = !strcmp(lg, "en") ? LANG_EN : !strcmp(lg, "zh") ? LANG_ZH : 255;
      if (want != 255 && want != g_lang) {
        g_lang = want;
        growPrefs.putUChar("lang", want);
        almanacView = !langEn();           // re-land the almanac orientation
        noteActivity();
        Serial.printf("cfg: lang -> %s\n", lg);
      }
    }
  } else if (strcmp(t, "time") == 0) {
    uint32_t epoch = d["epoch"] | 0u;  // host sends UTC + its utc offset
    int32_t  off   = d["off"] | 0;
    if (epoch) { clockSet(epoch + off); rtcHostSync(epoch + off); }
  } else if (strcmp(t, "demo") == 0) {
    demoStart = millis();              // 8-face tour, or one face via "face"
    demoFace = -1;
    noteActivity();
    static const char* names[14] = {"working", "needs_you", "done", "idle",
                                    "off", "listening", "offline", "surprised",
                                    "dizzy", "petting", "eating", "burp",
                                    "bored", "stretch"};
    const char* face = d["face"] | "";
    for (int i = 0; i < 14; i++)
      if (strcmp(face, names[i]) == 0) demoFace = i;
  }
  // "pong" and anything else just refresh lastRx
}

static void sdFileArrived(const char* path) {
  if (sdFontIsFontPath(path)) {                  // fresh font file: re-index (~0.4 s)
    sdFontLoadAll(); wdtFeed();
    sessRelabel();                               // titles may now be drawable
  }
  if (strncmp(path, "/agentpet/audio/", 16) == 0) playerRescan();   // new episode on the card
  if (strcmp(path, npMissAsked) == 0) npMissAsked[0] = 0;   // the cover we asked for landed
  coverForget(path);                             // ...so stop showing the placeholder for it
}

// RTC 解锁的第一个功能: with the date known at boot, the board pulls today's
// almanac from the year file the host keeps on the card
// (/agentpet/almanac/<year>.jsonl, one complete {"t":"almanac"} message per
// line — host almanac_year_file) and runs it through handleLine as if the
// host had pushed it. A later host push carries the same data and just wins.
static bool almanacFromCard(uint32_t localEpoch) {
  fs::FS* fs = sdFs();
  if (!fs || !localEpoch) return false;
  time_t tt = (time_t)localEpoch;
  struct tm tmv;
  gmtime_r(&tt, &tmv);                     // local seconds -> fields
  char path[40], k1[24], k2[24];
  snprintf(path, sizeof(path), "/agentpet/almanac/%04d.jsonl", tmv.tm_year + 1900);
  snprintf(k1, sizeof(k1), "\"date\": \"%02d.%02d\"", tmv.tm_mon + 1, tmv.tm_mday);
  snprintf(k2, sizeof(k2), "\"date\":\"%02d.%02d\"", tmv.tm_mon + 1, tmv.tm_mday);
  File f = fs->open(path, FILE_READ);
  if (!f) { Serial.printf("almanac: no %s on card\n", path); return false; }
  bool hit = false;
  uint32_t t0 = millis();
  // One block read into PSRAM, then a plain strstr: line-by-line
  // readStringUntil went through the VFS a byte at a time (928 ms for the
  // 85 KB up to September); this way is ~150 ms.
  size_t sz = f.size();
  char* all = (char*)ps_malloc(sz + 1);
  if (all) {
    size_t n = f.readBytes(all, sz);
    all[n] = 0;
    char* p = strstr(all, k1);
    if (!p) p = strstr(all, k2);
    if (p) {
      char* ls = p;
      while (ls > all && ls[-1] != '\n') ls--;
      char* le = strchr(p, '\n');
      if (le) *le = 0;
      handleLine(String(ls));
      hit = true;
    }
    free(all);
  }
  f.close();
  Serial.printf("almanac: %s %02d.%02d from card (%lu ms)\n",
                hit ? "loaded" : "no line for", tmv.tm_mon + 1, tmv.tm_mday,
                (unsigned long)(millis() - t0));
  return hit;
}

// Longest host->board line any channel accepts (fdat/pcm lines are ~1.4 KB).
// A longer line is dropped WHOLE and logged — the old 2048 cap silently cut
// the head off and handed the fragment to handleLine.
static const size_t RX_LINE_MAX = 4096;
static String rxBuf;
static bool   rxOverflow = false;     // current TCP line already too long
static void netPoll() {
  uint32_t now = millis();

  if (WiFi.status() != WL_CONNECTED) { hostUp = false; return; }

  if (!sock.connected()) {
    if (hostUp) Serial.println("net: link dropped");
    hostUp = false;
    static uint32_t lastTry = 0;
    if (now - lastTry < 3000) return;
    lastTry = now;
    sock.stop();
    // Owner Mac when there is one, else the config.local.h Mac.
    IPAddress ip;
    bool owned = ownerId.length() > 0;
    if (!owned && !HOST_FALLBACK_IP[0] && !HOST_MDNS_NAME[0]) return;   // public build, not claimed yet
    ip.fromString(owned ? ownerIp.c_str() : HOST_FALLBACK_IP);
    const char* mdns = owned ? ownerMdns.c_str() : HOST_MDNS_NAME;
    uint16_t port = owned ? ownerPort : HOST_PORT;
    if (*mdns) {
      IPAddress m = MDNS.queryHost(mdns, 1500);
      if (m != IPAddress()) ip = m;
    }
    if (ip == IPAddress()) return;       // owner never told us an address yet
    Serial.printf("net: connecting %s:%d\n", ip.toString().c_str(), port);
    if (!sock.connect(ip, port)) { Serial.println("net: connect failed"); return; }
    sock.setNoDelay(true);
    rxBuf = ""; rxOverflow = false;
    lastRx = now;
    JsonDocument d;
    d["t"] = "hello"; d["dev"] = "amoled216"; d["v"] = 2;   // v2 = carries 遥测
    d["up"] = uptimeSec();
    d["rst"] = resetReasonName(esp_reset_reason());         // why we last booted
    // Which seat we woke up on. A reboot resets `selected` to 0 while the
    // host still holds the seat it followed to before the OTA, and the first
    // dictation/approval after the reboot then lands in the wrong app until
    // the user swipes. The hello carries the truth so the host can realign.
    d["sel"] = AGENTS[selected].id;
    d["sd"] = sdInfo().mounted ? sdInfo().sizeMB : 0; d["sdrw"] = sdInfo().rwOk;
    d["rtc"] = rtcInfo().bootEpoch;      // 0 = chip had no usable time at boot
    d["build"] = FW_BUILD; d["part"] = esp_ota_get_running_partition()->label;
    d["owner"] = ownerName;
    addSkins(d);                                            // who wears what
    sendJson(d);
    Serial.println("net: hello sent");
    return;
  }

  {   // drain in blocks, ≤30 ms per frame so a file push keeps the face alive
    static uint8_t nb[1460];
    uint32_t t0 = millis();
    // During a file push the face drops to ~4 fps: we sit here up to 250 ms
    // streaming, because every frame drawn is a window's worth of stall for
    // the sender (lwIP rx window is only ~5.7 KB).
    int budget = xfId >= 0 ? 250 : 30;
    if (xfId >= 0) {
      if (xfLastPoll) { xfGapMs += millis() - xfLastPoll; xfGaps++; }
      xfLastPoll = millis();
      xfIterL = 0;
      int av = sock.available();
      if ((uint32_t)av > xfAvailMax) xfAvailMax = av;
      if ((int32_t)(millis() - xfLastData) > 5000) xfAbort("idle");
    }
    while ((int32_t)(millis() - t0) < budget) {
      if (!sock.available()) {
        if (xfId < 0) break;
        xfWaits++;
        delay(1);                          // mid-push: wait for the next segment
        continue;
      }
      int n = sock.read(nb, sizeof(nb));
      if (n <= 0) break;
      int start = 0;
      for (int i = 0; i < n; i++) {
        if (nb[i] != '\n') continue;
        if (!rxOverflow && rxBuf.length() + (i - start) <= RX_LINE_MAX)
          rxBuf.concat((const char*)nb + start, i - start);
        else
          rxOverflow = true;
        if (rxOverflow) Serial.printf("net: rx line > %u bytes dropped\n", (unsigned)RX_LINE_MAX);
        else            handleLine(rxBuf);
        rxBuf = ""; rxOverflow = false;
        start = i + 1;
      }
      if (start < n) {
        if (!rxOverflow && rxBuf.length() + (n - start) <= RX_LINE_MAX)
          rxBuf.concat((const char*)nb + start, n - start);
        else { rxOverflow = true; rxBuf = ""; }
      }
    }
    if (xfId >= 0 && xfIterL) { xfIters++; if (xfIterL > xfMaxL) xfMaxL = xfIterL; }
  }
  // signed diff: lastRx may be a few ms NEWER than `now` (set inside the
  // read loop above) — unsigned subtraction would underflow to ~4e9.
  if ((int32_t)(millis() - lastRx) > 35000) {
    Serial.println("net: rx timeout, reconnecting");
    sock.stop(); hostUp = false; return;
  }
  if (!hostUp) Serial.println("net: host link up");
  hostUp = true;
}

// ---------- Wi-Fi portable backoff ----------
// Away from home the stock auto-reconnect scans for the home network forever at the
// driver level, burning tens of mA for nothing while BLE carries the link.
// After a fruitless grace period the radio is switched OFF and only probes
// periodically; any successful association resets to normal home behavior.
static uint32_t wifiOkAt = 0;        // last time WL_CONNECTED was seen
static uint32_t wifiProbeAt = 0;     // park time / current probe window start
static bool     wifiParked = false;  // radio off between probes
static bool     mdnsRedo = false;    // re-arm mDNS after a radio-off episode

static void wifiPoll() {
  const int32_t GRACE_MS = 3 * 60000;   // stock retries before first park
  const int32_t PROBE_MS = 20000;       // how long each probe keeps trying
  uint32_t now = millis();
  if (wifiScanning) {
    int n = WiFi.scanComplete();
    if (n == WIFI_SCAN_RUNNING && (int32_t)(now - wifiScanAt) < 15000) return;
    wifiScanning = false;
    for (int i = 0; i <= WIFI_NETS_MAX; i++) wifiSeen[i] = 0;
    int best = -2, bestRssi = -1000;              // -1 = factory entry
    wifiNearN = 0;
    for (int k = 0; k < n; k++) {
      String sid = WiFi.SSID(k);
      int rs = WiFi.RSSI(k);
      if (sid.length()) {                         // strongest-first, one row per name
        int at = -1;
        for (int i = 0; i < wifiNearN; i++) if (wifiNear[i] == sid) at = i;
        if (at < 0 && wifiNearN < WIFI_NEAR_MAX) { at = wifiNearN++; wifiNear[at] = sid; wifiNearRssi[at] = -128; }
        if (at >= 0 && rs > wifiNearRssi[at]) {
          wifiNearRssi[at] = rs;
          while (at > 0 && wifiNearRssi[at] > wifiNearRssi[at - 1]) {
            String ts = wifiNear[at]; wifiNear[at] = wifiNear[at - 1]; wifiNear[at - 1] = ts;
            int16_t tr = wifiNearRssi[at]; wifiNearRssi[at] = wifiNearRssi[at - 1]; wifiNearRssi[at - 1] = tr;
            at--;
          }
        }
      }
      int hit = -2;
      if (kFactoryWifi && sid == WIFI_SSID) hit = -1;   // hidden nets have "" names
      for (int i = 0; i < wifiN; i++) if (wifiSsid[i] == sid) hit = i;
      if (hit == -2) continue;
      int16_t& seen = wifiSeen[hit < 0 ? WIFI_NETS_MAX : hit];
      if (!seen || rs > seen) seen = rs;
      if (rs > bestRssi) { bestRssi = rs; best = hit; }
    }
    WiFi.scanDelete();
    Serial.printf("wifi: scan %d nets, best known %s (%d dBm)\n", n,
                  best == -2 ? "-" : best < 0 ? WIFI_SSID : wifiSsid[best].c_str(), best == -2 ? 0 : bestRssi);
    if (best != -2 && WiFi.status() != WL_CONNECTED) {
      if (best < 0) WiFi.begin(WIFI_SSID, WIFI_PASS);
      else WiFi.begin(wifiSsid[best].c_str(), wifiPass[best].c_str());
    }
    if (wifiReplyId >= 0) { wifiReply(wifiReplyId, true, nullptr); wifiReplyId = -1; }
    return;
  }
  if (WiFi.status() == WL_CONNECTED) {
    wifiOkAt = now;
    wifiParked = false;      // an add from the Mac can bring the radio up mid-park
    if (mdnsRedo) {          // parked: mDNS was stopped with the radio (see below)
      mdnsRedo = false;      // (netPoll falls back to the fixed IP anyway)
      MDNS.end();
      MDNS.begin("agentpet");
    }
    return;
  }
  if (!wifiParked && (int32_t)(now - wifiScanAt) > 30000 &&
      (int32_t)(now - wifiOkAt) < 3 * 60000) {
    wifiStartScan();          // moved house? the driver keeps retrying the old SSID
    return;
  }
  if (!wifiParked) {
    // grace expired and the latest probe window came up empty -> park
    if ((int32_t)(now - wifiOkAt) > GRACE_MS &&
        (int32_t)(now - wifiProbeAt) > PROBE_MS) {
      wifiParked = true;
      wifiProbeAt = now;
      mdnsRedo = true;
      // mDNS must be down before the radio: WIFI_OFF destroys the STA
      // esp_netif, while mdns 1.6.0 keeps a cached pointer to it and only
      // clears that on STA_DISCONNECTED. A board that never joined anything
      // since boot never gets one, so the first GOT_IP after a park had the
      // mdns task join multicast on the freed netif: LoadProhibited in
      // esp_netif_is_netif_up (board 2, 2026-09-27, first network added from
      // the settings page). The WL_CONNECTED branch above starts it again,
      // and mdns_init re-reads the netif fresh.
      MDNS.end();
      WiFi.mode(WIFI_OFF);
      Serial.println("wifi: parked (AP not found; BLE carries the link)");
    }
  } else {
    // BLE up: relaxed probing. Both links down: Wi-Fi is the only way back
    // to the host, so probe more eagerly.
    int32_t every = bleConnected() ? 5 * 60000 : 2 * 60000;
    if ((int32_t)(now - wifiProbeAt) > every) {
      wifiParked = false;
      wifiProbeAt = now;
      WiFi.mode(WIFI_STA);
      WiFi.setSleep(false);
      wifiStartScan();
      Serial.println("wifi: probing for a known AP");
    }
  }
}

// Clean power-down: goodbye chirp + closed eyes, then the PMU cuts power.
// Shared by the PWR long-press and the low-battery guard (the AXP2101 would
// hard-cut on its own eventually; going out gracefully spares the cell).
static void powerOff(const char* msg) {
  // No loop pass will ever happen again from here. On battery pmu.shutdown()
  // kills the rail mid-sentence, but on USB power VBUS keeps the AXP2101 up
  // and we simply park in the while(true) below — the task watchdog would
  // then "rescue" us into a reboot ~15 s after the goodbye. Unsubscribe first
  // so the board just sits there dark, which is what the user asked for.
  wdtLeave();
  playerSaveState();               // where the episode got to, before the rail dies
  audioPlay(SND_BYE);              // plays during the animation delay below
  canvas->fillScreen(0);
  canvas->fillRoundRect(103, 200, 110, 10, 5, 0xFFFF);   // closed eyes
  canvas->fillRoundRect(267, 200, 110, 10, 5, 0xFFFF);
  canvas->setTextSize(3);
  canvas->setTextColor(0x7BEF);
  canvas->setCursor(240 - (int)strlen(msg) * 9, 300);
  canvas->print(msg);
  canvas->flush();
  delay(800);
  gfx->setBrightness(0);
  pmu.shutdown();          // PMU cuts power (battery mode); PWR key wakes it
  while (true) delay(1000);
}

// ---------- buttons ----------
// `armed` = this key has been seen released at least once since boot. The
// middle key starts unarmed: it is the PWR key, so the board is powered up
// BY holding it, and the finger is still on the glass when the first loop
// pass runs — that first "press" is the power-on, not a command, and the
// middle key is now hold-to-talk (it would open a dictation every boot).
// Chosen over "spin in setup() until GPIO16 reads released": setup() must
// not be held hostage by a finger (Wi-Fi, BLE and the boot animation are
// behind it, and a stuck key would hang the board forever), whereas the
// flag costs one bool and cannot deadlock. Left and right keys are armed
// from the start — nothing holds them down at power-on.
struct Btn { uint8_t pin; bool activeHigh; bool down; uint32_t at;
             bool pressed; bool released; bool armed; };
static Btn bPwr  = {PIN_KEY_PWR,  true,  false, 0, false, false, false};
static Btn bIo18 = {PIN_KEY_IO18, false, false, 0, false, false, true};
static Btn bBoot = {PIN_KEY_BOOT, false, false, 0, false, false, true};
static bool btnPwrArmed() { return bPwr.armed; }
static bool btnPwrDown()  { return bPwr.down; }

// Middle key via the AXP2101's PWRON edge IRQs (see the PMU block in setup):
// one I2C status read per loop pass, then clear. A release with no press on
// record (the finger that powered us on letting go) is ignored, which is all
// the arming the middle key needs on this path.
static void pmuKeyScan(Btn& b) {
  b.pressed = b.released = false;
  if (!pmuOk) return;
  uint64_t st = pmu.getIrqStatus();
  pmuIrqPolls++;
  if (st == 0) return;
  pmuIrqLast = st;
  bool neg = pmu.isPekeyNegativeIrq();     // PWRON pulled low = pressed
  bool pos = pmu.isPekeyPositiveIrq();     // released
  pmu.clearIrqStatus();
  if (neg && !b.down) { b.pressed = true; b.down = true; b.at = millis(); }
  if (pos && b.down)  { b.released = true; b.down = false; }
  if (b.pressed || b.released) {
    pmuKeyEdges++;
    Serial.printf("btn: pwr(pmu) %s\n", b.pressed ? "down" : "up");
  }
}

static void btnScan(Btn& b) {
  bool d = digitalRead(b.pin) == (b.activeHigh ? HIGH : LOW);
  if (!b.armed) {                  // swallow the power-on hold, edges and all
    b.pressed = b.released = false;
    b.down = false;                // so the release that arms us is not an edge
    if (d) return;
    b.armed = true;
    return;
  }
  b.pressed  = d && !b.down;
  b.released = !d && b.down;
  if (b.pressed) b.at = millis();
  if (b.pressed || b.released)     // debug 2026-09-19: key edges on the serial log
    Serial.printf("btn: pin %d %s\n", b.pin, b.pressed ? "down" : "up");
  b.down = d;
}

// ---------- IMU pickup detection ----------
// declared up here because imuPoll consults it: a finger on the glass means
// the jolt came from the touch, not from a hand lifting the board
static bool tpWasDown = false;
static float lastAx = 0, lastAy = 0, lastAz = 1, motion = 0;
static bool  pickedUp = false;
static bool  held = false;          // stricter in-hand signal (page binding)
static uint32_t heldCandAt = 0;
static uint8_t  gravSector = 0;     // live gravity sector, all 4 (held rotation)
static uint32_t stillSince = 0, lastImu = 0;

static void imuPoll() {
  if (!imuOk) return;
  uint32_t now = millis();
  if (now - lastImu < 40) return;   // 25 Hz
  lastImu = now;
  float ax, ay, az;
  IMUdata d;
  if (!qmi.getAccelerometer(d.x, d.y, d.z)) return;
  ax = d.x; ay = d.y; az = d.z;
  float delta = fabsf(ax - lastAx) + fabsf(ay - lastAy) + fabsf(az - lastAz);
  lastAx = ax; lastAy = ay; lastAz = az;
  motion = motion * 0.75f + delta * 0.25f;

  // gravity low-pass -> placement orientation (90° sectors, 700 ms stable;
  // near-flat placements keep the last orientation)
  static float gx = 0, gy = 0, gz = 1;
  static uint8_t cand = 0;
  static uint32_t candAt = 0;
  gx = gx * 0.9f + ax * 0.1f;
  gy = gy * 0.9f + ay * 0.1f;
  gz = gz * 0.9f + az * 0.1f;
  if (fabsf(gz) < 0.75f && (fabsf(gx) > 0.45f || fabsf(gy) > 0.45f)) {
    // signs verified on device 2026-08-27: upright = gy < 0
    uint8_t o = fabsf(gx) > fabsf(gy) ? (gx > 0 ? 3 : 1)
                                      : (gy > 0 ? 0 : 2);
    gravSector = o;      // live estimate incl. sector 2 (held-page rotation)
    if (o != 2) {   // upside-down: no way to place it, keep last page
      if (o != cand) { cand = o; candAt = now; }
      else if (o != orient &&
               (int32_t)(now - candAt) > (pickedUp ? 900 : 700)) {
        // doc/06: deliberate turns flip pages even mid-hold — jostle
        // protection is the stability window (stricter in hand), not the
        // old blanket freeze. A turn is also a newer intent than a shake,
        // so it dismisses a summoned face.
        orient = o;
        if (pickedUp) summonFace = false;
        Serial.printf("orient: %d (gx=%.2f gy=%.2f gz=%.2f)\n", o, gx, gy, gz);
        noteActivity();
      }
    }
  }

  // face-down = sleep: screen dark, chirps hushed, no focus stealing.
  // Face-up rests at gz ≈ +1 (the gravity filter boots with gz = 1); if the
  // panel reports the other sign, flip FLIP_DOWN_GZ. 800 ms of steady
  // face-down so handling the board doesn't false-trigger.
  {
    const float FLIP_DOWN_GZ = -0.75f;
    static uint32_t downAt = 0, sleepMoveAt = 0;
    bool fd = gz < FLIP_DOWN_GZ;
    if (!fd) downAt = 0;
    else if (!downAt) downAt = now;
    // pickup-wake (user request day 9): sustained motion while asleep =
    // someone is holding it. Same strict criterion as the grip confirm — a
    // desk bump decays in ~0.2 s and never wakes it, a carrying hand does
    // within half a second. Sleep-orientation-only used to strand the pet
    // dark in hand until it was tilted past face-up.
    bool grabbed = false;
    if (flipped && motion > 0.08f) {
      if (!sleepMoveAt) sleepMoveAt = now;
      else if ((int32_t)(now - sleepMoveAt) > 500) grabbed = true;
    } else sleepMoveAt = 0;
    if (!flipped && fd && (int32_t)(now - downAt) > 800) {
      flipped = true;
      audioSuppress(true);
      playerPause();                     // 扣桌 = 暂停并记断点
      pickedUp = false;
      surprisedUntil = 0;
      Serial.println("flip: face down, going to sleep");
    } else if (flipped && (gz > 0.5f || grabbed)) {
      flipped = false;
      audioSuppress(false);
      audioPlay(SND_SURPRISE);           // waking gasp
      surprisedUntil = now + 700;
      nudgeUntil = now + 3000;
      noteActivity();
      Serial.println(grabbed ? "flip: picked up, awake"
                             : "flip: face up, awake");
    }
  }

  // shake -> dizzy: several DISTINCT strong jolts in a burst. Spikes must be
  // ≥150 ms apart — at 25 Hz one brisk pickup sweep kept delta high across
  // 3+ consecutive samples and used to count as a full shake (doc/06 fix);
  // now only a real back-and-forth passes. A single bump stays the pickup
  // surprise; the gasp then the wobble reads as a nice sequence.
  {
    static uint8_t  nSpikes = 0;
    static uint32_t firstSpike = 0, lastSpikeAt = 0, lastDizzy = 0;
    if (delta > 0.70f && !flipped) {
      if (nSpikes == 0 || (int32_t)(now - firstSpike) > 1200) {
        nSpikes = 1;
        firstSpike = lastSpikeAt = now;
      } else if ((int32_t)(now - lastSpikeAt) > 150) {
        lastSpikeAt = now;
        if (++nSpikes >= 3) {
          nSpikes = 0;
          // shake while in hand = summon the pet (any binding mode); a later
          // deliberate turn dismisses it (last intent wins, doc/06). It
          // arrives dizzy — "you shook me!" — for free.
          if (pickedUp) summonFace = true;
          if ((int32_t)(now - lastDizzy) > 15000) {
            lastDizzy = now;
            dizzyUntil = now + 2600;
            audioPlay(SND_DIZZY);
            noteActivity();
          }
        }
      }
    }
  }

  if (flipped) return;                   // asleep: no pickup theatrics

  // a finger on the glass means the jolt is the user's own touch (a firm
  // swipe rocks the light board past 0.30 and used to fake a pickup, eating
  // the NEXT swipe behind the !pickedUp gates for 1.5 s — 2026-09-01 fix)
  if (!pickedUp && motion > 0.30f && !tpWasDown) {
    pickedUp = true;
    surprisedUntil = now + 700;
    noteActivity();
    static uint32_t lastGasp = 0;   // don't chatter while being carried
    if ((int32_t)(now - lastGasp) > 15000) {
      lastGasp = now;
      audioPlay(SND_SURPRISE);
    }
  }
  if (pickedUp) {
    if (motion < 0.05f) {
      if (!stillSince) stillSince = now;
      if (now - stillSince > 1500) {
        pickedUp = false;
        nudgeUntil = now + 3000;   // status toast lingers after putdown
      }
    } else stillSince = 0;
  }

  // stricter "in hand" (drives the pickup page binding): the pickup
  // spike must be FOLLOWED by ≥0.5 s of sustained motion — a desk bump
  // decays in ~0.2 s, a holding hand never goes quiet. The surprise face
  // keeps the twitchy pickedUp so bumps still get a cute gasp.
  if (!pickedUp) {
    held = false;
    heldCandAt = 0;
    summonFace = false;
  } else if (!held) {
    if (motion > 0.08f) {
      if (!heldCandAt) heldCandAt = now;
      else if ((int32_t)(now - heldCandAt) > 500) held = true;
    } else heldCandAt = 0;
  }
}

// ---------- touch ----------
static volatile bool tpIrq = false;
static void IRAM_ATTR onTpIrq() { tpIrq = true; }
// tpWasDown lives above the IMU section (pickup suppression reads it)

static int tpStartX = 0, tpStartY = 0;   // press-down point
static int tpLastX  = 0, tpLastY  = 0;   // latest point while held
static uint32_t tpDownAt = 0;            // press-down time (long-press actions)
static bool tpConsumed = false;          // hold already acted; release is a no-op
static int8_t  petDir     = 0;           // dominant-axis stroke dir while held (±1 x, ±2 y)
static uint8_t petStrokes = 0;           // direction reversals in this touch

// Real-world petting is short back-and-forth strokes WITH finger lifts, so a
// single stroke is indistinguishable from a swipe at lift-off. Swipe actions
// therefore run ~300 ms deferred: a returning finger that reverses direction
// reveals it was petting (deferred action cancelled); a same-direction
// follow-up fires it early, so deliberate multi-swipes stay snappy.
static int8_t   pendH = 0, pendV = 0;    // deferred swipe, sign = direction
static uint32_t pendAt = 0;

static void execPendingSwipe() {
  if (pendH) {
    selected = nextSeat(selected, pendH < 0 ? +1 : -1);   // off seats skipped
    sendEvent("select", "agent", AGENTS[selected].id);
    audioPlay(SND_SELECT);
  } else if (pendV) {
    audioSetVolume(audioVolume() + (pendV > 0 ? 10 : -10));
    volShowUntil = millis() + 1500;
    audioPlay(SND_TICK);
  }
  pendH = pendV = 0;
}

// 钉住 tap box: the pin is drawn in the VIEWED frame (faceRender runs under
// setRotation), so map the native touch point through the same rotation the
// swipe axes use below before testing the bottom-right 64 px corner. The box
// stays live even while the pin is invisible (desk + unpinned) — otherwise
// there would be no way back on from the desk.
static const int PIN_HIT = 64;
static bool pinHit(int x, int y) {
  // pageXY, the device-validated table: faceViewXY flips y in rotations 1/3
  // (the claim card found it 2026-09-26), which put the pin's hit zone in the
  // view's TOP-right corner on a side-lying board — over the session row's ›,
  // and nowhere near the pin icon itself
  int vx, vy;
  pageXY(x, y, vx, vy);
  return vx >= LCD_W - PIN_HIT && vy >= LCD_H - PIN_HIT;
}

// Claude 多会话 top band: y < 110 of the face's view frame, cut
// at x 240 — -1 = left half (previous), +1 = right half (next), 0 = not a
// session tap. Live only while the row is on screen and the seat on screen
// owns ≥2 sessions; a hidden row leaves the top as a plain tap (nudge).
// pageXY, not faceViewXY: the claim card found faceViewXY flips y in canvas
// rotations 1/3 (device, 2026-09-26) — with it, a face grabbed by needs_you
// onto a side-lying board would put this band at the bottom, over the bubble.
static int sessTopHalf(int x, int y) {
  if (!sessActive() || !faceSessRowShown()) return 0;
  int vx, vy;
  pageXY(x, y, vx, vy);
  if (vy >= 110) return 0;
  return vx < 240 ? -1 : 1;
}

static void touchPoll() {
  if (!tpOk) return;
  if ((pendH || pendV) && (int32_t)(millis() - pendAt) > 300) execPendingSwipe();
  if (!tpIrq && !tpWasDown) return;
  tpIrq = false;
  int16_t rx = 0, ry = 0;
  uint8_t n = tp.getPoint(&rx, &ry, 1);
  bool down = n > 0;
  if (down) {
    int x = rx, y = ry;
#if TOUCH_SWAP_XY
    int tmp = x; x = y; y = tmp;
#endif
#if TOUCH_MIRROR_X
    x = LCD_W - 1 - x;
#endif
#if TOUCH_MIRROR_Y
    y = LCD_H - 1 - y;
#endif
    if (!tpWasDown) {
      tpStartX = x; tpStartY = y;
      tpDownAt = millis();
      tpConsumed = false;
      noteActivity();
    }
    // petting = back-and-forth rubbing, either axis, lifts allowed: the
    // finger reversing direction is what a deliberate swipe never does
    if (tpWasDown && shownPage == PAGE_FACE && !pickedUp) {
      int sx = x - tpLastX, sy = y - tpLastY;
      int8_t dir = 0;
      if      (abs(sx) > 5 && abs(sx) >= abs(sy)) dir = sx > 0 ? 1 : -1;
      else if (abs(sy) > 5)                       dir = sy > 0 ? 2 : -2;
      if (dir) {
        // a deferred swipe resolves on the next touch's first move
        int8_t pend = pendH ? pendH : (pendV ? (pendV > 0 ? 2 : -2) : 0);
        if (pend) {
          if (dir == -pend) {            // reversed: it was petting all along
            pendH = pendV = 0;
            petUntil = millis() + 800;
            noteActivity();
          } else if (dir == pend) {      // same way: a real follow-up swipe
            execPendingSwipe();
          }
        }
        if ((int32_t)(petUntil - millis()) > 0) {
          petUntil = millis() + 800;     // stroking goes on (lifts allowed)
        } else if (petDir && dir == -petDir && petStrokes < 250 &&
                   ++petStrokes >= 2) {
          petUntil = millis() + 800;
          noteActivity();
        }
        petDir = dir;
      }
    }
    tpLastX = x; tpLastY = y;

    // hold ~1.5 s on the face = settings card. The approve bubble keeps its
    // own long-press (reject), so the hold is inert while needs_you is up
    // (the bubble's own rule: the current session's state).
    if (tpWasDown && !tpConsumed && !pendH && !pendV && !demoStart &&
        shownPage == PAGE_FACE && !listening &&
        sessEffState() != ST_NEEDS_YOU &&
        (int32_t)(petUntil - millis()) <= 0 &&
        (int32_t)(setCardUntil - millis()) <= 0 && !claimShowing() &&
        (int32_t)(millis() - tpDownAt) >= 1500 &&
        abs(x - tpStartX) < SWIPE_MIN_PX && abs(y - tpStartY) < SWIPE_MIN_PX) {
      tpConsumed = true;
      setCardUntil = millis() + 12000;
      profUntil = 0;
      sendUntil = 0;
      audioPlay(SND_TICK);
      noteActivity();
    }
  }
  // classify on release, so a swipe never fires the tap action first
  if (!down && tpWasDown) {
    bool wasPetting = (int32_t)(petUntil - millis()) > 0;
    petDir = 0; petStrokes = 0;
    int dx = tpLastX - tpStartX, dy = tpLastY - tpStartY;
    // native-frame classification (any-swipe checks, rotation-invariant)…
    bool hswipe = abs(dx) > SWIPE_MIN_PX && abs(dx) > 2 * abs(dy);
    bool vswipe = abs(dy) > SWIPE_MIN_PX && abs(dy) > 2 * abs(dx);
    // …and the swipe rotated into the VIEWED frame (pages render rotated by
    // (4-shownRot)&3). vdx rows were device-validated on the two-sided
    // pages; vdy rows are their 90°-consistent counterparts (only the face
    // page reads vdy — if in-hand volume swipes come out inverted in a
    // rotated grip, flip the vdy signs of cases 1 and 3).
    int vdx, vdy;
    switch ((4 - shownRot) & 3) {
      case 1:  vdx = -dy; vdy =  dx; break;
      case 2:  vdx = -dx; vdy = -dy; break;
      case 3:  vdx =  dy; vdy = -dx; break;
      default: vdx =  dx; vdy =  dy; break;
    }
    bool vh = abs(vdx) > SWIPE_MIN_PX && abs(vdx) > 2 * abs(vdy);
    bool vv = abs(vdy) > VSWIPE_MIN_PX && 2 * abs(vdy) > 3 * abs(vdx);
    if (claimShowing()) {
      // claim card owns the touch (modal). Only the green button says yes;
      // outside the card or a >=700 ms hold says no; swipes and the rest of
      // the card do nothing. The first 400 ms are ignored (a finger already
      // on the glass when it popped up).
      tpConsumed = false;
      pendH = pendV = 0;
      uint32_t nowMs = millis();
      if (claimOn && (int32_t)(nowMs - claimAt) >= 400 && (int32_t)(tpDownAt - claimAt) >= 0 &&
          !hswipe && !vswipe && !wasPetting) {
        // pageXY, not the old faceViewXY (gone 2026-10-04): the face is drawn with the same
        // canvas rotation as the side pages, and pageXY is the device-
        // validated table (faceViewXY flips y in rotations 1/3 — a side-lying
        // tap on Connect landed "outside the card", 2026-09-26 19:50).
        int vx, vy;
        pageXY(tpLastX, tpLastY, vx, vy);
        int hit = faceClaimHit(vx, vy);
        bool hold = (int32_t)(nowMs - tpDownAt) >= 700;
        Serial.printf("claim: tap %d,%d -> hit %d%s\n", vx, vy, hit, hold ? " (hold)" : "");
        if (hold || hit < 0) claimResolve(false, "declined");
        else if (hit == 1)   claimResolve(true, "tap");
      }
    } else if (tpConsumed) {
      tpConsumed = false;    // the hold already opened the settings card
    } else if (shownPage == PAGE_FACE &&
               (int32_t)(setCardUntil - millis()) > 0) {
      // settings card owns the touch: taps pick, anything else dismisses
      uint32_t nowMs = millis();
      if (hswipe || vswipe || wasPetting) {
        setCardUntil = 0;
      } else {
        int hit = faceSettingsHit(tpLastX, tpLastY);
        Serial.printf("setcard: tap %d,%d -> hit %d\n", tpLastX, tpLastY, hit);
        if (hit >= 0 && hit < N_SKINS) {          // dress the current seat
          if (wearSkin((uint8_t)hit)) audioPlay(SND_SELECT);
          setCardUntil = nowMs + 12000;
        } else if (hit >= 10 && hit <= 12) {      // brightness level
          brightLvl = hit - 10;
          growPrefs.putUChar("bright", brightLvl);
          audioPlay(SND_TICK);
          setCardUntil = nowMs + 12000;
        } else if (hit == -2) {
          setCardUntil = 0;                       // tapped outside: dismiss
        } else {
          setCardUntil = nowMs + 12000;           // inside the card: keep it
        }
      }
    } else if (shownPage == PAGE_FACE) {
      if (wasPetting) {
        // lifting off after a petting session: no swipe/tap side effects
      } else if (vh) {
        // swipe left = next seat to the right (executes deferred, see above).
        // Viewed-frame + no !pickedUp gate since 2026-09-01: the day-5 gate
        // made in-hand seat swiping dead (and a desk swipe that rocked the
        // board ate the follow-up swipe) — with shownRot mapping the axes,
        // swiping the face works in any grip.
        pendH = vdx < 0 ? -1 : 1;
        pendV = 0;
        pendAt = millis();
      } else if (vv) {
        // swipe up = louder, down = softer (±10). Screen coords are true
        // since TOUCH_MIRROR_Y was fixed (day 9): viewed up-swipe = vdy < 0.
        // Fires at once: the 300 ms petting-vs-swipe deferral stays for seat
        // switches only — a ±10 tick during a vertical rub is nothing, while
        // waiting 300 ms on every volume step felt sluggish (2026-09-02).
        pendV = vdy < 0 ? 1 : -1;
        pendH = 0;
        execPendingSwipe();
      } else if (!hswipe && !vswipe) {
        uint32_t nowMs = millis();
        // the decision follows the CURRENT session (the bubble does)
        bool effNeeds = sessEffState() == ST_NEEDS_YOU;
        // a ≥700 ms hold is a reject anywhere on the face, the top band too
        bool rejectHold = effNeeds && (int32_t)(nowMs - tpDownAt) >= 700;
        bool pin = pinHit(tpLastX, tpLastY);
        int half = (pin || rejectHold) ? 0 : sessTopHalf(tpLastX, tpLastY);
        if (pin) {
          // corner pin wins the tap before every other face action (doc/06):
          // status toast, double-tap profile and the approve bubble all live
          // in the middle, so the corner is unambiguously about pinning.
          setPinned(!pinned, PAGE_FACE);
        } else if (half) {
          // Claude 多会话: the session row is showing, so the top
          // band (y < 110) steps the list — left half back, right half on.
          // Not a "tap" for the double-tap clock: two quick taps here are two
          // steps, and a tap on the face right after must not open the card.
          sessStep(half);
        } else if ((int32_t)(sendUntil - nowMs) > 0) {  // bubble armed: send!
          sendUntil = 0;
          JsonDocument kd;      // same destination the words went to
          kd["t"] = "key"; kd["k"] = "enter";
          if (voiceToFront) kd["to"] = "front";
          else if (voiceSid[0]) kd["sid"] = voiceSid;   // the session it was said to
          sendJson(kd);
          audioPlay(SND_DONE);
        } else if ((int32_t)(profUntil - nowMs) > 0) {  // card open: dismiss
          profUntil = 0;     // skin picking lives on the settings card now
          audioPlay(SND_TICK);
        } else if (effNeeds && (hostUp || bleConnected())) {
          // approve bubble is up: tap = approve, hold >=700 ms = reject.
          // 1 s debounce so a nervous double-tap can't stack extra Enters.
          static uint32_t lastDecideAt = 0;
          if ((int32_t)(nowMs - lastDecideAt) > 1000) {
            lastDecideAt = nowMs;
            bool rej = (int32_t)(nowMs - tpDownAt) >= 700;
            JsonDocument kd;
            kd["t"] = "key"; kd["k"] = rej ? "reject" : "approve";
            // which session this decides; the host drops it if
            // that session no longer waits (answered on the Mac meanwhile)
            if (sessActive()) kd["sid"] = sessList[sessCur].id;
            sendJson(kd);
            audioPlay(rej ? SND_SIGH : SND_DONE);
            nudgeUntil = nowMs + 2500;   // toast shows who got the decision
          }
        } else if ((int32_t)(nowMs - lastTapAt) < 400) {  // double tap: profile
          profUntil = nowMs + 6000;
          nudgeUntil = 0;
          audioPlay(SND_TICK);
        } else {
          if ((int32_t)(boredUntil - nowMs) > 0) {    // poked mid-sigh: yay!
            boredUntil = 0;
            audioPlay(SND_DONE);
          }
          nudgeUntil = nowMs + 3000;  // tap = "what's up?" status toast
        }
        if (!half) lastTapAt = nowMs;
      }
    } else if (shownPage == PAGE_CALENDAR && almanacView && vv) {
      // 换一签 (2026-09-22, user): up = next reading, down
      // = previous, five readings wrap. The odd-rotation sign flip below is
      // the play page's, and this strip is the other odd rotation (sector 1),
      // so it carries over unchanged. Coming back round to the own draw shows
      // a pill toast.
      int up = (((4 - shownRot) & 3) & 1) ? (vdy > 0) : (vdy < 0);
      if (almanacAltCount() > 0) {
        int pick = almanacPickStep(up ? +1 : -1);
        audioPlay(pick == 0 ? SND_SELECT : SND_TICK);
        if (pick == 0) showToast("命运自有回应");
      } else {
        audioPlay(SND_TICK);                         // nothing to cycle yet (old host / old card file)
      }
    } else if (shownPage == PAGE_CLOCK && clockView != CV_CLOCK && vv) {
      // 播放页 up/down = volume ±10, like the face page; the toast is the
      // bar along the bottom band. Immediate: the petting deferral is a
      // face-page problem only. Sign: the shared table's vdy row for canvas
      // rotation 3 read inverted here — user 2026-09-09 morning: an up-swipe
      // on the podcast page made it softer — so this page flips it (the face
      // page's in-hand rows stay as validated). Same afternoon the clock
      // strip moved to the other side, canvas rotation 1 on the desk: rows
      // 1 and 3 of the table are 180° twins, so the inversion carries over —
      // flip on both odd rotations.
      int up = (((4 - shownRot) & 3) & 1) ? (vdy > 0) : (vdy < 0);
      audioSetVolume(audioVolume() + (up ? 10 : -10));
      volShowUntil = millis() + 1500;
      audioPlay(SND_TICK);
    } else if (shownPage == PAGE_CLOCK && clockView != CV_CLOCK &&
               !vh && !hswipe && !vswipe) {
      // compact card: the transport band is three 160×160
      // zones — prev|toggle|next on 当前播放, -15 s|toggle|+15 s on 播客 —
      // and the card itself is a toggle too. 300 ms debounce: a nervous
      // double tap must not stack two toggles on the Mac.
      static uint32_t lastPlayTapAt = 0;
      uint32_t nowMs = millis();
      if ((int32_t)(nowMs - lastPlayTapAt) > 300) {
        lastPlayTapAt = nowMs;
        int px, py;
        pageXY(tpLastX, tpLastY, px, py);
        playAct(pageSrc(), playZoneAt(px, py));
        audioPlay(SND_TICK);
      }
    } else if ((shownPage == PAGE_CALENDAR ||
                shownPage == PAGE_CLOCK) &&
               !hswipe && !vswipe) {
      // 求签: tapping the almanac asks the pet to read
      // today's fortune aloud — invited speech only, never unprompted.
      if (shownPage == PAGE_CALENDAR && almanacView &&
          (hostUp || bleConnected())) {
        JsonDocument q;                    // the host reads the reading on screen
        q["t"] = "qian"; q["pick"] = almanacPick();
        sendJson(q);
      }
    } else if (shownPage == PAGE_CALENDAR ||
               shownPage == PAGE_CLOCK) {
      // two-sided orientations, in the shared viewed frame (vdx above): a
      // viewed right-swipe opens the almanac / battle report, left-swipe
      // returns to the calendar / clock. (shownRot == orient on the desk;
      // in hand it is the live gravity sector, so held swipes match too.)
      if (shownPage == PAGE_CALENDAR) {
        // two pages wrap (2026-09-22, user): either direction flips between
        // 黄历 and 日历, so no swipe on this strip is ever a dead one
        if (abs(vdx) > SWIPE_MIN_PX) {
          almanacView = !almanacView;
          audioPlay(almanacView != langEn() ? SND_TICK : SND_SELECT);   // home = tick, away = select
        }
      } else {
        // 三联页 (user 2026-09-09): 播客 ←右滑← 钟表 →左滑→ 当前播放,
        // no wrap — the two players never reach each other directly.
        if (vdx > SWIPE_MIN_PX) {
          if (clockView == CV_CLOCK)   { clockView = CV_POD;   audioPlay(SND_SELECT); }
          else if (clockView == CV_NP) { clockView = CV_CLOCK; audioPlay(SND_TICK); }
        } else if (vdx < -SWIPE_MIN_PX) {
          if (clockView == CV_POD)        { clockView = CV_CLOCK; audioPlay(SND_TICK); }
          else if (clockView == CV_CLOCK) { clockView = CV_NP;    audioPlay(SND_SELECT); }
        }
      }
    }
  }
  tpWasDown = down;
}

// ---------- setup / loop ----------
void setup() {
  Serial.begin(115200);
  // USB-CDC rx queue: the core's default is 256 bytes and the ISR DROPS bytes
  // once it is full (no flow control), so anything bigger than that arriving
  // between two loop() passes lost its tail — including the newline, which
  // glued the next command onto junk. Big enough for one max line plus a bit.
  Serial.setRxBufferSize(RX_LINE_MAX + 256);

  Wire.begin(PIN_I2C_SDA, PIN_I2C_SCL);

  // PMU first: ALDO3 is the display rail.
  pmuOk = pmu.begin(Wire, AXP2101_SLAVE_ADDRESS, PIN_I2C_SDA, PIN_I2C_SCL);
  if (pmuOk) {
    pmu.setChargeTargetVoltage(XPOWERS_AXP2101_CHG_VOL_4V2);
    pmu.setChargerConstantCurr(XPOWERS_AXP2101_CHG_CUR_500MA);
    pmu.enableBattDetection();
    pmu.enableBattVoltageMeasure();
    pmu.enableVbusVoltageMeasure();
    pmu.enableALDO3();
    // The middle key is wired to GPIO16 *and* to the AXP2101's PWRON pin
    // (pins.h L21). PWRON's default long-press action is a hardware power
    // cut after ~6 s, decided inside the PMU with no firmware say in it —
    // and since 2026-09-19 that same key is hold-to-talk, so any dictation
    // longer than six seconds would kill the board mid-sentence. Turn the
    // hard cut off (REG 22H bit 1, XPowersAXP2101.tpp L823).
    // Cost, stated plainly: there is no longer a physical kill switch. If
    // the firmware wedges, holding a key does nothing and only unplugging /
    // draining the cell would stop it — which is why the task watchdog
    // (wdtBegin at the end of setup, 15 s, panic) is mandatory from here on.
    // Clean shutdown still exists: right key held 3 s -> powerOff().
    // The PWRON long-press hard cut cannot be turned off on this part: REG
    // 22H[1] reads back 0 after clearing and the PMU still cuts at OFFLEVEL
    // (rst=poweron, 2026-09-19). So it stays, as the physical emergency stop
    // for a wedged firmware, pinned at 6 s; a live firmware shuts down cleanly
    // at 3 s from the same key (loop). Never make the middle key a hold key.
    pmu.setPowerKeyPressOffTime(XPOWERS_POWEROFF_6S);
    // The middle key is ONLY readable through the PMU: GPIO16 is SYS_OUT on
    // the schematic, not a key sense line (measured 2026-09-19: pressing PWR
    // never moved it). PWRON edge IRQs give press (negative) and release
    // (positive); pmuKeyScan() polls the status register once per loop pass.
    pmu.disableIRQ(XPOWERS_AXP2101_ALL_IRQ);
    pmu.enableIRQ(XPOWERS_AXP2101_PKEY_NEGATIVE_IRQ | XPOWERS_AXP2101_PKEY_POSITIVE_IRQ);
    pmu.clearIrqStatus();            // the power-on press itself is not a command
  }

  // Touch reset line
  pinMode(PIN_TP_RESET, OUTPUT);
  digitalWrite(PIN_TP_RESET, LOW); delay(10);
  digitalWrite(PIN_TP_RESET, HIGH); delay(60);

  // Display: CO5300 over QSPI, full-frame PSRAM canvas
  bus = new Arduino_ESP32QSPI(PIN_LCD_CS, PIN_LCD_SCLK, PIN_LCD_SDIO0,
                              PIN_LCD_SDIO1, PIN_LCD_SDIO2, PIN_LCD_SDIO3);
  gfx = new Arduino_CO5300(bus, PIN_LCD_RESET, 0, LCD_W, LCD_H, 0, 0, 0, 0);
  canvas = new Arduino_Canvas(LCD_W, LCD_H, gfx);
  if (!canvas->begin()) Serial.println("canvas begin FAILED");
  bus->beginWrite();
  bus->writeC8D8(0x36, 0xA0);        // panel orientation (as factory demo)
  bus->endWrite();
  gfx->setBrightness(0);
  canvas->fillScreen(0);
  canvas->flush();
  delay(20);
  gfx->setBrightness(180);

  // Touch
  tp.setPins(-1, PIN_TP_INT);
  tpOk = tp.begin(Wire, 0x5A, PIN_I2C_SDA, PIN_I2C_SCL);
  if (tpOk) {
    tp.setMaxCoordinates(LCD_W, LCD_H);
    pinMode(PIN_TP_INT, INPUT);
    attachInterrupt(digitalPinToInterrupt(PIN_TP_INT), onTpIrq, FALLING);
  } else Serial.println("touch init FAILED");

  // IMU
  imuOk = qmi.begin(Wire, QMI8658_L_SLAVE_ADDRESS, PIN_I2C_SDA, PIN_I2C_SCL);
  if (imuOk) {
    qmi.reset();
    qmi.configAccelerometer(SensorQMI8658::ACC_RANGE_4G,
                            SensorQMI8658::ACC_ODR_125Hz);
    qmi.enableAccelerometer();
  } else Serial.println("IMU init FAILED");

  // RTC: seeds the soft clock at boot so the clock page, the day counters
  // and the almanac on the card work before (or without) a host; host time
  // pushes keep it trimmed (rtcHostSync). Power rail etc.: boardrtc.h.
  rtcInit();

  // Buttons
  pinMode(PIN_KEY_PWR, INPUT);         // external inverter drives it
  pinMode(PIN_KEY_IO18, INPUT_PULLUP);
  pinMode(PIN_KEY_BOOT, INPUT_PULLUP);

  // microSD (optional): mount + FAT self-test, facts land on the settings card;
  // then the card fonts (any glyph missing in flash comes from there)
  if (sdInit()) { sdFontLoadAll(); almanacFromCard(clockEpoch()); }
  playerInit();     // scan /agentpet/audio/ + restore the resume point

  // Growth stats + skin, loaded before the boot animation so the eyes
  // already wear the chosen skin while they open
  growPrefs.begin("petgrow", false);
  statDone   = growPrefs.getUInt("done", 0);
  statDays   = growPrefs.getUInt("days", 0);
  statPets   = growPrefs.getUInt("pets", 0);
  lastDayNum = growPrefs.getUInt("lastday", 0);
  brightLvl  = growPrefs.getUChar("bright", 1) % 3;
  pickupBind = growPrefs.getUChar("pickup", BIND_FOLLOW) % N_BINDS;
  {   // 钉住: 0 = off, else page + 1 (grey pin, no highlight after a reboot)
    uint8_t v = growPrefs.getUChar("pin", 0);
    pinned = v != 0;
    pinnedPage = v ? (uint8_t)(v - 1) : PAGE_FACE;
    if (pinnedPage != PAGE_FACE && pinnedPage != PAGE_CLOCK && pinnedPage != PAGE_CALENDAR)
      pinnedPage = PAGE_FACE;
  }
  gazeOn     = growPrefs.getUChar("gaze", 1) != 0;  // 眼神追声 default on
  audioGazeEnable(gazeOn);
  voiceTts   = growPrefs.getUChar("vtts", 0) != 0;
  showOffSeats = growPrefs.getUChar("showoff", 0) != 0;   // 离线席位默认隐藏
  g_lang = growPrefs.getUChar("lang", LANG_ZH) == LANG_EN ? LANG_EN : LANG_ZH;
  almanacView = !langEn();   // English lands on the calendar side
  for (int i = 0; i < N_AGENTS; i++) {
    char key[8];
    snprintf(key, sizeof(key), "skin%d", i);
    skinBy[i] = growPrefs.getUChar(key, skinBy[i]) % N_SKINS;
  }
  {   // exclusive wardrobe: dedupe outfits older builds may have saved
    bool claimed[N_SKINS] = {};
    for (int i = 0; i < N_AGENTS; i++) {
      if (claimed[skinBy[i]]) {
        for (uint8_t s = 0; s < N_SKINS; s++)
          if (!claimed[s]) { skinBy[i] = s; saveSkin(i); break; }
      }
      claimed[skinBy[i]] = true;
    }
  }
  faceSetSkin(skinBy[selected]);

  // Audio (ES8311 + speaker)
  bool audioOk = audioInit();

  // BLE link (advertises as "AgentPet"; Mac bridge connects)
  ownerLoad();               // first: the very first advertisement already carries the owner digest
  bleInit();

  // Wi-Fi
  // persistent(false): the driver keeps its STA config in RAM instead of
  // writing NVS on every connect. That flash write is the prime suspect for
  // the ppTask CacheError panics (2nd Mac 2026-09-26), which
  // hit exactly when the radio re-associates.
  WiFi.persistent(false);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);                // keep latency low for events
  wifiLoad();
  wifiStartScan();                     // wifiPoll joins the strongest known network
  MDNS.begin("agentpet");

  // Wake-up: chirp + eyes opening together, after connectivity has started
  // (Wi-Fi associates and the Mac's BLE scan land during the animation).
  // The chirp doubles as an audio works-check.
  if (audioOk) audioPlay(SND_BOOT);
  faceBootAnim(canvas);            // 1.7 s of blocking animation, deliberately
                                   // before the watchdog subscribes

  // Last thing in setup: everything above is one-shot and slow by design
  // (SD mount, font index, boot animation), and none of it would ever run
  // again — watching it would only buy false reboots.
  wdtBegin();

  noteActivity();
  Serial.println("AgentTouch up");
}

void loop() {
  uint32_t now = millis();
  wdtFeed();                       // one check-in per pass, before anything
                                   // that can stall (see wdtBegin's note for
                                   // the paths that feed on their own)

  if (sdPoll()) {                      // card slotted while running
    sdFontLoadAll();
    sessRelabel();                     // card titles vs folder names
    almanacFromCard(clockEpoch());
    playerInit();
    wdtFeed();                         // four index/scan passes over a cold
                                       // card in one loop pass (~1-3 s)
    JsonDocument d;
    d["t"] = "sd"; d["mb"] = sdInfo().sizeMB; d["rw"] = sdInfo().rwOk;
    sendJson(d);
  }

  {   // finished TTS clip: report its timing
    SpeakStats st;
    if (audioSpeakTakeStats(st)) {
      JsonDocument d;
      d["t"] = "spk"; d["id"] = spkId; d["bytes"] = st.bytes;
      d["fmt"] = st.fmt ? "ima" : "pcm"; d["start_ms"] = st.startMs;
      d["rx_ms"] = st.rxMs; d["play_ms"] = st.playMs; d["underrun"] = st.underrun;
      sendJson(d);
      Serial.printf("speak: %s %lu B, start +%lu ms, rx %lu ms, play %lu ms%s\n",
                    st.fmt ? "ima" : "pcm", (unsigned long)st.bytes, (unsigned long)st.startMs,
                    (unsigned long)st.rxMs, (unsigned long)st.playMs, st.underrun ? " UNDERRUN" : "");
    }
  }

  pmuKeyScan(bPwr); btnScan(bIo18); btnScan(bBoot);   // middle key lives on the PMU
  if (bPwr.pressed || bIo18.pressed || bBoot.pressed) noteActivity();
  tcpPump();                           // finish any parked TCP remainder before new writers
  micDrain();
  bleTestPoll();

  // Right key (IO18) = hold-to-talk, fn down at PRESS. A 250 ms click gate
  // (short click = summon) shipped and was reverted the same day (2026-09-01):
  // press-and-talk is muscle-memory timed, the gate clipped the first words.
  // Never put latency on this key — the pet is summoned by shake or turn.
  // 2026-09-19: moved to the middle key for one evening and moved back. The
  // middle key is the AXP2101's PWRON, and holding PWRON longer than OFFLEVEL
  // (10 s at most) hard-cuts the rail no matter what REG 22H[1] says — it is
  // EFUSE-locked on this part (measured: rst=poweron at ~10 s with the bit
  // cleared). A dictation key that kills the board mid-sentence is no key.
  if (bIo18.pressed) {
    listening = true;
    sendUntil = 0;                     // new dictation: retire the old bubble
    audioPlay(SND_VOICE_ON);
    JsonDocument d;
    d["t"] = "voice"; d["a"] = "start"; d["agent"] = AGENTS[selected].id;
    // shownPage is what is REALLY on screen this frame, so a face carried in
    // the hand (or grabbed by needs_you) still talks to its own seat.
    voiceToFront = shownPage != PAGE_FACE;
    if (voiceToFront) d["to"] = "front";
    // face-page dictation goes to the session on screen; latched like
    // voiceToFront so the send bubble's Return lands in the same tab even if
    // the row moves on before the tap. Desk pages stay "front" only.
    voiceSid[0] = 0;
    if (!voiceToFront && sessActive()) {
      strlcpy(voiceSid, sessList[sessCur].id, sizeof(voiceSid));
      d["sid"] = voiceSid;
    }
    d["mic"] = micOnTalk && audioMicAvailable();   // tells the host frames follow
    if (micOnTalk) micSessionStart();
    d["link"] = micLink == MICLINK_BLE ? "ble" : micLink == MICLINK_TCP ? "tcp" : "none";
    sendJson(d);
  }
  if (bIo18.released) {
    listening = false;
    sendUntil = now + 6000;            // arms the tap-to-send bubble
    audioPlay(SND_VOICE_OFF);
    JsonDocument d;
    d["t"] = "voice"; d["a"] = "stop"; d["agent"] = AGENTS[selected].id;
    sendJson(d);
    if (micOnTalk) audioMicStop();
  }

  // left key (BOOT): release = mute toggle (2026-09-21 晚); hold 1 s = 钉住
  // whatever page is on screen / unpin (2026-09-22, user — the slot the chat
  // mode vacated). Seat cycling is gone: swiping the face has
  // been the way to change seats for weeks, and off seats are not even drawn.
  // Mute stays on release so the hold cannot fire both; the hold fires the
  // moment 1 s is reached, so the 名牌 appears while the finger is still down.
  static bool bootHeld = false;
  if (bBoot.pressed) bootHeld = false;
  if (bBoot.down && !bootHeld && (int32_t)(now - bBoot.at) >= 1000) {
    bootHeld = true;
    setPinned(!pinned, shownPage);
  }
  if (bBoot.released && !bootHeld) {
    audioToggleMute();
    volShowUntil = now + 1500;
    audioPlay(SND_TICK);               // silent when it just muted
  }

  // Middle key (PWR): short press = status toast; hold 3 s = clean power off
  // (BYE BYE animation, then pmu.shutdown). Read through the PMU's PWRON edge
  // IRQs (pmuKeyScan) since 2026-09-19 — GPIO16 never carried this key, so
  // until today this block had never actually run: the "3 s shutdown" people
  // saw was the PMU's own 6 s hard cut. The hard cut stays as the backstop
  // for a wedged firmware (OFFLEVEL 6 s, set in setup); 3 s here wins the
  // race when the firmware is alive.
  if (bPwr.released && (int32_t)(now - bPwr.at) < 1000)
    nudgeUntil = now + 3000;
  if (bPwr.down && (int32_t)(now - bPwr.at) >= 3000 && pmuOk)
    powerOff("BYE BYE...");

  playerPoll();     // metadata after an auto-advance + the 10 s resume write
  imuPoll();
  touchPoll();
  wifiPoll();
  netPoll();

  // keepalive on EVERY link that is up — each host watchdog needs its own
  // feed. History of this ping (both directions of the same trap): first it
  // lived inside netPoll's TCP path and starved the host's 35 s BLE rx
  // watchdog; then it went through sendJson (BLE preferred) and starved the
  // host's 60 s TCP recv timeout instead — the TCP link cycled every minute
  // unnoticed for days, until TTS (TCP-only traffic) hit the gap (day 9).
  if ((int32_t)(now - lastPing) > 10000 &&
      (bleConnected() || sock.connected())) {
    lastPing = now;
    // The beat carries the board's vitals: a slow leak
    // shows up in the host log instead of an eventual silent reboot. Two
    // different pings share the name and never mix — this one is board→host
    // and expects nothing back (the host answers a bare {"t":"pong"}, which
    // has no branch here and is dropped), while /test/rtt is host→board
    // {"t":"ping","id":N} answered by the "ping" branch of handleLine and
    // matched on that id.
    JsonDocument d;
    d["t"]    = "ping";
    d["up"]   = uptimeSec();
    d["heap"] = (uint32_t)esp_get_free_heap_size();
    d["min"]  = (uint32_t)esp_get_minimum_free_heap_size();      // low water mark
    d["big"]  = (uint32_t)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT);
    String out;
    serializeJson(d, out);
    // Still BOTH links on purpose, not sendJson: each host watchdog needs its
    // own feed (the paragraph above is the scar tissue — sendJson prefers BLE
    // and would starve the host's 60 s TCP recv timeout all over again).
    if (bleConnected())   bleSendLine(out);
    if (sock.connected()) tcpWriteLine(out, 0, false);   // busy = skip this beat (mic frames keep the host fed)
  }

  // The same numbers on the serial log every 10 min, for a soak watched over
  // USB with no host at all.
  {
    static uint32_t lastHeapLog = 0;
    if ((int32_t)(now - lastHeapLog) > 600000) {
      lastHeapLog = now;
      Serial.printf("heap: free=%u min=%u big=%u\n",
                    (unsigned)esp_get_free_heap_size(),
                    (unsigned)esp_get_minimum_free_heap_size(),
                    (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_8BIT));
    }
  }

  // USB-CDC: a JSON line written to the serial port is a host→board message
  // like any other (host/shot.py uses it; handy for debugging with no network
  // at all). Replies that must reach the requester (shot) look at rxFromSerial;
  // everything else still answers over BLE/TCP via sendJson.
  {
    static String serBuf;
    static bool   serOverflow = false;
    while (Serial.available()) {
      char c = (char)Serial.read();
      if (c == '\n') {
        if (serOverflow) Serial.printf("serial: rx line > %u bytes dropped\n", (unsigned)RX_LINE_MAX);
        else { rxFromSerial = true; handleLine(serBuf); rxFromSerial = false; }
        serBuf = ""; serOverflow = false;
      } else if (c == '\r') {
      } else if (serOverflow || serBuf.length() >= RX_LINE_MAX) {
        serOverflow = true; serBuf = "";       // drop the whole line, not its tail
      } else {
        serBuf += c;
      }
    }
  }

  // BLE: drain received lines in the loop task (keeps app single-threaded)
  {
    String bl;
    uint16_t bh;
    while (blePopLine(bl, &bh)) { curBleLink = bh; handleLine(bl); curBleLink = BLE_NO_LINK; }
    bleTick();
    claimTick();
    static bool prevSub = false;
    bool sub = bleConnected();
    if (sub && !prevSub) {
      JsonDocument d;
      d["t"] = "hello"; d["dev"] = "amoled216"; d["v"] = 2; d["link"] = "ble";
      d["up"] = uptimeSec();
      d["rst"] = resetReasonName(esp_reset_reason());
      d["sel"] = AGENTS[selected].id;                       // seat we woke up on, see the TCP hello
      d["sd"] = sdInfo().mounted ? sdInfo().sizeMB : 0; d["sdrw"] = sdInfo().rwOk;
      d["rtc"] = rtcInfo().bootEpoch;
      d["build"] = FW_BUILD; d["part"] = esp_ota_get_running_partition()->label;
      d["owner"] = ownerName;
      addSkins(d);
      sendJson(d);
      Serial.println("ble: hello sent");
    }
    prevSub = sub;
  }

  // no host on either link = a stale session list; the host pushes
  // a fresh one once it is back (and after a card swap re-label it)
  if (sessN) {
    if (!(hostUp || bleConnected())) sessClear();
    else if (sdFontReady(SDF_TINY) != sessLabelCard) sessRelabel();
  }

  // charger = feeding time; charge-done = satisfied burp (2 s PMU poll).
  // Same poll also tracks the power state for the settings card and guards
  // the battery: sighs below 10%, clean goodbye at <=5% sustained a minute.
  {
    static uint32_t lastChgPoll = 0;
    static bool wasChg = false, wasFull = false;
    static uint32_t lowSince = 0, lastLowSigh = 0;
    if (pmuOk && (int32_t)(now - lastChgPoll) > 2000) {
      lastChgPoll = now;
      bool chg  = pmu.isCharging();
      bool vbus = pmu.isVbusIn();
      bool full = vbus &&
                  pmu.getChargerStatus() == XPOWERS_AXP2101_CHG_DONE_STATE;
      if (chg && !wasChg)   { eatUntil  = now + 3500; audioPlay(SND_NOM);  noteActivity(); }
      if (full && !wasFull) { burpUntil = now + 3000; audioPlay(SND_BURP); noteActivity(); }
      wasChg = chg; wasFull = full;
      powState = chg ? 1 : (vbus ? 2 : 0);

      int pct = pmu.isBatteryConnect() ? pmu.getBatteryPercent() : -1;
      if (!vbus && pct >= 0 && pct <= 5) {
        if (!lowSince) lowSince = now;     // 60 s of confirmed low, so one
      } else lowSince = 0;                 // noisy PMU reading can't kill us
      if (lowSince && (int32_t)(now - lowSince) > 60000)
        powerOff("LOW BATT...");
      if (!vbus && pct >= 0 && pct < 10 &&
          (int32_t)(now - lastLowSigh) > 180000) {
        lastLowSigh = now;                 // a soft nag every ~3 min
        audioPlay(SND_SIGH);
      }

      // low-battery 名牌 on whatever page is up (the pill every page draws).
      // Held back while the face is asking for an approve — the pill would
      // sit on the bubble — or the pet sleeps face-down (panel off), and
      // shown on the first poll after that. Not noteActivity(): the panel
      // goes full only while the pill is up (idle dimming below), so the
      // repeats neither keep a low battery's screen lit nor hush the sighs.
      bool fake = battFakeOn();
      int p = fake ? battFakePct : pct;
      if (p < 0 || (vbus && !fake)) {
        lowBattWarned = 0;
      } else {
        uint8_t rearm = p < 13 ? 2 : p < 23 ? 1 : 0;   // 3-point hysteresis
        if (lowBattWarned > rearm) lowBattWarned = rearm;
        uint8_t lvl = p < 10 ? 2 : p < 20 ? 1 : 0;
        uint32_t every = (fake && battFakeRepMs) ? battFakeRepMs
                       : (lvl == 2 ? 5 : 10) * 60000UL;
        bool again = lvl && lowBattWarned >= lvl &&
                     (int32_t)(now - lowBattToastAt) > (int32_t)every;
        if ((lvl > lowBattWarned || again) && !flipped &&
            sessEffState() != ST_NEEDS_YOU) {
          if (lvl > lowBattWarned) lowBattWarned = lvl;
          lowBattToastAt = now;
          char msg[40];
          snprintf(msg, sizeof(msg), tr(S_LOW_BATT), p);
          showToast(msg, 4000);
          Serial.printf("low batt: %d%%%s%s\n", p, again ? " again" : "",
                        fake ? " (fake)" : "");
        }
      }
    }
  }

  // purr loop while being stroked; count each stroking session once
  {
    static uint32_t lastPurr = 0;
    static bool petActive = false;
    bool nowPet = (int32_t)(petUntil - now) > 0;
    if (nowPet && !petActive) {
      statPets++;
      growPrefs.putUInt("pets", statPets);
    }
    petActive = nowPet;
    if (nowPet && (int32_t)(now - lastPurr) > 900) {
      lastPurr = now;
      audioPlay(SND_PURR);
    }
  }

  // companionship days: a new local date while powered on = one more day
  {
    static uint32_t lastDayChk = 0;
    if ((int32_t)(now - lastDayChk) > 60000) {
      lastDayChk = now;
      uint32_t ep = clockEpoch();
      if (ep) {
        uint32_t day = ep / 86400;
        if (day != lastDayNum) {
          almanacFromCard(ep);          // midnight without a host: turn the page ourselves
          statDays++;
          lastDayNum = day;
          growPrefs.putUInt("days", statDays);
          growPrefs.putUInt("lastday", lastDayNum);
        }
      }
    }
  }

  // lonely sighs: all agents idle/off and nobody has played with the pet
  // for 10 min -> a soft sigh every 2-5 min until someone pokes it
  {
    static uint32_t nextBored = 0;
    bool allQuiet = !listening && !pickedUp && !demoStart && !flipped;
    for (int i = 0; i < N_AGENTS && allQuiet; i++)
      allQuiet = agentStates[i] == ST_IDLE || agentStates[i] == ST_OFF;
    if (allQuiet && (int32_t)(now - lastActive) > 10 * 60000) {
      if (!nextBored) nextBored = now + (uint32_t)random(5000, 60000);
      if ((int32_t)(now - nextBored) > 0) {
        boredUntil = now + 4500;
        audioPlay(SND_SIGH);
        nextBored = now + (uint32_t)random(120000, 300000);
      }
    } else nextBored = 0;
  }

  // idle dimming: stay bright while anything deserves attention — a 名牌
  // included, so the low-battery one is read at full brightness. Bounded by
  // the longest 名牌 (4 s): a never-set toastUntil of 0 must not read as
  // "up" once millis() passes 2^31 (day 24.8).
  {
    int32_t toastLeft = (int32_t)(toastUntil - now);
    bool attention = listening || pickedUp || demoStart ||
                     (toastLeft > 0 && toastLeft <= 4000);
    for (int i = 0; i < N_AGENTS && !attention; i++)
      attention = agentStates[i] == ST_WORKING || agentStates[i] == ST_NEEDS_YOU;
    uint8_t want = BRIGHT_LVLS[brightLvl];
    if (flipped) want = 0;               // asleep face-down: AMOLED fully off
    else if (!attention) {
      int32_t idleFor = (int32_t)(now - lastActive);
      if      (idleFor > 20 * 60000) want = BRIGHT_LOW;
      else if (idleFor >  8 * 60000) want = BRIGHT_DIM;
    }
    if (want != curBright) { curBright = want; gfx->setBrightness(want); }
  }

  // render ~30 fps
  static uint32_t lastFrame = 0;
  if (now - lastFrame >= 33) {
    lastFrame = now;

    // asleep face-down: paint one black frame, then let the panel rest
    static bool sleepBlanked = false;
    if (flipped) {
      if (!sleepBlanked) {
        canvas->fillScreen(0);
        canvas->flush();
        sleepBlanked = true;
      }
      delay(2);
      return;
    }
    sleepBlanked = false;

    // orientation router: non-face pages render rotated so they stay upright
    // for the viewer (if a rotation comes out mirrored/wrong, use `orient`
    // instead of `(4 - orient) & 3` below)
    uint8_t pg = demoStart ? PAGE_FACE : PAGE_FOR_ORIENT[orient];
    uint8_t rotSec = orient;
    // pickup binding (doc/06 screen-sovereignty rules): while confirmed
    // in-hand an explicit binding shows its page rotated by live gravity so
    // it faces the holder; follow — and smart, now an alias — stays with the
    // orientation page, which tracks deliberate turns even mid-hold (stable-
    // sector window, imuPoll). Shake-summon pulls the face in; a later turn
    // is a newer intent and dismisses it (imuPoll clears summonFace).
    uint8_t effBind = pickupBind == BIND_SMART ? BIND_FOLLOW : pickupBind;
    if (held && summonFace) effBind = BIND_FACE;
    // 钉住 (doc/06; any placement page since 2026-09-22): an explicit finger
    // outranks gravity AND the binding — the pinned page stays on the desk and
    // in the hand alike, only rotated so it reads upright (live sector in
    // hand, placement sector on the desk, same as the needs_you grab below).
    // Applied BEFORE the strip resets below, so a pinned clock strip keeps
    // the sub-page you swiped to even while the board lies on another edge.
    // 唤宠 (shake in hand) still wins for its moment and hands back.
    if (pinned && !demoStart) {
      pg = pinnedPage;
      rotSec = held ? gravSector : orient;
    }
    bool bound = held && !demoStart && effBind != BIND_FOLLOW && (!pinned || summonFace);
    if (bound) {
      rotSec = gravSector;
      pg = effBind == BIND_FACE      ? PAGE_FACE
           : effBind == BIND_ALMANAC ? PAGE_CALENDAR
                                     : PAGE_CLOCK;
    } else {
      if (pg != PAGE_CALENDAR) almanacView = !langEn();  // re-enter on the almanac (English: calendar)
      // Re-enter the clock orientation in the middle of its three-page strip
      // — unless the board itself is playing, in which case you turned it
      // over to control what you are hearing (playerPlaying()
      // is the wave-1 stub and always says no).
      if (pg != PAGE_CLOCK) clockView = playerPlaying() ? CV_POD : CV_CLOCK;
    }
    // needs_you is the only state allowed to grab the screen (doc/06): the
    // selected seat raising its hand pulls the pet page in — desk or hand,
    // any binding — until it's answered. Seat auto-switch (edge-triggered,
    // AUTO_FOCUS_NEEDS_YOU) brings fresh hands to `selected`; if the user
    // deliberately swiped away, their intent wins and there is no grab. A
    // 2 s linger after the answer keeps the decision toast readable.
    {
      static uint32_t needsShowUntil = 0;
      if (agentStates[selected] == ST_NEEDS_YOU) needsShowUntil = now + 2000;
      if (!demoStart && (int32_t)(needsShowUntil - now) > 0) {
        pg = PAGE_FACE;
        rotSec = held ? gravSector : orient;
      }
    }
    // The claim card seizes the screen like needs_you: someone just clicked
    // 「让板子连这台」 on a Mac and is walking over.
    if (!demoStart && claimShowing()) {
      pg = PAGE_FACE;
      rotSec = held ? gravSector : orient;
    }
    // Debug view lock (host {"t":"view"}, /test/view): show any page for a
    // few seconds so /test/shot can photograph it without turning the board
    // over. Outranks the bindings and the needs_you grab — that is the point
    // — and expires by itself.
    bool viewLock = (int32_t)(viewUntil - now) > 0;
    static bool viewWasLocked = false;
    if (viewWasLocked && !viewLock) {      // lock over: hand the pages back
      clockView = viewSavedCv; almanacView = viewSavedAlm;
    }
    viewWasLocked = viewLock;
    if (viewLock && !demoStart) {
      pg = viewPg;
      rotSec = held ? gravSector : orient;
    }
    shownPage = pg;       // the touch layer keys gestures off what is really
    shownRot  = rotSec;   // on screen (and the rotation it was drawn in)
    if (viewLock && pg == PAGE_CLOCK) clockView = viewSub;   // so swipes resume from here
    // 钉住 名牌: 1.5 s over whatever page is up, in that page's rotation, so
    // the answer to "what did the hold just do" reads the same everywhere.
    int32_t pleft = (int32_t)(toastUntil - now);
    float pinK = pleft <= 0 ? 0.0f : (pleft < 400 ? pleft / 400.0f : 1.0f);
    const char* pinLabel = toastText;
    if (pg != PAGE_FACE) {
      canvas->setRotation((4 - rotSec) & 3);
      uint8_t cv = viewLock ? viewSub : bound ? CV_CLOCK : clockView;
      bool wantAlm = viewLock ? viewSub != 0 : (bound ? true : almanacView);
      if (pg == PAGE_CLOCK) {
        // clock is home for this orientation; 播客 (right) and 当前播放 (left)
        // are its two flip sides, one renderer with the page's own source
        if (cv != CV_CLOCK) {
          int32_t vleft = (int32_t)(volShowUntil - now);
          float volK = vleft <= 0 ? 0.0f : (vleft < 300 ? vleft / 300.0f : 1.0f);
          drawPlayPage(canvas, now, PAGE_ACCENT, cv == CV_NP ? PSRC_MAC : PSRC_SD,
                       audioVolume(), volK);
        } else {
          drawClockPage(canvas, now, PAGE_ACCENT);
        }
      } else {
        // almanac is home for this orientation; calendar is the flip side
        // (and the fallback until the host has pushed today's data)
        if (wantAlm && almanacValid()) drawAlmanacPage(canvas, now);
        else                           drawCalendarPage(canvas, now, PAGE_ACCENT);
      }
      // the same corner pin the face wears: seat color for 10 s, then grey
      if (pinned) drawPagePin(canvas, (int32_t)(pinLitUntil - now) > 0, AGENTS[selected].color);
      // low battery: the face's corner icon, bottom-left here (pages.cpp)
      drawPageBatt(canvas, battFakeOn() ? battFakePct
                           : (pmuOk && pmu.isBatteryConnect()) ? pmu.getBatteryPercent()
                           : -1, now);
      int toastCy = pg == PAGE_CALENDAR ? (wantAlm && almanacValid() ? 286 : 368)
                  : (cv == CV_CLOCK ? (langEn() ? 296 : 300) : 368);   // en panel top is 320
      drawPillToast(canvas, pinLabel, pinK, toastCy);
      canvas->setRotation(0);
      canvas->flush();
      delay(2);
      return;
    }

    faceSetSkin(skinBy[selected]);   // each seat wears its own pet
    FaceFrame f = {};
    f.agentIdx  = selected;
    f.st        = (AgentState)agentStates[selected];
    f.offline   = !(hostUp || bleConnected());
    f.listening = listening;
    f.surprised = now < surprisedUntil;
    f.nudge     = pickedUp || (int32_t)(nudgeUntil - now) > 0;
    f.dizzy     = (int32_t)(dizzyUntil - now) > 0;
    f.petting   = (int32_t)(petUntil - now) > 0;
    f.eating    = (int32_t)(eatUntil - now) > 0;
    f.burp      = (int32_t)(burpUntil - now) > 0;
    f.bored     = (int32_t)(boredUntil - now) > 0;
    f.stretch   = (int32_t)(stretchUntil - now) > 0;
    for (int i = 0; i < N_AGENTS; i++) f.agentStates[i] = agentStates[i];
    // Claude 多会话: with ≥2 sessions on the seat on screen
    // the face, the bottom state word, this seat's dot, the approve bubble and
    // the pinned toast all follow the CURRENT session. Only this frame's copy
    // changes: the screen grab above, the seat auto-switch and the chirps keep
    // reading the host-merged agentStates[].
    if (sessActive()) {
      uint8_t es = sessList[sessCur].st;
      f.st = (AgentState)es;
      f.agentStates[selected] = es;
      f.sessN     = sessN;
      f.sessCur   = sessCur;
      f.sessTitle = sessLabel[sessCur];
      f.sessQueue = sessQueued();
      f.sessDir   = sessDir;
      f.sessLit   = (int32_t)(sessLitUntil - now) > 0 ? sessLit : 0;
    }
    f.sessGen = sessGen;
    f.showOff = showOffSeats;
    f.wifiUp = WiFi.status() == WL_CONNECTED;
    f.hostUp = hostUp || bleConnected();
    f.battPct = -1;
    if (battFakeOn()) f.battPct = battFakePct;
    else if (pmuOk && pmu.isBatteryConnect()) f.battPct = pmu.getBatteryPercent();
    f.volume = audioVolume();
    // 眼神追声: eyes drift toward the last confident sound for
    // ~1.5 s, then ease back. Board axis (+ = MIC1, the right edge keys-up)
    // mapped into the sector the face is drawn for, so a sound from the
    // desk's right reads "right" whichever way the pet is turned.
    {
      GazeEst g = audioGaze();
      float target = 0;
      if ((int32_t)(gazeForceUntil - now) > 0) target = gazeForceAz;
      else if (gazeOn && g.n && (int32_t)(now - g.ms) < 1500) {
        float a = g.az, m = fabsf(a);        // deadband: straight ahead is no look
        m = m < 0.12f ? 0 : (m - 0.12f) / 0.88f;
        target = a < 0 ? -m : m;
      }
      lookAz += (target - lookAz) * (target != 0 ? 0.35f : 0.06f);
      if (fabsf(lookAz) < 0.01f) lookAz = 0;
      float lx = 0, ly = 0;
      switch (rotSec & 3) {
        case 0: lx =  lookAz; break;   // keys up: MIC1 on the viewer's right
        case 2: lx = -lookAz; break;
        case 1: ly =  lookAz; break;   // right edge (MIC1) down: sound from below
        case 3: ly = -lookAz; break;
      }
      f.lookX = (int8_t)lroundf(lx * 32);
      f.lookY = (int8_t)lroundf(ly * 10);
    }
    // corner pin: bright in the seat color right after locking, then a plain
    // grey reminder; unpinned it only surfaces in the hand (doc/06)
    f.pinVis = pinned ? ((int32_t)(pinLitUntil - now) > 0 ? 3 : 2)
                      : (pickedUp ? 1 : 0);
    {   // volume overlay: full for 1.1 s, then a 400 ms fade-out
      int32_t left = (int32_t)(volShowUntil - now);
      f.volK = left <= 0 ? 0.0f : (left < 400 ? left / 400.0f : 1.0f);
    }
    {   // send bubble: hold ~5.6 s, then a 400 ms fade-out
      int32_t left = (int32_t)(sendUntil - now);
      f.sendK = left <= 0 ? 0.0f : (left < 400 ? left / 400.0f : 1.0f);
    }
    {   // settings card: fade + live status line (link / IP / batt / fw)
      int32_t left = (int32_t)(setCardUntil - now);
      f.setK = left <= 0 ? 0.0f : (left < 400 ? left / 400.0f : 1.0f);
      f.setBright = brightLvl;
      for (int i = 0; i < N_AGENTS; i++) f.skinBy[i] = skinBy[i];
      if (f.setK > 0.0f) {
        const char* link = bleConnected() ? "BLE"
                           : (sock.connected() ? "TCP" : "--");
        char bat[12];
        if (f.battPct >= 0)
          snprintf(bat, sizeof(bat), "%d%%%s", f.battPct,
                   powState == 1 ? " CHG" : (powState == 2 ? " FULL" : ""));
        else
          snprintf(bat, sizeof(bat), "--");
        if (WiFi.status() == WL_CONNECTED)
          snprintf(f.setStatus, sizeof(f.setStatus), "%s / %s / %s",
                   link, WiFi.localIP().toString().c_str(), bat);
        else
          snprintf(f.setStatus, sizeof(f.setStatus), "%s / no wifi / %s",
                   link, bat);
        char sd[24];
        sdStatus(sd, sizeof(sd));
        snprintf(f.setStatus2, sizeof(f.setStatus2), "%s / %s", sd, FW_VERSION);
      }
    }
    {   // growth profile card, same fade curve as the volume overlay
      int32_t left = (int32_t)(profUntil - now);
      f.profK = left <= 0 ? 0.0f : (left < 400 ? left / 400.0f : 1.0f);
      uint32_t xp = statDone * 10 + statPets * 2 + statDays * 80;
      uint8_t lv = 1;
      while (xp >= (uint32_t)lv * lv * 100 && lv < 99) lv++;
      uint32_t lo = (uint32_t)(lv - 1) * (lv - 1) * 100;
      uint32_t hi = (uint32_t)lv * lv * 100;
      f.level    = lv;
      f.xpPct    = (uint8_t)((xp - lo) * 100 / (hi - lo));
      f.statDone = (uint16_t)(statDone > 65535 ? 65535 : statDone);
      f.statDays = (uint16_t)(statDays > 65535 ? 65535 : statDays);
      f.statPets = (uint16_t)(statPets > 65535 ? 65535 : statPets);
    }
    // demo: full tour (6 s per face) or one fixed face for 20 s
    int32_t demoLen = demoFace >= 0 ? 20000 : 14 * 6000;
    if (demoStart && (int32_t)(now - demoStart) < demoLen) {
      int step = demoFace >= 0 ? demoFace : (int)((now - demoStart) / 6000);
      f.offline = f.listening = f.surprised = f.nudge = false;
      f.dizzy = f.petting = f.eating = f.burp = f.bored = f.stretch = false;
      f.sendK = 0;
      f.setK = 0;
      f.sessN = 0;          // the tour shows faces, not sessions
      switch (step) {
        case 0: f.st = ST_WORKING;   break;
        case 1: f.st = ST_NEEDS_YOU; break;
        case 2: f.st = ST_DONE;      break;
        case 3: f.st = ST_IDLE;      break;
        case 4: f.st = ST_OFF;       break;
        case 5: f.listening = true;  break;
        case 6: f.offline = true;    break;
        case 7: f.surprised = true;  break;
        case 8:  f.dizzy = true;     break;
        case 9:  f.petting = true;   break;
        case 10: f.eating = true;    break;
        case 11: f.burp = true;      break;
        case 12: f.bored = true;     break;
        case 13: f.stretch = true;   break;
      }
    } else demoStart = 0;
    if (claimShowing()) {   // claim card: modal, the pet asks
      int32_t in = (int32_t)(now - claimAt);
      float k = in < 250 ? in / 250.0f : 1.0f;
      if (!claimOn) {       // outro: accepted holds 1 s then fades 400 ms, declined fades
        int32_t out = (int32_t)(now - claimResAt) - (claimAccepted ? 1000 : 0);
        if (out > 0) k *= 1.0f - out / 400.0f;
      }
      f.claimK    = k < 0 ? 0 : k;
      f.claimLeft = claimOn ? (float)(int32_t)(claimUntil - now) / CLAIM_ASK_MS : 0.0f;
      if (f.claimLeft < 0) f.claimLeft = 0;
      f.claimDone = !claimOn && claimAccepted;
      f.claimName = claimName; f.claimSub = claimSub;
      f.st = f.claimDone ? ST_DONE : ST_NEEDS_YOU;
      f.offline = f.listening = f.nudge = f.petting = f.bored = f.stretch = false;
      f.sendK = f.profK = f.setK = f.volK = 0;
      f.sessN = 0;          // modal: no session row floating above the card
    }
    // a face shown away from its home orientation still faces the viewer:
    // grabbed from a side orientation (bound) or pulled in by needs_you on
    // a desk-placed side page (doc/06). rotSec is 0 on the plain face page,
    // so this is a no-op there.
    canvas->setRotation((4 - rotSec) & 3);
    f.hideBubble = toastOnBubble && pinK > 0.0f;
    faceRender(canvas, f, now);
    // the 名牌 rides on top of any page; mac_approve sits where the bubble was
    drawPillToast(canvas, pinLabel, pinK, f.hideBubble ? 341 : 368);
    canvas->setRotation(0);
    canvas->flush();
  }
  delay(2);
}
