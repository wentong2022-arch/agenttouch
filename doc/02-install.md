# Install

English · [简体中文](02-install.zh-CN.md)

From a bare board to a working desk pet: the Mac side, the first flash, pairing, Wi-Fi and the card fonts. About 20 minutes, most of it waiting for the first build.

Prefer to hand this to a coding agent? Point it at [`host/agent_install.md`](../host/agent_install.md); it follows the same steps and stops whenever it needs you.

## What you need

| Item | Notes |
|---|---|
| Board | Waveshare [ESP32-S3-Touch-AMOLED-2.16](https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-2.16) |
| microSD card | **FAT32** (cards up to 32 GB usually are). Fonts, the almanac and podcasts live on it. exFAT does not mount. |
| USB-C data cable | First flash only. A charge-only cable shows no serial port. |
| Mac | macOS with the Xcode command line tools (`xcode-select --install`) |
| PlatformIO Core | `brew install platformio`, or `pip3 install --user platformio` |
| Wi-Fi | 2.4 GHz. Optional but recommended; the board cannot see 5 GHz networks. |

## 1. The Mac side (host)

```bash
git clone https://github.com/wentong2022-arch/agenttouch.git && cd agenttouch
host/setup.sh --check     # optional: report what it would change
host/setup.sh             # install; safe to re-run
```

`setup.sh` installs:

- the Python dependencies for `/usr/bin/python3`
- `~/Applications/AgentPetHost.app`, a tiny wrapper that lets the host use Bluetooth
- a LaunchAgent that starts the host at login, and the `pet` command
- the Claude Code hooks and the Codex hooks

It asks whether to install **Pillow**. Say yes: Pillow bakes the Chinese glyphs from this Mac's own system fonts, and both the firmware build and the card fonts need it. Without a terminal (for example when an agent runs it), use `host/setup.sh --pillow`.

It also lets `/usr/bin/python3` through the firewall, so the board can reach the Mac over Wi-Fi; that step asks for your admin password.

It ends with a short list that only you can do:

1. System Settings → Privacy & Security → **Bluetooth** → add `~/Applications/AgentPetHost.app`.
2. Same place → **Accessibility** → add `AgentPetHost.app` (needed to raise windows and press keys).
3. Trust the Codex hook the first time Codex asks, if you use Codex.

Run without a terminal, it does not ask for passwords; the firewall commands then appear in this list too.

Then restart the host and check it:

```bash
pet restart
pet status
```

## 2. Flash the firmware

Plug the board in with the data cable, card inserted.

```bash
cd firmware
pio run -e amoled216 -t upload
```

The first build downloads the ESP32 toolchain and libraries and takes several minutes. Before compiling, it bakes `firmware/src/almanac_font.h` from your system fonts (about a second). That file is not in git; every Mac bakes its own.

If the upload finds no port, hold the left key (BOOT) while plugging the cable in, then try again.

## 3. Pair the board with this Mac

After the flash the board restarts and advertises over Bluetooth. When the host finds it, the board asks 「连到这台 Mac？」 (connect to this Mac?). **Tap the green 连接 (Connect).** The card is in Chinese because a new board starts in Chinese; switch the language later in the settings page.

- Missed it, or tapped no? Run `pet claim` on the Mac (or click "Use Board on This Mac" in the settings page's Board section), then tap 连接 again.
- The board follows one Mac. To move it to another Mac, do the same there and confirm on the board.

## 4. Wi-Fi for the board

Open <http://127.0.0.1:8788/settings> → **Board**. Pick a network the board found and type its password. The board remembers up to 4 and joins the strongest.

- Bluetooth alone covers status, approve and dictation. Wi-Fi adds the card fonts, almanac, podcasts, speech and cable-free updates.
- The password goes to the board over Bluetooth, unencrypted, once, when you add the network. Do it at home if that matters to you.

## 5. Card fonts (automatic)

The firmware carries only the most common almanac characters. Everything else is read from four font files on the card.

Once the board is on Wi-Fi with a card in, the host bakes whichever fonts are missing (about 10 s) and pushes them (about 16 MB, about 5 minutes). Watch progress in the settings page under **Overview → Card Fonts**, or:

```bash
curl -s http://127.0.0.1:8788/fonts/status
curl -s "http://127.0.0.1:8788/fonts/push?force=1"   # rebake and push all four
```

The glyphs come from macOS's own Songti, Hiragino Sans GB and Menlo. To use other fonts, change the three paths at the top of `host/gen_almanac_font.py`.

## 6. Check

```bash
pet status                                   # host running, board connected
curl -s http://127.0.0.1:8788/state          # "ble": true, "boards": 1, "sd": {...}
```

Next: [map your agents to seats](03-usage.md#seats) and learn the [gestures](03-usage.md).

## Updating

Firmware, without a cable (the host writes it to the card over Wi-Fi; the board flashes its other app slot and restarts, about 50 s):

```bash
cd firmware && pio run -e amoled216 && cp .pio/build/amoled216/firmware.bin ~/.agentpet/fw.bin
curl -s http://127.0.0.1:8788/ota
curl -s "http://127.0.0.1:8788/ota?rollback=1"   # back to the previous version
```

Host: `git pull`, then run `host/setup.sh` again. It copies what changed into `~/.agentpet/` and restarts the host.

## Local overrides

Two files that never go into git:

- `firmware/platformio.local.ini`: when library downloads are slow, point `lib_deps` at local zips (see the comments in `platformio.ini`). Override `lib_deps` only, never `platform`.
- `firmware/src/config.local.h`: compile a Wi-Fi network and your Mac into the firmware (`WIFI_SSID`, `WIFI_PASS`, `HOST_MDNS_NAME`, `HOST_FALLBACK_IP`). Not needed, since Wi-Fi is set in the settings page. After editing it, run `touch firmware/src/config.h`, or it won't recompile.

## Turning it off or removing it

```bash
pet off                                  # stop the host (it starts again at the next login)
/usr/bin/python3 host/install_hooks.py --remove   # remove the Claude Code hooks
```

## Factory firmware

Waveshare's repository has full factory images under `firmware/` (`FactoryOnly-*.bin`). Write one from address 0:

```bash
esptool --port /dev/cu.usbmodem* write-flash 0x0 ESP32-S3-Touch-AMOLED-2.16-FactoryOnly-260318.bin
```
