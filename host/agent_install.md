# AgentTouch: install guide for an AI coding agent

> This page is written for an AI coding agent (Claude Code, Codex, ...). The user pasted a line such as
> "请按 https://github.com/wentong2022-arch/agenttouch/blob/main/host/agent_install.md 帮我安装 AgentTouch".
> Follow the steps in order. **Talk to the user in the language they wrote to you in.** Stop and wait whenever a step needs their hands.

## 1. What this is

- **AgentTouch** is a small desk screen (Waveshare ESP32-S3-Touch-AMOLED-2.16) that acts as the face of the user's AI coding agents (Claude Code, Codex, Qoder, QwenWork, Qoder desktop): idle / working / needs you / done. Tapping it approves, holding its right key dictates.
- The **host** is a background service on this Mac (LaunchAgent): `http://127.0.0.1:8788` on this Mac only, plus port 8737 on the LAN for the board's Wi-Fi link. It reaches the board over Bluetooth first, Wi-Fi second.
- **You will**: check prerequisites, run the installer, build and flash the firmware over USB, walk the user through pairing, Wi-Fi and the card fonts, then hand over to the seat setup guide. **You will not**: type passwords, grant macOS permissions, or change any code.

## 2. Boundaries (must follow)

1. **Never type a password**: not the Wi-Fi password, not `sudo` / the admin password, not an Apple ID. When a command needs one, give the user the exact command and wait.
2. **Never touch macOS privacy settings** (Bluetooth, Accessibility, firewall), by script or through the TCC database. Tell the user exactly what to click and wait.
3. **Never rebuild or re-sign** `~/Applications/AgentPetHost.app` (no `--rebuild-app` unless the user asks): a new signature silently voids its Bluetooth and Accessibility grants. Only `host/setup.sh` creates it.
4. **Ask before installing anything**: Pillow (`--pillow`), PlatformIO, Homebrew packages (setup.sh itself runs `brew install media-control` when Homebrew is present; say so when you ask). **Ask before flashing the board.**
5. Don't edit `~/.agentpet/*.py` or the repo's code, don't run `pet use`, don't call `/test/approve` or `/test/key/...` (they press keys in the front window), don't run setup.sh with `sudo`.

## 3. Prerequisites (check, then tell the user what is missing)

| Need | Check |
|---|---|
| macOS (the host is macOS-only) | `sw_vers` |
| Xcode command line tools (git, clang, `/usr/bin/python3`) | `xcode-select -p`; if missing, ask the user to run `xcode-select --install` (a GUI installer) and wait |
| The board: Waveshare ESP32-S3-Touch-AMOLED-2.16 | ask |
| A microSD card in the board, **FAT32** (required: Chinese fonts, almanac and podcasts live on it; exFAT does not mount, and cards over 32 GB usually ship as exFAT) | ask |
| A USB-C **data** cable, for the first flash only (charge-only cables show no port) | ask |
| 2.4 GHz Wi-Fi (the board cannot see 5 GHz networks) | ask |

## 4. Steps

### Step 1. Get the code
If the current directory already contains `host/setup.sh`, use it. Otherwise ask where to put it (suggest `~/agenttouch`; any folder works, the host runs from copies in `~/.agentpet/`):
```bash
git clone https://github.com/wentong2022-arch/agenttouch.git ~/agenttouch && cd ~/agenttouch
```

