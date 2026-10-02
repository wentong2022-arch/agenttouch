# Hardware

English · [简体中文](01-hardware.zh-CN.md)

AgentTouch runs on one off-the-shelf board, the Waveshare ESP32-S3-Touch-AMOLED-2.16. This page covers its parts, the pin map, the three keys, and how the case's four edges map to what the screen shows.

## The board

A square 2.16-inch AMOLED board in its own small case, with touch, a speaker, two microphones, a motion sensor, a real-time clock, a battery charger and a microSD slot. AgentTouch uses all of them; there is nothing to wire up.

| Part | Chip / spec | AgentTouch uses it for |
|---|---|---|
| MCU | ESP32-S3R8, dual-core Xtensa LX7, 240 MHz | everything |
| Memory | 8 MB octal PSRAM (in package), 16 MB QSPI flash | frame buffer, two firmware slots |
| Radio | 2.4 GHz Wi-Fi (b/g/n), Bluetooth 5 LE | link to the Mac |
| Display | 2.16" AMOLED, 480 × 480, CO5300 driver over QSPI | faces and pages |
| Touch | CST9220 capacitive, I2C | taps and swipes |
| Speaker | ES8311 codec, NS4150B amplifier | sounds, speech, podcasts |
| Microphones | ES7210 ADC, two MEMS mics about 39 mm apart | dictation through the board mic |
| Motion | QMI8658 6-axis IMU | which edge is down, pick-up, shake, face-down sleep |
| Clock | PCF85063 RTC | correct time before the Mac connects |
| Power | AXP2101 PMU | battery, charging, middle key |
| Storage | microSD slot | fonts, almanac, podcasts, firmware updates |
| USB | USB-C to the ESP32-S3's native USB | first flash, serial log |
| Battery | MX1.25 2-pin connector, 3.7 V Li-ion | runs without a cable |

The radio has an on-board antenna and an IPEX connector for an external one (switching needs a resistor moved).

## Pins

The firmware's copy of this map is `firmware/src/pins.h`.

| Group | Signal | GPIO | Notes |
|---|---|---|---|
| Display | QSPI D0–D3 | 4, 5, 6, 7 | CO5300 |
| Display | QSPI CLK | 38 | |
| Display | CS | 12 | |
| Display | RESET | 39 | |
| Touch | INT | 11 | falling edge |
| Touch | RESET | 40 | |
| I2C | SDA | 15 | shared bus, see below |
| I2C | SCL | 14 | |
| Audio | I2S MCLK | 42 | shared by ES8311 and ES7210 |
| Audio | I2S BCLK | 9 | shared |
| Audio | I2S LRCK | 45 | shared |
| Audio | I2S DOUT | 8 | ESP32 → ES8311 (playback) |
| Audio | I2S DIN | 10 | ES7210 → ESP32 (mics) |
| Audio | PA_CTRL | 46 | speaker amp enable, high = on |
| microSD | CLK / SCK | 2 | |
| microSD | CMD / MOSI | 1 | |
| microSD | D0 / MISO | 3 | |
| microSD | CS | 41 | SPI mode only |
| Keys | left key (BOOT) | 0 | active low, strapping pin |
| Keys | right key (IO18) | 18 | active low, 10 kΩ pull-up |
| IMU | INT1 / INT2 | 17 / 21 | not used |
| RTC | INT | 13 | not used |
| Power | SYS_OUT | 16 | not a key line |
| USB | D− / D+ | 19 / 20 | native USB, do not reuse |
| UART0 | TX / RX | 43 / 44 | download and log |

The middle key has no GPIO. It is wired to the AXP2101's PWRON pin (see [Keys](#keys)).

## I2C bus

Every on-board I2C chip shares one bus: SDA = GPIO15, SCL = GPIO14.

| Address | Chip | Role |
|---|---|---|
| 0x18 | ES8311 | speaker codec |
| 0x34 | AXP2101 | power management |
| 0x40–0x43 | ES7210 | mic ADC (address set by its AD0/AD1 pins; the firmware probes all four) |
| 0x51 | PCF85063 | real-time clock |
| 0x5A | CST9220 | touch |
| 0x6B | QMI8658 | IMU |

An extra I2C device on this bus must avoid these addresses.

## Keys

Three keys sit on the top edge. Left to right:

| Key | Wiring | In AgentTouch |
|---|---|---|
| Left (BOOT) | GPIO0, active low | Press: mute on / off. Hold 1 s: pin / unpin the page band on screen |
| Middle (PWR) | AXP2101 PWRON | While off: power on. Press: status toast for 3 s. Hold 3 s: power off |
| Right (IO18) | GPIO18, active low | Hold to talk (dictation) |

The full gesture map is in [Using it](03-usage.md); pinning is described in [Screen rules](06-screen-rules.md).

