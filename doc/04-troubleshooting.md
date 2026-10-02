# Troubleshooting

English · [简体中文](04-troubleshooting.zh-CN.md)

Common problems and how to fix them. Start with `pet status`: it tells you whether the host is running and how the board is connected.

## Connection

### The board does not connect (`ble=False`)

- Is the board on? Press the middle key once.
- Does `AgentPetHost.app` have the **Bluetooth** permission? Add it in System Settings → Privacy & Security → Bluetooth (remove an old AgentPetHost entry first), then `pet restart`.
- Is it following another Mac? Run `pet claim`, then tap 连接 (Connect) on the board.
- Lying face down means asleep. Turn it over.

### The board is not in the Mac's Bluetooth settings

Expected. The board does not pair with macOS; the host connects to it directly.

### Bluetooth works but Wi-Fi does not (`boards: 0`)

- Add a network in the settings page under **Board**. The board sees 2.4 GHz networks only.
- The firewall must let `/usr/bin/python3` accept connections: the board reaches the Mac on port 8737. Re-run `host/setup.sh` and check its output.

### The host does not answer on 8788

`pet status`; if it says OFF, run `pet on`. The log is at `/tmp/agentpet_host.log` (`pet log`).

## Agents

### Approve or dictation does nothing, or types into the wrong window

- `AgentPetHost.app` needs the **Accessibility** permission to raise windows and press keys.
- `curl -s http://127.0.0.1:8788/test/front` shows which app the host thinks is in front and which one it would raise. Fix the seat's app in the settings page under **Seats**.
- Dictation needs an input method that dictates while fn is held.

### A Claude Code seat never changes state

The hooks may be missing. Run `host/setup.sh` again; it adds them to `~/.claude/settings.json` when they are not there.

### Codex never shows "needs you"

- Codex asks once whether to trust the new hook: choose trust.
- Codex reviews low-risk requests by itself and approves them without asking you, so no card appears for those.

## Building and flashing

### The build says Pillow is missing

`host/setup.sh --pillow`, or `/usr/bin/python3 -m pip install --user pillow==11.3.0`.

### Library downloads are very slow

Point `lib_deps` at local zips in `firmware/platformio.local.ini` (see the comments in `platformio.ini`). Override `lib_deps` only.

### The upload finds no serial port

Use a data cable, not a charge-only one. Hold the left key (BOOT) while plugging it in, then upload again.

### A cable-free update went wrong

`curl -s "http://127.0.0.1:8788/ota?rollback=1"` goes back to the previous firmware. If the board does not come back at all, flash over USB.

## On the board

### Boxes instead of characters

The card fonts are missing: no card, a card that is not FAT32, or the fonts are still being pushed. Check **Overview → Card Fonts** in the settings page.

### It ignores taps

- Asleep (face down)? Taps don't wake it on purpose; turn it over or pick it up.
- A pinned page does not follow how you hold the board. Hold the left key for 1 s to unpin.

### It froze

Hold the middle key: after 3 s the firmware powers off; if the firmware is stuck, the power chip cuts power after about 6 s anyway. Press it again to start.

## Still stuck?

Open an [issue](https://github.com/wentong2022-arch/agenttouch/issues) with the output of `pet status` and the last lines of `/tmp/agentpet_host.log`.
