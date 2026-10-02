// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
// BLE transport — NimBLE server exposing a Nordic UART Service (NUS).
// RX chunks are reassembled into newline-delimited JSON lines and queued;
// the main loop drains the queue so all app state stays single-threaded.
//
// Two centrals may be connected at once: the owner Mac,
// and a second one that came to claim the board or to learn who owns it.
// Each link keeps its own reassembly buffer — two Macs writing at once would
// otherwise splice their chunks into one line.
#include "ble.h"
#include <NimBLEDevice.h>
#include <vector>
#include <mutex>

static const char* NUS_SVC = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E";
static const char* NUS_RX  = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"; // central -> board
static const char* NUS_TX  = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"; // board -> central (notify)

static const int      BLE_MAX_LINKS     = 2;       // owner + one visitor
static const uint32_t BLE_UNTRUSTED_MS  = 15000;   // a visitor that neither claims nor leaves
static const size_t   RX_LINE_MAX = 4096;          // same cap as main.cpp's TCP/serial lines

struct Link {
  uint16_t h = BLE_NO_LINK;
  bool     sub = false;
  bool     trusted = false;
  bool     ovf = false;          // current line already too long → drop it whole
  bool     hold = false;         // claim card up for this link: exempt from the visitor timeout
  uint32_t since = 0;
  uint32_t kickAt = 0;           // 0 = no kick pending
  String   acc;
};

static NimBLECharacteristic* txChar = nullptr;
static std::mutex qMutex;                 // guards links[], rxLines, owner flags
static Link links[BLE_MAX_LINKS + 1];     // +1: the stack allows 3, we kick the extra
static std::vector<std::pair<String, uint16_t>> rxLines;
static bool     ownerSet = false;
static uint32_t ownerDigest = 0;
static volatile bool advDirty = true;     // advertising data or mode needs a restart

static Link* findLink(uint16_t h) {       // caller holds qMutex
  for (auto& l : links) if (l.h == h) return &l;
  return nullptr;
}
static bool trustedLocked(const Link& l) { return !ownerSet || l.trusted; }

class SrvCb : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer*, NimBLEConnInfo& ci) override {
    std::lock_guard<std::mutex> g(qMutex);
    Link* l = findLink(BLE_NO_LINK);
    if (l) { *l = Link(); l->h = ci.getConnHandle(); l->since = millis(); }
    advDirty = true;
    Serial.printf("ble: central connected (h=%u)\n", ci.getConnHandle());
  }
  void onDisconnect(NimBLEServer*, NimBLEConnInfo& ci, int reason) override {
    std::lock_guard<std::mutex> g(qMutex);
    Link* l = findLink(ci.getConnHandle());
    if (l) *l = Link();
    advDirty = true;
    Serial.printf("ble: disconnected (h=%u, %d)\n", ci.getConnHandle(), reason);
  }
};

class TxCb : public NimBLECharacteristicCallbacks {
  void onSubscribe(NimBLECharacteristic*, NimBLEConnInfo& ci, uint16_t v) override {
    std::lock_guard<std::mutex> g(qMutex);
    Link* l = findLink(ci.getConnHandle());
    if (l) l->sub = v != 0;
    Serial.printf("ble: subscribe=%u (h=%u)\n", v, ci.getConnHandle());
  }
};

class RxCb : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* c, NimBLEConnInfo& ci) override {
    NimBLEAttValue v = c->getValue();
    std::lock_guard<std::mutex> g(qMutex);
    Link* l = findLink(ci.getConnHandle());
    if (!l) return;
    for (size_t i = 0; i < v.length(); i++) {
      char ch = (char)v.data()[i];
      if (ch == '\n') {
        if (l->ovf) Serial.printf("ble: rx line > %u bytes dropped\n", (unsigned)RX_LINE_MAX);
        else if (l->acc.length()) rxLines.push_back({l->acc, l->h});
        l->acc = ""; l->ovf = false;
      } else if (l->ovf || l->acc.length() >= RX_LINE_MAX) {
        l->ovf = true; l->acc = "";
      } else {
        l->acc += ch;
      }
    }
  }
};

static NimBLEUUID svcUuid;

