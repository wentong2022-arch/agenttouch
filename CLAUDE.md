# AgentTouch

An ESP32-S3-Touch-AMOLED-2.16 desk pet whose face shows the state of Claude Code, Codex, Qoder, QwenWork (wire id `qoderwork`) and the Qoder desktop app (`forest`). Firmware in `firmware/`, the Mac side ("host") in `host/`. Read `doc/05-architecture.md` for the architecture and protocol, `doc/06-screen-rules.md` for the screen and interaction rules.

## Hard rules

- Run the Mac service with **`/usr/bin/python3`** only. The firewall lets only that binary in; any other Python is blocked and the board cannot connect.
- `firmware/platformio.ini` is the public one (official library names, pinned). When downloads are slow, override `lib_deps` in `firmware/platformio.local.ini` (gitignored), **`lib_deps` only**: a different `platform` line re-downloads the whole toolchain. `config.local.h` (gitignored) holds your own Wi-Fi / Mac; **after editing it, `touch firmware/src/config.h` before building**, or it won't recompile.
- uint32 time comparisons in the firmware are always `(int32_t)(millis() - ts) > N` (no underflow).
- The board writes to TCP **only through `tcpWriteLine` / `tcpWriteLines`** (a single line gets its newline added, a batch must end with a newline, a half-written batch is always finished). Never a bare `sock.print` / `sock.write` / `send()`. **Line lengths always come from `sizeof` / `strlen`, never counted by hand.** **After touching the TCP or BLE send path, run the regression pair**: read `/test/badlines` → `/test/shot?s=1` plus a 10 s dictation session (`/test/mic/start` … `/stop`) → read `/test/badlines` again; the difference must be 0.
- New seats are only ever appended to the end of `AGENTS` (the host builds its report by index).

## How to report a change

Four lines: `Build: PASS/FAIL/NOT RUN`, `Host tests: …`, `Device tests: …`, `Unverified: what still needs the board, an instrument or a person`. **Never call "it compiles" "verified on hardware".** For layout, coordinate or font-size changes, look at a screenshot yourself first (`/test/shot` over Wi-Fi, `host/shot.py` over USB). Host tests at minimum: `/usr/bin/python3 host/test_host.py` (standard library only, no flashing).

## Commands

```bash
cd firmware && pio run -e amoled216 && cp .pio/build/amoled216/firmware.bin ~/.agentpet/fw.bin && curl -s http://127.0.0.1:8788/ota   # cable-free flash: to the card → board flashes its other app slot → back in ~50 s; /ota?rollback=1 = previous version; /state.fw = build / slot
cd firmware && pio run -e amoled216 -t upload        # USB flash (first time, or when OTA is broken)
host/setup.sh [--check]                              # install / repair the host (safe to re-run)
pet status | on | off | restart | log | claim        # host control; claim = make the board follow this Mac (tap 连接 on the board)
cp host/agentpet_host.py host/almanac_bank.json ~/.agentpet/ && launchctl kickstart -k gui/$(id -u)/com.agentpet.host   # apply host code changes (launchd runs the copy in ~/.agentpet/; it cannot read ~/Documents)
/usr/bin/python3 host/test_host.py -v                # host unit tests; run them before changing host logic
open http://127.0.0.1:8788/settings                  # settings page (seats / skins / sound / dictation / board Wi-Fi / pairing / podcasts / host health)
curl -s http://127.0.0.1:8788/state                  # service state (ble:true = Bluetooth up; boards = Wi-Fi TCP boards; sd = card)
curl -s http://127.0.0.1:8788/test/shot              # board screenshot → ~/.agentpet/shots/latest.png (?s=1 = full 480×480; Wi-Fi TCP only)
python host/shot.py [-s 1]                           # the same over USB (needs pyserial)
python host/sdpush_usb.py <local file> </agentpet/dst>   # push a file to the card over USB
curl -s "http://127.0.0.1:8788/sd/push?src=/abs/file&dst=/agentpet/x"   # push a file to the card over Wi-Fi; /sd/ls?path= /sd/cat?path= /sd/rm?path=
curl -s "http://127.0.0.1:8788/fonts/push?force=1"   # card fonts (.afn): the host bakes them from system fonts and pushes them (automatic when the board is on Wi-Fi and fonts are missing; /fonts/status)
/usr/bin/python3 host/gen_almanac_font.py            # rebake the firmware glyph table firmware/src/almanac_font.h (not in git; baked automatically before a build when missing)
curl -s http://127.0.0.1:8788/test/front             # what the host sees as the front app / target app / seat; /test/select/<agent> simulates a swipe to that seat
curl -s http://127.0.0.1:8788/test/faces             # tour every face; /test/face/<name> holds one for 20 s
curl -s "http://127.0.0.1:8788/test/view/np?s=20"    # force a page for s seconds, for /test/shot (np|pod|clock|face|almanac|calendar; s=0 cancels)
curl -s "http://127.0.0.1:8788/test/skin/grok?agent=claude"   # set a skin: 0-5 or classic/kitty/robo/bunny/sprout/grok
curl -s http://127.0.0.1:8788/test/sound/needs       # play a sound; /test/volume/50; /test/say/hello speaks on the board
curl -s "http://127.0.0.1:8788/test/mic/rec?sec=5"   # record 5 s from the board mic → ~/.agentpet/mic/latest.wav; /test/mic/ch/<0|1|2|3> (3 = by placement); /test/mic/link/<auto|tcp|ble>
curl -s http://127.0.0.1:8788/test/almanac           # push and echo today's almanac; /almanac/preview?date=YYYY-MM-DD for any day
curl -s "http://127.0.0.1:8788/audio/add?url=<RSS or audio URL>&n=1"   # podcast to the card; /audio/status /audio/list /audio/rm?id=
curl -s http://127.0.0.1:8788/test/pin/1             # pin the current page band (/test/pin/0 unpins); /test/pickup/<follow|face|clock|almanac>
curl -s http://127.0.0.1:8788/test/lang/en           # switch the board's language for now (the real switch is in the settings page)
curl -s http://127.0.0.1:8788/test/sd                # retry mounting the microSD; /test/rtc for the clock chip
curl -s http://127.0.0.1:8788/test/approve           # simulate tapping approve (raises the app and presses keys!); /test/reject likewise
```

