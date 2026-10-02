# Screen rules

English · [简体中文](06-screen-rules.zh-CN.md)

This page answers one question: at any moment, which page should the screen show? It describes what the firmware (`firmware/src/main.cpp`) does.

## Screen-sovereignty rules

**needs_you is the only agent state allowed to take over the screen.** The rest of the time the screen belongs to you: it shows the page for how the board is placed or held. If you want the pet, you call it (shake the board); the board never guesses.

- needs_you takes over because there is something to do: an approve bubble.
- working and done never move pages. They are ambient: the face page shows them when it is up, and done also plays a sound.
- When two intents conflict, the most recent deliberate one wins (see Last intent wins).

### What a takeover does

When the selected seat turns needs_you:

1. The screen jumps to that seat's face page, rotated upright for the current placement or grip, with the approve bubble.
2. Tap = approve, hold ≥ 0.7 s = reject.
3. After the answer the face stays 2 s so you can read the result, then the previous page comes back.

When a seat that is not selected newly raises its hand, the board first switches to that seat, and the takeover follows. It does not switch while you hold the voice key or while the board sleeps.

### One more takeover: the claim card

When a Mac asks to become the board's owner, the face page shows a "Connect to this Mac?" card. Only the green **Connect** button (连接 in Chinese) accepts; a tap outside the card, a hold ≥ 0.7 s, or 30 s without a tap declines. While it is up it owns the touch screen. It is never shown to a face-down board.

### Priority

Higher rows win. Face-down sleep sits above all of them: the screen is off and nothing below applies.

| # | Source | Screen shows |
|---|---|---|
| 1 | Claim card | Face page with the card |
| 2 | needs_you on the selected seat, plus 2 s after the answer | Face page |
| 3 | Shake summon, while in hand | Face page |
| 4 | Pin | The pinned page band |
| 5 | Pickup page binding, while in hand | The bound page |
| 6 | Placement | The page for the edge that is down |

## Pages by placement

Edges are named with the screen facing you and the three keys on top. See [Hardware](01-hardware.md) for what sits on each edge.

| Placement | Home page | Swipe left / right | Swipe up / down |
|---|---|---|---|
| Standing, keys up | Face page | Switch seat (left = next seat to the right) | Volume ±10 |
| Left edge down (microSD) | Clock | Left: now playing on the Mac. Right: board podcasts. No wrap. | Volume, on the two play pages |
| Right edge down (speaker) | Cyber almanac | Either way: almanac ⇄ calendar | On the almanac: another fortune |
| Face down | Sleep | — | — |
| Flat on its back | Keeps the last page | — | — |
| Keys down | Not a placement; keeps the last page | — | — |

- Coming into the clock band lands on the clock, or on the podcast page if the board's own player is playing.
- Coming into the almanac band lands on the almanac; with the board in English it lands on the calendar (the almanac stays Chinese).
- The almanac has five readings a day. Up = next, down = previous; coming back to the first shows 命运自有回应 ("fate answers in its own way"). A tap asks the pet to read the reading on screen aloud.
- Sleep: screen off, sounds muted, the board's podcast pauses and keeps its place. Turning it screen-up or picking it up wakes it.

## On the desk

| Event | Result |
|---|---|
| Stand it on another edge for 0.7 s | That edge's home page |
| The selected seat needs you | Face page until answered, plus 2 s |
| Another seat raises its hand | Board switches to that seat, then takes over |
| You swipe away from a waiting seat | That seat's takeover ends |
| Request answered, another seat still waiting | Board hands over to the waiting seat |
| A seat starts working or finishes | No page change |
| Face down for 0.8 s | Sleep |

## In the hand

- **Pickup**: a jolt with no finger on the screen. The pet looks surprised, and turns now need 0.9 s instead of 0.7 s.
- **In hand**: the jolt is followed by at least 0.5 s of continued motion. A knock on the desk dies out in about 0.2 s and never gets here. The pickup page binding and the shake summon only act once the board is in hand.
- **Put down**: still for 1.5 s. The board goes back to the page for how it now sits, and a summon ends.

| You | Result (pickup page `follow`) |
|---|---|
| Pick it up and do nothing else | The page facing you stays |
| Turn to another side and hold steady 0.9 s | That side's page |
| Shake a few times | Summon: the face page (with a dizzy look; that animation has a 15 s cooldown) |
| After a summon, turn and hold 0.9 s | Leaves the face for that side's page |
| Swipe on the face page | Switch seat / volume, in the direction you see it |
| The selected seat needs you | Face page, upright for your grip |

A takeover, a summon, a pinned page and a bound page rotate with live gravity to face you.

### Pickup page (拿起时显示)

Which page the board shows while in hand.

| Value | While in hand |
|---|---|
| `follow` (default) | The page facing you; turns change it |
| `face` | Always the face page |
| `clock` | Always the clock |
| `almanac` | Always the almanac |
| `smart` | Old name of `follow`; behaves the same |

- A bound page rotates with your grip, and turns do not change it. Shake summon and needs_you still take over; a pin overrides the binding.
- Set it on the settings page (Pickup → Show When Picked Up; a click writes it to the board at once), or with `"pickup_page"` in `~/.agentpet/config.json`. The board keeps it in NVS, so it also works without the host.

