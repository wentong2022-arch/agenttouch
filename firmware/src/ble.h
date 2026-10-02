// Copyright (c) 2026 yuwentong
// Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
#pragma once
#include <Arduino.h>

// Nordic-UART-style GATT link. Same newline-JSON protocol as the TCP path.
//
// Up to BLE_MAX_LINKS centrals at once: the owner Mac
// plus one that came to claim or to ask who the owner is. While a board has
// an owner (bleSetOwner(true, ..)) only TRUSTED links carry app traffic;
// the rest may only say hostinfo/claim and are kicked after BLE_UNTRUSTED_MS.
// With no owner every link is trusted — the pre-claim behaviour.
static const uint16_t BLE_NO_LINK = 0xFFFF;
bool bleInit();
bool bleConnected();                 // a trusted central connected AND subscribed to TX
bool blePopLine(String& out, uint16_t* link = nullptr);   // dequeue one received line (thread-safe)
void bleSendLine(const String& line);// chunked notify to every trusted link, '\n' appended
void bleSendLineTo(uint16_t link, const String& line);    // one link only, trusted or not
bool bleIsTrusted(uint16_t link);
void bleTrust(uint16_t link);        // this link is the owner's
void bleKick(uint16_t link, uint32_t afterMs);            // disconnect it (after a reply lands)
void bleKickOthers(uint16_t keep, const String& line, uint32_t afterMs);  // tell the rest, then drop them
void bleSetOwner(bool owned, uint32_t digest);            // advertised owner digest; 0 = no owner
void bleHold(uint16_t link, bool on);                     // a claim waits on a tap: no visitor timeout
bool bleLinked(uint16_t link);
void bleTick();                      // main loop: due kicks, untrusted timeouts, advertising
int  bleLinkCount();

// Link measurement helpers (mic-blackhole branch).
// bleSendRaw pushes up to n bytes as `chunk`-byte notifies with no pacing and
// stops at the first one the stack refuses (mbuf pool full = the real
// backpressure signal); returns bytes accepted. Callers retry the remainder.
// It goes to the first trusted link only (the mic stream has one listener).
size_t bleSendRaw(const uint8_t* p, size_t n, size_t chunk);
void bleTxLock();                  // hold across a multi-call line so other senders can't splice in
void bleTxUnlock();
uint32_t bleTxDropped();           // lines bleSendLine gave up on (mbufs never freed up)
struct BleLinkInfo { uint16_t mtu, itvl, latency, timeout; };   // itvl in 1.25 ms units
BleLinkInfo bleLinkInfo();         // of the first trusted link
bool bleRequestInterval(uint16_t itvlUnits);   // ask that central for a new connection interval