## Using the board

- **Power on**: press the middle key (PWR) once. **Power off**: hold it 3 s → BYE BYE → the PMU cuts power. (A 6 s hardware hold always cuts power; it cannot be disabled and serves as the emergency stop.)
- **Swipe left / right** = switch agent, **up / down** = volume ±10, **tap** = name tag and status lights for 3 s, **double tap** = profile card, **long-press the face 1.5 s** = settings card (skin, brightness, status line).
- **needs_you** shows an Approve bubble: tap = approve, hold ≥ 0.7 s = reject (the host raises the app and presses the key; `approve_keys` / `reject_keys` in `~/.agentpet/config.json` override per app). needs_you is the only state allowed to take over the screen (`doc/06`).
- **Hold the right key to talk**: on the face page you talk to the current seat, on other pages the text lands at the Mac's cursor; release → a Send bubble, tap = Return. The Mac side holds fn for an input method that dictates while fn is held; config `voice_source` picks the board mic or the Mac mic.
- **Switch seats on the board and the Mac's front app follows** (after 1.5 s of no swiping); **click into an agent's app on the Mac and the board follows** (config `follow_front`).
- **Left key**: press = mute, hold 1 s = pin the current page band (stops changing pages with placement).
- **On its side**: left edge (SD slot) down = clock; swipe left = Mac now playing, right = podcasts on the card. Right edge (speaker) down = almanac; swipe up / down = another fortune, tap = read it out. **Face down = sleep**; turn it back or pick it up to wake.
- **Pet things**: rub its face = pat, shake = dizzy, plug in = eat, left alone it sighs, 90 min of non-stop work = break reminder.
- **Battery**: < 10 % sighs, ≤ 5 % powers off; 3 min without known Wi-Fi turns the Wi-Fi radio off, Bluetooth keeps going.
- **Key names**: middle = PWR (readable only through the AXP2101 PWRON interrupt; **never a hold-to-use key**, a long hold always cuts power), right = IO18 (hold to talk), left = BOOT / GPIO0.

## Key facts

- **Only two ports, both on the Mac** (the board is a pure client): 8788 bound to 127.0.0.1 for the Claude / Codex hooks and debugging; 8737 on the LAN for the board's TCP link.
- **Links = BLE first (the board advertises `AgentPet`, Nordic UART service) + Wi-Fi TCP fallback.** On the Mac, bleak must run inside `~/Applications/AgentPetHost.app` (C wrapper `host/agentpet_wrap.c`); that is the only way a launchd service gets the Bluetooth permission.
- **Ownership**: the board follows one owner Mac (stored in NVS) and advertises the owner's digest; other Macs stand by. A new owner starts on the new Mac and is confirmed by tapping 连接 on the board.
- **Wi-Fi is set in the settings page** (pushed to the board over BLE, up to 4 networks, the board joins the strongest). The password crosses BLE unencrypted, once, when it is added.
- State detection: Claude = hooks (`~/.claude/settings.json`, `host/install_hooks.py --remove` uninstalls); Codex = hooks.json + PermissionRequest and the session log; Qoder, QwenWork and the Qoder desktop app = their own local logs and database.
- **The board's USB serial port is two-way**: JSON lines written to it are handled as host → board messages and the replies come back on the serial port (`shot.py` and `sdpush_usb.py` rely on it). **Never write more than 4 KB at once** (the CDC receive queue drops bytes when full).
- Mac now playing needs `media-control` (`brew tap ungive/media-control && brew install media-control`); without it the feature quietly turns off.
- Pins, the case's four edges, IMU axes and microphone channels: `doc/01-hardware.md`.