// Primary PDU: flags + 128-bit NUS UUID + manufacturer data (0xFFFF test id,
// 'A', version 1, owner digest LE; digest 0 = no owner) = 30 of 31 bytes.
// The name rides in the scan response, as before.
static void advApply(int nLinks) {
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->stop();
  if (nLinks >= BLE_MAX_LINKS) return;          // full: nobody else can come in anyway
  NimBLEAdvertisementData ad, sr;
  ad.setFlags(BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP);
  ad.addServiceUUID(svcUuid);
  uint32_t d = ownerSet ? ownerDigest : 0;
  uint8_t mfg[8] = {0xFF, 0xFF, 'A', 1, (uint8_t)d, (uint8_t)(d >> 8), (uint8_t)(d >> 16), (uint8_t)(d >> 24)};
  ad.setManufacturerData(mfg, sizeof(mfg));
  sr.setName("AgentPet");
  adv->setAdvertisementData(ad);
  adv->setScanResponseData(sr);
  // Nobody linked: fast, so a Mac finds a rebooted board in a second.
  // Linked: 1 s, only so another Mac can see who owns it and come to claim.
  uint16_t itv = nLinks == 0 ? 48 : 1600;       // 0.625 ms units: 30 ms / 1 s
  adv->setMinInterval(itv);
  adv->setMaxInterval(nLinks == 0 ? 80 : 1600);
  bool ok = adv->start();
  Serial.printf("ble: advertising %s (links=%d, owner=%08lx)\n",
                ok ? (nLinks ? "slow" : "fast") : "FAILED", nLinks, (unsigned long)d);
}