## Pin (钉住)

Pin = follow my hand, not gravity. A pinned page stays when the board is turned, picked up or turned in the hand; it still rotates to read upright.

| Item | Behaviour |
|---|---|
| Pin / unpin any page | Hold the left key 1 s. It acts at 1 s, while still held, and shows 钉住 / 解除钉住 (Pinned / Unpinned) for 1.5 s. A short press is still mute. |
| Pin / unpin the face | Also: tap the bottom-right corner of the face page (64 × 64 px), even when no pin is drawn. That tap does nothing else. |
| What is pinned | The band on screen: face, clock band or almanac band, on the sub-page it was on |
| Inside the band | Swipes still work (clock, podcasts, now playing; almanac, calendar; fortunes); the pin follows |
| Pin icon | Bottom-right corner: seat colour for 10 s, then grey while pinned. Unpinned: hidden on the desk, dim grey on the face page in the hand. On other pages it is not a button; unpin with the left key. |
| Still applies | needs_you takes over, then returns to the pinned page; face down still sleeps; a shake in hand shows the face for the moment |
| Overrides | Placement and the pickup page binding |
| Persists | Kept in NVS; still pinned after a reboot (grey icon) |

## Last intent wins (最后意图获胜)

When two signals disagree, the most recent deliberate action decides.

| Conflict | Winner |
|---|---|
| A shake summoned the face, then you turn and hold 0.9 s | The turn: that side's page |
| A seat needs you, then you swipe to another seat | The swipe: no takeover for the seat you left |
| A board swipe vs. clicking an agent's app on the Mac | The later one |
| Pin vs. gravity or the pickup page | The pin |
| Pin vs. needs_you | needs_you, then back to the pin |
| A new request or the Mac's front app, while you hold the voice key | The voice key: the seat stays put until you let go |

The board and the Mac follow each other's seat choice:

- **Board → Mac**: 1.5 s after your last swipe (`focus_settle_s`), the Mac brings that seat's app to the front. Fast swipes only raise the last seat.
- **Mac → board**: click into an agent's app and keep it in front 1 s; the board moves to that seat silently. Only the agents' apps count. The host does not follow within 5 s of a board swipe, ignores its own app raises for 3 s, and never while the voice key is held. `"follow_front": false` in `config.json` turns it off.

## Thresholds

`motion` is a smoothed sum of the per-sample change in acceleration on three axes, in g. `gx`, `gy`, `gz` are low-passed acceleration in g. The values live in `imuPoll` and `touchPoll` in `firmware/src/main.cpp`; swipe distances and the page per placement (`PAGES_BY_ORIENT`) in `firmware/src/config.h`; the two follow timings in `host/agentpet_host.py`.

| Parameter | Value |
|---|---|
| IMU sampling | 25 Hz |
| Pickup jolt | `motion` > 0.30, ignored while a finger is on the screen |
| In hand | `motion` > 0.08 for 0.5 s after the jolt |
| Put down | `motion` < 0.05 for 1.5 s |
| New placement | \|`gz`\| < 0.75 and \|`gx`\| or \|`gy`\| > 0.45, steady 0.7 s (0.9 s after a pickup) |
| Face down → sleep | `gz` < −0.75 for 0.8 s |
| Wake | `gz` > 0.5, or `motion` > 0.08 for 0.5 s |
| Shake | 3 jolts > 0.70, each ≥ 150 ms apart, within 1.2 s |
| Dizzy animation cooldown | 15 s (animation only, not the page) |
| needs_you linger after the answer | 2 s |
| Approve / reject | tap / hold ≥ 0.7 s; 1 s between decisions |
| Seat swipe | 80 px, 2:1 straight, decided 300 ms later (to tell it from petting) |
| Volume swipe | 60 px, 1.5:1 straight, immediate |
| Pin | left key 1 s; label 1.5 s; seat colour 10 s; corner 64 × 64 px |
| Claim card | 30 s to answer |
| Mac → board follow | app in front 1 s; waits 5 s after a board swipe, 3 s after the host's own raise |
| Board → Mac follow | 1.5 s after the last swipe (`focus_settle_s`) |

## Trade-offs and limits

- Keys down is never a placement: on the desk the last page stays. In the hand only a takeover, a summon, a pinned or a bound page turns to face you that way.
- Flat on its back keeps the last page, since no edge is down.
- Sleep beats everything. needs_you does not light a face-down board, and keys or taps do not wake it, on purpose. Turn it over or pick it up.
- One request at a time: a takeover follows the selected seat only. A second waiting seat gets its turn when the first is answered.
- If the link to the Mac drops while a request is up, the face stays (the board trusts the last state it got) and a tap sends nothing. It clears when the link is back.
- The right key starts dictation the moment it is pressed. It never waits to tell a short press from a hold, so it carries no second action.
- The settings card (hold the face 1.5 s) is hit-tested for the face standing keys-up. In a rotated view its buttons may miss; stand the board up first.

## Names in the code

Firmware comments point here as `doc/06` and use the Chinese names: 屏幕主权 = screen-sovereignty rules, 拿起时显示 = Pickup page, 钉住 = Pin, 最后意图获胜 = Last intent wins.
