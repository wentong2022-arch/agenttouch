#!/usr/bin/env python3
# Copyright (c) 2026 yuwentong
# Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
"""Push a file onto the board's microSD over the USB serial line (no Wi-Fi needed).

    /opt/anaconda3/bin/python host/sdpush_usb.py <local file> </agentpet/dst> [-p port] [-w 2]

Same fbeg / fdat / fend protocol as the host's TCP push_file, but USB-CDC has
no flow control (the board's RX queue is 4352 B and the ISR drops bytes once
it is full), so the board acks EVERY fdat line for a serial-origin transfer
and this script keeps at most -w lines (default 2, ~2.8 KB) in flight.
Needs pyserial, hence PlatformIO's python (see CLAUDE.md, host/shot.py).
"""
import argparse
import base64
import glob
import json
import sys
import time
import zlib

import serial


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("-p", "--port", help="serial port (default: /dev/cu.usbmodem*)")
    ap.add_argument("-w", "--window", type=int, default=2, help="fdat lines in flight (<=2 keeps under the 4 KB RX queue)")
    ap.add_argument("-t", "--timeout", type=float, default=8.0, help="seconds to wait for any single ack")
    a = ap.parse_args()

    port = a.port or (sorted(glob.glob("/dev/cu.usbmodem*")) or [None])[0]
    if not port:
        print(json.dumps({"ok": False, "why": "no /dev/cu.usbmodem* — board on USB?"}))
        sys.exit(1)
    with open(a.src, "rb") as f:
        data = f.read()
    crc = zlib.crc32(data) & 0xFFFFFFFF
    rid = int(time.time()) & 0x7FFFFFFF
    window = max(1, min(a.window, 2))

    s = serial.Serial(port, 115200, timeout=0.05)
    s.dtr, s.rts = True, False        # HWCDC only emits with DTR up; this pair does not reset
    s.reset_input_buffer()

    acks = {}                         # seq -> ack dict ("beg"/"end" for the bookends)

    def pump(block_until=None, deadline=None):
        """Read available lines; if block_until is given, wait for that ack key."""
        while True:
            line = s.readline()
            if line:
                try:
                    m = json.loads(line)
                except ValueError:
                    continue          # board log line on the same port
                if not isinstance(m, dict) or m.get("t") != "fack" or m.get("id") != rid:
                    continue
                key = m["seq"] if "seq" in m else ("end" if "bytes" in m or acks.get("beg") else "beg")
                acks[key] = m
                if block_until is not None and key == block_until:
                    return m
            elif block_until is None:
                return None
            if block_until is not None and deadline is not None and time.time() > deadline:
                return None

    t0 = time.time()
    s.write((json.dumps({"t": "fbeg", "id": rid, "path": a.dst, "size": len(data),
                         "crc": "%08x" % crc}) + "\n").encode())
    r = pump("beg", time.time() + a.timeout)
    if not r or not r.get("ok"):
        print(json.dumps({"ok": False, "why": (r or {}).get("why", "no answer to fbeg"), "port": port}))
        sys.exit(1)

    total = (len(data) + 1023) // 1024
    sent = 0
    for seq, i in enumerate(range(0, len(data), 1024)):
        # keep <= window lines in flight: wait for the ack of seq - window
        if seq >= window:
            need = seq - window
            if need not in acks and pump(need, time.time() + a.timeout) is None:
                print(json.dumps({"ok": False, "why": "no ack for chunk %d/%d" % (need, total),
                                  "sent": sent}))
                sys.exit(1)
        s.write((json.dumps({"t": "fdat", "id": rid, "seq": seq,
                             "d": base64.b64encode(data[i:i + 1024]).decode()}) + "\n").encode())
        sent = i + min(1024, len(data) - i)
        if seq % 200 == 199:
            sys.stderr.write("\r%d / %d KB" % (sent // 1024, len(data) // 1024))
    # drain the tail acks before fend so the board's queue is empty
    for need in range(max(0, total - window), total):
        if need not in acks and pump(need, time.time() + a.timeout) is None:
            print(json.dumps({"ok": False, "why": "no ack for chunk %d/%d" % (need, total)}))
            sys.exit(1)
    s.write((json.dumps({"t": "fend", "id": rid}) + "\n").encode())
    r = pump("end", time.time() + 60)
    ms = int((time.time() - t0) * 1000)
    sys.stderr.write("\n")
    if not r or not r.get("ok"):
        print(json.dumps({"ok": False, "why": (r or {}).get("why", "no answer to fend"), "ms": ms}))
        sys.exit(1)
    print(json.dumps({"ok": True, "dst": a.dst, "bytes": len(data), "ms": ms,
                      "kbps": round(len(data) / 1024 / max(ms, 1) * 1000, 1), "via": "usb",
                      "board": {k: v for k, v in r.items() if k not in ("t", "id", "ok")}}))


if __name__ == "__main__":
    main()
