#!/usr/bin/env python3
# Copyright (c) 2026 yuwentong
# Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
"""Screenshot of the AgentTouch board over USB-CDC — no Wi-Fi, no BLE, no host
service involved (the office case). The board treats any JSON line on its
serial port as a host→board message; sendShot() answers on the same port.

    /opt/anaconda3/bin/python host/shot.py [-s 1|2|4] [-o out.png]

Needs pyserial, which /usr/bin/python3 lacks; PlatformIO's interpreter (the
shebang of `which pio`) has it — the /usr/bin/python3 rule is about the
firewall and does not apply to a serial port. Output goes where /test/shot
puts it: ~/.agentpet/shots/<stamp>.png + latest.png, same JSON summary.
"""
import argparse
import base64
import glob
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    import serial
except ImportError:
    sys.exit("pyserial missing — run with PlatformIO's python: "
             "$(head -1 $(which pio) | cut -c3-) host/shot.py")
from agentpet_host import rgb565_to_png, SHOT_DIR   # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-s", "--step", type=int, default=2,
                    help="downsample step: 1 = 480x480, 2 = 240x240 (default)")
    ap.add_argument("-o", "--out", help="write here instead of ~/.agentpet/shots/")
    ap.add_argument("-p", "--port", help="serial port (default: /dev/cu.usbmodem*)")
    ap.add_argument("-t", "--timeout", type=float, default=20)
    a = ap.parse_args()

    port = a.port or (glob.glob("/dev/cu.usbmodem*") or [None])[0]
    if not port:
        print(json.dumps({"ok": False, "why": "no /dev/cu.usbmodem* — board on USB?"}))
        sys.exit(1)
    s = serial.Serial(port, 115200, timeout=0.5)
    s.dtr, s.rts = True, False        # HWCDC only emits with DTR up; this pair does not reset
    s.reset_input_buffer()

    rid = int(time.time()) & 0x7FFFFFFF
    t0 = time.time()
    s.write((json.dumps({"t": "shot", "id": rid, "s": a.step}) + "\n").encode())
    hdr, parts, fin = None, {}, None
    while time.time() - t0 < a.timeout and fin is None:
        line = s.readline()
        if not line:
            continue
        try:
            m = json.loads(line)
        except ValueError:
            continue                  # a log line on the same port — ignore
        if m.get("id") != rid:
            continue
        t = m.get("t")
        if t == "sbeg":
            hdr = m
        elif t == "sdat":
            parts[m.get("seq")] = m.get("d", "")
        elif t == "sfin":
            fin = m
    ms = int((time.time() - t0) * 1000)

    if fin is None:
        res = {"ok": False, "why": "no sfin in %.0f s" % a.timeout, "port": port,
               "got": len(parts)}
    elif not fin.get("ok"):
        res = {"ok": False, "why": fin.get("why", "board failed")}
    elif hdr is None:
        res = {"ok": False, "why": "sfin without sbeg"}
    else:
        missing = [i for i in range(hdr["n"]) if i not in parts]
        if missing:
            res = {"ok": False, "why": "missing chunks %s" % missing[:5]}
        else:
            raw = b"".join(base64.b64decode(parts[i]) for i in range(hdr["n"]))
            png = rgb565_to_png(raw, hdr["w"], hdr["h"])
            if a.out:
                path = a.out
            else:
                SHOT_DIR.mkdir(parents=True, exist_ok=True)
                path = str(SHOT_DIR / (time.strftime("%Y%m%d-%H%M%S") + ".png"))
                (SHOT_DIR / "latest.png").write_bytes(png)
            with open(path, "wb") as f:
                f.write(png)
            res = {"ok": True, "path": path, "w": hdr["w"], "h": hdr["h"], "bytes": len(raw),
                   "ms": ms, "board_ms": fin.get("ms"), "via": "usb"}
    print(json.dumps(res))
    sys.exit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()