bool bleInit() {
  NimBLEDevice::init("AgentPet");
  NimBLEDevice::setMTU(247);
  NimBLEServer* srv = NimBLEDevice::createServer();
  static SrvCb scb;
  srv->setCallbacks(&scb);
  srv->advertiseOnDisconnect(false);            // bleTick decides, per link count

  NimBLEService* svc = srv->createService(NUS_SVC);
  txChar = svc->createCharacteristic(NUS_TX, NIMBLE_PROPERTY::NOTIFY);
  static TxCb tcb;
  txChar->setCallbacks(&tcb);
  NimBLECharacteristic* rx = svc->createCharacteristic(
      NUS_RX, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
  static RxCb rcb;
  rx->setCallbacks(&rcb);
  svc->start();
  svcUuid = svc->getUUID();

  advDirty = false;
  advApply(0);
  return NimBLEDevice::getAdvertising()->isAdvertising();
}

int bleLinkCount() {
  std::lock_guard<std::mutex> g(qMutex);
  int n = 0;
  for (auto& l : links) if (l.h != BLE_NO_LINK) n++;
  return n;
}

void bleTick() {
  uint32_t now = millis();
  uint16_t kick[BLE_MAX_LINKS + 1];
  int nk = 0, n = 0;
  {
    std::lock_guard<std::mutex> g(qMutex);
    for (auto& l : links) {
      if (l.h == BLE_NO_LINK) continue;
      n++;
      bool due = l.kickAt && (int32_t)(now - l.kickAt) >= 0;
      bool stale = ownerSet && !l.trusted && !l.kickAt && !l.hold &&
                   (int32_t)(now - l.since) > (int32_t)BLE_UNTRUSTED_MS;
      if (due || stale || n > BLE_MAX_LINKS) { kick[nk++] = l.h; l.kickAt = 0; l.since = now; }
    }
  }
  NimBLEServer* srv = NimBLEDevice::getServer();
  for (int i = 0; i < nk; i++) {
    Serial.printf("ble: dropping link h=%u\n", kick[i]);
    if (srv) srv->disconnect(kick[i]);
  }
  // The stack stops advertising on every connect; a failed start or a lost
  // controller state shows up as "not advertising while there is room".
  bool want = n < BLE_MAX_LINKS;
  if (advDirty || want != NimBLEDevice::getAdvertising()->isAdvertising()) {
    static uint32_t lastApply = 0;
    if (advDirty || (int32_t)(now - lastApply) > 2000) {
      advDirty = false; lastApply = now;
      advApply(n);
    }
  }
}

bool bleConnected() {
  std::lock_guard<std::mutex> g(qMutex);
  for (auto& l : links) if (l.h != BLE_NO_LINK && l.sub && trustedLocked(l)) return true;
  return false;
}

bool bleIsTrusted(uint16_t h) {
  std::lock_guard<std::mutex> g(qMutex);
  Link* l = findLink(h);
  return l && trustedLocked(*l);
}

void bleTrust(uint16_t h) {
  std::lock_guard<std::mutex> g(qMutex);
  Link* l = findLink(h);
  if (l) { l->trusted = true; l->kickAt = 0; }
}

void bleKick(uint16_t h, uint32_t afterMs) {
  std::lock_guard<std::mutex> g(qMutex);
  Link* l = findLink(h);
  if (l) { l->kickAt = millis() + (afterMs ? afterMs : 1); l->trusted = false; }
}

void bleHold(uint16_t h, bool on) {
  std::lock_guard<std::mutex> g(qMutex);
  Link* l = findLink(h);
  if (l) { l->hold = on; if (!on) l->since = millis(); }
}

bool bleLinked(uint16_t h) {
  std::lock_guard<std::mutex> g(qMutex);
  return h != BLE_NO_LINK && findLink(h) != nullptr;
}

void bleSetOwner(bool owned, uint32_t digest) {
  std::lock_guard<std::mutex> g(qMutex);
  if (owned == ownerSet && digest == ownerDigest) return;
  ownerSet = owned; ownerDigest = digest;
  advDirty = true;
}

bool blePopLine(String& out, uint16_t* link) {
  std::lock_guard<std::mutex> g(qMutex);
  if (rxLines.empty()) return false;
  out = rxLines.front().first;
  if (link) *link = rxLines.front().second;
  rxLines.erase(rxLines.begin());
  return true;
}

// One line at a time on the notify pipe: a second task (bletest today, the
// mic sender tomorrow) chunking its own line in between ours would splice
// the two on the host. Hold the lock for the whole line.
static std::mutex txMutex;
static uint32_t txDropped = 0;        // lines abandoned after the retry budget
void bleTxLock()   { txMutex.lock(); }
void bleTxUnlock() { txMutex.unlock(); }

static bool subscribedTo(uint16_t h) {
  std::lock_guard<std::mutex> g(qMutex);
  Link* l = findLink(h);
  return l && l->sub;
}

static void sendTo(uint16_t h, const String& msg) {   // caller holds txMutex
  const size_t CH = 100;               // safe under negotiated MTU
  for (size_t i = 0; i < msg.length(); i += CH) {
    size_t n = min(CH, msg.length() - i);
    // notify() fails with ENOMEM while the mbuf pool is full (right after a
    // burst from another sender). Dropping the chunk silently spliced lines on
    // the host (the bletest summary went missing, 2026-09-05) — wait it out.
    int tries = 0;
    while (!txChar->notify((const uint8_t*)(msg.c_str() + i), n, h)) {
      if (++tries > 100 || !subscribedTo(h)) { txDropped++; return; }   // ~200 ms: link is gone
      delay(2);
    }
    if (msg.length() > CH) delay(3);   // pace multi-chunk bursts
  }
}

void bleSendLine(const String& line) {
  if (!txChar) return;
  uint16_t hs[BLE_MAX_LINKS + 1];
  int n = 0;
  {
    std::lock_guard<std::mutex> g(qMutex);
    for (auto& l : links) if (l.h != BLE_NO_LINK && l.sub && trustedLocked(l)) hs[n++] = l.h;
  }
  if (!n) return;
  std::lock_guard<std::mutex> g(txMutex);
  String msg = line + "\n";
  for (int i = 0; i < n; i++) sendTo(hs[i], msg);
}

void bleSendLineTo(uint16_t h, const String& line) {
  if (!txChar || !subscribedTo(h)) return;
  std::lock_guard<std::mutex> g(txMutex);
  sendTo(h, line + "\n");
}

void bleKickOthers(uint16_t keep, const String& line, uint32_t afterMs) {
  uint16_t hs[BLE_MAX_LINKS + 1];
  int n = 0;
  {
    std::lock_guard<std::mutex> g(qMutex);
    for (auto& l : links) if (l.h != BLE_NO_LINK && l.h != keep) hs[n++] = l.h;
  }
  for (int i = 0; i < n; i++) {
    if (line.length()) bleSendLineTo(hs[i], line);
    bleKick(hs[i], afterMs);
  }
}

uint32_t bleTxDropped() { return txDropped; }

static uint16_t firstTrusted() {
  std::lock_guard<std::mutex> g(qMutex);
  for (auto& l : links) if (l.h != BLE_NO_LINK && l.sub && trustedLocked(l)) return l.h;
  return BLE_NO_LINK;
}

size_t bleSendRaw(const uint8_t* p, size_t n, size_t chunk) {
  uint16_t h = firstTrusted();
  if (h == BLE_NO_LINK || !txChar) return 0;
  if (chunk < 20) chunk = 20;
  size_t sent = 0;
  while (sent < n) {
    size_t c = min(chunk, n - sent);
    if (!txChar->notify(p + sent, c, h)) break;   // ENOMEM: caller waits and retries
    sent += c;
  }
  return sent;
}

BleLinkInfo bleLinkInfo() {
  BleLinkInfo li = {0, 0, 0, 0};
  NimBLEServer* srv = NimBLEDevice::getServer();
  uint16_t h = firstTrusted();
  if (!srv || h == BLE_NO_LINK) return li;
  NimBLEConnInfo ci = srv->getPeerInfoByHandle(h);
  li.mtu = ci.getMTU(); li.itvl = ci.getConnInterval();
  li.latency = ci.getConnLatency(); li.timeout = ci.getConnTimeout();
  return li;
}

bool bleRequestInterval(uint16_t itvlUnits) {
  NimBLEServer* srv = NimBLEDevice::getServer();
  uint16_t h = firstTrusted();
  if (!srv || h == BLE_NO_LINK) return false;
  // macOS honours 15 ms (12 units) as its floor; supervision timeout 4 s
  srv->updateConnParams(h, itvlUnits, itvlUnits, 0, 400);
  return true;
}
