#!/usr/bin/env python3
# Copyright (c) 2026 yuwentong
# Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
"""AgentTouch host service (macOS, stdlib only).

- Watches Claude Code (via hooks -> HTTP), Codex (process + session files +
  optional notify hook), Qoder IDE (process + Quest log), Qoder Forest desktop
  (process + main.log session state machine, wire id "forest") and
  QwenWork 千问办公 (process + agents.db; wire id stays "qoderwork") and
  aggregates one state per agent:
      off | idle | working | needs_you | done
- Serves the board over TCP (newline JSON), pushing state on change + heartbeat.
- On the board's voice button: presses the macOS dictation shortcut (fn key)
  so speech lands in whatever window has focus.

Run:  python3 agentpet_host.py
Debug: curl http://127.0.0.1:8788/state
"""
import asyncio
import base64
import ctypes
import ctypes.util
import datetime
import email.utils
import gc
import hashlib
import collections
import json
import os
import queue
import random
import re
import shutil
import socket
import socketserver
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import tracemalloc
import urllib.request
import wave
import xml.etree.ElementTree as ET
from collections import deque
try:
    import audioop            # IMA ADPCM decode for the board mic (removed in Python 3.13; /usr/bin/python3 is 3.9)
except ImportError:
    audioop = None
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, unquote

# ------------------------------------------------------------------ config
TCP_PORT = 8737          # board connects here
HTTP_PORT = 8788         # claude hooks + debug
# Where `pet` redirects our stdout (host/pet LOG=). The settings page reads
# its tail through /log/tail, which takes no path of its own: one fixed file
# means a stray browser tab can never turn the host into a file reader.
LOG_PATH = Path("/tmp/agentpet_host.log")

# Which board this host serves: the first log line, /state and `pet status` say it.
PRODUCT = "s3"
HOST_VERSION = "2026-10-01a"

# `forest` (Qoder Forest, the "Qoder CN.app" desktop client, split from the
# IDE seat 2026-09-13) MUST stay last: report_msg() emits w[]/d[] in
# this order and both boards read them by index. `qoderwork` keeps its
# historical wire id (the board has it baked in).
AGENTS = ["claude", "codex", "qoder", "qoderwork", "forest"]

# Process detection: regex tried against the full `ps` command line.
# `exclude` removes lines that belong to a more specific sibling agent.
# Real-world process names (verified 2026-08-27 on this Mac):
#   codex     -> ChatGPT.app embeds it: .../ChatGPT.app/Contents/Resources/codex
#   qoder     -> "Qoder CN IDE.app" (Electron: Qoder, QoderCN helpers)
#   forest    -> "Qoder CN.app" (Qoder Forest desktop, 2026-09-13; same
#                CFBundleName "Qoder CN" as the IDE, so split by app path)
#   qoderwork -> wire id kept for the board; the app is now QwenWork (千问办公),
#                "QwenWorkCN.app" (Electron: QwenWorkCN + helpers)
PROC_RULES = {
    "claude":    {"match": r"(^|[/\s])claude(\s|$)", "exclude": None},
    "codex":     {"match": r"(^|[/\s])codex(\s|$)",  "exclude": None},
    "qoderwork": {"match": r"(?i)qwen[-_ ]?work",    "exclude": None},
    "qoder":     {"match": r"Qoder (CN )?IDE\.app",  "exclude": None},
    "forest":    {"match": r"(^|/)Qoder( CN)?\.app/", "exclude": r"Qoder (CN )?IDE\.app"},
}
# Qoder ships two builds with different names and data dirs: the domestic
# "Qoder CN IDE" / "Qoder CN" (this dev Mac) and the international "Qoder IDE"
# / "Qoder" (the second Mac, 2026-09-26: both seats never lit up). Every
# default accepts both; a Mac simply has whichever it has.
APP_VARIANTS = {
    "qoder":  ["Qoder CN IDE", "Qoder IDE"],
    "forest": ["Qoder CN", "Qoder"],
}
DEFAULT_PROC_RULES = {a: dict(r) for a, r in PROC_RULES.items()}
# What the settings page used to write back verbatim into config.json. A saved
# copy of an old default is not a user choice and must not pin the old names.
LEGACY_PROC_RULES = {
    "qoder":  [{"match": r"Qoder CN IDE\.app", "exclude": None}],
    "forest": [{"match": r"Qoder CN\.app", "exclude": r"Qoder CN IDE\.app"}],
}

def app_installed(name):
    return any((d / (name + ".app")).exists() for d in (Path("/Applications"), Path.home() / "Applications"))

def resolve_app_variants(apps):
    """focus_apps names a build this Mac does not have (config copied from
    another Mac) -> the sibling build it does have. Anything else is left be."""
    for seat, names in APP_VARIANTS.items():
        cur = apps.get(seat)
        if cur in names and not app_installed(cur):
            have = next((n for n in names if app_installed(n)), None)
            if have:
                apps[seat] = have
# CPU% above this (summed over matching processes) counts as "working".
# qoderwork (QwenWork) is NOT here: its Electron renderer idles at 10-20%
# CPU with the window open (measured 2026-08-27), so CPU is useless for it —
# it uses the agents.db WAL mtime below instead.
# forest: idle peak 2.2% over 90 s (2026-09-13), so the qoder threshold holds.
CPU_WORKING = {"codex": 8.0, "qoder": 12.0, "forest": 12.0}

# Which seats this Mac has at all. A seat whose app is not
# installed is simply always off, and the board already hides off seats, so
# this only feeds the settings page's "not installed here" label. Checked on
# every /settings/data call (a few stat()s), so installing an app shows up
# without a host restart.
INSTALL_PROBES = {
    "claude":    {"bins": ["claude"], "dirs": [".claude"]},
    "codex":     {"bins": ["codex"], "apps": ["ChatGPT.app", "Codex.app"], "dirs": [".codex"]},
    "qoder":     {"apps": [n + ".app" for n in APP_VARIANTS["qoder"]]},
    "forest":    {"apps": [n + ".app" for n in APP_VARIANTS["forest"]]},
    "qoderwork": {"apps": ["QwenWorkCN.app", "QwenWork.app"]},
}

# ---- 扫描本机: which agents this Mac has, and how well each is wired
# One row per known build. seat=None: an agent the board has no seat for yet
# (listed so the person sees it was noticed; borrowing a seat is a later step).
# how: "hooks" Claude Code hooks / "codex" notify+hooks.json / "log" a log dir
# we tail / "db" QwenWork's agents.db / None = only "open" and "busy by CPU".
_AS = Path.home() / "Library" / "Application Support"
AGENT_CATALOG = [
    {"key": "claude",       "name": "Claude Code",        "seat": "claude",    "bins": ["claude"], "how": "hooks"},
    {"key": "codex",        "name": "Codex (ChatGPT)",    "seat": "codex",     "apps": ["ChatGPT"], "bins": ["codex"], "how": "codex"},
    {"key": "codex-app",    "name": "Codex",              "seat": "codex",     "apps": ["Codex"], "how": "codex"},
    {"key": "qoder-ide-cn", "name": "Qoder CN IDE",       "seat": "qoder",     "apps": ["Qoder CN IDE"], "build": "cn",   "how": "log", "logs": _AS / "QoderCN" / "logs"},
    {"key": "qoder-ide",    "name": "Qoder IDE",          "seat": "qoder",     "apps": ["Qoder IDE"],    "build": "intl", "how": "log", "logs": _AS / "Qoder" / "logs"},
    {"key": "qoder-cn",     "name": "Qoder CN",           "seat": "forest",    "apps": ["Qoder CN"],     "build": "cn",   "how": "log", "logs": _AS / "com.qodercn.app.stable" / "logs"},
    {"key": "qoder",        "name": "Qoder",              "seat": "forest",    "apps": ["Qoder"],        "build": "intl", "how": "log", "logs": _AS / "com.qoder.app.stable" / "logs"},
    {"key": "qwenwork-cn",  "name": "千问办公 QwenWork",   "seat": "qoderwork", "apps": ["QwenWorkCN"], "build": "cn", "how": "db", "db": _AS / "QwenWorkCN" / "data" / "agents.db"},
    {"key": "qwenwork",     "name": "QwenWork",           "seat": "qoderwork", "apps": ["QwenWork"], "build": "intl"},
    # no seat yet
    {"key": "qoderwork",    "name": "QoderWork",          "seat": None, "apps": ["QoderWork"]},
    {"key": "claude-app",   "name": "Claude（桌面 app）",   "seat": None, "apps": ["Claude"]},
    {"key": "cursor",       "name": "Cursor",             "seat": None, "apps": ["Cursor"]},
    {"key": "windsurf",     "name": "Windsurf",           "seat": None, "apps": ["Windsurf"]},
    {"key": "trae",         "name": "Trae",               "seat": None, "apps": ["Trae", "Trae CN"]},
    {"key": "kiro",         "name": "Kiro",               "seat": None, "apps": ["Kiro"]},
    {"key": "codebuddy",    "name": "CodeBuddy",          "seat": None, "apps": ["CodeBuddy", "CodeBuddy CN"]},
    {"key": "lingma",       "name": "通义灵码 Lingma",      "seat": None, "apps": ["Lingma"]},
    {"key": "gemini",       "name": "Gemini CLI",         "seat": None, "bins": ["gemini"]},
    {"key": "opencode",     "name": "opencode",           "seat": None, "bins": ["opencode"]},
    {"key": "aider",        "name": "aider",              "seat": None, "bins": ["aider"]},
]

def _how_ok(e):
    """(wired, what) for the status column: is the state source actually there."""
    how = e.get("how")
    home = Path.home()
    if how == "hooks":
        try:
            n = (home / ".claude" / "settings.json").read_text().count("src=agentpet")
        except OSError:
            n = 0
        return n >= 7, "hooks"
    if how == "codex":
        try:
            hk = "codex_permission_hook.sh" in (home / ".codex" / "hooks.json").read_text()
        except OSError:
            hk = False
        try:
            nt = "codex_notify.sh" in (home / ".codex" / "config.toml").read_text()
        except OSError:
            nt = False
        return hk and nt, "codex"
    if how == "log":
        return e["logs"].is_dir(), "log"
    if how == "db":
        return e["db"].exists(), "db"
    return False, None

def scan_agents():
    """The 扫描本机 table: every catalog build found on this Mac (installed or
    running), with its seat, whether the state source is wired, and whether it
    is the build the seat currently raises. Unfound catalog rows are omitted."""
    home = Path.home()
    path = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin:" + str(home / ".local/bin")
    try:
        ps = subprocess.run(["ps", "-axo", "command"], capture_output=True, text=True, timeout=5).stdout.splitlines()
    except Exception:
        ps = []
    rows = []
    for e in AGENT_CATALOG:
        where = None
        for app in e.get("apps", []):
            for d in (Path("/Applications"), home / "Applications"):
                if (d / (app + ".app")).exists():
                    where = str(d / (app + ".app")).replace(str(home), "~")
                    break
            if where:
                break
        if not where:
            for b in e.get("bins", []):
                w = shutil.which(b, path=path)
                if w:
                    where = w.replace(str(home), "~")
                    break
        pats = ["/%s.app/" % re.escape(a) for a in e.get("apps", [])] + \
               [r"(^|/)%s(\s|$)" % re.escape(b) for b in e.get("bins", [])]
        running = any(re.search(pt, line) for pt in pats for line in ps)
        if not where and not running:
            continue
        wired, how = _how_ok(e)
        seat = e.get("seat")
        rows.append({
            "key": e["key"], "name": e["name"], "seat": seat, "build": e.get("build"),
            "where": where, "running": running, "how": how, "wired": wired,
            "app": (e.get("apps") or [None])[0],
            "current": bool(seat and e.get("apps") and FOCUS_APPS.get(seat) in e["apps"]),
        })
    return rows

def agent_setup_md():
    """GET /agent-setup: host/agent_setup.md (deployed next to this file by
    setup.sh / pet use) plus a live 本机现状 section, so an agent on any Mac
    reads the guide that matches its host and sees this Mac as it is now."""
    here = Path(__file__).resolve().parent
    repo = ""
    try:
        for line in (Path.home() / ".agentpet" / "repos").read_text().splitlines():
            if line.startswith("s3="):
                repo = line[3:].strip()
    except OSError:
        pass
    doc = None
    for p in (here / "agent_setup.md", Path(repo) / "host" / "agent_setup.md" if repo else None):
        if p and p.exists():
            doc = p.read_text()
            break
    if doc is None:
        doc = "# AgentTouch agent setup\n\n(agent_setup.md is missing next to the host; re-run host/setup.sh)\n"
    snap = state_snapshot()
    rows = scan_agents()
    lines = ["", "---", "", "## 本机现状（host 现场生成于 %s）" % time.strftime("%Y-%m-%d %H:%M:%S"), "",
             "- host 版本：%s；仓库：%s" % (HOST_VERSION, repo or "未知（~/.agentpet/repos 没有 s3= 行）"),
             "- 板子：ble=%s，boards=%s" % (snap.get("ble"), snap.get("boards")),
             "- 各席位此刻状态：%s" % json.dumps(snap.get("agents"), ensure_ascii=False),
             "- 各席位拉起的 app（focus_apps）：%s" % json.dumps(dict(FOCUS_APPS), ensure_ascii=False),
             "", "| agent | 席位 | 版本 | 在哪 | 在跑 | 状态来源 | 接好 | 在用 |", "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["name"], r["seat"] or "（暂无席位）", r["build"] or "", r["where"] or "", "是" if r["running"] else "",
            r["how"] or "只看开没开", "是" if r["wired"] else ("否" if r["how"] else ""), "是" if r["current"] else ""))
    if not rows:
        lines.append("| （没扫到认识的 agent） | | | | | | | |")
    return doc + "\n".join(lines) + "\n"

def agents_use(seat, app):
    """「用这个」: make `app` the app a seat raises (focus follow / approve).
    Merged into config.json's focus_apps — other seats' entries stay."""
    if seat not in AGENTS or not isinstance(app, str) or not app.strip():
        return {"ok": False, "why": "bad"}
    cfg = {}
    if CONFIG_OVERRIDE.exists():
        try:
            cfg = json.loads(CONFIG_OVERRIDE.read_text())
        except ValueError:
            return {"ok": False, "why": tr("config.json 读不了，没有覆盖它", "config.json is unreadable; left it alone")}
    fa = dict(cfg.get("focus_apps") or {})
    fa[seat] = app.strip()
    cfg["focus_apps"] = fa
    CONFIG_OVERRIDE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_OVERRIDE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    load_overrides()
    log("agents: seat", seat, "now raises", app)
    return {"ok": True}

def seats_installed():
    home = Path.home()
    path = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin:" + str(home / ".local/bin")
    out = {}
    for a in AGENTS:
        pr = INSTALL_PROBES.get(a, {})
        ok = any((Path(d) / app).exists() for app in pr.get("apps", [])
                 for d in ("/Applications", home / "Applications"))
        ok = ok or any(shutil.which(b, path=path) for b in pr.get("bins", []))
        ok = ok or any((home / d).is_dir() for d in pr.get("dirs", []))
        out[a] = ok
    return out

# Focus follow: every board-side seat switch (swipe, BOOT-key cycle, and the
# needs_you auto-focus) sends {"t":"select"}; mirror it by raising the app on
# the Mac. `open -a` needs no TCC grant, unlike osascript. Overridable via
# config.json: {"focus_follow": false, "focus_apps": {"claude": "iTerm"}}.
FOCUS_FOLLOW = True
FOCUS_APPS = {
    "claude":    "Warp",            # Claude Code lives in the Warp terminal
    "codex":     "ChatGPT",
    "qoder":     "Qoder CN IDE",
    "qoderwork": "QwenWorkCN",
    "forest":    "Qoder CN",        # Qoder Forest desktop
}
resolve_app_variants(FOCUS_APPS)    # international Qoder build on this Mac? use its names
# After focus follow raises the app, optionally press its "focus the chat
# input" shortcut ~0.6 s later so dictation lands in the right box.
# Calibrated 2026-08-29: Warp / Qoder / QwenWork focus their input on
# activation (no key needed). ChatGPT ships a "聚焦主聊天输入框" command
# UNASSIGNED (its web shortcut shift+esc means "mark all as read" in the
# desktop app — guessed once, hit live); we bind it to ⌃⇧I in
# ChatGPT ➜ Settings ➜ Keyboard Shortcuts and press that here.
FOCUS_KEYS = {"codex": "ctrl+shift+i"}

# Reverse focus follow (doc/06 "最后意图获胜"): clicking into an agent's app
# on the Mac is an intent too, so the board's seat follows the frontmost app
# — then the right key dictates straight into the app the user is looking at.
# Only the four FOCUS_APPS count (a browser or Finder changes nothing). The
# candidate must hold still for FOLLOW_STABLE_S so alt-tabbing past a window
# does not switch seats, and three lockouts keep it from fighting anyone:
# a board swipe wins for FOLLOW_BOARD_S, our own `open -a` is ignored for
# FOLLOW_RAISE_S (self-excitation: focus follow raises the app, which would
# otherwise bounce back as a "new" front), and nothing moves while the voice
# key is held. config.json: {"follow_front": false}.
FOLLOW_FRONT = True
FOLLOW_STABLE_S = 1.0
FOLLOW_BOARD_S = 5.0
FOLLOW_RAISE_S = 3.0

# Tap-to-approve: with the selected agent in needs_you the board shows an
# approve bubble — tap sends {"t":"key","k":"approve"}, long-press "reject".
# We raise the agent's app (the user may have switched windows since focus
# follow ran) and press its confirm/dismiss key. Defaults: Claude Code's
# permission prompt takes Enter for the highlighted "Yes" and Esc to refuse;
# Codex's desktop confirmation card gets the same pair until calibrated live.
# config.json: {"approve_keys": {"codex": "cmd+enter"}, "reject_keys": {...}}
APPROVE_KEYS = {a: "enter" for a in AGENTS}
REJECT_KEYS = {a: "esc" for a in AGENTS}

# Sedentary reminder: any agent "working" with gaps under 15 min counts as one
# continuous session; at 90 min the board suggests a stretch ("break time ~"
# face + sigh), then again no sooner than an hour later.
# config.json: {"stretch_after_min": 0} disables, other values remap.
STRETCH_AFTER_MIN = 90
STRETCH_GAP_MIN = 15
STRETCH_COOLDOWN_MIN = 60

# Activity files: newest mtime within this window also counts as "working"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
ACTIVITY_WINDOW = 60          # seconds
# QwenWork keeps sub_chats.stream_id set while an agent run is streaming and
# clears it the second the run ends (verified 2026-08-28 over a 105 s task:
# no mid-task dropout, instant clear). WAL-mtime silence windows either flash
# "done" during long thinking gaps or lag the real finish — don't go back.
QWEN_DB = (Path.home() / "Library" / "Application Support" /
           "QwenWorkCN" / "data" / "agents.db")
CLAUDE_WORK_DECAY = 120       # working -> idle after this much hook silence
DONE_LINGER = 90              # "done" face duration after Stop

# Voice input: "hold_fn" holds the fn (globe) key down while the board's
# voice button is held — matches WeChat IME's hold-fn-to-talk (and any IME
# with a hold-fn voice mode). "fn_double" taps fn twice for Apple dictation.
VOICE_MODE = "hold_fn"
FN_KEYCODE = 63

# Pickup page binding (doc/06 screen-sovereignty rules): what the board
# shows while held. config.json {"pickup_page": "follow"} (follow|face|
# clock|report|almanac|smart — smart is a legacy alias of follow since
# 2026-09-01; the board no longer guesses intent from agent states).
# None = not configured -> nothing pushed, the board's own NVS value rules
# (so /test/pickup experiments survive host restarts).
PICKUP_PAGE = None
PICKUP_PAGES = ("follow", "face", "clock", "report", "almanac", "smart")

# TTS voice (user-picked 2026-08-30 from 4 samples).
# Phrases are synthesized ONCE via edge-tts (pip --user, generation-time only)
# + afconvert to the board's 16 kHz mono s16le, then cached in VOICES_DIR —
# runtime playback is pure cache reads, works offline. TCP only (BLE is too
# slow for 32 KB/s audio); no TCP board connected = speak is silently skipped.
# Per language; the almanac fortune is ALWAYS read in Chinese
# (the almanac page stays a Chinese easter egg in English mode). The cache key
# carries the voice name, so the two languages never collide.
TTS_VOICES = {"zh": "zh-CN-XiaoyiNeural", "en": "en-US-AriaNeural"}
TTS_FMT = "ima"           # wire format for clips: "ima" (IMA ADPCM 4:1) | "pcm"; /test/say/..?fmt= overrides
VOICES_DIR = Path.home() / ".agentpet" / "voices"
# voice_style "chirp" (default) | "tts": with tts, fresh needs_you/done are
# SPOKEN (the board mutes its own chirps for those while its TCP link is up;
# we push the chirp as fallback if synthesis fails). config.json overrides.
# The almanac 求签 (board tap -> {"t":"qian"}) speaks in EITHER style —
# invited speech is exempt from the style switch (user decision 2026-08-30).
VOICE_STYLE = "chirp"
# ---- board microphone (mic-blackhole branch) ----
# voice_source "mac" (default): dictation uses the Mac's own microphone, as
# always. "board": the right key also streams the pet's ES7210 mic
# ({"t":"mic"} IMA ADPCM lines over TCP); the host decodes it and plays it
# into MIC_SINK_DEVICE (BlackHole, a virtual sound card), which it makes the
# system default input around the fn hold — so 微信输入法 hears the pet.
# Board frames are queued until fn is down, then the pipeline runs live;
# fn is released MIC_TAIL_S after the key-up so the last words get through
# (pipeline latency: see /test/mic/latency). config.json overrides all three.
VOICE_SOURCE = "mac"
MIC_SINK_DEVICE = "BlackHole 2ch"
MIC_TAIL_S = 0.6                                           # measured pipeline ≈ 0.3–0.4 s (/test/mic/latency) + sink prime
MIC_SINK_BIN = Path.home() / ".agentpet" / "mic_sink"     # swiftc -O -o ~/.agentpet/mic_sink host/mic_sink.swift
MIC_DIR = Path.home() / ".agentpet" / "mic"                # /test/mic/rec recordings
# Which link the dictation stream rides by default (BLE was
# measured at 20-33 KB/s against the stream's 11.5 KB/s, and beats TCP to the
# host by ~275 ms). Pushed to the board in every cfg: "ble" = BLE first with
# TCP as fallback, "tcp" = the reverse, "auto" = same as ble. The runtime
# override /test/mic/link/<x> outranks it until the next cfg lands.
MIC_LINK = "ble"
MIC_LINKS = ("ble", "tcp", "auto")
# ---- now playing: what the Mac is playing -> the board's play page ----
# Master switch for the media-control listener; config.json "now_playing".
NOW_PLAYING = True
# Off seats on the board (user's call 2026-09-19). False =
# today's behaviour: the seat dots only draw the agents that are running and a
# swipe skips the ones that quit. True = the board always shows all five and
# swipes through all five, which is what you want while setting the thing up
# ("where did my Codex seat go"). The board has no settings page of its own
# (CLAUDE.md), so this rides down in every cfg as an int 0/1.
SHOW_OFF_SEATS = False
# UI language (user's call 2026-09-24): "zh" = everything exactly as
# before, "en" = English settings page, English TTS phrases + voice, and the
# board's own strings (it reads "lang" off every cfg, NVS-persisted there).
# config.json "lang"; the settings sidebar's 中文 / English switch saves it.
LANG = "zh"
LANGS = ("zh", "en")

def tr(zh, en):
    """Host-side text in the current language (backend why/refusal strings)."""
    return en if LANG == "en" else zh

def tts_voice(lang=None):
    return TTS_VOICES.get(lang or LANG, TTS_VOICES["zh"])

def cfg_msg():
    cfg = {"t": "cfg", "voice": VOICE_STYLE, "mic": 1 if VOICE_SOURCE == "board" else 0,
           "mic_link": MIC_LINK, "show_off": 1 if SHOW_OFF_SEATS else 0, "lang": LANG}
    if PICKUP_PAGE:
        cfg["pickup"] = PICKUP_PAGE
    return cfg
SPEAK_NAMES = {"claude": "Claude", "codex": "Codex",
               "qoder": "Qoder", "qoderwork": "千问", "forest": "Forest"}
SPEAK_NAMES_EN = {"claude": "Claude", "codex": "Codex",
                  "qoder": "Qoder", "qoderwork": "Qwen", "forest": "Forest"}

CONFIG_OVERRIDE = Path.home() / ".agentpet" / "config.json"

# ------------------------------------------------------------------ state
lock = threading.Lock()
state = {a: "off" for a in AGENTS}
claude_sessions = {}     # session_id -> {"state": str, "ts": float, "done_until": float}
# Codex notify hook (~/.codex/config.toml `notify` -> POST /hook/codex/notify).
# Optional precision on top of the process/session-file heuristic.
codex_notify = {"turn_end": 0.0, "needs_since": 0.0, "needs_until": 0.0}
qwen_run = {"streaming": False, "done_until": 0.0,
            "since": 0.0, "needs_until": 0.0}
# QwenWork needs_you heuristic (2026-08-30, user hit the gap live): the app
# has no hooks, and a turn that stops to ASK the user zeroes stream_id
# exactly like a finished one. Closest signal = the turn's final assistant
# message carrying a question near its tail. Real-data calibration: clarify
# turns end "……能再说清楚一点吗，我来帮你。" (question mark ~20 chars from
# the end, final char is 。), delivery turns end "随时告诉我。" (no ？at all),
# and chit-chat questions ("有什么我能帮你的吗？") come from seconds-long
# turns — hence tail-window search + a minimum turn duration, not endswith.
QWEN_ASK_MIN_TURN = 30    # seconds of real work before the question test
QWEN_ASK_TAIL = 60        # chars from the end searched for ？/?
QWEN_NEEDS_TTL = 600      # like codex approvals: stale after 10 min
# QwenWork approval log (2026-09-02, user hit the gap live): the app's own
# session main.log prints "[Permission] registerPendingApproval {...}" when a
# tool call (or AskUserQuestion) waits for the user and "resolvePendingApproval
# hit {...}" when they answer. stream_id stays set the whole wait, so the DB
# alone can only ever say "working" — this is QwenWork's PermissionRequest
# hook in all but name. Requests auto-allowed by its default policy log a
# different line and never reach the user (no card, so no needs_you).
QWEN_LOG_DIR = Path.home() / "Library" / "Application Support" / "QwenWorkCN" / "logs"
QWEN_LOG_GLOB = "*/main.log"
qwen_pending = {"n": 0, "until": 0.0}
# Qoder IDE Quest log (2026-09-02, user caught "working" 4.5 min after a
# 5-second quest): questWindow/agent.log carries each task's state machine
# (initial → prompting → streaming → completed) plus a hasPendingTools flag.
# Authoritative while the log exists; the CPU pair/linger heuristic (which
# cannot tell a finished quest from the IDE re-indexing) only covers an IDE
# that has never opened a Quest window.
# The IDE's own chat (智能体 / 专家团 modes in the editor window) writes the
# same state machine to <launch>/window<N>/agent.log, keyed by a session UUID
# instead of a task id, plus "suspended" while a tool waits for approval
# (trigger permission_request -> needs_you). The Quest tail alone never saw
# those, and since every launch creates a questWindow log the CPU fallback was
# off too: the seat sat on idle through a whole agent turn (second Mac,
# 2026-09-27; samples from this Mac's 2026-08-27 session).
QODER_LOG_DIR = [Path.home() / "Library" / "Application Support" / d / "logs"
                 for d in ("QoderCN", "Qoder")]            # domestic / international build
QODER_LOG_GLOB = "*/questWindow/agent.log"
QODER_WIN_GLOB = "window*/agent.log"                        # inside the newest launch dir
QODER_TASK_TTL = 1800     # a task with no log line this long is not live
qoder_quest = {"tasks": {}, "done_until": 0.0, "needs_until": 0.0}
# Qoder Forest desktop (2026-09-13): the Electron main.log writes
# the chat session state machine as one JSON per line ("Main ChatSession
# control state committed"): runtimeState cold / opening / running /
# waiting-user / ready, pendingInteractionCount, terminalStatus. waiting-user
# is a question the user must answer (the SDK worker runs with
# bypassPermissions, so these are AskUserQuestion-style asks, not tool
# approvals); ready with a terminalStatus is the end of a turn. Authoritative
# while the log exists; the CPU pair/linger heuristic only covers a desktop
# that has never logged a session. Not the embedded CLI's
# qodercli/qoder-agent-sdk.log — that one carries control-plane traffic only.
FOREST_LOG_DIR = [Path.home() / "Library" / "Application Support" / d / "logs"
                  for d in ("com.qodercn.app.stable", "com.qoder.app.stable")]   # domestic / international
FOREST_LOG_GLOB = "*/main.log"
forest_run = {"sessions": {}, "done_until": 0.0}   # sessionId -> (runtimeState, pending, ts)
proc_seen = {a: False for a in AGENTS}
proc_cpu = {a: 0.0 for a in AGENTS}
# CPU busy detection. Agent workloads are short CPU bursts separated by long
# LLM waits (Qoder measured 2026-08-27: 90% peaks, then ~0% for 10s+), while
# idle apps throw isolated spikes (codex resident process, qwen background
# sync at 27-58% once a minute). So: two above-threshold scans within
# CPU_HOT_PAIR_WINDOW arm "busy", which then lingers CPU_WORK_LINGER past the
# last hot scan; a lone spike never arms it.
CPU_HOT_PAIR_WINDOW = 60
CPU_WORK_LINGER = 45
_cpu_hot_prev = {a: 0.0 for a in CPU_WORKING}
_cpu_hot_last = {a: 0.0 for a in CPU_WORKING}
cpu_busy = {a: False for a in CPU_WORKING}
boards = []              # connected TCP client sockets
board_sd = {}            # microSD facts from the board's hello / sd message ({mb, rw, ...})
board_sd_at = 0.0        # when the last sd message arrived (/test/sd waits on it)
board_rtc = {}           # hardware clock facts from the board's rtc message (/test/rtc)
board_rtc_at = 0.0
board_rtc_boot = None    # hello: local epoch the PCF85063 had at boot (0 = none)
board_build = None       # hello: firmware build stamp (__DATE__ __TIME__) and running app slot
board_part = None
# Who wears what, {"claude": "classic", ...}: the board reports it on every
# hello and after every change ({"t":"skins"}). Names mirror
# firmware face.cpp SKIN_NAMES; the page's picker is built from SKIN_NAMES.
board_skins = {}
SKIN_NAMES = ["classic", "kitty", "robo", "bunny", "sprout", "grok"]
board_hello_at = 0.0     # when the last hello arrived (/ota waits for the post-reboot one)
# The board's own vitals, refreshed by every hello and every ping (telemetry
# firmware only -- an older board sends a bare {"t":"ping"} and a hello
# without up/rst, and a missing field must keep the last value, never zero
# it). `at` is when a sample last carried anything; `stale` says the link went
# down since.
board_stats = {"up": None, "heap": None, "min": None, "big": None,
               "rst": None, "at": 0.0, "stale": False}
_board_min_floor = None  # lowest `min` already logged, reset by each new hello
OTA_CARD_PATH = "/agentpet/fw/new.bin"
active_agent = "claude"  # last selection from the board
board_select_at = 0.0    # last seat switch the BOARD asked for (reverse follow yields to it)
host_raise_at = 0.0      # last time WE raised an app (reverse follow must not chase its own tail)
voice_down = False       # the board's voice key is held (never move the seat mid-dictation)
_front_st = {}           # reverse-follow candidate: {"cand": agent, "since": ts}
_last_qian = 0.0         # fortune-reading debounce (impatient tapping)
# The dictation that just happened belonged to whatever was in front on the
# Mac, not to a seat ({"to":"front"} on the voice start).
# The 「⏎ 发送」 bubble's enter must land in the same window, so this outlives
# the voice stop and is only cleared by the next start. It is a FALLBACK: a
# board new enough to mark the dictation also marks the enter, and that field
# wins. A board too old to send either never sets this (its voice starts carry
# no `to`), so old firmware keeps the old behaviour exactly.
_voice_to_front = False

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

def load_overrides():
    global FOCUS_FOLLOW, STRETCH_AFTER_MIN, PICKUP_PAGE, VOICE_STYLE, VOICE_SOURCE, MIC_SINK_DEVICE, MIC_TAIL_S
    global FOLLOW_FRONT, MIC_LINK, FOCUS_SETTLE_S, NOW_PLAYING, SHOW_OFF_SEATS
    global NO_BOARD_RESTART_S, RSS_RESTART_MB, LANG
    if CONFIG_OVERRIDE.exists():
        try:
            cfg = json.loads(CONFIG_OVERRIDE.read_text())
            PROC_RULES.clear()
            PROC_RULES.update({a: dict(r) for a, r in DEFAULT_PROC_RULES.items()})
            for a, r in (cfg.get("proc_rules") or {}).items():
                if r == DEFAULT_PROC_RULES.get(a) or r in LEGACY_PROC_RULES.get(a, []):
                    continue                     # a saved default, not a choice
                PROC_RULES[a] = r
            CPU_WORKING.update(cfg.get("cpu_working", {}))
            FOCUS_APPS.update(cfg.get("focus_apps", {}))
            resolve_app_variants(FOCUS_APPS)
            FOCUS_KEYS.update(cfg.get("focus_keys", {}))
            APPROVE_KEYS.update(cfg.get("approve_keys", {}))
            REJECT_KEYS.update(cfg.get("reject_keys", {}))
            FOCUS_FOLLOW = cfg.get("focus_follow", FOCUS_FOLLOW)
            FOLLOW_FRONT = bool(cfg.get("follow_front", FOLLOW_FRONT))
            try:
                FOCUS_SETTLE_S = max(0.2, min(5.0, float(cfg.get("focus_settle_s", FOCUS_SETTLE_S))))
            except (TypeError, ValueError):
                log("config: focus_settle_s not a number, keeping", FOCUS_SETTLE_S)
            NOW_PLAYING = bool(cfg.get("now_playing", NOW_PLAYING))
            SHOW_OFF_SEATS = bool(cfg.get("show_off_seats", SHOW_OFF_SEATS))
            LANG = cfg.get("lang", LANG)
            if LANG not in LANGS:
                log("config: lang %r not in %s, using zh" % (LANG, "/".join(LANGS)))
                LANG = "zh"
            MIC_LINK = cfg.get("mic_link", MIC_LINK)
            if MIC_LINK not in MIC_LINKS:
                log("config: mic_link %r not in %s, using ble" % (MIC_LINK, "/".join(MIC_LINKS)))
                MIC_LINK = "ble"
            STRETCH_AFTER_MIN = cfg.get("stretch_after_min", STRETCH_AFTER_MIN)
            PICKUP_PAGE = cfg.get("pickup_page", PICKUP_PAGE)
            VOICE_STYLE = cfg.get("voice_style", VOICE_STYLE)
            VOICE_SOURCE = cfg.get("voice_source", VOICE_SOURCE)
            MIC_SINK_DEVICE = cfg.get("mic_sink_device", MIC_SINK_DEVICE)
            MIC_TAIL_S = float(cfg.get("mic_tail_s", MIC_TAIL_S))
            # Not on the settings page (nobody should have to think about
            # these): file-only knobs for the two last-resort restarts, 0 off.
            try:
                NO_BOARD_RESTART_S = float(cfg.get("no_board_restart_s", NO_BOARD_RESTART_S))
            except (TypeError, ValueError):
                log("config: no_board_restart_s not a number, keeping", NO_BOARD_RESTART_S)
            try:
                RSS_RESTART_MB = float(cfg.get("rss_restart_mb", RSS_RESTART_MB))
            except (TypeError, ValueError):
                log("config: rss_restart_mb not a number, keeping", RSS_RESTART_MB)
            log("config override loaded:", CONFIG_OVERRIDE)
        except Exception as e:
            log("config override error:", e)

# ------------------------------------------------------------------ focus follow
_focus_timer = None
# Focus follow wakes the target app, whose UI then burns CPU for a few
# seconds — long enough to arm the CPU working heuristic (user caught codex
# flipping to "working" on every board switch, 2026-08-30). Over-threshold
# scans within this window of our own `open -a` are ignored; file-activity
# signals (codex sessions, qwen stream_id) are untouched, so real work that
# starts right after a switch still shows.
FOCUS_CPU_MUTE = 30
_focus_raised = {}

# Settle window for the board->Mac focus follow. 0.8 s shipped first; the
# user's natural multi-swipe runs at ~1 s per seat (2026-09-05 20:50 log), so
# every seat crossed still got its app raised. 1.5 s covers that rhythm; the
# only cost is the final raise landing 1.5 s after the last swipe (dictation
# is unaffected: the voice key's focus insurance raises the app itself).
FOCUS_SETTLE_S = 1.5

def focus_should_raise(agent_state):
    """A board select raises the app only while the seat is not off: `open -a`
    LAUNCHES a quit app, and scrolling past a grey card (a seat stays visible
    for the session after its app quits) must not do
    that. Decided by the user 2026-09-14 22:30."""
    return agent_state != "off"

def focus_agent(agent):
    # Debounced: a fast multi-swipe streams one select per seat crossed;
    # only raise the app for the seat the user lands on.
    global _focus_timer
    app = FOCUS_APPS.get(agent)
    if not (FOCUS_FOLLOW and app):   # "" = config says this seat has no app
        return
    if _focus_timer:
        _focus_timer.cancel()

    def go():
        global host_raise_at
        with lock:
            st = state.get(agent, "off")
        if not focus_should_raise(st):         # checked when the timer fires, not when queued
            log("focus follow:", agent, "is off, not launching", app)
            return
        r = subprocess.run(["open", "-a", app], capture_output=True, timeout=10)
        _focus_raised[agent] = host_raise_at = time.time()
        log("focus follow:", agent, "->", app,
            "" if r.returncode == 0 else r.stderr.decode(errors="replace").strip())
        keyspec = FOCUS_KEYS.get(agent)
        if keyspec:
            time.sleep(0.6)             # let the window take frontmost first
            post_key(keyspec)

    _focus_timer = threading.Timer(FOCUS_SETTLE_S, go)
    _focus_timer.daemon = True
    _focus_timer.start()

# ------------------------------------------------------------------ ObjC memory
# Every thread this host runs is a plain Python thread: no NSRunLoop, so
# nothing drains the thread's autorelease pool and whatever AppKit /
# CoreFoundation autoreleases on it is leaked silently -- rss climbs while
# the Python heap stays flat, and macOS logs nothing.
#
# Measured 2026-09-15 (a 400-call probe per variant, plain thread,
# /usr/bin/python3 + PyObjC 12): CGWindowListCopyWindowInfo on its own is
# flat, but *iterating* the array it returns leaks ~7.2 KB per call and never
# plateaus. watcher_loop calls _frontmost_app() once a second, which is the
# ~5 MB / 10 min (~8.5 KB/s) the host grew by. Wrapped in
# objc_pool() the same probe is flat at +0 KB after warm-up.
#
# A pool only frees when it is *popped*, so it has to wrap one unit of work
# that is entered and left over and over (one query, one BLE write, one scan).
# A pool held open for a thread's whole life never drains and leaks the same.
#
# Only *synchronous* work goes in one. Pools pop LIFO per thread, and on the
# BLE thread's single event loop a `with objc_pool():` around an `await` lets
# another coroutine open and close its own pool inside that window -- popping
# the outer one then drains an inner pool that is still live (over-release,
# not just a missed drain). So ble_thread / ble_send are deliberately NOT
# pooled here: bleak's own CoreBluetooth backend has no pool anywhere either
# (checked 2026-09-15), so if /debug/mem still shows rss climbing with
# traced_mb flat once the watcher thread is fixed, the fix there is a pool
# *recycled* on the loop thread (drop and re-create between iterations),
# never one held across an await.
try:
    import objc as _objc              # PyObjC core, pulled in by Quartz/AppKit
except ImportError:                   # no PyObjC (CI, Linux): every call is a no-op
    _objc = None

class _NullPool:
    def __enter__(self):
        return None                   # same as objc.autorelease_pool(): yields nothing

    def __exit__(self, *a):
        return False                  # never swallow the caller's exception

def objc_pool():
    """`with objc_pool():` around one unit of ObjC work on a helper thread."""
    return _objc.autorelease_pool() if _objc is not None else _NullPool()

class _PooledCall:
    """One autorelease pool around one synchronous event-loop callback."""
    __slots__ = ("cb",)

    def __init__(self, cb):
        self.cb = cb

    def __call__(self, *a):
        with objc_pool():
            return self.cb(*a)

def pool_loop_callbacks(loop):
    """Give every callback handed to `loop` from another thread its own pool.

    This is how the BLE thread gets pooled without breaking either rule
    above. CoreBluetooth calls bleak's delegate on bleak's own private
    dispatch queue, and that selector does nothing but hand the work to us
    (bleak 1.1.1 CentralManagerDelegate.py:277):

        self.event_loop.call_soon_threadsafe(self.did_discover_peripheral,
                                             central, peripheral, adv, rssi)

    did_discover_peripheral then runs *on our thread*, synchronously, as one
    event-loop callback: peripheral.identifier().UUIDString(), every scanner
    callback walking the advertisement NSDictionary, and a logger.debug over
    advertisementData.keys(). Our thread has no run loop, so each of those
    bridged CFStrings / CFArrays / CFDictionaries is autoreleased into a pool
    nobody pops -- the same disease as _frontmost_app(), one dose per
    advertisement packet. A pool per callback is exactly "one unit of work
    that is entered and left over and over": it never crosses an await (a
    callback is not a coroutine) and it nests strictly inside anything else,
    so nothing can be popped out from under it.

    Measured 2026-09-17 (scan_probe.py, 20 scans of 3 s -- this host's own
    cadence -- on a plain thread, /usr/bin/python3 + PyObjC 12, ~35 devices a
    window, second-half slope so the interpreter is warm):

        none (shipped 09-16)                 56.9 / 80.0 KB per scan
        pooled callbacks only                30.2
        one long-lived scanner only          44.4
        both (this host)                     17.8 / 19.6
        a pool held across the await         21.3   (rule 2: never ship it)

    So the two halves together beat the rule-breaking pool, and the same
    devices are found either way. The rest of a fresh find_device_by_filter
    scan -- allocating a CentralManagerDelegate, its CBCentralManager and its
    dispatch queue, then tearing them all down -- autoreleases inside bleak's
    own coroutines, where we cannot put a pool without holding one across an
    await. That half is fixed by not doing it every scan; see ble_thread().
    """
    if _objc is None:                 # no PyObjC (CI, Linux): nothing to drain
        return loop
    orig = loop.call_soon_threadsafe

    def call_soon_threadsafe(cb, *args, **kw):
        return orig(_PooledCall(cb), *args, **kw)

    loop.call_soon_threadsafe = call_soon_threadsafe
    return loop

def _frontmost_app():
    """Best-effort frontmost app names ('' if unknown): localized name plus
    bundle basename, since open -a targets can differ from localized names
    (QwenWorkCN shows a Chinese title).

    Asks the window server, not NSWorkspace: in this long-lived process
    (no main run loop pumping workspace notifications) NSWorkspace's
    frontmostApplication() is a snapshot that never refreshes — it kept
    answering the app that was front at startup, so the focus insurance
    saw "already front" and dictation landed in the browser (2026-09-05).
    CGWindowListCopyWindowInfo is a fresh query (~3 ms, no TCC): the first
    on-screen layer-0 window belongs to the active app; its PID maps to a
    fresh NSRunningApplication for the names.

    The whole query runs inside objc_pool(): reading the window array bridges
    one CFDictionary per on-screen window and autoreleases them, and on the
    watcher thread (1 Hz, no run loop) that was the host's memory leak -- see
    the objc_pool() note above. The pool is per call, so it drains every tick;
    the leak also scaled with the number of on-screen windows, which is why
    the log shows flat stretches whenever the display was asleep."""
    try:
        import Quartz
        from AppKit import NSRunningApplication
        with objc_pool():
            wl = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly
                | Quartz.kCGWindowListExcludeDesktopElements, Quartz.kCGNullWindowID)
            pid = 0
            for w in wl or []:
                if w.get("kCGWindowLayer", 1) == 0:
                    pid = int(w.get("kCGWindowOwnerPID", 0))
                    names = {str(w.get("kCGWindowOwnerName") or "")}
                    break
            else:
                return ""
            fr = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
            if fr:
                names.add(str(fr.localizedName() or ""))
                url = fr.bundleURL()
                if url:
                    names.add(Path(str(url.path())).name.removesuffix(".app"))
            return "|".join(n for n in names if n)
    except Exception as e:
        log("frontmost query failed:", e)
        return ""

def front_agent(front_str, apps=None):
    """Which seat owns the frontmost app, or None. Takes _frontmost_app()'s
    "bundle|localized" string and matches any segment against FOCUS_APPS,
    case-insensitively (QwenWorkCN answers with a Chinese localized name, so
    one segment matching is enough). Anything else — browser, Finder, an
    editor — is not an agent and must not move the seat."""
    apps = FOCUS_APPS if apps is None else apps
    parts = {seg.strip().lower() for seg in str(front_str or "").split("|") if seg.strip()}
    if not parts:
        return None
    # longest name wins: "Qoder CN IDE" (qoder) and "Qoder CN" (forest) can
    # both appear in one front string (2026-09-13), the specific one owns it
    best = None
    for agent, app in apps.items():
        name = str(app or "").strip().lower()
        if name and name in parts and (best is None or len(name) > len(best[1])):
            best = (agent, name)
    return best[0] if best else None


def front_follow_decide(cand, active, now, st, board_select_at, host_raise_at,
                        voice_down, enabled):
    """Seat to switch to, or None. `st` is the caller's mutable candidate
    memory ({"cand", "since"}); the caller clears it after acting. Pure: no
    clock, no globals, so the whole policy is testable (test_host.py)."""
    if not enabled:
        return None
    if cand is None or cand == active:
        st.clear()                       # already there, or nothing to follow
        return None
    if st.get("cand") != cand:
        st["cand"], st["since"] = cand, now
        return None                      # start the stability window
    if now - st["since"] < FOLLOW_STABLE_S:
        return None                      # still settling (alt-tab fly-by)
    # stable — but yield to anyone with a fresher or louder intent. The
    # candidate keeps its timer so the switch lands as soon as they clear.
    if now - board_select_at < FOLLOW_BOARD_S:
        return None                      # the user just swiped on the board
    if now - host_raise_at < FOLLOW_RAISE_S:
        return None                      # that front app is our own `open -a`
    if voice_down:
        return None                      # mid-dictation: never move the target
    return cand


def front_follow_tick():
    """One watcher pass: frontmost app -> seat. Cheap (~3 ms CG query)."""
    global active_agent
    if not FOLLOW_FRONT:
        return
    try:
        front = _frontmost_app()
    except Exception as e:                # a wedged window server must not kill the watcher
        log("front follow: frontmost query failed:", e)
        return
    with lock:
        cur = active_agent
    agent = front_follow_decide(front_agent(front), cur, time.time(), _front_st,
                                board_select_at, host_raise_at, voice_down, FOLLOW_FRONT)
    if not agent:
        return
    _front_st.clear()
    with lock:
        active_agent = agent
    push_board({"t": "select", "agent": agent, "src": "host"})   # one delivery: two links = two echoes
    log("front follow:", front, "->", agent)


def raise_agent_app(agent, wait=0.5):
    """Focus insurance before typing/dictating into an agent's window (the
    user may have clicked elsewhere since focus follow ran): if the app is
    not frontmost, raise it, give the window server a beat, and re-press its
    input-focus key. Zero delay when it is already front — the common case.
    Runs on the inject worker, never on a link thread: the BLE link handles
    messages inside bleak's asyncio callback, and the first version slept
    here for 0.5 s (10 s worst case on a stuck `open -a`) with the whole
    BLE loop frozen behind it (2026-09-02 review)."""
    global host_raise_at
    app = FOCUS_APPS.get(agent)
    if not (FOCUS_FOLLOW and app):
        return
    host_raise_at = time.time()          # even when already front: reverse follow stands down
    front = _frontmost_app()
    if front and front_agent(front) == agent:
        # whole-segment match, same as focus follow: "Qoder CN" is a prefix of
        # "Qoder CN IDE", and a substring test called the IDE "already front"
        # for the Forest seat, so the approve Enter went to the wrong window
        # (2026-09-13 16:58)
        log("focus insurance:", app, "already front")
        return
    try:
        subprocess.run(["open", "-a", app], capture_output=True, timeout=2)
    except subprocess.TimeoutExpired:
        log("focus insurance: open -a", app, "timed out, typing anyway")
    _focus_raised[agent] = time.time()   # arm the CPU-mute, same as focus follow
    time.sleep(wait)
    keyspec = FOCUS_KEYS.get(agent)
    if keyspec:
        post_key(keyspec)
        time.sleep(0.15)
    log("focus insurance:", agent, "->", app, f"(front was {front or '?'})")

# Single worker for everything that raises a window and then presses keys
# (dictation fn, send-bubble Enter, approve/reject). One queue, one thread:
# a voice "stop" queued behind a still-running "start" waits its turn, so
# fn can never be released before it was pressed — the ordering the inline
# version got for free — while the link threads return immediately.
_inject_q = queue.Queue()

def inject(fn):
    _inject_q.put(fn)

def _inject_worker():
    while True:
        fn = _inject_q.get()
        try:
            fn()
        except Exception as e:
            log("inject error:", e)

threading.Thread(target=_inject_worker, daemon=True).start()

def decision_key(kind):
    # Board tap-to-approve / long-press-to-reject on a needs_you agent.
    agent = active_agent
    spec = (APPROVE_KEYS if kind == "approve" else REJECT_KEYS).get(agent)
    if not spec:
        return

    def go():
        raise_agent_app(agent)     # insurance: the card's app back to front
        post_key(spec)
        log("decision:", kind, agent, "->", spec)

    if agent == "codex":
        # the card is answered now, even if its exec script keeps the
        # rollout quiet for a while longer (no second 批准, no stray Enter)
        with lock:
            codex_notify["perm_done"] = codex_notify.get("perm_call")
    inject(go)

# ------------------------------------------------------------------ voice key
try:
    import Quartz
    _HAVE_QUARTZ = True
except ImportError:
    Quartz = None
    _HAVE_QUARTZ = False

def _post_fn(down: bool):
    """Post a raw fn (globe) key event at the HID tap so IMEs (WeChat input
    method's hold-fn voice mode, etc.) see it exactly like the real key."""
    with objc_pool():                   # inject worker thread, no run loop
        ev = Quartz.CGEventCreateKeyboardEvent(None, FN_KEYCODE, down)
        Quartz.CGEventSetFlags(
            ev, Quartz.kCGEventFlagMaskSecondaryFn if down else 0)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)

def _tap_fn_osascript(times):
    body = "\ndelay 0.15\n".join([f"key code {FN_KEYCODE}"] * times)
    script = f'tell application "System Events"\n{body}\nend tell'
    subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)

def voice_key(start: bool):
    global voice_down
    voice_down = bool(start)            # reverse focus follow freezes while held
    try:
        if VOICE_MODE == "hold_fn" and _HAVE_QUARTZ:
            _post_fn(start)
            log(f"voice: fn {'DOWN (hold)' if start else 'UP (release)'}")
        else:
            _tap_fn_osascript(2 if start else 1)
            log(f"voice: fn tapped ({'start' if start else 'stop'})")
    except Exception as e:
        log("voice key error:", e)

# ------------------------------------------------------------------ key injection
# Board-side "voice remote": {"t":"key","k":"enter"} presses Return (send the
# dictated text), "undo" = Cmd+Z (shake-to-undo; Warp's input editor honors
# Cmd+Z — user-tested 2026-08-29). post_key also fires FOCUS_KEYS after a
# focus-follow app switch. Debug: GET /test/key/<spec>, e.g. /test/key/cmd+z
KEYCODES = {  # macOS virtual keycodes (US layout)
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8,
    "v": 9, "b": 11, "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17,
    "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26,
    "8": 28, "0": 29, "o": 31, "u": 32, "i": 34, "p": 35, "enter": 36,
    "l": 37, "j": 38, "k": 40, "n": 45, "m": 46, "tab": 48, "space": 49,
    "esc": 53,
}

KEY_MODS = ("cmd", "shift", "alt", "option", "ctrl")

def key_spec_ok(spec):
    """True when post_key() below would really press `spec`. Same split, same
    table -- the settings page hands every seat key through here before
    anything reaches config.json, so a typo shows up as a toast instead of as
    a key that silently never fires.

    Stricter than post_key on one point, deliberately: post_key ignores a
    modifier it does not know ("cmd" misspelt = a bare key press), which is
    exactly the typo a person wants told about. Empty is not ok either --
    "no key at all" is spelt as a missing/empty config entry, and callers
    (save_settings) test for that themselves before asking."""
    if not isinstance(spec, str) or not spec or spec != spec.strip():
        return False
    parts = spec.lower().split("+")
    if any(not part or re.search(r"\s", part) for part in parts):
        return False
    if parts[-1] not in KEYCODES:
        return False
    return all(part in KEY_MODS for part in parts[:-1])

def post_key(spec):
    """spec: "enter", "cmd+z", "cmd+shift+l", ..."""
    if not _HAVE_QUARTZ:
        log("post_key: Quartz unavailable")
        return
    parts = spec.lower().split("+")
    kc = KEYCODES.get(parts[-1])
    if kc is None:
        log("post_key: unknown key", spec)
        return
    mm = {"cmd": Quartz.kCGEventFlagMaskCommand,
          "shift": Quartz.kCGEventFlagMaskShift,
          "alt": Quartz.kCGEventFlagMaskAlternate,
          "option": Quartz.kCGEventFlagMaskAlternate,
          "ctrl": Quartz.kCGEventFlagMaskControl}
    flags = 0
    for m in parts[:-1]:
        flags |= mm.get(m, 0)
    with objc_pool():                   # inject worker thread, no run loop
        for down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(None, kc, down)
            Quartz.CGEventSetFlags(ev, flags if down else 0)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
    log("key posted:", spec)

# ------------------------------------------------------------------ watchers
def scan_processes():
    try:
        out = subprocess.run(["ps", "-axo", "pcpu=,args="],
                             capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return
    seen = {a: False for a in AGENTS}
    cpu = {a: 0.0 for a in AGENTS}
    for line in out.splitlines():
        line = line.strip()
        if not line or "agentpet_host" in line:
            continue
        try:
            pcpu_s, args = line.split(None, 1)
            pcpu = float(pcpu_s)
        except ValueError:
            continue
        for agent, rule in PROC_RULES.items():
            if re.search(rule["match"], args):
                if rule["exclude"] and re.search(rule["exclude"], args):
                    continue
                seen[agent] = True
                cpu[agent] += pcpu
    with lock:
        proc_seen.update(seen)
        proc_cpu.update(cpu)
        ts = time.time()
        for a, thr in CPU_WORKING.items():
            _cpu_hot_prev.setdefault(a, 0.0)
            _cpu_hot_last.setdefault(a, 0.0)
            if (cpu.get(a, 0.0) > thr
                    and ts - _focus_raised.get(a, 0.0) >= FOCUS_CPU_MUTE):
                _cpu_hot_prev[a] = _cpu_hot_last[a]
                _cpu_hot_last[a] = ts
            cpu_busy[a] = (
                _cpu_hot_last[a] - _cpu_hot_prev[a] <= CPU_HOT_PAIR_WINDOW
                and ts - _cpu_hot_last[a] < CPU_WORK_LINGER)

def qwen_streams():
    """Active QwenWork agent runs, or -1 if the read-only query fails."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % QWEN_DB, uri=True,
                              timeout=0.3)
        try:
            con.execute("PRAGMA busy_timeout=200")
            return con.execute(
                "SELECT COUNT(*) FROM sub_chats "
                "WHERE stream_id IS NOT NULL AND stream_id != ''"
            ).fetchone()[0]
        finally:
            con.close()
    except Exception:
        return -1

def qwen_asked_question():
    """Did the just-finished QwenWork turn stop to ask the user something?
    One indexed read, called only on the stream-zeroing edge."""
    try:
        con = sqlite3.connect("file:%s?mode=ro" % QWEN_DB, uri=True,
                              timeout=0.3)
        try:
            con.execute("PRAGMA busy_timeout=200")
            row = con.execute(
                "SELECT searchable_text FROM messages "
                "WHERE role='assistant' AND sub_chat_id=("
                "SELECT id FROM sub_chats ORDER BY updated_at DESC LIMIT 1) "
                "ORDER BY sequence DESC LIMIT 1").fetchone()
        finally:
            con.close()
        tail = (row[0] or "").strip()[-QWEN_ASK_TAIL:] if row else ""
        return "？" in tail or "?" in tail
    except Exception:
        return False

def codex_turn_is_main(p):
    """The Codex desktop app runs side threads through the same notify hook —
    e.g. a one-shot "provide a short title for this task" turn a few seconds
    after a task starts (2026-09-05: the user saw a false done, then working
    again). Those threads never get a rollout file under ~/.codex/sessions;
    the real task thread always has one (rollout-<ts>-<thread-id>.jsonl).
    Returns (is_main, reason)."""
    tid = p.get("thread-id") or p.get("thread_id")
    if not tid:
        return True, "no thread id"          # older notify format: keep the old behaviour
    try:
        if any(CODEX_SESSIONS.rglob(f"*{tid}.jsonl")):
            return True, "rollout found"
    except OSError:
        return True, "sessions unreadable"
    first = (p.get("input-messages") or [""])[0] or ""
    kind = "task title" if "short title" in first else "no rollout"
    return False, "side thread (%s): %r" % (kind, first[:40].replace("\n", " "))

# Codex's second kind of approval card (2026-09-27, ChatGPT.app 26.924, code
# mode): the model asks for access with tools.request_permissions(...) inside
# an `exec` call, the app shows a card, and no hook of any kind fires -- the
# PermissionRequest hook covers only escalated shell commands. What the
# rollout shows instead: the exec call line, then nothing but token
# bookkeeping until the card is answered, when its output lands (the same
# second the board's Enter did, 18:16:42). So "the last real line of a recent
# rollout is a request_permissions call" = waiting on the user.
CODEX_PERM_MIN_AGE = 2        # s; a request a standing rule answers at once must not flash needs_you
CODEX_PERM_TTL = 600          # s; same as the hook path's needs_until
CODEX_TAIL_BYTES = 256 * 1024 # tool outputs make long lines; the verdict needs only the last one
_CODEX_SKIP = ("token_usage_record",)          # line types that follow a call without answering it
_CODEX_SKIP_EVENTS = ("token_count",)
_codex_tail_cache = {}        # path -> ((size, mtime), call_id or None)

def codex_rollout_pending(lines):
    """call_id of the request_permissions call a rollout ends on, or None.
    Walks back over token bookkeeping to the last real line. The call line
    says "status": "completed" while its card is still up -- that is about
    the model's output, not the tool's -- so it is not read. Pure."""
    for raw in reversed(lines):
        try:
            d = json.loads(raw)
        except ValueError:
            continue                  # the cut-off head of a tail read
        if not isinstance(d, dict):
            continue
        p = d.get("payload") if isinstance(d.get("payload"), dict) else {}
        if d.get("type") in _CODEX_SKIP or (
                d.get("type") == "event_msg" and p.get("type") in _CODEX_SKIP_EVENTS):
            continue
        if d.get("type") != "response_item":
            return None
        if p.get("type") == "custom_tool_call" and "request_permissions(" in str(p.get("input", "")):
            return p.get("call_id") or "?"
        if p.get("type") == "function_call" and p.get("name") == "request_permissions":
            return p.get("call_id") or "?"
        return None
    return None

def codex_perm_pending(files, now):
    """call_id of a request_permissions card Codex is waiting on, from the
    rollouts in `files` [(mtime, path)] (every recent one: a second thread
    may be writing while the first waits), or None. Tails are cached by
    (size, mtime), so an idle rollout is read once."""
    seen = set()
    hit = None
    for m, p in sorted(files, reverse=True):
        if p.suffix != ".jsonl" or not (CODEX_PERM_MIN_AGE <= now - m < CODEX_PERM_TTL):
            continue
        seen.add(p)
        try:
            st = p.stat()
            key = (st.st_size, st.st_mtime)
            c = _codex_tail_cache.get(p)
            if c is None or c[0] != key:
                with open(p, "rb") as f:
                    f.seek(max(0, st.st_size - CODEX_TAIL_BYTES))
                    lines = f.read().decode("utf-8", "replace").splitlines()
                c = (key, codex_rollout_pending(lines))
                _codex_tail_cache[p] = c
        except OSError:
            continue
        if c[1] and hit is None:
            hit = c[1]
    for p in [p for p in _codex_tail_cache if p not in seen]:
        del _codex_tail_cache[p]
    return hit

def newest_mtime(root: Path, recent=None, since=0.0):
    """Newest file mtime under root; with `recent` (a list), also collects
    (mtime, path) of every file modified at or after `since` in the same walk."""
    latest = 0.0
    if not root.exists():
        return 0.0
    try:
        for p in root.rglob("*"):
            if p.is_file():
                m = p.stat().st_mtime
                if m > latest:
                    latest = m
                if recent is not None and m >= since:
                    recent.append((m, p))
    except Exception:
        pass
    return latest

class LogTail:
    """Follow the newest file matching root/pattern (these apps open a fresh
    dated log dir per launch, so the target moves). The first file is opened
    at EOF — history is never replayed, a stale approval from yesterday must
    not raise needs_you at host start; a file that appears later (app
    relaunched) is read from its beginning. poll() returns complete new
    lines; called from the watcher thread only."""
    def __init__(self, root, pattern):
        # root: one dir, or a list of dirs (same app, domestic / international build)
        self.roots = list(root) if isinstance(root, (list, tuple)) else [root]
        self.root, self.pattern = self.roots[0], pattern
        self.path, self.fh, self.buf = None, None, ""
        self.checked, self.opened_once = 0.0, False

    def _newest(self):
        try:
            cands = [p for r in self.roots for p in r.glob(self.pattern) if p.is_file()]
            return max(cands, key=lambda p: p.stat().st_mtime) if cands else None
        except Exception:
            return None

    def poll(self):
        now = time.time()
        if now - self.checked >= 5:
            self.checked = now
            p = self._newest()
            if p != self.path:
                if self.fh:
                    self.fh.close()
                self.fh, self.buf, self.path = None, "", p
                if p:
                    try:
                        self.fh = open(p, "r", encoding="utf-8", errors="replace")
                        if not self.opened_once:
                            self.fh.seek(0, 2)
                        self.opened_once = True
                        log("logtail:", str(p).replace(str(Path.home()), "~"))
                    except Exception as e:
                        log("logtail open failed:", p, e)
                        self.fh = None
        if not self.fh:
            return []
        try:
            data = self.fh.read()
        except Exception:
            return []
        if not data:
            return []
        self.buf += data
        lines = self.buf.split("\n")
        self.buf = lines.pop()
        return lines

class WindowLogs:
    """Follow every `pattern` file inside the newest launch dir of `roots`
    at once: the Qoder IDE keeps one agent.log per editor window and any of
    them may carry the chat that is working, so following only the newest
    file (LogTail) would flip between windows and lose a turn's end. Files
    present when first seen are opened at EOF (no history replay); files that
    appear later in a watched launch dir, or in a newer one, are new and read
    from the start. poll() returns complete new lines; watcher thread only."""
    def __init__(self, roots, pattern):
        self.roots = list(roots)
        self.pattern = pattern
        self.dir, self.files = None, {}      # path -> [fh, buf]
        self.checked, self.primed = 0.0, False

    def _newest_dir(self):
        try:
            logs = [p for r in self.roots for p in r.glob("*/" + self.pattern) if p.is_file()]
            return max(logs, key=lambda p: p.stat().st_mtime).parent.parent if logs else None
        except Exception:
            return None

    def poll(self):
        now = time.time()
        if now - self.checked >= 5:
            self.checked = now
            d = self._newest_dir()
            if d != self.dir:
                for fh, _ in self.files.values():
                    fh.close()
                self.dir, self.files = d, {}
            if d is not None:
                try:
                    found = [p for p in d.glob(self.pattern) if p.is_file()]
                except Exception:
                    found = []
                for p in found:
                    if p in self.files:
                        continue
                    try:
                        fh = open(p, "r", encoding="utf-8", errors="replace")
                    except Exception as e:
                        log("logtail open failed:", p, e)
                        continue
                    if not self.primed:
                        fh.seek(0, 2)
                    self.files[p] = [fh, ""]
                    log("logtail:", str(p).replace(str(Path.home()), "~"))
            self.primed = True
        out = []
        for entry in self.files.values():
            try:
                data = entry[0].read()
            except Exception:
                continue
            if data:
                lines = (entry[1] + data).split("\n")
                entry[1] = lines.pop()
                out += lines
        return out

_qwen_tail = LogTail(QWEN_LOG_DIR, QWEN_LOG_GLOB)
_qoder_tail = LogTail(QODER_LOG_DIR, QODER_LOG_GLOB)
_qoder_win_tail = WindowLogs(QODER_LOG_DIR, QODER_WIN_GLOB)
_forest_tail = LogTail(FOREST_LOG_DIR, FOREST_LOG_GLOB)
_FOREST_MARK = "Main ChatSession control state committed {"


def forest_apply(line, now, run=None):
    """One Qoder Forest main.log line into `run` (forest_run by default).
    Returns the runtimeState the line carried, or None for any other line.
    Pure apart from log(), so test_host.py feeds it the real samples."""
    run = forest_run if run is None else run
    i = line.find(_FOREST_MARK)
    if i < 0:
        return None
    try:
        d = json.loads(line[i + len(_FOREST_MARK) - 1:])
    except ValueError:
        return None
    if not isinstance(d, dict):
        return None
    sid = str(d.get("sessionId") or "?")
    st = str(d.get("runtimeState") or "")
    pending = int(d.get("pendingInteractionCount") or 0)
    prev = run["sessions"].get(sid, (None, 0, 0.0))[0]
    if st == "ready" and d.get("terminalStatus"):
        # done only on a live→ready edge: a re-logged ready must not chirp
        if prev in ("opening", "running", "waiting-user"):
            run["done_until"] = now + DONE_LINGER
            log("forest: turn", d.get("terminalStatus"))
    elif st == "waiting-user" and prev != "waiting-user":
        log("forest: waiting on the user -> needs_you")
    elif st in ("opening", "running") and prev not in ("opening", "running"):
        log("forest: session", st)
    run["sessions"][sid] = (st, pending, now)
    return st
_RE_QUEST_STATE = re.compile(r'"state":"(initial|prompting|streaming|suspended|completed)"')
_RE_QUEST_TASK = re.compile(r'(task-[0-9a-f]+)')
_RE_QODER_SESSION = re.compile(r'"sessionId":"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"')
_RE_QUEST_PENDING = re.compile(r'"hasPendingTools":(true|false)')
QODER_LIVE = ("prompting", "streaming", "suspended")

def qoder_apply(line, now, quest=None):
    """One Qoder IDE agent.log line into `quest` (qoder_quest by default):
    a Quest window line keyed by its task id, or an editor-window chat line
    keyed by its session UUID. Returns the state the line carried, or None.
    Pure apart from log(), so test_host.py feeds it the real samples."""
    quest = qoder_quest if quest is None else quest
    t = _RE_QUEST_TASK.search(line)
    if t:
        key, kind = t.group(1), "quest"
    else:
        m = _RE_QODER_SESSION.search(line)
        if not m:
            return None
        key, kind = m.group(1), "chat"
    st = None
    s = _RE_QUEST_STATE.search(line)
    if s:
        st = s.group(1)
        prev = quest["tasks"].get(key, (None, 0.0))[0]
        # done only on a live→completed edge: re-rendering an old tab
        # logs "completed" again and must not chirp
        if st == "completed" and prev in QODER_LIVE:
            quest["done_until"] = now + DONE_LINGER
            log("qoder:", kind, key[:13], "completed")
        elif st == "suspended" and prev != "suspended":
            quest["needs_until"] = now + QWEN_NEEDS_TTL
            log("qoder:", kind, key[:13], "waiting for approval -> needs_you")
        elif st != prev and st in ("prompting", "streaming"):
            log("qoder:", kind, key[:13], st)
        if prev == "suspended" and st != "suspended":
            quest["needs_until"] = 0.0           # approved or rejected on the Mac
        quest["tasks"][key] = (st, now)
    p = _RE_QUEST_PENDING.search(line)
    if p:
        if p.group(1) == "true":
            if quest["needs_until"] <= now:
                log("qoder:", kind, key[:13], "waiting on tools -> needs_you")
            quest["needs_until"] = now + QWEN_NEEDS_TTL
        else:
            quest["needs_until"] = 0.0
    return st

def poll_app_logs():
    """Feed the QwenWork approval log and the Qoder Quest log into their
    state dicts (see the notes at qwen_pending / qoder_quest)."""
    now = time.time()
    for line in _qwen_tail.poll():
        if "[Permission]" not in line:
            continue
        if "registerPendingApproval" in line:
            m = re.search(r'"pendingTotal":(\d+)', line)
            qwen_pending["n"] = int(m.group(1)) if m else qwen_pending["n"] + 1
            qwen_pending["until"] = now + QWEN_NEEDS_TTL
            log("qwen: approval pending (%d) -> needs_you" % qwen_pending["n"])
        elif "resolvePendingApproval" in line:
            qwen_pending["n"] = max(0, qwen_pending["n"] - 1)
            if qwen_pending["n"] == 0:
                qwen_pending["until"] = 0.0
            log("qwen: approval resolved, pending", qwen_pending["n"])
    for line in _qoder_tail.poll() + _qoder_win_tail.poll():
        qoder_apply(line, now)
    for line in _forest_tail.poll():
        forest_apply(line, now)

# A user interrupt (Esc / Ctrl-C mid-turn) fires NO Stop hook — documented
# Claude Code behaviour — so the seat sat on "working" until the 120 s decay
# (user noticed 2026-09-05 evening). The transcript does record it though:
# a {"type":"user", "message":{"content":[{"text":"[Request interrupted by
# user]"}]}, "interruptedMessageId": …, "timestamp": …} line. Every hook
# payload carries transcript_path, so a working session's tail is checked
# every couple of seconds for a marker newer than its last hook.
CLAUDE_INTERRUPT_MARK = "[Request interrupted by user]"

def claude_interrupted_after(path, since_ts, tail_bytes=16384):
    """True when the transcript at `path` ends with an interrupt marker whose
    timestamp is later than `since_ts` (the session's last hook). Only the
    tail is read; a marker older than the last hook means the user already
    typed again (UserPromptSubmit came after it) and is ignored."""
    if not path:
        return False
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - tail_bytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return False
    for line in reversed(data.splitlines()):
        if CLAUDE_INTERRUPT_MARK not in line and "interruptedMessageId" not in line:
            continue
        try:
            m = json.loads(line)
        except ValueError:
            continue                      # the cut-off first line of the tail
        ts = m.get("timestamp") or ""
        try:
            t = datetime.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(
                tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            return True                   # marker without a usable time: trust it
        return t > since_ts
    return False

def aggregate():
    now = time.time()
    with lock:
        # ---- claude: hooks are authoritative, process as fallback
        for sid in list(claude_sessions):
            v = claude_sessions[sid]
            if now - v["ts"] > 6 * 3600:
                del claude_sessions[sid]
                continue
            # interrupted mid-turn? (no Stop hook comes; see claude_interrupted_after)
            if v["state"] == "working" and now - v.get("tr_at", 0) >= 2:
                v["tr_at"] = now
                if claude_interrupted_after(v.get("transcript"), v["ts"]):
                    v["state"] = "idle"
                    log("claude interrupted by user (transcript):", sid[:8])
        s = "off"
        if claude_sessions:
            vals = claude_sessions.values()
            if any(v["state"] == "needs_you" for v in vals):
                s = "needs_you"
            elif any(v["state"] == "working" and now - v["ts"] < CLAUDE_WORK_DECAY
                     for v in vals):
                s = "working"
            elif any(v.get("done_until", 0) > now for v in vals):
                s = "done"
            else:
                s = "idle"
        elif proc_seen["claude"]:
            s = "idle"
        state["claude"] = s

        # ---- codex: process + session file activity, refined by notify hook
        if proc_seen["codex"]:
            fresh = []
            mt = newest_mtime(CODEX_SESSIONS, fresh, now - CODEX_PERM_TTL)
            # session writes older than the last turn-complete notify are the
            # finished turn's tail, not new activity (rollout tail lands ~6s
            # after the notify fires — measured 2026-08-27)
            recent = mt > now - ACTIVITY_WINDOW and mt > codex_notify["turn_end"] + 10
            # CPU linger must not outlive a turn-complete notify and mask "done"
            busy = recent or (cpu_busy["codex"]
                              and _cpu_hot_last["codex"] > codex_notify["turn_end"])
            # approval request pending, and no session writes since it fired
            needs = (codex_notify["needs_until"] > now
                     and mt <= codex_notify["needs_since"] + 3)
            # a request_permissions card (no hook fires for it, see
            # codex_rollout_pending); one the board already answered stays
            # answered while the rest of its exec script runs
            perm = codex_perm_pending(fresh, now)
            if perm is not None and perm == codex_notify.get("perm_done"):
                perm = None
            if perm != codex_notify.get("perm_call"):
                log("codex: waiting on a permission card (request_permissions)" if perm
                    else "codex: permission card answered")
                codex_notify["perm_call"] = perm
            if needs or perm:
                state["codex"] = "needs_you"
            elif busy:
                state["codex"] = "working"
            elif 0 < now - codex_notify["turn_end"] < DONE_LINGER:
                state["codex"] = "done"
            else:
                state["codex"] = "idle"
        else:
            state["codex"] = "off"

        # ---- qoder: agent logs (Quest + editor windows) authoritative, CPU
        # heuristic only without any
        if proc_seen["qoder"]:
            if _qoder_tail.path is not None or _qoder_win_tail.files:
                tasks = qoder_quest["tasks"]
                for k in [k for k, (_, ts) in tasks.items()
                          if now - ts > QODER_TASK_TTL]:
                    del tasks[k]
                live = any(st in QODER_LIVE for st, _ in tasks.values())
                if qoder_quest["needs_until"] > now:
                    state["qoder"] = "needs_you"
                elif live:
                    state["qoder"] = "working"
                elif qoder_quest["done_until"] > now:
                    state["qoder"] = "done"
                else:
                    state["qoder"] = "idle"
            else:
                state["qoder"] = "working" if cpu_busy["qoder"] else "idle"
        else:
            state["qoder"] = "off"
            qoder_quest["tasks"].clear()
            qoder_quest["needs_until"] = 0.0

        # ---- forest (Qoder Forest desktop): main.log session state machine
        # authoritative, CPU heuristic only without a log
        if proc_seen["forest"]:
            if _forest_tail.path is not None:
                ss = forest_run["sessions"]
                for k in [k for k, (_, _, ts) in ss.items()
                          if now - ts > QODER_TASK_TTL]:
                    del ss[k]
                waiting = any((st == "waiting-user" or pend > 0)
                              and now - ts < QWEN_NEEDS_TTL
                              for st, pend, ts in ss.values())
                live = any(st in ("opening", "running") for st, _, _ in ss.values())
                if waiting:
                    state["forest"] = "needs_you"
                elif live:
                    state["forest"] = "working"
                elif forest_run["done_until"] > now:
                    state["forest"] = "done"
                else:
                    state["forest"] = "idle"
            else:
                state["forest"] = "working" if cpu_busy["forest"] else "idle"
        else:
            state["forest"] = "off"
            forest_run["sessions"].clear()

        # ---- qoderwork (QwenWork): live stream flag straight from agents.db
        if proc_seen["qoderwork"]:
            n = qwen_streams()
            if n > 0:
                qwen_run["streaming"] = True
                if not qwen_run["since"]:
                    qwen_run["since"] = now
                qwen_run["needs_until"] = 0.0   # user replied / new run
                # an approval card is up (log-tailed, see qwen_pending): the
                # run is blocked on the user although stream_id still says
                # "streaming" — the question-mark heuristic below never sees it
                if qwen_pending["n"] > 0 and qwen_pending["until"] > now:
                    state["qoderwork"] = "needs_you"
                else:
                    state["qoderwork"] = "working"
            elif n == 0:
                if qwen_run["streaming"]:           # run just finished
                    qwen_run["streaming"] = False
                    qwen_pending["n"], qwen_pending["until"] = 0, 0.0  # card died with the run
                    turn_s = now - qwen_run["since"] if qwen_run["since"] else 0
                    qwen_run["since"] = 0.0
                    # a real-work turn that ends on a question = the agent
                    # stopped to ask (see heuristic notes at qwen_run)
                    if turn_s >= QWEN_ASK_MIN_TURN and qwen_asked_question():
                        qwen_run["needs_until"] = now + QWEN_NEEDS_TTL
                        log("qwen: turn ended asking (%.0fs) -> needs_you"
                            % turn_s)
                    else:
                        qwen_run["done_until"] = now + DONE_LINGER
                if qwen_run["needs_until"] > now:
                    state["qoderwork"] = "needs_you"
                else:
                    state["qoderwork"] = ("done" if qwen_run["done_until"] > now
                                          else "idle")
            # n < 0: query failed (transient lock etc.) — keep previous state
        else:
            state["qoderwork"] = "off"
            qwen_run["streaming"] = False
            qwen_run["since"] = 0.0
            qwen_run["needs_until"] = 0.0

# ------------------------------------------------------------------ 今日战报
# Daily battle report: per-agent working seconds + done counts, accumulated
# on the 1 s watcher tick, reset at local midnight, persisted across host
# restarts. The board shows it behind the clock page (right-swipe).
REPORT_FILE = Path.home() / ".agentpet" / "report.json"
report = {"date": "", "w": {a: 0 for a in AGENTS}, "d": {a: 0 for a in AGENTS}}
_report_pushed = None

def load_report():
    try:
        saved = json.loads(REPORT_FILE.read_text())
        if saved.get("date") == datetime.date.today().isoformat():
            report["date"] = saved["date"]
            for a in AGENTS:
                report["w"][a] = int(saved.get("w", {}).get(a, 0))
                report["d"][a] = int(saved.get("d", {}).get(a, 0))
            log("report restored:", REPORT_FILE)
    except Exception:
        pass

def report_msg():
    d = datetime.date.today()
    return {"t": "report", "date": "%02d.%02d" % (d.month, d.day),
            "w": [report["w"][a] // 60 for a in AGENTS],
            "d": [report["d"][a] for a in AGENTS]}

def report_tick(prev, snap):
    global _report_pushed
    today = datetime.date.today().isoformat()
    if report["date"] != today:              # first tick / midnight rollover
        report["date"] = today
        for a in AGENTS:
            report["w"][a] = 0
            report["d"][a] = 0
    for a in AGENTS:
        if snap[a] == "working":
            report["w"][a] += 1
        if snap[a] == "done" and prev and prev.get(a) != "done":
            report["d"][a] += 1
    msg = report_msg()
    key = (msg["date"], tuple(msg["w"]), tuple(msg["d"]))
    if key != _report_pushed:                # at most 1/min per working agent
        _report_pushed = key
        push_msg(msg)
        try:
            REPORT_FILE.write_text(json.dumps(report))
        except OSError as e:
            log("report save error:", e)

def time_msg():
    # board's clock page: local seconds = epoch + off (soft clock in between)
    return (json.dumps({"t": "time", "epoch": int(time.time()),
                        "off": time.localtime().tm_gmtoff}) + "\n").encode()

def push_time():
    push_raw(time_msg() + (json.dumps(almanac_msg()) + "\n").encode())

def watcher_loop():
    prev = None
    ticks = 0
    while True:
        # ps every 3 s (CPU pair/linger logic is tuned to that cadence);
        # aggregate every 1 s so file-mtime agents (QwenWork) show up fast
        if ticks % 3 == 0:
            scan_processes()
        try:
            poll_app_logs()          # QwenWork approvals + Qoder Quest states
        except Exception as e:
            log("app log poll error:", e)
        aggregate()
        stretch_tick()
        try:
            front_follow_tick()          # Mac frontmost app -> board seat
        except Exception as e:
            log("front follow error:", e)
        with lock:
            snap = dict(state)
        report_tick(prev, snap)
        if snap != prev:
            log("state:", snap)
            push_state()
            speak_events(prev, snap)
            prev = snap
        ticks += 1
        if ticks % 600 == 0:      # every ~10 min: re-sync the board clock
            push_time()
            # keep today's fortune voice warm across midnight (cache no-op
            # while the date is unchanged)
            threading.Thread(target=prime_voices, daemon=True).start()
        time.sleep(1)

# ------------------------------------------------------------------ board microphone
# Wire: {"t":"mic","seq":N,"p":peak,"d":base64 IMA ADPCM} 25×/s over TCP while
# the board captures; {"t":"micstat",...} when a session ends; {"t":"mic",ok,on,
# ch,tcp} answers a host {"t":"mic"} command. The board packs the low nibble
# first (WAV order); audioop.adpcm2lin wants the high nibble first, hence the
# swap table — the decode itself is C-speed and state-continuous per session
# (the board resets its encoder at seq 0).
_SWAP_NIB = bytes(((b & 15) << 4) | (b >> 4) for b in range(256))
_mic = {"frames": 0, "bytes": 0, "t0": 0.0, "last": 0.0, "peak": 0, "rms": 0, "seq": -1,
        "gaps": 0, "state": None, "rec": None, "probe": None, "stat": {}, "board": {},
        "times": deque(maxlen=200), "sink_gate": False, "sink_q": deque(maxlen=50),
        "session_board": False, "prev_input": None, "sink_bytes": 0}
_mic_lock = threading.Lock()
_sink_proc = None

def _mic_reset():
    with _mic_lock:
        _mic.update(frames=0, bytes=0, t0=0.0, last=0.0, peak=0, rms=0, seq=-1, gaps=0,
                    state=None, stat={}, sink_bytes=0)
        _mic["times"].clear()
        _mic["sink_q"].clear()

def _mic_frame(m):
    if audioop is None:
        return
    try:
        data = base64.b64decode(m.get("d", ""))
    except Exception:
        return
    now = time.time()
    with _mic_lock:
        seq = int(m.get("seq", 0))
        if seq == 0 or _mic["frames"] == 0:
            _mic["state"] = None
            _mic["t0"] = now
        elif seq != (_mic["seq"] + 1) & 0xFFFF:
            _mic["gaps"] += 1
        _mic["seq"] = seq
        pcm, _mic["state"] = audioop.adpcm2lin(data.translate(_SWAP_NIB), 2, _mic["state"])
        _mic["frames"] += 1
        _mic["bytes"] += len(data)
        _mic["last"] = now
        _mic["times"].append(now)
        _mic["peak"] = audioop.max(pcm, 2)
        _mic["rms"] = audioop.rms(pcm, 2)
        if _mic["rec"] is not None:
            _mic["rec"].append(pcm)
        pr = _mic["probe"]
        if pr and now >= pr["armed_at"]:
            pr["max"] = max(pr.get("max", 0), _mic["peak"])
            if pr.get("hit") is None and _mic["peak"] >= pr["thr"]:
                pr["hit"] = now
                pr["peak"] = _mic["peak"]
        gate = _mic["sink_gate"]
        if not gate:
            _mic["sink_q"].append(pcm)
        sink = _sink_proc
    if gate and sink is not None and sink.poll() is None:
        _sink_write(pcm)

def _sink_write(pcm):
    global _sink_proc
    try:
        _sink_proc.stdin.write(pcm)
        _sink_proc.stdin.flush()
        _mic["sink_bytes"] += len(pcm)
    except (OSError, ValueError) as e:
        log("mic sink write error:", e)
        _sink_proc = None

def mic_sink_proc():
    """The AVAudioEngine helper playing our PCM into MIC_SINK_DEVICE; spawned
    on first use and kept alive (it plays silence when idle)."""
    global _sink_proc
    if _sink_proc is not None and _sink_proc.poll() is None:
        return _sink_proc
    if not MIC_SINK_BIN.exists():
        log("mic sink: helper missing:", MIC_SINK_BIN)
        return None
    try:
        MIC_DIR.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, MIC_SINK_LOG=str(MIC_DIR / "sink_stats.txt"))   # 1 Hz helper status
        p = subprocess.Popen([str(MIC_SINK_BIN), "play", MIC_SINK_DEVICE], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, env=env)
        line = p.stdout.readline().decode(errors="replace").strip()
        if not line.startswith("ready"):
            err = p.stderr.read().decode(errors="replace").strip()
            log("mic sink failed:", line or err)
            return None
        log("mic sink:", line)
        _sink_proc = p
    except Exception as e:
        log("mic sink spawn error:", e)
        return None
    return _sink_proc

def mic_input_set(name):
    """Make `name` the default input; returns the previous device name or None."""
    try:
        r = subprocess.run([str(MIC_SINK_BIN), "input", name], capture_output=True, text=True, timeout=3)
    except Exception as e:
        log("mic input switch error:", e)
        return None
    if r.returncode != 0:
        log("mic input switch failed:", r.stderr.strip())
        return None
    return r.stdout.strip()

def mic_sink_begin():
    """Route the pet's mic to the input method: sink up, BlackHole as default
    input. Returns True when the path is ready (else the Mac mic is used)."""
    if mic_sink_proc() is None:
        return False
    prev = mic_input_set(MIC_SINK_DEVICE)
    if prev is None:
        return False
    with _mic_lock:
        if _mic["prev_input"] is None and prev.lower() != MIC_SINK_DEVICE.lower():
            _mic["prev_input"] = prev
    return True

def mic_sink_open_gate():
    with _mic_lock:
        _mic["sink_gate"] = True
        queued = list(_mic["sink_q"])
        _mic["sink_q"].clear()
    if _sink_proc is not None and _sink_proc.poll() is None:
        for pcm in queued:
            _sink_write(pcm)

def mic_sink_end():
    with _mic_lock:
        _mic["sink_gate"] = False
        _mic["sink_q"].clear()
        prev = _mic["prev_input"]
        _mic["prev_input"] = None
    if prev:
        mic_input_set(prev)

def mic_snapshot():
    with _mic_lock:
        now = time.time()
        recent = [t for t in _mic["times"] if now - t <= 2.0]
        span = (recent[-1] - recent[0]) if len(recent) > 1 else 0
        return {"live": now - _mic["last"] < 0.5 if _mic["last"] else False,
                "frames": _mic["frames"], "gaps": _mic["gaps"], "wire_bytes": _mic["bytes"],
                "fps": round((len(recent) - 1) / span, 1) if span > 0 else 0,
                "kbps": round(_mic["bytes"] * 8 / 1000 / (_mic["last"] - _mic["t0"]), 1)
                        if _mic["last"] > _mic["t0"] else 0,
                "peak": _mic["peak"], "rms": _mic["rms"], "sink_bytes": _mic["sink_bytes"],
                "sink": _sink_proc is not None and _sink_proc.poll() is None,
                "gate": _mic["sink_gate"], "board": _mic["board"], "stat": _mic["stat"],
                "source": VOICE_SOURCE, "sink_device": MIC_SINK_DEVICE, "tail_s": MIC_TAIL_S}

def mic_record(seconds):
    """Capture `seconds` of the board mic into ~/.agentpet/mic/rec_<ts>.wav (16 kHz mono)."""
    _mic_reset()
    with _mic_lock:
        _mic["rec"] = []
    push_board({"t": "mic", "on": 1})
    time.sleep(seconds)
    push_board({"t": "mic", "on": 0})
    time.sleep(0.3)
    with _mic_lock:
        chunks = _mic["rec"] or []
        _mic["rec"] = None
    pcm = b"".join(chunks)
    MIC_DIR.mkdir(parents=True, exist_ok=True)
    path = MIC_DIR / ("rec_" + datetime.datetime.now().strftime("%Y%m%d_%H%M%S") + ".wav")
    for dst in (path, MIC_DIR / "latest.wav"):
        with wave.open(str(dst), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(pcm)
    snap = mic_snapshot()
    snap.update(path=str(path), seconds=round(len(pcm) / 32000, 2),
                peak_all=audioop.max(pcm, 2) if pcm else 0, rms_all=audioop.rms(pcm, 2) if pcm else 0)
    return snap

def mic_latency(runs=3, sound="tick"):
    """Pet-speaker click -> board mic -> TCP -> decode: ms from the sound
    command leaving the host to the first frame whose peak clears the floor."""
    _mic_reset()                                 # fresh session: floor must come from live frames
    push_board({"t": "mic", "on": 1})
    for _ in range(30):                          # wait for the stream (≤1.5 s)
        time.sleep(0.05)
        if _mic["frames"] >= 3:
            break
    out = []
    for _ in range(runs):
        floor = 0
        for _ in range(10):                      # 0.4 s of ambient peaks
            time.sleep(0.04)
            floor = max(floor, _mic["peak"])
        thr = max(floor * 4, 2000)
        with _mic_lock:
            _mic["probe"] = {"thr": thr, "armed_at": time.time(), "hit": None}
        t0 = time.time()
        push_tcp({"t": "sound", "name": sound})
        hit = None
        while time.time() - t0 < 2.0:
            time.sleep(0.01)
            with _mic_lock:
                hit = _mic["probe"].get("hit")
            if hit:
                break
        with _mic_lock:
            pr = _mic["probe"]; _mic["probe"] = None
        out.append({"ms": round((hit - t0) * 1000) if hit else None, "floor": floor, "thr": thr,
                    "peak": pr.get("peak"), "max_in_window": pr.get("max", 0)})
        time.sleep(0.7)
    push_board({"t": "mic", "on": 0})
    ms = sorted(r["ms"] for r in out if r["ms"] is not None)
    return {"runs": out, "median_ms": ms[len(ms) // 2] if ms else None, "live": mic_snapshot()}

# ------------------------------------------------------------------ shared board messages
_bad_lines = [0]

# BLE bandwidth probe: the board floods {"t":"bt","s":N,"d":…}
# lines for a few seconds, this side counts bytes, lines and sequence gaps.
_bt = {"lines": 0, "bytes": 0, "gaps": 0, "last": -1, "t0": 0.0, "t1": 0.0,
       "btack": None, "btend": None, "t_end": 0.0}

def _bt_reset():
    _bt.update(lines=0, bytes=0, gaps=0, last=-1, t0=0.0, t1=0.0, btack=None, btend=None, t_end=0.0)

def _bt_line(m, nraw):
    now = time.time()
    if not _bt["t0"]:
        _bt["t0"] = now
    _bt["t1"] = now
    _bt["lines"] += 1
    _bt["bytes"] += nraw + 1                       # + the newline the splitter ate
    try:
        seq = int(m.get("s"))
    except (TypeError, ValueError):
        return
    if _bt["last"] >= 0 and seq != _bt["last"] + 1:
        _bt["gaps"] += 1
    _bt["last"] = seq

def ble_bandwidth(sec=3, ln=470, chunk=0, gap=0, itvl=0):
    """Run the board's BLE flood for `sec` seconds and return both sides' counts."""
    if _ble_client is None:
        return {"error": "no BLE link"}
    _bt_reset()
    bad0 = _bad_lines[0]
    ble_send((json.dumps({"t": "bletest", "sec": sec, "len": ln, "chunk": chunk,
                          "gap": gap, "itvl": itvl}) + "\n").encode())
    deadline = time.time() + sec + 5
    while time.time() < deadline and _bt["btend"] is None:
        time.sleep(0.1)
    span = _bt["t1"] - _bt["t0"]
    out = {"host": {"lines": _bt["lines"], "bytes": _bt["bytes"], "gaps": _bt["gaps"],
                    "span_ms": round(span * 1000), "bad_lines": _bad_lines[0] - bad0,
                    "bps": round(_bt["bytes"] / span) if span > 0.2 else None},
           "board": _bt["btend"], "ack": _bt["btack"],
           "params": {"sec": sec, "len": ln, "chunk": chunk, "gap": gap, "itvl": itvl}}
    if _bt["btend"] is None:
        out["error"] = "no btend from board (old firmware or link dropped)"
    log("ble bw:", out["host"], "board", _bt["btend"])
    return out

# ------------------------------------------------------------------ board telemetry
# board side: the firmware puts its uptime and
# heap on every ping so a week-long soak shows a slow leak in the log instead
# of a silent reboot. Every field is optional -- the firmware that shipped
# before them sends a bare ping, and that must read as "unchanged", never 0.
def board_stat_update(m):
    """Fold one hello or ping into board_stats. Fields are optional (old
    firmware has none of them): what is missing keeps its last value."""
    global _board_min_floor
    got = False
    for k in ("up", "heap", "min", "big"):
        v = m.get(k)
        if isinstance(v, int) and not isinstance(v, bool):
            board_stats[k] = v
            got = True
    rst = m.get("rst")
    if isinstance(rst, str):
        board_stats["rst"] = rst
        got = True
    if got:
        board_stats["at"] = time.time()
        board_stats["stale"] = False
    mn = m.get("min")                # this message's, not a previous link's
    if isinstance(mn, int) and not isinstance(mn, bool) \
            and (_board_min_floor is None or mn < _board_min_floor):
        # one line per new low water mark -- a healthy board prints a handful
        # in its first minutes and then goes quiet; a leaking one never stops
        _board_min_floor = mn
        log("board heap: min fell to %d KB (big %d KB, up %d min)"
            % (mn // 1024, (board_stats["big"] or 0) // 1024,
               (board_stats["up"] or 0) // 60))

def board_mem_note():
    """The ` board ...` tail of a `mem:` line -- empty until a board has
    reported once, so a host that never saw one keeps its old one-line log."""
    if not board_stats["at"]:
        return ""
    def kb(k):
        v = board_stats[k]
        return "%d KB" % (v // 1024) if isinstance(v, int) else "? KB"
    up = board_stats["up"]
    return (" board up=%s heap=%s min=%s big=%s"
            % ("%dmin" % (up // 60) if isinstance(up, int) else "?",
               kb("heap"), kb("min"), kb("big")))

def board_link_down():
    """A link dropped: the last vitals stay readable, they are just not
    current any more. This
    pet has two links -- while the other one is still up the board keeps
    reporting, so nothing goes stale."""
    if boards or _ble_client is not None:
        return
    board_stats["stale"] = True

def board_snapshot():
    """/state.board: the board's own vitals, alongside the `fw` / `sd` / `rtc`
    blocks that already carry its hello fields. `up` comes from whichever of
    hello / ping arrived last, so it tracks the pings."""
    now = time.time()
    return {"up": board_stats["up"], "heap": board_stats["heap"],
            "min": board_stats["min"], "big": board_stats["big"],
            "rst": board_stats["rst"],
            "age_s": round(now - board_stats["at"]) if board_stats["at"] else None,
            "stale": board_stats["stale"],
            # why the link is down: the BLE thread's last
            # note, forced empty while any link -- BLE or TCP -- is up so a
            # green dot never carries a stale "bluetooth off" next to it
            "link_reason": "" if (_ble_client is not None or boards) else ble_reason,
            "hello_age_s": round(now - board_hello_at) if board_hello_at else None}

def handle_board_msg(raw, reply):
    global active_agent, board_sd, board_sd_at, board_rtc, board_rtc_at, board_rtc_boot
    global board_build, board_part, board_hello_at, board_select_at, _board_min_floor, board_skins
    try:
        m = json.loads(raw)
    except ValueError:
        _bad_lines[0] += 1
        if _bad_lines[0] <= 5 or _bad_lines[0] % 100 == 0:
            log("board line not JSON (#%d, %d B):" % (_bad_lines[0], len(raw)), raw[:70], "..", raw[-24:])
        return
    if not isinstance(m, dict):          # "[1,2]" / "42" are valid JSON, not messages
        return
    t = m.get("t")
    if t == "ping":
        # the BOARD's own heartbeat, not our /test/rtt probe:
        # that one goes host -> board and comes back as {"t":"pong"}
        board_stat_update(m)
        reply(b'{"t":"pong"}\n')
    elif t == "hello":
        log("board hello:", m)
        board_hello_at = time.time()
        _board_min_floor = None       # a rebooted board starts its own descent
        board_stat_update(m)
        # A reboot puts the board back on seat 0 while we still hold whatever
        # seat reverse follow left us on (20:39 codex -> 20:45 OTA -> 20:48 the
        # board shows Claude and we still type at codex). The hello says where
        # it actually woke up: realign quietly, exactly like `select src:auto`
        # -- no `open -a` (nobody asked for an app) and no lockout. An old
        # firmware sends no `sel` at all, so a missing/unknown id changes nothing.
        sel = m.get("sel")
        if sel in AGENTS and sel != active_agent:
            active_agent = sel
            log("board hello: seat ->", sel)
        board_build = m.get("build", board_build)
        board_part = m.get("part", board_part)
        if isinstance(m.get("skins"), dict):
            board_skins = m["skins"]
        if "rtc" in m:
            board_rtc_boot = m.get("rtc")
        if "sd" in m:
            board_sd = {"mb": m.get("sd"), "rw": m.get("sdrw")}
            if not m.get("sd"):
                font_step("nocard")
            if m.get("sd") and not m.get("link"):        # TCP hello + card present
                threading.Thread(target=fonts_ensure, args=(2,), daemon=True).start()
                threading.Thread(target=push_queue_drain, daemon=True).start()
                def _year():
                    time.sleep(3)                          # let hello traffic settle
                    r = almanac_push_year()
                    if not r.get("skipped"):
                        log("almanac year push:", r)
                threading.Thread(target=_year, daemon=True).start()
                threading.Thread(target=au_push_pending, args=(6,),   # after the almanac, same reason
                                 daemon=True).start()
        if not m.get("link"):            # TCP: say who we are (IP drift, first owner)
            reply(hostinfo_msg())
        reply(time_msg())
        reply((json.dumps(almanac_msg()) + "\n").encode())
        reply((json.dumps(report_msg()) + "\n").encode())
        reply((json.dumps(cfg_msg()) + "\n").encode())
        np_now = np_hello_msg()          # a board that just booted knows nothing about the play page
        if np_now:
            reply((json.dumps(np_now) + "\n").encode())
            with np_lock:
                np_rt["msg"] = np_now
                np_rt["sent_at"] = time.time()
    elif t == "media":                   # play page, Mac source: pet -> the Mac's player
        threading.Thread(target=media_cmd, args=(m.get("cmd", ""),), daemon=True).start()
    elif t == "np_miss":                 # the board wants a cover that is not on the card
        threading.Thread(target=np_miss, args=(m.get("cover", ""),), daemon=True).start()
    elif t in ("fack", "fls", "fcat", "frm", "ota", "spk", "pong", "plst"):   # answers to SD file / OTA / speak / rtt / player endpoints
        _park_reply(m)
    elif t == "skins":            # who wears what, after a change on the card or from /test/skin
        if isinstance(m.get("skins"), dict):   # same key as on the hello
            board_skins = m["skins"]
            log("board skins:", m["skins"])
    elif t == "wifi":             # board Wi-Fi list; never carries passwords
        _park_reply(m)
    elif t in ("owner", "released"):   # 认领制
        owner_msg(m)
    elif t == "pin":              # 钉住 state echo, answer to /test/pin/0|1
        log("board pin:", m)
        _park_reply(m)
    elif t in ("sbeg", "sdat", "sfin"):        # screenshot stream, see _shot_part
        _shot_part(m)
    elif t == "gaze":             # answer to /test/gaze (眼神追声)
        _park_reply(m)
    elif t == "rtc":              # answer to /test/rtc
        board_rtc = {k: v for k, v in m.items() if k != "t"}
        board_rtc_at = time.time()
        log("board rtc:", m)
    elif t == "sd":               # card mounted (hot-plug) or answer to /test/sd
        board_sd = {k: v for k, v in m.items() if k != "t"}
        board_sd_at = time.time()
        log("board sd:", m)
        if m.get("mb"):                   # a card just went in: it may be a blank one
            threading.Thread(target=fonts_ensure, args=(1,), daemon=True).start()
    elif t == "select":
        src = m.get("src")
        if src == "host":
            # the board echoing a seat WE pushed (reverse focus follow): not a
            # user intent — no app raise, and the board lockout stays untouched
            log("board confirmed select:", m.get("agent", active_agent))
            return
        if src == "auto":
            # the selected seat went off and the board moved itself: dictation
            # must follow the new seat, but nobody asked for an app — no raise
            # (`open -a` would relaunch the app that just quit), and no lockout
            # on reverse follow
            active_agent = m.get("agent", active_agent)
            log("board auto-selected agent:", active_agent)
            return
        active_agent = m.get("agent", active_agent)
        board_select_at = time.time()
        log("board selected agent:", active_agent)
        focus_agent(active_agent)
    elif t == "voice":
        start = m.get("a") == "start"
        agent = m.get("agent") or active_agent
        if start:
            board_mic = VOICE_SOURCE == "board" and bool(m.get("mic"))
            # {"to":"front"} — the board is on a desk page (clock / almanac),
            # where dictation is for whatever the user is typing in right now,
            # not for a seat. Every other page, and every board too old to
            # send the field, keeps the raise.
            to_front = m.get("to") == "front"
            global _voice_to_front
            _voice_to_front = to_front      # ... and so does the enter that follows
            _mic_reset()
            with _mic_lock:
                _mic["session_board"] = board_mic

            def focus():
                # asked inside the worker, at the moment fn goes down: the
                # frontmost app is only true then (2026-09-05 stale-snapshot)
                if to_front:
                    log("voice: -> front (%s)" % (_frontmost_app() or "?"))
                else:
                    raise_agent_app(agent)

            if board_mic:
                # route A: sink up + BlackHole as default input BEFORE fn goes
                # down; frames that arrived meanwhile are flushed once it is
                def go():
                    ok = mic_sink_begin()
                    focus()
                    voice_key(True)
                    if ok:
                        mic_sink_open_gate()
                    log("voice: board mic (%s) ->" % m.get("link", "?"), MIC_SINK_DEVICE if ok else "FAILED, Mac mic")
                inject(go)
            else:
                # the user may have clicked another window since focus follow ran
                # (2026-09-01 gap): dictation must land in the agent's input box.
                # Queued, not inline — see inject()
                inject(lambda: (focus(), voice_key(True)))
        else:
            with _mic_lock:
                board_mic = _mic["session_board"]
                _mic["session_board"] = False
            if board_mic:
                def stop():
                    time.sleep(MIC_TAIL_S)       # let the tail of the audio reach the input method
                    voice_key(False)
                    time.sleep(0.5)              # input method finalizes on the current device
                    mic_sink_end()
                inject(stop)
            else:
                inject(lambda: voice_key(False))
    elif t == "mic":
        if "d" in m:
            _mic_frame(m)
        else:                                  # answer to a host {"t":"mic"} command
            _mic["board"] = {k: v for k, v in m.items() if k != "t"}
            log("board mic:", _mic["board"])
    elif t == "bt":                      # BLE bandwidth probe line (/test/ble/bw)
        _bt_line(m, len(raw))
    elif t in ("btack", "btend"):
        _bt[t] = {k: v for k, v in m.items() if k != "t"}
        if t == "btend":
            _bt["t_end"] = time.time()
            log("board btend:", _bt["btend"])
    elif t == "micstat":
        _mic["stat"] = {k: v for k, v in m.items() if k != "t"}
        log("board micstat:", _mic["stat"], "host frames", _mic["frames"], "gaps", _mic["gaps"])
        if m.get("dropped") or m.get("stale") or (m.get("loop_max_ms", 0) > 500):
            log("mic link weak: rssi %s dBm, dropped %s, stale %s, loop max %s ms" %
                (m.get("rssi"), m.get("dropped"), m.get("stale"), m.get("loop_max_ms")))
    elif t == "key":                     # board voice-remote / decisions
        k = m.get("k", "")
        if k == "enter":
            # The 「⏎ 发送」 bubble. {"to":"front"} = the dictation it is sending
            # went into whatever the user has in front (a desk page started it),
            # so raising a seat's window here would type the enter into the
            # wrong app -- and, worse, send whatever half-line was sitting in
            # that agent's box. The board's field decides; `_voice_to_front` is
            # only the fallback for a board that marks the voice start but not
            # the key, and stays False for firmware that marks neither.
            to = m.get("to")
            to_front = (to == "front") if to is not None else _voice_to_front
            if to_front:
                inject(lambda: (log("key: enter -> front (%s)" % (_frontmost_app() or "?")),
                                post_key("enter")))
            else:
                target = active_agent
                inject(lambda: (raise_agent_app(target), post_key("enter")))
        elif k == "undo":
            post_key("cmd+z")
        elif k in ("approve", "reject"):
            decision_key(k)
        log("board key:", k)
    elif t == "qian":                    # almanac tapped: read today's fortune
        global _last_qian
        now = time.time()
        if now - _last_qian < 6:         # impatient tapping while it plays
            return
        _last_qian = now
        pick = m.get("pick", 0)
        log("board qian: reading fortune", ("(alt %s)" % pick) if pick else "")
        threading.Thread(target=say_qian, args=(pick,), daemon=True).start()

# ------------------------------------------------------------------ BLE bridge
NUS_SVC = "6e400001-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
BLE_NAME = "AgentPet"
BLE_RX_TIMEOUT = 35       # board pings every 10s; no rx for this long = dead link
# The board pings EACH link every 10 s (main.cpp keepalive). So while its TCP
# link is beating, a BLE link silent past one and a half beats is a zombie the
# host kept through a sleep: the board dropped it long ago (supervision
# timeout) but CoreBluetooth never said so. Waiting the full 35 awake seconds
# kept Bluetooth down for ~38 s after the lid opened, TCP back in 4 (second
# Mac, 2026-09-27 15:41).
BLE_RX_TIMEOUT_TCP = 15
TCP_FRESH_S = 12          # "TCP is beating": bytes from a TCP board this recently
# The rx watchdog counts AWAKE seconds only. On this Mac's Python (3.9)
# time.monotonic() is mach_absolute_time(), which stops while the system
# sleeps; time.time() does not. With the wall clock, the first DarkWake after a
# clamshell sleep (every ~50 s, all night) found "35 s without a ping" -- pings
# the board had been sending into a link the sleeping Mac was not reading -- and
# dropped a perfectly good link. Worse, bleak's disconnect() returns without
# cancelling when that manager no longer sees the peripheral as connected, so
# the controller kept the link, the board never re-advertised, and the host
# scanned for two hours until the no-board guard restarted it (2026-09-18,
# three times in one night: 02:02, 04:10, 06:16). Awake time is the only time
# the board can be judged in.
ble_awake_clock = time.monotonic
# After a watchdog drop the board usually still holds the link (the Mac's
# cancel never reached the controller), so it never re-advertises and no scan
# can find it. A few normal, empty windows after such a drop = restart now,
# not after the two-hour guard (2026-09-18: 3/3 instant reconnects).
# Eight windows is ~3 min at the fast-then-slow cadence; the two-hour guard
# stays as the backstop for a board that is simply gone. S3:
# the restart goes ahead even while the board is also on Wi-Fi TCP -- a zombie
# BLE link is where the board would otherwise keep sending its dictation
# stream (mic_link defaults to ble), and TCP reconnects ~1 s after relaunch.
BLE_STALE_LINK_RESCANS = 8
# The rx watchdog judges nothing while the display is asleep. The first
# night on the awake clock (2026-09-19): every DarkWake after the clamshell
# sleep was long enough to count 35 awake seconds, and after any sleep entry
# -- even a 3 s one -- the process never hears another notify on the link it
# holds (the controller and the board keep it; bleak's CBPeripheral is a
# zombie). So the awake clock alone turned "dropped every 2 h" into "dropped
# every 3.5 min", seventeen restarts in an hour. With the lights out nobody is
# looking at the panel and nothing the board sends can reach us, so the only
# honest verdict is none: the timer is re-armed each tick and the link is
# judged again from the first 35 awake seconds after the display comes back.
# A board that really died in the dark is caught then; one that merely slept
# through it (the usual case) still has its link, pings resume, nothing fires.
# Display sleep is read from the window server (CGDisplayIsAsleep, ~7 us, no
# fork, no run loop, the same way _frontmost_app() asks it).
_coregraphics = None
# A connect that has not finished in this long never will. bleak 1.1.1's
# CentralManagerDelegate.connect() bounds the connection itself (10 s) but on
# that timeout sends cancelPeripheralConnection_ and then awaits the disconnect
# callback with no bound at all -- and a manager the Mac slept on mid-connect
# never delivers it (2026-09-20 00:02 -> 07:59, seven hours fifty-
# seven minutes with a healthy board advertising next to it). Its disconnect()
# has the same unbounded await. Both go through ble_link_open / ble_link_close.
BLE_CONNECT_TIMEOUT_S = 30
BLE_DISCONNECT_TIMEOUT_S = 15

# Scan cadence: a board that was seen recently gets the fast plan so a
# reboot reconnects in seconds; after BLE_IDLE_BACKOFF_S with no board at all we
# drop to one short scan per ~30 s so an idle host stays quiet.
BLE_SCAN_FAST = (8.0, 4.0)      # (scan seconds, pause seconds)
BLE_SCAN_SLOW = (3.0, 27.0)
BLE_IDLE_BACKOFF_S = 60
# A scanner start or stop that has not answered in this long is a central
# manager that will never answer: the Mac slept mid-cycle (2026-09-17 17:35,
# clamshell) and CoreBluetooth silently dropped the scan request because the
# manager was no longer powered on, so bleak's start() waited forever for a
# did-start-scanning event -- five hours, with the board advertising at -32 dBm
# next to it. A fresh scanner re-checks power on construction; a reused one
# never does, so the reuse needs this bound.
BLE_SCAN_OP_TIMEOUT_S = 15
# CoreBluetooth inside a long-running process can stay "powered off" after a Mac
# sleep while macOS itself says Bluetooth is on (2026-09-13 23:46 ->
# 09-14 21:27).  A fresh process recovers, so after this many consecutive
# "turned off" scan errors -- and only if macOS really says on -- the host exits
# and lets launchd KeepAlive bring it back.
BLE_OFF_RESTART_N = 6
BLE_OFF_MIN_UPTIME_S = 60       # a brand-new process reports off once or twice
HOST_T0 = time.time()

# Last resort, for the failures we cannot name. ble_off_verdict above catches a
# CoreBluetooth that admits it is off; these two catch a scan branch that has
# quietly stopped finding a board that is right there, and native memory we
# have not accounted for (the 09-16 night: 76 -> 108 MB while no board was
# around). A fresh process costs nothing when there is no board: no link to
# break, no session to lose -- which is also why both guards are dead while a
# board is connected, whatever the numbers say. 0 turns a guard off.
NO_BOARD_RESTART_S = 7200       # 2 h with no board linked at all
RSS_RESTART_MB = 200            # ~2.6x a healthy day (76 MB)
# mem_loop sleeps in short ticks and compares the wall clock, because a single
# 600 s timed wait on this Python does not count time the Mac spends asleep:
# through the 2026-09-19 night (135 DarkWakes of ~22 s) the ten-minute
# timer did not complete once, so the no-board guard fired seven minutes after
# the lid opened rather than seconds. A tick of 30 s bounds that delay.
MEM_LOOP_TICK_S = 30

_ble_loop = None
_ble_client = None
link_owner_cb = [lambda m: owner_msg(m)]   # the BLE link's owner-answer hook (ble_thread)
_ble_last_link_at = HOST_T0     # when a board was last linked (or process start); mem_loop's guard reads it
_tcp_last_link_at = 0.0         # S3: when the last TCP board session ended (0 = never had one)
_tcp_rx_at = 0.0                # wall time of the last bytes from a TCP board (rx watchdog's witness)

# Why the link is down, for the settings page's 板子 row. Written by the BLE
# thread at three points only (scan came back empty / "turned off" / about to
# self-restart), read by board_snapshot(). Closed set:
#   ""          a board is linked, or we have not scanned yet
#   "scanning"  the stack answers fine, we just have not found the board
#   "bt_off"    CoreBluetooth says the Bluetooth device is turned off
#   "stuck"     ... while macOS says it is on, so the stack is wedged and we
#               are restarting ourselves
# "bt_off" is taken at face value until the verdict disproves it: the ordinary
# cause really is someone turning Bluetooth off, and a wedged stack corrects
# itself to "stuck" within BLE_OFF_RESTART_N failed scans (~25 s).
BLE_REASONS = ("", "scanning", "bt_off", "stuck")
ble_reason = ""

def ble_note(reason):
    """Record why the link is down; returns what is now recorded. A value the
    page cannot spell is dropped rather than shown."""
    global ble_reason
    if reason in BLE_REASONS:
        ble_reason = reason
    return ble_reason

def ble_scan_plan(idle_s):
    """(scan_s, pause_s) for the next scan given seconds since a board was last linked."""
    return BLE_SCAN_SLOW if idle_s >= BLE_IDLE_BACKOFF_S else BLE_SCAN_FAST

def ble_is_our_board(local_name, cached_name):
    """Scan filter: the advertised name only. Another board may advertise the
    same NUS service UUID under its own name, so the UUID does not
    tell ours apart and this host must connect nothing but BLE_NAME. The
    scan-response local name wins over CoreBluetooth's cached peripheral name,
    which can lag a firmware rename."""
    return (local_name or cached_name) == BLE_NAME

def ble_off_verdict(consec_off, sys_bt_on, uptime_s):
    """'ok' keep scanning / 'wait' macOS Bluetooth is really off / 'restart' stuck."""
    if consec_off < BLE_OFF_RESTART_N:
        return "ok"
    if sys_bt_on is False:
        return "wait"
    if uptime_s < BLE_OFF_MIN_UPTIME_S:
        return "ok"
    return "restart"

def stale_link_verdict(dropped_by_watchdog, empty_scans, limit=None):
    """True when the host should restart to shake off a link the board still
    holds: the last link ended in our rx watchdog (not a disconnect event,
    not an error) and `empty_scans` windows since then answered normally with
    nothing. Pure; the BLE loop feeds it and the tests drive it."""
    limit = BLE_STALE_LINK_RESCANS if limit is None else limit
    return bool(dropped_by_watchdog) and limit > 0 and empty_scans >= limit

def rx_watchdog_verdict(idle_s, display_asleep, limit=None, tcp_fresh=False):
    """'drop' the link is dead / 'dark' the display is asleep, judge nothing
    and re-arm / 'ok'. tcp_fresh: the board's TCP link is beating, so BLE
    gets BLE_RX_TIMEOUT_TCP instead of the full BLE_RX_TIMEOUT. Pure; the
    link loop feeds it every tick."""
    if display_asleep:
        return "dark"
    if limit is None:
        limit = BLE_RX_TIMEOUT_TCP if tcp_fresh else BLE_RX_TIMEOUT
    return "drop" if idle_s > limit else "ok"

def mac_display_asleep():
    """True when the main display is asleep (lid closed, display sleep).
    False when it is awake or when the window server cannot be asked -- an
    unknown must not silence the watchdog."""
    global _coregraphics
    try:
        if _coregraphics is None:
            cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            cg.CGMainDisplayID.restype = ctypes.c_uint32
            cg.CGDisplayIsAsleep.restype = ctypes.c_uint32
            cg.CGDisplayIsAsleep.argtypes = [ctypes.c_uint32]
            _coregraphics = cg
        return bool(_coregraphics.CGDisplayIsAsleep(_coregraphics.CGMainDisplayID()))
    except Exception:
        return False

class BleConnectTimeout(asyncio.TimeoutError):
    """ble_link_open gave up; the loop tells it apart from a scanner timeout."""

async def ble_link_open(client, timeout=None):
    """client.connect() with a bound bleak does not have (see
    BLE_CONNECT_TIMEOUT_S). On timeout the pending connect is cancelled, which
    also unblocks bleak's own unbounded wait for the cancel to be acknowledged."""
    try:
        await asyncio.wait_for(client.connect(),
                               BLE_CONNECT_TIMEOUT_S if timeout is None else timeout)
    except asyncio.TimeoutError:
        raise BleConnectTimeout() from None

async def ble_link_close(client, timeout=None):
    """client.disconnect() with a bound; a link that will not close is
    abandoned with one log line (the restart guards tear it down later)."""
    try:
        await asyncio.wait_for(client.disconnect(),
                               BLE_DISCONNECT_TIMEOUT_S if timeout is None else timeout)
    except asyncio.TimeoutError:
        log(f"ble: disconnect did not answer in {BLE_DISCONNECT_TIMEOUT_S}s, abandoning the link")
    except Exception as e:
        log("ble: disconnect error:", e)

def no_board_restart_verdict(idle_s, rss_kb, linked,
                             idle_limit_s=None, rss_limit_mb=None):
    """'' keep going / 'idle' / 'rss': why the host should hand itself to
    launchd. Always '' while a board is linked, whatever the numbers say."""
    if linked:
        return ""
    idle_limit = NO_BOARD_RESTART_S if idle_limit_s is None else idle_limit_s
    rss_limit = RSS_RESTART_MB if rss_limit_mb is None else rss_limit_mb
    if idle_limit > 0 and idle_s >= idle_limit:
        return "idle"
    if rss_limit > 0 and rss_kb // 1024 >= rss_limit:
        return "rss"
    return ""

def no_board_restart_check(idle_s, linked):
    """Trip either guard if it has tripped: one log line the user can read in
    the morning, then out. Returns the verdict, '' when nothing fired."""
    why = no_board_restart_verdict(idle_s, mem_last_rss_kb, linked)
    if why:
        minutes, rss_mb = int(idle_s) // 60, mem_last_rss_kb // 1024
        log(f"self-restart: no board for {minutes} min, rss {rss_mb} MB")
        host_self_restart(f"no board for {minutes} min" if why == "idle"
                          else f"rss {rss_mb} MB with no board")
    return why

def mac_bluetooth_on():
    """True/False from system_profiler (0.1 s); None if it could not be read."""
    try:
        out = subprocess.run(["system_profiler", "SPBluetoothDataType", "-json"],
                             capture_output=True, text=True, timeout=15).stdout
        st = json.loads(out)["SPBluetoothDataType"][0]["controller_properties"]["controller_state"]
        return st == "attrib_on"
    except Exception:
        return None

def host_exit(code=0):
    """os._exit, named so tests can stand in for it. Not sys.exit: the BLE and
    watcher threads are daemons mid-await and we want the process gone now."""
    os._exit(code)

def host_self_restart(why):
    """Exit so launchd (KeepAlive) relaunches the wrapper; a manual `python3
    agentpet_host.py` just exits -- start it again with `pet on`."""
    log("host: restarting --", why)
    host_exit(3)                # = os._exit; log() already flushes

# ------------------------------------------------------------------ host itself
# What the settings page's 状态 block and its host section need about US: the
# line `pet status` prints, the tail of our own log, and the restart button.
def host_brief():
    """/settings/data.host -- version, pid, minutes up, resident MB."""
    return {"version": HOST_VERSION, "product": PRODUCT, "pid": os.getpid(),
            "up_min": int((time.time() - HOST_T0) // 60),
            "rss_mb": mem_rss_kb() // 1024}

LOG_TAIL_MAX = 500          # a browser asking for more gets this many
LOG_TAIL_BYTES = 256 * 1024  # never read the whole log: it runs for days

def log_tail(n=60, path=None):
    """Last n lines of LOG_PATH, newest last; [] when there is no log yet.
    `path` is for tests only -- the endpoint never takes one, see LOG_PATH."""
    try:
        n = max(1, min(LOG_TAIL_MAX, int(n)))
    except (TypeError, ValueError):
        n = 60
    src = Path(path) if path else LOG_PATH
    try:
        with src.open("rb") as f:
            size = f.seek(0, os.SEEK_END)
            back = min(size, LOG_TAIL_BYTES)
            f.seek(size - back)
            data = f.read(back)
    except OSError:
        return []
    text = data.decode("utf-8", "replace")
    if back < size:
        text = text.split("\n", 1)[-1]      # the first line came in cut in half
    return text.splitlines()[-n:]

HOST_RESTART_DELAY_S = 0.4   # long enough for the HTTP answer to reach the page

def host_restart():
    """POST /host/restart: answer the page, then leave. launchd's KeepAlive
    (com.agentpet.host.plist) starts us again about a second later, which is
    the whole point -- a person who just changed voice_source or dropped a
    cpu_working key needs a fresh process and should not have to open a
    terminal for it."""
    log("host restart asked from the settings page")
    threading.Timer(HOST_RESTART_DELAY_S, host_exit).start()
    return {"ok": True, "version": HOST_VERSION, "in_s": HOST_RESTART_DELAY_S}

# ------------------------------------------------------------------ memory debug
# `mem:` in the log is the always-on curve; GET /debug/mem is the detail, and
# tracemalloc behind a flag file is what separates the two kinds of leak:
# traced flat while rss climbs = native (ObjC autorelease, see objc_pool()),
# both climbing = a Python container that never sheds. tracemalloc roughly
# doubles allocation cost, so production never runs it -- create the flag,
# restart the host, read /debug/mem, delete the flag, restart.
MEMDEBUG_FLAG = Path.home() / ".agentpet" / "memdebug"
_tm_base = None                 # first tracemalloc snapshot; every top list is vs this
mem_last_rss_kb = 0             # newest `mem:` sample, for no_board_restart_verdict (0 = none yet)
# The settings page's memory trend: one (t, rss_kb, threads)
# per mem_loop tick (30 s), 24 h deep, from this host's own start. The `mem:`
# log line stays the every-10-min record; this is the curve behind it.
MEM_HIST_MAX = 2880
MEM_WARMUP_S = 120              # the first two minutes climb ~12 MB (imports, BLE): drawn, not sloped
mem_hist = collections.deque(maxlen=MEM_HIST_MAX)

def mem_hist_add(t, rss_kb, threads):
    mem_hist.append((float(t), int(rss_kb), int(threads)))

MEM_SLOPE_MIN_S = 1800          # no MB/h off less than half an hour: on 7 min one 1.2 MB
                                # step read as +15 MB/h (2026-09-22 21:50, first live look)

def mem_slope_mb_h(pts, now=None, window_s=3600, min_span_s=MEM_SLOPE_MIN_S):
    """Least-squares MB/h over the points inside the last `window_s`; None
    until two points span `min_span_s` -- extrapolating a short span to an
    hour turns every GC wobble into a leak number."""
    now = time.time() if now is None else now
    xs = [(p[0], p[1] / 1024.0) for p in pts if p[0] >= now - window_s]
    if len(xs) < 2 or xs[-1][0] - xs[0][0] < min_span_s:
        return None
    n = len(xs)
    mx = sum(x for x, _ in xs) / n
    my = sum(y for _, y in xs) / n
    sxx = sum((x - mx) ** 2 for x, _ in xs)
    if sxx == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in xs)
    return round(sxy / sxx * 3600, 2)

def mem_hist_body(max_pts=300):
    """The /host/mem body: points as [up_s, rss_mb, threads], thinned to
    max_pts, plus the numbers the page prints next to the line."""
    pts = list(mem_hist)
    step = max(1, -(-len(pts) // max_pts))
    thin = pts[::step]
    if pts and thin[-1] is not pts[-1]:
        thin.append(pts[-1])
    now = time.time()
    warm = [p for p in pts if p[0] >= HOST_T0 + MEM_WARMUP_S]
    # before the slope is honest, the page shows the plain change since warm-up
    delta = round((warm[-1][1] - warm[0][1]) / 1024.0, 1) if len(warm) >= 2 else None
    return {"t0": HOST_T0, "up_s": int(now - HOST_T0), "tick_s": MEM_LOOP_TICK_S,
            "span_s": int(warm[-1][0] - warm[0][0]) if len(warm) >= 2 else 0,
            "delta_mb": delta,
            "pts": [[int(t - HOST_T0), round(kb / 1024.0, 1), th] for t, kb, th in thin],
            "now_mb": round(pts[-1][1] / 1024.0, 1) if pts else None,
            "start_mb": round(pts[0][1] / 1024.0, 1) if pts else None,
            "threads": pts[-1][2] if pts else None,
            "slope_mb_h": mem_slope_mb_h(warm, now)}

class _proc_taskinfo(ctypes.Structure):
    # struct proc_taskinfo from <libproc.h>: 96 bytes on arm64 and x86_64.
    _fields_ = [("pti_virtual_size", ctypes.c_uint64), ("pti_resident_size", ctypes.c_uint64),
                ("pti_total_user", ctypes.c_uint64), ("pti_total_system", ctypes.c_uint64),
                ("pti_threads_user", ctypes.c_uint64), ("pti_threads_system", ctypes.c_uint64),
                ("pti_policy", ctypes.c_int32), ("pti_faults", ctypes.c_int32),
                ("pti_pageins", ctypes.c_int32), ("pti_cow_faults", ctypes.c_int32),
                ("pti_messages_sent", ctypes.c_int32), ("pti_messages_received", ctypes.c_int32),
                ("pti_syscalls_mach", ctypes.c_int32), ("pti_syscalls_unix", ctypes.c_int32),
                ("pti_csw", ctypes.c_int32), ("pti_threadnum", ctypes.c_int32),
                ("pti_numrunning", ctypes.c_int32), ("pti_priority", ctypes.c_int32)]

_PROC_PIDTASKINFO = 4

def mem_rss_kb(pid=None):
    """This process's resident size in KB, 0 if the kernel would not say.

    Read through proc_pidinfo(), not `ps`: `ps` means a fork, and this host is
    a multi-threaded PyObjC process whose BLE thread spends its life inside
    CoreBluetooth callbacks. A fork that lands while that thread holds an
    ObjC-runtime lock leaves the child deadlocked before exec and the parent
    thread parked forever on the exec-status pipe -- which is what the two
    2026-09-17 host runs on 17a looked like: mem_loop never printed a single
    sample, /debug/threads was not there yet to prove it, and the no-board
    self-restart reported "rss 0 MB". subprocess.run() on this Python (3.9,
    close_fds=True by default) always takes the fork path, so the fix is to
    not spawn at all. Every other subprocess call in this file is short-lived
    and runs from the inject worker or a hook thread, not on a timer; if one
    of those ever sticks, /debug/threads will show it in os.read()."""
    try:
        ti = _proc_taskinfo()
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        n = libc.proc_pidinfo(int(pid or os.getpid()), _PROC_PIDTASKINFO,
                              ctypes.c_uint64(0), ctypes.byref(ti), ctypes.sizeof(ti))
        return int(ti.pti_resident_size // 1024) if n == ctypes.sizeof(ti) else 0
    except Exception:
        return 0

def mem_debug_start(flag=None):
    """Turn tracemalloc on iff the flag file is there. Called once from main()
    before any thread starts, so the base snapshot is the quiet host."""
    global _tm_base
    flag = MEMDEBUG_FLAG if flag is None else flag
    try:
        if not flag.exists():
            return False
        tracemalloc.start(8)
        _tm_base = tracemalloc.take_snapshot()
        log("mem: tracemalloc on (flag", str(flag) + ")")
        return True
    except Exception as e:
        log("mem: tracemalloc start failed:", e)
        return False

def mem_traced_mb():
    """Python heap tracked by tracemalloc, in MB; None when it is off."""
    if not tracemalloc.is_tracing():
        return None
    return round(tracemalloc.get_traced_memory()[0] / (1 << 20), 1)

def mem_types_top(n=20, objects=None):
    """gc.get_objects() counted by type name, biggest first -- a Python
    container that never sheds shows up here as one type that keeps climbing
    across two calls."""
    counts = {}
    for o in (gc.get_objects() if objects is None else objects):
        name = type(o).__name__
        counts[name] = counts.get(name, 0) + 1
    return [{"type": t, "n": c}
            for t, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]

def mem_traced_top(n=20):
    """Growth per source line since the base snapshot; [] when tracing is off."""
    if not tracemalloc.is_tracing() or _tm_base is None:
        return []
    try:
        stats = tracemalloc.take_snapshot().compare_to(_tm_base, "lineno")
    except Exception as e:
        log("mem: tracemalloc compare failed:", e)
        return []
    return [{"where": str(s.traceback[0]) if s.traceback else "?",
             "kb": round(s.size_diff / 1024, 1), "count": s.count_diff}
            for s in stats[:n]]

def mem_snapshot(top=20):
    """The /debug/mem body. Takes no host lock and touches no board -- it must
    never stall the BLE loop. gc.get_objects() is the expensive part (a few
    hundred ms on a 100 MB host, and it briefly allocates a list as long as
    the heap), which is why this is a hand-pulled endpoint, not a loop."""
    counts = gc.get_count()
    return {"rss_kb": mem_rss_kb(),
            "uptime_s": int(time.time() - HOST_T0),
            "threads": sorted(t.name for t in threading.enumerate()),
            "gc_count": list(counts),
            "gc_objects": len(gc.get_objects()),
            "types_top": mem_types_top(top),
            "tracemalloc": tracemalloc.is_tracing(),
            "traced_mb": mem_traced_mb(),
            "traced_top": mem_traced_top(top)}

def mem_loop(period_s=600, stop=None, tick_s=None):
    """One `mem:` line per period so a slow leak shows up in the log.
    The sample is also what the no-board restart guard reads, so the number in
    `self-restart:` is always one the log already printed. `stop` is an Event
    the tests hand in to end the loop; the host never sets one."""
    global mem_last_rss_kb
    pid = os.getpid()
    stop = stop or threading.Event()
    tick = min(period_s, MEM_LOOP_TICK_S if tick_s is None else tick_s)
    due = time.time() + period_s
    def sample_hist():              # the page's curve: every tick, never logged
        try:
            mem_hist_add(time.time(), mem_rss_kb(pid), threading.active_count())
        except Exception:
            pass
    sample_hist()                   # a point at start, so a fresh host shows where it began
    while not stop.wait(tick):
        sample_hist()
        if time.time() < due:       # wall clock, so sleep counts (MEM_LOOP_TICK_S note)
            continue
        due = time.time() + period_s
        try:
            rss = mem_rss_kb(pid)
            mem_last_rss_kb = rss
            traced = mem_traced_mb()
            log(f"mem: rss={rss // 1024} MB threads={threading.active_count()} "
                f"up={int(time.time() - HOST_T0) // 60} min"
                + (f" traced={traced} MB" if traced is not None else "")
                + board_mem_note())
            # The no-board guard runs here, not in the BLE loop it watches:
            # on 2026-09-17 that loop sat five hours inside a wedged scanner
            # and the guard, living inside it, never got its turn.
            # S3 has two links: a board on Wi-Fi TCP alone is linked all the
            # same, and a TCP drop resets the idle clock like a BLE drop does.
            # Links we closed for a Mac sleep count as linked: a DarkWake
            # hours into the night must not restart a host that is merely
            # waiting for the lid (links_paused re-stamps the clock on wake).
            no_board_restart_check(time.time() - max(_ble_last_link_at, _tcp_last_link_at),
                                   _ble_client is not None or bool(boards) or _power["sleeping"])
        except Exception as e:
            log("mem: sample failed:", e)

def ble_send(data: bytes):
    import asyncio
    client, loop = _ble_client, _ble_loop
    if not client or not loop:
        return
    async def _w():
        try:
            for i in range(0, len(data), 100):
                await client.write_gatt_char(NUS_RX, data[i:i + 100],
                                             response=False)
        except Exception as e:
            log("ble write error:", e)
    try:
        asyncio.run_coroutine_threadsafe(_w(), loop)
    except Exception as e:
        log("ble send error:", e)

async def ble_scan_window(sc, win, timeout, op_timeout=None):
    """One scan window on a scanner we keep, in place of
    BleakScanner.find_device_by_filter.

    find_device_by_filter builds a CentralManagerDelegate, a CBCentralManager
    and a dispatch queue for every single scan and tears them all down again,
    and that work autoreleases inside bleak's own coroutines, where a pool of
    ours cannot go without being held across an await (pool_loop_callbacks
    note). Starting and stopping one scanner leaves only the per-advertisement
    work, which is pooled (measured 2026-09-17 at this cadence).

    Every call into the scanner is bounded by op_timeout (BLE_SCAN_OP_TIMEOUT_S):
    a kept scanner can outlive the Mac's sleep, and a central manager that lost
    power mid-cycle ignores the next scan request without ever raising, so an
    unbounded start() is a five-hour hang (2026-09-17). On timeout this raises
    asyncio.TimeoutError and the caller discards the scanner.

    `win` is the caller's advertisement mailbox: win["hit"] is the Event the
    detection callback sets, win["dev"] the device it found (ble_offer keeps
    the best of several; its digest stays in win["digest"] for the caller)."""
    import asyncio
    op = BLE_SCAN_OP_TIMEOUT_S if op_timeout is None else op_timeout
    win["hit"] = asyncio.Event()
    for k in ("dev", "digest", "rank"):
        win.pop(k, None)
    try:
        await asyncio.wait_for(sc.start(), op)
        try:
            await asyncio.wait_for(win["hit"].wait(), timeout)
        except asyncio.TimeoutError:
            pass
    finally:
        win["hit"] = None     # no stray set() between windows
        g = win.pop("grace", None)
        if g is not None:
            g.cancel()
        await asyncio.wait_for(sc.stop(), op)
    win.pop("rank", None)
    return win.pop("dev", None)

# Two boards in range: the first advertisement heard must not
# decide, or someone else's board next to ours puts this host on standby. A
# board nobody is linked to advertises every 30-50 ms, one linked elsewhere
# every 1 s (firmware bleAdvertise), so this long sees every board around.
BLE_PICK_GRACE_S = 1.5

def ble_pick_rank(digest, mine, known, claim, declined=False):
    """Scan preference among boards advertising at once, lower wins:
    0 ours, 1 nobody's / old firmware / the one a claim may take,
    2 someone else's we have no name for (worth one probe),
    3 someone else's we know, or a free one that just said no (standby:
    only worth its owner's name when nothing better is around)."""
    if digest is not None and digest == mine:
        return 0
    v = ble_owner_verdict(digest, mine, known, claim, declined)
    return {"connect": 1, "legacy": 1, "claim": 1, "probe": 2}.get(v, 3)

def ble_offer(win, dev, digest, rank, loop=None, grace=None):
    """One of our boards' advertisements into the scan window `win`. Keeps
    the best-ranked board; ours ends the window at once, anything else after
    a short grace in which a better one can still take its place. Runs on the
    scan loop's thread (bleak calls the detection callback there)."""
    hit = win.get("hit")
    if hit is None or hit.is_set() or rank >= win.get("rank", 99):
        return
    win["dev"], win["digest"], win["rank"] = dev, digest, rank
    if rank == 0:
        hit.set()
    elif "grace" not in win:
        import asyncio
        win["grace"] = (loop or asyncio.get_running_loop()).call_later(
            BLE_PICK_GRACE_S if grace is None else grace, hit.set)

# ------------------------------------------------------------------ Mac sleep
# No board link is carried into a system sleep (2026-09-27). The
# second Mac, lid closed with both links up: its Bluetooth chip kept the LE
# link through the sleep, the board's 10 s heartbeat woke the whole Mac
# (pmset: 20-54 DarkWakes per 10 min, 227 of 238 "wifibt"), the woken host
# answered, the board saw a healthy link, and round it went until morning.
# With the links dropped before the sleep the board only retries -- TCP
# connects every 3 s, advertising -- and in two hours of that the same Mac
# woke 1-2 times per 10 min, as it does with no board at all.
#
# IORegisterForSystemPower tells us: WillSleep (must be acknowledged; up to
# 30 s later the system sleeps anyway), HasPoweredOn. HasPoweredOn also comes
# on every DarkWake, and a link opened in a dark wake is carried into the next
# sleep, so the links stay down until the display is on as well.
POWER_SLEEP_WAIT_S = 10       # WillSleep waits this long for the BLE link to close
POWER_MISSED_WAKE_S = 60      # awake seconds with the display on and no HasPoweredOn = we missed it
_IOKIT_CAN_SLEEP = 0xE0000270        # kIOMessageCanSystemSleep (IOMessage.h)
_IOKIT_WILL_SLEEP = 0xE0000280       # kIOMessageSystemWillSleep
_IOKIT_HAS_POWERED_ON = 0xE0000300   # kIOMessageSystemHasPoweredOn
_power_lock = threading.Lock()
# sleeping: a WillSleep came and the links have not resumed since; woke: a
# HasPoweredOn came after it; at: ble_awake_clock() at the WillSleep;
# refused: board TCP connects turned away since then.
_power = {"sleeping": False, "woke": False, "at": 0.0, "refused": 0, "cb": None}
_power_kick = []              # ble_thread's "end what you are waiting on" (runs on its loop)
_ble_parked = threading.Event()   # set while the BLE loop holds nothing because of a sleep

def links_paused_verdict(woke, display_asleep, awake_s):
    """After a WillSleep, True while the host must hold no board link: until
    the Mac is back (HasPoweredOn) with the display on. A dark display alone
    keeps the pause (DarkWake); a lit one with no HasPoweredOn for
    POWER_MISSED_WAKE_S awake seconds ends it, so a lost notice cannot keep
    the board away for good. Pure; links_paused() feeds it."""
    if display_asleep:
        return True
    return not woke and awake_s < POWER_MISSED_WAKE_S

def links_paused():
    """Whether board links must stay down right now. The first call that
    finds the Mac back ends the pause, re-stamps the no-board guard's clock
    (the hours asleep are not hours of scanning in vain) and says so."""
    global _ble_last_link_at
    with _power_lock:
        if not _power["sleeping"]:
            return False
        if links_paused_verdict(_power["woke"], mac_display_asleep(),
                                ble_awake_clock() - _power["at"]):
            return True
        _power["sleeping"] = False
        missed, refused = not _power["woke"], _power["refused"]
    _ble_parked.clear()
    _ble_last_link_at = time.time()
    log("power: Mac awake, board links resume"
        + (" (no wake notice came)" if missed else "")
        + (f"; turned away {refused} board connects while asleep" if refused else ""))
    return False

def power_will_sleep():
    """WillSleep, on the power thread: stop accepting, close every TCP board
    socket, have the BLE loop close its link, and wait (bounded) until it
    has, so the Mac does not go to sleep holding it."""
    t0 = time.time()
    with _power_lock:
        _power.update(sleeping=True, woke=False, at=ble_awake_clock(), refused=0)
    _ble_parked.clear()
    with lock:
        socks = list(boards)
    for c in socks:              # the handler's recv() sees EOF and cleans up as usual
        try:
            c.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    had_ble = _ble_client is not None
    parked = True
    if _ble_loop is not None and _power_kick:
        try:
            _ble_loop.call_soon_threadsafe(_power_kick[0])
        except RuntimeError:     # loop already closed
            pass
        parked = _ble_parked.wait(POWER_SLEEP_WAIT_S)
    log(f"power: Mac going to sleep, closed {len(socks)} TCP"
        f"{' + BLE' if had_ble else ''} in {time.time() - t0:.1f}s"
        + ("" if parked else " (BLE loop still busy, sleeping anyway)"))

def power_woke():
    """HasPoweredOn, on the power thread. The links wait for the display
    (links_paused), so a DarkWake changes nothing but this flag."""
    with _power_lock:
        _power["woke"] = True
    log("power: Mac woke" + (" (display dark)" if mac_display_asleep() else ""))

def power_thread():
    """IOKit sleep/wake notices on a CFRunLoop of this thread's own. If the
    registration fails nothing is ever paused: the links behave as before."""
    try:
        iokit = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        # void (*IOServiceInterestCallback)(void *refcon, io_service_t, natural_t messageType, void *arg)
        cb_t = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p)
        iokit.IORegisterForSystemPower.restype = ctypes.c_uint32          # io_connect_t, 0 = failed
        iokit.IORegisterForSystemPower.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                                   cb_t, ctypes.POINTER(ctypes.c_uint32)]
        iokit.IONotificationPortGetRunLoopSource.restype = ctypes.c_void_p
        iokit.IONotificationPortGetRunLoopSource.argtypes = [ctypes.c_void_p]
        iokit.IOAllowPowerChange.argtypes = [ctypes.c_uint32, ctypes.c_void_p]   # (io_connect_t, intptr_t)
        cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        cf.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        mode = ctypes.c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")
        root = [0]

        def on_power(_refcon, _service, msg, arg):
            try:
                if msg == _IOKIT_WILL_SLEEP:
                    power_will_sleep()
                elif msg == _IOKIT_HAS_POWERED_ON:
                    power_woke()
            except Exception as e:
                log("power: handler error:", e)
            finally:
                # Both must be acknowledged, or every sleep waits 30 s.
                if msg in (_IOKIT_WILL_SLEEP, _IOKIT_CAN_SLEEP):
                    iokit.IOAllowPowerChange(root[0], arg)

        _power["cb"] = cb = cb_t(on_power)    # held for good: a collected callback crashes the host
        port, notifier = ctypes.c_void_p(), ctypes.c_uint32()
        root[0] = iokit.IORegisterForSystemPower(None, ctypes.byref(port), cb, ctypes.byref(notifier))
        if not root[0]:
            log("power: IORegisterForSystemPower refused, links will not pause for sleep")
            return
        cf.CFRunLoopAddSource(cf.CFRunLoopGetCurrent(),
                              iokit.IONotificationPortGetRunLoopSource(port), mode)
        log("power: listening for sleep/wake")
        cf.CFRunLoopRun()
    except Exception as e:
        log("power: sleep/wake notices unavailable:", e)

def ble_thread():
    global _ble_loop, _ble_client
    try:
        import asyncio
        from bleak import BleakClient, BleakScanner
    except ImportError:
        log("ble: bleak not installed, BLE bridge disabled")
        return
    _ble_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_ble_loop)
    pool_loop_callbacks(_ble_loop)    # every CoreBluetooth callback gets a pool

    async def main():
        global _ble_client, _ble_last_link_at
        last_link = time.time()     # last moment a board was linked (or process start)
        _ble_last_link_at = last_link
        consec_off = 0              # consecutive "Bluetooth device is turned off"
        slow = False
        scanner = None              # kept between scans, see ble_scan_window()
        win = {"hit": None}         # the scan window in flight: filter input + result
        dropped_by_watchdog = False # the last link ended in our rx watchdog
        empty_scans = 0             # normal, empty windows since that drop
        _claim["evt"] = asyncio.Event()   # board_claim() cuts a scan pause short
        mine = owner_digest(my_identity()["id"])
        loop = asyncio.get_running_loop()
        link = {"gone": None}       # the held link's gone Event, for the sleep kick
        slept = False               # the loop sat out a Mac sleep (links_paused)

        def kick():
            """WillSleep (power_will_sleep, via call_soon_threadsafe): end the
            scan window, the pause or the held link now, so the loop reaches
            its sleep gate and _ble_parked before the Mac goes down."""
            for e in (win["hit"], link["gone"], _claim["evt"]):
                if e is not None:
                    e.set()
        _power_kick[:] = [kick]

        async def pause(sec):
            if _claim["want"]:
                sec = 1       # a claim is waiting: straight back to scanning
            _claim["evt"].clear()
            try:
                await asyncio.wait_for(_claim["evt"].wait(), sec)
            except asyncio.TimeoutError:
                pass

        def on_ad(d, ad):
            """bleak hands us every advertisement here: synchronously, on this
            thread, from inside the pooled did_discover_peripheral callback."""
            if win["hit"] is None or win["hit"].is_set():
                return
            if ble_is_our_board(ad.local_name, d.name):
                digest = adv_owner_digest(ad.manufacturer_data)
                rank = ble_pick_rank(digest, mine, _owner_names, _claim["want"],
                                     time.time() - board_owner["declined_at"] < OWNER_REASK_S)
                ble_offer(win, d, digest, rank, loop)

        while True:
            try:
                if links_paused():
                    # Mac asleep or in a dark wake: no scan (a scan left
                    # running is a way to be woken), no link, fresh central
                    # manager afterwards (BLE_SCAN_OP_TIMEOUT_S note).
                    slept, scanner = True, None
                    _ble_parked.set()
                    await asyncio.sleep(1)
                    continue
                if slept:
                    # The board was right here when we let go: fast plan,
                    # and our own drop is no evidence of a stale link.
                    slept = False
                    last_link = time.time()
                    dropped_by_watchdog, empty_scans = False, 0
                scan_s, pause_s = ble_scan_plan(time.time() - last_link)
                if _claim["want"]:
                    scan_s, pause_s = BLE_SCAN_FAST    # the user is waiting on a claim
                if (scan_s, pause_s) == BLE_SCAN_SLOW and not slow:
                    log(f"ble: no board for {BLE_IDLE_BACKOFF_S}s, scanning {scan_s:.0f}s every {scan_s + pause_s:.0f}s")
                slow = (scan_s, pause_s) == BLE_SCAN_SLOW
                if scanner is None:
                    scanner = BleakScanner(detection_callback=on_ad)
                dev = await ble_scan_window(scanner, win, scan_s)
                consec_off = 0
                ble_note("scanning")      # the stack answered; no board in range yet
                if _power["sleeping"]:
                    continue              # cut short by the sleep kick: to the gate, found or not
                if dev is None:
                    # The no-board guard is NOT checked here: it lives in
                    # mem_loop, on its own thread, so that a scan loop wedged
                    # inside the scanner (the very thing the guard exists for)
                    # cannot take the guard down with it (2026-09-17 evening).
                    empty_scans += 1
                    if _claim["want"]:
                        log("ble: claim pending, no board in this scan window")
                    if stale_link_verdict(dropped_by_watchdog, empty_scans):
                        log(f"self-restart: board vanished mid-link and {empty_scans} empty scans since"
                            f" (it is probably still holding the old link; tcp boards={len(boards)})")
                        host_self_restart("stale link after a watchdog drop")
                    await pause(pause_s)
                    continue
                dropped_by_watchdog, empty_scans = False, 0
                digest = win.pop("digest", None)
                verdict = ble_owner_verdict(digest, mine, _owner_names, _claim["want"],
                                            time.time() - board_owner["declined_at"] < OWNER_REASK_S)
                if _claim["want"] or verdict != "standby":
                    log(f"ble: board advertises owner {digest!r}, verdict {verdict}")
                if verdict == "standby":
                    # Someone else's board, and we know whose: leave it be. It
                    # is right there, so the no-board guard has nothing to do.
                    owner_note("standby", _owner_names.get(digest, board_owner["name"]))
                    _ble_last_link_at = time.time()
                    await pause(pause_s)
                    continue
                if verdict == "legacy":
                    owner_note("legacy")
                # Nothing CoreBluetooth owns outlives the scan: address and
                # name are plain str here, and dev.details (the CBPeripheral
                # and this scanner's delegate) is handed straight to bleak.
                log(f"ble: found board {str(dev.address)}, connecting")
                gone = asyncio.Event()
                link["gone"] = gone
                last_rx = ble_awake_clock()
                def on_dc(_c):
                    _ble_loop.call_soon_threadsafe(gone.set)
                client = BleakClient(dev, disconnected_callback=on_dc)
                await ble_link_open(client)
                try:
                    buf = b""
                    def on_notify(_h, data):
                        nonlocal buf, last_rx
                        last_rx = ble_awake_clock()
                        buf += bytes(data)
                        while b"\n" in buf:
                            line, buf = buf.split(b"\n", 1)
                            if not line.strip():
                                continue
                            if b'"t":"owner"' in line or b'"t":"released"' in line:
                                try:
                                    link_owner_cb[0](json.loads(line))
                                except ValueError:
                                    pass
                                continue
                            handle_board_msg(line, ble_send)
                    answered = asyncio.Event()
                    def on_owner(m):
                        owner_msg(m)
                        answered.set()
                    link_owner_cb[0] = on_owner
                    await client.start_notify(NUS_TX, on_notify)
                    if verdict != "legacy":
                        # Say who we are first; the board answers "owner" and,
                        # unless it is ours, drops this link a moment later.
                        hi = hostinfo_msg(claim=verdict == "claim")
                        for i in range(0, len(hi), 100):
                            await client.write_gatt_char(NUS_RX, hi[i:i + 100], response=False)
                        try:
                            await asyncio.wait_for(answered.wait(), OWNER_PROBE_S)
                        except asyncio.TimeoutError:
                            log("ble: board did not answer hostinfo; treating as legacy")
                            owner_note("legacy")
                        # The claim card is up: hold the link while a person
                        # walks over; the board answers again once tapped.
                        while board_owner["state"] == "asking":
                            answered.clear()
                            log("ble: board is asking on screen, waiting for a tap")
                            left = max(1.0, board_owner["ask_until"] - time.time()) + 5
                            try:
                                await asyncio.wait_for(answered.wait(), left)
                            except asyncio.TimeoutError:
                                owner_note("standby", board_owner["name"])
                                break
                        if board_owner["state"] not in ("mine", "legacy"):
                            log("ble: board is not ours (%s %r) - standing by"
                                % (board_owner["state"], board_owner["name"]))
                            scanner = None    # a link ran on its manager: fresh one next scan
                            continue          # finally: closes the link
                    if _power["sleeping"]:
                        continue              # the Mac went to sleep mid-handshake: finally closes it
                    _ble_client = client
                    ble_note("")
                    log("ble: connected, link active")
                    push_state()
                    # CoreBluetooth can silently drop the disconnect event when
                    # the board loses power, leaving gone unset forever -- so
                    # also watch the rx stream (board pings every 10s)
                    while not gone.is_set():
                        try:
                            await asyncio.wait_for(gone.wait(), timeout=5)
                        except asyncio.TimeoutError:
                            pass
                        verdict = rx_watchdog_verdict(ble_awake_clock() - last_rx,
                                                      mac_display_asleep(),
                                                      tcp_fresh=time.time() - _tcp_rx_at < TCP_FRESH_S)
                        if verdict == "dark":
                            last_rx = ble_awake_clock()   # lights out: nobody is judged in the dark
                        elif verdict == "drop":
                            log("ble: rx watchdog timeout, dropping link")
                            dropped_by_watchdog, empty_scans = True, 0
                            break
                    if _power["sleeping"]:
                        log("ble: Mac going to sleep, closing the link")
                finally:
                    await ble_link_close(client)
                    link["gone"] = None
            except Exception as e:
                scanner = None      # a scanner that raised is not reused: the next
                                    # pass builds a fresh central manager, exactly
                                    # what find_device_by_filter used to do anyway
                if "turned off" in str(e):
                    consec_off += 1
                    ble_note("bt_off")
                    if consec_off <= 2 or consec_off % 12 == 0:   # 1 line/min, not 1 per 5 s
                        log("ble error:", e, f"(x{consec_off})")
                    verdict = ble_off_verdict(consec_off, mac_bluetooth_on(),
                                              time.time() - HOST_T0)
                    if verdict == "restart":
                        ble_note("stuck")   # one poll's worth of explanation before we go
                        host_self_restart("CoreBluetooth stuck off while macOS says on")
                    elif verdict == "wait":
                        await asyncio.sleep(26)     # macOS Bluetooth really is off: poll every 30 s
                elif isinstance(e, BleConnectTimeout):
                    log(f"ble: connect did not answer in {BLE_CONNECT_TIMEOUT_S}s, rebuilding the scanner")
                elif isinstance(e, asyncio.TimeoutError):
                    log(f"ble: scanner did not answer in {BLE_SCAN_OP_TIMEOUT_S}s, rebuilding it")
                else:
                    log("ble error:", e)
                await asyncio.sleep(4)
            finally:
                if _ble_client is not None:
                    last_link = time.time()
                    _ble_last_link_at = last_link
                    # A link ran on this scanner's central manager (bleak
                    # connects through the delegate that found the device), so
                    # retire it with the link. What the pooling is for is the
                    # idle half hour of scanning, not one manager per session.
                    scanner = None
                    _ble_client = None
                    board_link_down()      # vitals go stale unless TCP still carries them
                _ble_client = None
    _ble_loop.run_until_complete(main())

# ------------------------------------------------------------------ board TCP
_state_push_lock = threading.Lock()      # keep two callers' state lines in order

def push_state():
    with _state_push_lock:
        with lock:
            msg = (json.dumps({"t": "state", "agents": state}) + "\n").encode()
        push_raw(msg)                    # via tcp_send: never splice into a file push

send_lock = threading.Lock()     # one JSON line at a time on the board sockets

def tcp_send(msg):
    """sendall to every TCP board. The global state lock is NOT held while
    sending: a file push blocks on TCP backpressure and must not stall the
    rest of the host; send_lock alone keeps lines from interleaving.
    THE ONLY place that writes to a board socket, apart from the per-connection
    reply() — 2026-09-08 push_state's own sendall spliced a state line into the
    middle of a 5.6 KB fdat batch and failed two OTAs with why=b64."""
    with lock:
        socks = list(boards)
    dead = []
    for c in socks:
        with send_lock:
            try:
                c.sendall(msg)
            except OSError:
                dead.append(c)
    if dead:
        with lock:
            for c in dead:
                if c in boards:
                    boards.remove(c)

def push_raw(msg):
    """Pre-encoded line(s), newline-terminated, to every link."""
    tcp_send(msg)
    ble_send(msg)

def push_msg(obj):
    push_raw((json.dumps(obj) + "\n").encode())

def push_tcp(obj):
    """push_msg minus BLE — for audio and file chunks, which would swamp the NUS pipe."""
    tcp_send((json.dumps(obj) + "\n").encode())

def push_board(obj):
    """Exactly one delivery: TCP when a board socket is up, else BLE. For
    commands that must not run twice (mic session start resets the encoder)."""
    msg = (json.dumps(obj) + "\n").encode()
    with lock:
        has_tcp = bool(boards)
    if has_tcp:
        tcp_send(msg)
    else:
        ble_send(msg)

# ------------------------------------------------------------------ SD file push (基B)
# Board replies ({"t":"fack"/"fls"/"fcat","id":N,...}) are parked here by
# handle_board_msg; the HTTP thread that asked waits on the condition.
_replies = {}
_replies_cv = threading.Condition()

def _park_reply(m):
    key = (m.get("t"), m.get("id"))
    with _replies_cv:
        _replies[key] = m
        _replies_cv.notify_all()

def board_wifi(op="list", ssid="", password="", join=False):
    """Settings page -> board Wi-Fi list. The password goes
    straight to the board and is not kept or logged here. A scan waits for
    the board to finish one (a couple of seconds)."""
    if op not in ("list", "add", "rm", "scan"):
        return {"ok": False, "why": "op"}
    msg = {"t": "wifi", "op": op, "id": _new_rid()}
    if op in ("add", "rm"):
        ssid = (ssid or "").strip()
        if not ssid:
            return {"ok": False, "why": "ssid"}
        msg["s"] = ssid
    if op == "add":
        msg["p"] = password or ""
        if join:
            msg["join"] = 1
    with lock:
        online = bool(boards) or _ble_client is not None
    if not online:
        return {"ok": False, "why": "offline"}
    push_board(msg)
    r = wait_reply("wifi", msg["id"], 20 if op == "scan" else 4)
    if op in ("add", "rm"):
        log("board wifi:", op, repr(ssid), "->", "ok" if r and r.get("ok") else (r or {}).get("why", "no answer"))
    return r or {"ok": False, "why": "timeout"}

def wait_reply(t, rid, timeout):
    key = (t, rid)
    deadline = time.time() + timeout
    with _replies_cv:
        while key not in _replies:
            left = deadline - time.time()
            if left <= 0:
                return None
            _replies_cv.wait(left)
        return _replies.pop(key)

def _new_rid():
    return int(time.time() * 1000) & 0x7FFFFFFF

# ------------------------------------------------------------------ owner (认领制)
# The board follows one Mac, its owner (board NVS). It advertises a 4-byte
# digest of the owner's host_id; a host that sees someone else's digest stays
# on standby and does not connect. Changing hands is always a claim made on
# the new Mac (settings page 「让板子连这台」 / `pet claim`); an absent owner
# is never timed out (用户 09-26 定).
HOST_ID_PATH = Path.home() / ".agentpet" / "host_id"
OWNER_MFG_ID = 0xFFFF            # manufacturer data: b"A\x01" + digest LE
OWNER_PROBE_S = 4                # how long a link waits for the board's "owner" answer
CLAIM_WAIT_S = 60                # /board/claim: scan + connect + the 30 s tap on the board
OWNER_REASK_S = 600              # a declined first-owner prompt is not re-asked for this long

def owner_digest(host_id):
    """FNV-1a 32 of the id's UTF-8 bytes; 0 is reserved for "no owner".
    Same function as ownerDigest() in firmware main.cpp."""
    h = 2166136261
    for b in host_id.encode():
        h = ((h ^ b) * 16777619) & 0xFFFFFFFF
    return h or 1

def adv_owner_digest(mfg):
    """Owner digest from bleak's manufacturer_data; None = firmware before
    the claim system (no such field), 0 = no owner."""
    raw = (mfg or {}).get(OWNER_MFG_ID)
    if not raw or len(raw) < 6 or raw[0:2] != b"A\x01":
        return None
    return struct.unpack("<I", bytes(raw[2:6]))[0]

def ble_owner_verdict(digest, mine, known, claim, declined=False):
    """What to do with a board advertising `digest`:
    'legacy'  old firmware, connect as before
    'connect' ours, or nobody's (the board asks on screen, then adopts us)
    'claim'   someone else's, and the user asked to take it
    'probe'   someone else's we have no name for: connect once to ask
    'standby' someone else's, or nobody's but its screen just said no to us
              (`declined`): leave it alone"""
    if digest is None:
        return "legacy"
    if digest == 0 and declined and not claim:
        return "standby"
    if digest in (0, mine):
        return "claim" if claim and digest == 0 else "connect"
    if claim:
        return "claim"
    return "standby" if digest in known else "probe"

def host_identity():
    """What the board needs to find this Mac: id, a name for people, the
    Bonjour name and a LAN IP for the TCP link."""
    try:
        hid = HOST_ID_PATH.read_text().strip()
    except OSError:
        hid = ""
    if not hid:
        import uuid
        hid = str(uuid.uuid4())
        try:
            HOST_ID_PATH.parent.mkdir(parents=True, exist_ok=True)
            HOST_ID_PATH.write_text(hid + "\n")
        except OSError:
            pass
    def sc(key):
        try:
            return subprocess.run(["/usr/sbin/scutil", "--get", key], capture_output=True,
                                  text=True, timeout=3).stdout.strip()
        except Exception:
            return ""
    return {"id": hid, "name": sc("ComputerName") or socket.gethostname(),
            "mdns": sc("LocalHostName"), "ip": lan_ip(), "port": TCP_PORT}

def is_lan_ip(ip):
    """RFC 1918 only. A proxy's TUN (Clash fake-ip 198.18/15, utun) owns the
    default route on some Macs, so "the address toward the internet" is not
    the one a board on the same Wi-Fi can reach (2026-09-26: 198.18.0.1)."""
    try:
        a, b = [int(x) for x in ip.split(".")[:2]]
    except ValueError:
        return False
    return a == 10 or (a == 172 and 16 <= b <= 31) or (a == 192 and b == 168)

def lan_ip():
    """This Mac's address on its real network interface (Wi-Fi/Ethernet)."""
    for ifc in ("en0", "en1", "en2", "en3"):
        try:
            ip = subprocess.run(["/usr/sbin/ipconfig", "getifaddr", ifc], capture_output=True,
                                text=True, timeout=2).stdout.strip()
        except Exception:
            continue
        if is_lan_ip(ip):
            return ip
    return ""

_host_ident = None
def my_identity():
    global _host_ident
    if _host_ident is None:
        _host_ident = host_identity()
    return _host_ident

def hostinfo_msg(claim=False):
    me = dict(my_identity())
    ip = host_identity()["ip"]           # DHCP may have moved us since start
    if ip:
        me["ip"] = ip
    me["t"] = "claim" if claim else "hostinfo"
    return (json.dumps(me, ensure_ascii=False) + "\n").encode()

# What this host knows about who owns the board. state:
#   ""        not known yet (no board seen since start)
#   "mine"    the board follows this Mac
#   "standby" it follows `name`; we leave it alone until a claim
#   "legacy"  firmware from before the claim system
#   "asking"  the board shows the claim card; a tap there decides (ask_until)
#   "free"    the board has no owner and its screen said no to us
board_owner = {"state": "", "name": "", "at": 0.0, "ask_until": 0.0, "declined_at": 0.0}
_owner_names = {}                # digest -> owner name, learned from "owner"/"released"
_claim = {"want": False, "evt": None, "result": ""}

def owner_note(state, name=""):
    prev = board_owner["state"], board_owner["name"]
    board_owner.update(state=state, name=name or "", at=time.time())
    if (state, name or "") != prev:
        log(f"owner: {state}" + (f" ({name})" if name else ""))

_test_claim = {"until": 0.0}     # /test/claim in flight: its answers are not about us

def owner_msg(m):
    """{"t":"owner",...} / {"t":"released",...} from the board, either link.
    owner: mine / pending (the claim card is up, sec left) / refused with
    why = owned | declined | timeout | asleep | busy."""
    if m.get("t") == "released":
        by = m.get("by", "")
        if m.get("id"):
            _owner_names[owner_digest(m["id"])] = by
        owner_note("standby", by)
        return
    name = m.get("name", "")
    if time.time() < _test_claim["until"] and board_owner["state"] == "mine" and m.get("t") == "owner":
        log("owner: /test/claim answer:", m)
        return
    if m.get("id"):
        _owner_names[owner_digest(m["id"])] = name
    if m.get("mine"):
        board_owner["ask_until"] = 0.0
        owner_note("mine", name)
        _claim["want"] = False
        return
    if m.get("pending"):
        board_owner["ask_until"] = time.time() + float(m.get("sec", 30))
        owner_note("asking", name)
        return
    why = m.get("why") or ("declined" if m.get("declined") else "owned")
    board_owner["ask_until"] = 0.0
    if why in ("declined", "timeout"):
        board_owner["declined_at"] = time.time()
    if _claim["want"] and why != "owned":
        _claim["result"] = why
        _claim["want"] = False   # here, not in board_claim's poll: the BLE loop would claim again first
    owner_note("standby" if m.get("id") else "free", name)

def board_claim():
    """Settings page / `pet claim`: make the board follow this Mac. Over a
    link we already hold it is one message; otherwise the BLE loop connects
    to the board as a second central and claims."""
    if board_owner["state"] == "mine":
        return {"ok": True, "name": board_owner["name"], "already": True}
    with lock:
        linked = bool(boards) or _ble_client is not None
    _claim["result"] = ""
    _claim["want"] = True
    if linked:
        push_board_raw_claim()
    elif _ble_loop is not None and _claim["evt"] is not None:
        _ble_loop.call_soon_threadsafe(_claim["evt"].set)   # cut the scan pause short
    deadline = time.time() + CLAIM_WAIT_S
    while time.time() < deadline:
        if board_owner["state"] == "mine":
            return {"ok": True, "name": board_owner["name"]}
        if _claim["result"]:                  # the board said no (tap / 30 s / asleep / busy)
            _claim["want"] = False
            return {"ok": False, "why": _claim["result"]}
        time.sleep(0.3)
    _claim["want"] = False
    return {"ok": False, "why": "asking" if board_owner["state"] == "asking" else "timeout"}

def push_board_raw_claim():
    msg = hostinfo_msg(claim=True)
    with lock:
        has_tcp = bool(boards)
    if has_tcp:
        tcp_send(msg)
    else:
        ble_send(msg)

def owner_snapshot():
    me = my_identity()
    left = board_owner["ask_until"] - time.time()
    return {"state": board_owner["state"], "name": board_owner["name"],
            "me": me["name"], "claiming": _claim["want"],
            "ask_s": max(0, int(left)) if board_owner["state"] == "asking" else 0}

# The board has exactly ONE file transfer slot (xfId): a second fbeg replaces
# the first, so two overlapping pushes corrupt each other — 2026-09-08 a cover
# push landed inside an SD-OTA window and killed it twice (why=b64). Every
# transfer, and the whole OTA sequence around one, takes this lock; a caller
# that cannot have it waits its turn. RLock, because board_ota holds it and
# then calls push_file on the same thread.
_file_push_lock = threading.RLock()

# Files that must reach the card but could not (board on BLE only): a small
# queue drained on the next TCP hello. ~/.agentpet/push_queue.json =
# [{"src": "/abs/local", "dst": "/agentpet/…"}]; entries leave on success.
PUSH_QUEUE = Path.home() / ".agentpet" / "push_queue.json"

def push_queue_add(src, dst):
    try:
        q = json.loads(PUSH_QUEUE.read_text()) if PUSH_QUEUE.exists() else []
    except (OSError, ValueError):
        q = []
    q = [e for e in q if not (e.get("src") == src and e.get("dst") == dst)]
    q.append({"src": src, "dst": dst})
    PUSH_QUEUE.write_text(json.dumps(q, ensure_ascii=False, indent=1))
    return len(q)

def push_queue_drain(delay=5.0):
    """Run on a thread after a TCP hello: push what is owed, keep what failed."""
    time.sleep(delay)
    try:
        q = json.loads(PUSH_QUEUE.read_text()) if PUSH_QUEUE.exists() else []
    except (OSError, ValueError):
        return
    if not q:
        return
    left = []
    for e in q:
        src, dst = e.get("src", ""), e.get("dst", "")
        if not (src and dst and Path(src).exists()):
            log("push queue: dropping", e)
            continue
        r = push_file(src, dst, wait=30)          # busy card: stays queued for the next hello
        log("push queue:", dst, "ok" if r.get("ok") else r.get("why"))
        if not r.get("ok"):
            left.append(e)
    try:
        if left:
            PUSH_QUEUE.write_text(json.dumps(left, ensure_ascii=False, indent=1))
        else:
            PUSH_QUEUE.unlink()
    except OSError:
        pass

def push_file(src, dst, progress=None, wait=None):
    """Copy a local file onto the board's microSD at dst (absolute card path).
    fbeg -> chunks of 1024 raw bytes as base64 -> fend; the board verifies size
    + CRC32 and renames its .part over dst. Returns a dict with ok/why/timing.
    Serialized against every other transfer by _file_push_lock.
    progress(sent, total) is called every ~256 KB when given (audio pushes).
    wait=None queues behind whoever holds the lock (OTA, audio, /sd/push —
    the caller asked for exactly this file). wait=N gives up after N s with
    {"ok": False, "busy": True}: for the non-critical pushes (covers, the
    almanac year file, the push queue) that can simply come back later
    instead of stacking up behind a 4-minute OTA."""
    if not _file_push_lock.acquire(timeout=-1 if wait is None else max(0.0, float(wait))):
        return {"ok": False, "busy": True,
                "why": "transfer busy (another push or an OTA holds the card)"}
    try:
        try:
            data = Path(src).read_bytes()
        except OSError as e:
            return {"ok": False, "why": f"read {src}: {e}"}
        with lock:
            if not boards:
                return {"ok": False, "why": "no TCP board (Wi-Fi link needed)"}
        rid = _new_rid()
        crc = zlib.crc32(data) & 0xFFFFFFFF
        t0 = time.time()
        push_tcp({"t": "fbeg", "id": rid, "path": dst, "size": len(data), "crc": "%08x" % crc})
        r = wait_reply("fack", rid, 10)
        if not r or not r.get("ok"):
            return {"ok": False, "why": (r or {}).get("why", "no answer to fbeg")}
        lines = []
        for seq, i in enumerate(range(0, len(data), 1024)):
            lines.append(json.dumps({"t": "fdat", "id": rid, "seq": seq,
                                     "d": base64.b64encode(data[i:i + 1024]).decode()}))
            if len(lines) == 4:                    # ~5.6 KB per sendall: full segments
                tcp_send(("\n".join(lines) + "\n").encode())
                lines = []
                if progress and (seq & 0xFF) == 0xFF:      # every 256 KB, never per chunk
                    try:
                        progress(min(i + 1024, len(data)), len(data))
                    except Exception:
                        progress = None                   # a broken reporter must not kill the push
        if lines:
            tcp_send(("\n".join(lines) + "\n").encode())
        push_tcp({"t": "fend", "id": rid})
        r = wait_reply("fack", rid, 120)
        ms = int((time.time() - t0) * 1000)
        if not r or not r.get("ok"):
            return {"ok": False, "why": (r or {}).get("why", "no answer to fend"), "ms": ms}
        out = {"ok": True, "dst": dst, "bytes": len(data), "ms": ms,
               "kbps": round(len(data) / 1024 / max(ms, 1) * 1000, 1)}
        out.update({k: v for k, v in r.items() if k not in ("t", "id", "ok", "bytes", "ms")})
        log(f"sd push: {dst} {len(data)} B in {ms} ms")
        return out
    finally:
        _file_push_lock.release()

def board_ota(src, rollback=False):
    """SD-OTA: push the .bin onto the card, have the board flash
    it into the other app slot and reboot, then wait for its hello.
    rollback=True: boot the other slot (the previous firmware) instead."""
    # One transfer at a time, and the flash + reboot window belongs to us
    # too: a cover push arriving mid-OTA takes the board's only xfId slot.
    with _file_push_lock:
        t0 = time.time()
        old = {"build": board_build, "part": board_part}
        rid = _new_rid()
        push = None
        if rollback:
            push_tcp({"t": "ota", "id": rid, "rollback": True})
            r = wait_reply("ota", rid, 15)
        else:
            push = push_file(src, OTA_CARD_PATH)
            if not push.get("ok"):
                return {"ok": False, "stage": "push", **push}
            crc = zlib.crc32(Path(src).read_bytes()) & 0xFFFFFFFF
            push_tcp({"t": "ota", "id": rid, "path": OTA_CARD_PATH,
                      "size": push["bytes"], "crc": "%08x" % crc})
            r = wait_reply("ota", rid, 90)          # SD read + flash write, ~10 s
        if not r or not r.get("ok"):
            return {"ok": False, "stage": "flash", "why": (r or {}).get("why", "no answer"),
                    "push": push, "board": r}
        t_reply = time.time()
        while time.time() - t_reply < 60 and board_hello_at < t_reply:
            time.sleep(0.2)
        back = board_hello_at >= t_reply
        out = {"ok": back, "stage": "done" if back else "reboot",
               "push_ms": push and push.get("ms"), "push_kbps": push and push.get("kbps"),
               "flash_ms": r.get("ms"), "bytes": r.get("bytes"), "slot": r.get("part"),
               "old": old, "new": {"build": board_build, "part": board_part},
               "reboot_ms": int((board_hello_at - t_reply) * 1000) if back else None,
               "total_ms": int((time.time() - t0) * 1000)}
        if not back:
            out["why"] = "no hello within 60 s after reboot"
        log("ota:", out)
        return out

def board_ls(path):
    rid = _new_rid()
    push_msg({"t": "fls", "id": rid, "path": path})
    r = wait_reply("fls", rid, 8)
    return r or {"ok": False, "why": "no answer"}

def board_rm(path):
    rid = _new_rid()
    push_msg({"t": "frm", "id": rid, "path": path})
    return wait_reply("frm", rid, 8) or {"ok": False, "why": "no answer"}

def board_cat(path):
    rid = _new_rid()
    push_msg({"t": "fcat", "id": rid, "path": path})
    r = wait_reply("fcat", rid, 8)
    if not r:
        return {"ok": False, "why": "no answer"}
    if "d" in r:
        r["text"] = base64.b64decode(r.pop("d")).decode("utf-8", "replace")
    return r

# ------------------------------------------------------------------ card fonts
# The board's full CJK faces live on the card (/agentpet/fonts/*.afn, ~16 MB;
# the flash tables only hold the almanac's own words). No font data ships with
# the code: this Mac bakes the faces from its own system fonts
# (gen_almanac_font.py --sd, ~10 s) and pushes every face the card lacks, on
# each TCP hello with a card in and when a card is plugged in. A push lands as
# .part and is renamed only once the CRC checks, so a face that is on the card
# is whole. The settings page shows font_rt; /fonts/push?force=1 redoes all.
FONT_FACES = ("head26", "body26", "quot24", "tiny18")
FONT_CARD_DIR = "/agentpet/fonts"
FONT_GEN = Path(__file__).resolve().parent / "gen_almanac_font.py"
FONT_TMP = Path.home() / ".agentpet" / "afn.tmp"
# state: "" (not checked yet) / checking / ok / nocard / baking / pushing /
# nopil (Pillow missing) / wait (needs Wi-Fi or the card is busy) / error
font_rt = {"state": "", "why": "", "face": "", "i": 0, "n": 0, "pct": 0, "at": 0.0}
_font_job = threading.Lock()

def font_step(state, **kw):
    font_rt.update({"state": state, "why": "", "face": "", "i": 0, "n": 0, "pct": 0,
                    "at": time.time()}, **kw)

def fonts_ensure(delay=0.0, force=False):
    """Put every missing face on the card (force: all four). One run at a
    time; a second caller gets busy instead of a second 16 MB push."""
    if delay:
        time.sleep(delay)
    if not _font_job.acquire(blocking=False):
        return {"ok": False, "busy": True, "why": "font push already running"}
    try:
        r = _fonts_ensure(force)
    except Exception as e:                  # never take the hello thread down with us
        font_step("error", why=str(e)[:120])
        r = {"ok": False, "why": str(e)}
    finally:
        _font_job.release()
        shutil.rmtree(FONT_TMP, ignore_errors=True)
    if r.get("pushed") or not r.get("ok"):
        log("fonts:", r)
    return r

def _fonts_ensure(force):
    with lock:
        tcp = bool(boards)
    if not tcp:
        font_step("wait", why="no TCP board")
        return {"ok": False, "why": "no TCP board (Wi-Fi link needed)"}
    font_step("checking")
    r = board_ls(FONT_CARD_DIR)
    if r.get("why") == "no sd":
        font_step("nocard")
        return {"ok": False, "why": "no sd"}
    if r.get("why") not in (None, "not a dir"):      # "not a dir" = no fonts yet
        font_step("error", why=r.get("why"))
        return {"ok": False, "why": r.get("why")}
    have = {e[0] for e in r.get("e") or [] if len(e) >= 3 and not e[2] and e[1] > 0}
    need = [f for f in FONT_FACES if force or f + ".afn" not in have]
    if not need:
        font_step("ok", n=len(FONT_FACES))
        return {"ok": True, "pushed": []}
    font_step("baking", n=len(need))
    shutil.rmtree(FONT_TMP, ignore_errors=True)
    try:
        p = subprocess.run([sys.executable, str(FONT_GEN), "--sd", str(FONT_TMP),
                            "--only", ",".join(need)],
                           capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        font_step("error", why=str(e)[:120])
        return {"ok": False, "why": str(e)}
    if p.returncode:
        if "No module named 'PIL'" in p.stderr:
            font_step("nopil")
            return {"ok": False, "why": "Pillow missing"}
        tail = (p.stderr.strip().splitlines() or ["exit %d" % p.returncode])[-1]
        font_step("error", why=tail[:120])
        return {"ok": False, "why": tail}
    files = [FONT_TMP / (f + ".afn") for f in need]
    total = sum(f.stat().st_size for f in files)
    done, pushed = 0, []
    # all faces under one hold of the card: an episode push that a later
    # hello starts must not wedge itself between them
    if not _file_push_lock.acquire(timeout=600):
        font_step("wait", why="card busy")
        return {"ok": False, "why": "transfer busy"}
    try:
        for i, f in enumerate(files, 1):
            face = f.stem
            font_step("pushing", face=face, i=i, n=len(files),
                      pct=int(done * 100 / max(total, 1)))
            r = push_file(str(f), "%s/%s.afn" % (FONT_CARD_DIR, face),
                          progress=lambda sent, _t, d=done: font_rt.update(
                              pct=int((d + sent) * 100 / max(total, 1))))
            if not r.get("ok"):
                with lock:
                    tcp = bool(boards)
                font_step("error" if tcp else "wait", why=r.get("why"))
                return {"ok": False, "why": r.get("why"), "pushed": pushed}
            done += f.stat().st_size
            pushed.append(face)
    finally:
        _file_push_lock.release()
    font_step("ok", n=len(FONT_FACES))
    return {"ok": True, "pushed": pushed, "bytes": total}

# ------------------------------------------------------------------ TTS
# ------------------------------------------------------------------ screenshot (/test/shot)
# The board answers {"t":"shot","id","s"} with sbeg (w,h,n) + n × sdat (seq,
# base64 of ≤1440 RGB565-LE bytes) + sfin (ok,ms) — TCP only, see main.cpp
# sendShot(). Parts are gathered per id; on sfin the frame is written as PNG
# (stdlib zlib only — /usr/bin/python3 has no Pillow) to ~/.agentpet/shots/
# <stamp>.png, mirrored to latest.png, and the result parked as ("shot", id)
# for the HTTP thread waiting in board_shot(). Closes the "Claude can't see
# the face it drew" loop: Read latest.png and look.
SHOT_DIR = Path.home() / ".agentpet" / "shots"
_shots = {}
_rgb565_lut = None

def rgb565_to_png(raw, w, h):
    global _rgb565_lut
    if _rgb565_lut is None:                              # 64 K entries, built once
        r5 = [(i * 255 + 15) // 31 for i in range(32)]
        g6 = [(i * 255 + 31) // 63 for i in range(64)]
        _rgb565_lut = [bytes((r5[v >> 11], g6[(v >> 5) & 63], r5[v & 31]))
                       for v in range(65536)]
    lut = _rgb565_lut
    px = struct.unpack("<%dH" % (w * h), raw[:w * h * 2])
    rows = bytearray()
    for y in range(h):
        rows.append(0)                                   # PNG filter: none
        rows += b"".join([lut[v] for v in px[y * w:(y + 1) * w]])

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
            + chunk(b"IEND", b""))

def _shot_part(m):
    sid, t = m.get("id"), m.get("t")
    if t == "sbeg":
        _shots[sid] = {"w": m.get("w"), "h": m.get("h"), "n": m.get("n"),
                       "parts": {}, "t0": time.time()}
        return
    if t == "sdat":
        s = _shots.get(sid)
        if s is not None:
            s["parts"][m.get("seq")] = m.get("d", "")
        return
    s = _shots.pop(sid, None)                            # sfin
    res = {"t": "shot", "id": sid, "ok": False}
    if not m.get("ok"):
        res["why"] = m.get("why", "board failed")
    elif s is None:
        res["why"] = "sfin without sbeg"
    else:
        missing = [i for i in range(s["n"]) if i not in s["parts"]]
        if missing:
            res["why"] = "missing chunks %s" % missing[:5]
        else:
            try:
                raw = b"".join(base64.b64decode(s["parts"][i]) for i in range(s["n"]))
                png = rgb565_to_png(raw, s["w"], s["h"])
                SHOT_DIR.mkdir(parents=True, exist_ok=True)
                path = SHOT_DIR / (time.strftime("%Y%m%d-%H%M%S") + ".png")
                path.write_bytes(png)
                (SHOT_DIR / "latest.png").write_bytes(png)
                res.update(ok=True, path=str(path), w=s["w"], h=s["h"], bytes=len(raw),
                           ms=int((time.time() - s["t0"]) * 1000), board_ms=m.get("ms"))
                log(f"shot: {s['w']}x{s['h']} -> {path} in {res['ms']} ms")
            except Exception as e:                       # bad base64 / short frame
                res["why"] = "decode: %s" % e
    _park_reply(res)

def board_shot(step=2):
    """Grab the board's canvas as PNG; returns {ok, path, w, h, ms, ...}.
    Needs the Wi-Fi link: the reply is ~150 KB at step 2, ~600 KB at step 1."""
    with lock:
        if not boards:
            return {"ok": False, "why": "no TCP board (Wi-Fi link needed)"}
    rid = _new_rid()
    push_tcp({"t": "shot", "id": rid, "s": step})
    r = wait_reply("shot", rid, 30)
    if not r:
        _shots.pop(rid, None)
        return {"ok": False, "why": "no answer in 30 s"}
    return {k: v for k, v in r.items() if k not in ("t", "id")}

def tts_wav(text, voice=None):
    """Cached synthesis: text -> 16 kHz mono WAV path (None on failure).
    voice None = the current language's voice (tts_voice())."""
    voice = voice or tts_voice()
    VOICES_DIR.mkdir(parents=True, exist_ok=True)
    key = hashlib.md5(f"{voice}|{text}".encode()).hexdigest()[:16]
    wav_path = VOICES_DIR / f"{key}.wav"
    if wav_path.exists():
        return wav_path
    mp3 = wav_path.with_suffix(".mp3")
    try:
        r = subprocess.run(
            ["/usr/bin/python3", "-m", "edge_tts", "-v", voice,
             "-t", text, "--write-media", str(mp3)],
            capture_output=True, timeout=25)
        if r.returncode != 0 or not mp3.exists():
            raise RuntimeError(r.stderr.decode(errors="replace")[:200])
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
             str(mp3), str(wav_path)], check=True, timeout=15)
        log(f"tts: synthesized '{text[:24]}' -> {wav_path.name}")
        return wav_path if wav_path.exists() else None
    except Exception as e:
        log(f"tts failed: {e}")
        return None
    finally:
        mp3.unlink(missing_ok=True)

# IMA ADPCM: 4 bits per sample, first sample of each byte in
# the LOW nibble, predictor/index start at 0 for every clip. audio.cpp's
# decoder mirrors this exactly, so the reconstruction here IS the board's.
IMA_INDEX = [-1, -1, -1, -1, 2, 4, 6, 8]
IMA_STEP = [7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31, 34, 37, 41, 45,
            50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143, 157, 173, 190, 209, 230,
            253, 279, 307, 337, 371, 408, 449, 494, 544, 598, 658, 724, 796, 876, 963,
            1060, 1166, 1282, 1411, 1552, 1707, 1878, 2066, 2272, 2499, 2749, 3024, 3327,
            3660, 4026, 4428, 4871, 5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442,
            11487, 12635, 13899, 15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794,
            32767]

def ima_encode_state(pcm, pred=0, idx=0):
    """s16le mono bytes -> (IMA ADPCM bytes, (pred, idx) after the last sample).
    Pure Python twin of audioop.lin2adpcm — kept because audioop is gone in
    3.13, and because it packs the nibbles in the BOARD's order."""
    n = len(pcm) // 2
    out = bytearray((n + 1) // 2)
    for i, s in enumerate(struct.unpack("<%dh" % n, pcm[:n * 2])):
        step = IMA_STEP[idx]
        diff = s - pred
        nib = 0
        if diff < 0:
            nib, diff = 8, -diff
        d = step >> 3
        if diff >= step:
            nib |= 4; diff -= step; d += step
        if diff >= step >> 1:
            nib |= 2; diff -= step >> 1; d += step >> 1
        if diff >= step >> 2:
            nib |= 1; d += step >> 2
        pred = max(-32768, min(32767, pred - d if nib & 8 else pred + d))
        idx = max(0, min(88, idx + IMA_INDEX[nib & 7]))
        if i & 1:
            out[i >> 1] |= nib << 4
        else:
            out[i >> 1] = nib
    return bytes(out), (pred, idx)

def ima_encode(pcm):
    """s16le mono bytes -> IMA ADPCM bytes (4:1), predictor starting at 0."""
    return ima_encode_state(pcm)[0]

# ---- .ima block container: what the board's player reads ----
# 16 B file header ("AGPTIMA1" + u32 LE rate + u32 LE block count), then one
# 8004 B block per second: 4 B header (int16 LE predictor + u8 step index + u8
# 0, the state ENTERING the block) + 8000 nibble bytes = 16000 samples. Every
# block is self-describing, so the board can start at any block without a click
# — that is what makes "resume where I left off" and "skip forward" free.
IMA_MAGIC = b"AGPTIMA1"
IMA_HEAD = 16                                  # magic + rate + block count
IMA_BLOCK_HEAD = 4                             # pred (i16) + idx (u8) + reserved
IMA_BLOCK_SAMPLES = 16000                      # 1 s at 16 kHz
IMA_BLOCK_DATA = IMA_BLOCK_SAMPLES // 2        # 8000 nibble bytes
IMA_BLOCK = IMA_BLOCK_HEAD + IMA_BLOCK_DATA    # 8004 B — a full block, so block k is at a fixed offset
IMA_RATE = 16000
# audioop.lin2adpcm packs sample 0 in the HIGH nibble; audio.cpp reads the LOW
# one first (`data[i] & 0x0F` then `>> 4`). Measured 2026-09-08: [20000,0,-8000,
# 3000] -> audioop 7af7 vs the board's a77f.
# So every audioop block gets its nibbles swapped on the way out.
IMA_NIBSWAP = bytes.maketrans(bytes(range(256)),
                              bytes((((b & 15) << 4) | (b >> 4)) for b in range(256)))

def ima_block_encode(pcm, pred=0, idx=0):
    """One container block from ≤1 s of s16le PCM: (bytes, next state).
    audioop when it is there (C speed: an hour encodes in ~0.3 s), the pure
    encoder otherwise; both produce the same bytes."""
    head = struct.pack("<hBB", pred, idx, 0)
    pcm = pcm[:len(pcm) // 4 * 4]              # whole nibble pairs; audioop drops a lone sample too
    if audioop is not None:
        data, (pred, idx) = audioop.lin2adpcm(pcm, 2, (pred, idx))
        data = data.translate(IMA_NIBSWAP)
    else:
        data, (pred, idx) = ima_encode_state(pcm, pred, idx)
    return head + data, (pred, idx)

def ima_pack(pcm, rate=IMA_RATE):
    """s16le mono PCM -> .ima container bytes. The last block may be short."""
    blocks, pred, idx, n = [], 0, 0, 0
    step = IMA_BLOCK_SAMPLES * 2
    for off in range(0, max(len(pcm) // 4 * 4, 0), step):
        blk, (pred, idx) = ima_block_encode(pcm[off:off + step], pred, idx)
        if len(blk) <= IMA_BLOCK_HEAD:         # a tail shorter than one sample pair
            break
        blocks.append(blk)
        n += 1
    return IMA_MAGIC + struct.pack("<II", rate, n) + b"".join(blocks)

def ima_block_offset(k):
    """Byte offset of block k — every block but the last is exactly 8004 B."""
    return IMA_HEAD + k * IMA_BLOCK

def ima_head(data):
    """(rate, block count) from a container header; raises on a foreign file."""
    if len(data) < IMA_HEAD or bytes(data[:8]) != IMA_MAGIC:
        raise ValueError("not an AGPTIMA1 container")
    return struct.unpack("<II", bytes(data[8:16]))

def ima_unpack(data, first=0, count=None):
    """Container -> s16le PCM, decoding blocks [first, first+count) exactly the
    way player.cpp does: seek to the block, take its state from its own header,
    low nibble before high. Starting anywhere gives bit-identical samples."""
    rate, n = ima_head(data)
    if count is None:
        count = n - first
    out = bytearray()
    for k in range(first, min(first + count, n)):
        off = ima_block_offset(k)
        if off + IMA_BLOCK_HEAD > len(data):
            break
        pred, idx, _ = struct.unpack("<hBB", bytes(data[off:off + IMA_BLOCK_HEAD]))
        body = bytes(data[off + IMA_BLOCK_HEAD:off + IMA_BLOCK])
        for b in body:
            for nib in (b & 0x0F, b >> 4):
                stp = IMA_STEP[idx]
                d = stp >> 3
                if nib & 1:
                    d += stp >> 2
                if nib & 2:
                    d += stp >> 1
                if nib & 4:
                    d += stp
                pred = pred - d if nib & 8 else pred + d
                pred = max(-32768, min(32767, pred))
                idx = max(0, min(88, idx + IMA_INDEX[nib & 7]))
                out += struct.pack("<h", pred)
    return bytes(out)

def wav_open_pcm(path):
    """Open a RIFF/WAVE file and stop at its samples: returns
    (file object, format code, channels, rate, bits, data bytes).
    Not the wave module: afconvert writes WAVE_FORMAT_EXTENSIBLE (0xFFFE) when
    the source is an m4a — measured 2026-09-08 on a 小宇宙 episode — and
    wave.open refuses that with "unknown format: 65534". It also pads its
    output with an FLLR chunk, so the chunk walk is not optional either."""
    f = open(path, "rb")
    try:
        head = f.read(12)
        if len(head) < 12 or head[:4] != b"RIFF" or head[8:12] != b"WAVE":
            raise ValueError("not a RIFF/WAVE file")
        fmt = None
        while True:
            hdr = f.read(8)
            if len(hdr) < 8:
                raise ValueError("no data chunk in " + str(path))
            tag, size = struct.unpack("<4sI", hdr)
            if tag == b"fmt ":
                body = f.read(size + (size & 1))
                if len(body) < 16:
                    raise ValueError("truncated fmt chunk")
                code, ch, srate, _bps, _align, bits = struct.unpack("<HHIIHH", body[:16])
                if code == 0xFFFE and len(body) >= 26:       # extensible: the real code is in the GUID
                    code = struct.unpack("<H", body[24:26])[0]
                fmt = (code, ch, srate, bits)
            elif tag == b"data":
                if fmt is None:
                    raise ValueError("data chunk before fmt")
                left = os.path.getsize(path) - f.tell()
                return (f,) + fmt + (min(size, max(left, 0)),)
            else:
                f.seek(size + (size & 1), 1)
    except Exception:
        f.close()
        raise

def ima_pack_file(wav_path, out_path, rate=IMA_RATE):
    """Stream a 16 kHz mono WAV into a .ima container (a one-hour episode is
    ~115 MB of PCM; never hold that in RAM). Returns {blocks, bytes, secs}."""
    f, code, ch, got, bits, left = wav_open_pcm(wav_path)
    with f:
        if code != 1 or ch != 1 or bits != 16:
            raise ValueError("need 16-bit mono PCM, got format %d / %d ch / %d bit"
                             % (code, ch, bits))
        if got != rate:
            raise ValueError("need %d Hz, got %d" % (rate, got))
        pred, idx, n = 0, 0, 0
        tmp = Path(str(out_path) + ".part")
        with open(tmp, "wb") as out:
            out.write(IMA_MAGIC + struct.pack("<II", rate, 0))   # count patched at the end
            while left >= 4:
                pcm = f.read(min(left, IMA_BLOCK_SAMPLES * 2))
                if len(pcm) < 4:
                    break
                left -= len(pcm)
                blk, (pred, idx) = ima_block_encode(pcm, pred, idx)
                out.write(blk)
                n += 1
            out.flush()
            out.seek(12)
            out.write(struct.pack("<I", n))
        tmp.replace(out_path)
    return {"blocks": n, "bytes": Path(out_path).stat().st_size, "secs": n}

def speak_push(wav_path, fmt=None, wait_stats=False):
    """Stream a cached WAV to the board: {"t":"speak",id,len,fmt} header, then
    base64 {"t":"pcm"} chunks of ≤1024 wire bytes. fmt "ima" sends IMA ADPCM
    (cached next to the wav as .ima), "pcm" the raw samples. The board starts
    playing after 8 KB of PCM and reports {"t":"spk"} timing when the clip ends;
    wait_stats=True waits for it. Returns {ok, fmt, pcm_bytes, wire_bytes,
    push_ms[, board]}."""
    fmt = fmt or TTS_FMT
    try:
        with wave.open(str(wav_path), "rb") as w:
            if (w.getframerate(), w.getnchannels(), w.getsampwidth()) != (16000, 1, 2):
                log(f"speak: bad wav format {wav_path}")
                return {"ok": False, "why": "bad wav format"}
            pcm = w.readframes(w.getnframes())
    except Exception as e:
        log(f"speak: {e}")
        return {"ok": False, "why": str(e)}
    for attempt in (0, 1):
        with lock:
            tcp_up = bool(boards)
        if tcp_up:
            break
        if attempt:                  # still down after the grace wait
            log("speak: no TCP board, skipped")
            return {"ok": False, "why": "no TCP board"}
        time.sleep(3)                # reconnect blips heal in ~1-3 s
    if fmt == "ima":
        ima_path = Path(wav_path).with_suffix(".ima")
        try:
            payload = ima_path.read_bytes()
        except OSError:
            payload = ima_encode(pcm)
            try:
                ima_path.write_bytes(payload)
            except OSError:
                pass
    else:
        fmt, payload = "pcm", pcm
    rid = _new_rid()
    t0 = time.time()
    push_tcp({"t": "speak", "id": rid, "len": len(pcm), "fmt": fmt})
    for i in range(0, len(payload), 1024):
        push_tcp({"t": "pcm", "d": base64.b64encode(payload[i:i + 1024]).decode()})
    out = {"ok": True, "fmt": fmt, "pcm_bytes": len(pcm), "wire_bytes": len(payload),
           "push_ms": int((time.time() - t0) * 1000)}
    if wait_stats:
        r = wait_reply("spk", rid, 20)
        out["board"] = {k: v for k, v in r.items() if k not in ("t", "id")} if r else None
    log(f"speak: {fmt} {len(payload)} B on the wire in {out['push_ms']} ms"
        + (f", board {out.get('board')}" if wait_stats else ""))
    return out

def say(text, voice=None):
    w = tts_wav(text, voice)
    return bool(w) and bool(speak_push(w).get("ok"))

def say_qian(pick=0):
    """The fortune is Chinese text on a Chinese page: Chinese voice in both languages."""
    return say(qian_text(pick), TTS_VOICES["zh"])

def phrase_needs(a):
    if LANG == "en":
        return f"Hey, {SPEAK_NAMES_EN.get(a, a)} needs you!"
    return f"主人，{SPEAK_NAMES.get(a, a)}在等你哦！"

def phrase_done(a):
    if LANG == "en":
        return f"{SPEAK_NAMES_EN.get(a, a)} is done!"
    return f"{SPEAK_NAMES.get(a, a)}干完活啦！"

def qian_text(pick=0):
    """The fortune the board is SHOWING: pick 0 = the day's own draw, 1..4 =
    the alternates the almanac page cycles (board {"t":"qian","pick":n});
    anything out of range reads as the main draw, same as on the board."""
    m = almanac_msg()
    q = m["qian"]
    try:
        pick = int(pick or 0)
    except (TypeError, ValueError):
        pick = 0
    alts = m.get("alt") or []
    if 0 < pick <= len(alts):
        q = alts[pick - 1]["qian"]
    return "今日签文。" + q

def speak_events(prev, snap):
    """voice_style tts: speak fresh needs_you/done. The board mutes its own
    chirps for these while TCP is up, so on failure we push the chirp over
    TCP ourselves — the moment is never silent. needs_you outranks done,
    same as the board's own chirp rule."""
    if VOICE_STYLE != "tts" or prev is None:
        return
    needs = [a for a in AGENTS
             if snap.get(a) == "needs_you" and prev.get(a) != "needs_you"]
    done = [a for a in AGENTS
            if snap.get(a) == "done" and prev.get(a) != "done"]
    if needs:
        text, chirp = phrase_needs(needs[0]), "needs"
    elif done:
        text, chirp = phrase_done(done[0]), "done"
    else:
        return
    def run():
        if not say(text):
            with lock:
                tcp_up = bool(boards)
            if tcp_up:               # BLE-only boards already chirped themselves
                push_tcp({"t": "sound", "name": chirp})
    threading.Thread(target=run, daemon=True).start()

def prime_voices():
    """Pre-synthesize the fixed phrase set so runtime speech is pure cache
    (cloud quality + zero latency + works offline afterwards)."""
    try:
        if VOICE_STYLE == "tts":
            for a in AGENTS:
                tts_wav(phrase_needs(a))
                tts_wav(phrase_done(a))
        tts_wav(qian_text(), TTS_VOICES["zh"])   # 求签 works in either style and language
    except Exception as e:
        log("prime voices:", e)

# ------------------------------------------------------------------ cyber almanac
# 赛博黄历: real sexagenary day / zodiac / 建除 (solar-term month, approximate
# fixed boundary dates, ±1 day) + a date-seeded draw from the wordbank below —
# same result all day, fresh each morning. Board renders (方向一 朱砂符纸,
# design canvas artifact 29d853d2). Wordbank hot-override: ~/.agentpet/almanac.json
# with any of {"yi": [...], "ji": [...], "qian": [...]} — read on every draw.
GAN = "甲乙丙丁戊己庚辛壬癸"
ZHI = "子丑寅卯辰巳午未申酉戌亥"
SHENGXIAO = "鼠牛虎兔龙蛇马羊猴鸡狗猪"
JIANCHU = "建除满平定执破危成收开闭"
JIE_DAY = {1: 6, 2: 4, 3: 6, 4: 5, 5: 6, 6: 6,           # 小寒..芒种 (start day
           7: 7, 8: 8, 9: 8, 10: 8, 11: 7, 12: 7}        # of the 节 in month m)

ALMANAC_YI = [
    "仰望真实的星空", "雨夜听 synthwave", "访问旧城区", "与街角的猫交换情报",
    "收集电子羊", "备份珍贵记忆", "为义体充能", "断网冥想三十分钟",
    "擦拭屏幕上的指纹", "去没有信号的地方走走", "给旧设备一次重生",
    "尝一杯热的合成咖啡", "手写一段模拟信号", "在天台看飞行器起降",
    "收听深夜电台", "给生物模块浇水", "读取一段冷存储的回忆",
    "降低主时钟频率慢走", "与真人面对面通信", "看一场限定渲染的日落",
    "热水澡散热重启", "提前进入低功耗模式", "接收恒星的无线充电",
    "逛一逛原始数据集市", "校准机体关节", "听一段模拟信号老歌",
    "打开窗户执行换气协议", "重读一本纸质旧书", "去水边释放内存",
    "擦亮光学传感器", "为缓冲区添一束花", "步行最后一公里",
    "收藏一枚旧零件", "冒雨去吃一碗热面", "整理缠绕的能量线", "给梦预留存储空间",
    # 二期扩充（2026-08-30，宜 36→60）
    "把窗台让给一株多肉", "用现金买一次早餐", "给老朋友发一段语音",
    "关掉推送看完一场雨", "把手机调成灰度一下午", "去菜市场校准烟火气",
    "给耳机做一次除尘", "在纸上画自己的电路图", "把旧照片洗成实体",
    "沿着河岸做信号漫游", "给键盘换一颗新键帽", "在日落前完成今日存档",
    "认领一颗肉眼可见的星", "把被子晒出恒星的味道", "去楼下和保安互道晚安",
    "给明天留一行注释", "亲手擦一次真实桌面", "喝水时什么都不想",
    "去还一本欠很久的书", "把闹钟往后调十分钟", "背下一位老友的手机号",
    "给绿植的叶子擦灰", "在天桥上数一百辆车", "把今天的好事写进日志",
]
ALMANAC_JI = [
    "相信全息广告", "在酸雨中久留", "直视耀斑", "接入来路不明的网络",
    "睡前接入信息洪流", "电量低于两成出门", "同时开三十个标签页",
    "与导航争论路线", "向自动门鞠躬", "凌晨三点查看物流",
    "在梦里签署协议", "对镜子里的自己撒谎", "吞下未验证的补丁",
    "与电梯抢时间", "在深夜放大旧照片", "给过去的自己发消息",
    "高峰期挤进运输舱", "用怒气回复任何消息", "空腹喝冷的合成咖啡",
    "淋雨后不烘干机体", "把秘密存进公共云", "在闪烁的霓虹下久坐",
    "追逐红色的激光点", "打断正在做梦的猫", "拔掉别人的充电线",
    "冲动升级自己的固件", "把心事加密后丢失密钥", "透支明天的电量",
    "在噪音里校准听觉", "回复三年前的争论", "用倍速播放黄昏",
    "囤积不再点亮的设备", "跟全息偶像告白", "忽略低电量警告", "在防火墙外裸奔",
    # 二期扩充（2026-08-30，忌 35→60）
    "睡前刷新负面新闻", "和客服机器人置气", "在电量焦虑中反复插拔",
    "把体检报告拖进回收站", "用外卖代替所有晚餐", "深夜下单不需要的东西",
    "对着进度条大喊加速", "在地铁里回味尴尬瞬间", "给旧爱的动态点赞",
    "同时追三部烂剧", "在雨天穿新买的白鞋", "把伞借给不会还的人",
    "把梦话设为公开可见", "囤积永远不看的收藏夹", "用冷水唤醒热引擎",
    "在凌晨对比别人的高光", "给差评后反复查看回复", "穿着湿袜子走完全程",
    "把最后一格电留给广告", "在风口张嘴说大话", "反复排练没发生的争吵",
    "把密码写在显示器边上", "空腹逛生鲜市场", "在打折时买第三个键盘",
    "让眼睛硬撑到天亮",
]
ALMANAC_QIAN = [
    "所有断线，终将重连。", "缓存会清空，记忆不会。", "电量有限，热爱无限。",
    "今日信号良好，宜与旧友重连。", "心跳也是一种时钟频率。",
    "别让后台进程偷走此刻。", "每一次重启，都是新生。",
    "雨落在霓虹上，也落在你肩上。", "数据会过期，黄昏不会。",
    "你不是冗余，你是孤本。", "慢一点，带宽留给重要的人。",
    "防火墙外，春天照常运行。", "未读消息会等你，月亮不会。",
    "噪声之中，仍有你的频率。", "旧城区的猫认识所有捷径。",
    "星空不需要会员，抬头即可。", "今天适合把自己调成飞行模式。",
    "破损的像素也在发光。", "记得给梦留一点存储空间。",
    "世界在加载，请勿频繁刷新。", "有些答案在离线时才会出现。",
    "你的孤独已加密，无人可读。", "霓虹再亮，也替代不了晨光。",
    "保持心跳，其余交给时间。", "温柔是最古老的协议。",
    "明天的版本号，由今晚决定。", "长夜漫游，注意剩余电量。",
    "万物皆有裂缝，那是光进来的端口。",
    # 二期扩充（2026-08-30，签文 28→50）
    "信号弱的地方，星星更亮。", "你丢失的包，宇宙已签收。",
    "慢加载的，往往值得等。", "错过的班车，载走了错的方向。",
    "别在低电量时做大决定。", "尘埃也曾是恒星的代码。",
    "今天的乱码，明天读作诗。", "被拒绝的请求，换个端口再来。",
    "月亮不回消息，但一直在线。", "把心跳交给今天，把心事交给明天。",
    "旧城区的雨，落在新版本上。", "你不必同步所有人的时区。",
    "掉线的那段路，风景最清楚。", "温柔运行，无需超频。",
    "答案在缓冲，请勿跳过。", "每个孤本都自带备份的勇气。",
    "黄昏是白昼优雅的降级。", "留白，是界面对你的温柔。",
    "没有信号的山顶，满格的自由。", "重要的更新，都在深夜安静完成。",
    "你的频率，总有人在收听。", "灯火向你敞开全部端口。",
]
ALMANAC_COLORS = [("霓虹青", "#3EE6D2"), ("电子紫", "#A778FF"),
                  ("磷光绿", "#3DDC84"), ("全息蓝", "#5096FF"),
                  ("朱砂红", "#E23E2B"), ("日落橙", "#FF8A3D"),
                  ("铬银", "#C9D1D9"), ("蜜金", "#E8C56A")]
ALMANAC_DIRS = ["东", "南", "西", "北", "东南", "东北", "西南", "西北"]
ALMANAC_OVERRIDE = Path.home() / ".agentpet" / "almanac.json"
# Big wordbank (deployed copy of host/almanac_bank.json; 宜/忌 ≥730, 签文 ≥365
# so a whole year never repeats a line). The lists above stay as fallback.
ALMANAC_BANK = Path.home() / ".agentpet" / "almanac_bank.json"
ALMANAC_PUSHED = Path.home() / ".agentpet" / "almanac_pushed.json"

def almanac_bank():
    """(yi, ji, qian): bank file if present, else the built-in lists; the
    hot-override file ADDS lines on top (it used to replace the lists)."""
    yi, ji, qian = ALMANAC_YI, ALMANAC_JI, ALMANAC_QIAN
    if ALMANAC_BANK.exists():
        try:
            bank = json.loads(ALMANAC_BANK.read_text())
            yi = bank.get("yi") or yi
            ji = bank.get("ji") or ji
            qian = bank.get("qian") or qian
        except Exception as e:
            log("almanac bank error:", e)
    if ALMANAC_OVERRIDE.exists():
        try:
            ov = json.loads(ALMANAC_OVERRIDE.read_text())
            yi = yi + [x for x in (ov.get("yi") or []) if x not in yi]
            ji = ji + [x for x in (ov.get("ji") or []) if x not in ji]
            qian = qian + [x for x in (ov.get("qian") or []) if x not in qian]
        except Exception as e:
            log("almanac override error:", e)
    return yi, ji, qian

def almanac_plan(d):
    """Year schedule instead of per-day random draws: each pool is shuffled
    once per year (seeded by the year) and handed out in day order, so every
    line is used exactly once before any repeat — zero repeats within a year
    when the pool is big enough (2/day -> 730 宜/忌, 1/day -> 365 签)."""
    yi, ji, qian = almanac_bank()
    rng = random.Random(f"{d.year}-agentpet-plan")
    yi_p, ji_p, q_p = yi[:], ji[:], qian[:]
    rng.shuffle(yi_p); rng.shuffle(ji_p); rng.shuffle(q_p)
    i = d.timetuple().tm_yday - 1
    pick = lambda pool, k: [pool[(i * k + j) % len(pool)] for j in range(k)]
    return pick(yi_p, 2), pick(ji_p, 2), pick(q_p, 1)[0]

def almanac_alts(d, yi2, ji2, qian1, n=4):
    """The other four readings of the same day (2026-09-22): the board cycles
    five readings locally — 换一签 on the almanac page is an up/down swipe,
    and it has to work with the Mac asleep, the year file on the card carries
    them too. Seeded by the date, so the same day
    always offers the same five and a re-push after a hello does not move the
    reading the user is looking at (the board keeps `pick` only while the
    words are identical). The day's own draw is excluded and the four are
    sampled without replacement, so no line appears twice across the five. A
    bank too small to fill four complete readings gives however many it can."""
    yi, ji, qian = almanac_bank()
    rng = random.Random(d.isoformat() + "-agentpet-alt")
    yi_pool = [x for x in yi if x not in yi2]
    ji_pool = [x for x in ji if x not in ji2]
    q_pool = [x for x in qian if x != qian1]
    k = min(n, len(yi_pool) // 2, len(ji_pool) // 2, len(q_pool))
    if k <= 0:
        return []
    yi_pick = rng.sample(yi_pool, 2 * k)
    ji_pick = rng.sample(ji_pool, 2 * k)
    q_pick = rng.sample(q_pool, k)
    return [{"yi": yi_pick[2 * i:2 * i + 2], "ji": ji_pick[2 * i:2 * i + 2],
             "qian": q_pick[i]} for i in range(k)]

def almanac_msg(d=None):
    d = d or datetime.date.today()
    jdn = d.toordinal() + 1721425
    day_idx = (jdn + 49) % 60                    # anchor: 2000-01-01 = 戊午
    day_branch = day_idx % 12
    # solar-term month: before the 节 of month m the branch month is m-1
    bm = d.month if d.day >= JIE_DAY[d.month] else d.month - 1
    if bm == 0:
        bm = 12
    month_branch = bm % 12                       # 立春(2月)=寅(2) ... 大雪(12月)=子(0)
    year_eff = d.year if (d.month, d.day) >= (2, 4) else d.year - 1
    yi2, ji2, qian1 = almanac_plan(d)
    rng = random.Random(d.isoformat() + "-agentpet")
    cname, chex = rng.choice(ALMANAC_COLORS)
    return {"t": "almanac",
            "gz": GAN[day_idx % 10] + ZHI[day_branch] + "日",
            "sx": "属" + SHENGXIAO[(year_eff - 4) % 12],
            "jc": JIANCHU[(day_branch - month_branch) % 12] + "日",
            "date": "%02d.%02d" % (d.month, d.day),
            "yi": yi2, "ji": ji2, "qian": qian1,
            "dir": rng.choice(ALMANAC_DIRS), "sig": rng.randint(2, 4),
            "cn": cname, "ch": chex,
            "alt": almanac_alts(d, yi2, ji2, qian1)}   # own rng: colour/signal unchanged

def almanac_year_file(year):
    """Write ~/.agentpet/almanac_<year>.jsonl (one almanac per day) and return
    its path + a hash of the wordbank that produced it."""
    lines = []
    d = datetime.date(year, 1, 1)
    while d.year == year:
        lines.append(json.dumps(almanac_msg(d), ensure_ascii=False))
        d += datetime.timedelta(days=1)
    path = Path.home() / ".agentpet" / f"almanac_{year}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    yi, ji, qian = almanac_bank()
    # "alt4" = the file format (five readings per line since 2026-09-22): a
    # bank that has not changed still gets one re-push when the format does.
    h = hashlib.md5(json.dumps([yi, ji, qian, "alt4"], ensure_ascii=False).encode()).hexdigest()[:12]
    return path, h

def almanac_push_year(year=None, force=False):
    """Push this year's (and December: next year's) almanac onto the card so
    the board has it offline. Skipped when the bank hasn't changed since the
    last push, unless force."""
    year = year or datetime.date.today().year
    path, h = almanac_year_file(year)
    done = {}
    try:
        done = json.loads(ALMANAC_PUSHED.read_text())
    except Exception:
        pass
    if not force and done.get(str(year)) == h:
        return {"ok": True, "skipped": "unchanged", "year": year}
    r = push_file(str(path), f"/agentpet/almanac/{year}.jsonl", wait=30)   # busy card: the next hello tries again (hash not recorded)
    if r.get("ok"):
        done[str(year)] = h
        ALMANAC_PUSHED.write_text(json.dumps(done))
    r["year"] = year
    return r

# ------------------------------------------------------------------ stretch reminder
_stretch_start = None
_stretch_last = 0.0
_stretch_sent = 0.0

def stretch_tick():
    global _stretch_start, _stretch_last, _stretch_sent
    if STRETCH_AFTER_MIN <= 0:
        return
    now = time.time()
    with lock:
        busy = any(v == "working" for v in state.values())
    if not busy:
        return
    if _stretch_start is None or now - _stretch_last > STRETCH_GAP_MIN * 60:
        _stretch_start = now
    _stretch_last = now
    if (now - _stretch_start >= STRETCH_AFTER_MIN * 60 and
            now - _stretch_sent >= STRETCH_COOLDOWN_MIN * 60):
        _stretch_sent = now
        push_msg({"t": "stretch"})
        log("stretch reminder: worked %.0f min straight" %
            ((now - _stretch_start) / 60))

_stall_until = [0.0]     # /test/mic/stall: the board reader pauses recv() until then (link-stall simulation)

class BoardHandler(socketserver.BaseRequestHandler):
    def handle(self):
        global _tcp_rx_at
        sockc = self.request
        if links_paused():
            # Mac asleep or in a dark wake: the board retries every 3 s, and
            # a link taken now would ride into the sleep (Mac sleep note).
            # Returning closes it; one log line per sleep, not one per try.
            with _power_lock:
                _power["refused"] += 1
                first = _power["refused"] == 1
            if first:
                log("board connect turned away: the Mac is asleep (links resume when the display is on)")
            return
        sockc.settimeout(60)
        # Every line is < MSS: with Nagle on, each one waited for the board's
        # delayed ACK (~25 ms) -> file/PCM pushes crawled at ~40 KB/s.
        sockc.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        log("board connected:", self.client_address)
        with lock:
            boards.append(sockc)
        push_state()
        buf = b""
        try:
            while True:
                while time.time() < _stall_until[0]:    # simulated stall: TCP window fills, board must not block
                    time.sleep(0.05)
                data = sockc.recv(1024)
                if not data:
                    break
                _tcp_rx_at = time.time()
                buf += data
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self.on_msg(line)
        except OSError:
            pass
        finally:
            with lock:
                if sockc in boards:
                    boards.remove(sockc)
            board_link_down()          # vitals stay readable, just not current
            global _tcp_last_link_at
            _tcp_last_link_at = time.time()
            log("board disconnected")

    def on_msg(self, raw):
        def reply(data):
            with send_lock:
                try:
                    self.request.sendall(data)
                except OSError:
                    pass
        handle_board_msg(raw, reply)

# ------------------------------------------------------------------ now playing
# `media-control` (ungive/mediaremote-adapter) is the only way left to read the
# macOS now-playing info after 15.4 locked MediaRemote down; `media-control
# stream` prints one JSON line per change:
#     {"type":"data","diff":bool,"payload":{title,artist,album,elapsedTime,
#      duration,playing,playbackRate,bundleIdentifier,artworkData,...}}
# diff=true merges into the payload we hold, diff=false replaces it, an empty
# payload means nothing is playing. We turn that into the board's np message,
# throttle it (every visible change, plus a 15 s refresh so the board can
# re-sync the progress it interpolates locally) and ship covers to the card as
# 200x200 baseline JPEGs. No binary = feature silently off after one log line.
MEDIA_CONTROL_BIN = "/opt/homebrew/bin/media-control"
NP_REFRESH_S = 15                       # keep-alive push while something plays
NP_TEXT_MAX = 60                        # characters, not bytes
NP_BACKOFF = (2, 5, 10, 20, 40, 60)     # stream restart delays after a crash
NP_MISS_COOLDOWN = 60                   # per-path re-push floor for np_miss
NP_MISS_GRACE = 5                       # a cover that landed this recently is not re-pushed (the board sees it itself)
NP_MISS_WAIT = 5                        # np_miss gives up on a busy card after this (s)
NP_PUSH_WAIT = 20                       # a cover push waits this long for the card before backing off
NP_COVER_RETRY = (30, 30, 60, 60, 120)  # back-off pauses when the card is busy (OTA / audio)
NP_COVER_TRY = 30                       # per-hash floor for cover convert/push attempts
NP_HELLO_SETTLE = 5                     # no cover push this soon after a board hello (it may be rebooting)
NP_COVER_CARD = "/agentpet/covers"      # cover dir on the microSD
NP_COVERS_DIR = Path.home() / ".agentpet" / "covers"
NP_COVERS_SEEN = Path.home() / ".agentpet" / "covers.json"
# bundle id -> the short app tag the board draws. Everything else falls back to
# the last dotted segment, lowercased (com.spotify.client -> "client").
NP_APPS = {"com.netease.163music": "netease",
           "com.apple.Music": "music",
           "com.apple.podcasts": "podcasts",
           "com.google.Chrome": "chrome"}
# Fields whose change makes a push urgent; pos/dur/rate ride along on the next
# one (the board interpolates position between pushes).
NP_KEYS = ("on", "title", "artist", "album", "play", "app", "cover")
NP_MEDIA_CMDS = {"toggle": "toggle-play-pause", "next": "next-track",
                 "prev": "previous-track"}

np_lock = threading.Lock()
np_rt = {"payload": {},      # merged media-control payload (may hold artworkData)
         "msg": None,        # last np message actually sent to the board
         "sent_at": 0.0,
         "thread": None,
         "proc": None,
         "restarts": 0,
         "pos_at": 0.0,      # local clock when elapsedTime was last true
         "art_b64": None,    # last artworkData seen, so we hash each one once
         "art_hash": None,
         "cov_try": {},      # cover hash -> last convert/push attempt
         "miss": {},         # card path -> last np_miss re-push
         "cov_inflight": set(),   # cover hashes a worker is pushing right now
         "cov_done": {},     # cover hash -> when its push landed (np_miss grace)
         "hold_until": 0.0}  # /test/np/push: keep the fake entry on screen until then
_np_cov_q = queue.Queue()
_np_pushed = None            # cover hashes already on the card (covers.json)

def np_bin():
    """Path to the media-control binary, or None when it is not installed."""
    if os.path.exists(MEDIA_CONTROL_BIN):
        return MEDIA_CONTROL_BIN
    return shutil.which("media-control")

# ---- pure functions (unit-tested; no subprocess, no sockets) ----
def np_app_label(bundle_id):
    """com.google.Chrome -> "chrome"; unknown ids -> their last segment."""
    if not bundle_id:
        return ""
    bid = str(bundle_id)
    if bid in NP_APPS:
        return NP_APPS[bid]
    return bid.strip(".").rsplit(".", 1)[-1].lower()

def np_clip(s, n=NP_TEXT_MAX):
    """One line, at most n CHARACTERS (CJK titles are counted per glyph)."""
    if s is None:
        return ""
    return str(s).replace("\n", " ").replace("\r", " ").strip()[:n]

def np_num(v, default=0.0):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):     # NaN / inf from a flaky player
        return default
    return f

def np_merge(state, obj):
    """Apply one `media-control stream` line to the payload we hold."""
    if not isinstance(obj, dict) or obj.get("type") != "data":
        return dict(state or {})
    payload = obj.get("payload")
    if not isinstance(payload, dict):
        return dict(state or {})
    if obj.get("diff"):
        out = dict(state or {})
        out.update(payload)
        return out
    return dict(payload)

def np_from_state(state, cover_path=""):
    """Merged payload -> the host->board np message (no title = no entry)."""
    st = state or {}
    title = np_clip(st.get("title"))
    if not title:
        return {"t": "np", "on": 0}
    return {"t": "np", "on": 1,
            "title": title,
            "artist": np_clip(st.get("artist")),
            "album": np_clip(st.get("album")),
            "pos": round(np_num(st.get("elapsedTime")), 1),
            "dur": round(np_num(st.get("duration")), 1),
            "play": 1 if st.get("playing") else 0,
            "rate": round(np_num(st.get("playbackRate"), 1.0), 2),
            "app": np_app_label(st.get("bundleIdentifier")),
            "cover": cover_path or ""}

def np_cover_for_msg(h, pushed):
    """The card path the np message names — "" until the card really has the
    file. Naming it early (pre 2026-09-22) made the board ask np_miss while
    the first push was still in flight, so every new cover went up twice; the
    np that follows the landed push carries the path and the board picks it
    up (cover is an NP_KEYS field, so that np is never throttled away)."""
    return np_cover_card(h) if h and h in pushed else ""

def np_changed(prev, cur):
    if prev is None or cur is None:
        return True
    return any(prev.get(k) != cur.get(k) for k in NP_KEYS)

def np_should_send(prev, cur, last_sent_at, now, refresh=NP_REFRESH_S):
    """Every visible change goes out at once; while something plays we also
    refresh every `refresh` seconds so the board re-syncs its interpolated
    position. "Nothing is playing" is sent once, on the 1 -> 0 edge."""
    if not isinstance(cur, dict):
        return False
    if prev is None:
        return bool(cur.get("on"))      # a fresh host with nothing playing stays quiet
    if np_changed(prev, cur):
        return True
    if not cur.get("on"):
        return False
    return (now - last_sent_at) >= refresh

def np_advance(state, secs):
    """The payload with elapsedTime moved forward by `secs` of wall clock.
    media-control only speaks when something CHANGES, so a steady playback
    emits no lines at all; the 15 s refresh has to carry the position the
    player is really at, or the board would snap back to a stale one."""
    if not state or not state.get("playing") or secs <= 0:
        return state
    rate = np_num(state.get("playbackRate"), 1.0)
    if rate <= 0:                       # "playing" with no rate: assume 1x
        rate = 1.0
    pos = np_num(state.get("elapsedTime")) + secs * rate
    dur = np_num(state.get("duration"))
    if dur > 0:
        pos = min(pos, dur)
    out = dict(state)
    out["elapsedTime"] = pos
    return out

def np_cover_hash(raw):
    """Cover id = md5 of the artwork's raw (decoded) bytes, first 12 hex."""
    return hashlib.md5(raw).hexdigest()[:12]

def np_cover_card(h):
    return "%s/%s.jpg" % (NP_COVER_CARD, h)

def np_cover_local(h):
    return NP_COVERS_DIR / (h + ".jpg")

# ---- cover pipeline (worker thread: sips + push_file, never the listener) ----
def np_pushed():
    global _np_pushed
    if _np_pushed is None:
        try:
            d = json.loads(NP_COVERS_SEEN.read_text()).get("pushed", {})
            _np_pushed = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            _np_pushed = {}
    return _np_pushed

def np_pushed_add(h):
    with np_lock:
        d = np_pushed()
        d[h] = int(time.time())
        try:
            NP_COVERS_SEEN.parent.mkdir(parents=True, exist_ok=True)
            NP_COVERS_SEEN.write_text(json.dumps({"pushed": d}))
        except OSError as e:
            log("np cover: covers.json write failed:", e)

def np_cover_make(h, raw, mime=""):
    """Any artwork bytes -> 200x200 baseline JPEG in ~/.agentpet/covers."""
    out = np_cover_local(h)
    if out.exists() and out.stat().st_size > 0:
        return out
    NP_COVERS_DIR.mkdir(parents=True, exist_ok=True)
    ext = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
           "image/tiff": ".tiff", "image/heic": ".heic"}.get((mime or "").lower(), ".bin")
    src = NP_COVERS_DIR / ("tmp-" + h + ext)
    try:
        src.write_bytes(raw)
        r = subprocess.run(["sips", "-z", "200", "200", "-s", "format", "jpeg",
                            "-s", "formatOptions", "70", str(src), "--out", str(out)],
                           capture_output=True, timeout=20)
        if r.returncode != 0 or not out.exists():
            log("np cover: sips failed:", r.stderr.decode(errors="replace").strip()[:120])
            return None
    except (OSError, subprocess.SubprocessError) as e:
        log("np cover: convert error:", e)
        return None
    finally:
        try:
            src.unlink()
        except OSError:
            pass
    log("np cover: %s.jpg %d B" % (h, out.stat().st_size))
    return out

def np_cover_push(h, force=False, wait=None):
    """Ship one prepared cover to the card. TCP only (fbeg/fdat is a TCP path).
    wait: see push_file — the worker and np_miss pass a bound, audio does not."""
    local = np_cover_local(h)
    if not local.exists():
        return {"ok": False, "why": "no local cover " + h}
    if not force and h in np_pushed():      # covers.json: the card already has it
        return {"ok": True, "skipped": "already pushed", "dst": np_cover_card(h)}
    with lock:
        has_tcp = bool(boards)
    if not has_tcp:
        return {"ok": False, "why": "no TCP board"}
    r = push_file(str(local), np_cover_card(h), wait=wait)
    if r.get("ok"):
        np_pushed_add(h)
        np_rt["cov_done"][h] = time.time()
    return r

def np_board_settled():
    """False right after a hello: the board has just booted (an OTA reboot,
    say) and is still bringing its card up — covers can wait."""
    return (time.time() - board_hello_at) >= NP_HELLO_SETTLE

def np_cover_job(h, raw, mime):
    """Worker thread: convert, then queue behind every other card transfer.
    Converting is always safe; only the push waits."""
    local = np_cover_local(h)
    if not local.exists() and raw:
        np_cover_make(h, raw, mime)
    if not local.exists() or h in np_pushed():
        return
    end = time.time() + 60
    while not np_board_settled() and time.time() < end:
        time.sleep(0.5)
    np_rt["cov_inflight"].add(h)            # np_miss for this hash: let it land
    try:
        r = {}
        for i, pause in enumerate((0,) + NP_COVER_RETRY):
            if pause:
                time.sleep(pause)
            r = np_cover_push(h, wait=NP_PUSH_WAIT)   # bounded: an OTA / audio push owns the card for minutes
            if r.get("ok") or not r.get("busy"):
                break
            log("np cover: card busy, retry %d for %s in %ss" % (i + 1, h, NP_COVER_RETRY[min(i, len(NP_COVER_RETRY) - 1)]))
        if not r.get("ok"):
            log("np cover: push %s: %s" % (h, r.get("why")))
    finally:
        np_rt["cov_inflight"].discard(h)

def np_cover_worker():
    while True:
        h, raw, mime = _np_cov_q.get()
        try:
            np_cover_job(h, raw, mime)
        except Exception as e:                      # a bad cover must never kill the worker
            log("np cover worker:", e)

def np_artwork(payload):
    """(hash, raw bytes or None) for this payload's artwork. The bytes come
    back only the first time an artworkData string is seen, so a stream that
    repeats the same 200 KB of base64 is hashed once."""
    b64 = payload.get("artworkData")
    if not isinstance(b64, str) or not b64:
        return "", None
    if b64 == np_rt["art_b64"]:
        return np_rt["art_hash"] or "", None
    try:
        raw = base64.b64decode(b64)
    except Exception as e:
        log("np: artworkData is not base64:", e)
        return "", None
    h = np_cover_hash(raw)
    np_rt["art_b64"], np_rt["art_hash"] = b64, h
    return h, raw

def np_miss(card_path):
    """Board says a cover file is missing from the card: push it again."""
    path = str(card_path or "")
    h = path.rsplit("/", 1)[-1]
    if h.endswith(".jpg"):
        h = h[:-4]
    if not h:
        return {"ok": False, "why": "no path"}
    now = time.time()
    # The first push is still on its way, or landed a moment ago: the board
    # clears its own placeholder when the file arrives (sdFileArrived), so a
    # second copy would only cost 13 KB and 0.7 s of the card (39 times in
    # the log before 2026-09-22).
    if h in np_rt["cov_inflight"]:
        log("np_miss:", path, "in flight, letting it land")
        return {"ok": False, "why": "in flight"}
    if now - np_rt["cov_done"].get(h, 0.0) < NP_MISS_GRACE:
        log("np_miss:", path, "just landed")
        return {"ok": False, "why": "just landed"}
    if now - np_rt["miss"].get(path, 0.0) < NP_MISS_COOLDOWN:
        return {"ok": False, "why": "cooldown"}
    np_rt["miss"][path] = now
    r = np_cover_push(h, force=True, wait=NP_MISS_WAIT)
    log("np_miss:", path, r.get("ok") and "re-pushed" or r.get("why"))
    return r

# ---- listener ----
np_send_lock = threading.Lock()      # the stream thread and the ticker both push

def np_apply_state():
    """Current payload -> maybe a push. Called per stream line and per tick."""
    if not NOW_PLAYING:              # a tick already in flight when the switch went off
        return None
    now = time.time()
    with np_lock:
        payload = dict(np_rt["payload"])
        pos_at = np_rt["pos_at"]
    with np_send_lock:
        if now < np_rt["hold_until"]:      # /test/np/push owns the board for a while
            return None
        h, raw = np_artwork(payload)
        cover = np_cover_for_msg(h, np_pushed())
        if h and (raw is not None or not np_cover_local(h).exists() or h not in np_pushed()):
            if now - np_rt["cov_try"].get(h, 0.0) > NP_COVER_TRY:
                np_rt["cov_try"][h] = now
                _np_cov_q.put((h, raw, payload.get("artworkMimeType", "")))
        msg = np_from_state(np_advance(payload, now - pos_at), cover)
        prev = np_rt["msg"]
        if not np_should_send(prev, msg, np_rt["sent_at"], now):
            return None
        if np_changed(prev, msg):
            if msg.get("on"):
                log("np -> %s(%s) play=%d" % (msg["title"][:40], msg["app"], msg["play"]))
            else:
                log("np -> nothing playing")
        np_send(msg)
    return msg

def np_send(msg):
    push_board(msg)
    with np_lock:
        np_rt["msg"] = msg
        np_rt["sent_at"] = time.time()

def np_line(line):
    try:
        obj = json.loads(line)
    except ValueError:
        return
    with np_lock:
        np_rt["payload"] = np_merge(np_rt["payload"], obj)
        np_rt["pos_at"] = time.time()      # elapsedTime was true as of now
    np_apply_state()

def np_tick_loop():
    """media-control is silent while a track just plays, so nothing would
    re-check the throttle; this is what makes the 15 s refresh happen."""
    try:
        while NOW_PLAYING:
            time.sleep(2.0)
            try:
                if np_rt["payload"]:
                    np_apply_state()
            except Exception as e:
                log("np tick:", e)
    finally:
        np_rt["ticker"] = False

def np_loop():
    """`media-control stream` forever: read lines, restart with backoff."""
    binp = np_bin()
    if not binp:
        log("now playing: media-control not installed "
            "(brew tap ungive/media-control && brew install media-control) — feature off")
        return
    log("now playing: watching", binp)
    tries = 0
    while NOW_PLAYING:
        started = time.time()
        try:
            p = subprocess.Popen([binp, "stream"], stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, bufsize=1,
                                 universal_newlines=True)
        except OSError as e:
            log("now playing: cannot start stream:", e)
            return
        np_rt["proc"] = p
        try:
            for line in p.stdout:
                if not NOW_PLAYING:
                    break
                line = line.strip()
                if line:
                    np_line(line)
        except (OSError, ValueError) as e:
            log("now playing: stream read error:", e)
        finally:
            np_rt["proc"] = None
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                pass
        if not NOW_PLAYING:
            break
        if time.time() - started > 60:              # it ran fine, this was a one-off
            tries = 0
        delay = NP_BACKOFF[min(tries, len(NP_BACKOFF) - 1)]
        tries += 1
        np_rt["restarts"] += 1
        log("now playing: stream exited, restarting in %d s" % delay)
        end = time.time() + delay
        while NOW_PLAYING and time.time() < end:
            time.sleep(0.25)
    log("now playing: listener stopped")

def np_start():
    th = np_rt["thread"]
    if th is not None and th.is_alive():
        return
    if not np_rt.get("worker"):
        np_rt["worker"] = True
        threading.Thread(target=np_cover_worker, daemon=True).start()
    np_rt["thread"] = th = threading.Thread(target=np_loop, daemon=True)
    th.start()
    if not np_rt.get("ticker"):
        np_rt["ticker"] = True
        threading.Thread(target=np_tick_loop, daemon=True).start()

def np_disable():
    """Switch off: kill the stream and tell the board there is nothing."""
    p = np_rt["proc"]
    if p is not None:
        try:
            p.terminate()
        except Exception:
            pass
    with np_lock:
        np_rt["payload"] = {}
        np_rt["pos_at"] = 0.0
        np_rt["art_b64"] = np_rt["art_hash"] = None
    np_send({"t": "np", "on": 0})
    log("now playing: disabled")

def np_apply():
    """Called at boot and after every settings save; only the on->off edge
    tells the board to clear the page."""
    was = np_rt.get("applied")
    np_rt["applied"] = NOW_PLAYING
    if NOW_PLAYING:
        np_start()
    elif was:
        np_disable()

def np_hello_msg():
    """What to hand a board that just said hello (None = nothing playing)."""
    if not NOW_PLAYING:
        return None
    with np_lock:
        payload = dict(np_rt["payload"])
        pos_at = np_rt["pos_at"]
    h = np_rt["art_hash"] if payload.get("artworkData") else ""
    msg = np_from_state(np_advance(payload, time.time() - pos_at),
                        np_cover_card(h) if h else "")
    return msg if msg.get("on") else None

def np_brief():
    m = np_rt["msg"] or {}
    return {"on": m.get("on", 0), "title": m.get("title", ""), "app": m.get("app", "")}

def np_snapshot():
    now = time.time()
    with np_lock:
        st = dict(np_rt["payload"])
        pos_at = np_rt["pos_at"]
    art = st.pop("artworkData", None)
    if art is not None:
        st["artworkData_len"] = len(art)
    th = np_rt["thread"]
    return {"enabled": NOW_PLAYING, "bin": np_bin(), "state": st,
            "pos_now": round(np_num(np_advance(st, now - pos_at).get("elapsedTime")), 1),
            "last_msg": np_rt["msg"], "last_sent_at": round(np_rt["sent_at"], 3),
            "sent_ago_s": round(now - np_rt["sent_at"], 1) if np_rt["sent_at"] else None,
            "thread_alive": bool(th is not None and th.is_alive()),
            "streaming": np_rt["proc"] is not None,
            "hold_s": max(0.0, round(np_rt["hold_until"] - now, 1)),
            "restarts": np_rt["restarts"], "covers_pushed": len(np_pushed())}

def media_cmd(cmd):
    """Board -> the Mac's player: toggle / next / prev."""
    arg = NP_MEDIA_CMDS.get(cmd)
    if not arg:
        log("media: unknown cmd", repr(cmd))
        return {"ok": False, "why": "unknown cmd %r" % (cmd,)}
    binp = np_bin()
    if not binp:
        return {"ok": False, "why": "media-control not installed"}
    try:
        r = subprocess.run([binp, arg], capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as e:
        log("media:", cmd, "failed:", e)
        return {"ok": False, "cmd": cmd, "arg": arg, "why": str(e)}
    err = r.stderr.decode(errors="replace").strip()[:120]
    log("media:", cmd, "->", arg, "rc=%d" % r.returncode, err)
    return {"ok": r.returncode == 0, "cmd": cmd, "arg": arg,
            "rc": r.returncode, "err": err}

# ------------------------------------------------------------------ audio onto the card
# "听什么": paste a podcast RSS, an audio link or a local file and the host
# downloads it, transcodes to the board's 16 kHz mono, packs it into the .ima
# block container and ships it to the microSD, where player.cpp finds it.
#   /audio/add?url=<RSS or audio URL>[&n=1]   /audio/add?file=/abs/path
#   /audio/status   /audio/list   /audio/rm?id=<name>
# One worker thread does the whole chain, so the HTTP handler and the board
# socket never block on a 40 MB transfer. Card layout is name-based:
#   /agentpet/audio/<YYYYMMDD>-<hash8>.ima   + the same name .json sidecar
# (the board only lists entries that have a sidecar, so the sidecar goes last
# on disk but FIRST on the card — an .ima without one is simply invisible).
AUDIO_DIR = Path.home() / ".agentpet" / "audio"
AUDIO_SRC_DIR = AUDIO_DIR / "src"                  # downloads + the intermediate wav
AUDIO_CARD = "/agentpet/audio"
AUDIO_PUSHED = AUDIO_DIR / "pushed.json"           # name -> {at, bytes}: what the card already has
AUDIO_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) AgentTouch/1.0")
AUDIO_MAX_N = 5                                    # episodes per /audio/add
AUDIO_OPEN_TIMEOUT = 30                            # connect + first byte
AUDIO_FEED_MAX = 8 * 1024 * 1024                   # a feed is text; refuse a "feed" that is a movie
AUDIO_COVER_MAX = 8 * 1024 * 1024
AUDIO_CONVERT_TIMEOUT = 1800                       # afconvert on a 3-hour file still lands well inside this
AUDIO_EXTS = (".mp3", ".m4a", ".m4b", ".mp4", ".aac", ".wav", ".aif", ".aiff",
              ".caf", ".flac", ".ogg", ".opus", ".wma")
ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"

au_q = queue.Queue()
au_lock = threading.Lock()
au_rt = {"job": None,        # the episode being worked on right now
         "queue": [],        # short descriptions of what is waiting
         "done": deque(maxlen=12),
         "seq": 0,
         "worker": None}
_au_pushed = None

# ---- pure helpers (unit-tested) ----
def au_hash8(s):
    return hashlib.md5(str(s).encode("utf-8", "replace")).hexdigest()[:8]

def au_pub_date(text, today=None):
    """RFC-822 pubDate -> ("YYYY-MM-DD", epoch). Anything unparseable is today."""
    if text:
        try:
            dt = email.utils.parsedate_to_datetime(str(text).strip())
            if dt is not None:
                return dt.strftime("%Y-%m-%d"), dt.timestamp()
        except (TypeError, ValueError, OverflowError):
            pass
    d = today or datetime.date.today()
    return d.strftime("%Y-%m-%d"), 0.0

def au_duration(text):
    """itunes:duration -> whole seconds. "01:22:31", "22:31" and "4951" all work."""
    s = str(text or "").strip()
    if not s:
        return 0
    try:
        if ":" in s:
            secs = 0.0
            for part in s.split(":"):
                secs = secs * 60 + float(part or 0)
            return int(round(secs))
        return int(round(float(s)))
    except ValueError:
        return 0

def au_name(src, date_str):
    """Card/file name: YYYYMMDD-hash8, so the board's lexical order is
    chronological and re-adding the same URL lands on the same file."""
    return "%s-%s" % (str(date_str or "").replace("-", "")[:8] or
                      datetime.date.today().strftime("%Y%m%d"), au_hash8(src))

def au_ext(url, ctype=""):
    """Audio extension for a URL / content type; ".bin" when nothing says."""
    name = str(url or "").split("?", 1)[0].split("#", 1)[0].rsplit("/", 1)[-1].lower()
    for e in AUDIO_EXTS:
        if name.endswith(e):
            return e
    ct = str(ctype or "").split(";", 1)[0].strip().lower()
    return {"audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a",
            "audio/x-m4a": ".m4a", "audio/aac": ".aac", "audio/wav": ".wav",
            "audio/x-wav": ".wav", "audio/wave": ".wav", "audio/flac": ".flac",
            "audio/ogg": ".ogg", "video/mp4": ".mp4"}.get(ct, ".bin")

def au_looks_rss(head, ctype=""):
    """Is this response a feed rather than the audio itself?"""
    ct = str(ctype or "").lower()
    if ct.startswith("audio/") or ct.startswith("video/"):
        return False
    if "xml" in ct or "rss" in ct:
        return True
    probe = bytes(head or b"")[:400].lstrip()
    return probe.startswith(b"<?xml") or probe.startswith(b"<rss") or probe.startswith(b"<feed")

def au_rss_parse(data, n=1):
    """Podcast feed -> {"show", "cover", "items":[{title,url,dur,date,ts,cover}]}.
    Newest first: RSS convention, but we sort on pubDate so an oldest-first
    feed still hands back the latest episodes."""
    root = ET.fromstring(data)
    ch = root if root.tag == "channel" else root.find("channel")
    if ch is None:
        raise ValueError("no <channel> in this feed")
    show = (ch.findtext("title") or "").strip()
    cover = ""
    img = ch.find(ITUNES + "image")
    if img is not None:
        cover = (img.get("href") or "").strip()
    if not cover:
        cover = (ch.findtext("image/url") or "").strip()
    items = []
    for i, it in enumerate(ch.findall("item")):
        enc = it.find("enclosure")
        url = (enc.get("url") if enc is not None else "") or ""
        url = url.strip()
        if not url:
            continue
        date, ts = au_pub_date(it.findtext("pubDate"))
        eimg = it.find(ITUNES + "image")
        items.append({"title": (it.findtext("title") or "").strip(),
                      "url": url,
                      "dur": au_duration(it.findtext(ITUNES + "duration")),
                      "date": date, "ts": ts,
                      "cover": ((eimg.get("href") or "").strip() if eimg is not None else "") or cover,
                      "i": i})
    items.sort(key=lambda e: (-e["ts"], e["i"]))
    for e in items:
        e.pop("i", None)
    return {"show": show, "cover": cover,
            "items": items if not n or int(n) <= 0 else items[:int(n)]}

def au_sidecar(ep, dur, cover_card):
    """The .json the board reads next to an .ima."""
    return {"title": np_clip(ep.get("title") or ep.get("name") or "", 120),
            "show": np_clip(ep.get("show") or "", 60),
            "dur": int(dur or 0),
            "date": ep.get("date") or datetime.date.today().strftime("%Y-%m-%d"),
            "cover": cover_card or ""}

# ---- bookkeeping ----
def au_pushed():
    global _au_pushed
    if _au_pushed is None:
        try:
            d = json.loads(AUDIO_PUSHED.read_text())
            _au_pushed = d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            _au_pushed = {}
    return _au_pushed

def au_pushed_write():
    try:
        AUDIO_DIR.mkdir(parents=True, exist_ok=True)
        AUDIO_PUSHED.write_text(json.dumps(au_pushed(), ensure_ascii=False))
    except OSError as e:
        log("audio: pushed.json write failed:", e)

def au_step(step, **kw):
    """Update the live job so /audio/status (and the settings page) can watch.
    Progress fields are cleared unless this call sets them, or the download's
    last percentage would still be on screen while the card push starts."""
    with au_lock:
        job = au_rt["job"]
        if job is not None:
            job["step"] = step
            for k in ("pct", "bytes", "total", "part"):
                job[k] = kw.pop(k, None)
            job.update(kw)
            job["at"] = time.time()

def au_local(name, ext=".ima"):
    return AUDIO_DIR / (name + ext)

def au_card(name, ext=".ima"):
    return "%s/%s%s" % (AUDIO_CARD, name, ext)

def au_has_tcp():
    with lock:
        return bool(boards)

# ---- network ----
def au_get(url, cap, what="file"):
    """Small GET (feed, cover art) -> (bytes, content type). Capped: a feed URL
    that turns out to be a 200 MB file must not eat the host's memory."""
    req = urllib.request.Request(url, headers={"User-Agent": AUDIO_UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=AUDIO_OPEN_TIMEOUT) as r:
        ctype = r.headers.get("Content-Type", "")
        data = r.read(cap + 1)
    if len(data) > cap:
        raise ValueError("%s is larger than %d B" % (what, cap))
    return data, ctype

def au_download(url, dest):
    """Stream a big file to disk, reporting progress into the live job."""
    req = urllib.request.Request(url, headers={"User-Agent": AUDIO_UA, "Accept": "*/*"})
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    t0, got, last = time.time(), 0, 0.0
    with urllib.request.urlopen(req, timeout=AUDIO_OPEN_TIMEOUT) as r:
        total = int(r.headers.get("Content-Length") or 0)
        au_step("download", bytes=0, total=total, pct=0)
        with open(tmp, "wb") as f:
            while True:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                now = time.time()
                if now - last > 0.5:
                    last = now
                    au_step("download", bytes=got, total=total,
                            pct=int(got * 100 / total) if total else 0)
    tmp.replace(dest)
    ms = int((time.time() - t0) * 1000)
    log("audio: downloaded %d B in %d ms -> %s" % (got, ms, dest.name))
    return {"bytes": got, "ms": ms}

def au_convert(src, wav):
    """afconvert -> 16 kHz mono s16le WAV, the only format the board plays."""
    t0 = time.time()
    r = subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1",
                        str(src), str(wav)],
                       capture_output=True, timeout=AUDIO_CONVERT_TIMEOUT)
    if r.returncode != 0 or not Path(wav).exists():
        raise RuntimeError("afconvert: " + r.stderr.decode(errors="replace").strip()[:200])
    return {"ms": int((time.time() - t0) * 1000), "bytes": Path(wav).stat().st_size}

def au_cover(url):
    """Episode/show art -> the card path for it (converted, not yet pushed)."""
    if not url:
        return ""
    try:
        raw, ctype = au_get(url, AUDIO_COVER_MAX, "cover")
    except Exception as e:
        log("audio: cover fetch failed:", e)
        return ""
    h = np_cover_hash(raw)
    if not np_cover_make(h, raw, ctype.split(";", 1)[0].strip()):
        return ""
    return np_cover_card(h)

# ---- the card ----
def au_push_one(name, cover_card=""):
    """Sidecar first, then the audio, then tell the board to rescan. The board
    ignores an .ima with no .json, so this order never shows a half file."""
    ima, side = au_local(name, ".ima"), au_local(name, ".json")
    if not ima.exists() or not side.exists():
        return {"ok": False, "why": "missing local files for " + name}
    if not au_has_tcp():
        return {"ok": False, "why": "no TCP board (Wi-Fi link needed)"}
    if cover_card:
        h = cover_card.rsplit("/", 1)[-1][:-4]
        r = np_cover_push(h)
        if not r.get("ok"):
            log("audio: cover push:", r.get("why"))
    au_step("push", pct=0, part="json")
    r = push_file(str(side), au_card(name, ".json"))
    if not r.get("ok"):
        return r
    total = ima.stat().st_size
    au_step("push", pct=0, part="ima", total=total, bytes=0)
    t0 = time.time()
    r = push_file(str(ima), au_card(name, ".ima"),
                  progress=lambda sent, tot: au_step("push", part="ima", total=tot,
                                                     bytes=sent, pct=int(sent * 100 / max(tot, 1))))
    if not r.get("ok"):
        board_rm(au_card(name, ".json"))          # no orphan sidecar on the card
        return r
    au_pushed()[name] = {"at": int(time.time()), "bytes": r.get("bytes", 0),
                         "ms": int((time.time() - t0) * 1000)}
    au_pushed_write()
    push_board({"t": "pl"})
    au_step("push", pct=100)
    return r

def au_push_pending(delay=0.0):
    """Everything encoded but not on the card yet (the board was offline when
    it was added, a push failed, or the host was restarted mid-transfer — a
    40 MB episode is ~15 minutes of Wi-Fi and does not survive a kickstart).
    Called on every hello and after each job; shows up in /audio/status like
    any other job so the settings page is not blind while it runs."""
    if delay:
        time.sleep(delay)
    out = []
    for name in au_names():
        if name in au_pushed():
            continue
        side = {}
        try:
            side = json.loads(au_local(name, ".json").read_text())
        except (OSError, ValueError):
            pass
        with au_lock:
            if au_rt["job"] is not None:           # a real job owns the card right now
                return out
            au_rt["job"] = {"name": name, "title": side.get("title", ""),
                            "show": side.get("show", ""), "step": "push",
                            "resume": True, "t0": time.time(), "at": time.time()}
        try:
            r = au_push_one(name, side.get("cover", ""))
        finally:
            with au_lock:
                au_rt["job"] = None
        out.append({"name": name, **{k: r.get(k) for k in ("ok", "why", "ms", "kbps")}})
        if not r.get("ok"):
            break                                  # board gone: stop, hello will call us again
    if out:
        log("audio: pending push:", out)
    return out

def au_names():
    """Local episodes, chronological (= the board's own order)."""
    try:
        return sorted(p.stem for p in AUDIO_DIR.glob("*.ima")
                      if au_local(p.stem, ".json").exists())
    except OSError:
        return []

def au_list():
    out = []
    for name in au_names():
        ima = au_local(name, ".ima")
        side = {}
        try:
            side = json.loads(au_local(name, ".json").read_text())
        except (OSError, ValueError):
            pass
        try:
            size = ima.stat().st_size
        except OSError:
            size = 0
        out.append({"name": name, "title": side.get("title", ""),
                    "show": side.get("show", ""), "dur": side.get("dur", 0),
                    "date": side.get("date", ""), "cover": side.get("cover", ""),
                    "size": size, "pushed": name in au_pushed()})
    return out

def au_rm(name):
    """Delete one episode here and on the card. The id is a bare episode name:
    a path is refused outright, never quietly reduced to its last segment."""
    name = str(name or "").strip()
    if name.endswith(".ima") or name.endswith(".json"):
        name = name.rsplit(".", 1)[0]
    if not name or not re.match(r"^[0-9A-Za-z][0-9A-Za-z._-]*$", name) or ".." in name:
        return {"ok": False, "why": "bad id %r" % str(name)[:40]}
    gone = []
    for ext in (".ima", ".json"):
        p = au_local(name, ext)
        if p.exists():
            try:
                p.unlink()
                gone.append(p.name)
            except OSError as e:
                return {"ok": False, "why": str(e)}
    card = {}
    if gone and au_has_tcp():                      # nothing here means nothing of ours there
        for ext in (".ima", ".json"):
            card[ext] = board_rm(au_card(name, ext)).get("ok", False)
        push_board({"t": "pl"})
    if au_pushed().pop(name, None) is not None:
        au_pushed_write()
    if not gone:
        return {"ok": False, "why": "no such episode " + name, "name": name}
    return {"ok": True, "name": name, "local": gone, "card": card}

# ---- the worker ----
def au_episode(ep):
    """One episode end to end. ep: {src, title, show, date, dur, cover, kind}."""
    src, kind = ep["src"], ep.get("kind", "url")
    name = au_name(src, ep.get("date"))
    ep["name"] = name
    with au_lock:
        au_rt["job"] = {"name": name, "title": ep.get("title", ""),
                        "show": ep.get("show", ""), "step": "start",
                        "t0": time.time(), "at": time.time()}
    ima = au_local(name, ".ima")
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    AUDIO_SRC_DIR.mkdir(parents=True, exist_ok=True)
    timing = {}
    if ima.exists() and au_local(name, ".json").exists():
        au_step("ready", note="already encoded")
    else:
        if kind == "file":
            media = Path(src)
            if not media.exists():
                raise ValueError("no such file: " + src)
        else:
            media = AUDIO_SRC_DIR / (name + au_ext(src, ep.get("ctype", "")))
            if not media.exists():                 # a retry reuses what already came down
                got = au_download(src, media)
                timing["download_ms"], timing["src_bytes"] = got["ms"], got["bytes"]
        wav = AUDIO_SRC_DIR / (name + ".wav")
        au_step("convert")
        c = au_convert(media, wav)
        timing["convert_ms"] = c["ms"]
        au_step("encode")
        t0 = time.time()
        packed = ima_pack_file(wav, ima)
        timing["encode_ms"] = int((time.time() - t0) * 1000)
        timing["blocks"] = packed["blocks"]
        timing["ima_bytes"] = packed["bytes"]
        au_step("cover")
        cover_card = au_cover(ep.get("cover", ""))
        dur = int(ep.get("dur") or 0) or packed["secs"]
        au_local(name, ".json").write_text(
            json.dumps(au_sidecar(ep, dur, cover_card), ensure_ascii=False))
        for p in (wav, media if kind != "file" else None):
            if p is not None:
                try:
                    p.unlink()
                except OSError:
                    pass
    side = {}
    try:
        side = json.loads(au_local(name, ".json").read_text())
    except (OSError, ValueError):
        pass
    au_step("push")
    r = au_push_one(name, side.get("cover", ""))
    timing["push_ms"] = r.get("ms")
    timing["push_kbps"] = r.get("kbps")
    done = {"name": name, "title": side.get("title", ""), "show": side.get("show", ""),
            "dur": side.get("dur", 0), "ok": bool(r.get("ok")),
            "pushed": bool(r.get("ok")), "why": r.get("why"),
            "at": int(time.time()), **timing}
    with au_lock:
        au_rt["job"] = None
        au_rt["done"].appendleft(done)
    log("audio: done", json.dumps(done, ensure_ascii=False))
    return done

def au_resolve(task):
    """An /audio/add spec -> the episodes to work on (feeds fan out)."""
    if task.get("kind") == "file":
        p = Path(task["src"]).expanduser()
        if not p.exists():
            raise ValueError("no such file: " + str(p))
        d = datetime.date.today().strftime("%Y-%m-%d")
        return [{"kind": "file", "src": str(p), "title": p.stem, "show": tr("本地文件", "Local file"),
                 "date": d, "dur": 0, "cover": ""}]
    url = task["src"]
    head, ctype = b"", ""
    if not url.lower().split("?", 1)[0].endswith(AUDIO_EXTS):
        head, ctype = au_get(url, AUDIO_FEED_MAX, "feed")   # could be a feed; look before downloading
    if head and au_looks_rss(head, ctype):
        feed = au_rss_parse(head, task.get("n", 1))
        if not feed["items"]:
            raise ValueError("feed has no <enclosure> to play")
        return [{"kind": "url", "src": e["url"], "title": e["title"],
                 "show": feed["show"], "date": e["date"], "dur": e["dur"],
                 "cover": e["cover"]} for e in feed["items"]]
    d = datetime.date.today().strftime("%Y-%m-%d")
    title = unquote(url.split("?", 1)[0].rsplit("/", 1)[-1]) or url
    return [{"kind": "url", "src": url, "title": title, "show": "", "date": d,
             "dur": 0, "cover": "", "ctype": ctype}]

def au_worker():
    while True:
        task = au_q.get()
        try:
            with au_lock:
                au_rt["queue"] = [q for q in au_rt["queue"] if q["seq"] != task["seq"]]
            if task.get("t") == "ep":
                au_episode(task["ep"])
            else:
                eps = au_resolve(task)
                for ep in eps:
                    au_enqueue({"t": "ep", "ep": ep,
                                "desc": "%s · %s" % (ep.get("title", "")[:40], ep.get("show", ""))})
                log("audio: queued %d episode(s) from %s" % (len(eps), task["src"][:70]))
        except Exception as e:                      # one bad feed must not kill the worker
            log("audio: %s failed: %s" % (task.get("desc", task.get("src", "?"))[:60], e))
            with au_lock:
                au_rt["job"] = None
                au_rt["done"].appendleft({"name": task.get("desc", ""), "ok": False,
                                          "why": str(e)[:200], "at": int(time.time())})
        finally:
            au_q.task_done()

def au_enqueue(task):
    with au_lock:
        au_rt["seq"] += 1
        task["seq"] = au_rt["seq"]
        au_rt["queue"].append({"seq": task["seq"], "desc": task.get("desc", task.get("src", ""))})
        if au_rt["worker"] is None or not au_rt["worker"].is_alive():
            au_rt["worker"] = threading.Thread(target=au_worker, daemon=True)
            au_rt["worker"].start()
    au_q.put(task)
    return task["seq"]

def au_add(url="", file="", n=1):
    """Endpoint side of /audio/add: validate, queue, answer at once. Fetching
    the feed happens on the worker so a slow feed cannot hang the settings page."""
    try:
        n = max(1, min(AUDIO_MAX_N, int(n)))
    except (TypeError, ValueError):
        n = 1
    if file:
        p = Path(file).expanduser()
        if not p.is_absolute() or not p.exists():
            return {"ok": False, "why": "need an existing absolute path"}
        seq = au_enqueue({"t": "add", "kind": "file", "src": str(p), "desc": p.name})
    else:
        url = str(url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            return {"ok": False, "why": "need an http(s) URL or an absolute file path"}
        seq = au_enqueue({"t": "add", "kind": "url", "src": url, "n": n, "desc": url[:80]})
    return {"ok": True, "seq": seq, "n": n, "queued": au_status()["queue"]}

def au_status():
    with au_lock:
        job = dict(au_rt["job"]) if au_rt["job"] else None
        q = [dict(x) for x in au_rt["queue"]]
        done = [dict(x) for x in au_rt["done"]]
    if job:
        job["elapsed_s"] = round(time.time() - job["t0"], 1)
    return {"job": job, "queue": q, "busy": bool(job or q), "done": done,
            "tcp": au_has_tcp(), "pending": [n for n in au_names() if n not in au_pushed()]}

# ------------------------------------------------------------------ settings page
# http://127.0.0.1:8788/settings — the third tier of the settings hierarchy
# (gesture-direct on board / board settings card / Mac). Set-once mechanism
# prefs live here + a debug button grid; board-NVS items (pickup, volume) are
# live-pushed via the /test endpoints, config.json items go through
# /settings/save (merge + reload + cfg push, no host restart needed).
# Settings page strings: one key, two languages. Injected into
# the page as JS `T`; test_host.py fails if the two key sets ever differ or
# the page uses a key that is not here. {0} {1} ... are tf() placeholders.
# Values under data-th keys are trusted HTML (they carry <code>).
SETTINGS_T = {
 "zh": {
  "title": "AgentTouch 设置", "lang_saved": "语言：中文",
  "loading": "读取中", "loading_dots": "读取中……", "board": "板子",
  "page_overview": "总览", "pdesc_overview": "host 与板子此刻好不好，五个席位在干什么。",
  "page_seats": "席位", "pdesc_seats": "每一席对应的 Mac app 与按键。留空 = 这一席没有 app / 不按键。",
  "page_sound": "声音", "pdesc_sound": "宠物开口的方式与音量。",
  "page_mic": "语音听写", "pdesc_mic": "按住板子右键说话时，声音从哪来、走哪条路。",
  "page_pickup": "拿起时显示", "pdesc_pickup": "把板子拿起来那一刻显示哪一页。",
  "page_behavior": "行为", "pdesc_behavior": "板子与 Mac 前台互相跟随的规则。",
  "page_np": "正在播放", "pdesc_np": "把 Mac 正在播的东西显示在板上。",
  "page_audio": "听什么", "pdesc_audio": "给板上播放器加播客或音频，转码后推上卡。",
  "page_host": "host", "pdesc_host": "常驻服务本身：重启、日志、调试按钮。",
  "page_wifi": "板子", "pdesc_wifi": "板子跟着哪台 Mac，能连哪些 Wi-Fi（它会自动连信号最强的那个）。",
  "ow_title": "跟着哪台 Mac", "ow_mine": "跟着这台（{0}）", "ow_standby": "跟着「{0}」。这台在待机，不抢蓝牙",
  "ow_legacy": "板子固件还不支持认领，升级固件后可用", "ow_unknown": "还没看到板子",
  "ow_claim": "让板子连这台", "ow_claiming": "请到板子上点绿色的「连接」…", "ow_done": "板子已经跟着这台了",
  "ow_asking": "板子屏幕上在问：点绿色的「连接」确认（还剩 {0} 秒）", "ow_free": "板子还没有主人。点「让板子连这台」，再到板子上点「连接」",
  "ow_why_declined": "板子上点了拒绝", "ow_why_timeout": "30 秒内没在板子上点「连接」", "ow_why_asleep": "板子扣着在睡觉，翻过来再试",
  "ow_why_busy": "板子正在问另一台 Mac，等它答完再试", "ow_why_asking": "板子还在等你点「连接」",
  "ow_fail": "没连上板子：", "ow_bar": "板子现在跟着「{0}」，这台 Mac 在待机。", "ow_q": "让板子改跟这台？「{0}」那边会转为待机。",
  "ow_cap": "板子一次只跟一台 Mac：状态、听写、Wi-Fi 那条线都走那台。换 Mac 时在新的那台点「让板子连这台」，旧的那台自动转待机。主人不在也不会自动换，免得两台来回抢。",
  "wf_now": "现在连着", "wf_cur": "{0} · 信号 {1} dBm · {2}", "wf_none": "没连上 Wi-Fi（只有蓝牙）",
  "wf_offline": "板子不在线：打开板子、等它蓝牙连上再来", "wf_scan": "重新搜索", "wf_scanning": "板子在搜索附近的 Wi-Fi…",
  "wf_fac": "出厂", "wf_seen": "信号 {0} dBm", "wf_unseen": "这里搜不到", "wf_using": "在用", "wf_rm": "删除",
  "wf_add": "添加一个 Wi-Fi", "wf_add_sub": "名字要和路由器里的一字不差（区分大小写）。", "wf_ssid_ph": "Wi-Fi 名称",
  "wf_pass_ph": "密码（没有就留空）", "wf_join": "加入", "wf_saved": "已存进板子，正在连…",
  "wf_rm_q": "让板子忘掉「{0}」？", "wf_rmd": "已删除",
  "wf_5g_q": "这个名字带 5G。板子只能连 2.4 GHz，5G 网络它搜不到。还要加吗？",
  "wf_why_ssid": "名字不对（1–32 个字符）", "wf_why_pass": "密码要 8–63 位，或者留空", "wf_why_factory": "出厂网络不能改也不能删",
  "wf_why_unknown": "板子里没有这个网络", "wf_why_timeout": "板子没回应", "wf_fail": "没读到：",
  "wf_near": "板子在这里搜得到", "wf_near_sub": "点一下填进名字。只列 2.4 GHz（板子只看得见这些）。", "wf_near_none": "还没搜过，点「重新搜索」",
  "sc_t": "扫描本机", "sc_sub": "这台 Mac 上装了哪些 agent、放在哪个席位、状态接好了没有。", "sc_btn": "重新扫描",
  "sc_none": "没扫到认识的 agent", "sc_cn": "国内版", "sc_intl": "国际版", "sc_noseat": "暂无席位",
  "sc_run": "在运行", "sc_inst": "已装", "sc_using": "在用", "sc_use": "用这个", "sc_used": "这一席改用 {0}",
  "sc_cli": "命令行，在终端里用", "sc_how_hooks": "状态：hooks", "sc_how_codex": "状态：通知 + hooks",
  "sc_how_log": "状态：读日志", "sc_how_db": "状态：读数据库", "sc_how_none": "只能看出开着 / 在忙",
  "sc_miss_hooks": "hooks 没装：跑一次 host/setup.sh", "sc_miss_codex": "Codex 的通知或 hooks 没接：跑一次 host/setup.sh",
  "sc_miss_log": "还没有日志：打开它用一次就有", "sc_miss_db": "还没有数据库：打开它用一次就有",
  "ag_btn": "交给我的 agent", "ag_copied": "已复制：贴给你电脑上的任何一个 AI agent 就行",
  "ag_prompt": "请读 http://127.0.0.1:8788/agent-setup ，按里面的步骤把这台 Mac 上的 AI agent 配到 AgentTouch 桌宠上。只用它列出的接口改配置，不要改代码；需要我动手的（系统权限、看板子）停下来叫我。做完按文末的格式告诉我结果。",
  "sc_cap": "板子上的席位目前固定五个。同一家的国内版和国际版都认，两版都装时点「用这个」选切席位时拉哪一个。「暂无席位」的 agent 只是列出来，放进席位要等后面的版本。「交给我的 agent」复制一句话，贴给这台 Mac 上的任何 AI agent，它会照说明替你配好。",
  "wf_cap": "板子只支持 2.4 GHz，连不了 5G；路由器 2.4G 和 5G 同名时没关系，板子自己会用 2.4G。最多记 4 个，加第 5 个会顶掉最早的。密码经蓝牙直接发给板子、只存在板子里，这台 Mac 不保存。",
  "seat_claude": "Claude", "seat_codex": "Codex", "seat_qoder": "Qoder",
  "seat_qoderwork": "千问办公", "seat_forest": "Forest",
  "st_off": "离线", "st_idle": "空闲", "st_working": "干活中", "st_needs_you": "等你", "st_done": "干完了",
  "rsn_scanning": "扫描中", "rsn_bt_off": "Mac 蓝牙已关", "rsn_stuck": "蓝牙栈卡住，host 正在自愈",
  "ov_seats": "席位", "ov_seats_sub": "当前席位带白框", "ov_np": "正在播放",
  "fn_title": "卡上字库", "fn_ok": "{0} 个字库都在卡上", "fn_nocard": "板子没插 microSD 卡：黄历页很多字会显示成方框",
  "fn_wait": "等板子连上 Wi-Fi 再查（字库只能经 Wi-Fi 推）", "fn_checking": "正在查卡上的字库……",
  "fn_baking": "正在用这台 Mac 的系统字体生成 {0} 个字库……", "fn_pushing": "正在推 {0}（第 {1}/{2} 个）· 共 {3}%",
  "fn_nopil": "缺 Pillow，生成不了字库：重跑 host/setup.sh，或 /usr/bin/python3 -m pip install --user pillow",
  "fn_err": "出错：{0}（板子下次经 Wi-Fi 连上会再试）", "fn_redo": "重推", "fn_retry": "再试",
  "fn_q": "重新生成并推送全部 4 个字库？约 16 MB，经 Wi-Fi 要几分钟。", "fn_started": "开始了，进度看这一行", "fn_busy": "正在推，等它推完",
  "ov_cap": "这一页替代 <code>pet status</code>：每 2 秒刷新一次。板子只要 Wi-Fi 或蓝牙有一条就算连着。",
  "link_both": "Wi-Fi + 蓝牙", "link_bt": "蓝牙", "connected": "已连接", "not_connected": "未连接",
  "disc": "断开 {0} s", "bsub": "开机 {0} min · 堆 {1} / {2} KB", "last_rst": "上次复位 {0}",
  "hup": "已跑 {0} min · {1} MB", "fw": "固件 {0}", "n_off": "{0} 席离线",
  "np_off": "已关闭", "np_none": "没有在播的",
  "seats_cap": "「用当前前台」：先点到那个 app 的窗口，再回来点它。CPU 阈值留空只是不写这个键；要真正去掉一席已有的阈值，保存后到 host 页重启一次。皮肤是板端 NVS，点选即生效不走保存；小色点 = 这件正被哪一席穿着。",
  "pr_t": "高级：进程识别规则",
  "pr_sub": "每席 {\"match\": 正则, \"exclude\": 正则或 null}，拿去比对 <code>ps -axo args</code> 整行；正则编不过就整次不保存",
  "no_app_ph": "留空 = 这一席没有 app", "use_front": "用当前前台", "mac_app": "对应的 Mac app",
  "kf_focus_keys": "切过去后按", "kf_approve_keys": "批准键", "kf_reject_keys": "拒绝键",
  "key_ph": "空 = 不按", "cpu_aria": "CPU 阈值", "cpu_lab": "CPU 判「在干活」的阈值",
  "cpu_hint": "留空 = 这一席不看 CPU", "skin": "皮肤", "skin_hint": "点选即写板端；点别家在穿的 = 两家互换",
  "no_app": "— 无 app —", "not_here": "本机未安装", "skin_fail": "没换成", "send_fail": "没发出去：",
  "vs_t": "等你批准 / 干完活的播报", "vs_sub": "只管这两个时刻；求签和其余音效不受影响",
  "vs_chirp": "滴滴", "vs_tts": "晓伊说话",
  "vol": "音量", "vol_sub": "直接写板端 NVS，0 = 静音",
  "say_t": "让宠物说点什么", "say_sub": "晓伊 TTS，要 Wi-Fi 在线", "say_ph": "输入一句……", "play": "播放",
  "mic_src": "声音从哪来", "mic_hint_ok": "按住板子右键说话时",
  "mic_hint_no": "板子上的麦要先编 mic_sink：swiftc -O -o ~/.agentpet/mic_sink host/mic_sink.swift",
  "mic_mac": "Mac 的麦", "mic_board": "板子的麦",
  "ml_t": "听写链路", "ml_sub": "板麦音频走哪条；auto 同蓝牙优先。/test/mic/link 是临时覆盖，下次下发会盖回这里",
  "ml_ble": "蓝牙优先", "ml_tcp": "Wi-Fi 优先", "msd": "虚拟声卡名",
  "mts_t": "松开右键后多等几秒再放开 fn",
  "mts_sub": "管道有延迟，尾巴太短最后几个字会被吞；默认 0.6，实测值看 /test/mic/latency",
  "pk_t": "拿起时显示",
  "pk_sub": "点选即写板端 NVS。follow = 正对页（持握中转面稳 0.9 s 换页、摇几下唤宠）；smart 是它的旧别名；needs_you 一律抢屏，详见 doc/06",
  "ff_t": "切换席位时 Mac 前台跟随", "ff_sub": "停手后拉那一家 app 到前台并聚焦输入框",
  "fb_t": "Mac 切到某家 app 时板子跟着换席位",
  "fb_sub": "只认五家 app；板上刚滑过 5 秒内、host 刚拉过前台 3 秒内、按住语音时都不动",
  "so_t": "显示离线席位",
  "so_sub": "默认不显示：席位点只画在跑的，滑动跳过已退出的 app（全退出就不跳）。刚装好想看齐五席时打开",
  "st_t": "连续工作提醒", "st_sub": "板上喊休息；0 = 关", "minutes": "分钟",
  "fs_t": "板上滑动停手几秒后 Mac 才切前台",
  "fs_sub": "连滑只拉最后停下的那一家；太小会逐格拉，太大切换显得慢。默认 1.5",
  "np_t": "把 Mac 正在播的东西显示在板上",
  "np_sub": "靠 media-control 读系统「正在播放」，没装就自动静默关掉；板上钟表方向左滑 = 播放页",
  "au_ph": "播客 RSS / 音频直链 / 本地绝对路径",
  "au_n1": "最新 1 集", "au_n3": "最新 3 集", "au_n5": "最新 5 集", "add": "加入", "queue": "队列",
  "au_cap": "下载 → 转成 16 kHz 单声道 → 编成板子能放的块文件 → 推到卡上（一小时的节目约 29 MB，走 Wi-Fi 推卡要十来分钟；板子不在线就先存本地，等它连上自动补推）。板上钟表方向右滑 = 播客页。",
  "austep_start": "准备", "austep_ready": "准备", "austep_download": "下载", "austep_convert": "转码",
  "austep_encode": "编码", "austep_cover": "封面", "austep_push": "上卡",
  "dur_s": "{0} 秒", "dur_m": "{0} 分", "dur_hm": "{0} 小时 {1} 分",
  "au_more": "（还有 {0} 项排队）", "au_queued": "排队中 {0} 项",
  "au_pend_tcp": "{0} 集待上卡", "au_pend_wait": "{0} 集等板子连上 Wi-Fi 再上卡",
  "au_lastfail": "上一条失败：{0}", "au_idle": "空闲", "au_idle_off": "空闲（板子没连 Wi-Fi，加进来的先存本地）",
  "au_added": "已加入队列", "au_addfail": "加入失败", "au_rm_q": "删除「{0}」？本地和卡上都会删。",
  "au_rmd": "已删除", "au_rmfail": "删除失败", "au_pushed": "已上卡 ✓", "au_pending": "待上卡",
  "del": "删除", "au_empty": "还没有加过音频。", "local_file": "本地文件",
  "host_t": "host 常驻服务",
  "host_sub": "改完设置立刻生效，只有极少数键（清空 CPU 阈值之类）要重启；板子会断一下自己重连。要彻底停掉用终端 <code>pet off</code>，下次登录仍会自启",
  "restart_btn": "重启 host", "mem": "内存",
  "mem_sub": "rss，每 30 s 一点，从这次启动算起；平线是好的，斜率才是告警",
  "mem_sub_f": "rss，每 {0} s 一点，从这次启动算起（{1} min，{2} 点）；平线是好的，斜率才是告警",
  "mem_aria": "rss 趋势", "mem_now": "现在", "mem_start": "启动时", "mem_hr": "每小时", "mem_thr": "线程",
  "mem_recent": "近 {0} min",
  "log": "日志", "last": "最后", "lines": "行", "refresh": "刷新", "log_unread": "（还没读）",
  "log_empty": "（日志还是空的）", "log_fail": "读不到：",
  "dbg": "调试", "dbg_sub": "直接打板子", "b_faces": "巡演 14 脸", "b_qian": "念签文", "b_alm": "推黄历",
  "b_rep": "推战报", "b_prof": "档案卡", "b_str": "久坐提醒", "b_nudge": "名牌+四灯",
  "restart_q": "重启 host？板子会断一下自己重连。", "restarting": "正在重启……",
  "restarted": "已重启 · {0}", "restart_lost": "没回来，终端 pet status 看一眼", "restart_fail": "重启请求没发出去",
  "front_none": "读不到前台 app", "front_many": "前台叫 {0}，填了第一个", "front_one": "前台：{0}",
  "front_fail": "读不到前台：",
  "save_btn": "保存到 config.json", "dirty": "有改动还没保存",
  "bad_json": "进程识别规则不是合法 JSON，没保存", "not_saved": "没保存",
  "saved": "已保存 {0} 项：{1}", "save_fail": "保存失败：", "no_host": "读不到 host：",
 },
 "en": {
  "title": "AgentTouch Settings", "lang_saved": "Language: English",
  "loading": "Loading", "loading_dots": "Loading…", "board": "Board",
  "page_overview": "Overview", "pdesc_overview": "How the host and the board are doing, and what the five seats are up to.",
  "page_seats": "Seats", "pdesc_seats": "The Mac app and keys for each seat. Leave a field empty for no app or no key.",
  "page_sound": "Sound", "pdesc_sound": "How the pet speaks, and how loud.",
  "page_mic": "Dictation", "pdesc_mic": "Where your voice comes from, and which link it takes, while you hold the board's right key.",
  "page_pickup": "Pickup", "pdesc_pickup": "Which page the board shows the moment you pick it up.",
  "page_behavior": "Behavior", "pdesc_behavior": "How the board and the Mac's front app follow each other.",
  "page_np": "Now Playing", "pdesc_np": "Show what the Mac is playing on the board.",
  "page_audio": "Listen", "pdesc_audio": "Add podcasts or audio to the board's player. They're converted and copied to the card.",
  "page_host": "Host", "pdesc_host": "The background service itself: restart, log, and debug buttons.",
  "page_wifi": "Board", "pdesc_wifi": "Which Mac the board follows, and the Wi-Fi networks it can join (it picks the strongest one in range).",
  "ow_title": "Follows", "ow_mine": "This Mac ({0})", "ow_standby": "“{0}”. This Mac is on standby and leaves Bluetooth alone",
  "ow_legacy": "The board's firmware doesn't support claiming yet. Update the firmware first.", "ow_unknown": "No board seen yet",
  "ow_claim": "Use Board on This Mac", "ow_claiming": "Tap the green Connect button on the board…", "ow_done": "The board now follows this Mac",
  "ow_asking": "The board is asking on its screen: tap the green Connect button ({0} s left)", "ow_free": "The board has no owner yet. Click Use Board on This Mac, then tap Connect on the board.",
  "ow_why_declined": "It was declined on the board", "ow_why_timeout": "Nobody tapped Connect on the board within 30 seconds", "ow_why_asleep": "The board is face down, asleep. Turn it over and try again.",
  "ow_why_busy": "The board is asking another Mac right now. Try again when it's done.", "ow_why_asking": "The board is still waiting for a tap on Connect",
  "ow_fail": "Couldn't reach the board: ", "ow_bar": "The board is following “{0}”. This Mac is on standby.", "ow_q": "Move the board to this Mac? “{0}” will go on standby.",
  "ow_cap": "The board follows one Mac at a time: status, dictation and its Wi-Fi link all go to that Mac. To switch, click Use Board on This Mac on the new one; the old one goes on standby. The board never switches on its own, so two Macs can't fight over it.",
  "wf_now": "Connected to", "wf_cur": "{0} · signal {1} dBm · {2}", "wf_none": "Not on Wi-Fi (Bluetooth only)",
  "wf_offline": "The board is offline. Turn it on and wait for Bluetooth to connect.", "wf_scan": "Scan Again", "wf_scanning": "The board is scanning for Wi-Fi…",
  "wf_fac": "Built-in", "wf_seen": "Signal {0} dBm", "wf_unseen": "Not in range", "wf_using": "In use", "wf_rm": "Remove",
  "wf_add": "Add a Wi-Fi Network", "wf_add_sub": "Type the name exactly as the router shows it (case matters).", "wf_ssid_ph": "Network name",
  "wf_pass_ph": "Password (blank if none)", "wf_join": "Join", "wf_saved": "Saved on the board. Connecting…",
  "wf_rm_q": "Make the board forget “{0}”?", "wf_rmd": "Removed",
  "wf_5g_q": "This name ends in 5G. The board only supports 2.4 GHz and can't see 5G networks. Add it anyway?",
  "wf_why_ssid": "Invalid name (1–32 characters)", "wf_why_pass": "The password must be 8–63 characters, or blank", "wf_why_factory": "The built-in network can't be changed or removed",
  "wf_why_unknown": "The board doesn't have this network", "wf_why_timeout": "The board didn't answer", "wf_fail": "Couldn't read: ",
  "wf_near": "The board can see here", "wf_near_sub": "Click a name to fill it in. Only 2.4 GHz networks are listed; that's all the board can see.", "wf_near_none": "No scan yet. Click Scan Again.",
  "sc_t": "Scan This Mac", "sc_sub": "Which agents this Mac has, which seat each one fills, and whether its status is wired up.", "sc_btn": "Scan Again",
  "sc_none": "No known agents found", "sc_cn": "China build", "sc_intl": "International", "sc_noseat": "No seat",
  "sc_run": "Running", "sc_inst": "Installed", "sc_using": "In use", "sc_use": "Use This", "sc_used": "Seat now uses {0}",
  "sc_cli": "Command line, runs in a terminal", "sc_how_hooks": "Status: hooks", "sc_how_codex": "Status: notify + hooks",
  "sc_how_log": "Status: reads its log", "sc_how_db": "Status: reads its database", "sc_how_none": "Only open / busy",
  "sc_miss_hooks": "Hooks not installed. Run host/setup.sh once.", "sc_miss_codex": "Codex notify or hooks not wired. Run host/setup.sh once.",
  "sc_miss_log": "No log yet. Open the app and use it once.", "sc_miss_db": "No database yet. Open the app and use it once.",
  "ag_btn": "Hand to My Agent", "ag_copied": "Copied. Paste it into any AI agent on this Mac.",
  "ag_prompt": "Read http://127.0.0.1:8788/agent-setup and follow its steps to set up the AI agents on this Mac for the AgentTouch desk pet. Change settings only through the endpoints it lists, never the code; stop and ask me for anything I have to do myself (system permissions, looking at the board). When you're done, report back in the format at the end.",
  "sc_cap": "The board has five fixed seats. Both the China and international builds of an app are recognized; if you have both, click Use This to pick which one a seat brings to the front. Agents with no seat are listed only; putting them in a seat comes in a later version. Hand to My Agent copies one sentence you can paste into any AI agent on this Mac; it sets things up for you by following the guide.",
  "wf_cap": "The board supports 2.4 GHz only. If your router uses one name for 2.4 GHz and 5 GHz, that's fine: the board joins on 2.4 GHz. It remembers up to 4 networks; adding a 5th replaces the oldest. The password goes straight to the board over Bluetooth and is stored only there, not on this Mac.",
  "seat_claude": "Claude", "seat_codex": "Codex", "seat_qoder": "Qoder",
  "seat_qoderwork": "Qwen", "seat_forest": "Forest",
  "st_off": "Offline", "st_idle": "Idle", "st_working": "Working", "st_needs_you": "Needs You", "st_done": "Done",
  "rsn_scanning": "Scanning", "rsn_bt_off": "Mac Bluetooth is off", "rsn_stuck": "Bluetooth stack stuck; the host is recovering",
  "ov_seats": "Seats", "ov_seats_sub": "The current seat is outlined", "ov_np": "Now Playing",
  "fn_title": "Card Fonts", "fn_ok": "All {0} fonts are on the card", "fn_nocard": "No microSD card in the board: many characters on the almanac page will show as boxes",
  "fn_wait": "Checked once the board is on Wi-Fi (fonts only go over Wi-Fi)", "fn_checking": "Checking the fonts on the card…",
  "fn_baking": "Building {0} fonts from this Mac's system fonts…", "fn_pushing": "Pushing {0} ({1} of {2}) · {3}% overall",
  "fn_nopil": "Pillow is missing, so the fonts can't be built: rerun host/setup.sh, or /usr/bin/python3 -m pip install --user pillow",
  "fn_err": "Error: {0} (tried again the next time the board joins over Wi-Fi)", "fn_redo": "Re-push", "fn_retry": "Retry",
  "fn_q": "Rebuild and push all 4 fonts? About 16 MB, a few minutes over Wi-Fi.", "fn_started": "Started; this row shows the progress", "fn_busy": "Already pushing; wait for it to finish",
  "ov_cap": "This page replaces <code>pet status</code> and refreshes every 2 seconds. The board counts as connected while either Wi-Fi or Bluetooth is up.",
  "link_both": "Wi-Fi + Bluetooth", "link_bt": "Bluetooth", "connected": "Connected", "not_connected": "Not Connected",
  "disc": "Disconnected {0} s", "bsub": "Up {0} min · Heap {1} / {2} KB", "last_rst": "Last reset {0}",
  "hup": "Up {0} min · {1} MB", "fw": "Firmware {0}", "n_off": "{0} Offline",
  "np_off": "Off", "np_none": "Nothing playing",
  "seats_cap": "Use Front App: click into that app's window first, then come back and click it. Leaving a CPU threshold empty only skips writing it; to remove an existing one, save, then restart the host on the Host page. Skins live on the board and apply as soon as you click, no save needed. A small colored dot shows which seat is wearing that skin.",
  "pr_t": "Advanced: Process Rules",
  "pr_sub": "Per seat {\"match\": regex, \"exclude\": regex or null}, matched against the whole <code>ps -axo args</code> line. If any regex fails to compile, nothing is saved.",
  "no_app_ph": "Empty = no app for this seat", "use_front": "Use Front App", "mac_app": "Mac App",
  "kf_focus_keys": "Key After Switching", "kf_approve_keys": "Approve Key", "kf_reject_keys": "Reject Key",
  "key_ph": "Empty = none", "cpu_aria": "CPU threshold", "cpu_lab": "CPU “Working” Threshold",
  "cpu_hint": "Empty = ignore CPU for this seat", "skin": "Skin", "skin_hint": "Applies on click. Picking another seat's skin swaps the two.",
  "no_app": "— No app —", "not_here": "Not installed", "skin_fail": "Couldn't change the skin", "send_fail": "Couldn't send: ",
  "vs_t": "Needs You and Done Alerts", "vs_sub": "Only these two moments. Fortunes and other sounds are unaffected.",
  "vs_chirp": "Chirp", "vs_tts": "Voice",
  "vol": "Volume", "vol_sub": "Written straight to the board. 0 = mute.",
  "say_t": "Make the Pet Say Something", "say_sub": "Aria text-to-speech. Needs Wi-Fi.", "say_ph": "Type a sentence…", "play": "Play",
  "mic_src": "Microphone", "mic_hint_ok": "While you hold the board's right key",
  "mic_hint_no": "The board mic needs mic_sink built first: swiftc -O -o ~/.agentpet/mic_sink host/mic_sink.swift",
  "mic_mac": "Mac Mic", "mic_board": "Board Mic",
  "ml_t": "Dictation Link", "ml_sub": "Which link carries the board mic's audio; auto means Bluetooth first. /test/mic/link overrides it until the next push.",
  "ml_ble": "Bluetooth First", "ml_tcp": "Wi-Fi First", "msd": "Virtual Audio Device",
  "mts_t": "Keep fn Held After Release",
  "mts_sub": "The pipeline lags, so a short tail swallows the last words. Default 0.6. Measure with /test/mic/latency.",
  "pk_t": "Show When Picked Up",
  "pk_sub": "Written to the board on click. follow = the page facing you (turn and hold 0.9 s to switch, shake to call the pet). smart is its old name. needs_you always takes the screen; see doc/06.",
  "ff_t": "Mac Follows Seat Switches", "ff_sub": "When you stop swiping, brings that app forward and focuses its input field.",
  "fb_t": "Board Follows the Mac's Front App",
  "fb_sub": "Only the five agent apps. Stays put for 5 s after a board swipe, 3 s after the host raised an app, and while you hold to talk.",
  "so_t": "Show Offline Seats",
  "so_sub": "Off by default: seat dots show only running agents and swipes skip apps that quit (unless all have quit). Turn on to see all five while setting up.",
  "st_t": "Break Reminder", "st_sub": "The board calls for a break. 0 = off.", "minutes": "min",
  "fs_t": "Settle Time Before the Mac Switches",
  "fs_sub": "A run of swipes raises only the seat you land on. Too short raises each one; too long feels slow. Default 1.5.",
  "np_t": "Show the Mac's Now Playing on the Board",
  "np_sub": "Reads Now Playing through media-control and turns itself off if that isn't installed. On the board, swipe left from the clock for the player.",
  "au_ph": "Podcast RSS, audio URL, or local absolute path",
  "au_n1": "Latest 1", "au_n3": "Latest 3", "au_n5": "Latest 5", "add": "Add", "queue": "Queue",
  "au_cap": "Download → 16 kHz mono → the board's block format → copy to the card. An hour of audio is about 29 MB and takes ten-odd minutes over Wi-Fi. If the board is offline it's kept on the Mac and copied when the board reconnects. On the board, swipe right from the clock for podcasts.",
  "austep_start": "Preparing", "austep_ready": "Preparing", "austep_download": "Downloading", "austep_convert": "Converting",
  "austep_encode": "Encoding", "austep_cover": "Cover", "austep_push": "Copying to card",
  "dur_s": "{0} s", "dur_m": "{0} min", "dur_hm": "{0} h {1} min",
  "au_more": "({0} more queued)", "au_queued": "{0} queued",
  "au_pend_tcp": "{0} waiting to copy", "au_pend_wait": "{0} waiting for the board's Wi-Fi",
  "au_lastfail": "Last one failed: {0}", "au_idle": "Idle", "au_idle_off": "Idle (board is off Wi-Fi; new items stay on the Mac for now)",
  "au_added": "Added to queue", "au_addfail": "Couldn't add", "au_rm_q": "Delete “{0}”? It's removed from the Mac and the card.",
  "au_rmd": "Deleted", "au_rmfail": "Couldn't delete", "au_pushed": "On card ✓", "au_pending": "Not on card yet",
  "del": "Delete", "au_empty": "No audio added yet.", "local_file": "Local file",
  "host_t": "Host Service",
  "host_sub": "Settings apply right away; only a few keys (like clearing a CPU threshold) need a restart. The board drops briefly and reconnects. To stop it for good, run <code>pet off</code> in Terminal; it still starts at the next login.",
  "restart_btn": "Restart Host", "mem": "Memory",
  "mem_sub": "RSS, one point every 30 s since this launch. A flat line is good; a slope is the warning.",
  "mem_sub_f": "RSS, one point every {0} s since this launch ({1} min, {2} points). A flat line is good; a slope is the warning.",
  "mem_aria": "RSS trend", "mem_now": "Now", "mem_start": "At Launch", "mem_hr": "Per Hour", "mem_thr": "Threads",
  "mem_recent": "Last {0} min",
  "log": "Log", "last": "Last", "lines": "lines", "refresh": "Refresh", "log_unread": "(not loaded yet)",
  "log_empty": "(log is empty)", "log_fail": "Couldn't read: ",
  "dbg": "Debug", "dbg_sub": "Sent straight to the board", "b_faces": "Tour 14 Faces", "b_qian": "Read Fortune", "b_alm": "Push Almanac",
  "b_rep": "Push Report", "b_prof": "Profile Card", "b_str": "Break Reminder", "b_nudge": "Name Card + Lights",
  "restart_q": "Restart the host? The board will drop briefly and reconnect.", "restarting": "Restarting…",
  "restarted": "Restarted · {0}", "restart_lost": "Not back yet. Check pet status in Terminal.", "restart_fail": "Couldn't send the restart request",
  "front_none": "Can't read the front app", "front_many": "The front app goes by {0}; used the first", "front_one": "Front app: {0}",
  "front_fail": "Can't read the front app: ",
  "save_btn": "Save to config.json", "dirty": "Unsaved changes",
  "bad_json": "Process rules aren't valid JSON. Nothing saved.", "not_saved": "Not saved",
  "saved": "Saved {0}: {1}", "save_fail": "Save failed: ", "no_host": "Can't reach the host: ",
 },
}

SETTINGS_HTML = """<!doctype html><html lang="__LANG__"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title><style>
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:#000;color:#f2f2f2;font:14px/1.45 -apple-system,"SF Pro Text","PingFang SC","Hiragino Sans GB",sans-serif;
 -webkit-font-smoothing:antialiased;display:flex;overflow:hidden}
a{color:#0a84ff;text-decoration:none}
.mono{font-family:"SF Mono",Menlo,monospace}
.t2{color:rgba(255,255,255,.62)}.t3{color:rgba(255,255,255,.40)}
/* ---- sidebar ---- */
aside{width:236px;flex:none;background:#0d0d0d;border-right:1px solid rgba(255,255,255,.07);
 display:flex;flex-direction:column;padding:22px 12px 12px;height:100vh}
.brand{display:flex;align-items:center;gap:10px;padding:0 8px 18px}
.brand .lg{width:36px;height:36px;border-radius:10px;background:#151515;border:1px solid rgba(255,255,255,.08);
 display:flex;align-items:center;justify-content:center;flex:none}
.brand b{display:block;font-size:15px;font-weight:600}
.brand small{display:block;font-size:12px;color:rgba(255,255,255,.40)}
nav{display:flex;flex-direction:column;gap:2px}
nav a{display:flex;align-items:center;gap:10px;height:34px;padding:0 10px;border-radius:8px;
 color:rgba(255,255,255,.72);font-size:13.5px}
nav a svg{color:rgba(255,255,255,.5);flex:none}
nav a:hover{background:rgba(255,255,255,.05)}
nav a.on{background:rgba(255,255,255,.10);color:#fff}
nav a.on svg{color:#fff}
.sbst{margin-top:auto;background:#151515;border:1px solid rgba(255,255,255,.07);border-radius:12px;
 padding:12px 14px;display:flex;flex-direction:column;gap:6px;font-size:13px}
.sbst .l{display:flex;align-items:center;gap:8px;min-width:0}
.sbst .l span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.sbst .s{font-size:11px;line-height:1.6;color:rgba(255,255,255,.40);font-family:"SF Mono",Menlo,monospace;
 margin:-2px 0 4px 16px;white-space:pre-line}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;flex:none;background:#555}
.dot.ok{background:#30d158}.dot.warn{background:#ffd60a}
/* ---- main ---- */
main{flex:1;min-width:0;display:flex;flex-direction:column;height:100vh}
header{display:flex;align-items:flex-end;justify-content:space-between;gap:20px;padding:34px 40px 18px;flex:none}
header h1{margin:0;font-size:26px;font-weight:700;letter-spacing:-.01em}
header p{margin:6px 0 0;font-size:13px;color:rgba(255,255,255,.62);max-width:640px}
#hd_pills{display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end}
.content{flex:1;overflow:auto;padding:0 40px 32px}
.page{display:none;flex-direction:column;gap:22px;max-width:860px}
.page.on{display:flex}
.sec{font-size:12px;font-weight:600;letter-spacing:.08em;color:rgba(255,255,255,.40);text-transform:uppercase;padding:0 18px;margin:0 0 8px}
.grp{background:#151515;border:1px solid rgba(255,255,255,.07);border-radius:14px;overflow:hidden}
.row{display:flex;align-items:center;gap:14px;min-height:52px;padding:8px 18px;border-top:1px solid rgba(255,255,255,.07)}
.grp>.row:first-child,.grp>.seat:first-child>.row{border-top:0}
.lab{flex:1;min-width:0;font-size:14px;line-height:1.35}
.lab small{display:block;font-size:12px;color:rgba(255,255,255,.40);margin-top:2px}
.val{font-size:14px;color:rgba(255,255,255,.62)}
.cap{font-size:12px;color:rgba(255,255,255,.40);padding:8px 18px 0;line-height:1.5;margin:0}
.cap code,.lab small code{font-family:"SF Mono",Menlo,monospace;font-size:11px}
/* controls */
.tg{position:relative;width:46px;height:28px;flex:none;cursor:pointer}
.tg input{position:absolute;opacity:0;width:0;height:0;margin:0}
.tg .k{position:absolute;inset:0;border-radius:14px;background:rgba(255,255,255,.14);transition:.2s}
.tg .k::after{content:"";position:absolute;top:2px;left:2px;width:24px;height:24px;border-radius:12px;background:#fff;
 box-shadow:0 2px 6px rgba(0,0,0,.5);transition:.2s}
.tg input:checked+.k{background:#30d158}
.tg input:checked+.k::after{left:20px}
.tg input:focus-visible+.k{outline:2px solid #0a84ff;outline-offset:2px}
.tg input:disabled+.k{opacity:.35}
.seg{display:inline-flex;background:rgba(255,255,255,.08);border-radius:9px;padding:2px;flex:none}
.seg label{position:relative;cursor:pointer}
.seg label input{position:absolute;opacity:0;width:0;height:0;margin:0}
.seg label span{display:block;padding:5px 14px;border-radius:7px;font-size:13px;color:rgba(255,255,255,.62);min-height:28px;line-height:18px}
.seg label input:checked+span{background:#3a3a3c;color:#fff;box-shadow:0 1px 3px rgba(0,0,0,.6)}
.seg label input:focus-visible+span{outline:2px solid #0a84ff}
.seg label input:disabled+span{opacity:.35;cursor:default}
.seg label.off span{opacity:.35}
.seg button{background:transparent;color:rgba(255,255,255,.62);padding:5px 12px;min-height:28px;border-radius:7px;font-weight:400;display:inline-flex;align-items:center;gap:5px}
.seg button:hover{background:rgba(255,255,255,.06)}
.seg button.on{background:#3a3a3c;color:#fff;box-shadow:0 1px 3px rgba(0,0,0,.6)}
.seg button i{width:6px;height:6px;border-radius:50%;display:inline-block}
.seg.dis{opacity:.4;pointer-events:none}
input[type=text],input[type=password],input[type=number],select,textarea{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.10);
 border-radius:8px;color:#f2f2f2;font:13px "SF Mono",Menlo,monospace;padding:6px 10px;min-height:30px;outline:0}
input[type=text]:focus,input[type=password]:focus,input[type=number]:focus,select:focus,textarea:focus{border-color:#0a84ff}
input[type=text]{width:220px}
input.w{width:300px;font-family:-apple-system,"PingFang SC",sans-serif}
input.g{flex:1;width:auto;font-family:-apple-system,"PingFang SC",sans-serif}
input[type=number]{width:76px;text-align:right}
select{font-family:-apple-system,"PingFang SC",sans-serif;padding-right:24px}
textarea{width:100%;font-size:12px;line-height:1.5;min-height:150px;resize:vertical}
.unit{font-size:12px;color:rgba(255,255,255,.40)}
.num{display:flex;gap:6px;align-items:center}
input[type=range]{width:220px;accent-color:#fff;margin:0}
.ownbar{display:flex;align-items:center;gap:12px;margin:0 40px 8px;padding:10px 14px;border-radius:12px;
 background:rgba(255,214,10,.10);color:#ffd60a;font-size:13px;flex:none}
.ownbar[hidden]{display:none}.ownbar span{flex:1}
.pill{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 10px;border-radius:12px;font-size:12px;font-weight:500;
 background:rgba(255,255,255,.08);color:rgba(255,255,255,.62);white-space:nowrap}
.pill i{width:7px;height:7px;border-radius:50%;background:currentColor;display:inline-block}
.pill.working{color:#30d158;background:rgba(48,209,88,.14)}
.pill.needs_you{color:#ffd60a;background:rgba(255,214,10,.14)}
.pill.done{color:#64d2ff;background:rgba(100,210,255,.14)}
.pill.off{color:rgba(255,255,255,.32);background:rgba(255,255,255,.05)}
.pill.sel{box-shadow:inset 0 0 0 1px rgba(255,255,255,.35)}
button{border:0;border-radius:9px;padding:7px 14px;font:13px/1 -apple-system,"PingFang SC",sans-serif;font-weight:500;
 cursor:pointer;min-height:32px;background:rgba(255,255,255,.10);color:#f2f2f2;white-space:nowrap}
button:hover{background:rgba(255,255,255,.16)}
button.pri{background:#0a84ff;color:#fff}button.pri:hover{background:#2b95ff}
button.danger{color:#ff453a;background:rgba(255,69,58,.12)}button.danger:hover{background:rgba(255,69,58,.2)}
button.sm{padding:5px 10px;min-height:28px;font-size:12px}
button:disabled{opacity:.4;cursor:default}
.chev{width:16px;height:16px;flex:none;color:rgba(255,255,255,.3);transition:.15s}
/* seats */
.seat .row.hd{cursor:pointer;min-height:56px;user-select:none}
.seat .row.hd:hover{background:rgba(255,255,255,.03)}
.seat.open .chev{transform:rotate(90deg)}
.seat .sub{display:none;background:#101010;border-top:1px solid rgba(255,255,255,.07);padding:6px 18px 12px 40px;flex-direction:column}
.seat.open .sub{display:flex}
.srow{display:flex;align-items:center;justify-content:space-between;min-height:40px;font-size:13px;gap:12px}
.srow>span:first-child{color:rgba(255,255,255,.62)}
.srow small{color:rgba(255,255,255,.40);font-size:11px;margin-left:6px}
.srow .r{display:flex;gap:8px;align-items:center}
.seat .app{width:170px;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* audio list */
.au{display:flex;gap:12px;align-items:center;min-height:48px;padding:6px 18px;border-top:1px solid rgba(255,255,255,.07)}
.au .t{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.au .m{color:rgba(255,255,255,.40);font-size:12px;white-space:nowrap}
pre{background:#0d0d0d;border-top:1px solid rgba(255,255,255,.07);padding:10px 18px;margin:0;max-height:320px;overflow:auto;
 font:12px/1.45 "SF Mono",Menlo,monospace;color:rgba(255,255,255,.62);white-space:pre-wrap;word-break:break-all}
.dbg{display:flex;gap:8px;flex-wrap:wrap;padding:12px 18px;border-top:1px solid rgba(255,255,255,.07)}
/* footer + toast */
footer{flex:none;display:flex;align-items:center;gap:12px;padding:14px 40px;border-top:1px solid rgba(255,255,255,.07);background:#0d0d0d}
footer .sp{flex:1}
#savehint{font-size:12px;color:rgba(255,255,255,.40)}
#toast{position:fixed;bottom:76px;left:50%;transform:translateX(-50%);background:#2c2c2e;border:1px solid rgba(255,255,255,.12);
 padding:9px 18px;border-radius:10px;opacity:0;transition:.3s;font-size:13px;max-width:70vw;pointer-events:none;box-shadow:0 8px 24px rgba(0,0,0,.6)}
.sl{display:flex;align-items:center;gap:12px}
.memw{display:flex;flex-direction:column;gap:8px;align-items:flex-end}
.memn{display:flex;gap:18px}
.memn span{display:flex;flex-direction:column;align-items:flex-end;min-width:56px}
.memn b{font:500 14px "SF Mono",Menlo,monospace;color:#f2f2f2}
.memn b.warn{color:#ffd60a}
.memn i{font-style:normal;font-size:11px;color:rgba(255,255,255,.40)}
.lang{display:flex;margin:0 8px 16px}
.lang label{flex:1;text-align:center}
</style></head><body>
<aside>
 <div class="brand"><div class="lg"><svg width="22" height="22" viewBox="0 0 22 22" fill="none"><circle cx="7.5" cy="10" r="3" fill="#fff"/><circle cx="14.5" cy="10" r="3" fill="#fff"/></svg></div>
  <div><b>AgentTouch</b><small id="sb_ver">…</small></div></div>
 <div class="seg lang" id="langseg"><label><input type="radio" name="lang" value="zh"><span>中文</span></label><label><input type="radio" name="lang" value="en"><span>English</span></label></div>
 <nav id="nav"></nav>
 <div class="sbst">
  <div class="l"><i class="dot" id="sb_bdot"></i><span id="sb_board" data-t="board"></span></div>
  <div class="s" id="sb_boardsub" data-t="loading"></div>
  <div class="l"><i class="dot" id="sb_hdot"></i><span id="sb_host">host</span></div>
  <div class="s" id="sb_hostsub" data-t="loading"></div>
 </div>
</aside>
<main>
 <header><div><h1 id="hd_t"></h1><p id="hd_d"></p></div><div id="hd_pills"></div></header>
 <div class="ownbar" id="ownbar" hidden><span id="ownbar_t"></span><button class="pri sm" onclick="ownClaim()" data-t="ow_claim"></button></div>
 <div class="content">

 <div class="page" data-page="overview">
  <section><div class="grp">
   <div class="row"><i class="dot" id="s_bdot"></i><div class="lab"><span data-t="board"></span><small id="s_board" data-t="loading_dots"></small></div><span class="val mono" id="s_fw"></span></div>
   <div class="row"><i class="dot" id="s_fdot"></i><div class="lab"><span data-t="fn_title"></span><small id="s_fonts" data-t="loading_dots"></small></div><button class="sm" id="s_fbtn" onclick="fontRedo()" hidden></button></div>
   <div class="row"><i class="dot ok"></i><div class="lab">host<small id="s_host" data-t="loading_dots"></small></div></div>
   <div class="row"><div class="lab"><span data-t="ov_seats"></span><small data-t="ov_seats_sub"></small></div><div id="s_seats" style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end"></div></div>
   <div class="row"><div class="lab" data-t="ov_np"></div><span class="val" id="npnow" data-t="loading_dots"></span></div>
  </div><p class="cap" data-th="ov_cap"></p></section>
 </div>

 <div class="page" data-page="wifi">
  <section><div class="grp">
   <div class="row"><i class="dot" id="ow_dot"></i><div class="lab"><span data-t="ow_title"></span><small id="ow_sub" data-t="loading_dots"></small></div><button class="sm" id="ow_btn" onclick="ownClaim()" data-t="ow_claim" hidden></button></div>
  </div><p class="cap" data-t="ow_cap"></p></section>
  <section><div class="grp">
   <div class="row"><i class="dot" id="wf_dot"></i><div class="lab"><span data-t="wf_now"></span><small id="wf_nowsub" data-t="loading_dots"></small></div><button class="sm" onclick="wfScan()" data-t="wf_scan"></button></div>
   <div id="wflist"></div>
  </div></section>
  <section><div class="grp">
   <div class="row" style="align-items:flex-start;padding-top:12px;padding-bottom:12px"><div class="lab"><span data-t="wf_near"></span><small data-t="wf_near_sub"></small></div>
    <div id="wfnear" style="display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;max-width:60%"></div></div>
   <div class="row"><div class="lab"><span data-t="wf_add"></span><small data-t="wf_add_sub"></small></div></div>
   <div class="row" style="gap:10px"><input type="text" class="g" id="wfs" data-tp="wf_ssid_ph" autocomplete="off" spellcheck="false"><input type="password" class="g" id="wfp" data-tp="wf_pass_ph" autocomplete="new-password"><button class="pri sm" onclick="wfAdd()" data-t="wf_join"></button></div>
  </div><p class="cap" data-t="wf_cap"></p></section>
 </div>

 <div class="page" data-page="seats">
  <section><div class="grp">
   <div class="row"><div class="lab"><span data-t="sc_t"></span><small data-t="sc_sub"></small></div><button class="sm" onclick="agCopy()" data-t="ag_btn"></button><button class="sm" onclick="scLoad(1)" data-t="sc_btn"></button></div>
   <div id="sclist"></div>
  </div><p class="cap" data-t="sc_cap"></p></section>
  <section><div class="grp" id="cards"></div>
   <p class="cap" data-t="seats_cap"></p></section>
  <section><div class="grp">
   <div class="seat" id="prseat"><div class="row hd" onclick="this.parentNode.classList.toggle('open')"><div class="lab"><span data-t="pr_t"></span><small data-th="pr_sub"></small></div><svg class="chev" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M6 3l5 5-5 5"/></svg></div>
    <div class="sub" style="padding:12px 18px"><textarea id="pr" spellcheck="false"></textarea></div></div>
  </div></section>
 </div>

 <div class="page" data-page="sound">
  <section><div class="grp">
   <div class="row"><div class="lab"><span data-t="vs_t"></span><small data-t="vs_sub"></small></div>
    <div class="seg" id="vs"><label><input type="radio" name="vs" value="chirp"><span data-t="vs_chirp"></span></label><label><input type="radio" name="vs" value="tts"><span data-t="vs_tts"></span></label></div></div>
   <div class="row"><div class="lab"><span data-t="vol"></span><small data-t="vol_sub"></small></div>
    <div class="sl"><input type="range" id="vol" min="0" max="100" step="10" value="60"><span class="val mono" id="volv" style="width:28px;text-align:right">—</span></div></div>
   <div class="row"><div class="lab"><span data-t="say_t"></span><small data-t="say_sub"></small></div>
    <input type="text" class="w" id="sayt" data-tp="say_ph"><button class="sm" onclick="doSay()" data-t="play"></button></div>
  </div></section>
 </div>

 <div class="page" data-page="mic">
  <section><div class="grp">
   <div class="row"><div class="lab"><span data-t="mic_src"></span><small id="vs_hint" data-t="mic_hint_ok"></small></div>
    <div class="seg" id="vs2"><label><input type="radio" name="vsrc" id="vs_mac" value="mac"><span data-t="mic_mac"></span></label><label id="vs_board_l"><input type="radio" name="vsrc" id="vs_board" value="board"><span data-t="mic_board"></span></label></div></div>
   <div class="row"><div class="lab"><span data-t="ml_t"></span><small data-t="ml_sub"></small></div>
    <div class="seg" id="ml"><label><input type="radio" name="ml" value="ble"><span data-t="ml_ble"></span></label><label><input type="radio" name="ml" value="tcp"><span data-t="ml_tcp"></span></label><label><input type="radio" name="ml" value="auto"><span>auto</span></label></div></div>
   <div class="row"><div class="lab" data-t="msd"></div><input type="text" id="msd" placeholder="BlackHole 2ch"></div>
   <div class="row"><div class="lab"><span data-t="mts_t"></span><small data-t="mts_sub"></small></div>
    <div class="num"><input type="number" id="mts" min="0" max="5" step="0.1"><span class="unit">s</span></div></div>
  </div></section>
 </div>

 <div class="page" data-page="pickup">
  <section><div class="grp">
   <div class="row"><div class="lab"><span data-t="pk_t"></span><small data-t="pk_sub"></small></div>
    <div class="seg" id="pk"><label><input type="radio" name="pk" value="follow"><span>follow</span></label><label><input type="radio" name="pk" value="face"><span>face</span></label><label><input type="radio" name="pk" value="clock"><span>clock</span></label><label><input type="radio" name="pk" value="almanac"><span>almanac</span></label><label><input type="radio" name="pk" value="smart"><span>smart</span></label></div></div>
  </div></section>
 </div>

 <div class="page" data-page="behavior">
  <section><div class="grp">
   <div class="row"><div class="lab"><span data-t="ff_t"></span><small data-t="ff_sub"></small></div><label class="tg"><input type="checkbox" id="ff"><span class="k"></span></label></div>
   <div class="row"><div class="lab"><span data-t="fb_t"></span><small data-t="fb_sub"></small></div><label class="tg"><input type="checkbox" id="fb"><span class="k"></span></label></div>
   <div class="row"><div class="lab"><span data-t="so_t"></span><small data-t="so_sub"></small></div><label class="tg"><input type="checkbox" id="so"><span class="k"></span></label></div>
   <div class="row"><div class="lab"><span data-t="st_t"></span><small data-t="st_sub"></small></div><div class="num"><input type="number" id="st" min="0" max="600"><span class="unit" data-t="minutes"></span></div></div>
   <div class="row"><div class="lab"><span data-t="fs_t"></span><small data-t="fs_sub"></small></div><div class="num"><input type="number" id="fs" min="0.2" max="5" step="0.1"><span class="unit">s</span></div></div>
  </div></section>
 </div>

 <div class="page" data-page="np">
  <section><div class="grp">
   <div class="row"><div class="lab"><span data-t="np_t"></span><small data-t="np_sub"></small></div><label class="tg"><input type="checkbox" id="np"><span class="k"></span></label></div>
  </div></section>
 </div>

 <div class="page" data-page="audio">
  <section><div class="grp">
   <div class="row" style="min-height:60px"><input type="text" class="g" id="au" data-tp="au_ph">
    <select id="aun"><option value="1" data-t="au_n1"></option><option value="3" data-t="au_n3"></option><option value="5" data-t="au_n5"></option></select>
    <button class="pri sm" onclick="auAdd()" data-t="add"></button></div>
   <div class="row"><div class="lab"><span data-t="queue"></span><small id="aust" data-t="loading_dots"></small></div></div>
   <div id="aulist"></div>
  </div><p class="cap" data-t="au_cap"></p></section>
 </div>

 <div class="page" data-page="host">
  <section><div class="grp">
   <div class="row"><div class="lab"><span data-t="host_t"></span><small data-th="host_sub"></small></div><button class="danger sm" onclick="restart()" data-t="restart_btn"></button></div>
   <div class="row" style="align-items:flex-start;padding-top:12px;padding-bottom:12px"><div class="lab"><span data-t="mem"></span><small id="memsub" data-t="mem_sub"></small></div>
    <div class="memw"><svg id="memsvg" viewBox="0 0 320 48" width="320" height="48" data-ta="mem_aria"><polyline id="memline" fill="none" stroke="#fff" stroke-width="1.5" stroke-linejoin="round" points=""/><line id="memzero" x1="0" x2="320" y1="47" y2="47" stroke="rgba(255,255,255,.12)"/></svg>
     <div class="memn"><span><b id="mem_now">—</b><i data-t="mem_now"></i></span><span><b id="mem_start">—</b><i data-t="mem_start"></i></span><span><b id="mem_slope">—</b><i data-t="mem_hr"></i></span><span><b id="mem_thr">—</b><i data-t="mem_thr"></i></span></div></div></div>
  </div></section>
  <section><div class="grp">
   <div class="seat" id="logdet"><div class="row hd" onclick="logToggle(this.parentNode)"><div class="lab"><span data-t="log"></span><small>/tmp/agentpet_host.log</small></div>
    <div class="num" onclick="event.stopPropagation()"><span class="unit" data-t="last"></span><input type="number" id="logn" min="1" max="500" step="10" value="60"><span class="unit" data-t="lines"></span><button class="sm" onclick="logLoad()" data-t="refresh"></button></div>
    <svg class="chev" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M6 3l5 5-5 5"/></svg></div>
    <div class="sub" style="padding:0"><pre id="logbox" data-t="log_unread"></pre></div></div>
  </div></section>
  <section><div class="grp">
   <div class="row"><div class="lab"><span data-t="dbg"></span><small data-t="dbg_sub"></small></div></div>
   <div class="dbg">
    <button class="sm" onclick="hit('/test/faces')" data-t="b_faces"></button>
    <button class="sm" onclick="hit('/test/qian')" data-t="b_qian"></button>
    <button class="sm" onclick="hit('/test/almanac')" data-t="b_alm"></button>
    <button class="sm" onclick="hit('/test/report')" data-t="b_rep"></button>
    <button class="sm" onclick="hit('/test/profile')" data-t="b_prof"></button>
    <button class="sm" onclick="hit('/test/stretch')" data-t="b_str"></button>
    <button class="sm" onclick="hit('/test/nudge')" data-t="b_nudge"></button>
   </div>
  </div></section>
 </div>

 </div>
 <footer><span id="savehint"></span><span class="sp"></span><button id="save" disabled onclick="save()" data-t="save_btn"></button></footer>
</main>
<div id="toast"></div>
<script>
// Every fetch goes through API so this page can be dumped to a static file
// and re-styled against a canned /settings/data.
const API=window.API_BASE||'';
const $=q=>document.querySelector(q);
// ---- i18n: T is SETTINGS_T from the host, L the config.json lang ----
const T=__T_JSON__;
let L=__LANG_JSON__;
function t(k){const d=T[L]||T.zh;return k in d?d[k]:(k in T.zh?T.zh[k]:k)}
function tf(k){let s=t(k);for(let i=1;i<arguments.length;i++)s=s.split('{'+(i-1)+'}').join(arguments[i]);return s}
function tx(e,k){e.dataset.t=k;e.textContent=t(k);return e}
const seatNm=a=>(('seat_'+a) in T.zh)?t('seat_'+a):a;
const stNm=s=>(('st_'+s) in T.zh)?t('st_'+s):s;
const SEATCOL={claude:'#FF6B5A',codex:'#5096FF',qoder:'#3DDC84',qoderwork:'#A778FF',forest:'#F2A33C'};  // firmware config.h AGENTS[]
const KEYFIELDS=['focus_keys','approve_keys','reject_keys'];
const ICON={
 overview:'<circle cx="8" cy="8" r="6"/><path d="M8 5v3l2 2"/>',
 seats:'<circle cx="5" cy="6" r="2.2"/><circle cx="11" cy="6" r="2.2"/><path d="M1.5 13c.6-2.2 2-3.2 3.5-3.2s2.9 1 3.5 3.2M7.5 13c.6-2.2 2-3.2 3.5-3.2s2.9 1 3.5 3.2"/>',
 sound:'<path d="M2 6h2.5L8 3v10L4.5 10H2z"/><path d="M10.5 5.5a3.5 3.5 0 010 5M12.5 3.5a6 6 0 010 9"/>',
 mic:'<rect x="5.5" y="1.5" width="5" height="8" rx="2.5"/><path d="M3 7.5a5 5 0 0010 0M8 12.5v2"/>',
 pickup:'<rect x="3" y="2" width="10" height="12" rx="2"/><path d="M6 11h4"/>',
 behavior:'<path d="M2 8h12M9 4l4 4-4 4"/>',
 np:'<circle cx="8" cy="8" r="6"/><path d="M6.5 5.5v5l4-2.5z"/>',
 audio:'<path d="M3 11V6a5 5 0 0110 0v5"/><rect x="2" y="9" width="3" height="4" rx="1"/><rect x="11" y="9" width="3" height="4" rx="1"/>',
 wifi:'<path d="M1.5 6a9.5 9.5 0 0113 0M3.8 8.4a6.3 6.3 0 018.4 0M6 10.8a3.2 3.2 0 014 0"/><circle cx="8" cy="13" r=".8" fill="currentColor"/>',
 host:'<rect x="2" y="3" width="12" height="5" rx="1.5"/><rect x="2" y="9" width="12" height="4" rx="1.5"/><circle cx="4.5" cy="5.5" r=".7" fill="currentColor"/><circle cx="4.5" cy="11" r=".7" fill="currentColor"/>'};
// page ids; titles and one-liners are T keys page_<id> / pdesc_<id>
const PAGES=['overview','wifi','seats','sound','mic','pickup','behavior','np','audio','host'];
function toast(t){const e=$('#toast');e.textContent=t;e.style.opacity=1;
 clearTimeout(toast.t);toast.t=setTimeout(()=>e.style.opacity=0,1800)}
function hit(p){fetch(API+p).then(r=>r.text()).then(t=>toast(t.slice(0,60)))}
// ---- pages: hash routing, one section visible at a time ----
function buildNav(){const n=$('#nav');
 PAGES.forEach(p=>{const a=document.createElement('a');a.href='#'+p;a.dataset.p=p;
  a.innerHTML='<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">'+ICON[p]+'</svg><span></span>';
  tx(a.lastChild,'page_'+p);n.appendChild(a)})}
function route(){let h=(location.hash||'#overview').slice(1);
 const p=PAGES.indexOf(h)>=0?h:PAGES[0];
 document.querySelectorAll('nav a').forEach(a=>a.className=a.dataset.p===p?'on':'');
 document.querySelectorAll('.page').forEach(e=>e.className='page'+(e.dataset.page===p?' on':''));
 $('#hd_t').textContent=t('page_'+p);$('#hd_d').textContent=t('pdesc_'+p);
 if(p==='wifi')wfLoad();
 if(p==='seats')scLoad()}
buildNav();window.addEventListener('hashchange',()=>{route();$('.content').scrollTop=0});route();
// ---- language: repaint every marked node in place; typed-in values survive ----
let lastD=null;
function applyLang(){
 document.documentElement.lang=L;document.title=t('title');
 document.querySelectorAll('[data-t]').forEach(e=>e.textContent=t(e.dataset.t));
 document.querySelectorAll('[data-th]').forEach(e=>e.innerHTML=t(e.dataset.th));
 document.querySelectorAll('[data-tp]').forEach(e=>e.placeholder=t(e.dataset.tp));
 document.querySelectorAll('[data-ta]').forEach(e=>e.setAttribute('aria-label',t(e.dataset.ta)));
 document.querySelectorAll('[name=lang]').forEach(r=>r.checked=r.value===L);
 document.querySelectorAll('.seat[data-a] .lab>span:first-child').forEach(e=>e.textContent=seatNm(e.closest('.seat').dataset.a));
 document.querySelectorAll('[data-k="focus_apps"]').forEach(mirrorApp);
 route();if(lastD)status(lastD);auTick();memTick()}
document.querySelectorAll('[name=lang]').forEach(r=>r.onchange=()=>{
 const v=r.value;if(v===L)return;L=v;applyLang();
 fetch(API+'/settings/save',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({lang:v})}).then(r=>r.json())
  .then(d=>toast(d.ok?t('lang_saved'):(d.why||t('not_saved'))))
  .catch(e=>toast(t('save_fail')+e))});
applyLang();
// ---- dirty state: the save button stays disabled until something changed ----
function touch(){const b=$('#save');b.disabled=false;b.className='pri';tx($('#savehint'),'dirty')}
function clean(){const b=$('#save');b.disabled=true;b.className='';const h=$('#savehint');delete h.dataset.t;h.textContent=''}
function onEdit(e){const t=e.target;
 if(!t.matches||!t.matches('input,select,textarea'))return;
 if(['vol','logn','sayt','au','aun','wfs','wfp'].indexOf(t.id)>=0)return;  // board NVS / log / one-shot boxes
 if(t.name==='pk'||t.name==='lang')return;                     // written on click
 if(t.dataset.k==='focus_apps')mirrorApp(t);
 touch()}
document.addEventListener('input',onEdit);
document.addEventListener('change',onEdit);
// ---- the seat rows (expandable) + CPU field, built once in the host's seat order ----
function el(tag,cls,txt){const e=document.createElement(tag);if(cls)e.className=cls;if(txt!=null)e.textContent=txt;return e}
function field(k,a,ph){const e=el('input');e.type='text';e.dataset.k=k;e.dataset.a=a;
 e.dataset.tp=ph;e.placeholder=t(ph);e.dataset.ta=ph;e.setAttribute('aria-label',t(ph));return e}
function srow(label,ctl,hint){const r=el('div','srow');const l=tx(el('span'),label);
 const w0=el('span');w0.appendChild(l);
 if(hint)w0.appendChild(tx(el('small'),hint));r.appendChild(w0);
 const w=el('span','r');(Array.isArray(ctl)?ctl:[ctl]).forEach(c=>w.appendChild(c));r.appendChild(w);return r}
function mirrorApp(inp){const e=document.querySelector('.seat[data-a="'+inp.dataset.a+'"] .app');
 if(e)e.textContent=inp.value.trim()||t('no_app')}
function buildCards(seats){
 const box=$('#cards');box.textContent='';
 seats.forEach(a=>{
  const s=el('div','seat');s.dataset.a=a;
  const hd=el('div','row hd');hd.onclick=()=>s.classList.toggle('open');
  const d=el('i','dot');d.style.background=SEATCOL[a]||'#888';hd.appendChild(d);
  const lab=el('div','lab');const nm=el('span',null,seatNm(a));nm.style.fontWeight='600';lab.appendChild(nm);
  const key=el('span','mono t3',a);key.style.cssText='font-size:12px;margin-left:8px';lab.appendChild(key);hd.appendChild(lab);
  hd.appendChild(el('span','pill st off',stNm('off')));
  hd.appendChild(el('span','val app','…'));
  hd.insertAdjacentHTML('beforeend','<svg class="chev" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M6 3l5 5-5 5"/></svg>');
  s.appendChild(hd);
  const sub=el('div','sub');
  const app=field('focus_apps',a,'no_app_ph');app.className='w';
  const b=tx(el('button','sm'),'use_front');b.onclick=()=>useFront(a);
  sub.appendChild(srow('mac_app',[app,b]));
  KEYFIELDS.forEach(k=>sub.appendChild(srow('kf_'+k,field(k,a,'key_ph'))));
  const cpu=el('input');cpu.type='number';cpu.min='0';cpu.max='100';cpu.step='1';cpu.dataset.cpu=a;cpu.placeholder='—';
  cpu.dataset.ta='cpu_aria';cpu.setAttribute('aria-label',t('cpu_aria'));
  sub.appendChild(srow('cpu_lab',[cpu,el('span','unit','%')],'cpu_hint'));
  const sk=el('div','seg sk');sk.dataset.sk=a;
  SKINS.forEach((nm,i)=>{const b=el('button',null,nm);b.type='button';b.dataset.s=i;b.appendChild(el('i'));
   b.onclick=()=>wear(a,i,nm);sk.appendChild(b)});
  sub.appendChild(srow('skin',sk,'skin_hint'));
  s.appendChild(sub);box.appendChild(s)})}
// ---- skins: board NVS, not a form value -- painted on every poll, never dirty ----
let SKINS=[];
function paintSkins(d){const by=d.skins||{};const online=Object.keys(by).length>0;
 document.querySelectorAll('.seg.sk').forEach(sk=>{const a=sk.dataset.sk;
  sk.className='seg sk'+(online?'':' dis');
  sk.querySelectorAll('button').forEach(b=>{const nm=SKINS[+b.dataset.s];
   b.className=by[a]===nm?'on':'';
   const other=Object.keys(by).find(x=>x!==a&&by[x]===nm);
   b.lastChild.style.background=other?(SEATCOL[other]||'#888'):'transparent'})})}
function wear(a,i,nm){
 fetch(API+'/test/skin/'+i+'?agent='+encodeURIComponent(a)).then(r=>r.json()).then(d=>{
  if(!d.ok){toast(d.why||t('skin_fail'));return}
  toast(seatNm(a)+' → '+nm);
  const sk=document.querySelector('.seg.sk[data-sk="'+a+'"]');
  if(sk)sk.querySelectorAll('button').forEach(b=>b.className=+b.dataset.s===i?'on':'')})
 .catch(e=>toast(t('send_fail')+e))}
// ---- 状态: redrawn every poll; it never touches an input being typed in ----
// -> [dot class, full line, detail line, short line for the sidebar]
function boardLine(d){
 const b=d.board||{};
 const link=d.boards&&d.ble?t('link_both'):d.boards?'Wi-Fi':(d.ble?t('link_bt'):'');
 const kb=v=>(typeof v==='number')?Math.round(v/1024):'?';
 if(link)return['ok',t('connected')+' · '+link,
  tf('bsub',typeof b.up==='number'?Math.floor(b.up/60):'?',kb(b.heap),kb(b.min))
  +(b.rst?' · '+tf('last_rst',b.rst):''),link];
 if(b.age_s==null&&b.hello_age_s==null)return['',t('not_connected'),'',t('not_connected')];
 const why=b.link_reason&&(('rsn_'+b.link_reason) in T.zh)?t('rsn_'+b.link_reason):'';
 const dl=tf('disc',b.age_s==null?'?':b.age_s);
 return['warn',dl,why,dl]}
function pillEl(st,txt,sel){const e=el('span','pill '+st+(sel?' sel':''));e.appendChild(el('i'));
 e.appendChild(document.createTextNode(txt));return e}
function ownPaint(o){o=o||{};const st=o.state||'';
 const bar=st==='standby'?tf('ow_bar',o.name||'?'):st==='free'?t('ow_free'):st==='asking'?tf('ow_asking',o.ask_s||0):'';
 $('#ownbar').hidden=!bar;$('#ownbar_t').textContent=bar;
 $('#ownbar button').hidden=st==='asking';
 $('#ow_dot').className='dot'+(st==='mine'?' ok':st?' warn':'');
 $('#ow_sub').textContent=st==='mine'?tf('ow_mine',o.me||''):st==='standby'?tf('ow_standby',o.name||'?')
  :st==='asking'?tf('ow_asking',o.ask_s||0):st==='free'?t('ow_free'):st==='legacy'?t('ow_legacy'):t('ow_unknown');
 $('#ow_btn').hidden=st==='mine'||st==='legacy'||st==='asking'}
function ownClaim(){const o=(lastD||{}).owner||{};
 if(o.state==='standby'&&!confirm(tf('ow_q',o.name||'?')))return;
 toast(t('ow_claiming'));
 fetch(API+'/board/claim',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'}).then(r=>r.json())
  .then(d=>{toast(d.ok?t('ow_done'):('ow_why_'+d.why) in T.zh?t('ow_why_'+d.why):t('ow_fail')+(d.why||''));tick()})
  .catch(e=>toast(t('ow_fail')+e));setTimeout(tick,4000)}
// card fonts: baked on this Mac, pushed over Wi-Fi when the card lacks one
function fontPaint(d){
 const f=d.fonts||{},s=f.state||'',sd=d.sd||{},e=$('#s_fonts');delete e.dataset.t;
 let txt,dot='warn';
 if(s==='ok'){txt=tf('fn_ok',f.n||4);dot='ok'}
 else if(s==='nocard'||sd.mb===0)txt=t('fn_nocard');
 else if(s==='checking')txt=t('fn_checking');
 else if(s==='baking')txt=tf('fn_baking',f.n);
 else if(s==='pushing')txt=tf('fn_pushing',f.face,f.i,f.n,f.pct);
 else if(s==='nopil')txt=t('fn_nopil');
 else if(s==='error')txt=tf('fn_err',f.why||'?');
 else{txt=t('fn_wait');dot=''}
 e.textContent=txt;$('#s_fdot').className='dot '+dot;
 const b=$('#s_fbtn');b.hidden=s==='checking'||s==='baking'||s==='pushing'||!(d.boards>0)||!sd.mb;
 b.textContent=t(s==='ok'?'fn_redo':'fn_retry')}
function fontRedo(){const all=((lastD||{}).fonts||{}).state==='ok';
 if(all&&!confirm(t('fn_q')))return;
 fetch(API+'/fonts/push'+(all?'?force=1':'')).then(r=>r.json())
  .then(d=>{toast(d.ok?t('fn_started'):t('fn_busy'));tick()}).catch(e=>toast(String(e)))}
function status(d){
 lastD=d;ownPaint(d.owner);
 const h=d.host||{};
 const hl=(h.product||'?')+' '+(h.version||'?')+' · pid '+(h.pid==null?'?':h.pid);
 const hs=tf('hup',h.up_min==null?'?':h.up_min,h.rss_mb==null?'?':h.rss_mb);
 $('#s_host').textContent=hl+' · '+hs;
 $('#sb_ver').textContent=(h.product||'?').toUpperCase()+' · '+(h.version||'?');
 $('#sb_host').textContent='host · pid '+(h.pid==null?'?':h.pid);$('#sb_hostsub').textContent=hs;
 [$('#s_host'),$('#sb_hostsub'),$('#s_board'),$('#sb_board'),$('#sb_boardsub'),$('#npnow')].forEach(e=>delete e.dataset.t);
 $('#sb_hdot').className='dot ok';
 const bl=boardLine(d);
 $('#s_bdot').className='dot '+bl[0];$('#sb_bdot').className='dot '+bl[0];
 $('#s_board').textContent=bl[1]+(bl[2]?' · '+bl[2]:'');
 $('#sb_board').textContent=t('board')+' · '+bl[3];$('#sb_boardsub').textContent=bl[2]||'';
 const fw=d.fw||{};$('#s_fw').textContent=fw.build?(tf('fw',fw.build)+(fw.part?' · '+fw.part:'')):'';
 fontPaint(d);
 const box=$('#s_seats');box.textContent='';const hp=$('#hd_pills');hp.textContent='';
 let noff=0;
 (d.seats||[]).forEach(a=>{
  const st=(d.agents||{})[a]||'off';
  box.appendChild(pillEl(st,seatNm(a)+' '+stNm(st),a===d.active));
  if(st==='off')noff++;else hp.appendChild(pillEl(st,seatNm(a)+' '+stNm(st),false));
  const sp=document.querySelector('.seat[data-a="'+a+'"] .pill.st');
  const nh=(d.installed||{})[a]===false&&st==='off';
  if(sp){sp.className='pill st '+st;sp.textContent='';sp.appendChild(el('i'));sp.appendChild(document.createTextNode(nh?t('not_here'):stNm(st)))}});
 if(noff)hp.appendChild(pillEl('off',tf('n_off',noff),false));
 if(built)paintSkins(d);
 $('#npnow').textContent=!d.np_enabled?t('np_off'):
  (d.np_title?(d.np_play?'▶ ':'⏸ ')+d.np_title+(d.np_app?' · '+d.np_app:''):t('np_none'))}
// ---- the form: filled on load and after a save, never on a poll ----
let built=false;
function fill(d){
 const seats=d.seats||[];
 if(!built){SKINS=d.skin_names||[];buildCards(seats);built=true;paintSkins(d)}
 if(d.lang&&d.lang!==L){L=d.lang;applyLang()}
 document.querySelectorAll('[name=vs]').forEach(r=>r.checked=r.value===d.voice_style);
 document.querySelectorAll('[name=ml]').forEach(r=>r.checked=r.value===d.mic_link);
 document.querySelectorAll('[name=pk]').forEach(r=>r.checked=r.value===d.pickup_page);
 $('#ff').checked=!!d.focus_follow;$('#fb').checked=!!d.follow_front;
 $('#so').checked=!!d.show_off_seats;
 $('#st').value=d.stretch_after_min;$('#fs').value=d.focus_settle_s;
 $('#np').checked=!!d.np_enabled;
 $('#msd').value=d.mic_sink_device||'';$('#mts').value=d.mic_tail_s;
 $(d.voice_source==='board'?'#vs_board':'#vs_mac').checked=true;
 $('#vs_board').disabled=!d.mic_sink_ok;
 $('#vs_board_l').className=d.mic_sink_ok?'':'off';
 tx($('#vs_hint'),d.mic_sink_ok?'mic_hint_ok':'mic_hint_no');
 seats.forEach(a=>{
  [['focus_apps',d.focus_apps],['focus_keys',d.focus_keys],
   ['approve_keys',d.approve_keys],['reject_keys',d.reject_keys]].forEach(p=>{
   const e=document.querySelector('[data-k="'+p[0]+'"][data-a="'+a+'"]');
   if(e){e.value=(p[1]||{})[a]||'';if(p[0]==='focus_apps')mirrorApp(e)}});
  const c=document.querySelector('[data-cpu="'+a+'"]');
  if(c){const v=(d.cpu_working||{})[a];c.value=(v==null)?'':v}});
 $('#pr').value=JSON.stringify(d.proc_rules||{},null,2);
 clean()}
// ---- host section ----
function restart(){
 if(!confirm(t('restart_q')))return;
 fetch(API+'/host/restart',{method:'POST'}).then(r=>r.json()).then(()=>{
  toast(t('restarting'));
  let n=0;
  const tm=setInterval(()=>{
   n++;
   fetch(API+'/state',{cache:'no-store'}).then(r=>r.json()).then(d=>{
    clearInterval(tm);toast(tf('restarted',d.host_version||''))})
    .catch(()=>{if(n>=9){clearInterval(tm);toast(t('restart_lost'))}})},1000)})
 .catch(()=>toast(t('restart_fail')))}
function logLoad(){
 const n=+$('#logn').value||60;
 fetch(API+'/log/tail?n='+n).then(r=>r.json()).then(d=>{
  const box=$('#logbox');delete box.dataset.t;
  box.textContent=(d.lines||[]).join('\\n')||t('log_empty');
  box.scrollTop=box.scrollHeight}).catch(e=>{const box=$('#logbox');delete box.dataset.t;box.textContent=t('log_fail')+e})}
function logToggle(s){s.classList.toggle('open');if(s.classList.contains('open'))logLoad()}
// ---- the rest of the buttons ----
function useFront(a){fetch(API+'/front').then(r=>r.json()).then(d=>{
 const nm=d.name||'';
 if(!nm){toast(t('front_none'));return}
 const e=document.querySelector('[data-k="focus_apps"][data-a="'+a+'"]');
 e.value=nm;mirrorApp(e);touch();
 toast((d.names||[]).length>1?tf('front_many',d.names.join(' / ')):tf('front_one',nm))})
 .catch(e=>toast(t('front_fail')+e))}
document.querySelectorAll('[name=pk]').forEach(r=>r.onchange=()=>hit('/test/pickup/'+r.value));
$('#vol').oninput=e=>{$('#volv').textContent=e.target.value};
$('#vol').onchange=e=>{hit('/test/volume/'+e.target.value)};
function doSay(){const v=$('#sayt').value.trim();
 if(v)hit('/test/say/'+encodeURIComponent(v))}
$('#sayt').addEventListener('keydown',e=>{if(e.key==='Enter')doSay()});
const auStep=s=>(('austep_'+s) in T.zh)?t('austep_'+s):s;
// ---- 扫描本机: the host looks, the page only shows and picks ----
function scLoad(say){fetch(API+'/agents/scan').then(r=>r.json()).then(d=>{
 const box=$('#sclist');box.textContent='';
 if(!d.rows.length){box.appendChild(el('div','au',t('sc_none')));return}
 d.rows.forEach(r=>{const row=el('div','au');
  const dot=el('i','dot');dot.style.background=r.seat?(SEATCOL[r.seat]||'#888'):'rgba(255,255,255,.18)';row.appendChild(dot);
  const nm=el('span','t',r.name);
  if(r.build)nm.appendChild(el('span','mono t3',' '+t(r.build==='cn'?'sc_cn':'sc_intl')));
  row.appendChild(nm);
  const how=r.how?(r.wired?t('sc_how_'+r.how):t('sc_miss_'+r.how)):t('sc_how_none');
  const m=el('span','m',(r.seat?seatNm(r.seat):t('sc_noseat'))+' · '+t(r.running?'sc_run':'sc_inst')+' · '+how);
  if(r.how&&!r.wired)m.style.color='#ffd60a';
  row.appendChild(m);
  if(r.current){const pl=el('span','pill working');pl.appendChild(el('i'));pl.appendChild(document.createTextNode(t('sc_using')));row.appendChild(pl)}
  else if(r.seat&&r.app){const b=el('button','sm',t('sc_use'));b.onclick=()=>scUse(r.seat,r.app);row.appendChild(b)}
  else if(r.seat&&!r.app)row.appendChild(el('span','m',t('sc_cli')));
  box.appendChild(row)});
 if(say)toast(t('sc_btn')+' ✓')}).catch(e=>toast(String(e)))}
function agCopy(){const s=t('ag_prompt');
 (navigator.clipboard?navigator.clipboard.writeText(s):Promise.reject()).then(()=>toast(t('ag_copied')))
  .catch(()=>{const a=el('textarea');a.value=s;document.body.appendChild(a);a.select();document.execCommand('copy');a.remove();toast(t('ag_copied'))})}
function scUse(seat,app){fetch(API+'/agents/use',{method:'POST',body:JSON.stringify({seat:seat,app:app})})
 .then(r=>r.json()).then(d=>{if(!d.ok){toast(d.why||'✗');return}toast(tf('sc_used',app));
  scLoad();fetch(API+'/settings/data').then(r=>r.json()).then(fill)})}
// ---- board Wi-Fi: the list lives on the board, this page only asks ----
let wfLast=null;
function wfWhy(w){return ('wf_why_'+w) in T.zh?t('wf_why_'+w):w==='offline'?t('wf_offline'):t('wf_fail')+(w||'')}
function wfPaint(d){const box=$('#wflist');wfLast=d;
 if(!d.nets){$('#wf_dot').className='dot';$('#wf_nowsub').textContent=d.why==='offline'?t('wf_offline'):wfWhy(d.why);box.innerHTML='';return}
 $('#wf_dot').className='dot'+(d.cur?' ok':'');
 $('#wf_nowsub').textContent=d.cur?tf('wf_cur',d.cur,d.rssi,d.ip):t('wf_none');
 box.innerHTML=d.nets.map(n=>'<div class="au"><span class="t">'+esc(n.s)
  +(n.s===d.cur?' <span class="pill working" style="margin-left:8px"><i></i>'+esc(t('wf_using'))+'</span>':'')+'</span>'
  +'<span class="m">'+esc(n.seen?tf('wf_seen',n.seen):t('wf_unseen'))+(n.fac?' · '+esc(t('wf_fac')):'')+'</span>'
  +(n.fac?'':'<button class="danger sm" data-s="'+esc(n.s)+'" onclick="wfRm(this.dataset.s)">'+esc(t('wf_rm'))+'</button>')
  +'</div>').join('');
 const nr=$('#wfnear');nr.textContent='';
 if(!(d.near||[]).length)nr.appendChild(el('span','val',t('wf_near_none')));
 (d.near||[]).forEach(n=>{const b=el('button','sm',n.s+'  '+n.r);b.type='button';
  b.onclick=()=>{$('#wfs').value=n.s;$('#wfp').focus()};nr.appendChild(b)});
 clearTimeout(wfPaint.tm);if(d.scan)wfPaint.tm=setTimeout(wfLoad,3000)}
function wfLoad(){fetch(API+'/board/wifi').then(r=>r.json()).then(wfPaint).catch(e=>wfPaint({why:String(e)}))}
function wfScan(){toast(t('wf_scanning'));fetch(API+'/board/wifi/scan').then(r=>r.json()).then(wfPaint)}
function wfPost(o){return fetch(API+'/board/wifi',{method:'POST',body:JSON.stringify(o)}).then(r=>r.json())}
function wfAdd(){const s=$('#wfs').value.trim(),p=$('#wfp').value;if(!s)return;
 if(/5\\s*g\\b/i.test(s)&&!confirm(t('wf_5g_q')))return;
 wfPost({op:'add',ssid:s,pass:p}).then(d=>{
  if(!d.ok){toast(wfWhy(d.why));return}
  toast(t('wf_saved'));$('#wfs').value='';$('#wfp').value='';wfPaint(d);
  setTimeout(wfLoad,8000);setTimeout(wfLoad,16000)})}
function wfRm(s){if(!confirm(tf('wf_rm_q',s)))return;
 wfPost({op:'rm',ssid:s}).then(d=>{toast(d.ok?t('wf_rmd'):wfWhy(d.why));if(d.nets)wfPaint(d)})}
function esc(s){const d=document.createElement('div');d.textContent=s==null?'':s;return d.innerHTML}
function fmtd(s){s=Math.round(+s||0);if(!s)return '—';if(s<60)return tf('dur_s',s);
 const m=Math.floor(s/60);
 return m>=60?tf('dur_hm',Math.floor(m/60),m%60):tf('dur_m',m)}
function auMsg(d){const j=d.job;
 if(j)return auStep(j.step)+': '+(j.title||j.name)
  +((j.pct!=null&&(j.step=='download'||j.step=='push'))?' '+j.pct+'%':'')+' · '+j.elapsed_s+' s'
  +(d.queue.length?' '+tf('au_more',d.queue.length):'');
 if(d.queue.length)return tf('au_queued',d.queue.length);
 if(d.pending.length)return tf(d.tcp?'au_pend_tcp':'au_pend_wait',d.pending.length);
 const last=(d.done||[])[0];
 if(last&&!last.ok)return tf('au_lastfail',last.why||'');
 return t(d.tcp?'au_idle':'au_idle_off')}
function auAdd(){const v=$('#au').value.trim();if(!v)return;
 const q=v[0]=='/'||v[0]=='~'?'file='+encodeURIComponent(v)
  :'url='+encodeURIComponent(v)+'&n='+$('#aun').value;
 fetch(API+'/audio/add?'+q).then(r=>r.json()).then(d=>{toast(d.ok?t('au_added'):(d.why||t('au_addfail')));
  if(d.ok)$('#au').value='';auTick()})}
function auRm(n){if(!confirm(tf('au_rm_q',n)))return;
 fetch(API+'/audio/rm?id='+encodeURIComponent(n)).then(r=>r.json())
  .then(d=>{toast(d.ok?t('au_rmd'):(d.why||t('au_rmfail')));auList();auTick()})}
function auList(){fetch(API+'/audio/list').then(r=>r.json()).then(d=>{
 $('#aulist').innerHTML=d.items.length?d.items.map(x=>
  '<div class="au"><span class="t">'+esc(x.title||x.name)+'</span>'
  +'<span class="m">'+esc(showNm(x.show))+(x.show?' · ':'')+fmtd(x.dur)
  +' · '+(x.pushed?t('au_pushed'):t('au_pending'))+'</span>'
  +'<button class="sm" onclick="auRm(&quot;'+esc(x.name)+'&quot;)">'+esc(t('del'))+'</button></div>').join('')
  :'<div class="au"><span class="m">'+esc(t('au_empty'))+'</span></div>'})}
// sidecars written before the switch (or in the other language) keep their word
const showNm=s=>(s==='本地文件'||s==='Local file')?t('local_file'):s;
let auBusy=null,auN=0;
function auTick(){fetch(API+'/audio/status').then(r=>r.json()).then(d=>{
 const a=$('#aust');delete a.dataset.t;a.textContent=auMsg(d);
 if(d.busy!==auBusy||auN%4===0){auBusy=d.busy;auList()}
 auN++}).catch(()=>{})}
auTick();setInterval(auTick,3000);
// ---- host memory trend: its own slow timer (30 s), the line is SVG ----
function memTick(){fetch(API+'/host/mem').then(r=>r.json()).then(d=>{
 const pts=d.pts||[];
 const fmt=v=>v==null?'—':v.toFixed(1);
 $('#mem_now').textContent=fmt(d.now_mb)+' MB';$('#mem_start').textContent=fmt(d.start_mb)+' MB';
 const sl=$('#mem_slope');
 const lab=sl.nextElementSibling;
 if(d.slope_mb_h!=null){sl.textContent=(d.slope_mb_h>=0?'+':'')+d.slope_mb_h.toFixed(2)+' MB';sl.className=d.slope_mb_h>=2?'warn':'';tx(lab,'mem_hr')}
 else if(d.delta_mb!=null){sl.textContent=(d.delta_mb>=0?'+':'')+d.delta_mb.toFixed(1)+' MB';sl.className='';delete lab.dataset.t;lab.textContent=tf('mem_recent',Math.max(1,Math.round((d.span_s||0)/60)))}
 else{sl.textContent='—';sl.className='';tx(lab,'mem_hr')}
 $('#mem_thr').textContent=d.threads==null?'—':d.threads;
 const ms=$('#memsub');delete ms.dataset.t;
 ms.textContent=tf('mem_sub_f',d.tick_s||30,Math.floor((d.up_s||0)/60),pts.length);
 if(pts.length<2){$('#memline').setAttribute('points','');return}
 const xs=pts.map(p=>p[0]),ys=pts.map(p=>p[1]);
 const x0=xs[0],x1=Math.max(xs[xs.length-1],x0+1);
 let y0=Math.min(...ys),y1=Math.max(...ys);if(y1-y0<4){const m=(y0+y1)/2;y0=m-2;y1=m+2}
 $('#memline').setAttribute('points',pts.map(p=>((p[0]-x0)/(x1-x0)*316+2).toFixed(1)+','+(44-(p[1]-y0)/(y1-y0)*40+2).toFixed(1)).join(' '))})
 .catch(()=>{})}
memTick();setInterval(memTick,30000);
// ---- save: one POST writes every config.json key on the page ----
function seatMap(k){const o={};
 document.querySelectorAll('[data-k="'+k+'"]').forEach(e=>{o[e.dataset.a]=e.value.trim()});
 return o}
function cpuMap(){const o={};
 document.querySelectorAll('[data-cpu]').forEach(e=>{
  if(e.value.trim()!=='')o[e.dataset.cpu]=+e.value});
 return o}
function save(){
 let pr;
 try{pr=JSON.parse($('#pr').value.trim()||'{}')}
 catch(e){toast(t('bad_json'));return}
 const body={voice_style:(document.querySelector('[name=vs]:checked')||{}).value||'chirp',
  focus_follow:$('#ff').checked,follow_front:$('#fb').checked,
  show_off_seats:$('#so').checked,
  mic_link:(document.querySelector('[name=ml]:checked')||{}).value||'ble',
  pickup_page:(document.querySelector('[name=pk]:checked')||{}).value||'',
  now_playing:$('#np').checked,
  focus_settle_s:+$('#fs').value||1.5,
  stretch_after_min:+$('#st').value||0,
  focus_apps:seatMap('focus_apps'),focus_keys:seatMap('focus_keys'),
  approve_keys:seatMap('approve_keys'),reject_keys:seatMap('reject_keys'),
  proc_rules:pr,cpu_working:cpuMap(),
  voice_source:$('#vs_board').checked?'board':'mac',
  mic_sink_device:$('#msd').value,mic_tail_s:+$('#mts').value};
 fetch(API+'/settings/save',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify(body)}).then(r=>r.json()).then(d=>{
   if(!d.ok){toast(d.why||t('not_saved'));return}
   toast(tf('saved',(d.keys||[]).length,(d.keys||[]).join(' ')));
   return fetch(API+'/settings/data').then(r=>r.json()).then(fill)})
  .catch(e=>toast(t('save_fail')+e))}
document.addEventListener('keydown',e=>{if((e.metaKey||e.ctrlKey)&&e.key==='s'){e.preventDefault();if(!$('#save').disabled)save()}});
// One timer for the whole page: 状态 wants 2 s, everything else rides along.
function tick(){fetch(API+'/settings/data').then(r=>r.json()).then(status).catch(()=>{})}
fetch(API+'/settings/data').then(r=>r.json()).then(d=>{fill(d);status(d)})
 .catch(e=>toast(t('no_host')+e));
setInterval(tick,2000);
</script></body></html>"""

def settings_html():
    """The page with the current language baked in (no flash of the other one
    before /settings/data lands) and the whole T table for live switching."""
    return (SETTINGS_HTML.replace("__LANG__", LANG)
            .replace("__TITLE__", SETTINGS_T[LANG]["title"])
            .replace("__LANG_JSON__", json.dumps(LANG))
            .replace("__T_JSON__", json.dumps(SETTINGS_T, ensure_ascii=False)))

SEAT_TEXT_KEYS = ("focus_apps",)                                  # free text, any app name
SEAT_KEY_KEYS = ("focus_keys", "approve_keys", "reject_keys")     # must parse as a key spec

def seat_map_clean(raw, keys_are_specs):
    """One of the per-seat config dicts off the settings page -> what we store,
    or (None, bad_spec). Unknown seats are dropped (the page only ever sends
    AGENTS), values are stripped, and an empty value is kept as "" because
    that is how a person says "press nothing here"."""
    out = {}
    if not isinstance(raw, dict):
        return out, None
    for seat in AGENTS:
        if seat not in raw:
            continue
        val = raw[seat]
        if not isinstance(val, str):
            continue
        val = val.strip()
        if val and keys_are_specs and not key_spec_ok(val):
            return None, val
        out[seat] = val
    return out, None

def proc_rules_clean(raw):
    """The 高级 JSON textarea. Each seat gets {"match": regex, "exclude": regex
    or null}; both regexes are compiled here so a bad one is a refusal, not a
    scan_processes() traceback every second."""
    if not isinstance(raw, dict):
        return None, tr("进程识别规则要是一个 JSON 对象", "Process rules must be a JSON object")
    out = {}
    for seat, rule in raw.items():
        if seat not in AGENTS:
            return None, tr("没有这个席位：%s", "No such seat: %s") % seat
        if not isinstance(rule, dict) or not isinstance(rule.get("match"), str):
            return None, tr("%s 缺 match", "%s is missing match") % seat
        exclude = rule.get("exclude")
        if exclude is not None and not isinstance(exclude, str):
            return None, tr("%s 的 exclude 要是正则或 null", "%s: exclude must be a regex or null") % seat
        for pat in (rule["match"], exclude):
            if pat is None:
                continue
            try:
                re.compile(pat)
            except re.error as e:
                return None, tr("%s 的正则不对：%s", "%s: bad regex: %s") % (seat, e)
        out[seat] = {"match": rule["match"], "exclude": exclude}
    return out, None

def cpu_working_clean(raw):
    """{seat: percent}. A seat left out has no CPU rule at all (qoderwork never
    had one); a number out of 0-100 is a typo, not a threshold."""
    if not isinstance(raw, dict):
        return None, tr("CPU 阈值要是一个 JSON 对象", "CPU thresholds must be a JSON object")
    out = {}
    for seat, pct in raw.items():
        if seat not in AGENTS:
            return None, tr("没有这个席位：%s", "No such seat: %s") % seat
        if isinstance(pct, bool) or not isinstance(pct, (int, float)):
            return None, tr("%s 的 CPU 阈值不是数字", "%s: CPU threshold is not a number") % seat
        if not 0 <= pct <= 100:
            return None, tr("%s 的 CPU 阈值要在 0-100 之间", "%s: CPU threshold must be 0-100") % seat
        out[seat] = float(pct)
    return out, None

def test_lang_msg(lang):
    """/test/lang/<x> -> the one message it pushes (None = refused)."""
    return {"t": "cfg", "lang": lang} if lang in LANGS else None

def test_lang(lang):
    msg = test_lang_msg(lang)
    if msg is None:
        return {"ok": False, "why": "lang must be one of %s" % "/".join(LANGS)}
    push_msg(msg)
    return {"ok": True, "sent": msg, "config_lang": LANG}

def save_settings(patch):
    """Settings page save: merge whitelisted keys into config.json (other
    hand-edited keys survive), reload overrides, push the board bits.

    Returns {"ok": True, "keys": [...]} -- the page names the keys it wrote in
    its toast -- or {"ok": False, "why": "..."} with nothing written. Every
    refusal is checked BEFORE the file is opened: a page that sends one bad
    key spec must not leave half a save behind."""
    vs = patch.get("voice_style")
    clean = {}
    if vs in ("chirp", "tts"):
        clean["voice_style"] = vs
    if isinstance(patch.get("focus_follow"), bool):
        clean["focus_follow"] = patch["focus_follow"]
    if isinstance(patch.get("follow_front"), bool):
        clean["follow_front"] = patch["follow_front"]
    if isinstance(patch.get("focus_settle_s"), (int, float)) and not isinstance(patch.get("focus_settle_s"), bool):
        clean["focus_settle_s"] = max(0.2, min(5.0, float(patch["focus_settle_s"])))
    if patch.get("mic_link") in MIC_LINKS:
        clean["mic_link"] = patch["mic_link"]
    if isinstance(patch.get("now_playing"), bool):
        clean["now_playing"] = patch["now_playing"]
    if isinstance(patch.get("show_off_seats"), bool):
        clean["show_off_seats"] = patch["show_off_seats"]
    if patch.get("voice_source") in ("mac", "board"):
        clean["voice_source"] = patch["voice_source"]
    if isinstance(patch.get("mic_sink_device"), str) and patch["mic_sink_device"].strip():
        clean["mic_sink_device"] = patch["mic_sink_device"].strip()
    if isinstance(patch.get("mic_tail_s"), (int, float)) and not isinstance(patch.get("mic_tail_s"), bool):
        clean["mic_tail_s"] = max(0.0, min(5.0, float(patch["mic_tail_s"])))
    if patch.get("pickup_page") in PICKUP_PAGES:
        clean["pickup_page"] = patch["pickup_page"]
    if patch.get("lang") in LANGS:
        clean["lang"] = patch["lang"]
    try:
        clean["stretch_after_min"] = max(0, min(600, int(patch.get("stretch_after_min"))))
    except (TypeError, ValueError):
        pass
    for name in SEAT_TEXT_KEYS + SEAT_KEY_KEYS:
        if name not in patch:
            continue
        got, bad = seat_map_clean(patch[name], name in SEAT_KEY_KEYS)
        if bad is not None:
            return {"ok": False, "why": tr("按键写法不对：%s", "Bad key spec: %s") % bad}
        if got:
            clean[name] = got
    for name, fn in (("proc_rules", proc_rules_clean), ("cpu_working", cpu_working_clean)):
        if name not in patch:
            continue
        got, why = fn(patch[name])
        if why:
            return {"ok": False, "why": why}
        if name == "proc_rules" and got is not None:   # defaults stay out of config.json
            got = {a: r for a, r in got.items() if r != DEFAULT_PROC_RULES.get(a)}
            clean[name] = got
        elif got:
            clean[name] = got
    cfg = {}
    if CONFIG_OVERRIDE.exists():
        try:
            cfg = json.loads(CONFIG_OVERRIDE.read_text())
        except ValueError:
            log("settings: config.json unreadable, refusing to overwrite")
            return {"ok": False, "why": tr("config.json 读不了，没有覆盖它", "config.json is unreadable; left it alone")}
    # per-seat maps merge: a patch naming one seat (an agent following
    # /agent-setup) must not wipe what the other seats had saved
    for name in SEAT_TEXT_KEYS + SEAT_KEY_KEYS:
        if name in clean and isinstance(cfg.get(name), dict):
            clean[name] = {**cfg[name], **clean[name]}
    cfg.update(clean)
    CONFIG_OVERRIDE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_OVERRIDE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n")
    load_overrides()
    push_msg(cfg_msg())
    np_apply()                       # now_playing flipped: start or stop the listener
    threading.Thread(target=prime_voices, daemon=True).start()
    log("settings saved:", clean)
    return {"ok": True, "keys": sorted(clean)}

# ------------------------------------------------------------------ HTTP (hooks + debug)
def state_snapshot():
    """The /state JSON as a dict. Takes `lock`: the agent tables are shared with
    the watcher. product/host_version identify this host."""
    with lock:
        return {"product": PRODUCT, "host_version": HOST_VERSION, "lang": LANG,
                "agents": dict(state), "active": active_agent,
                "claude_sessions": len(claude_sessions),
                "boards": len(boards), "ble": _ble_client is not None,
                "sd": dict(board_sd),
                "rtc": dict(board_rtc, boot_hello=board_rtc_boot),
                "fw": {"build": board_build, "part": board_part},
                "board": board_snapshot(),
                "np": np_brief(),
                "probe": {"seen": dict(proc_seen), "cpu": dict(proc_cpu),
                          "busy": dict(cpu_busy)}}

class HookHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _ok(self, body=b"ok"):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/settings":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(settings_html().encode())
        elif self.path == "/settings/data":
            # The settings page polls this every 2 s (one timer for the whole
            # page). The read-only blocks -- host / board / agents -- feed the
            # 状态 section; everything else is a config.json key the page can
            # write back through /settings/save.
            snap = state_snapshot()
            self._ok(json.dumps({
                "host": host_brief(),
                "board": snap["board"],
                # S3's main link is Wi-Fi TCP, BLE is the fallback:
                # the 板子 row says which one is carrying it.
                "boards": snap["boards"],
                "ble": snap["ble"],
                "fw": snap["fw"],
                "sd": snap["sd"],
                "fonts": dict(font_rt),
                "agents": snap["agents"],
                "active": snap["active"],
                "seats": list(AGENTS),
                "installed": seats_installed(),
                "owner": owner_snapshot(),
                "skins": dict(board_skins),       # {} until a board said hello
                "skin_names": list(SKIN_NAMES),
                "lang": LANG,
                "voice_style": VOICE_STYLE,
                "focus_follow": FOCUS_FOLLOW,
                "follow_front": FOLLOW_FRONT,
                "focus_settle_s": FOCUS_SETTLE_S,
                "focus_apps": dict(FOCUS_APPS),
                "focus_keys": {a: FOCUS_KEYS.get(a, "") for a in AGENTS},
                "approve_keys": {a: APPROVE_KEYS.get(a, "") for a in AGENTS},
                "reject_keys": {a: REJECT_KEYS.get(a, "") for a in AGENTS},
                "proc_rules": dict(PROC_RULES),
                "cpu_working": dict(CPU_WORKING),
                "voice_source": VOICE_SOURCE,
                "mic_link": MIC_LINK,
                "mic_sink_device": MIC_SINK_DEVICE,
                "mic_tail_s": MIC_TAIL_S,
                "mic_sink_ok": MIC_SINK_BIN.exists(),   # no helper -> the board mic option greys out
                "stretch_after_min": STRETCH_AFTER_MIN,
                "pickup_page": PICKUP_PAGE or "",
                "show_off_seats": SHOW_OFF_SEATS,
                "np_enabled": NOW_PLAYING,
                "np_title": np_brief()["title"],
                "np_app": np_brief()["app"],
                "np_play": (np_rt["msg"] or {}).get("play", 0),
            }, ensure_ascii=False).encode())
        elif self.path in ("/agent-setup", "/agent-setup.md"):
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.end_headers()
            self.wfile.write(agent_setup_md().encode())
        elif self.path == "/agents/scan":
            self._ok(json.dumps({"rows": scan_agents()}, ensure_ascii=False).encode())
        elif self.path in ("/board/wifi", "/board/wifi/scan"):
            r = board_wifi("scan" if self.path.endswith("/scan") else "list")
            self._ok(json.dumps(r, ensure_ascii=False).encode())
        elif self.path.startswith("/test/claim"):
            # /test/claim[?name=..]: a made-up Mac asks for the board over our
            # own link, so the claim card can be photographed (/test/shot)
            # without a second Mac. Not tapping = it times out in 30 s and
            # nothing changes; TAPPING CONNECT HANDS THE BOARD TO NOBODY (then
            # `pet claim` here to take it back).
            q = dict(parse_qsl(self.path.split("?", 1)[1])) if "?" in self.path else {}
            _test_claim["until"] = time.time() + 40
            msg = {"t": "claim", "id": "test-" + str(_new_rid()), "name": q.get("name", "测试用的 MacBook Air"),
                   "mdns": q.get("mdns", "Test-MacBook-Air"), "ip": "", "port": TCP_PORT}
            push_board(msg)
            self._ok(json.dumps({"ok": True, "sent": msg}, ensure_ascii=False).encode())
        elif self.path == "/board/owner":
            self._ok(json.dumps(owner_snapshot(), ensure_ascii=False).encode())
        elif self.path == "/front":
            # The seat cards' 「用当前前台」 button. _frontmost_app() answers
            # "bundle|localized" out of a set, so sort the pieces: the page
            # fills the first and shows the rest, and the same app always
            # offers them in the same order. (/test/front below is the focus
            # insurance's own view -- target seat, follow candidate -- and
            # stays as it is.)
            raw = _frontmost_app()
            names = sorted({seg.strip() for seg in raw.split("|") if seg.strip()})
            self._ok(json.dumps({"front": raw, "names": names,
                                 "name": names[0] if names else ""},
                                ensure_ascii=False).encode())
        elif self.path.startswith("/log/tail"):
            # /log/tail?n=60 -- the tail of OUR log, for the settings page's
            # host section. No path parameter, ever: see LOG_PATH.
            q = dict(parse_qsl(self.path.partition("?")[2]))
            lines = log_tail(q.get("n", 60))
            self._ok(json.dumps({"path": str(LOG_PATH), "n": len(lines),
                                 "lines": lines}, ensure_ascii=False).encode())
        elif self.path.startswith("/test/raw?"):
            # /test/raw?j=<urlencoded JSON object> — push one raw host->board line
            # (TCP if up, else BLE) for protocol work before a real endpoint
            # exists. Debug only; the board treats it like any host message.
            q = dict(kv.split("=", 1) for kv in self.path.split("?", 1)[1].split("&") if "=" in kv)
            try:
                obj = json.loads(unquote(q.get("j", "")))
                if not isinstance(obj, dict) or "t" not in obj:
                    raise ValueError("need a JSON object with a t field")
            except ValueError as e:
                self._ok(json.dumps({"ok": False, "why": str(e)}).encode())
                return
            push_board(obj)
            self._ok(json.dumps({"ok": True, "sent": obj}).encode())
        elif self.path.startswith("/test/np"):
            # /test/np                what the media-control listener sees right now
            # /test/np/push?title=..&artist=..&album=..&pos=..&dur=..&play=1&app=..&art=<abs image>
            #                         fake one np message (with a real cover push) for board shots
            q = dict(parse_qsl(self.path.split("?", 1)[1], keep_blank_values=True)) if "?" in self.path else {}
            rest = self.path.split("?", 1)[0][len("/test/np"):]
            if rest == "/push":
                fake = {"title": q.get("title") or "测试标题 Test Title",
                        "artist": q.get("artist", ""), "album": q.get("album", ""),
                        "elapsedTime": np_num(q.get("pos", 0)),
                        "duration": np_num(q.get("dur", 0)),
                        "playing": q.get("play", "1") not in ("0", "false", ""),
                        "playbackRate": np_num(q.get("rate", 1), 1.0),
                        "bundleIdentifier": q.get("bundle", "")}
                cover, cov = "", None
                art = q.get("art")
                if art:
                    try:
                        raw = Path(art).read_bytes()
                    except OSError as e:
                        cov = {"ok": False, "why": str(e)}
                    else:
                        h = np_cover_hash(raw)
                        np_cover_make(h, raw, "")
                        cover = np_cover_card(h)
                        cov = np_cover_push(h, force=q.get("force") == "1")
                msg = np_from_state(fake, cover)
                if q.get("app"):
                    msg["app"] = q["app"]
                hold = np_num(q.get("hold", 60), 60.0)
                np_rt["hold_until"] = time.time() + hold      # the real player must not overwrite the shot
                np_send(msg)
                log("np push (test) ->", msg.get("title", "")[:40], "hold %.0f s" % hold)
                self._ok(json.dumps({"sent": msg, "cover": cov, "hold_s": hold},
                                    ensure_ascii=False).encode())
            else:
                self._ok(json.dumps(np_snapshot(), ensure_ascii=False).encode())
        elif self.path.startswith("/audio/"):
            # 听什么: RSS / audio link / local file -> the card
            #   /audio/add?url=<feed or audio>[&n=1] | /audio/add?file=/abs/path
            #   /audio/status  /audio/list  /audio/rm?id=<name>
            q = dict(parse_qsl(self.path.split("?", 1)[1], keep_blank_values=True)) if "?" in self.path else {}
            what = self.path.split("?", 1)[0][len("/audio/"):]
            if what == "add":
                r = au_add(q.get("url", ""), q.get("file", ""), q.get("n", 1))
            elif what == "status":
                r = au_status()
            elif what == "list":
                r = {"ok": True, "items": au_list(), "dir": str(AUDIO_DIR)}
            elif what == "rm":
                r = au_rm(q.get("id", ""))
            else:
                r = {"ok": False, "why": "unknown /audio/%s" % what}
            self._ok(json.dumps(r, ensure_ascii=False).encode())
        elif self.path.startswith("/test/media/"):
            # /test/media/<toggle|next|prev> — drive the Mac's own player
            self._ok(json.dumps(media_cmd(self.path.rsplit("/", 1)[1]), ensure_ascii=False).encode())
        elif self.path.startswith("/test/view/"):
            # /test/view/<play|clock|report|face|almanac|calendar>[?s=20] — force a page for s seconds
            q = dict(parse_qsl(self.path.split("?", 1)[1], keep_blank_values=True)) if "?" in self.path else {}
            p = self.path.split("?", 1)[0].rsplit("/", 1)[1]
            if p not in ("np", "pod", "play", "clock", "face", "almanac", "calendar"):   # play = np alias; report retired 2026-09-09
                self._ok(json.dumps({"ok": False, "why": "unknown page %r" % p}).encode())
                return
            secs = int(np_num(q.get("s", 20), 20))
            push_board({"t": "view", "p": p, "s": secs})
            self._ok(json.dumps({"ok": True, "p": p, "s": secs}).encode())
        elif self.path.startswith("/test/play/"):
            # /test/play/<toggle|pause|next|prev|status|src> — the board's own player (wave 2);
            # src = what a double tap does (swap the shown source)
            cmd = self.path.rsplit("/", 1)[1]
            if cmd not in ("toggle", "pause", "next", "prev", "status", "back", "fwd"):
                self._ok(json.dumps({"ok": False, "why": "unknown cmd %r" % cmd}).encode())
                return
            rid = _new_rid()
            push_board({"t": "play", "cmd": cmd, "id": rid})
            r = wait_reply("plst", rid, 3)
            self._ok(json.dumps(r or {"ok": False, "why": "no plst"}, ensure_ascii=False).encode())
        elif self.path == "/test/qian":
            # speak today's fortune, exactly as tapping the almanac would
            threading.Thread(target=say_qian, daemon=True).start()
            self._ok()
        elif self.path == "/state":
            self._ok(json.dumps(state_snapshot()).encode())
        elif self.path.startswith("/host/mem"):
            # the settings page's memory trend line
            self._ok(json.dumps(mem_hist_body()).encode())
        elif self.path.startswith("/debug/mem"):
            # /debug/mem[?top=N] — rss, threads, gc by type, and (only when
            # ~/.agentpet/memdebug existed at start) tracemalloc growth per
            # line since boot. Pull it twice 30 min apart and diff.
            q = dict(parse_qsl(self.path.partition("?")[2]))
            try:
                top = max(1, min(200, int(q.get("top", 20))))
            except ValueError:
                top = 20
            self._ok(json.dumps(mem_snapshot(top)).encode())
        elif self.path == "/debug/threads":
            # Every thread's Python stack, for the day a thread goes quiet
            # (2026-09-17: mem_loop stopped printing after 17a and nothing on
            # the outside could say where it sat). Read-only, no side effects.
            import sys, traceback
            names = {t.ident: t.name for t in threading.enumerate()}
            out = {}
            for ident, frame in sys._current_frames().items():
                out[names.get(ident, str(ident))] = traceback.format_stack(frame)[-6:]
            self._ok(json.dumps(out, ensure_ascii=False, indent=1).encode())
        elif self.path.startswith("/test/select/"):
            # pretend the board swiped to <agent>: runs the real select path
            # (active seat, board lockout, debounced focus follow)
            handle_board_msg(json.dumps({"t": "select", "agent": self.path.rsplit("/", 1)[1]}).encode(),
                             lambda _b: None)
            self._ok(json.dumps({"active": active_agent}).encode())
        elif self.path == "/test/front":
            # what the focus insurance would see right now (fresh query)
            front = _frontmost_app()
            self._ok(json.dumps({"front": front,
                                 "target": FOCUS_APPS.get(active_agent),
                                 "active": active_agent,
                                 "follow_cand": front_agent(front),
                                 "follow_enabled": FOLLOW_FRONT}).encode())
        elif self.path.startswith("/test/mic"):
            # board microphone (mic-blackhole branch):
            #   /test/mic/start | /stop      stream on/off ({"t":"mic","on":N})
            #   /test/mic/ch/<0|1|2|3>       slot pick: 0 MIC1 (right edge) / 1 MIC2 (left edge) / 2 average /
            #                                3 auto by placement (default: sector 1 = right edge down -> MIC2)
            #   /test/mic/link/<auto|tcp|ble> force the frame link (ble with Wi-Fi up = test)
            #   /test/mic/level              live stats (fps, kbps, peak, rms, gaps, sink)
            #   /test/mic/rec?sec=5          record to ~/.agentpet/mic/rec_<ts>.wav (+latest.wav)
            #   /test/mic/latency            pet click -> mic -> host, 3 runs, median ms
            #   /test/mic/sink?on=1|0        manual route A: mic + sink + BlackHole as default input
            rest = self.path[len("/test/mic"):]
            q = dict(kv.split("=", 1) for kv in rest.split("?", 1)[1].split("&") if "=" in kv) if "?" in rest else {}
            rest = rest.split("?", 1)[0]
            if rest == "/start":
                _mic_reset()
                push_board({"t": "mic", "on": 1})
                time.sleep(0.4)
                self._ok(json.dumps(mic_snapshot()).encode())
            elif rest == "/stop":
                push_board({"t": "mic", "on": 0})
                time.sleep(0.4)
                self._ok(json.dumps(mic_snapshot()).encode())
            elif rest.startswith("/ch/"):
                push_board({"t": "mic", "ch": int(rest.rsplit("/", 1)[1])})
                time.sleep(0.3)
                self._ok(json.dumps(mic_snapshot()).encode())
            elif rest.startswith("/link/"):
                # force the stream's link for the next sessions: auto | tcp | ble
                push_board({"t": "mic", "link": rest.rsplit("/", 1)[1]})
                time.sleep(0.4)
                self._ok(json.dumps(mic_snapshot()).encode())
            elif rest == "/rec":
                self._ok(json.dumps(mic_record(min(60.0, float(q.get("sec", "5"))))).encode())
            elif rest == "/stall":
                # stop reading the board socket for ?sec= (default 3): the board's send
                # buffer fills; its loop must stay responsive and drop/skip instead
                # the Mac's receive buffer absorbs ~180 KB before the board feels
                # anything (15 s of stream), so shrink it for the stall: the board's
                # lwIP send buffer then fills within a second, as on a weak radio link
                secs = min(20.0, float(q.get("sec", "3")))
                with lock:
                    socks = list(boards)
                for c in socks:
                    try:
                        c.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, int(q.get("rcvbuf", "4096")))
                    except OSError as e:
                        log("stall: rcvbuf", e)
                _stall_until[0] = time.time() + secs
                def _restore():
                    time.sleep(secs + 0.2)
                    for c in socks:
                        try:
                            c.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 131072)
                        except OSError:
                            pass
                threading.Thread(target=_restore, daemon=True).start()
                self._ok(json.dumps({"stall_s": secs}).encode())
            elif rest == "/latency":
                self._ok(json.dumps(mic_latency(sound=q.get("sound", "tick"))).encode())
            elif rest == "/sink":
                if q.get("on", "1") != "0":
                    _mic_reset()
                    ok = mic_sink_begin()
                    push_board({"t": "mic", "on": 1})
                    if ok:
                        mic_sink_open_gate()
                    time.sleep(0.4)
                    r = mic_snapshot(); r["route"] = ok
                else:
                    push_board({"t": "mic", "on": 0})
                    mic_sink_end()
                    time.sleep(0.3)
                    r = mic_snapshot()
                self._ok(json.dumps(r).encode())
            else:
                self._ok(json.dumps(mic_snapshot()).encode())
        elif self.path.startswith("/test/ble/bw"):
            # BLE notify throughput board->Mac: ?sec=3&len=470&chunk=0&gap=0&itvl=0 (see ble_bandwidth)
            q = {}
            if "?" in self.path:
                for kv in self.path.split("?", 1)[1].split("&"):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        try: q[k] = int(v)
                        except ValueError: pass
            r = ble_bandwidth(q.get("sec", 3), q.get("len", 470), q.get("chunk", 0),
                              q.get("gap", 0), q.get("itvl", 0))
            self._ok(json.dumps(r).encode())
        elif self.path == "/test/rtt":
            # TCP round trip through the board's main loop: {"t":"ping",id} -> {"t":"pong",id}, 5 runs
            runs = []
            for _ in range(5):
                rid = _new_rid()
                t0 = time.time()
                push_tcp({"t": "ping", "id": rid})
                r = wait_reply("pong", rid, 2.0)
                runs.append(round((time.time() - t0) * 1000) if r else None)
                time.sleep(0.2)
            ok = sorted(x for x in runs if x is not None)
            self._ok(json.dumps({"rtt_ms": runs, "median_ms": ok[len(ok) // 2] if ok else None}).encode())
        elif self.path.startswith("/test/dictation"):
            # hold fn for ?sec= seconds (default 2.5) — dictation into the frontmost window
            hold = float(self.path.split("sec=", 1)[1].split("&")[0]) if "sec=" in self.path else 2.5
            def _demo():
                voice_key(True)
                time.sleep(min(hold, 30.0))
                voice_key(False)
            threading.Thread(target=_demo, daemon=True).start()
            self._ok()
        elif self.path == "/test/stretch":
            # board-side "break time ~" face + sigh, as the 90-min reminder would
            push_msg({"t": "stretch"})
            self._ok()
        elif self.path == "/test/profile":
            # board-side growth card (level/XP/stats), as a double-tap would
            push_msg({"t": "profile"})
            self._ok()
        elif self.path.startswith("/test/key/"):
            # inject a key combo locally: /test/key/enter, /test/key/cmd+z
            post_key(self.path.rsplit("/", 1)[1])
            self._ok()
        elif self.path in ("/test/approve", "/test/reject"):
            # as if the pet were tapped / long-pressed on a needs_you agent:
            # raises active_agent's app, presses its confirm/dismiss key
            decision_key(self.path.rsplit("/", 1)[1])
            self._ok()
        elif self.path.startswith("/test/say/"):
            # /test/say/<urlencoded text>[?fmt=pcm|ima]: synth (cached) + play on the
            # board; answers with timing (host push ms + the board's start/rx/play ms).
            # First-time phrases block ~2s on edge-tts; cached ones are instant.
            rest = self.path[len("/test/say/"):]
            q = ""
            if "?" in rest:
                rest, q = rest.split("?", 1)
            q = dict(kv.split("=", 1) for kv in q.split("&") if "=" in kv)
            text = unquote(rest).strip()
            w = tts_wav(text, tts_voice(q.get("lang")) if q.get("lang") in LANGS else None) if text else None
            res = (speak_push(w, fmt=q.get("fmt"), wait_stats=True) if w
                   else {"ok": False, "why": "tts failed"})
            self._ok(json.dumps(res, ensure_ascii=False).encode())
        elif self.path.startswith("/sd/queue?"):
            # /sd/queue?src=<local file>&dst=<card path> — push later, on the next TCP hello
            q = dict(kv.split("=", 1) for kv in self.path.split("?", 1)[1].split("&") if "=" in kv)
            src = unquote(q.get("src", "")); dst = unquote(q.get("dst", ""))
            if not (src and dst and Path(src).exists()):
                self._ok(json.dumps({"ok": False, "why": "need existing src and dst"}).encode())
                return
            n = push_queue_add(src, dst)
            with lock:
                has_tcp = bool(boards)
            if has_tcp:
                threading.Thread(target=push_queue_drain, args=(0.5,), daemon=True).start()
            self._ok(json.dumps({"ok": True, "queued": n, "now": has_tcp}).encode())
        elif self.path.startswith("/sd/push?"):
            # /sd/push?src=<local file>&dst=<card path>  (dst defaults to /agentpet/<basename>)
            q = dict(kv.split("=", 1) for kv in self.path.split("?", 1)[1].split("&") if "=" in kv)
            src = unquote(q.get("src", ""))
            dst = unquote(q.get("dst", "")) or ("/agentpet/" + Path(src).name)
            self._ok(json.dumps(push_file(src, dst), ensure_ascii=False).encode())
        elif self.path.startswith("/ota"):
            # /ota[?src=<.bin>]  (default ~/.agentpet/fw.bin: launchd cannot read
            # ~/Documents, so copy the build there first)  |  /ota?rollback=1
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            q = dict(kv.split("=", 1) for kv in q.split("&") if "=" in kv)
            if q.get("rollback"):
                res = board_ota(None, rollback=True)
            else:
                res = board_ota(unquote(q.get("src", "")) or str(Path.home() / ".agentpet" / "fw.bin"))
            self._ok(json.dumps(res).encode())
        elif self.path == "/fonts/status":
            self._ok(json.dumps(font_rt, ensure_ascii=False).encode())
        elif self.path.startswith("/fonts/push"):
            # card fonts by hand: missing faces only, ?force=1 = bake + push all
            # four again. Runs on its own thread (minutes); /fonts/status follows it.
            force = "force=1" in self.path
            if _font_job.locked():
                self._ok(json.dumps({"ok": False, "busy": True, **font_rt}).encode())
            else:
                threading.Thread(target=fonts_ensure, kwargs={"force": force}, daemon=True).start()
                self._ok(json.dumps({"ok": True, "started": True, "force": force}).encode())
        elif self.path.startswith("/sd/ls"):
            q = self.path.split("?", 1)[1] if "?" in self.path else "path=/"
            path = unquote(dict(kv.split("=", 1) for kv in q.split("&") if "=" in kv).get("path", "/"))
            self._ok(json.dumps(board_ls(path), ensure_ascii=False).encode())
        elif self.path.startswith("/sd/rm?"):
            q = dict(kv.split("=", 1) for kv in self.path.split("?", 1)[1].split("&") if "=" in kv)
            self._ok(json.dumps(board_rm(unquote(q.get("path", "")))).encode())
        elif self.path.startswith("/sd/cat?"):
            q = dict(kv.split("=", 1) for kv in self.path.split("?", 1)[1].split("&") if "=" in kv)
            self._ok(json.dumps(board_cat(unquote(q.get("path", ""))), ensure_ascii=False).encode())
        elif self.path == "/test/sd":
            # ask the board to (re)try the microSD and report; waits up to 4 s
            t0 = time.time()
            push_msg({"t": "sd?"})
            while time.time() - t0 < 4 and board_sd_at < t0:
                time.sleep(0.1)
            a = dict(board_sd)
            a["fresh"] = board_sd_at >= t0
            self._ok(json.dumps(a).encode())
        elif self.path.startswith("/test/gaze"):
            # 眼神追声: /test/gaze = the board's latest sound-direction
            # estimate (az -1..1, + = its right edge keys-up; conf; lag in samples; age ms;
            # n since boot; floor = room-noise estimate the level gate uses; look = the
            # smoothed eye offset; spk = own speaker gating).
            # /on | /off (NVS), /force?az=-1..1&ms=3000 (screenshot aid),
            # /tune?min=120&minc=0.55 (level / peak gates), /dbg/1|0 (serial line per
            # confident frame). Every form replies with the fresh status.
            rest = self.path[len("/test/gaze"):]
            q = dict(kv.split("=", 1) for kv in rest.split("?", 1)[1].split("&") if "=" in kv) if "?" in rest else {}
            rest = rest.split("?", 1)[0]
            msg = {"t": "gaze", "id": _new_rid()}
            if rest == "/on":
                msg["on"] = 1
            elif rest == "/off":
                msg["on"] = 0
            elif rest == "/force":
                msg["az"] = float(q.get("az", "1")); msg["ms"] = int(q.get("ms", "3000"))
            elif rest == "/tune":
                if "min" in q: msg["min"] = int(q["min"])
                if "minc" in q: msg["minc"] = float(q["minc"])
            elif rest.startswith("/dbg/"):
                msg["dbg"] = int(rest.rsplit("/", 1)[1])
            push_board(msg)
            r = wait_reply("gaze", msg["id"], 4)
            self._ok(json.dumps(r or {"ok": False, "why": "no answer in 4 s"}).encode())
        elif self.path == "/test/rtc":
            # ask the board's PCF85063: chip time vs host time, boot seeding, drift
            t0 = time.time()
            push_msg({"t": "rtc?"})
            while time.time() - t0 < 4 and board_rtc_at < t0:
                time.sleep(0.1)
            a = dict(board_rtc)
            a["fresh"] = board_rtc_at >= t0
            a["boot_hello"] = board_rtc_boot
            host_local = int(time.time()) + time.localtime().tm_gmtoff
            a["host"] = host_local
            for k in ("chip", "clock", "boot", "host"):
                if a.get(k):
                    a[k + "_str"] = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(a[k]))
            if a.get("chip"):
                a["chip_minus_host"] = a["chip"] - host_local
            self._ok(json.dumps(a).encode())
        elif self.path.startswith("/test/shot"):
            # /test/shot[?s=1|2|4]: PNG of the board's canvas -> ~/.agentpet/shots/latest.png
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            step = dict(kv.split("=", 1) for kv in q.split("&") if "=" in kv).get("s", "2")
            self._ok(json.dumps(board_shot(int(step) if step.isdigit() else 2)).encode())
        elif self.path.startswith("/test/pickup/"):
            # live-switch the pickup page binding (board persists it in NVS):
            # /test/pickup/<follow|face|clock|report|almanac|smart>
            push_msg({"t": "cfg", "pickup": self.path.rsplit("/", 1)[1]})
            self._ok()
        elif self.path.startswith("/test/lang/"):
            # /test/lang/<zh|en>: temporary override on the board only (its
            # NVS keeps it until the next cfg -- any save or reconnect -- puts
            # config.json's lang back). config.json is not touched.
            self._ok(json.dumps(test_lang(self.path.rsplit("/", 1)[1])).encode())
        elif self.path == "/test/badlines":
            # board lines that failed JSON parsing since host start — the framing
            # regression check after any change to the board's TCP/BLE send path
            self._ok(json.dumps({"bad_lines": _bad_lines[0]}).encode())
        elif self.path == "/test/nudge":
            # status toast (name card + four seat dots) for 3 s — lets /test/shot capture it
            push_board({"t": "nudge"})
            self._ok()
        elif self.path.startswith("/test/pin/"):
            # 钉住 debug (doc/06): /test/pin/1 locks the pet page to the hand
            # (gravity only turns it upright), /test/pin/0 releases it. Single
            # delivery — a doubled toggle would land back where it started.
            on = 0 if self.path.rsplit("/", 1)[1] in ("0", "off", "false") else 1
            rid = _new_rid()
            push_board({"t": "pin", "on": on, "id": rid})
            r = wait_reply("pin", rid, 3)
            self._ok(json.dumps(r or {"ok": False, "why": "no answer in 3 s"}).encode())
        elif self.path == "/test/report":
            # push today's battle report now and echo it for inspection
            a = report_msg()
            push_msg(a)
            self._ok(json.dumps(a).encode())
        elif self.path.startswith("/test/almanac_year"):
            # generate + push the whole year's almanac onto the card (offline pack)
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            yr = dict(kv.split("=", 1) for kv in q.split("&") if "=" in kv).get("year")
            r = almanac_push_year(int(yr) if yr else None, force=True)
            self._ok(json.dumps(r, ensure_ascii=False).encode())
        elif self.path.startswith("/almanac/preview"):
            # /almanac/preview?date=YYYY-MM-DD -> that day's almanac, nothing pushed
            q = self.path.split("?", 1)[1] if "?" in self.path else ""
            ds = dict(kv.split("=", 1) for kv in q.split("&") if "=" in kv).get("date")
            d = datetime.date.fromisoformat(ds) if ds else None
            self._ok(json.dumps(almanac_msg(d), ensure_ascii=False).encode())
        elif self.path == "/test/almanac":
            # push today's cyber almanac now and echo it for inspection
            a = almanac_msg()
            push_msg(a)
            self._ok(json.dumps(a, ensure_ascii=False).encode())
        elif self.path.startswith("/test/skin/"):
            # /test/skin/<n>[?agent=codex] -- dress a seat (default: the shown one):
            # 0=classic 1=kitty 2=robo 3=bunny 4=sprout 5=grok, board NVS. A name
            # works too (/test/skin/grok). Exclusive wardrobe: the seat that wore
            # it gets this seat's old outfit; the board answers {"t":"skins"}.
            q = dict(parse_qsl(self.path.partition("?")[2]))
            raw = self.path.partition("?")[0].rsplit("/", 1)[1]
            n = SKIN_NAMES.index(raw) if raw in SKIN_NAMES else np_num(raw, 0)
            msg = {"t": "skin", "id": int(n) % len(SKIN_NAMES)}
            a = q.get("agent", "")
            if a:
                if a not in AGENTS:
                    self._ok(json.dumps({"ok": False, "why": "unknown seat %r" % a}).encode())
                    return
                msg["agent"] = a
            push_board(msg)          # one delivery: two would swap a pair and swap it back
            self._ok(json.dumps({"ok": True, "skin": SKIN_NAMES[msg["id"]],
                                 "agent": a or None}).encode())
        elif self.path.startswith("/test/sound/"):
            # board-side chirp: /test/sound/<boot|bye|select|needs|done|
            #   voice_on|voice_off|surprise|tick|purr|nom|burp|dizzy|sigh>
            push_msg({"t": "sound", "name": self.path.rsplit("/", 1)[1]})
            self._ok()
        elif self.path.startswith("/test/volume/"):
            # board volume 0-100, persisted on the board (0 = mute)
            try:
                vol = max(0, min(100, int(self.path.rsplit("/", 1)[1])))
            except ValueError:
                self._ok(b"bad volume")
                return
            push_msg({"t": "sound", "vol": vol})
            self._ok()
        elif self.path == "/test/faces" or self.path.startswith("/test/face/"):
            # board-side demo: /test/faces = 14-face tour (6 s each);
            # /test/face/<name> holds one face for 20 s (working / needs_you /
            # done / idle / off / listening / offline / surprised / dizzy /
            # petting / eating / burp / bored / stretch)
            payload = {"t": "demo"}
            if self.path.startswith("/test/face/"):
                payload["face"] = self.path.rsplit("/", 1)[1]
            push_msg(payload)
            log("demo:", payload.get("face", "tour"))
            self._ok()
        else:
            self._ok(b"agentpet")

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except ValueError:
            payload = {}
        if self.path == "/settings/save":
            self._ok(json.dumps(save_settings(payload), ensure_ascii=False).encode())
            return
        if self.path == "/host/restart":
            self._ok(json.dumps(host_restart()).encode())
            return
        if self.path == "/agents/use":
            r = agents_use(str(payload.get("seat", "")), payload.get("app"))
            self._ok(json.dumps(r, ensure_ascii=False).encode())
            return
        if self.path == "/board/claim":
            self._ok(json.dumps(board_claim(), ensure_ascii=False).encode())
            return
        if self.path == "/board/wifi":
            r = board_wifi(str(payload.get("op", "list")), str(payload.get("ssid", "")),
                           str(payload.get("pass", "")), bool(payload.get("join")))
            self._ok(json.dumps(r, ensure_ascii=False).encode())
            return
        m = re.match(r"^/hook/claude/(\w+)", self.path)
        if m:
            self.claude_event(m.group(1), payload)
        elif self.path.startswith("/hook/codex"):
            self.codex_event(payload)
        self._ok()

    def codex_event(self, p):
        # Two senders share this endpoint: codex `notify` ({"type":
        # "agent-turn-complete"} is the ONLY type notify ever emits — checked
        # in the 0.149 binary) and the hooks.json PermissionRequest hook
        # ({"hook_event_name": "PermissionRequest", ...} via
        # ~/.agentpet/codex_permission_hook.sh), which is the real
        # needs_you signal.
        typ = (p.get("type") or p.get("hook_event_name") or "")
        typ = typ.replace("_", "-").lower()
        now = time.time()
        log("codex notify:", typ or p)
        with lock:
            if "turn-complete" in typ:
                main, why = codex_turn_is_main(p)
                if not main:                 # the app's title/summary side turn, not the task
                    log("codex notify: ignored,", why)
                    return
                codex_notify["turn_end"] = now
                codex_notify["needs_until"] = 0.0
            elif ("permission" in typ or "approval" in typ or
                  "input" in typ or "elicit" in typ):
                codex_notify["needs_since"] = now
                codex_notify["needs_until"] = now + 600
        aggregate()
        push_state()

    def claude_event(self, event, p):
        sid = p.get("session_id", "?")
        now = time.time()
        with lock:
            s = claude_sessions.setdefault(sid, {"state": "idle", "ts": now,
                                                 "done_until": 0})
            s["ts"] = now
            if p.get("transcript_path"):
                s["transcript"] = p["transcript_path"]
            if event in ("PreToolUse", "PostToolUse", "UserPromptSubmit"):
                s["state"] = "working"
            elif event == "Notification":
                # Two kinds arrive here: permission requests (real needs_you)
                # and the 60s idle reminder ("Claude is waiting for your
                # input"). The reminder must NOT raise the question-mark face
                # — done/idle already said we finished; don't steal focus.
                msg = (p.get("message") or "").lower()
                if "waiting for" in msg and "input" in msg:
                    # …but "waiting for input" does prove we are not working:
                    # after an interrupt it is the 60 s safety net behind the
                    # transcript check above
                    if s["state"] == "working":
                        s["state"] = "idle"
                        log("claude idle reminder: working -> idle", sid[:8])
                    else:
                        log("claude idle reminder ignored:", sid[:8])
                else:
                    s["state"] = "needs_you"
            elif event == "Stop":
                s["state"] = "idle"
                s["done_until"] = now + DONE_LINGER
            elif event == "SessionStart":
                s["state"] = "idle"
            elif event == "SessionEnd":
                claude_sessions.pop(sid, None)
        aggregate()
        push_state()

# ------------------------------------------------------------------ main
def main():
    log(f"host: product={PRODUCT} version={HOST_VERSION}")
    mem_debug_start()                 # before any thread: the base snapshot is a quiet host
    load_overrides()
    load_report()
    np_apply()                        # media-control listener
    threading.Thread(target=watcher_loop, daemon=True).start()
    threading.Thread(target=ble_thread, daemon=True).start()
    threading.Thread(target=power_thread, daemon=True).start()   # links down before a Mac sleep
    threading.Thread(target=mem_loop, daemon=True).start()
    threading.Thread(target=prime_voices, daemon=True).start()

    http = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), HookHandler)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    if VOICE_SOURCE == "board":              # engine start ~0.3 s: not on the first key press
        threading.Thread(target=mic_sink_proc, daemon=True).start()
    log(f"hook/debug HTTP on 127.0.0.1:{HTTP_PORT}")

    class TCP(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

    srv = TCP(("0.0.0.0", TCP_PORT), BoardHandler)
    log(f"board TCP on 0.0.0.0:{TCP_PORT}")
    srv.serve_forever()

if __name__ == "__main__":
    main()
