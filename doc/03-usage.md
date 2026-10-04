# Using it

English · [简体中文](03-usage.zh-CN.md)

Everyday use: keys, gestures, approving, dictation, the side pages, seats and the settings page. For the exact rules on which page shows when, see [Screen rules](06-screen-rules.md).

## Keys

Hold the board with the screen facing you and the three keys on top.

| Key | Press | Hold |
|---|---|---|
| Left (BOOT) | Mute / unmute | 1 s: pin or unpin the current page |
| Middle (PWR) | Power on; when on, show the name tag and status lights | 3 s: power off |
| Right | | Talk (dictation) while held |

The middle key can only be pressed, never used as a hold-to-talk key: holding it long enough always cuts power in hardware.

## On the face page

| Gesture | What it does |
|---|---|
| Swipe left / right | Switch to the next / previous seat |
| Swipe up / down | Volume up / down |
| Tap | Show the seat's name tag and status lights for 3 s |
| Double tap | Profile card (level and experience); tap again to close |
| Long-press 1.5 s | Settings card: skin, brightness, link and battery status |
| Rub back and forth | It purrs and blushes |
| Shake | It gets dizzy |

## Approving

When an agent asks for permission, its state becomes **needs you**. Whatever the board was showing, it jumps to that agent's face with an **Approve** bubble.

- **Tap** = approve, **hold 0.7 s** = reject.
- The host brings the agent's window to the front, then presses the key for you (Enter / Esc by default; change it per seat in the settings page).
- Once answered, the board goes back to the page it was on.

## Several Claude Code windows

Open as many Claude Code terminals as you like; the Claude seat still shows one face. With two or more, a short row appears at the top of the face, together with the bottom status line (when you tap, and while the session shown is waiting for you): the session's title, the same one Claude puts on the terminal tab (or its folder when there is none), and its place, like `2/4`.

- **Tap the left half of the top strip** = previous session, **right half** = next. It wraps around.
- The face, the **Approve** bubble and dictation all follow the session on the top row: what you see is what you act on. The Mac brings that session's tab to the front 1.5 s after your last tap.
- A permission request in another session moves the row there at once, and its tab comes to the front. When several are waiting they are answered one by one, in the order they asked; the counter turns green while others are queued.
- Typing in a session on the Mac moves the row to it.
- The exact tab is found in Warp and Terminal; iTerm2 and Ghostty are supported but not tested yet. Terminal, iTerm2 and Ghostty ask once whether `AgentPetHost` may control them. In other terminals (VS Code, Cursor, …) only the app can be brought forward, so when two sessions share one of them the board asks you to approve on the Mac instead of guessing the tab.
- Sessions that were already running when you installed or updated AgentTouch are found by their folder; no need to restart them.

## Dictation

Hold the right key and talk. Release, and a **Send** bubble appears for 6 s; tap the screen to press Return.

- On the face page, your words go to the current seat's app. On any other page, they go wherever the Mac's cursor is.
- The Mac side holds the fn key for you, so you need an input method that dictates while fn is held (the author uses WeChat Input).
- By default the Mac's microphone listens. To talk into the board's microphone instead, choose it under **Dictation → Microphone** in the settings page (needs the BlackHole virtual audio driver).

## On its side

Standing upright with the keys on top, the board shows the face page. Stand it on a side edge and it becomes something else. Lying flat with the screen up, it keeps whatever page it was on.

| Placement | Pages | Gestures |
|---|---|---|
| Left edge down (SD slot) | Ring clock | Swipe left: what the Mac is playing · swipe right: podcasts on the card |
| Right edge down (speaker) | Almanac ⇄ calendar | Swipe left / right: switch · on the almanac, swipe up / down for another fortune, tap to hear it |
| Face down | Sleep (muted) | Turn it back over, or pick it up, to wake it |

On the now-playing and podcast pages, the three zones under the card are previous / play-pause / next (±15 s for podcasts). Add podcasts in the settings page under **Listen**.

## Pin

Hold the left key for 1 s to **pin** the page band on screen: the board stops changing pages when you turn it or pick it up, and only rotates the picture upright. Hold again to unpin. On the face page you can also tap the pin in the bottom-right corner. A needs-you request still takes over.

## Seats

The board has five seats, one per agent:

| Seat | Agent | Brings to the front |
|---|---|---|
| `claude` | Claude Code | your terminal (Warp by default) |
| `codex` | Codex | ChatGPT |
| `qoder` | Qoder IDE | Qoder IDE |
| `qoderwork` | QwenWork | QwenWork |
| `forest` | Qoder desktop | Qoder |

- Seats whose app is not running are hidden, so swiping skips them (turn on **Show offline seats** in the settings page to always see all five).
- Switch seats on the board and, after you stop for 1.5 s, the Mac brings that app to the front and puts the cursor in its input box. Click into an agent's app on the Mac and the board follows.
- In the settings page under **Seats**: **Scan This Mac** finds your agents; for each seat pick the app it raises, its approve / reject keys and its skin. **Hand to My Agent** copies a one-line prompt that lets your coding agent do this setup for you.

## Skins

Six skins: classic, kitty, robo, bunny, sprout and grok. Each seat wears one, and no two seats wear the same skin: picking one that another seat wears swaps the two. Change them on the board (long-press the face → settings card) or in the settings page.

## The settings page

<http://127.0.0.1:8788/settings>. Switch between 中文 and English at the top of the sidebar; the board switches with it (the almanac page stays in Chinese on purpose).

| Section | What's there |
|---|---|
| Overview | The board, card fonts, host and every seat, refreshed every 2 s |
| Board | Which Mac the board follows, its Wi-Fi networks |
| Seats | Agents on this Mac, the app / keys / skin for each seat |
| Sound | Volume; chirps or a spoken voice for needs-you and done |
| Dictation | Microphone source and link |
| Pickup | What to show when you pick the board up |
| Behavior | Following the Mac's front app, break reminder, offline seats |
| Now Playing | Show what the Mac is playing |
| Listen | Podcasts and audio files to put on the card |
| Host | Restart, memory trend, log, test buttons |

## Being a pet

- Plug it in and it eats; when the battery is full it burps.
- Leave it alone for 10 minutes and it sighs for attention; poke it and it cheers up.
- After 90 minutes of non-stop work it tells you to take a break.
- The profile card's level grows with finished tasks, pats and days together.

## Battery and power

- The settings card shows the battery level, with `CHG` while charging and `FULL` when done.
- Below 10 % it sighs every 3 minutes; at 5 % it powers itself off.
- Away from known Wi-Fi for 3 minutes, it turns Wi-Fi off to save power. Bluetooth keeps working; only spoken announcements need Wi-Fi.

## Config file

Most settings live in `~/.agentpet/config.json`, which the settings page writes for you. A few keys you may want by hand:

```json
{
  "approve_keys": {"codex": "cmd+enter"},
  "follow_front": false,
  "stretch_after_min": 0,
  "pickup_page": "follow"
}
```

`stretch_after_min: 0` turns the break reminder off. Restart the host (`pet restart`) after editing the file by hand.