- **Middle key.** It can only be read through the PMU: the firmware enables the AXP2101's PWRON press and release interrupts and polls them over I2C. GPIO16 (SYS_OUT) does not change when it is pressed.
- **6-second power cut.** The AXP2101 cuts power by itself when PWRON is held for 6 s, and this cannot be turned off on this board. So the middle key can never be a hold-to-use key. The cut doubles as an emergency stop if the firmware hangs; a running firmware shuts down cleanly at 3 s first.
- **Left key at boot.** GPIO0 is a strapping pin: holding the left key while the chip resets puts it into download mode.

## Notes for firmware work

- Display power. The panel rail is the AXP2101's ALDO3. The firmware brings up the PMU and enables ALDO3 before it touches the panel.
- Rotation. The panel is rotated with MADCTL `0xA0`. Touch coordinates are mapped with swap-XY plus mirror-X (`TOUCH_*` in `firmware/src/config.h`).
- Frame buffer. The whole 480 × 480 RGB565 frame (about 450 KB) is a canvas in PSRAM, flushed over QSPI. The display owns SPI2.
- Audio. ES8311 and ES7210 share one I2S port in full duplex: 16 kHz, MCLK 4.096 MHz. The ES7210 runs without TDM, so the left slot is MIC1 and the right slot is MIC2. Its third input is an echo reference taken from the speaker output; the fourth is unused.
- microSD. The firmware tries the native SDMMC host in 1-bit mode first, then falls back to SD-SPI on SPI3. Some cards only answer in native mode. The card must be FAT32.
- RTC. The PCF85063 runs from the AXP2101's RTC LDO, which stays on while the board is off (about 40 µA), so the time survives a power-off. Only a flat or unplugged battery loses it. VBACKUP has just a capacitor. The chip holds local time.
- IMU. Only the accelerometer is used (±4 g, 125 Hz, read 25 times a second). The gyroscope and both interrupt lines are unused.
- Charging. The firmware sets a 4.2 V target and 500 mA charge current.
- Flash. 16 MB, stock `default_16MB.csv` partition table. Its two app slots let a cable-free update write the idle slot and roll back.

## Links

- Waveshare documentation: [English](https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-2.16) · [Chinese](https://docs.waveshare.net/ESP32-S3-Touch-AMOLED-2.16)
- Waveshare examples: [github.com/waveshareteam/ESP32-S3-Touch-AMOLED-2.16](https://github.com/waveshareteam/ESP32-S3-Touch-AMOLED-2.16)
- [Schematic (PDF)](https://files.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-2.16/ESP32-S3-Touch-AMOLED-2.16-Schematic.pdf)

## The four edges and the sensor axes

Looking at the screen with the three keys on top:

```
                    top edge
          [BOOT]     [PWR]     [IO18]
           left      middle     right
       +---------------------------------+
  MIC2 o                                 o MIC1
  left |                                 | right
  edge |                                 | edge
microSD|           480 x 480             |
  slot |            screen               |::
       |                                 |::  speaker
       |                                 |::  grille
       +-------------[ USB-C ]-----------+
                   bottom edge
```

| Edge | What is on it |
|---|---|
| Top | the three keys: BOOT, PWR, IO18 |
| Bottom | USB-C |
| Left | microSD slot, MIC2 hole near the top corner |
| Right | speaker grille on the lower half, MIC1 hole near the top corner |

**IMU axes**, as printed on the board: +X points to the right edge, +Y to the top edge (the keys), +Z out of the screen. At rest the accelerometer reads about +1 g on whichever axis points up.

### Placement → page

| Placement | Accelerometer | Sector | Shows |
|---|---|---|---|
| Standing, keys on top | +Y up | 0 | face page (swipe = switch seats) |
| Left edge down (microSD) | +X up | 3 | clock band: clock; swipe left = now playing on the Mac, swipe right = board podcasts |
| Right edge down (speaker) | −X up | 1 | almanac band: almanac and calendar, either swipe flips between them |
| Keys down | −Y up | 2 | not used: keeps the last page |
| Flat, screen up | +Z up | — | keeps the last page |
| Screen down | −Z up | — | sleep |

- A new placement counts after 0.7 s of steady gravity (0.9 s once picked up); screen-down needs 0.8 s. In the hand, what shows follows the [screen rules](06-screen-rules.md#in-the-hand).
- With the English interface, the almanac band opens on the calendar.
- The mapping lives in `PAGES_BY_ORIENT` (`firmware/src/config.h`) and `imuPoll()` (`firmware/src/main.cpp`).

### Why this way round, and which mic

Resting on a side edge covers that edge's mic hole, and resting on the right edge also points the speaker at the desk. So the clock band, which plays podcasts, is on the left edge: speaker and MIC1 face up. The almanac only speaks a few seconds when tapped.

When dictation uses the board's mic (settings page, Dictation), the mic follows the placement sector, not live gravity:

| Placement | Mic |
|---|---|
| Right edge down (sector 1, almanac band) | MIC2, on the left edge |
| Any other placement | MIC1, on the right edge |