### Step 2. Install the host
```bash
host/setup.sh --check      # reports only, changes nothing
```
Summarize for the user what it would change (lines with 会做) and any ✗. Then ask in one go: run the installer? Install Pillow? (Pillow only bakes the Chinese glyphs from this Mac's own system fonts; no font data ships with the code. The firmware build and the card fonts both need it.)
```bash
host/setup.sh --pillow </dev/null     # user agreed to Pillow
host/setup.sh </dev/null              # user declined Pillow (it then shows up in the manual list)
```
- Safe to re-run; it only changes what is missing. With stdin not a terminal (the `</dev/null` makes sure, even if your shell has one) it never prompts: Pillow is installed only with `--pillow`, and the firewall commands, which need the admin password, are not run but listed for the user.
- The output ends with **还要你手动做** (what only the user can do: Bluetooth and Accessibility for AgentPetHost.app, firewall commands, trusting the Codex hook). **Relay that list verbatim, numbered as printed, and wait until the user says it is done.** Then run `pet restart` and `pet status`.
- The Bluetooth grant plus `pet restart` must happen before step 5: without it the host never sees the board.
- `pet: command not found`: it was linked into a folder not on PATH (setup.sh printed which). Use `~/.agentpet/pet`, and ask before editing `~/.zshrc`.
- Exit status 1 = something under ✗ failed. Quote those lines to the user; pip problems are in `/tmp/agentpet_setup_pip.log`.

### Step 3. PlatformIO
Check `pio --version`. If missing, ask, then:
```bash
brew install platformio            # if Homebrew is installed
pip3 install --user platformio     # otherwise; pio lands in "$(/usr/bin/python3 -m site --user-base)/bin"
```
If `pio` is still not found after the pip route, call it by that full path.

### Step 4. First flash over USB (ask first)
Ask the user to plug the board in with the data cable, card inserted. `ls /dev/cu.usbmodem*` should show a port. Then:
```bash
cd firmware && pio run -e amoled216 -t upload
```
- The first build downloads the ESP32 toolchain and libraries: several minutes. Use a long timeout or run it in the background and poll.
- Before compiling, it bakes `firmware/src/almanac_font.h` from the system fonts (about 1 s). If the build stops because Pillow is missing, go back to the Pillow question in step 2.
- Library downloads very slow: `firmware/platformio.local.ini` may override `lib_deps` with local zips (see the comments in `platformio.ini`). Override `lib_deps` only.
- Later updates need no cable: `pio run -e amoled216 && cp .pio/build/amoled216/firmware.bin ~/.agentpet/fw.bin && curl -s http://127.0.0.1:8788/ota` (over Wi-Fi, about 50 s).

### Step 5. Let the board follow this Mac
After the flash the board restarts and advertises over Bluetooth. When the host finds it, the board shows **「连到这台 Mac？」**: tell the user to tap the green **连接** within 30 s. If they missed it or declined, run `pet claim` (waits up to 60 s while they tap 连接 again; if it answers 扣着在睡觉, the board is lying face-down: flip it over and retry). The board follows one Mac only; the same `pet claim` takes it over from another Mac. Check `curl -s http://127.0.0.1:8788/state` for `"ble": true`.

### Step 6. Wi-Fi for the board
`open http://127.0.0.1:8788/settings` and tell the user: section **板子** (Board) → pick a network the board found, or 添加一个 Wi-Fi, and **type the password themselves**. Up to 4 networks, 2.4 GHz only. The password travels to the board over Bluetooth, unencrypted, once, when it is added. Bluetooth alone already covers status, approve and dictation; Wi-Fi adds card fonts, almanac, podcasts, speech, cable-free updates. Check `/state` for `"boards": 1`.

### Step 7. Card fonts (automatic)
With the board on Wi-Fi and a card in, the host bakes 4 fonts from the system fonts (about 10 s) and pushes them (about 16 MB, about 5 min). Poll every 30 s or so:
```bash
curl -s http://127.0.0.1:8788/fonts/status
```
| `state` | Meaning / what to do |
|---|---|
| `""`, `checking` | not checked yet / reading the card: wait |
| `wait` | `why` = `no TCP board`: the board is not on Wi-Fi yet (step 6); it starts by itself on connect. `card busy`: another transfer is running, try `curl -s http://127.0.0.1:8788/fonts/push` in a minute |
| `nocard` | no readable card: ask the user to insert a FAT32 microSD; it resumes on the next card detect or Wi-Fi connect |
| `nopil` | Pillow missing: ask, then `host/setup.sh --pillow </dev/null`, then `curl -s http://127.0.0.1:8788/fonts/push` |
| `baking` | generating `n` fonts: wait |
| `pushing` | copying `face` (`i` of `n`, `pct` %): wait |
| `ok` | all 4 fonts are on the card: done |
| `error` | relay `why` verbatim; retry once with `/fonts/push` (`/fonts/push?force=1` re-bakes all four) |

### Step 8. Verify
```bash
pet status                                   # host running, product=s3, ble=True, tcp=1
curl -s http://127.0.0.1:8788/state          # "ble": true (Bluetooth), "boards": 1 (Wi-Fi), "sd": {"mb": card size}
curl -s http://127.0.0.1:8788/fonts/status   # "state": "ok"
```

### Step 9. Hand over to the seat setup
```bash
curl -s http://127.0.0.1:8788/agent-setup
```
That is the seat configuration guide (`host/agent_setup.md`, in Chinese), served with a live section about this Mac at the end. Read it and follow it: it maps the agents on this Mac to the board's five seats, using only the endpoints it lists.

## 5. Troubleshooting

- **8788 not answering**: `pet status`; if it says OFF, `pet on`. Log: `tail -80 /tmp/agentpet_host.log`.
- **Board not connecting** (`ble:false`, `boards:0`): is it on (one press of the middle key)? Was Bluetooth granted to AgentPetHost.app, followed by `pet restart`? Is it following another Mac (`pet claim`, then tap 连接)? Face-down = asleep.
- **Upload fails / no port**: data cable? `ls /dev/cu.usbmodem*`. Hold the BOOT key (the left key) while plugging the cable in, then retry the upload.
- **Bluetooth permission**: System Settings → Privacy & Security → Bluetooth → + `~/Applications/AgentPetHost.app` (remove an old AgentPetHost entry first), then `pet restart`.

## 6. Report to the user

A short table, one row per step (prerequisites, code, host install, manual permissions, PlatformIO, firmware flash, pairing, Wi-Fi, card fonts, verification, seat setup) with **done / skipped / waiting on you** and a one-line note. Below it, list what is still waiting on the user.
