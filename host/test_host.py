#!/usr/bin/python3
# Copyright (c) 2026 yuwentong
# Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
"""Host-side logic tests: no board, no network, no Mac UI.

    /usr/bin/python3 host/test_host.py -v            # from the repo root
    /usr/bin/python3 -m unittest host/test_host.py

Imports agentpet_host as a module (its servers only start under main()).
The almanac tests read the repo's host/almanac_bank.json — not the deployed
copy — and ignore ~/.agentpet/almanac.json, so they talk about the code and
bank that are in git, whatever this machine has hot-appended.
"""
import ast
import base64
import contextlib
import datetime
import hashlib
import inspect
import io
import json
import math
import os
import re
import struct
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
import wave
import zlib
from unittest import mock
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import agentpet_host as H   # noqa: E402


# ------------------------------------------------------------------ helpers
def png_decode(png):
    """Minimal reader for what rgb565_to_png emits: 8-bit RGB, filter 0.
    Returns (w, h, rows) with rows as lists of (r, g, b); checks every CRC."""
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos, w, h, idat = 8, None, None, b""
    while pos < len(png):
        ln, = struct.unpack(">I", png[pos:pos + 4])
        tag = png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + ln]
        crc, = struct.unpack(">I", png[pos + 8 + ln:pos + 12 + ln])
        assert crc == zlib.crc32(tag + data) & 0xFFFFFFFF, "bad CRC on " + tag.decode()
        if tag == b"IHDR":
            w, h, depth, ctype = struct.unpack(">IIBB", data[:10])
            assert (depth, ctype) == (8, 2), "expected 8-bit RGB"
        elif tag == b"IDAT":
            idat += data
        pos += 12 + ln
    raw = zlib.decompress(idat)
    stride = 1 + 3 * w
    rows = []
    for y in range(h):
        line = raw[y * stride:(y + 1) * stride]
        assert line[0] == 0, "filter byte must be 0 (none)"
        rows.append([tuple(line[1 + 3 * x:4 + 3 * x]) for x in range(w)])
    return w, h, rows


def rgb565_frame(w, h, color_at):
    """Little-endian RGB565 frame, rows top to bottom; color_at(x, y) -> u16."""
    return struct.pack("<%dH" % (w * h),
                       *[color_at(x, y) for y in range(h) for x in range(w)])


RED, GREEN, BLUE, WHITE, BLACK = 0xF800, 0x07E0, 0x001F, 0xFFFF, 0x0000


def cells(s):
    """Rough width the board pays for a bank line, in 26 px cells: a CJK
    glyph is one cell, a Latin letter / space about half."""
    return sum(1.0 if ord(ch) > 0x7F else 0.5 for ch in s)


# ------------------------------------------------------------------ almanac
class AlmanacTests(unittest.TestCase):
    def setUp(self):
        self._saved = (H.ALMANAC_BANK, H.ALMANAC_OVERRIDE)
        H.ALMANAC_BANK = HERE / "almanac_bank.json"
        H.ALMANAC_OVERRIDE = Path(tempfile.gettempdir()) / "agentpet-no-such-override.json"
        self.assertTrue(H.ALMANAC_BANK.exists())
        self.assertFalse(H.ALMANAC_OVERRIDE.exists())

    def tearDown(self):
        H.ALMANAC_BANK, H.ALMANAC_OVERRIDE = self._saved

    def test_bank_is_big_enough_and_clean(self):
        yi, ji, qian = H.almanac_bank()
        self.assertGreaterEqual(len(yi), 732)      # 2 per day, leap year
        self.assertGreaterEqual(len(ji), 732)
        self.assertGreaterEqual(len(qian), 366)    # 1 per day
        for name, pool in (("yi", yi), ("ji", ji), ("qian", qian)):
            self.assertEqual(len(pool), len(set(pool)), name + " has exact duplicates")
            for line in pool:
                self.assertEqual(line, line.strip(), repr(line))
                self.assertNotIn("\n", line)

    def test_bank_lines_fit_the_page(self):
        # pages.cpp: 宜/忌 rows are 26 px glyphs from x=108 to 446 -> 13 cells;
        # the 签文 row is 24 px centred in 412 px -> 17 cells.
        yi, ji, qian = H.almanac_bank()
        for line in yi + ji:
            self.assertLessEqual(cells(line), 13, line)
        for line in qian:
            self.assertLessEqual(cells(line), 17, line)

    def test_year_plan_never_repeats(self):
        for year in (2026, 2027, 2028):           # 2028 is a leap year
            d = datetime.date(year, 1, 1)
            yi_seen, ji_seen, q_seen, days = [], [], [], 0
            while d.year == year:
                yi2, ji2, q1 = H.almanac_plan(d)
                self.assertEqual(len(yi2), 2)
                self.assertEqual(len(ji2), 2)
                self.assertIsInstance(q1, str)
                yi_seen += yi2
                ji_seen += ji2
                q_seen.append(q1)
                days += 1
                d += datetime.timedelta(days=1)
            self.assertEqual(len(set(yi_seen)), 2 * days, f"{year}: 宜 repeats")
            self.assertEqual(len(set(ji_seen)), 2 * days, f"{year}: 忌 repeats")
            self.assertEqual(len(set(q_seen)), days, f"{year}: 签 repeats")

    def test_plan_is_stable_and_differs_by_year(self):
        d = datetime.date(2026, 9, 5)
        self.assertEqual(H.almanac_plan(d), H.almanac_plan(d))
        self.assertNotEqual(H.almanac_plan(d), H.almanac_plan(datetime.date(2027, 9, 5)))

    def test_almanac_msg_shape_and_ganzhi_anchor(self):
        m = H.almanac_msg(datetime.date(2000, 1, 1))     # the code's 干支 anchor day
        self.assertEqual(m["t"], "almanac")
        self.assertEqual(m["gz"], "戊午日")
        self.assertEqual(m["sx"], "属兔")                 # before 立春 -> still 己卯年
        self.assertEqual(m["date"], "01.01")
        for k in ("jc", "yi", "ji", "qian", "dir", "sig", "cn", "ch"):
            self.assertIn(k, m)
        self.assertIn(m["sig"], (2, 3, 4))
        self.assertEqual(m, H.almanac_msg(datetime.date(2000, 1, 1)))   # deterministic


# ------------------------------------------------------------------ board protocol
class ProtocolTests(unittest.TestCase):
    """handle_board_msg is the one entry for BLE, TCP and (via shot.py) USB
    lines; drive it with raw lines the way the link threads do."""
    def setUp(self):
        self.replies, self.parked = [], []
        self._park, self._dir = H._park_reply, H.SHOT_DIR
        H._park_reply = self.parked.append
        self._tmp = tempfile.TemporaryDirectory()
        H.SHOT_DIR = Path(self._tmp.name)

    def tearDown(self):
        H._park_reply, H.SHOT_DIR = self._park, self._dir
        self._tmp.cleanup()

    def feed(self, msg):
        raw = msg if isinstance(msg, (bytes, str)) else json.dumps(msg)
        H.handle_board_msg(raw, self.replies.append)

    def test_ping_gets_pong(self):
        self.feed({"t": "ping"})
        self.assertEqual(self.replies, [b'{"t":"pong"}\n'])

    def test_garbage_is_ignored_not_raised(self):
        for raw in (b"", b"not json", b"{", b'{"t":"no-such-type"}', b"[1,2]", b"42", b"null"):
            self.feed(raw)
        self.assertEqual(self.replies, [])
        self.assertEqual(self.parked, [])

    def _stream(self, w, h, raw, sid, chunk=1440, drop=None, reverse=False):
        parts = [raw[i:i + chunk] for i in range(0, len(raw), chunk)]
        self.feed({"t": "sbeg", "id": sid, "w": w, "h": h, "n": len(parts), "step": 2})
        order = range(len(parts) - 1, -1, -1) if reverse else range(len(parts))
        for i in order:
            if i != drop:
                self.feed({"t": "sdat", "id": sid, "seq": i,
                           "d": base64.b64encode(parts[i]).decode()})
        self.feed({"t": "sfin", "id": sid, "ok": True, "ms": 123, "bytes": len(raw)})
        self.assertEqual(len(self.parked), 1)
        self.assertNotIn(sid, H._shots)                    # state cleaned up
        return self.parked[0]

    def test_shot_stream_assembles_png(self):
        w = h = 240

        def quad(x, y):
            return (RED if x < 120 else GREEN) if y < 120 else (BLUE if x < 120 else WHITE)
        raw = rgb565_frame(w, h, quad)
        res = self._stream(w, h, raw, sid=9001, reverse=True)     # chunks out of order
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["w"], res["h"], res["bytes"]), (w, h, w * h * 2))
        self.assertEqual(res["board_ms"], 123)
        png = Path(res["path"]).read_bytes()
        self.assertEqual((H.SHOT_DIR / "latest.png").read_bytes(), png)
        pw, ph, rows = png_decode(png)
        self.assertEqual((pw, ph), (w, h))
        self.assertEqual(rows[0][0], (255, 0, 0))
        self.assertEqual(rows[0][239], (0, 255, 0))
        self.assertEqual(rows[239][0], (0, 0, 255))
        self.assertEqual(rows[239][239], (255, 255, 255))
        self.assertEqual(rows[119][119], (255, 0, 0))
        self.assertEqual(rows[120][120], (255, 255, 255))

    def test_shot_missing_chunk_is_reported(self):
        raw = rgb565_frame(8, 8, lambda x, y: BLACK)
        res = self._stream(8, 8, raw, sid=9002, chunk=32, drop=2)
        self.assertFalse(res["ok"])
        self.assertIn("missing chunks [2]", res["why"])

    def test_shot_board_failure_passes_reason(self):
        self.feed({"t": "sfin", "id": 9003, "ok": False, "why": "busy"})
        self.assertEqual(self.parked, [{"t": "shot", "id": 9003, "ok": False, "why": "busy"}])

    def test_sfin_without_sbeg(self):
        self.feed({"t": "sfin", "id": 9004, "ok": True})
        self.assertEqual(self.parked[0]["why"], "sfin without sbeg")

    def test_ota_answer_is_parked_and_hello_tracks_build(self):
        self.feed({"t": "ota", "id": 31, "ok": True, "ms": 9000, "bytes": 1873064, "part": "app1"})
        self.assertEqual(self.parked[-1]["part"], "app1")
        self.feed({"t": "hello", "dev": "amoled216", "v": 1, "build": "Sep  5 2026 12:00:00", "part": "app1"})
        self.assertEqual((H.board_build, H.board_part), ("Sep  5 2026 12:00:00", "app1"))
        self.assertGreater(H.board_hello_at, 0)

    def test_rtc_answer_lands_in_board_rtc(self):
        self.feed({"t": "rtc", "ok": True, "integ": True, "chip": 1788900000,
                   "clock": 1788900001, "boot": 1788899000, "seeded": True,
                   "drift": 0, "syncs": 1, "writes": 0, "up": 42})
        self.assertEqual(H.board_rtc["chip"], 1788900000)
        self.assertNotIn("t", H.board_rtc)
        self.assertGreater(H.board_rtc_at, 0)

    def test_hello_gets_time_almanac_report_cfg(self):
        before = int(time.time())
        self.feed({"t": "hello", "dev": "amoled216", "v": 1, "rtc": 1788899000})
        self.assertEqual(H.board_rtc_boot, 1788899000)
        kinds = [json.loads(r)["t"] for r in self.replies]
        # a TCP hello is first told who we are (认领制: IP drift, first owner)
        # ... and the Claude session row last (empty with no sessions)
        self.assertEqual(kinds, ["hostinfo", "time", "almanac", "report", "cfg", "sess"])
        tm = json.loads(self.replies[1])
        self.assertAlmostEqual(tm["epoch"], before, delta=5)      # UTC seconds ...
        self.assertEqual(tm["off"], time.localtime().tm_gmtoff)   # ... plus the host's tz offset


# ------------------------------------------------------------------ PNG encoder
class PngTests(unittest.TestCase):
    def test_exact_colours(self):
        raw = struct.pack("<8H", RED, GREEN, BLUE, WHITE, BLACK, 0x8410, BLUE, RED)
        w, h, rows = png_decode(H.rgb565_to_png(raw, 4, 2))
        self.assertEqual((w, h), (4, 2))
        self.assertEqual(rows[0], [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)])
        self.assertEqual(rows[1][0], (0, 0, 0))
        self.assertEqual(rows[1][1], (132, 130, 132))   # 5/6-bit mid grey scaled to 8-bit
        self.assertEqual(rows[1][2:], [(0, 0, 255), (255, 0, 0)])

    def test_little_endian_words_and_row_order(self):
        raw = b"\x00\xf8" + b"\xe0\x07"                  # 0xF800 then 0x07E0 on the wire
        _, _, rows = png_decode(H.rgb565_to_png(raw, 1, 2))
        self.assertEqual(rows, [[(255, 0, 0)], [(0, 255, 0)]])

    def test_trailing_bytes_are_ignored(self):
        raw = struct.pack("<3H", RED, GREEN, BLUE)
        _, _, rows = png_decode(H.rgb565_to_png(raw, 2, 1))
        self.assertEqual(rows, [[(255, 0, 0), (0, 255, 0)]])


# ------------------------------------------------------------------ codex notify
class ClaudeInterruptTests(unittest.TestCase):
    """Esc mid-turn fires no Stop hook; the transcript's interrupt marker must
    flip a working session to idle, but only when newer than the last hook."""

    def _transcript(self, lines):
        d = tempfile.mkdtemp()
        path = Path(d) / "s.jsonl"
        path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
        return str(path)

    @staticmethod
    def _iso(ts):
        return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def test_marker_newer_than_last_hook(self):
        now = 1_800_000_000.0
        p = self._transcript([
            {"type": "assistant", "timestamp": self._iso(now - 5), "message": {"content": "x" * 3000}},
            {"type": "user", "timestamp": self._iso(now + 3), "interruptedMessageId": "m1",
             "message": {"role": "user", "content": [{"type": "text", "text": H.CLAUDE_INTERRUPT_MARK}]}},
        ])
        self.assertTrue(H.claude_interrupted_after(p, now))

    def test_marker_older_than_last_hook_is_ignored(self):
        now = 1_800_000_000.0
        p = self._transcript([
            {"type": "user", "timestamp": self._iso(now - 30), "interruptedMessageId": "m1",
             "message": {"content": [{"type": "text", "text": H.CLAUDE_INTERRUPT_MARK}]}},
        ])
        self.assertFalse(H.claude_interrupted_after(p, now))   # user typed again since

    def test_no_marker_and_missing_file(self):
        now = 1_800_000_000.0
        p = self._transcript([{"type": "assistant", "timestamp": self._iso(now + 1), "message": {"content": "hi"}}])
        self.assertFalse(H.claude_interrupted_after(p, now))
        self.assertFalse(H.claude_interrupted_after(p + ".missing", now))
        self.assertFalse(H.claude_interrupted_after(None, now))

    def test_marker_deep_in_a_long_tail(self):
        now = 1_800_000_000.0
        big = {"type": "assistant", "timestamp": self._iso(now - 1), "message": {"content": "y" * 20000}}
        mark = {"type": "user", "timestamp": self._iso(now + 2), "interruptedMessageId": "m2",
                "message": {"content": [{"type": "text", "text": H.CLAUDE_INTERRUPT_MARK}]}}
        # marker then a small system line after it: still found within the tail window
        p = self._transcript([big, mark, {"type": "system", "timestamp": self._iso(now + 2), "subtype": "x"}])
        self.assertTrue(H.claude_interrupted_after(p, now))


class CodexNotifyTests(unittest.TestCase):
    MAIN = "01a06fd0-7184-7442-8b39-c8ddeae10172"
    SIDE = "01a06fd2-5440-7713-8412-a25148ea1638"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._saved = H.CODEX_SESSIONS
        H.CODEX_SESSIONS = Path(self._tmp.name)
        d = H.CODEX_SESSIONS / "2026" / "09" / "05"
        d.mkdir(parents=True)
        (d / f"rollout-2026-09-05T12-25-26-{self.MAIN}.jsonl").write_text("{}\n")

    def tearDown(self):
        H.CODEX_SESSIONS = self._saved
        self._tmp.cleanup()

    def test_task_thread_counts(self):
        ok, why = H.codex_turn_is_main({"type": "agent-turn-complete", "thread-id": self.MAIN,
                                        "input-messages": ["用 Three.js 做一个场景"]})
        self.assertTrue(ok, why)

    def test_title_side_thread_is_ignored(self):
        ok, why = H.codex_turn_is_main({"type": "agent-turn-complete", "thread-id": self.SIDE,
                                        "client": "Codex Desktop",
                                        "input-messages": ["You are a helpful assistant. You will be "
                                                           "presented with a user prompt, and your job is "
                                                           "to provide a short title for a task"]})
        self.assertFalse(ok)
        self.assertIn("side thread", why)
        self.assertIn("task title", why)

    def test_legacy_payload_without_thread_id_still_counts(self):
        ok, _ = H.codex_turn_is_main({"type": "agent-turn-complete"})
        self.assertTrue(ok)


# ------------------------------------------------------------------ reverse focus follow
class FrontAgentTests(unittest.TestCase):
    """_frontmost_app() -> seat id. Only the four FOCUS_APPS count."""
    def test_terminal_and_localized_names(self):
        self.assertEqual(H.front_agent("Warp|Warp"), "claude")
        self.assertEqual(H.front_agent("QwenWorkCN|千问办公"), "qoderwork")
        self.assertEqual(H.front_agent("ChatGPT|ChatGPT"), "codex")
        self.assertEqual(H.front_agent("Qoder CN IDE|Qoder"), "qoder")

    def test_case_insensitive(self):
        self.assertEqual(H.front_agent("warp|WARP"), "claude")
        self.assertEqual(H.front_agent("qwenworkcn|千问办公"), "qoderwork")

    def test_strangers_and_empties(self):
        for front in ("Safari|Safari", "Finder|访达", "", None, "|", "Warpspeed|Warpspeed"):
            self.assertIsNone(H.front_agent(front), front)

    def test_custom_map(self):
        self.assertEqual(H.front_agent("iTerm2|iTerm", {"claude": "iTerm2"}), "claude")
        self.assertIsNone(H.front_agent("Warp|Warp", {"claude": "iTerm2"}))


class SeatSplitTests(unittest.TestCase):
    """2026-09-13: the Qoder IDE and the Qoder Forest desktop share
    CFBundleName "Qoder CN"; they are two seats now and must never merge."""
    def test_forest_is_the_fifth_and_last_seat(self):
        self.assertEqual(len(H.AGENTS), 5)
        self.assertEqual(H.AGENTS[-1], "forest")
        self.assertEqual(H.AGENTS[:4], ["claude", "codex", "qoder", "qoderwork"])

    def _hits(self, args):
        out = []
        for agent, rule in H.PROC_RULES.items():
            if H.re.search(rule["match"], args):
                if rule["exclude"] and H.re.search(rule["exclude"], args):
                    continue
                out.append(agent)
        return out

    def test_process_rules_split_by_app_path(self):
        ide = "/Applications/Qoder CN IDE.app/Contents/MacOS/Qoder CN"
        forest = "/Applications/Qoder CN.app/Contents/MacOS/Qoder CN"
        self.assertEqual(self._hits(ide), ["qoder"])
        self.assertEqual(self._hits(forest), ["forest"])
        self.assertEqual(self._hits("/Applications/Qoder CN IDE.app/Contents/Frameworks/"
                                    "Qoder CN Helper (Renderer).app/Contents/MacOS/Qoder CN Helper"),
                         ["qoder"])
        self.assertEqual(self._hits("/Applications/Qoder CN.app/Contents/Resources/bin/"
                                    "appshot_bare_modifier_monitor --key CommandBothSides"),
                         ["forest"])
        self.assertEqual(self._hits("/Applications/QwenWorkCN.app/Contents/MacOS/QwenWorkCN"),
                         ["qoderwork"])

    def test_front_agent_tells_the_two_qoders_apart(self):
        self.assertEqual(H.front_agent("Qoder CN|Qoder CN"), "forest")
        self.assertEqual(H.front_agent("Qoder CN IDE"), "qoder")
        # both names in one front string: the specific one wins either way round
        self.assertEqual(H.front_agent("Qoder CN|Qoder CN IDE"), "qoder")
        self.assertEqual(H.front_agent("Qoder CN IDE|Qoder CN"), "qoder")

    def test_focus_insurance_does_not_mistake_the_ide_for_forest(self):
        # raise_agent_app() decides "already front" with front_agent(): the IDE
        # in front must not satisfy the forest seat (substring bug 2026-09-13)
        self.assertNotEqual(H.front_agent("Qoder CN IDE|Qoder CN IDE"), "forest")
        self.assertEqual(H.front_agent("Qoder CN|Qoder CN"), "forest")

    def test_every_seat_has_its_tables(self):
        for a in H.AGENTS:
            self.assertIn(a, H.PROC_RULES, a)
            self.assertIn(a, H.FOCUS_APPS, a)
            self.assertIn(a, H.SPEAK_NAMES, a)
            self.assertIn(a, H.APPROVE_KEYS, a)
            self.assertIn(a, H.REJECT_KEYS, a)
        self.assertEqual(len(H.report_msg()["w"]), 5)


class ForestLogTests(unittest.TestCase):
    """Qoder Forest's main.log state machine, fed the lines
    captured 2026-09-13 16:06-16:10 (one task, two questions, completed)."""
    PFX = "[2026-09-13T08:06:03.212Z] [INFO] [main] [ChatSession] Main ChatSession control state committed "
    SID = "cba08d44-e6fc-4604-bd5d-525711de2201"
    TID = "7bd6d062-be6d-41a4-b1dd-6617dd04ff48"

    def line(self, prev, st, turn_state, pending, terminal=None):
        return self.PFX + json.dumps({
            "sessionId": self.SID, "previousRuntimeState": prev, "runtimeState": st,
            "previousActiveTurnId": self.TID, "previousActiveTurnState": None,
            "activeTurnId": None if terminal else self.TID, "activeTurnState": turn_state,
            "pendingInteractionCount": pending, "queuedTurnCount": 0,
            "terminalTurnId": self.TID if terminal else None, "terminalStatus": terminal})

    def test_one_task_with_two_questions(self):
        run = {"sessions": {}, "done_until": 0.0}
        self.assertEqual(H.forest_apply(self.line("cold", "opening", "starting", 0), 100.0, run), "opening")
        self.assertEqual(H.forest_apply(self.line("opening", "running", "running", 0), 103.0, run), "running")
        self.assertEqual(run["sessions"][self.SID][:2], ("running", 0))
        self.assertEqual(H.forest_apply(self.line("running", "waiting-user", "waiting-user", 1), 110.0, run), "waiting-user")
        self.assertEqual(run["sessions"][self.SID][:2], ("waiting-user", 1))
        self.assertEqual(run["done_until"], 0.0)
        self.assertEqual(H.forest_apply(self.line("waiting-user", "running", "running", 0), 340.0, run), "running")
        self.assertEqual(H.forest_apply(self.line("running", "waiting-user", "waiting-user", 1), 344.0, run), "waiting-user")
        self.assertEqual(H.forest_apply(self.line("waiting-user", "running", "running", 0), 354.0, run), "running")
        self.assertEqual(H.forest_apply(self.line("running", "ready", None, 0, "completed"), 357.0, run), "ready")
        self.assertEqual(run["sessions"][self.SID][:2], ("ready", 0))
        self.assertEqual(run["done_until"], 357.0 + H.DONE_LINGER)

    def test_a_relogged_ready_does_not_chirp_again(self):
        run = {"sessions": {}, "done_until": 0.0}
        H.forest_apply(self.line("running", "ready", None, 0, "completed"), 10.0, run)
        self.assertEqual(run["done_until"], 0.0)       # never saw it live: no edge
        H.forest_apply(self.line("cold", "running", "running", 0), 20.0, run)
        H.forest_apply(self.line("running", "ready", None, 0, "completed"), 30.0, run)
        self.assertEqual(run["done_until"], 30.0 + H.DONE_LINGER)
        H.forest_apply(self.line("ready", "ready", None, 0, "completed"), 40.0, run)
        self.assertEqual(run["done_until"], 30.0 + H.DONE_LINGER)

    def test_other_lines_are_ignored(self):
        run = {"sessions": {}, "done_until": 0.0}
        other = ('[2026-09-13T08:06:13.769Z] [INFO] [main] [ChatSession] ChatSession interaction '
                 'state published {"sessionId":"x","phase":"waiting","runtimeState":"waiting-user","pendingCount":1}')
        self.assertIsNone(H.forest_apply(other, 1.0, run))
        self.assertIsNone(H.forest_apply(self.PFX + "not json", 1.0, run))
        self.assertEqual(run["sessions"], {})


class FrontFollowDecideTests(unittest.TestCase):
    """The policy is pure: candidate + clock in, seat or None out."""
    def setUp(self):
        self.st = {}

    def decide(self, cand="codex", active="claude", now=100.0, board=0.0,
               raise_at=0.0, voice=False, enabled=True):
        return H.front_follow_decide(cand, active, now, self.st, board,
                                     raise_at, voice, enabled)

    def test_needs_a_stable_second(self):
        self.assertIsNone(self.decide(now=100.0))          # first sighting
        self.assertIsNone(self.decide(now=100.9))          # < 1 s
        self.assertEqual(self.decide(now=101.0), "codex")  # >= 1 s

    def test_same_as_active_never_switches(self):
        self.assertIsNone(self.decide(cand="claude", now=100.0))
        self.assertIsNone(self.decide(cand="claude", now=105.0))
        self.assertEqual(self.st, {})                      # and forgets the candidate

    def test_unknown_front_clears_the_candidate(self):
        self.assertIsNone(self.decide(now=100.0))
        self.assertIsNone(self.decide(cand=None, now=100.5))
        self.assertEqual(self.st, {})
        self.assertIsNone(self.decide(now=101.6))          # timer restarted, not carried over

    def test_flapping_candidate_restarts_the_timer(self):
        self.assertIsNone(self.decide(cand="codex", now=100.0))
        self.assertIsNone(self.decide(cand="qoder", now=100.8))   # changed mid-window
        self.assertIsNone(self.decide(cand="qoder", now=101.5))   # only 0.7 s of qoder
        self.assertEqual(self.decide(cand="qoder", now=101.8), "qoder")

    def test_board_swipe_wins_for_five_seconds(self):
        self.assertIsNone(self.decide(now=100.0, board=99.0))
        self.assertIsNone(self.decide(now=101.5, board=99.0))     # stable but board is 2.5 s old
        self.assertEqual(self.decide(now=104.1, board=99.0), "codex")   # lockout expired

    def test_our_own_raise_is_not_chased(self):
        self.assertIsNone(self.decide(now=100.0, raise_at=99.5))
        self.assertIsNone(self.decide(now=101.2, raise_at=99.5))  # stable, but we raised it
        self.assertEqual(self.decide(now=102.6, raise_at=99.5), "codex")

    def test_voice_key_held_freezes_the_seat(self):
        self.assertIsNone(self.decide(now=100.0, voice=True))
        self.assertIsNone(self.decide(now=101.5, voice=True))
        self.assertEqual(self.decide(now=101.6), "codex")         # key released

    def test_disabled_does_nothing(self):
        self.assertIsNone(self.decide(now=100.0, enabled=False))
        self.assertIsNone(self.decide(now=105.0, enabled=False))


class BoardSelectTests(unittest.TestCase):
    """A board swipe and the board's echo of a host-pushed seat must not look
    alike: only the swipe raises the Mac app and locks reverse follow out."""
    def setUp(self):
        self._focus, self._active, self._at = H.focus_agent, H.active_agent, H.board_select_at
        self.raised = []
        H.focus_agent = self.raised.append          # never really `open -a` in a test

    def tearDown(self):
        H.focus_agent, H.active_agent, H.board_select_at = self._focus, self._active, self._at

    def feed(self, m):
        H.handle_board_msg(json.dumps(m), lambda b: None)

    def test_swipe_moves_the_seat_and_arms_the_lockout(self):
        H.active_agent, H.board_select_at = "claude", 0.0
        self.feed({"t": "select", "agent": "codex"})
        self.assertEqual(H.active_agent, "codex")
        self.assertEqual(self.raised, ["codex"])
        self.assertGreater(H.board_select_at, 0.0)

    def test_host_echo_raises_nothing_and_leaves_the_lockout(self):
        H.active_agent, H.board_select_at = "qoder", 0.0
        self.feed({"t": "select", "agent": "qoder", "src": "host"})
        self.assertEqual(self.raised, [])
        self.assertEqual(H.board_select_at, 0.0)

    def test_auto_select_moves_the_seat_without_raising_or_locking(self):
        # the seat went off and the board picked the next one itself:
        # dictation follows it, nothing else does
        H.active_agent, H.board_select_at = "codex", 0.0
        self.feed({"t": "select", "agent": "claude", "src": "auto"})
        self.assertEqual(H.active_agent, "claude")
        self.assertEqual(self.raised, [])
        self.assertEqual(H.board_select_at, 0.0)

    def test_auto_select_without_an_agent_keeps_the_seat(self):
        H.active_agent, H.board_select_at = "forest", 0.0
        self.feed({"t": "select", "src": "auto"})
        self.assertEqual(H.active_agent, "forest")
        self.assertEqual(self.raised, [])
        self.assertEqual(H.board_select_at, 0.0)


class BoardHelloSeatTests(unittest.TestCase):
    """A reboot resets the board to seat 0 while we still hold the seat reverse
    follow left us on, so the first dictation after an OTA used to land in the
    old app. The hello's `sel` realigns us -- quietly, like `select src:auto`:
    no app raise, no lockout."""
    HELLO = {"t": "hello", "dev": "amoled216", "v": 2, "link": "ble",
             "up": 3, "rst": "sw_reset"}

    def setUp(self):
        self._focus, self._active, self._at = H.focus_agent, H.active_agent, H.board_select_at
        self._hello_at, self._floor, self._log = H.board_hello_at, H._board_min_floor, H.log
        self.raised, self.lines = [], []
        H.focus_agent = self.raised.append      # never really `open -a` in a test
        H.log = lambda *a: self.lines.append(" ".join(str(x) for x in a))

    def tearDown(self):
        H.focus_agent, H.active_agent, H.board_select_at = self._focus, self._active, self._at
        H.board_hello_at, H._board_min_floor, H.log = self._hello_at, self._floor, self._log

    def feed(self, **kw):
        m = dict(self.HELLO, **kw)
        H.handle_board_msg(json.dumps(m), lambda _b: None)

    def test_hello_pulls_the_seat_back_to_the_board_without_raising(self):
        H.active_agent, H.board_select_at = "codex", 0.0
        self.feed(sel="claude")
        self.assertEqual(H.active_agent, "claude")
        self.assertEqual(self.raised, [])
        self.assertEqual(H.board_select_at, 0.0)
        self.assertIn("board hello: seat -> claude", self.lines)

    def test_an_unknown_seat_is_ignored(self):
        H.active_agent, H.board_select_at = "codex", 0.0
        self.feed(sel="nope")
        self.assertEqual(H.active_agent, "codex")
        self.assertEqual(self.raised, [])
        self.assertEqual([l for l in self.lines if l.startswith("board hello: seat")], [])

    def test_a_hello_without_sel_leaves_the_seat_alone(self):
        # firmware older than 2026-09-19 does not carry the field
        H.active_agent, H.board_select_at = "forest", 0.0
        self.feed()
        self.assertEqual(H.active_agent, "forest")
        self.assertEqual(self.raised, [])
        self.assertEqual([l for l in self.lines if l.startswith("board hello: seat")], [])


class BoardTelemetryTests(unittest.TestCase):
    """board side: hello and ping carry the
    board's uptime, reset reason and heap. Every field is optional -- the
    firmware that shipped before them sends a bare ping, and that must read as
    "unchanged", never 0."""
    HELLO = {"t": "hello", "dev": "amoled216", "v": 1, "link": "ble",
             "up": 7200, "rst": "poweron"}

    def setUp(self):
        self._stats = dict(H.board_stats)
        self._floor, self._log = H._board_min_floor, H.log
        self._hello_at = H.board_hello_at
        self._boards, self._ble = list(H.boards), H._ble_client
        self.lines = []
        H.log = lambda *a: self.lines.append(" ".join(str(x) for x in a))
        H.board_stats.update({"up": None, "heap": None, "min": None, "big": None,
                              "rst": None, "at": 0.0, "stale": False})
        H._board_min_floor = None

    def tearDown(self):
        H.log = self._log
        H.board_stats.clear()
        H.board_stats.update(self._stats)
        H._board_min_floor = self._floor
        H.board_hello_at = self._hello_at
        H.boards[:] = self._boards
        H._ble_client = self._ble

    def feed(self, m):
        H.handle_board_msg(json.dumps(m), lambda _b: None)

    def heap(self):
        return [l for l in self.lines if l.startswith("board heap:")]

    # -------------------------------------------------- hello
    def test_hello_records_uptime_and_reset_reason(self):
        self.feed(self.HELLO)
        self.assertEqual(H.board_stats["up"], 7200)
        self.assertEqual(H.board_stats["rst"], "poweron")
        self.assertGreater(H.board_stats["at"], 0)
        self.assertFalse(H.board_stats["stale"])

    def test_hello_without_the_new_fields_is_fine(self):
        self.feed({"t": "hello", "dev": "amoled216", "v": 1, "link": "ble"})
        self.assertIsNone(H.board_stats["up"])
        self.assertIsNone(H.board_stats["rst"])
        self.assertEqual(H.board_stats["at"], 0.0)     # nothing arrived to age

    # -------------------------------------------------- ping
    def test_ping_carries_the_heap_and_still_gets_a_pong(self):
        replies = []
        H.handle_board_msg(json.dumps({"t": "ping", "up": 60, "heap": 140 * 1024,
                                       "min": 120 * 1024, "big": 64 * 1024}),
                           replies.append)
        self.assertEqual(replies, [b'{"t":"pong"}\n'])
        self.assertEqual((H.board_stats["up"], H.board_stats["heap"],
                          H.board_stats["min"], H.board_stats["big"]),
                         (60, 140 * 1024, 120 * 1024, 64 * 1024))

    def test_our_own_rtt_probe_answer_is_not_telemetry(self):
        # /test/rtt sends host -> board {"t":"ping"}; what comes BACK is
        # {"t":"pong"}, which parks a reply and must not touch the vitals
        H.handle_board_msg(json.dumps({"t": "pong", "id": 3}), lambda _b: None)
        self.assertEqual(H.board_stats["at"], 0.0)

    def test_a_bare_ping_keeps_the_last_numbers(self):
        self.feed({"t": "ping", "up": 60, "heap": 140 * 1024, "min": 120 * 1024})
        at = H.board_stats["at"]
        self.feed({"t": "ping"})                        # pre-telemetry firmware
        self.assertEqual(H.board_stats["heap"], 140 * 1024)
        self.assertEqual(H.board_stats["at"], at)       # nothing new to date

    def test_junk_fields_are_ignored_not_stored(self):
        self.feed({"t": "ping", "up": "soon", "heap": None, "min": True, "rst": 7})
        self.assertEqual([H.board_stats[k] for k in ("up", "heap", "min", "rst")],
                         [None, None, None, None])

    # -------------------------------------------------- min descent
    def test_min_logs_once_per_new_low(self):
        self.feed(dict(self.HELLO, min=120 * 1024, big=64 * 1024))
        self.feed({"t": "ping", "up": 7260, "min": 120 * 1024})    # same floor
        self.feed({"t": "ping", "up": 7320, "min": 130 * 1024})    # higher
        self.assertEqual(len(self.heap()), 1)
        self.feed({"t": "ping", "up": 7380, "min": 100 * 1024, "big": 32 * 1024})
        self.assertEqual(len(self.heap()), 2)
        self.assertEqual(self.heap()[1],
                         "board heap: min fell to 100 KB (big 32 KB, up 123 min)")

    def test_a_bare_ping_does_not_relog_the_same_floor(self):
        self.feed(dict(self.HELLO, min=120 * 1024))
        self.feed({"t": "ping"})
        self.assertEqual(len(self.heap()), 1)

    def test_a_reconnect_waits_for_a_real_number(self):
        self.feed(dict(self.HELLO, min=100 * 1024))
        self.feed({"t": "hello", "dev": "amoled216", "v": 1, "link": "ble"})
        self.assertEqual(len(self.heap()), 1)       # nothing re-logged from session 1
        self.feed({"t": "ping", "min": 90 * 1024})
        self.assertEqual(len(self.heap()), 2)

    def test_a_new_hello_restarts_the_descent(self):
        self.feed(dict(self.HELLO, min=100 * 1024))
        self.assertEqual(len(self.heap()), 1)
        self.feed(dict(self.HELLO, up=5, rst="panic", min=150 * 1024))
        self.assertEqual(len(self.heap()), 2)           # a rebooted board starts over
        self.assertEqual(H.board_stats["rst"], "panic")

    # -------------------------------------------------- mem: line
    def test_mem_note_is_empty_until_a_board_reports(self):
        self.assertEqual(H.board_mem_note(), "")
        self.feed({"t": "ping"})                        # old firmware: still nothing
        self.assertEqual(H.board_mem_note(), "")

    def test_mem_note_reads_in_minutes_and_kb(self):
        self.feed({"t": "ping", "up": 7380, "heap": 140 * 1024,
                   "min": 100 * 1024, "big": 64 * 1024})
        self.assertEqual(H.board_mem_note(),
                         " board up=123min heap=140 KB min=100 KB big=64 KB")

    def test_mem_note_marks_what_the_board_never_sent(self):
        self.feed({"t": "ping", "heap": 140 * 1024})
        self.assertEqual(H.board_mem_note(),
                         " board up=? heap=140 KB min=? KB big=? KB")

    def test_the_note_is_a_tail_a_mem_line_can_just_append(self):
        # mem_loop writes `log(... + board_mem_note())`, so the note carries
        # its own leading space and never its own newline
        self.feed({"t": "ping", "up": 60, "heap": 140 * 1024})
        note = H.board_mem_note()
        self.assertTrue(note.startswith(" board "))
        self.assertNotIn("\n", note)
        self.assertEqual(("mem: rss=42 MB" + note).count("board"), 1)

    # -------------------------------------------------- /state.board + link down
    def test_state_board_carries_the_vitals(self):
        self.feed(dict(self.HELLO, heap=140 * 1024, min=100 * 1024, big=64 * 1024))
        snap = H.state_snapshot()
        b = snap["board"]
        self.assertLessEqual({"up", "heap", "min", "big", "rst", "age_s"}, set(b))
        self.assertEqual((b["up"], b["rst"], b["heap"], b["min"], b["big"]),
                         (7200, "poweron", 140 * 1024, 100 * 1024, 64 * 1024))
        self.assertEqual(b["age_s"], 0)
        self.assertFalse(b["stale"])
        # the hello's own fields stay where `pet status` and /test/* read them
        self.assertEqual(snap["product"], "s3")
        self.assertLessEqual({"sd", "rtc", "fw"}, set(snap))
        json.dumps(snap)                                # what /state serves

    def test_ping_uptime_wins_over_the_hello(self):
        self.feed(self.HELLO)
        self.feed({"t": "ping", "up": 7800})
        self.assertEqual(H.state_snapshot()["board"]["up"], 7800)

    def test_no_board_yet_reads_as_unknown(self):
        H.board_hello_at = 0.0
        b = H.state_snapshot()["board"]
        self.assertEqual([b[k] for k in ("up", "heap", "min", "big", "rst",
                                         "age_s", "hello_age_s")], [None] * 7)

    def test_link_down_keeps_the_last_numbers_and_marks_them_old(self):
        self.feed(dict(self.HELLO, heap=140 * 1024))
        H.boards[:] = [object()]                    # TCP still up, BLE dropped
        H._ble_client = None
        H.board_link_down()
        self.assertFalse(H.board_stats["stale"])    # the other link reports on
        H.boards[:] = []
        H.board_link_down()
        self.assertEqual(H.board_stats["heap"], 140 * 1024)     # not zeroed
        self.assertTrue(H.board_stats["stale"])
        self.assertTrue(H.state_snapshot()["board"]["stale"])


class VoiceToFrontTests(unittest.TestCase):
    """a voice start carrying {"to":"front"} came from a
    desk page, so the dictation belongs to whatever the user has in front on
    the Mac and the host must not raise a seat's window under it. Without the
    field nothing changes, which is what today's board sends."""

    def setUp(self):
        self._saved = {k: getattr(H, k) for k in
                       ("inject", "raise_agent_app", "voice_key", "_frontmost_app",
                        "mic_sink_begin", "mic_sink_open_gate", "log", "post_key",
                        "VOICE_SOURCE", "active_agent", "_voice_to_front")}
        self.raised, self.keys, self.lines, self.posted = [], [], [], []
        H.inject = lambda fn: fn()          # the worker, run inline
        H.raise_agent_app = self.raised.append
        H.voice_key = self.keys.append
        H.post_key = self.posted.append
        H._frontmost_app = lambda: "Notes|Notes"
        H.mic_sink_begin = lambda: True
        H.mic_sink_open_gate = lambda: None
        H.log = lambda *a: self.lines.append(" ".join(str(x) for x in a))
        H.active_agent = "claude"
        H._voice_to_front = False

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(H, k, v)

    def start(self, **extra):
        m = {"t": "voice", "a": "start", "agent": "codex", "link": "ble"}
        m.update(extra)
        H.handle_board_msg(json.dumps(m), lambda b: None)

    def enter(self, **extra):
        m = {"t": "key", "k": "enter"}
        m.update(extra)
        H.handle_board_msg(json.dumps(m), lambda b: None)

    def test_mac_mic_to_front_skips_the_raise(self):
        H.VOICE_SOURCE = "mac"
        self.start(to="front")
        self.assertEqual(self.raised, [])
        self.assertEqual(self.keys, [True])          # fn still goes down
        self.assertIn("voice: -> front (Notes|Notes)", self.lines)

    def test_board_mic_to_front_skips_the_raise(self):
        H.VOICE_SOURCE = "board"
        self.start(to="front", mic=True)
        self.assertEqual(self.raised, [])
        self.assertEqual(self.keys, [True])
        self.assertIn("voice: -> front (Notes|Notes)", self.lines)

    def test_without_the_field_both_paths_still_raise(self):
        H.VOICE_SOURCE = "mac"
        self.start()
        self.assertEqual(self.raised, ["codex"])
        H.VOICE_SOURCE = "board"
        self.start(mic=True)
        self.assertEqual(self.raised, ["codex", "codex"])
        self.assertEqual(self.keys, [True, True])

    def test_any_other_to_value_is_the_old_behaviour(self):
        # "front" is the only value there is; anything else is a board this
        # host does not understand, and the safe reading is the seat
        H.VOICE_SOURCE = "mac"
        self.start(to="seat")
        self.assertEqual(self.raised, ["codex"])

    # ---- the 「⏎ 发送」 bubble that follows such a dictation (2026-09-19) ----
    def test_enter_to_front_types_where_the_dictation_went(self):
        self.enter(to="front")
        self.assertEqual(self.raised, [])              # no open -a, no seat window
        self.assertEqual(self.posted, ["enter"])
        self.assertIn("key: enter -> front (Notes|Notes)", self.lines)

    def test_enter_without_the_field_still_raises_the_seat(self):
        self.enter()
        self.assertEqual(self.raised, ["claude"])      # active_agent
        self.assertEqual(self.posted, ["enter"])

    def test_enter_with_an_unknown_to_is_the_old_behaviour(self):
        self.enter(to="seat")
        self.assertEqual(self.raised, ["claude"])

    def test_a_front_dictation_carries_its_enter_when_the_key_is_unmarked(self):
        """Fallback for a board that marks the voice start but not the key.
        It survives the stop (the bubble is tapped seconds later) and is only
        cleared by the next start."""
        H.VOICE_SOURCE = "mac"
        self.start(to="front")
        H.handle_board_msg(json.dumps({"t": "voice", "a": "stop"}), lambda b: None)
        self.enter()
        self.assertEqual(self.raised, [])
        self.assertEqual(self.posted, ["enter"])
        self.start()                                   # a seat dictation clears it
        self.enter()
        self.assertEqual(self.raised, ["codex", "claude"])

    def test_old_firmware_never_arms_the_fallback(self):
        """A board from before `to` sends none on either line, so
        the fallback stays False and nothing about its enter changes."""
        H.VOICE_SOURCE = "mac"
        self.start()
        self.assertFalse(H._voice_to_front)
        self.enter()
        self.assertEqual(self.raised, ["codex", "claude"])

    def test_approve_is_untouched_by_the_front_flag(self):
        H._voice_to_front = True
        decided = []
        saved = H.decision_key
        H.decision_key = lambda k, sid=None: decided.append(k)
        try:
            H.handle_board_msg(json.dumps({"t": "key", "k": "approve"}), lambda b: None)
            H.handle_board_msg(json.dumps({"t": "key", "k": "reject", "to": "front"}),
                               lambda b: None)
        finally:
            H.decision_key = saved
        self.assertEqual(decided, ["approve", "reject"])
        self.assertEqual(self.posted, [])


class CfgMsgTests(unittest.TestCase):
    def test_mic_link_defaults_to_ble(self):
        self.assertEqual(H.MIC_LINK, "ble")
        self.assertEqual(H.cfg_msg()["mic_link"], "ble")

    def test_show_off_defaults_to_hidden(self):
        self.assertFalse(H.SHOW_OFF_SEATS)
        self.assertEqual(H.cfg_msg()["show_off"], 0)

    def test_show_off_rides_along_as_an_int(self):
        """The firmware parses it with a JSON int reader, so True/False must
        never reach the wire."""
        old = H.SHOW_OFF_SEATS
        try:
            H.SHOW_OFF_SEATS = True
            cfg = H.cfg_msg()
            self.assertEqual(cfg["show_off"], 1)
            self.assertIs(type(cfg["show_off"]), int)
            self.assertIn('"show_off": 1', json.dumps(cfg))
        finally:
            H.SHOW_OFF_SEATS = old

    def test_mic_link_rides_along(self):
        old = H.MIC_LINK
        try:
            H.MIC_LINK = "tcp"
            cfg = H.cfg_msg()
            self.assertEqual(cfg["mic_link"], "tcp")
            self.assertEqual(cfg["t"], "cfg")
        finally:
            H.MIC_LINK = old


# ------------------------------------------------------------------ IMA ADPCM
def ima_decode(data, n_samples):
    """Reference decoder = audio.cpp's imaDecode, transcribed."""
    pred = idx = 0
    out = []
    for i in range(n_samples):
        nib = (data[i >> 1] >> (4 if i & 1 else 0)) & 0x0F
        step = H.IMA_STEP[idx]
        diff = step >> 3
        if nib & 1: diff += step >> 2
        if nib & 2: diff += step >> 1
        if nib & 4: diff += step
        pred = max(-32768, min(32767, pred - diff if nib & 8 else pred + diff))
        idx = max(0, min(88, idx + H.IMA_INDEX[nib & 7]))
        out.append(pred)
    return out


class ImaTests(unittest.TestCase):
    def test_sizes_and_silence(self):
        self.assertEqual(H.ima_encode(b""), b"")
        self.assertEqual(len(H.ima_encode(b"\0" * 2 * 7)), 4)      # 7 samples -> 4 bytes
        self.assertEqual(H.ima_encode(b"\0" * 2 * 100), b"\0" * 50)  # silence stays silent

    def test_sine_round_trip_is_close(self):
        n = 16000
        pcm = struct.pack("<%dh" % n, *[int(12000 * math.sin(2 * math.pi * 440 * i / 16000))
                                        for i in range(n)])
        enc = H.ima_encode(pcm)
        self.assertEqual(len(enc), n // 2)                       # 4:1
        dec = ima_decode(enc, n)
        src = struct.unpack("<%dh" % n, pcm)
        sig = sum(x * x for x in src)
        err = sum((a - b) ** 2 for a, b in zip(src, dec))
        snr = 10 * math.log10(sig / max(err, 1))
        self.assertGreater(snr, 25, f"SNR {snr:.1f} dB")

    def test_step_response_tracks_within_a_few_samples(self):
        pcm = struct.pack("<40h", *([0] * 8 + [20000] * 32))
        dec = ima_decode(H.ima_encode(pcm), 40)
        self.assertGreater(dec[-1], 19000)                      # settled near the target
        self.assertLess(dec[-1], 21000)


# ------------------------------------------------------------------ .ima container
def sine_pcm(n, freq=440, amp=12000, phase=0.0):
    return struct.pack("<%dh" % n, *[int(amp * math.sin(2 * math.pi * freq * (i + phase) / 16000))
                                     for i in range(n)])


class ImaContainerTests(unittest.TestCase):
    B = H.IMA_BLOCK_SAMPLES                                   # 16000 samples = 1 s

    def test_header_is_magic_rate_and_block_count(self):
        c = H.ima_pack(sine_pcm(self.B * 2))
        self.assertEqual(c[:8], b"AGPTIMA1")
        self.assertEqual(H.ima_head(c), (16000, 2))
        self.assertEqual(len(c), H.IMA_HEAD + 2 * H.IMA_BLOCK)
        self.assertEqual(H.ima_head(H.ima_pack(b"", rate=8000)), (8000, 0))

    def test_a_foreign_file_is_refused(self):
        with self.assertRaises(ValueError):
            H.ima_head(b"RIFF....WAVEfmt ")
        with self.assertRaises(ValueError):
            H.ima_unpack(b"")

    def test_last_block_is_short_and_still_carries_a_header(self):
        c = H.ima_pack(sine_pcm(self.B + 5000))
        self.assertEqual(H.ima_head(c), (16000, 2))
        self.assertEqual(len(c), H.IMA_HEAD + H.IMA_BLOCK + H.IMA_BLOCK_HEAD + 2500)
        self.assertEqual(len(H.ima_unpack(c)) // 2, self.B + 5000)

    def test_a_tail_shorter_than_a_nibble_pair_is_dropped(self):
        self.assertEqual(H.ima_head(H.ima_pack(sine_pcm(1))), (16000, 0))   # 1 sample: no block
        c = H.ima_pack(sine_pcm(self.B + 1))                                # 1 sample past a block
        self.assertEqual(H.ima_head(c), (16000, 1))

    def test_block_header_holds_the_state_entering_that_block(self):
        pcm = sine_pcm(self.B * 3)
        c = H.ima_pack(pcm)
        self.assertEqual(struct.unpack("<hBB", c[H.IMA_HEAD:H.IMA_HEAD + 4]), (0, 0, 0))
        pred = idx = 0
        for k in range(3):
            off = H.ima_block_offset(k)
            self.assertEqual(struct.unpack("<hBB", c[off:off + 4]), (pred, idx, 0),
                             "block %d header" % k)
            _blk, (pred, idx) = H.ima_block_encode(pcm[k * self.B * 2:(k + 1) * self.B * 2],
                                                   pred, idx)

    def test_decoding_from_any_block_matches_decoding_from_the_top(self):
        """The whole point of the container: the board can resume mid-file."""
        c = H.ima_pack(sine_pcm(self.B * 3 + 800, freq=173))
        whole = H.ima_unpack(c)
        for k in range(4):
            self.assertEqual(H.ima_unpack(c, first=k), whole[k * self.B * 2:],
                             "block %d start" % k)
        self.assertEqual(H.ima_unpack(c, first=1, count=1),
                         whole[self.B * 2:self.B * 4])

    @unittest.skipIf(H.audioop is None, "audioop is gone (3.13+)")
    def test_each_block_decodes_to_what_audioop_says(self):
        """audioop.lin2adpcm packs sample 0 in the HIGH nibble, audio.cpp reads
        the LOW one first — hence IMA_NIBSWAP. Same samples, mirrored bytes."""
        c = H.ima_pack(sine_pcm(self.B * 2 + 4000, freq=97, amp=20000))
        for k in range(3):
            off = H.ima_block_offset(k)
            pred, idx, _ = struct.unpack("<hBB", c[off:off + 4])
            body = c[off + 4:off + H.IMA_BLOCK]
            ref, _st = H.audioop.adpcm2lin(body.translate(H.IMA_NIBSWAP), 2, (pred, idx))
            self.assertEqual(H.ima_unpack(c, first=k, count=1), ref, "block %d" % k)

    def test_bytes_match_the_pure_python_encoder(self):
        pcm = sine_pcm(self.B + 1000, freq=311)
        blk, st = H.ima_block_encode(pcm[:self.B * 2])
        pure, st2 = H.ima_encode_state(pcm[:self.B * 2])
        self.assertEqual(blk[4:], pure)
        self.assertEqual(st, st2)
        self.assertEqual(blk[4:5000], H.ima_encode(pcm[:self.B * 2])[:4996])

    def test_round_trip_is_recognisably_the_same_audio(self):
        n = self.B
        pcm = sine_pcm(n, freq=440)
        dec = struct.unpack("<%dh" % n, H.ima_unpack(H.ima_pack(pcm)))
        src = struct.unpack("<%dh" % n, pcm)
        err = sum((a - b) ** 2 for a, b in zip(src, dec))
        snr = 10 * math.log10(sum(x * x for x in src) / max(err, 1))
        self.assertGreater(snr, 25, "SNR %.1f dB" % snr)

    def test_pack_file_streams_the_same_bytes_as_pack(self):
        pcm = sine_pcm(self.B * 2 + 777, freq=220)
        with tempfile.TemporaryDirectory() as d:
            wav, ima = Path(d) / "a.wav", Path(d) / "a.ima"
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
                w.writeframes(pcm)
            r = H.ima_pack_file(wav, ima)
            self.assertEqual(r["blocks"], 3)
            self.assertEqual(ima.read_bytes(), H.ima_pack(pcm))
            self.assertFalse((Path(d) / "a.ima.part").exists())

    def test_pack_file_reads_the_extensible_wav_afconvert_writes(self):
        """afconvert -f WAVE on an m4a source emits WAVE_FORMAT_EXTENSIBLE with
        a filler chunk before the data; wave.open dies on it ("unknown format:
        65534"), which is every podcast episode. Same bytes out either way."""
        pcm = sine_pcm(20000, freq=180)
        fmt = struct.pack("<HHIIHH", 0xFFFE, 1, 16000, 32000, 2, 16) + \
            struct.pack("<HHI", 22, 16, 4) + b"\x01\x00\x00\x00\x00\x00\x10\x00" \
            b"\x80\x00\x00\xaa\x00\x38\x9b\x71"
        body = (b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt +
                b"FLLR" + struct.pack("<I", 8) + b"\0" * 8 +
                b"data" + struct.pack("<I", len(pcm)) + pcm)
        with tempfile.TemporaryDirectory() as d:
            wav, ima = Path(d) / "x.wav", Path(d) / "x.ima"
            wav.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
            with self.assertRaises(Exception):                  # the stdlib reader gives up
                wave.open(str(wav), "rb")
            self.assertEqual(H.ima_pack_file(wav, ima)["blocks"], 2)
            self.assertEqual(ima.read_bytes(), H.ima_pack(pcm))

    def test_pack_file_refuses_the_wrong_format(self):
        with tempfile.TemporaryDirectory() as d:
            wav = Path(d) / "s.wav"
            with wave.open(str(wav), "wb") as w:
                w.setnchannels(2); w.setsampwidth(2); w.setframerate(16000)
                w.writeframes(b"\0" * 400)
            with self.assertRaises(ValueError):
                H.ima_pack_file(wav, Path(d) / "s.ima")
            wav2 = Path(d) / "r.wav"
            with wave.open(str(wav2), "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100)
                w.writeframes(b"\0" * 400)
            with self.assertRaises(ValueError):
                H.ima_pack_file(wav2, Path(d) / "r.ima")
            bad = Path(d) / "n.wav"
            bad.write_bytes(b"ID3\x04not a wav at all")
            with self.assertRaises(ValueError):
                H.ima_pack_file(bad, Path(d) / "n.ima")


# ------------------------------------------------------------------ audio onto the card
FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" version="2.0"><channel>
 <title>暂停实验</title>
 <itunes:image href="https://img.example/show.jpg"/>
 <item><title>older</title>
  <enclosure url="https://cdn.example/a/old.m4a" type="audio/mp4" length="10"/>
  <itunes:duration>22:31</itunes:duration>
  <pubDate>Sun, 02 Aug 2026 23:00:00 GMT</pubDate></item>
 <item><title>newest</title>
  <enclosure url="https://cdn.example/a/new.m4a" type="audio/mp4" length="80071628"/>
  <itunes:duration>01:22:31</itunes:duration>
  <itunes:image href="https://img.example/ep.jpg"/>
  <pubDate>Wed, 26 Aug 2026 23:00:00 GMT</pubDate></item>
 <item><title>no audio here</title><pubDate>Wed, 27 Aug 2026 23:00:00 GMT</pubDate></item>
</channel></rss>""".encode()   # bytes, like a real download


class AudioMetaTests(unittest.TestCase):
    def test_duration_accepts_every_shape_itunes_uses(self):
        self.assertEqual(H.au_duration("01:22:31"), 4951)
        self.assertEqual(H.au_duration("22:31"), 1351)
        self.assertEqual(H.au_duration("4951"), 4951)
        self.assertEqual(H.au_duration("4951.4"), 4951)
        self.assertEqual(H.au_duration(""), 0)
        self.assertEqual(H.au_duration("about an hour"), 0)
        self.assertEqual(H.au_duration(None), 0)

    def test_pub_date_parses_rfc822_and_falls_back_to_today(self):
        self.assertEqual(H.au_pub_date("Wed, 26 Aug 2026 23:00:00 GMT")[0], "2026-08-26")
        self.assertGreater(H.au_pub_date("Wed, 26 Aug 2026 23:00:00 GMT")[1], 0)
        day = datetime.date(2011, 3, 4)
        self.assertEqual(H.au_pub_date("", today=day), ("2011-03-04", 0.0))
        self.assertEqual(H.au_pub_date("last tuesday", today=day), ("2011-03-04", 0.0))

    def test_name_is_the_date_plus_a_hash_of_the_source(self):
        n = H.au_name("https://cdn.example/a/new.m4a", "2026-08-26")
        self.assertTrue(n.startswith("20260826-"), n)
        self.assertEqual(len(n), 8 + 1 + 8)
        self.assertEqual(n, H.au_name("https://cdn.example/a/new.m4a", "2026-08-26"))
        self.assertNotEqual(n, H.au_name("https://cdn.example/a/old.m4a", "2026-08-26"))
        self.assertTrue(H.au_name("x", "").startswith(
            datetime.date.today().strftime("%Y%m%d")))

    def test_extension_comes_from_the_url_then_the_content_type(self):
        self.assertEqual(H.au_ext("https://x/y.m4a?token=1"), ".m4a")
        self.assertEqual(H.au_ext("https://x/y.MP3"), ".mp3")
        self.assertEqual(H.au_ext("https://x/stream", "audio/mpeg; charset=x"), ".mp3")
        self.assertEqual(H.au_ext("https://x/stream", "audio/mp4"), ".m4a")
        self.assertEqual(H.au_ext("https://x/stream", ""), ".bin")

    def test_feed_sniffing_tells_xml_from_audio(self):
        self.assertTrue(H.au_looks_rss(FEED[:300], "application/rss+xml"))
        self.assertTrue(H.au_looks_rss(FEED[:300], ""))            # body says <?xml
        self.assertFalse(H.au_looks_rss(b"\xff\xfb\x90d", "audio/mpeg"))
        self.assertFalse(H.au_looks_rss(b"\x00\x00\x00 ftypM4A ", ""))

    def test_sidecar_holds_what_the_board_draws(self):
        side = H.au_sidecar({"title": "T" * 200, "show": "S", "date": "2026-08-26"},
                            4951, "/agentpet/covers/abc.jpg")
        self.assertEqual(side["dur"], 4951)
        self.assertEqual(side["date"], "2026-08-26")
        self.assertEqual(side["cover"], "/agentpet/covers/abc.jpg")
        self.assertEqual(len(side["title"]), 120)                  # clipped, in characters
        self.assertEqual(sorted(side), ["cover", "date", "dur", "show", "title"])
        self.assertEqual(H.au_sidecar({}, 0, "")["cover"], "")


class AudioRssTests(unittest.TestCase):
    def test_newest_episode_first_with_the_itunes_fields(self):
        f = H.au_rss_parse(FEED, 1)
        self.assertEqual(f["show"], "暂停实验")
        self.assertEqual(f["cover"], "https://img.example/show.jpg")
        self.assertEqual(len(f["items"]), 1)
        e = f["items"][0]
        self.assertEqual(e["title"], "newest")
        self.assertEqual(e["url"], "https://cdn.example/a/new.m4a")
        self.assertEqual(e["dur"], 4951)
        self.assertEqual(e["date"], "2026-08-26")
        self.assertEqual(e["cover"], "https://img.example/ep.jpg")   # per-episode art wins

    def test_items_without_an_enclosure_are_skipped(self):
        f = H.au_rss_parse(FEED, 0)
        self.assertEqual([e["title"] for e in f["items"]], ["newest", "older"])
        self.assertEqual(f["items"][1]["cover"], "https://img.example/show.jpg")

    def test_n_slices_after_the_sort(self):
        self.assertEqual([e["title"] for e in H.au_rss_parse(FEED, 5)["items"]],
                         ["newest", "older"])
        self.assertEqual([e["title"] for e in H.au_rss_parse(FEED, 2)["items"]],
                         ["newest", "older"])

    def test_not_a_feed_raises(self):
        with self.assertRaises(ValueError):
            H.au_rss_parse(b"<html><body>hi</body></html>")
        with self.assertRaises(Exception):
            H.au_rss_parse(b"not xml at all")


class AudioLibraryTests(unittest.TestCase):
    """au_list / au_rm / au_add against a throwaway ~/.agentpet/audio."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = (H.AUDIO_DIR, H.AUDIO_PUSHED, H._au_pushed)
        H.AUDIO_DIR = Path(self.tmp.name)
        H.AUDIO_PUSHED = H.AUDIO_DIR / "pushed.json"
        H._au_pushed = None

    def tearDown(self):
        H.AUDIO_DIR, H.AUDIO_PUSHED, H._au_pushed = self.old
        self.tmp.cleanup()

    def _episode(self, name, **side):
        (H.AUDIO_DIR / (name + ".ima")).write_bytes(H.ima_pack(b"\0" * 4000))
        s = {"title": "T", "show": "S", "dur": 60, "date": "2026-08-26", "cover": ""}
        s.update(side)
        (H.AUDIO_DIR / (name + ".json")).write_text(json.dumps(s, ensure_ascii=False))

    def test_only_episodes_with_a_sidecar_are_listed_and_they_sort_by_date(self):
        self._episode("20260826-aaaaaaaa", title="new one")
        self._episode("20260102-bbbbbbbb", title="old one")
        (H.AUDIO_DIR / "20260901-cccccccc.ima").write_bytes(b"half pushed")  # no sidecar
        self.assertEqual(H.au_names(), ["20260102-bbbbbbbb", "20260826-aaaaaaaa"])
        items = H.au_list()
        self.assertEqual([i["title"] for i in items], ["old one", "new one"])
        self.assertEqual([i["pushed"] for i in items], [False, False])
        self.assertGreater(items[0]["size"], 16)

    def test_pushed_bookkeeping_survives_a_reload(self):
        self._episode("20260826-aaaaaaaa")
        H.au_pushed()["20260826-aaaaaaaa"] = {"at": 1, "bytes": 2}
        H.au_pushed_write()
        H._au_pushed = None
        self.assertTrue(H.au_list()[0]["pushed"])
        self.assertEqual([n for n in H.au_names() if n not in H.au_pushed()], [])

    def test_rm_deletes_both_files_and_forgets_the_push(self):
        self._episode("20260826-aaaaaaaa")
        H.au_pushed()["20260826-aaaaaaaa"] = {"at": 1}
        r = H.au_rm("20260826-aaaaaaaa.ima")                       # extension tolerated
        self.assertTrue(r["ok"])
        self.assertEqual(sorted(r["local"]), ["20260826-aaaaaaaa.ima",
                                              "20260826-aaaaaaaa.json"])
        self.assertEqual(H.au_names(), [])
        self.assertNotIn("20260826-aaaaaaaa", H.au_pushed())

    def test_rm_refuses_a_path_instead_of_reducing_it_to_a_basename(self):
        for bad in ("", "../../etc/passwd", "a b", "/agentpet/audio/x", "..", ".hidden",
                    "x/y", "a;rm -rf"):
            r = H.au_rm(bad)
            self.assertFalse(r["ok"], bad)
            self.assertIn("bad id", r["why"], bad)

    def test_rm_says_no_when_there_was_nothing_to_delete(self):
        r = H.au_rm("20260826-aaaaaaaa")            # well-formed, simply not here
        self.assertFalse(r["ok"])
        self.assertIn("no such episode", r["why"])

    def test_add_validates_before_it_queues(self):
        seen, old = [], H.au_enqueue
        H.au_enqueue = lambda task: (seen.append(task), 1)[1]
        try:
            self.assertFalse(H.au_add("ftp://x/y.mp3")["ok"])
            self.assertFalse(H.au_add("", "relative/path.mp3")["ok"])
            self.assertFalse(H.au_add("", "/no/such/file.mp3")["ok"])
            self.assertEqual(seen, [])
            r = H.au_add("https://feed.example/x", n="3")
            self.assertTrue(r["ok"])
            self.assertEqual(seen[0]["n"], 3)
            self.assertEqual(H.au_add("https://feed.example/x", n="99")["n"], H.AUDIO_MAX_N)
            self.assertEqual(H.au_add("https://feed.example/x", n="junk")["n"], 1)
        finally:
            H.au_enqueue = old

    def test_resolve_tells_a_feed_from_a_direct_link_and_a_local_file(self):
        """The one network call is stubbed; what is under test is the routing."""
        calls, old = [], H.au_get
        H.au_get = lambda url, cap, what="file": (calls.append(url), (FEED, "application/xml"))[1]
        try:
            eps = H.au_resolve({"kind": "url", "src": "https://feed.example/x", "n": 2})
            self.assertEqual(calls, ["https://feed.example/x"])
            self.assertEqual([e["title"] for e in eps], ["newest", "older"])
            self.assertEqual(eps[0]["show"], "暂停实验")
            self.assertEqual(eps[0]["src"], "https://cdn.example/a/new.m4a")
            self.assertEqual(eps[0]["kind"], "url")

            calls[:] = []                                   # an audio extension skips the probe
            eps = H.au_resolve({"kind": "url", "src": "https://cdn.example/a/one.m4a?t=1"})
            self.assertEqual(calls, [])
            self.assertEqual(eps[0]["src"], "https://cdn.example/a/one.m4a?t=1")
            self.assertEqual(eps[0]["title"], "one.m4a")
            self.assertEqual(eps[0]["date"], datetime.date.today().strftime("%Y-%m-%d"))

            H.au_get = lambda url, cap, what="file": (b"\xff\xfb\x90d", "audio/mpeg")
            eps = H.au_resolve({"kind": "url", "src": "https://cdn.example/stream"})
            self.assertEqual(eps[0]["src"], "https://cdn.example/stream")
            self.assertEqual(eps[0]["show"], "")
        finally:
            H.au_get = old

    def test_resolve_refuses_a_feed_with_nothing_to_play(self):
        old = H.au_get
        H.au_get = lambda url, cap, what="file": (
            b"<?xml version='1.0'?><rss><channel><title>x</title></channel></rss>", "text/xml")
        try:
            with self.assertRaises(ValueError):
                H.au_resolve({"kind": "url", "src": "https://feed.example/empty"})
        finally:
            H.au_get = old

    def test_resolve_of_a_missing_file_says_so(self):
        with self.assertRaises(ValueError):
            H.au_resolve({"kind": "file", "src": "/no/such/thing.mp3"})

    def test_status_reports_the_pending_queue_without_a_board(self):
        self._episode("20260826-aaaaaaaa")
        st = H.au_status()
        self.assertEqual(st["pending"], ["20260826-aaaaaaaa"])
        self.assertFalse(st["tcp"])
        self.assertIsNone(st["job"])
        self.assertFalse(st["busy"])

    def test_pending_push_skips_what_the_card_already_has(self):
        self._episode("20260826-aaaaaaaa")
        self._episode("20260102-bbbbbbbb")
        H.au_pushed()["20260102-bbbbbbbb"] = {"at": 1}
        tried, old = [], H.au_push_one
        H.au_push_one = lambda name, cover="": (tried.append(name), {"ok": True})[1]
        try:
            H.au_push_pending()
        finally:
            H.au_push_one = old
        self.assertEqual(tried, ["20260826-aaaaaaaa"])

    def test_pending_push_stands_aside_while_a_job_owns_the_card(self):
        self._episode("20260826-aaaaaaaa")
        tried, old = [], H.au_push_one
        H.au_push_one = lambda name, cover="": (tried.append(name), {"ok": True})[1]
        H.au_rt["job"] = {"name": "busy", "step": "push", "t0": time.time()}
        try:
            self.assertEqual(H.au_push_pending(), [])
            self.assertEqual(tried, [])
        finally:
            H.au_push_one = old
            H.au_rt["job"] = None

    def test_pending_push_clears_its_job_even_when_the_push_throws(self):
        self._episode("20260826-aaaaaaaa")
        old = H.au_push_one
        H.au_push_one = lambda name, cover="": (_ for _ in ()).throw(OSError("card gone"))
        try:
            with self.assertRaises(OSError):
                H.au_push_pending()
        finally:
            H.au_push_one = old
        self.assertIsNone(H.au_rt["job"])

    def test_push_needs_local_files_and_a_tcp_board(self):
        self.assertFalse(H.au_push_one("nope")["ok"])
        self._episode("20260826-aaaaaaaa")
        r = H.au_push_one("20260826-aaaaaaaa")
        self.assertFalse(r["ok"])
        self.assertIn("no TCP board", r["why"])


# ------------------------------------------------------------------ now playing
# One real `media-control stream` line, captured 2026-09-08 off the Chrome tab
# the user was watching (no artwork: Chrome does not publish one).
NP_CHROME = {
    "artist": "", "album": "", "playbackRate": 1,
    "contentItemIdentifier": "C81A02E2-EB3A-4824-BE57-EE030B1A69BD",
    "title": "无悔追踪第16集-电视剧-全集-高清正版在线观看-bilibili-哔哩哔哩",
    "elapsedTime": 741.74942599999997, "duration": 2792, "playing": True,
    "bundleIdentifier": "com.google.Chrome", "processIdentifier": 627,
    "timestamp": "2026-09-08T13:46:50Z"}


class NowPlayingLabelTests(unittest.TestCase):
    def test_known_bundles(self):
        self.assertEqual(H.np_app_label("com.netease.163music"), "netease")
        self.assertEqual(H.np_app_label("com.apple.Music"), "music")
        self.assertEqual(H.np_app_label("com.apple.podcasts"), "podcasts")
        self.assertEqual(H.np_app_label("com.google.Chrome"), "chrome")

    def test_unknown_bundle_is_last_segment_lowercased(self):
        self.assertEqual(H.np_app_label("com.spotify.client"), "client")
        self.assertEqual(H.np_app_label("com.apple.QuickTimePlayerX"), "quicktimeplayerx")
        self.assertEqual(H.np_app_label("VLC"), "vlc")

    def test_empty(self):
        self.assertEqual(H.np_app_label(""), "")
        self.assertEqual(H.np_app_label(None), "")


class NowPlayingMessageTests(unittest.TestCase):
    def test_real_chrome_payload(self):
        m = H.np_from_state(NP_CHROME, "/agentpet/covers/abc123.jpg")
        self.assertEqual(m["t"], "np")
        self.assertEqual(m["on"], 1)
        self.assertEqual(m["play"], 1)
        self.assertEqual(m["app"], "chrome")
        self.assertEqual(m["pos"], 741.7)
        self.assertEqual(m["dur"], 2792)
        self.assertEqual(m["rate"], 1)
        self.assertEqual(m["cover"], "/agentpet/covers/abc123.jpg")
        self.assertTrue(m["title"].startswith("无悔追踪第16集"))
        self.assertEqual(m["artist"], "")
        # the wire line has to stay small enough for the BLE path
        self.assertLess(len(json.dumps(m, ensure_ascii=False).encode()), 400)

    def test_no_title_means_nothing_playing(self):
        self.assertEqual(H.np_from_state({}), {"t": "np", "on": 0})
        self.assertEqual(H.np_from_state(None), {"t": "np", "on": 0})
        self.assertEqual(H.np_from_state({"title": "   ", "playing": True}),
                         {"t": "np", "on": 0})

    def test_truncation_is_by_character(self):
        m = H.np_from_state({"title": "无" * 100, "artist": "喵" * 80,
                             "album": "x" * 200})
        self.assertEqual(len(m["title"]), 60)
        self.assertEqual(len(m["artist"]), 60)
        self.assertEqual(len(m["album"]), 60)
        self.assertEqual(m["title"], "无" * 60)

    def test_paused_and_missing_numbers(self):
        m = H.np_from_state({"title": "t", "playing": False,
                             "duration": None, "elapsedTime": "nope"})
        self.assertEqual(m["play"], 0)
        self.assertEqual(m["dur"], 0.0)
        self.assertEqual(m["pos"], 0.0)
        self.assertEqual(m["rate"], 1.0)          # a player that omits the rate is 1x

    def test_newlines_collapse(self):
        m = H.np_from_state({"title": " a\nb\r\n "})
        self.assertEqual(m["title"], "a b")


class NowPlayingAdvanceTests(unittest.TestCase):
    """The 15 s refresh has to carry a position the player is really at:
    media-control says nothing at all while a track just plays."""
    def test_playing_moves_with_the_clock(self):
        st = H.np_advance(NP_CHROME, 10.0)
        self.assertAlmostEqual(st["elapsedTime"], 751.749, places=2)
        self.assertEqual(NP_CHROME["elapsedTime"], 741.74942599999997)   # input untouched

    def test_rate_is_honoured(self):
        st = H.np_advance(dict(NP_CHROME, playbackRate=2), 10.0)
        self.assertAlmostEqual(st["elapsedTime"], 761.749, places=2)

    def test_playing_without_a_rate_counts_as_1x(self):
        st = H.np_advance(dict(NP_CHROME, playbackRate=0), 10.0)
        self.assertAlmostEqual(st["elapsedTime"], 751.749, places=2)

    def test_paused_stands_still(self):
        paused = dict(NP_CHROME, playing=False, playbackRate=0)
        self.assertEqual(H.np_advance(paused, 600.0)["elapsedTime"], paused["elapsedTime"])

    def test_never_runs_past_the_end(self):
        self.assertEqual(H.np_advance(NP_CHROME, 99999.0)["elapsedTime"], 2792)

    def test_no_entry_and_no_time(self):
        self.assertEqual(H.np_advance({}, 10.0), {})
        self.assertEqual(H.np_advance(NP_CHROME, 0.0), NP_CHROME)


class NowPlayingMergeTests(unittest.TestCase):
    def test_full_line_replaces(self):
        st = H.np_merge({"title": "old", "album": "gone"},
                        {"type": "data", "diff": False, "payload": {"title": "new"}})
        self.assertEqual(st, {"title": "new"})

    def test_diff_line_merges(self):
        st = H.np_merge({"title": "t", "playing": True},
                        {"type": "data", "diff": True, "payload": {"playing": False}})
        self.assertEqual(st, {"title": "t", "playing": False})

    def test_empty_payload_is_nothing_playing(self):
        st = H.np_merge(NP_CHROME, {"type": "data", "diff": False, "payload": {}})
        self.assertEqual(st, {})
        self.assertEqual(H.np_from_state(st), {"t": "np", "on": 0})

    def test_junk_lines_keep_the_state(self):
        for line in ({"type": "heartbeat"}, {"payload": {"title": "x"}},
                     {"type": "data", "diff": False, "payload": None}, [1, 2], "hi"):
            self.assertEqual(H.np_merge({"title": "keep"}, line), {"title": "keep"})

    def test_merge_does_not_mutate_its_input(self):
        st = {"title": "t"}
        H.np_merge(st, {"type": "data", "diff": True, "payload": {"title": "u"}})
        self.assertEqual(st, {"title": "t"})


class NowPlayingThrottleTests(unittest.TestCase):
    def msg(self, **kw):
        base = {"t": "np", "on": 1, "title": "a", "artist": "b", "album": "c",
                "pos": 10.0, "dur": 100.0, "play": 1, "rate": 1, "app": "music",
                "cover": ""}
        base.update(kw)
        return base

    def test_change_sends_at_once(self):
        prev = self.msg()
        for field, value in (("title", "z"), ("artist", "z"), ("album", "z"),
                             ("play", 0), ("app", "chrome"),
                             ("cover", "/agentpet/covers/a.jpg")):
            self.assertTrue(H.np_should_send(prev, self.msg(**{field: value}),
                                             1000.0, 1000.5), field)

    def test_no_change_stays_quiet_until_the_refresh(self):
        prev = self.msg()
        cur = self.msg(pos=14.0)                  # only the position moved
        self.assertFalse(H.np_should_send(prev, cur, 1000.0, 1004.0))
        self.assertFalse(H.np_should_send(prev, cur, 1000.0, 1014.9))
        self.assertTrue(H.np_should_send(prev, cur, 1000.0, 1015.0))

    def test_paused_entry_still_refreshes(self):
        prev = self.msg(play=0)
        self.assertTrue(H.np_should_send(prev, self.msg(play=0), 1000.0, 1020.0))

    def test_off_is_sent_once(self):
        off = {"t": "np", "on": 0}
        self.assertTrue(H.np_should_send(self.msg(), off, 1000.0, 1000.1))
        self.assertFalse(H.np_should_send(off, off, 1000.0, 1099.0))

    def test_first_message_only_when_something_plays(self):
        self.assertFalse(H.np_should_send(None, {"t": "np", "on": 0}, 0.0, 1000.0))
        self.assertTrue(H.np_should_send(None, self.msg(), 0.0, 1000.0))

    def test_garbage_never_sends(self):
        self.assertFalse(H.np_should_send(self.msg(), None, 1000.0, 2000.0))


class NowPlayingCoverTests(unittest.TestCase):
    def test_hash_is_12_hex_of_the_raw_bytes(self):
        raw = b"\x89PNG\r\n\x1a\n fake artwork"
        h = H.np_cover_hash(raw)
        self.assertEqual(h, hashlib.md5(raw).hexdigest()[:12])
        self.assertEqual(len(h), 12)
        self.assertTrue(all(c in "0123456789abcdef" for c in h))
        self.assertNotEqual(h, H.np_cover_hash(raw + b"!"))

    def test_paths(self):
        h = "0123456789ab"
        self.assertEqual(H.np_cover_card(h), "/agentpet/covers/0123456789ab.jpg")
        self.assertEqual(H.np_cover_local(h).name, "0123456789ab.jpg")
        self.assertTrue(str(H.np_cover_local(h)).endswith("/.agentpet/covers/0123456789ab.jpg"))

    def test_media_cmd_map(self):
        self.assertEqual(H.NP_MEDIA_CMDS["toggle"], "toggle-play-pause")
        self.assertEqual(H.NP_MEDIA_CMDS["next"], "next-track")
        self.assertEqual(H.NP_MEDIA_CMDS["prev"], "previous-track")

    def test_unknown_media_cmd_is_refused_without_running_anything(self):
        r = H.media_cmd("rm -rf")
        self.assertFalse(r["ok"])
        self.assertIn("unknown", r["why"])

    def test_a_cover_already_on_the_card_is_not_pushed_again(self):
        tmp = tempfile.TemporaryDirectory()
        old_dir, old_pushed = H.NP_COVERS_DIR, H._np_pushed
        try:
            H.NP_COVERS_DIR = Path(tmp.name)
            (Path(tmp.name) / "abc123abc123.jpg").write_bytes(b"jpeg")
            H._np_pushed = {"abc123abc123": 1788876424}
            r = H.np_cover_push("abc123abc123")        # covers.json says the card has it
            self.assertTrue(r["ok"])
            self.assertEqual(r["skipped"], "already pushed")
            self.assertEqual(r["dst"], "/agentpet/covers/abc123abc123.jpg")
            self.assertFalse(H.np_cover_push("nosuchcover1")["ok"])
        finally:
            H.NP_COVERS_DIR, H._np_pushed = old_dir, old_pushed
            tmp.cleanup()

    def test_cover_pushes_wait_for_a_settled_board(self):
        old = H.board_hello_at
        try:
            H.board_hello_at = time.time()             # just rebooted after an OTA
            self.assertFalse(H.np_board_settled())
            H.board_hello_at = time.time() - H.NP_HELLO_SETTLE - 1
            self.assertTrue(H.np_board_settled())
        finally:
            H.board_hello_at = old

    def test_np_message_names_the_cover_only_once_the_card_has_it(self):
        # 2026-09-22: naming it early = every new cover pushed twice (np_miss raced the first push)
        self.assertEqual(H.np_cover_for_msg("abc123abc123", {}), "")
        self.assertEqual(H.np_cover_for_msg("abc123abc123", {"abc123abc123": 1}),
                         "/agentpet/covers/abc123abc123.jpg")
        self.assertEqual(H.np_cover_for_msg("", {"abc123abc123": 1}), "")
        self.assertIn("cover", H.NP_KEYS)     # the follow-up np with the path is never throttled away

    def test_np_miss_lets_an_in_flight_or_just_landed_cover_be(self):
        saved = dict(H.np_rt["miss"])
        H.np_rt["miss"].clear()
        try:
            H.np_rt["cov_inflight"].add("feedfacef00d")
            self.assertEqual(H.np_miss("/agentpet/covers/feedfacef00d.jpg")["why"], "in flight")
            H.np_rt["cov_inflight"].discard("feedfacef00d")
            H.np_rt["cov_done"]["feedfacef00d"] = time.time()
            self.assertEqual(H.np_miss("/agentpet/covers/feedfacef00d.jpg")["why"], "just landed")
            H.np_rt["cov_done"]["feedfacef00d"] = time.time() - H.NP_MISS_GRACE - 1
            r = H.np_miss("/agentpet/covers/feedfacef00d.jpg")   # neither guard: goes looking for it
            self.assertIn("feedfacef00d", r["why"])
        finally:
            H.np_rt["cov_inflight"].discard("feedfacef00d")
            H.np_rt["cov_done"].pop("feedfacef00d", None)
            H.np_rt["miss"].clear()
            H.np_rt["miss"].update(saved)

    def test_np_miss_takes_the_hash_from_the_card_path_and_cools_down(self):
        saved = dict(H.np_rt["miss"])
        H.np_rt["miss"].clear()
        try:
            r = H.np_miss("/agentpet/covers/deadbeef0000.jpg")
            self.assertFalse(r["ok"])
            self.assertIn("deadbeef0000", r["why"])     # went looking for that cover
            self.assertEqual(H.np_miss("/agentpet/covers/deadbeef0000.jpg")["why"],
                             "cooldown")
            self.assertFalse(H.np_miss("")["ok"])
        finally:
            H.np_rt["miss"].clear()
            H.np_rt["miss"].update(saved)


def lock_free_elsewhere(lock):
    """Could ANOTHER thread start a transfer right now? (An RLock is always
    re-acquirable by the thread that owns it, so ask from a fresh one.)"""
    out = []

    def probe():
        got = lock.acquire(blocking=False)
        if got:
            lock.release()
        out.append(got)
    t = threading.Thread(target=probe)
    t.start()
    t.join(3)
    return out[0]


def funcs_that_call(src, method):
    """{function name -> } for every `x.<method>(...)` call in the source."""
    out, stack = set(), []

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def visit_Call(self, node):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == method:
                out.add(stack[-1] if stack else "<module>")
            self.generic_visit(node)
    V().visit(ast.parse(src))
    return out


def funcs_calling_name(src, name):
    """{function name -> } for every bare `<name>(...)` call in the source.
    funcs_that_call's sibling: that one matches `x.method(...)`, this one a
    plain function call. Nested `async def`s are not pushed on the stack in
    either, so a call inside `ble_send`'s `_w()` reads as `ble_send`."""
    out, stack = set(), []

    class V(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def visit_Call(self, node):
            if isinstance(node.func, ast.Name) and node.func.id == name:
                out.add(stack[-1] if stack else "<module>")
            self.generic_visit(node)
    V().visit(ast.parse(src))
    return out


class BoardWritePathTests(unittest.TestCase):
    """One door onto the board sockets. 2026-09-08 push_state did its own
    sendall without send_lock and spliced a state line into the middle of a
    5.6 KB fdat batch; two OTAs died with why=b64."""
    def test_only_tcp_send_and_the_connection_reply_touch_a_socket(self):
        src = (HERE / "agentpet_host.py").read_text()
        self.assertEqual(
            funcs_that_call(src, "sendall"), {"tcp_send", "reply"},
            "board writes go through tcp_send (or the per-connection reply), "
            "which hold send_lock; a bare sendall interleaves with file pushes")

    def test_push_state_holds_send_lock_while_writing(self):
        seen = []

        class FakeSock:
            def sendall(self, data):
                seen.append((bytes(data), lock_free_elsewhere(H.send_lock)))
        sock = FakeSock()
        with H.lock:
            H.boards.append(sock)
        try:
            H.push_state()
        finally:
            with H.lock:
                H.boards.remove(sock)
        self.assertEqual(len(seen), 1)
        data, free = seen[0]
        self.assertTrue(data.endswith(b"\n"))
        self.assertEqual(json.loads(data)["t"], "state")
        self.assertFalse(free, "send_lock must be held for the whole line")

    def test_a_socket_that_dies_is_dropped_from_boards(self):
        class DeadSock:
            def sendall(self, data):
                raise OSError("broken pipe")
        sock = DeadSock()
        with H.lock:
            H.boards.append(sock)
        try:
            H.push_state()
            with H.lock:
                self.assertNotIn(sock, H.boards)
        finally:
            with H.lock:
                if sock in H.boards:
                    H.boards.remove(sock)


class FilePushLockTests(unittest.TestCase):
    """The board has one file transfer slot: a second fbeg replaces the first.
    2026-09-08 a cover push landed inside an SD-OTA and killed it twice."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.src = Path(self._tmp.name) / "payload.bin"
        self.src.write_bytes(b"x" * 64)

    def tearDown(self):
        self._tmp.cleanup()

    def test_lock_is_reentrant_so_board_ota_can_call_push_file(self):
        with H._file_push_lock:
            again = H._file_push_lock.acquire(blocking=False)
            if again:
                H._file_push_lock.release()
        self.assertTrue(again, "board_ota holds the lock and then calls push_file")

    def test_push_file_queues_behind_whoever_holds_it(self):
        order, ready = [], threading.Event()

        def holder():
            with H._file_push_lock:
                ready.set()
                time.sleep(0.4)
                order.append("holder")
        t = threading.Thread(target=holder)
        t.start()
        self.assertTrue(ready.wait(3))
        r = H.push_file(str(self.src), "/agentpet/x")   # no board: fails, but only after waiting
        order.append("push_file")
        t.join(3)
        self.assertEqual(order, ["holder", "push_file"])
        self.assertIn("no TCP board", r["why"])

    def test_push_file_with_a_wait_gives_up_instead_of_queueing(self):
        ready = threading.Event()

        def holder():
            with H._file_push_lock:
                ready.set()
                time.sleep(1.0)
        t = threading.Thread(target=holder)
        t.start()
        self.assertTrue(ready.wait(3))
        t0 = time.time()
        r = H.push_file(str(self.src), "/agentpet/x", wait=0.2)
        self.assertLess(time.time() - t0, 0.8)
        self.assertFalse(r["ok"])
        self.assertTrue(r["busy"])
        self.assertIn("busy", r["why"])
        t.join(3)
        # and the lock is free again afterwards (the give-up path must not leak a hold)
        self.assertTrue(H._file_push_lock.acquire(blocking=False))
        H._file_push_lock.release()

    def test_board_ota_owns_the_flash_and_reboot_window_too(self):
        seen, old_wait, old_push = [], H.wait_reply, H.push_tcp
        H.push_tcp = lambda obj: None

        def fake_wait(t, rid, timeout):
            seen.append(lock_free_elsewhere(H._file_push_lock))
            return None
        H.wait_reply = fake_wait
        try:
            r = H.board_ota(str(self.src), rollback=True)
        finally:
            H.wait_reply, H.push_tcp = old_wait, old_push
        self.assertEqual(seen, [False], "a cover push could have started mid-OTA")
        self.assertFalse(r["ok"])

    def test_progress_is_reported_every_256_kb_and_never_per_chunk(self):
        """The settings page draws a bar for a 40 MB episode; a callback per
        1 KB chunk would be 40 000 status updates."""
        big = Path(self._tmp.name) / "big.bin"
        big.write_bytes(b"y" * (900 * 1024))
        seen, old_send, old_tcp, old_wait = [], H.tcp_send, H.push_tcp, H.wait_reply
        H.tcp_send = lambda msg: None
        H.push_tcp = lambda obj: None
        H.wait_reply = lambda t, rid, timeout: {"t": "fack", "id": rid, "ok": True}
        with H.lock:
            H.boards.append(object())
        try:
            r = H.push_file(str(big), "/agentpet/big.bin",
                            progress=lambda sent, total: seen.append((sent, total)))
        finally:
            with H.lock:
                H.boards.clear()
            H.tcp_send, H.push_tcp, H.wait_reply = old_send, old_tcp, old_wait
        self.assertTrue(r["ok"])
        self.assertEqual([t for _s, t in seen], [900 * 1024] * len(seen))
        self.assertEqual([s for s, _t in seen], [256 * 1024, 512 * 1024, 768 * 1024])

    def test_a_throwing_progress_callback_cannot_kill_the_push(self):
        big = Path(self._tmp.name) / "big.bin"
        big.write_bytes(b"y" * (600 * 1024))
        old_send, old_tcp, old_wait = H.tcp_send, H.push_tcp, H.wait_reply
        H.tcp_send = lambda msg: None
        H.push_tcp = lambda obj: None
        H.wait_reply = lambda t, rid, timeout: {"t": "fack", "id": rid, "ok": True}
        with H.lock:
            H.boards.append(object())
        try:
            def boom(sent, total):
                raise RuntimeError("the settings page went away")
            r = H.push_file(str(big), "/agentpet/big.bin", progress=boom)
        finally:
            with H.lock:
                H.boards.clear()
            H.tcp_send, H.push_tcp, H.wait_reply = old_send, old_tcp, old_wait
        self.assertTrue(r["ok"])
        self.assertEqual(r["bytes"], 600 * 1024)

    def test_nothing_holds_the_lock_between_pushes(self):
        H.push_file(str(self.src), "/agentpet/x")
        self.assertTrue(lock_free_elsewhere(H._file_push_lock))


class CardFontTests(unittest.TestCase):
    """No font data ships with the code: the host bakes the card faces from
    this Mac's system fonts and pushes whichever the card lacks (2026-10-01)."""
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = (H.FONT_TMP, H.board_ls, H.push_file)
        H.FONT_TMP = Path(self._tmp.name) / "afn.tmp"
        self.ls = {"t": "fls", "path": "/agentpet/fonts", "e": [], "n": 0}
        H.board_ls = lambda path: self.ls
        self.pushed = []

        def fake_push(src, dst, progress=None, wait=None):
            self.pushed.append(dst)
            if progress:
                progress(Path(src).stat().st_size, Path(src).stat().st_size)
            return {"ok": True, "bytes": Path(src).stat().st_size}
        H.push_file = fake_push
        self.baked = []
        with H.lock:
            H.boards.append(object())

    def tearDown(self):
        with H.lock:
            H.boards.clear()
        H.FONT_TMP, H.board_ls, H.push_file = self._old
        H.font_step("")
        self._tmp.cleanup()

    def fake_gen(self, cmd, **kw):
        """Stands in for gen_almanac_font.py --sd DIR --only a,b."""
        out, only = Path(cmd[cmd.index("--sd") + 1]), cmd[cmd.index("--only") + 1].split(",")
        self.baked.append(only)
        out.mkdir(parents=True, exist_ok=True)
        for f in only:
            (out / (f + ".afn")).write_bytes(b"AFN1" + b"\0" * 1000)
        return mock.Mock(returncode=0, stderr="")

    def card(self, *faces):
        self.ls["e"] = [[f + ".afn", 2000000, False] for f in faces]
        self.ls["n"] = len(faces)

    def test_a_card_with_every_face_needs_nothing(self):
        self.card(*H.FONT_FACES)
        with mock.patch.object(H.subprocess, "run", self.fake_gen):
            r = H.fonts_ensure()
        self.assertEqual((r["ok"], r["pushed"], self.baked), (True, [], []))
        self.assertEqual(H.font_rt["state"], "ok")

    def test_only_the_missing_faces_are_baked_and_pushed(self):
        self.card("head26", "quot24")
        with mock.patch.object(H.subprocess, "run", self.fake_gen):
            r = H.fonts_ensure()
        self.assertTrue(r["ok"])
        self.assertEqual(self.baked, [["body26", "tiny18"]])
        self.assertEqual(self.pushed, ["/agentpet/fonts/body26.afn", "/agentpet/fonts/tiny18.afn"])
        self.assertEqual((H.font_rt["state"], H.font_rt["n"]), ("ok", 4))
        self.assertFalse(H.FONT_TMP.exists(), "the baked copies are not kept around")

    def test_a_blank_card_with_no_fonts_dir_gets_all_four(self):
        self.ls = {"t": "fls", "path": "/agentpet/fonts", "e": [], "why": "not a dir"}
        with mock.patch.object(H.subprocess, "run", self.fake_gen):
            r = H.fonts_ensure()
        self.assertEqual(r["pushed"], list(H.FONT_FACES))

    def test_force_redoes_all_four_even_when_present(self):
        self.card(*H.FONT_FACES)
        with mock.patch.object(H.subprocess, "run", self.fake_gen):
            r = H.fonts_ensure(force=True)
        self.assertEqual(r["pushed"], list(H.FONT_FACES))

    def test_an_empty_file_counts_as_missing(self):
        self.card(*H.FONT_FACES)
        self.ls["e"][1][1] = 0
        with mock.patch.object(H.subprocess, "run", self.fake_gen):
            H.fonts_ensure()
        self.assertEqual(self.baked, [["body26"]])

    def test_no_card_and_no_wifi_change_nothing(self):
        self.ls = {"t": "fls", "e": [], "why": "no sd"}
        with mock.patch.object(H.subprocess, "run", self.fake_gen):
            H.fonts_ensure()
        self.assertEqual((H.font_rt["state"], self.baked), ("nocard", []))
        with H.lock:
            H.boards.clear()
        with mock.patch.object(H.subprocess, "run", self.fake_gen):
            r = H.fonts_ensure()
        self.assertEqual((H.font_rt["state"], r["ok"], self.pushed), ("wait", False, []))

    def test_missing_pillow_says_so(self):
        fail = mock.Mock(returncode=1, stderr="Traceback ...\nModuleNotFoundError: No module named 'PIL'\n")
        with mock.patch.object(H.subprocess, "run", return_value=fail):
            r = H.fonts_ensure()
        self.assertFalse(r["ok"])
        self.assertEqual((H.font_rt["state"], self.pushed), ("nopil", []))

    def test_a_failed_push_stops_and_says_why(self):
        H.push_file = lambda src, dst, progress=None, wait=None: {"ok": False, "why": "no answer to fend"}
        with mock.patch.object(H.subprocess, "run", self.fake_gen):
            r = H.fonts_ensure()
        self.assertEqual((r["ok"], r["pushed"]), (False, []))
        self.assertEqual((H.font_rt["state"], H.font_rt["why"]), ("error", "no answer to fend"))
        self.assertTrue(lock_free_elsewhere(H._file_push_lock))

    def test_one_run_at_a_time(self):
        with H._font_job:
            r = H.fonts_ensure()
        self.assertTrue(r["busy"])

    def test_the_generator_takes_only(self):
        src = (Path(__file__).parent / "gen_almanac_font.py").read_text()
        self.assertIn('"--only"', src)
        self.assertEqual(H.FONT_GEN.name, "gen_almanac_font.py")


class NowPlayingBoardMsgTests(unittest.TestCase):
    """The board->host side: hello answers, media commands, np_miss."""
    def setUp(self):
        self.saved = (dict(H.np_rt["payload"]), H.np_rt["msg"],
                      H.np_rt["sent_at"], H.NOW_PLAYING)
        H.NOW_PLAYING = True

    def tearDown(self):
        H.np_rt["payload"], H.np_rt["msg"], H.np_rt["sent_at"], H.NOW_PLAYING = self.saved

    def hello(self):
        replies = []
        H.handle_board_msg(json.dumps({"t": "hello", "dev": "amoled216", "v": 1}),
                           replies.append)
        return [json.loads(l) for l in b"".join(replies).decode().splitlines()]

    def test_hello_carries_the_current_entry(self):
        H.np_rt["payload"] = {"title": "T", "playing": True,
                              "bundleIdentifier": "com.apple.Music"}
        np = [m for m in self.hello() if m.get("t") == "np"]
        self.assertEqual(len(np), 1)
        self.assertEqual((np[0]["on"], np[0]["title"], np[0]["app"]), (1, "T", "music"))

    def test_hello_is_quiet_with_nothing_playing(self):
        H.np_rt["payload"] = {}
        self.assertEqual([m for m in self.hello() if m.get("t") == "np"], [])

    def test_hello_is_quiet_when_the_feature_is_off(self):
        H.NOW_PLAYING = False
        H.np_rt["payload"] = {"title": "T"}
        self.assertEqual([m for m in self.hello() if m.get("t") == "np"], [])

    def _routed(self, msg, name):
        got, done = [], threading.Event()
        old = getattr(H, name)

        def spy(arg):
            got.append(arg)
            done.set()
        setattr(H, name, spy)
        try:
            H.handle_board_msg(json.dumps(msg), lambda _b: None)
            self.assertTrue(done.wait(3), name + " never ran")
        finally:
            setattr(H, name, old)
        return got

    def test_media_message_runs_the_command_off_the_socket_thread(self):
        self.assertEqual(self._routed({"t": "media", "cmd": "next"}, "media_cmd"),
                         ["next"])

    def test_np_miss_message_reaches_the_cover_pusher(self):
        self.assertEqual(self._routed({"t": "np_miss", "cover": "/agentpet/covers/a.jpg"},
                                      "np_miss"), ["/agentpet/covers/a.jpg"])

    def test_a_tcp_hello_with_a_card_restarts_the_pending_audio_push(self):
        """A 40 MB episode does not survive a host restart, so every hello has
        to re-offer whatever is still only on this Mac."""
        got, done = [], threading.Event()
        old_pend, old_year = H.au_push_pending, H.almanac_push_year
        H.au_push_pending = lambda delay=0.0: (got.append(delay), done.set(), [])[2]
        H.almanac_push_year = lambda *a, **k: {"ok": True, "skipped": "test"}
        try:
            H.handle_board_msg(json.dumps({"t": "hello", "dev": "amoled216",
                                           "sd": 32000, "sdrw": True}), lambda _b: None)
            self.assertTrue(done.wait(3), "hello never reached au_push_pending")
        finally:
            H.au_push_pending, H.almanac_push_year = old_pend, old_year
        self.assertEqual(len(got), 1)

    def test_a_ble_hello_does_not_start_a_card_push(self):
        """fbeg/fdat is a TCP path; a BLE-only hello has no card link to use."""
        got, old_pend = [], H.au_push_pending
        H.au_push_pending = lambda delay=0.0: got.append(delay)
        try:
            H.handle_board_msg(json.dumps({"t": "hello", "dev": "amoled216",
                                           "link": "ble", "sd": 32000}), lambda _b: None)
            time.sleep(0.3)
        finally:
            H.au_push_pending = old_pend
        self.assertEqual(got, [])

    def test_plst_is_parked_for_the_waiting_endpoint(self):
        parked, old = [], H._park_reply
        H._park_reply = parked.append
        try:
            H.handle_board_msg(json.dumps({"t": "plst", "id": 7, "play": 1}), lambda _b: None)
        finally:
            H._park_reply = old
        self.assertEqual(parked, [{"t": "plst", "id": 7, "play": 1}])


class BleBridgeTests(unittest.TestCase):
    """the scan plan backs off when no board is around, and a
    CoreBluetooth session stuck at "turned off" gets the host relaunched --
    but only when macOS itself says Bluetooth is on."""
    def test_scan_filter_is_the_advertised_name_only(self):
        # both boards advertise NUS, so only the name tells them apart
        self.assertTrue(H.ble_is_our_board(H.BLE_NAME, None))
        self.assertTrue(H.ble_is_our_board(None, H.BLE_NAME))       # no scan response: cached name
        other = "OtherPet" if H.BLE_NAME == "AgentPet" else "AgentPet"
        self.assertFalse(H.ble_is_our_board(other, other))
        self.assertFalse(H.ble_is_our_board(None, None))
        # a fresh scan-response name beats a stale CoreBluetooth cache
        self.assertFalse(H.ble_is_our_board(other, H.BLE_NAME))
        self.assertTrue(H.ble_is_our_board(H.BLE_NAME, other))

    def test_scan_is_fast_while_a_board_was_seen_recently(self):
        self.assertEqual(H.ble_scan_plan(0), H.BLE_SCAN_FAST)
        self.assertEqual(H.ble_scan_plan(H.BLE_IDLE_BACKOFF_S - 1), H.BLE_SCAN_FAST)

    def test_scan_backs_off_after_a_minute_without_a_board(self):
        self.assertEqual(H.ble_scan_plan(H.BLE_IDLE_BACKOFF_S), H.BLE_SCAN_SLOW)
        self.assertEqual(H.ble_scan_plan(3600), H.BLE_SCAN_SLOW)
        scan, pause = H.BLE_SCAN_SLOW
        self.assertLessEqual(scan + pause, 30.5)      # a board powering on waits <= 30 s

    def test_a_few_off_errors_are_normal(self):
        for n in range(H.BLE_OFF_RESTART_N):
            self.assertEqual(H.ble_off_verdict(n, True, 3600), "ok", n)

    def test_stuck_off_restarts_only_when_macos_says_on(self):
        n = H.BLE_OFF_RESTART_N
        self.assertEqual(H.ble_off_verdict(n, True, 3600), "restart")
        self.assertEqual(H.ble_off_verdict(n, None, 3600), "restart")   # unreadable = assume on
        self.assertEqual(H.ble_off_verdict(n, False, 3600), "wait")
        self.assertEqual(H.ble_off_verdict(n * 10, False, 3600), "wait")

    def test_a_fresh_process_is_given_time_to_settle(self):
        n = H.BLE_OFF_RESTART_N
        self.assertEqual(H.ble_off_verdict(n, True, H.BLE_OFF_MIN_UPTIME_S - 1), "ok")
        self.assertEqual(H.ble_off_verdict(n, True, H.BLE_OFF_MIN_UPTIME_S), "restart")


class FocusFollowOffSeatTests(unittest.TestCase):
    """2026-09-14 22:30 (user): landing on the seat of an app that has quit must
    not launch it -- `open -a` would. The check happens when the debounce
    timer fires, against the host's own state table."""
    def test_only_off_is_held_back(self):
        self.assertFalse(H.focus_should_raise("off"))
        for st in ("idle", "working", "needs_you", "done"):
            self.assertTrue(H.focus_should_raise(st), st)

    def test_board_select_on_a_quit_app_does_not_open_it(self):
        calls = []
        saved = (H.subprocess.run, H.post_key, H.FOCUS_SETTLE_S, H.FOCUS_FOLLOW, dict(H.state))
        H.subprocess.run = lambda cmd, **kw: calls.append(cmd) or type("R", (), {"returncode": 0, "stderr": b""})()
        H.post_key = lambda spec: None
        H.FOCUS_SETTLE_S, H.FOCUS_FOLLOW = 0.01, True
        try:
            with H.lock:
                H.state["qoder"] = "off"
            H.focus_agent("qoder")
            time.sleep(0.15)
            self.assertEqual(calls, [])                      # quit app stays quit
            with H.lock:
                H.state["qoder"] = "idle"
            H.focus_agent("qoder")
            time.sleep(0.15)
            self.assertEqual(calls, [["open", "-a", H.FOCUS_APPS["qoder"]]])
        finally:
            H.subprocess.run, H.post_key, H.FOCUS_SETTLE_S, H.FOCUS_FOLLOW = saved[:4]
            with H.lock:
                H.state.clear(); H.state.update(saved[4])

    def test_an_empty_focus_app_never_reaches_open_dash_a(self):
        # config.json may map a seat to "" (this Mac has no app for it).
        # `open -a ""` opens a Finder window; the seat key is still there, so
        # the old `agent not in FOCUS_APPS` test let it through.
        saved_app, saved_timer = H.FOCUS_APPS["forest"], H._focus_timer
        H.FOCUS_APPS["forest"] = ""
        H._focus_timer = None
        try:
            H.focus_agent("forest")
            self.assertIsNone(H._focus_timer)               # no timer armed at all
        finally:
            H.FOCUS_APPS["forest"] = saved_app
            H._focus_timer = saved_timer
        self.assertIsNone(H.front_agent("Finder|Finder"))   # "" matches nothing either


class ProductTests(unittest.TestCase):
    """the two products deploy to the same ~/.agentpet/ and launchd
    label, so /state must say which host is running (`pet status` / `pet use`
    read it). This repo's host serves the S3 pet only."""
    def test_state_says_which_product_this_host_is(self):
        snap = H.state_snapshot()
        self.assertEqual(snap["product"], "s3")
        self.assertEqual(snap["host_version"], H.HOST_VERSION)
        self.assertEqual(set(snap["agents"]), set(H.AGENTS))   # the rest of /state is still there
        json.dumps(snap)                                        # and it still serialises


class BleLinkReasonTests(unittest.TestCase):
    """The settings page's 板子 row says WHY the link is down, so "Mac 蓝牙已关"
    and "蓝牙栈卡住" stop looking like the same grey dot."""

    def setUp(self):
        self._saved = (H.ble_reason, H._ble_client)
        H.ble_reason, H._ble_client = "", None

    def tearDown(self):
        H.ble_reason, H._ble_client = self._saved

    def test_the_reason_is_set_cleared_and_never_invented(self):
        self.assertEqual(H.ble_note("bt_off"), "bt_off")
        self.assertEqual(H.ble_reason, "bt_off")
        self.assertEqual(H.ble_note("stuck"), "stuck")
        self.assertEqual(H.ble_note("bluetooth is sad"), "stuck")   # not a value the page can spell
        self.assertEqual(H.ble_note(None), "stuck")
        self.assertEqual(H.ble_note(""), "")                        # a link came up
        self.assertEqual(H.ble_reason, "")

    def test_board_snapshot_carries_it_and_a_live_link_clears_it(self):
        H.ble_note("bt_off")
        self.assertEqual(H.board_snapshot()["link_reason"], "bt_off")
        H._ble_client = object()            # linked: whatever the thread last wrote is stale
        self.assertEqual(H.board_snapshot()["link_reason"], "")
        H._ble_client = None
        self.assertEqual(H.board_snapshot()["link_reason"], "bt_off")

    def test_a_tcp_link_clears_it_too(self):
        """S3's main link is Wi-Fi TCP: a board that never touched BLE is not
        'scanning', it is connected."""
        H.ble_note("scanning")
        H.boards.append(object())
        try:
            self.assertEqual(H.board_snapshot()["link_reason"], "")
        finally:
            H.boards.pop()
        self.assertEqual(H.board_snapshot()["link_reason"], "scanning")

    def test_the_ble_thread_still_writes_at_every_point(self):
        """The write points live inside an async loop no unit test can drive,
        so guard them at the source: drop one and the row goes quietly stale."""
        src = inspect.getsource(H.ble_thread)
        for call in ('ble_note("scanning")', 'ble_note("")',
                     'ble_note("bt_off")', 'ble_note("stuck")'):
            self.assertIn(call, src, call)
        # and the page knows how to say each of them
        for reason in H.BLE_REASONS:
            if reason:
                for lang in H.LANGS:
                    self.assertIn("rsn_" + reason, H.SETTINGS_T[lang], (lang, reason))


class ObjCPoolTests(unittest.TestCase):
    """2026-09-15: the host grew ~5 MB / 10 min forever. Every thread here is a
    plain Python thread with no NSRunLoop, so nothing drained its autorelease
    pool: iterating CGWindowListCopyWindowInfo's array leaked ~7.2 KB per call
    and watcher_loop makes that call once a second. objc_pool() around each
    call is the fix; these tests keep every ObjC-touching path inside one."""
    def test_pool_is_a_context_manager_that_does_not_swallow(self):
        with H.objc_pool():                  # enters and drains without raising
            pass
        with self.assertRaises(ValueError):
            with H.objc_pool():
                raise ValueError("must propagate")

    def test_pool_is_a_no_op_without_pyobjc(self):
        saved = H._objc
        H._objc = None                       # CI / Linux: no PyObjC to pool with
        try:
            self.assertIsInstance(H.objc_pool(), H._NullPool)
            with H.objc_pool():
                pass
            with self.assertRaises(ValueError):
                with H.objc_pool():
                    raise ValueError("must propagate")
        finally:
            H._objc = saved

    def test_every_synchronous_objc_call_site_is_pooled(self):
        src = (HERE / "agentpet_host.py").read_text()
        pooled = funcs_calling_name(src, "objc_pool")
        for method, why in [("CGWindowListCopyWindowInfo", "1 Hz on the watcher thread"),
                            ("CGEventPost", "inject worker thread")]:
            callers = funcs_that_call(src, method)
            self.assertTrue(callers, method + " moved or was renamed")
            self.assertLessEqual(callers, pooled,
                                 f"{method} ({why}) must run inside objc_pool()")

    def test_the_ble_loop_pools_what_core_bluetooth_hands_it(self):
        """2026-09-17: the scan branch leaked ~57 KB per 3 s scan (0.6 MB /
        10 min in the log, 76 -> 108 MB over the 09-16 night). bleak's
        delegate runs on its own dispatch queue and does nothing but
        call_soon_threadsafe(did_discover_peripheral, ...), so the bridging
        of every advertisement happens on OUR unpooled thread. The fix pools
        each such callback -- one synchronous unit, never across an await."""
        src = (HERE / "agentpet_host.py").read_text()
        self.assertIn("ble_thread", funcs_calling_name(src, "pool_loop_callbacks"),
                      "the BLE thread's loop must pool CoreBluetooth's callbacks")
        # _PooledCall.__call__ is the only __call__ in the host, and the pool
        # has to be inside it (one per callback), not around the install.
        self.assertIn("__call__", funcs_calling_name(src, "objc_pool"))

    def test_pool_loop_callbacks_wraps_each_callback_once(self):
        import asyncio
        seen = []

        class FakePool:
            def __enter__(self):
                seen.append("in")

            def __exit__(self, *a):
                seen.append("out")
                return False

        class FakeObjC:
            @staticmethod
            def autorelease_pool():
                return FakePool()

        got = []
        saved = H._objc
        H._objc = FakeObjC
        loop = asyncio.new_event_loop()
        try:
            self.assertIs(H.pool_loop_callbacks(loop), loop)
            loop.call_soon_threadsafe(lambda *a: (seen.append("cb"), got.append(a)), 1, 2)
            loop.call_later(0.3, loop.stop)     # not threadsafe, so not pooled
            loop.run_forever()
        finally:
            loop.close()
            H._objc = saved
        self.assertEqual(seen, ["in", "cb", "out"])   # drained before the next callback
        self.assertEqual(got, [(1, 2)])               # arguments pass through untouched

    def test_pool_loop_callbacks_is_a_no_op_without_pyobjc(self):
        import asyncio
        saved = H._objc
        H._objc = None                       # CI / Linux: nothing to drain
        loop = asyncio.new_event_loop()
        try:
            H.pool_loop_callbacks(loop)
            self.assertNotIn("call_soon_threadsafe", loop.__dict__)
        finally:
            loop.close()
            H._objc = saved

    def test_the_scan_branch_keeps_one_scanner(self):
        """find_device_by_filter builds a CentralManagerDelegate, a
        CBCentralManager and a dispatch queue per scan and throws them away;
        that teardown autoreleases inside bleak's own coroutines, where a pool
        of ours cannot go. One scanner, started and stopped, does not."""
        src = (HERE / "agentpet_host.py").read_text()
        self.assertNotIn("ble_thread", funcs_that_call(src, "find_device_by_filter"))
        self.assertIn("ble_thread", funcs_calling_name(src, "BleakScanner"))

    def test_no_pool_is_held_across_an_await(self):
        """Pools pop LIFO per thread. On the BLE thread's event loop a pool
        around an `await` lets another coroutine open and close its own pool
        inside that window, and popping the outer one then drains an inner
        pool that is still live -- over-release, not just a missed drain. Any
        pooling of the BLE thread has to recycle a pool between iterations
        instead, so no objc_pool() block may contain an await."""
        src = (HERE / "agentpet_host.py").read_text()
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.With):
                continue
            if not any(isinstance(i.context_expr, ast.Call)
                       and isinstance(i.context_expr.func, ast.Name)
                       and i.context_expr.func.id == "objc_pool" for i in node.items):
                continue
            waits = [n for n in ast.walk(node)
                     if isinstance(n, (ast.Await, ast.AsyncWith, ast.AsyncFor))]
            self.assertFalse(waits,
                             f"objc_pool() at line {node.lineno} holds a pool across an await")


class ScanWindowTests(unittest.TestCase):
    """ble_scan_window bounds every call into the kept scanner. On 2026-09-17
    the Mac slept mid-cycle (clamshell, 17:35), the reused central manager
    silently ignored the next scan request, and bleak's start() waited five
    hours for a did-start-scanning event while the board advertised at
    -32 dBm. A fresh scanner re-checks power; a kept one must be given up on."""

    class Scanner:
        def __init__(self, start_hangs=False, stop_hangs=False, finds=None):
            self.start_hangs, self.stop_hangs, self.finds = start_hangs, stop_hangs, finds
            self.started = self.stopped = 0
            self.win = None

        async def start(self):
            import asyncio
            self.started += 1
            if self.start_hangs:
                await asyncio.Event().wait()          # never
            if self.finds is not None:
                self.win["dev"] = self.finds
                self.win["hit"].set()

        async def stop(self):
            import asyncio
            self.stopped += 1
            if self.stop_hangs:
                await asyncio.Event().wait()

    def run_window(self, sc, timeout=0.5, op_timeout=0.05):
        import asyncio
        win = {}
        sc.win = win
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(H.ble_scan_window(sc, win, timeout, op_timeout)), win
        finally:
            loop.close()

    def test_a_found_board_comes_back_and_the_scanner_is_stopped(self):
        sc = self.Scanner(finds="the-board")
        dev, win = self.run_window(sc)
        self.assertEqual(dev, "the-board")
        self.assertEqual((sc.started, sc.stopped), (1, 1))
        self.assertIsNone(win["hit"])                 # no stray set() between windows
        self.assertNotIn("dev", win)

    def test_an_empty_window_times_out_quietly(self):
        sc = self.Scanner()
        t0 = time.time()
        dev, _ = self.run_window(sc, timeout=0.1)
        self.assertIsNone(dev)
        self.assertLess(time.time() - t0, 1.0)
        self.assertEqual((sc.started, sc.stopped), (1, 1))

    def test_a_scanner_that_never_starts_is_given_up_on(self):
        import asyncio
        sc = self.Scanner(start_hangs=True)
        t0 = time.time()
        with self.assertRaises(asyncio.TimeoutError):
            self.run_window(sc, op_timeout=0.05)
        self.assertLess(time.time() - t0, 1.0)         # not five hours
        self.assertEqual(sc.stopped, 1)                # still told to stop on the way out

    def test_a_scanner_that_never_stops_is_given_up_on(self):
        import asyncio
        sc = self.Scanner(stop_hangs=True)
        t0 = time.time()
        with self.assertRaises(asyncio.TimeoutError):
            self.run_window(sc, timeout=0.05, op_timeout=0.05)
        self.assertLess(time.time() - t0, 1.0)

    def test_the_default_bound_is_the_module_constant_and_is_short(self):
        self.assertGreater(H.BLE_SCAN_OP_TIMEOUT_S, 2)      # a real start() is milliseconds
        self.assertLess(H.BLE_SCAN_OP_TIMEOUT_S, 60)        # a wedge must not eat the 30 s cadence
        src = inspect.getsource(H.ble_scan_window)
        self.assertIn("asyncio.wait_for(sc.start()", src)
        self.assertIn("asyncio.wait_for(sc.stop()", src)

    def test_the_ble_loop_scans_only_through_the_bounded_window(self):
        src = inspect.getsource(H.ble_thread)
        self.assertIn("ble_scan_window(", src)
        self.assertNotIn("sc.start()", src)
        self.assertNotIn("scanner.start()", src)
        self.assertIn("scanner did not answer", src)   # a timeout is logged as what it is


class TwoBoardsTests(unittest.TestCase):
    """with two boards in range the scan used to stop at the
    first AgentPet advertisement. Someone else's board heard first put this
    host on standby next to its own (or a free) board. Now the window keeps
    the best: ours at once, else the best seen within BLE_PICK_GRACE_S."""
    MINE, OTHER, STRANGER = 0x1111, 0x2222, 0x3333

    def rank(self, digest, claim=False, declined=False):
        return H.ble_pick_rank(digest, self.MINE, {self.OTHER: "Bob's Mac"}, claim, declined)

    def test_rank_order(self):
        r = self.rank
        self.assertEqual(r(self.MINE), 0)
        self.assertEqual(r(0), 1)                   # nobody's
        self.assertEqual(r(None), 1)                # firmware before the claim system
        self.assertEqual(r(self.STRANGER), 2)       # someone else's, name unknown: probe
        self.assertEqual(r(self.OTHER), 3)          # someone else's we know: standby
        self.assertEqual(r(0, declined=True), 3)    # free, but its screen just said no
        self.assertEqual(r(self.OTHER, claim=True), 1)
        self.assertEqual(r(self.MINE, claim=True), 0)

    class Scanner:
        """start() replays advertisements (seconds after start, name, digest)."""
        def __init__(self, test, ads):
            self.test, self.ads, self.win = test, ads, None

        async def start(self):
            import asyncio
            loop = asyncio.get_running_loop()
            for at, name, digest in self.ads:
                loop.call_later(at, lambda n=name, d=digest: H.ble_offer(
                    self.win, n, d, self.test.rank(d), loop, grace=0.2))

        async def stop(self):
            pass

    def pick(self, ads, timeout=1.0):
        import asyncio
        win = {}
        sc = self.Scanner(self, ads)
        sc.win = win
        loop = asyncio.new_event_loop()
        try:
            t0 = time.time()
            dev = loop.run_until_complete(H.ble_scan_window(sc, win, timeout, 0.5))
            return dev, win.get("digest"), time.time() - t0, win
        finally:
            loop.close()

    def test_ours_wins_over_someone_elses_heard_first(self):
        dev, digest, _, _ = self.pick([(0.0, "bob", self.OTHER), (0.05, "ours", self.MINE)])
        self.assertEqual((dev, digest), ("ours", self.MINE))

    def test_ours_wins_over_a_free_board_heard_first(self):
        dev, _, _, _ = self.pick([(0.0, "free", 0), (0.05, "ours", self.MINE)])
        self.assertEqual(dev, "ours")

    def test_ours_ends_the_window_at_once(self):
        dev, _, took, _ = self.pick([(0.0, "ours", self.MINE), (0.05, "free", 0)])
        self.assertEqual(dev, "ours")
        self.assertLess(took, 0.15)

    def test_a_free_board_beats_someone_elses(self):
        dev, digest, _, _ = self.pick([(0.0, "bob", self.OTHER), (0.1, "free", 0)])
        self.assertEqual((dev, digest), ("free", 0))

    def test_a_lone_free_board_waits_only_the_grace(self):
        dev, _, took, _ = self.pick([(0.0, "free", 0)], timeout=3.0)
        self.assertEqual(dev, "free")
        self.assertLess(took, 1.0)                  # the 0.2 s grace, not the 3 s window

    def test_someone_elses_is_used_when_nothing_better(self):
        dev, digest, _, _ = self.pick([(0.0, "bob", self.OTHER)])
        self.assertEqual((dev, digest), ("bob", self.OTHER))

    def test_a_worse_board_after_the_pick_does_not_replace_it(self):
        dev, _, _, _ = self.pick([(0.0, "free", 0), (0.05, "bob", self.OTHER)])
        self.assertEqual(dev, "free")

    def test_window_leaves_no_state_behind(self):
        _, _, _, win = self.pick([(0.0, "free", 0)])
        self.assertIsNone(win["hit"])
        for k in ("dev", "rank", "grace"):
            self.assertNotIn(k, win)

    def test_the_scan_callback_goes_through_the_pick(self):
        src = inspect.getsource(H.ble_thread)
        self.assertIn("ble_pick_rank(", src)
        self.assertIn("ble_offer(", src)
        self.assertNotIn('win["hit"].set()', src)


class RxWatchdogClockTests(unittest.TestCase):
    """The rx watchdog must count awake seconds. 2026-09-18: with the wall
    clock, every DarkWake after a clamshell sleep judged a healthy link dead
    (\"35 s without a ping\" that the sleeping Mac simply had not read), bleak
    skipped the cancel, the board kept the link and stopped advertising, and
    the host scanned for two hours -- three times in one night."""

    def test_the_awake_clock_stops_while_the_mac_sleeps(self):
        self.assertIs(H.ble_awake_clock, time.monotonic)
        if sys.platform == "darwin":
            # mach_absolute_time does not advance during sleep; a Python that
            # switched monotonic to a continuous clock would bring the bug back
            self.assertEqual(time.get_clock_info("monotonic").implementation,
                             "mach_absolute_time()")

    def test_the_watchdog_reads_only_that_clock(self):
        src = inspect.getsource(H.ble_thread)
        self.assertNotIn("time.time() - last_rx", src)
        self.assertNotIn("last_rx = time.time()", src)
        self.assertEqual(src.count("ble_awake_clock()"), 4)   # set, refresh, compare, re-arm in the dark


class StaleLinkTests(unittest.TestCase):
    """After a watchdog drop the board usually still holds the link (the Mac's
    cancel never reached the controller), so it never re-advertises and no
    scan can find it. A few normal, empty windows after such a drop = restart
    now, not after the two-hour guard (3/3 instant reconnects
    on 2026-09-18)."""

    def test_only_a_watchdog_drop_arms_it(self):
        v = H.stale_link_verdict
        self.assertFalse(v(False, 100))                # a disconnect event, an error: never
        self.assertFalse(v(True, H.BLE_STALE_LINK_RESCANS - 1))
        self.assertTrue(v(True, H.BLE_STALE_LINK_RESCANS))
        self.assertTrue(v(True, H.BLE_STALE_LINK_RESCANS + 5))
        self.assertFalse(v(True, 100, limit=0))       # 0 = switched off

    def test_the_limit_is_a_few_minutes_not_two_hours(self):
        # fast cadence 12 s/window for the first minute, then 30 s: 8 windows ~ 3 min
        self.assertGreaterEqual(H.BLE_STALE_LINK_RESCANS, 4)
        self.assertLessEqual(H.BLE_STALE_LINK_RESCANS, 20)

    def test_the_loop_arms_on_the_drop_and_disarms_on_a_link(self):
        src = inspect.getsource(H.ble_thread)
        i = src.index('log("ble: rx watchdog timeout, dropping link")')
        self.assertIn("dropped_by_watchdog, empty_scans = True, 0", src[i:i + 200])
        self.assertIn("stale_link_verdict(dropped_by_watchdog, empty_scans)", src)
        self.assertIn("board vanished mid-link", src)
        # a found board clears both, before the link is even made
        j = src.index("dropped_by_watchdog, empty_scans = False, 0")
        self.assertLess(j, src.index("await ble_link_open(client)"))

    def test_s3_restarts_even_with_a_tcp_board_and_says_so(self):
        # A zombie BLE link would swallow the board's dictation stream (mic_link
        # defaults to ble) while TCP looks fine; TCP reconnects ~1 s after the
        # relaunch. The line names the TCP count so a log reader can tell.
        src = inspect.getsource(H.ble_thread)
        i = src.index("board vanished mid-link")
        self.assertIn("len(boards)", src[i:i + 200])
        self.assertNotIn("bool(boards)", src[i - 300:i])   # no TCP veto in front of it


class DisplayGateTests(unittest.TestCase):
    """The rx watchdog judges nothing while the display is asleep. The
    first night on the awake clock (2026-09-19): DarkWakes were long enough to
    count 35 awake seconds and no notify ever arrives after a sleep entry, so
    the link was dropped every 3.5 min -- seventeen restarts in an hour."""

    def test_the_verdict(self):
        v = H.rx_watchdog_verdict
        self.assertEqual(v(0, False), "ok")
        self.assertEqual(v(H.BLE_RX_TIMEOUT, False), "ok")        # not yet
        self.assertEqual(v(H.BLE_RX_TIMEOUT + 1, False), "drop")
        self.assertEqual(v(H.BLE_RX_TIMEOUT + 1, True), "dark")   # lights out: never a drop
        self.assertEqual(v(0, True), "dark")
        self.assertEqual(v(3, False, limit=2), "drop")

    def test_a_beating_tcp_link_shortens_the_ble_wait(self):
        """2026-09-27: lid opened, TCP back in 4 s, the zombie BLE link kept
        for the full 35 awake seconds. The board pings each link every 10 s,
        so TCP beating + BLE silent for 1.5 beats = the BLE link is dead."""
        v = H.rx_watchdog_verdict
        self.assertLess(H.BLE_RX_TIMEOUT_TCP, H.BLE_RX_TIMEOUT)
        self.assertGreater(H.BLE_RX_TIMEOUT_TCP, 10)                  # > one ping interval
        self.assertEqual(v(H.BLE_RX_TIMEOUT_TCP, False, tcp_fresh=True), "ok")
        self.assertEqual(v(H.BLE_RX_TIMEOUT_TCP + 1, False, tcp_fresh=True), "drop")
        self.assertEqual(v(H.BLE_RX_TIMEOUT_TCP + 1, False), "ok")   # no TCP witness: full wait
        self.assertEqual(v(99, True, tcp_fresh=True), "dark")         # DarkWake: still never a drop
        src = inspect.getsource(H.ble_thread)
        i = src.index("rx_watchdog_verdict(ble_awake_clock() - last_rx")
        self.assertIn("tcp_fresh=time.time() - _tcp_rx_at < TCP_FRESH_S", src[i:i + 250])
        h = inspect.getsource(H.BoardHandler.handle)
        self.assertIn("_tcp_rx_at = time.time()", h)
        self.assertIn("global _tcp_rx_at", h)

    def test_the_display_probe_answers_a_bool_quickly_and_never_raises(self):
        t0 = time.monotonic()
        for _ in range(200):
            a = H.mac_display_asleep()
        self.assertIsInstance(a, bool)
        self.assertLess(time.monotonic() - t0, 0.5)          # ~7 us a call on the real Mac
        if sys.platform == "darwin":
            self.assertIsNotNone(H._coregraphics)            # dlopen happened once
        saved = H._coregraphics
        try:
            H._coregraphics = object()                        # anything without the symbols
            self.assertIs(H.mac_display_asleep(), False)     # unknown = awake, watchdog stays armed
        finally:
            H._coregraphics = saved

    def test_the_loop_re_arms_in_the_dark_and_drops_only_when_lit(self):
        src = inspect.getsource(H.ble_thread)
        i = src.index("rx_watchdog_verdict(ble_awake_clock() - last_rx")
        self.assertIn("mac_display_asleep()", src[i:i + 200])
        self.assertIn('if verdict == "dark":', src)
        j = src.index('if verdict == "dark":')
        self.assertIn("last_rx = ble_awake_clock()", src[j:j + 200])
        k = src.index('elif verdict == "drop":')
        self.assertIn('log("ble: rx watchdog timeout, dropping link")', src[k:k + 200])


class MacSleepTests(unittest.TestCase):
    """No board link rides into a Mac sleep (2026-09-27): the
    second Mac kept the BLE link asleep and the board's 10 s heartbeat woke
    it 20-54 times per 10 min. WillSleep closes both links; they stay down
    through DarkWakes and come back once the display is on."""

    def setUp(self):
        import socket
        self.socket = socket
        self.saved = (dict(H._power), H._ble_loop, list(H._power_kick), list(H.boards),
                      H.log, H._ble_last_link_at, H.POWER_SLEEP_WAIT_S)
        self.lines = []
        H.log = lambda *a: self.lines.append(" ".join(str(x) for x in a))
        H._power.update(sleeping=False, woke=False, at=0.0, refused=0)
        H._ble_loop, H._power_kick[:] = None, []
        H._ble_parked.clear()
        self.dark = False
        self.clock = 1000.0
        for name, fn in (("mac_display_asleep", lambda: self.dark),
                         ("ble_awake_clock", lambda: self.clock)):
            p = mock.patch.object(H, name, fn)
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        (power, H._ble_loop, kick, boards, H.log, H._ble_last_link_at,
         H.POWER_SLEEP_WAIT_S) = self.saved
        H._power.clear(); H._power.update(power)
        H._power_kick[:] = kick
        H.boards[:] = boards
        H._ble_parked.clear()

    def test_the_verdict(self):
        v = H.links_paused_verdict
        self.assertTrue(v(False, True, 0))                       # asleep
        self.assertTrue(v(True, True, 0))                        # DarkWake: HasPoweredOn, display dark
        self.assertFalse(v(True, False, 0))                      # back, display on
        self.assertTrue(v(False, False, 5))                      # Apple menu Sleep: lit until it goes
        self.assertFalse(v(False, False, H.POWER_MISSED_WAKE_S))  # lit a minute, no notice: we missed it
        self.assertTrue(v(False, True, 10 * H.POWER_MISSED_WAKE_S))   # ... but never while dark

    def test_will_sleep_closes_tcp_and_links_stay_down_through_a_dark_wake(self):
        a, b = self.socket.socketpair()
        self.addCleanup(a.close); self.addCleanup(b.close)
        H.boards[:] = [a]
        self.dark = True
        H.power_will_sleep()
        b.settimeout(1)
        self.assertEqual(b.recv(10), b"")                        # the board's side sees the FIN
        self.assertTrue(H.links_paused())
        self.assertIn("power: Mac going to sleep, closed 1 TCP", self.lines[-1])
        H.power_woke()                                           # DarkWake
        self.clock += 600
        self.assertTrue(H.links_paused())                        # display still dark
        H._ble_last_link_at = 0.0
        self.dark = False                                        # lid open
        self.assertFalse(H.links_paused())
        self.assertFalse(H._power["sleeping"])
        self.assertGreater(H._ble_last_link_at, time.time() - 5)  # the no-board guard starts over
        self.assertIn("power: Mac awake, board links resume", self.lines[-1])
        self.assertNotIn("no wake notice", self.lines[-1])
        n = len(self.lines)
        self.assertFalse(H.links_paused())                       # said once
        self.assertEqual(len(self.lines), n)

    def test_a_lost_wake_notice_cannot_keep_the_board_away(self):
        H.power_will_sleep()
        self.assertTrue(H.links_paused())                        # lit, but the sleep is under way
        self.clock += H.POWER_MISSED_WAKE_S
        self.assertFalse(H.links_paused())
        self.assertIn("no wake notice came", self.lines[-1])

    def test_will_sleep_waits_for_the_ble_loop_and_no_longer(self):
        class Loop:
            def call_soon_threadsafe(self, fn):
                fn()
        H._ble_loop = Loop()
        H._power_kick[:] = [lambda: threading.Timer(0.05, H._ble_parked.set).start()]
        t0 = time.monotonic()
        H.power_will_sleep()
        self.assertLess(time.monotonic() - t0, 1)
        self.assertNotIn("still busy", self.lines[-1])
        H.POWER_SLEEP_WAIT_S = 0.1
        H._power_kick[:] = [lambda: None]                        # a loop stuck in a connect
        H.power_will_sleep()
        self.assertIn("BLE loop still busy, sleeping anyway", self.lines[-1])

    def test_board_connects_are_turned_away_while_paused(self):
        a, b = self.socket.socketpair()
        self.addCleanup(a.close); self.addCleanup(b.close)
        self.dark = True
        H.power_will_sleep()
        H.boards[:] = []
        for _ in range(3):
            H.BoardHandler(a, ("10.0.0.107", 5000), None)
        self.assertEqual(H.boards, [])
        self.assertEqual(H._power["refused"], 3)
        self.assertEqual(sum("turned away" in l for l in self.lines), 1)   # one line per sleep
        self.dark = False
        H.power_woke()
        self.assertFalse(H.links_paused())
        self.assertIn("turned away 3 board connects", self.lines[-1])

    def test_the_ble_loop_sits_out_the_sleep(self):
        src = inspect.getsource(H.ble_thread)
        gate = src.index("if links_paused():")
        self.assertLess(gate, src.index("scan_s, pause_s = ble_scan_plan("))
        self.assertIn("_ble_parked.set()", src[gate:gate + 400])
        self.assertIn("slept, scanner = True, None", src[gate:gate + 400])   # fresh manager after a sleep
        # back awake: fast scan plan, and our own drop is not a stale link
        w = src.index("if slept:")
        self.assertIn("last_link = time.time()", src[w:w + 300])
        self.assertIn("dropped_by_watchdog, empty_scans = False, 0", src[w:w + 300])
        # the kick ends a scan window, a pause and a held link
        k = src.index("def kick():")
        self.assertIn('(win["hit"], link["gone"], _claim["evt"])', src[k:k + 500])
        self.assertIn("_power_kick[:] = [kick]", src)
        # a window cut short by the kick goes to the gate, whatever it found
        s = src.index("dev = await ble_scan_window(")
        self.assertLess(src.index('if _power["sleeping"]:', s), src.index("if dev is None:", s))
        self.assertIn('link["gone"] = gone', src)

    def test_the_no_board_guard_sleeps_with_the_mac(self):
        src = inspect.getsource(H.mem_loop)
        i = src.index("no_board_restart_check(")
        self.assertIn('_power["sleeping"]', src[i:i + 250])

    def test_the_host_listens_for_sleep(self):
        src = inspect.getsource(H.main)
        self.assertIn("target=power_thread", src)
        p = inspect.getsource(H.power_thread)
        self.assertIn('_power["cb"] = cb', p)                    # the callback is held
        self.assertIn("(_IOKIT_WILL_SLEEP, _IOKIT_CAN_SLEEP)", p)  # both acknowledged
        self.assertIn("finally:", p)
        self.assertEqual((H._IOKIT_CAN_SLEEP, H._IOKIT_WILL_SLEEP, H._IOKIT_HAS_POWERED_ON),
                         (0xE0000270, 0xE0000280, 0xE0000300))   # IOMessage.h


class LinkTimeoutTests(unittest.TestCase):
    """Every await into bleak's connect/disconnect is bounded.
    2026-09-20: a connect that timed out inside bleak went on to await the
    cancel's acknowledgement with no bound, and a manager the Mac slept on
    never sent it -- 7 h 57 min with a healthy board advertising next to it."""

    class Client:
        def __init__(self, connect_hangs=False, disconnect_hangs=False):
            self.connect_hangs, self.disconnect_hangs = connect_hangs, disconnect_hangs
            self.connected = self.disconnected = 0

        async def connect(self):
            import asyncio
            if self.connect_hangs:
                await asyncio.Event().wait()          # never
            self.connected += 1

        async def disconnect(self):
            import asyncio
            if self.disconnect_hangs:
                await asyncio.Event().wait()
            self.disconnected += 1

    def go(self, coro):
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_a_connect_that_answers_goes_through(self):
        c = self.Client()
        self.go(H.ble_link_open(c, timeout=1))
        self.assertEqual(c.connected, 1)

    def test_a_connect_that_never_answers_raises_its_own_timeout(self):
        import asyncio
        c = self.Client(connect_hangs=True)
        t0 = time.monotonic()
        with self.assertRaises(H.BleConnectTimeout):
            self.go(H.ble_link_open(c, timeout=0.2))
        self.assertLess(time.monotonic() - t0, 2)
        self.assertTrue(issubclass(H.BleConnectTimeout, asyncio.TimeoutError))

    def test_a_disconnect_that_never_answers_is_abandoned_with_one_line(self):
        c = self.Client(disconnect_hangs=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.go(H.ble_link_close(c, timeout=0.2))     # returns, does not raise
        self.assertIn("disconnect did not answer", out.getvalue())
        c2 = self.Client()
        self.go(H.ble_link_close(c2, timeout=1))
        self.assertEqual(c2.disconnected, 1)

    def test_the_bounds_are_seconds_not_forever(self):
        self.assertGreaterEqual(H.BLE_CONNECT_TIMEOUT_S, 10)      # bleak's own connect bound is 10 s
        self.assertLessEqual(H.BLE_CONNECT_TIMEOUT_S, 120)
        self.assertGreaterEqual(H.BLE_DISCONNECT_TIMEOUT_S, 2)
        self.assertLessEqual(H.BLE_DISCONNECT_TIMEOUT_S, 60)

    def test_the_loop_only_links_through_the_bounded_helpers(self):
        src = inspect.getsource(H.ble_thread)
        self.assertNotIn("async with BleakClient", src)         # __aenter__/__aexit__ are unbounded
        self.assertIn("await ble_link_open(client)", src)
        self.assertIn("await ble_link_close(client)", src)
        self.assertIn("connect did not answer", src)
        # the close sits in a finally so a watchdog drop or an error still tears down
        i = src.index("await ble_link_close(client)")
        self.assertIn("finally:", src[i - 60:i])
        # and the connect-timeout branch is tested before the generic TimeoutError one
        self.assertLess(src.index("isinstance(e, BleConnectTimeout)"),
                        src.index("isinstance(e, asyncio.TimeoutError)"))
        # S3: the outer finally still drops the vitals (board_link_down) after the close
        self.assertLess(i, src.index("board_link_down()"))


class MemLoopClockTests(unittest.TestCase):
    """mem_loop measures its period on the wall clock in short ticks, since a
    single long timed wait does not count time the Mac spends asleep
    (2026-09-19 night: no sample in eight hours; the guard fired
    seven minutes after the lid opened)."""

    def test_it_waits_the_period_then_samples(self):
        saved = (H._ble_client, H.host_exit)
        H._ble_client = object()          # a linked board keeps the guard quiet
        H.host_exit = lambda code=0: None
        out, stop = io.StringIO(), threading.Event()
        try:
            with contextlib.redirect_stdout(out):
                th = threading.Thread(target=H.mem_loop,
                                      kwargs={"period_s": 0.3, "stop": stop, "tick_s": 0.01},
                                      daemon=True)
                th.start()
                time.sleep(0.15)
                self.assertNotIn("mem:", out.getvalue())     # ticks alone do not sample
                time.sleep(0.4)
            self.assertIn("mem:", out.getvalue())
        finally:
            stop.set()
            th.join(2)
            H._ble_client, H.host_exit = saved

    def test_every_tick_feeds_the_trend_ring_and_the_slope_needs_five_minutes(self):
        """the settings page's memory line is one point per tick
        (never a log line), starting with a point at loop start."""
        saved = (H._ble_client, H.host_exit, list(H.mem_hist))
        H._ble_client = object()
        H.host_exit = lambda code=0: None
        H.mem_hist.clear()
        out, stop = io.StringIO(), threading.Event()
        try:
            with contextlib.redirect_stdout(out):
                th = threading.Thread(target=H.mem_loop,
                                      kwargs={"period_s": 60, "stop": stop, "tick_s": 0.01},
                                      daemon=True)
                th.start()
                # poll, not a fixed 0.2 s: a busy CI runner managed 4 ticks in it
                deadline = time.time() + 2
                while len(H.mem_hist) < 5 and time.time() < deadline:
                    time.sleep(0.02)
            self.assertNotIn("mem:", out.getvalue())
            self.assertGreaterEqual(len(H.mem_hist), 5)
            t, kb, th_n = H.mem_hist[0]
            self.assertGreater(kb, 1000)
            self.assertGreaterEqual(th_n, 1)
            body = H.mem_hist_body()
            self.assertEqual(body["now_mb"], round(H.mem_hist[-1][1] / 1024, 1))
            self.assertIsNone(body["slope_mb_h"])            # a seconds-long span is not a trend
            # the start-up climb is drawn but never sloped: +12 MB in the first
            # two minutes, then a flat 33 min, must read as a flat line
            H.mem_hist.clear()
            saved_t0 = H.HOST_T0
            H.HOST_T0 = time.time() - 2400
            try:
                for i in range(4):                                   # 0..90 s: the climb
                    H.mem_hist_add(H.HOST_T0 + i * 30, (72 + i * 4) * 1024, 9)
                for i in range(4, 70):                               # 120 s..: flat at 84 MB, 33 min
                    H.mem_hist_add(H.HOST_T0 + i * 30, 84 * 1024, 10)
                body = H.mem_hist_body()
                self.assertEqual(body["slope_mb_h"], 0.0)
                self.assertEqual(body["start_mb"], 72.0)
                self.assertEqual(body["now_mb"], 84.0)
                self.assertEqual(body["delta_mb"], 0.0)          # since warm-up, not since start
                self.assertEqual(body["span_s"], 65 * 30)
            finally:
                H.HOST_T0 = saved_t0
            self.assertLessEqual(len(body["pts"]), 301)
        finally:
            stop.set()
            th.join(2)
            H._ble_client, H.host_exit = saved[0], saved[1]
            H.mem_hist.clear(); H.mem_hist.extend(saved[2])

    def test_slope_is_mb_per_hour_over_the_last_hour_only(self):
        now = 1_000_000.0
        flat = [(now - 3000 + i * 30, 90 * 1024, 10) for i in range(100)]
        self.assertEqual(H.mem_slope_mb_h(flat, now), 0.0)
        leak = [(now - 3000 + i * 30, int((90 + i * 30 / 3600 * 6) * 1024), 10) for i in range(100)]
        self.assertAlmostEqual(H.mem_slope_mb_h(leak, now), 6.0, places=1)   # +6 MB/h ramp
        old = [(now - 7200, 50 * 1024, 10), (now - 7000, 200 * 1024, 10)]    # outside the window
        self.assertIsNone(H.mem_slope_mb_h(old + [(now, 90 * 1024, 10)], now))
        # seven minutes with one 1.2 MB step is not +15 MB/h: no slope under half an hour
        step = [(now - 420 + i * 30, (85 if i < 12 else 86) * 1024 + 200, 10) for i in range(15)]
        self.assertIsNone(H.mem_slope_mb_h(step, now))
        self.assertEqual(H.MEM_SLOPE_MIN_S, 1800)

    def test_the_tick_is_short_and_the_period_is_compared_on_the_wall_clock(self):
        self.assertLessEqual(H.MEM_LOOP_TICK_S, 60)
        src = inspect.getsource(H.mem_loop)
        self.assertIn("time.time() < due", src)
        self.assertIn("stop.wait(tick)", src)
        self.assertNotIn("stop.wait(period_s)", src)


class NoBoardGuardPlacementTests(unittest.TestCase):
    """The no-board self-restart must not depend on the loop it guards. Before
    2026-09-17 evening it was called from the scan branch, so a scan loop
    wedged inside the scanner took the guard down with it: five hours idle,
    two-hour limit, no restart. Now mem_loop runs it every sample."""

    def test_the_guard_is_called_from_mem_loop_and_not_from_the_ble_loop(self):
        src = (HERE / "agentpet_host.py").read_text()
        callers = funcs_calling_name(src, "no_board_restart_check")
        self.assertIn("mem_loop", callers)
        self.assertNotIn("ble_thread", callers)

    def test_mem_loop_trips_the_guard_while_the_ble_loop_is_dead(self):
        saved = (H._ble_last_link_at, H._ble_client, H.host_exit,
                 H.NO_BOARD_RESTART_S, H.RSS_RESTART_MB)
        gone = []

        def exit_stub(code=0):
            gone.append(code)
            raise SystemExit          # ends the test thread, as _exit would end the process

        H._ble_last_link_at = time.time() - 7300
        H._ble_client = None          # the BLE loop never links again -- and never runs the guard
        H.host_exit = exit_stub
        H.NO_BOARD_RESTART_S, H.RSS_RESTART_MB = 7200, 200
        out, stop = io.StringIO(), threading.Event()
        try:
            with contextlib.redirect_stdout(out):
                th = threading.Thread(target=H.mem_loop, kwargs={"period_s": 0.01, "stop": stop},
                                      daemon=True)
                th.start()
                th.join(5)
            self.assertEqual(gone, [3])
            self.assertIn("self-restart: no board for 121 min", out.getvalue())
        finally:
            stop.set()
            th.join(2)
            (H._ble_last_link_at, H._ble_client, H.host_exit,
             H.NO_BOARD_RESTART_S, H.RSS_RESTART_MB) = saved

    def test_a_tcp_board_counts_as_linked_in_mem_loop(self):
        """S3's main link is TCP; the guard must not restart a host whose
        board is right there on Wi-Fi, and a TCP drop resets its idle clock."""
        src = inspect.getsource(H.mem_loop)
        self.assertIn("bool(boards)", src)
        self.assertIn("max(_ble_last_link_at, _tcp_last_link_at)", src)

    def test_a_linked_board_still_vetoes_it_from_mem_loop(self):
        saved = (H._ble_last_link_at, H._ble_client, H.host_exit,
                 H.NO_BOARD_RESTART_S, H.RSS_RESTART_MB)
        gone = []
        H._ble_last_link_at = time.time() - 99999
        H._ble_client = object()      # linked
        H.host_exit = lambda code=0: gone.append(code)
        H.NO_BOARD_RESTART_S, H.RSS_RESTART_MB = 7200, 200
        stop = threading.Event()
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                th = threading.Thread(target=H.mem_loop, kwargs={"period_s": 0.01, "stop": stop},
                                      daemon=True)
                th.start()
                time.sleep(0.15)      # several samples
            self.assertEqual(gone, [])
        finally:
            stop.set()
            th.join(2)
            (H._ble_last_link_at, H._ble_client, H.host_exit,
             H.NO_BOARD_RESTART_S, H.RSS_RESTART_MB) = saved


class MemRssTests(unittest.TestCase):
    """mem_rss_kb reads the kernel, never forks (2026-09-17: a `ps` fork in the
    PyObjC host parked mem_loop forever, twice)."""

    def test_reads_a_plausible_resident_size_without_spawning(self):
        with mock.patch.object(H.subprocess, "run",
                               side_effect=AssertionError("mem_rss_kb must not spawn")):
            kb = H.mem_rss_kb()
        self.assertGreater(kb, 4 * 1024)          # a Python process is never under 4 MB
        self.assertLess(kb, 4 * 1024 * 1024)      # nor over 4 GB

    def test_a_dead_pid_reads_as_zero(self):
        self.assertEqual(H.mem_rss_kb(2 ** 30), 0)

    def test_source_has_no_ps_left_in_mem_rss(self):
        src = inspect.getsource(H.mem_rss_kb)
        self.assertNotIn('"ps"', src)
        self.assertIn("proc_pidinfo", src)


class NoBoardRestartTests(unittest.TestCase):
    """The two last-resort restarts (2026-09-17). After two nights of the host
    sitting in the scan branch and growing 76 -> 108 MB, it hands itself to
    launchd after NO_BOARD_RESTART_S with no board at all, or once the `mem:`
    sample passes RSS_RESTART_MB. Both are vetoed by a linked board -- a
    restart then would drop a session the user is in the middle of."""

    def setUp(self):
        self.saved = (H.NO_BOARD_RESTART_S, H.RSS_RESTART_MB, H.mem_last_rss_kb,
                      H.host_exit, H.CONFIG_OVERRIDE)
        self.td = tempfile.TemporaryDirectory()
        H.CONFIG_OVERRIDE = Path(self.td.name) / "config.json"
        H.NO_BOARD_RESTART_S, H.RSS_RESTART_MB, H.mem_last_rss_kb = 7200, 200, 0
        self.gone = []
        H.host_exit = lambda code=0: self.gone.append(code)   # never really _exit

    def tearDown(self):
        (H.NO_BOARD_RESTART_S, H.RSS_RESTART_MB, H.mem_last_rss_kb,
         H.host_exit, H.CONFIG_OVERRIDE) = self.saved
        self.td.cleanup()

    def check(self, idle_s, linked=False, rss_mb=0):
        H.mem_last_rss_kb = int(rss_mb * 1024)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            why = H.no_board_restart_check(idle_s, linked)
        return why, out.getvalue()

    def test_the_verdict_reads_both_limits(self):
        v = H.no_board_restart_verdict
        self.assertEqual(v(7200, 0, False), "idle")
        self.assertEqual(v(7199, 0, False), "")
        self.assertEqual(v(0, 200 * 1024, False), "rss")
        self.assertEqual(v(0, 199 * 1024, False), "")
        self.assertEqual(v(0, 0, False), "")

    def test_two_hours_with_no_board_hands_the_process_over(self):
        why, out = self.check(7260, rss_mb=108)
        self.assertEqual(why, "idle")
        self.assertIn("self-restart: no board for 121 min, rss 108 MB", out)
        self.assertEqual(self.gone, [3])                  # host_exit(3), launchd's cue

    def test_a_runaway_rss_hands_it_over_too(self):
        why, out = self.check(60, rss_mb=214)
        self.assertEqual(why, "rss")
        self.assertIn("self-restart: no board for 1 min, rss 214 MB", out)
        self.assertEqual(self.gone, [3])

    def test_a_linked_board_vetoes_both(self):
        why, out = self.check(99999, linked=True, rss_mb=999)
        self.assertEqual(why, "")
        self.assertEqual(out, "")
        self.assertEqual(self.gone, [])

    def test_under_both_limits_nothing_happens(self):
        why, out = self.check(7199, rss_mb=199)
        self.assertEqual(why, "")
        self.assertEqual(out, "")
        self.assertEqual(self.gone, [])

    def test_a_limit_of_zero_turns_that_guard_off(self):
        H.NO_BOARD_RESTART_S = 0
        self.assertEqual(self.check(99999, rss_mb=10)[0], "")
        H.NO_BOARD_RESTART_S, H.RSS_RESTART_MB = 7200, 0
        self.assertEqual(self.check(60, rss_mb=9999)[0], "")
        self.assertEqual(self.gone, [])

    def test_config_json_moves_both_limits(self):
        H.CONFIG_OVERRIDE.write_text(json.dumps({"no_board_restart_s": 600,
                                                 "rss_restart_mb": 120}))
        with contextlib.redirect_stdout(io.StringIO()):
            H.load_overrides()
        self.assertEqual((H.NO_BOARD_RESTART_S, H.RSS_RESTART_MB), (600, 120))
        self.assertEqual(self.check(601, rss_mb=10)[0], "idle")
        self.assertEqual(self.check(60, rss_mb=121)[0], "rss")

    def test_a_limit_that_is_not_a_number_keeps_the_default(self):
        H.CONFIG_OVERRIDE.write_text(json.dumps({"no_board_restart_s": "tonight",
                                                 "rss_restart_mb": None}))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            H.load_overrides()
        self.assertEqual((H.NO_BOARD_RESTART_S, H.RSS_RESTART_MB), (7200, 200))
        self.assertIn("no_board_restart_s not a number", out.getvalue())
        self.assertIn("rss_restart_mb not a number", out.getvalue())

    def test_the_number_it_quotes_is_the_one_the_log_printed(self):
        """The guard reads mem_loop's last sample rather than shelling out to
        ps of its own, so `self-restart:` can never disagree with `mem:`."""
        self.assertIn("mem_last_rss_kb = rss", inspect.getsource(H.mem_loop))


class MemDebugTests(unittest.TestCase):
    """GET /debug/mem + the tracemalloc flag: what tells a native leak (traced
    flat, rss climbing) from a Python container that never sheds."""
    def tearDown(self):
        if tracemalloc.is_tracing():
            tracemalloc.stop()
        H._tm_base = None

    def test_snapshot_shape(self):
        snap = H.mem_snapshot(5)
        self.assertEqual(set(snap), {"rss_kb", "uptime_s", "threads", "gc_count",
                                     "gc_objects", "types_top", "tracemalloc",
                                     "traced_mb", "traced_top"})
        self.assertGreater(snap["rss_kb"], 0)
        self.assertIn("MainThread", snap["threads"])
        self.assertEqual(snap["threads"], sorted(snap["threads"]))
        self.assertEqual(len(snap["gc_count"]), 3)
        self.assertGreater(snap["gc_objects"], 0)
        self.assertLessEqual(len(snap["types_top"]), 5)

    def test_types_top_ranks_by_count(self):
        objs = [1, 2, 3] + ["a", "b"] + [(), (), (), ()]
        self.assertEqual(H.mem_types_top(2, objects=objs),
                         [{"type": "tuple", "n": 4}, {"type": "int", "n": 3}])

    def test_tracemalloc_is_off_without_the_flag(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(H.mem_debug_start(Path(d) / "memdebug"))
        self.assertFalse(tracemalloc.is_tracing())
        self.assertIsNone(H.mem_traced_mb())
        self.assertEqual(H.mem_traced_top(), [])
        self.assertFalse(H.mem_snapshot(3)["tracemalloc"])

    def test_tracemalloc_starts_behind_the_flag(self):
        with tempfile.TemporaryDirectory() as d:
            flag = Path(d) / "memdebug"
            flag.write_text("")
            self.assertTrue(H.mem_debug_start(flag))
        self.assertTrue(tracemalloc.is_tracing())
        self.assertIsInstance(H.mem_traced_mb(), float)
        keep = [dict(i=i) for i in range(5000)]          # something to show up as growth
        top = H.mem_traced_top(10)
        self.assertTrue(top)
        self.assertLessEqual(len(top), 10)
        self.assertEqual(set(top[0]), {"where", "kb", "count"})
        self.assertEqual(len(keep), 5000)                # keep it alive past the snapshot

    def test_the_default_flag_lives_under_agentpet(self):
        self.assertEqual(H.MEMDEBUG_FLAG, Path.home() / ".agentpet" / "memdebug")

    def test_rss_of_a_dead_pid_is_zero(self):
        self.assertEqual(H.mem_rss_kb(999999), 0)


class DebugMemEndpointTests(unittest.TestCase):
    """The endpoint on a real server (port 0, never the live host's 8788)."""
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H.HookHandler)
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.th.join(5)

    def get(self, path):
        with urlopen(self.base + path, timeout=20) as r:
            self.assertEqual(r.status, 200)
            return json.loads(r.read())

    def test_debug_mem_answers_json(self):
        body = self.get("/debug/mem")
        self.assertGreater(body["rss_kb"], 0)
        self.assertIn("MainThread", body["threads"])
        self.assertFalse(body["tracemalloc"])           # production default

    def test_top_is_clamped_and_bad_input_falls_back(self):
        self.assertLessEqual(len(self.get("/debug/mem?top=3")["types_top"]), 3)
        self.assertLessEqual(len(self.get("/debug/mem?top=nonsense")["types_top"]), 20)
        self.assertLessEqual(len(self.get("/debug/mem?top=99999")["types_top"]), 200)

    def test_state_still_answers(self):
        self.assertEqual(self.get("/state")["host_version"], H.HOST_VERSION)


class KeySpecTests(unittest.TestCase):
    """key_spec_ok() is the settings page's one check on a seat key: would
    post_key() really press this?"""

    def test_specs_post_key_presses(self):
        for spec in ("enter", "esc", "cmd+enter", "ctrl+shift+k", "cmd+z",
                     "alt+tab", "option+space", "CMD+Enter"):
            self.assertTrue(H.key_spec_ok(spec), spec)

    def test_specs_post_key_would_drop(self):
        for spec in ("cmd++", "foo", " ", "", "cmd + enter", "+enter", "enter+",
                     "ctrl+f13", "cmd+shift+", "en ter", "enter ", None, 7, ["enter"]):
            self.assertFalse(H.key_spec_ok(spec), repr(spec))

    def test_a_typo_in_the_modifier_is_a_refusal(self):
        # post_key() ignores a modifier it does not know, which is exactly the
        # mistake the page has to tell the user about
        self.assertTrue(H.key_spec_ok("cmd+k"))
        self.assertFalse(H.key_spec_ok("cmdd+k"))
        self.assertFalse(H.key_spec_ok("command+k"))

    def test_every_key_the_table_knows_is_accepted(self):
        for k in H.KEYCODES:
            self.assertTrue(H.key_spec_ok(k), k)
            self.assertTrue(H.key_spec_ok("cmd+shift+" + k), k)


class SettingsSaveTests(unittest.TestCase):
    """/settings/save merges into config.json AND the module globals follow,
    because nothing restarts the host after a save."""

    # the last three came from this board's own settings
    SCALARS = ("FOCUS_FOLLOW", "FOLLOW_FRONT", "FOCUS_SETTLE_S", "NOW_PLAYING",
               "MIC_LINK", "VOICE_SOURCE", "MIC_SINK_DEVICE", "MIC_TAIL_S",
               "VOICE_STYLE", "STRETCH_AFTER_MIN", "PICKUP_PAGE", "SHOW_OFF_SEATS",
               "LANG")
    MAPS = ("FOCUS_APPS", "FOCUS_KEYS", "APPROVE_KEYS", "REJECT_KEYS",
            "PROC_RULES", "CPU_WORKING")

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self._cfg = H.CONFIG_OVERRIDE
        self._scalars = {n: getattr(H, n) for n in self.SCALARS}
        self._maps = {n: dict(getattr(H, n)) for n in self.MAPS}
        self._sent = []
        self._push, self._np, self._prime = H.push_msg, H.np_apply, H.prime_voices
        H.CONFIG_OVERRIDE = Path(self.td.name) / "config.json"
        H.push_msg = self._sent.append          # no BLE, no media-control child
        H.np_apply = lambda: None
        H.prime_voices = lambda: None           # S3 only: no edge-tts synthesis

    def tearDown(self):
        H.CONFIG_OVERRIDE = self._cfg
        H.push_msg, H.np_apply, H.prime_voices = self._push, self._np, self._prime
        for n, v in self._scalars.items():
            setattr(H, n, v)
        for n, v in self._maps.items():
            getattr(H, n).clear()
            getattr(H, n).update(v)
        self.td.cleanup()

    def cfg(self):
        return json.loads(H.CONFIG_OVERRIDE.read_text())

    def test_the_five_seat_maps_round_trip(self):
        r = H.save_settings({
            "focus_apps": {"claude": "iTerm", "forest": ""},
            "focus_keys": {"claude": "cmd+l", "codex": ""},
            "approve_keys": {"claude": "cmd+enter"},
            "reject_keys": {"claude": "ctrl+shift+k"},
            "proc_rules": {"claude": {"match": "my-claude", "exclude": None}},
            "cpu_working": {"claude": 7, "codex": 8.5},
            "voice_source": "board",
            "mic_sink_device": "BlackHole 16ch",
            "mic_tail_s": 1.25,
        })
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["keys"], ["approve_keys", "cpu_working", "focus_apps",
                                     "focus_keys", "mic_sink_device", "mic_tail_s",
                                     "proc_rules", "reject_keys", "voice_source"])
        saved = self.cfg()
        self.assertEqual(saved["focus_apps"], {"claude": "iTerm", "forest": ""})
        self.assertEqual(saved["focus_keys"], {"claude": "cmd+l", "codex": ""})
        self.assertEqual(saved["approve_keys"], {"claude": "cmd+enter"})
        self.assertEqual(saved["reject_keys"], {"claude": "ctrl+shift+k"})
        self.assertEqual(saved["proc_rules"], {"claude": {"match": "my-claude", "exclude": None}})
        self.assertEqual(saved["cpu_working"], {"claude": 7.0, "codex": 8.5})
        self.assertEqual(saved["voice_source"], "board")
        self.assertEqual(saved["mic_sink_device"], "BlackHole 16ch")
        self.assertEqual(saved["mic_tail_s"], 1.25)
        # ... and the running host is already using them
        self.assertEqual(H.FOCUS_APPS["claude"], "iTerm")
        self.assertEqual(H.FOCUS_APPS["forest"], "")
        self.assertEqual(H.FOCUS_KEYS["claude"], "cmd+l")
        self.assertEqual(H.APPROVE_KEYS["claude"], "cmd+enter")
        self.assertEqual(H.REJECT_KEYS["claude"], "ctrl+shift+k")
        self.assertEqual(H.PROC_RULES["claude"]["match"], "my-claude")
        self.assertEqual(H.CPU_WORKING["claude"], 7.0)
        self.assertEqual(H.VOICE_SOURCE, "board")
        self.assertEqual(H.MIC_SINK_DEVICE, "BlackHole 16ch")
        self.assertEqual(H.MIC_TAIL_S, 1.25)
        self.assertEqual(self._sent[-1]["mic"], 1)          # cfg pushed to the board

    def test_the_s3_only_keys_still_save(self):
        """voice_style / stretch_after_min / pickup_page:
        merging the new settings page must not drop them."""
        r = H.save_settings({"voice_style": "tts", "stretch_after_min": 45,
                             "pickup_page": "clock"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["keys"], ["pickup_page", "stretch_after_min", "voice_style"])
        self.assertEqual(self.cfg()["voice_style"], "tts")
        self.assertEqual(H.VOICE_STYLE, "tts")
        self.assertEqual(H.STRETCH_AFTER_MIN, 45)
        self.assertEqual(H.PICKUP_PAGE, "clock")
        self.assertEqual(self._sent[-1]["pickup"], "clock")   # cfg carries it to the board
        # an unset radio sends "" and must not pin the board to anything
        r = H.save_settings({"pickup_page": ""})
        self.assertNotIn("pickup_page", r["keys"])

    def test_show_off_seats_round_trips_and_rides_the_cfg(self):
        """The 「显示离线席位」 checkbox (2026-09-19): config.json key, live
        global, and the int the board reads -- in one save."""
        self.assertFalse(H.SHOW_OFF_SEATS)                   # default: hidden
        self.assertEqual(self._sent, [])
        r = H.save_settings({"show_off_seats": True})
        self.assertEqual(r, {"ok": True, "keys": ["show_off_seats"]})
        self.assertIs(self.cfg()["show_off_seats"], True)
        self.assertTrue(H.SHOW_OFF_SEATS)
        self.assertEqual(self._sent[-1]["show_off"], 1)
        r = H.save_settings({"show_off_seats": False})
        self.assertEqual(r["keys"], ["show_off_seats"])
        self.assertFalse(H.SHOW_OFF_SEATS)
        self.assertEqual(self._sent[-1]["show_off"], 0)
        # not a bool = not from the page; nothing written, nothing changed
        r = H.save_settings({"show_off_seats": "yes"})
        self.assertNotIn("show_off_seats", r["keys"])
        self.assertFalse(H.SHOW_OFF_SEATS)

    def test_an_empty_focus_app_never_reaches_open_dash_a(self):
        H.save_settings({"focus_apps": {"forest": ""}})
        raised = []
        saved = H._focus_timer
        H._focus_timer = None
        try:
            H.focus_agent("forest")
            self.assertIsNone(H._focus_timer)               # no timer armed at all
        finally:
            H._focus_timer = saved
        self.assertEqual(raised, [])
        self.assertIsNone(H.front_agent("Finder|Finder"))   # "" matches nothing either

    def test_one_bad_key_spec_writes_nothing(self):
        r = H.save_settings({"focus_follow": False,
                             "approve_keys": {"claude": "cmd++"}})
        self.assertFalse(r["ok"])
        self.assertEqual(r["why"], "按键写法不对：cmd++")
        self.assertFalse(H.CONFIG_OVERRIDE.exists())        # not even the good half
        self.assertTrue(H.FOCUS_FOLLOW)

    def test_proc_rules_are_shape_checked_and_compiled(self):
        for bad, why in (({"nope": {"match": "x"}}, "没有这个席位：nope"),
                         ({"claude": {"exclude": "x"}}, "claude 缺 match"),
                         ({"claude": {"match": "x", "exclude": 7}},
                          "claude 的 exclude 要是正则或 null")):
            r = H.save_settings({"proc_rules": bad})
            self.assertFalse(r["ok"], bad)
            self.assertEqual(r["why"], why)
        r = H.save_settings({"proc_rules": {"claude": {"match": "(unclosed"}}})
        self.assertFalse(r["ok"])
        self.assertTrue(r["why"].startswith("claude 的正则不对："), r["why"])
        self.assertFalse(H.CONFIG_OVERRIDE.exists())

    def test_cpu_working_takes_numbers_in_range(self):
        for bad, why in (({"claude": "8"}, "claude 的 CPU 阈值不是数字"),
                         ({"claude": True}, "claude 的 CPU 阈值不是数字"),
                         ({"claude": 140}, "claude 的 CPU 阈值要在 0-100 之间"),
                         ({"nope": 8}, "没有这个席位：nope")):
            r = H.save_settings({"cpu_working": bad})
            self.assertFalse(r["ok"], bad)
            self.assertEqual(r["why"], why)

    def test_hand_edited_keys_survive_a_save(self):
        H.CONFIG_OVERRIDE.write_text(json.dumps({"memdebug_note": "keep me",
                                                 "focus_apps": {"codex": "ChatGPT"}}))
        r = H.save_settings({"focus_apps": {"claude": "iTerm"}})
        self.assertTrue(r["ok"])
        saved = self.cfg()
        self.assertEqual(saved["memdebug_note"], "keep me")
        # per-seat maps merge (2026-09-26, /agent-setup patches one seat at a time)
        self.assertEqual(saved["focus_apps"], {"codex": "ChatGPT", "claude": "iTerm"})

    def test_unreadable_config_is_not_overwritten(self):
        H.CONFIG_OVERRIDE.write_text("{not json")
        r = H.save_settings({"focus_follow": False})
        self.assertFalse(r["ok"])
        self.assertEqual(H.CONFIG_OVERRIDE.read_text(), "{not json")


class LogTailTests(unittest.TestCase):
    """/log/tail reads one fixed path and never more than 500 lines."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self._saved = H.LOG_PATH
        H.LOG_PATH = Path(self.td.name) / "agentpet_host.log"
        H.LOG_PATH.write_text("".join("line %d\n" % i for i in range(1, 801)))

    def tearDown(self):
        H.LOG_PATH = self._saved
        self.td.cleanup()

    def test_the_deployed_path_is_the_one_pet_writes(self):
        self.assertEqual(self._saved, Path("/tmp/agentpet_host.log"))

    def test_the_tail_is_the_newest_lines_in_order(self):
        self.assertEqual(H.log_tail(3), ["line 798", "line 799", "line 800"])

    def test_n_is_clamped_and_junk_falls_back_to_sixty(self):
        self.assertEqual(len(H.log_tail(9999)), 500)
        self.assertEqual(len(H.log_tail(0)), 1)
        self.assertEqual(len(H.log_tail("nonsense")), 60)
        self.assertEqual(len(H.log_tail(None)), 60)
        self.assertEqual(len(H.log_tail("12")), 12)

    def test_a_missing_log_is_an_empty_list(self):
        H.LOG_PATH.unlink()
        self.assertEqual(H.log_tail(10), [])
        H.LOG_PATH = Path(self.td.name)                    # a directory, not a file
        self.assertEqual(H.log_tail(10), [])

    def test_a_log_longer_than_the_read_window_keeps_whole_lines(self):
        H.LOG_PATH.write_text("".join("%06d %s\n" % (i, "x" * 200) for i in range(4000)))
        tail = H.log_tail(5)
        self.assertEqual(len(tail), 5)
        for line in tail:
            self.assertEqual(len(line), 207)               # no half a line at the top


class HostRestartTests(unittest.TestCase):
    def test_it_answers_first_and_leaves_after(self):
        gone = threading.Event()
        saved = H.host_exit
        H.host_exit = lambda code=0: gone.set()            # never really _exit in a test
        try:
            r = H.host_restart()
            self.assertEqual(r["ok"], True)
            self.assertEqual(r["version"], H.HOST_VERSION)
            self.assertFalse(gone.is_set())                # the answer goes out first
            self.assertTrue(gone.wait(5), "host_exit was never called")
        finally:
            H.host_exit = saved

    def test_host_exit_is_the_one_that_calls_os_underscore_exit(self):
        self.assertIn("os._exit(code)", inspect.getsource(H.host_exit))


class SettingsEndpointTests(unittest.TestCase):
    """The new endpoints on a real server (port 0, never the live host's 8788).
    Nothing here may touch ~/.agentpet or /tmp/agentpet_host.log."""

    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), H.HookHandler)
        cls.th = threading.Thread(target=cls.srv.serve_forever, daemon=True)
        cls.th.start()
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.th.join(5)

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        d = Path(self.td.name)
        self._saved = (H.CONFIG_OVERRIDE, H.ALMANAC_OVERRIDE, H.LOG_PATH,
                       H.MIC_SINK_BIN, H._frontmost_app, H.host_exit,
                       H.push_msg, H.np_apply, H.prime_voices)
        H.CONFIG_OVERRIDE = d / "config.json"
        H.ALMANAC_OVERRIDE = d / "almanac.json"
        H.LOG_PATH = d / "agentpet_host.log"
        H.MIC_SINK_BIN = d / "mic_sink"
        H.push_msg = lambda *a: None
        H.np_apply = lambda: None
        H.prime_voices = lambda: None

    def tearDown(self):
        (H.CONFIG_OVERRIDE, H.ALMANAC_OVERRIDE, H.LOG_PATH, H.MIC_SINK_BIN,
         H._frontmost_app, H.host_exit, H.push_msg, H.np_apply,
         H.prime_voices) = self._saved
        self.td.cleanup()

    def get(self, path):
        with urlopen(self.base + path, timeout=20) as r:
            self.assertEqual(r.status, 200)
            return json.loads(r.read())

    def post(self, path, body):
        req = Request(self.base + path, data=json.dumps(body).encode(),
                      headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(req, timeout=20) as r:
            self.assertEqual(r.status, 200)
            return json.loads(r.read())

    def test_settings_data_carries_host_board_and_agents(self):
        d = self.get("/settings/data")
        self.assertEqual(d["host"]["version"], H.HOST_VERSION)
        self.assertEqual(d["host"]["product"], H.PRODUCT)
        self.assertEqual(d["host"]["pid"], os.getpid())
        self.assertGreaterEqual(d["host"]["up_min"], 0)
        self.assertGreater(d["host"]["rss_mb"], 0)
        self.assertEqual(set(d["board"]) >= {"age_s", "stale", "up", "heap", "min",
                                             "big", "rst", "hello_age_s",
                                             "link_reason"}, True)
        # S3's 板子 row also needs the link type: Wi-Fi TCP first, BLE second
        self.assertIsInstance(d["boards"], int)
        self.assertIsInstance(d["ble"], bool)
        self.assertIn("build", d["fw"])
        self.assertEqual(sorted(d["agents"]), sorted(H.AGENTS))
        self.assertEqual(d["seats"], list(H.AGENTS))
        self.assertIn(d["active"], H.AGENTS)
        self.assertFalse(d["mic_sink_ok"])                 # stubbed at a temp path

    def test_settings_data_offers_every_key_the_page_writes_back(self):
        d = self.get("/settings/data")
        for k in ("focus_apps", "focus_keys", "approve_keys", "reject_keys",
                  "proc_rules", "cpu_working", "voice_source", "mic_link",
                  "mic_sink_device", "mic_tail_s", "focus_follow", "follow_front",
                  "focus_settle_s", "np_enabled", "show_off_seats",
                  # this board's own keys
                  "voice_style", "stretch_after_min", "pickup_page"):
            self.assertIn(k, d, k)
        self.assertNotIn("pair", d)
        for k in ("focus_keys", "approve_keys", "reject_keys"):
            self.assertEqual(sorted(d[k]), sorted(H.AGENTS), k)

    def test_the_page_really_has_the_off_seats_checkbox_wired(self):
        """A key in /settings/data that no input fills is a dead key: check the
        box, its fill and its save line all exist (2026-09-19)."""
        for frag in ('<input type="checkbox" id="so">',
                     "$('#so').checked=!!d.show_off_seats",
                     "show_off_seats:$('#so').checked"):
            self.assertIn(frag, H.SETTINGS_HTML, frag)

    def test_settings_save_answers_with_what_it_wrote(self):
        saved = {n: dict(getattr(H, n)) for n in ("FOCUS_APPS", "FOCUS_KEYS")}
        try:
            r = self.post("/settings/save", {"focus_apps": {"claude": "iTerm"}})
            self.assertEqual(r, {"ok": True, "keys": ["focus_apps"]})
            self.assertEqual(json.loads(H.CONFIG_OVERRIDE.read_text())["focus_apps"],
                             {"claude": "iTerm"})
            bad = self.post("/settings/save", {"focus_keys": {"claude": "nope"}})
            self.assertEqual(bad, {"ok": False, "why": "按键写法不对：nope"})
        finally:
            for n, v in saved.items():
                getattr(H, n).clear()
                getattr(H, n).update(v)

    def test_front_answers_a_name_the_button_can_paste(self):
        H._frontmost_app = lambda: "Warp|Warp Terminal"
        d = self.get("/front")
        self.assertEqual(d["front"], "Warp|Warp Terminal")
        self.assertEqual(d["names"], ["Warp", "Warp Terminal"])
        self.assertIsInstance(d["name"], str)
        self.assertEqual(d["name"], "Warp")
        H._frontmost_app = lambda: ""                      # window server said nothing
        self.assertEqual(self.get("/front"), {"front": "", "names": [], "name": ""})

    def test_log_tail_reads_one_path_and_caps_n(self):
        H.LOG_PATH.write_text("".join("line %d\n" % i for i in range(1, 801)))
        d = self.get("/log/tail?n=3")
        self.assertEqual(d["lines"], ["line 798", "line 799", "line 800"])
        self.assertEqual(d["n"], 3)
        self.assertEqual(d["path"], str(H.LOG_PATH))
        self.assertEqual(len(self.get("/log/tail")["lines"]), 60)
        self.assertEqual(len(self.get("/log/tail?n=9999")["lines"]), 500)
        # no path parameter exists: a made-up one changes nothing
        self.assertEqual(self.get("/log/tail?n=2&path=/etc/passwd")["path"],
                         str(H.LOG_PATH))

    def test_log_tail_of_a_host_that_never_logged(self):
        self.assertEqual(self.get("/log/tail?n=10"), {"path": str(H.LOG_PATH),
                                                      "n": 0, "lines": []})

    def test_host_mem_endpoint_has_the_shape_the_page_draws(self):
        d = self.get("/host/mem")
        for k in ("t0", "up_s", "tick_s", "pts", "now_mb", "start_mb", "threads", "slope_mb_h"):
            self.assertIn(k, d, k)
        for p in d["pts"]:
            self.assertEqual(len(p), 3)

    def test_host_restart_answers_ok_and_then_exits(self):
        gone = threading.Event()
        H.host_exit = lambda code=0: gone.set()
        r = self.post("/host/restart", {})
        self.assertTrue(r["ok"])
        self.assertEqual(r["version"], H.HOST_VERSION)
        self.assertTrue(gone.wait(5), "host_exit was never called")

    # ---- per-seat skins on the settings page ----
    def test_settings_data_carries_who_wears_what(self):
        saved = H.board_skins
        try:
            H.board_skins = {}
            d = self.get("/settings/data")
            self.assertEqual(d["skins"], {})                 # no board yet: picker greys out
            self.assertEqual(d["skin_names"], H.SKIN_NAMES)
            H.board_skins = {"claude": "grok", "codex": "robo"}
            self.assertEqual(self.get("/settings/data")["skins"], {"claude": "grok", "codex": "robo"})
        finally:
            H.board_skins = saved

    def test_test_skin_names_the_seat_and_accepts_a_skin_name(self):
        sent = []
        saved = H.push_board
        self.addCleanup(setattr, H, "push_board", saved)
        H.push_board = lambda m: sent.append(m)
        r = self.get("/test/skin/2?agent=codex")
        self.assertEqual(r, {"ok": True, "skin": "robo", "agent": "codex"})
        self.assertEqual(sent[-1], {"t": "skin", "id": 2, "agent": "codex"})
        r = self.get("/test/skin/grok")                      # no seat: the shown one, old CLI form
        self.assertEqual(r["skin"], "grok")
        self.assertEqual(sent[-1], {"t": "skin", "id": 5})
        bad = self.get("/test/skin/1?agent=nobody")
        self.assertFalse(bad["ok"])
        self.assertEqual(len(sent), 2)                       # nothing pushed for an unknown seat

    def test_board_hello_and_skins_report_update_who_wears_what(self):
        saved = H.board_skins
        try:
            H.board_skins = {}
            H.handle_board_msg(json.dumps({"t": "skins", "skins": {"claude": "kitty", "forest": "grok"}}).encode(),
                               lambda b: None)
            self.assertEqual(H.board_skins, {"claude": "kitty", "forest": "grok"})
            H.handle_board_msg(json.dumps({"t": "skins", "skins": "junk"}).encode(), lambda b: None)
            self.assertEqual(H.board_skins, {"claude": "kitty", "forest": "grok"})   # a bad report changes nothing
        finally:
            H.board_skins = saved


class AlmanacAltTests(unittest.TestCase):
    """五份黄历 (2026-09-22):
    the board cycles the day's own draw + up to four alternates locally."""

    def test_alts_are_four_readings_that_repeat_no_line(self):
        d = datetime.date(2026, 9, 22)
        yi2, ji2, qian1 = H.almanac_plan(d)
        alts = H.almanac_alts(d, yi2, ji2, qian1)
        self.assertEqual(len(alts), 4)
        seen_yi, seen_ji, seen_q = set(yi2), set(ji2), {qian1}
        for a in alts:
            self.assertEqual(len(a["yi"]), 2)
            self.assertEqual(len(a["ji"]), 2)
            for x in a["yi"]:
                self.assertNotIn(x, seen_yi); seen_yi.add(x)
            for x in a["ji"]:
                self.assertNotIn(x, seen_ji); seen_ji.add(x)
            self.assertNotIn(a["qian"], seen_q); seen_q.add(a["qian"])

    def test_alts_are_stable_by_day_and_move_with_it(self):
        d, e = datetime.date(2026, 9, 22), datetime.date(2026, 9, 23)
        yi2, ji2, qian1 = H.almanac_plan(d)
        self.assertEqual(H.almanac_alts(d, yi2, ji2, qian1), H.almanac_alts(d, yi2, ji2, qian1))
        self.assertNotEqual(H.almanac_alts(d, yi2, ji2, qian1), H.almanac_alts(e, *H.almanac_plan(e)))

    def test_msg_and_year_file_carry_the_alternates(self):
        m = H.almanac_msg(datetime.date(2026, 9, 22))
        self.assertEqual(len(m["alt"]), 4)
        line = json.dumps(m)                      # the wire line (escaped, as push_msg sends it)
        self.assertLess(len(line.encode()), 4096)  # firmware RX_LINE_MAX
        self.assertEqual(json.loads(line)["alt"], m["alt"])

    def test_year_hash_changes_with_the_format_not_only_the_bank(self):
        # the "alt4" tag in the hash is what makes the card get the new file once
        self.assertIn('"alt4"', inspect.getsource(H.almanac_year_file))

    def test_qian_text_reads_the_reading_the_board_shows(self):
        m = H.almanac_msg()
        self.assertEqual(H.qian_text(), "今日签文。" + m["qian"])
        self.assertEqual(H.qian_text(0), "今日签文。" + m["qian"])
        self.assertEqual(H.qian_text(2), "今日签文。" + m["alt"][1]["qian"])
        self.assertEqual(H.qian_text("3"), "今日签文。" + m["alt"][2]["qian"])
        self.assertEqual(H.qian_text(9), "今日签文。" + m["qian"])   # out of range = main draw
        self.assertEqual(H.qian_text("x"), "今日签文。" + m["qian"])


# ------------------------------------------------------------------ language
class LangTests(unittest.TestCase):
    """中文 / English: config.json lang -> settings page, cfg, TTS. zh must stay
    exactly as it was before the switch existed."""

    def setUp(self):
        self._lang = H.LANG

    def tearDown(self):
        H.LANG = self._lang

    # ① the dictionary
    def test_both_languages_have_exactly_the_same_keys(self):
        zh, en = set(H.SETTINGS_T["zh"]), set(H.SETTINGS_T["en"])
        self.assertEqual(zh - en, set(), "missing in en")
        self.assertEqual(en - zh, set(), "missing in zh")
        self.assertEqual(sorted(H.SETTINGS_T), sorted(H.LANGS))

    def test_no_empty_strings_and_placeholders_match(self):
        for k in H.SETTINGS_T["zh"]:
            zh, en = H.SETTINGS_T["zh"][k], H.SETTINGS_T["en"][k]
            self.assertTrue(zh and en, k)
            ph = lambda s: sorted(re.findall(r"\{\d\}", s))
            self.assertEqual(ph(zh), ph(en), k)

    def test_every_key_the_page_uses_exists(self):
        h = H.SETTINGS_HTML
        used = set(re.findall(r'data-t[hpa]?="(\w+)"', h))
        used |= set(re.findall(r"\b(?:t|tf)\('(\w+)'", h))
        used |= set(re.findall(r"\btx\((?:[^()]|\([^()]*\))*?,'(\w+)'\)", h))
        used |= set(re.findall(r"srow\('(\w+)'", h))
        used |= set(re.findall(r"field\(\w+,a,'(\w+)'\)", h))
        used |= {"page_" + p for p in re.findall(r"'(\w+)'", h.split("const PAGES=[", 1)[1].split("]", 1)[0])}
        used |= {"pdesc_" + k[5:] for k in list(used) if k.startswith("page_")}
        used |= {"seat_" + a for a in H.AGENTS}
        used |= {"st_" + st for st in ("off", "idle", "working", "needs_you", "done")}
        used |= {"kf_" + k for k in H.SEAT_KEY_KEYS}
        used = {k for k in used if not k.endswith("_")}   # 'page_'+p style prefixes, expanded above
        self.assertGreater(len(used), 100)
        self.assertEqual(sorted(used - set(H.SETTINGS_T["zh"])), [])

    def test_page_carries_the_table_and_the_current_lang(self):
        for lang, title in (("zh", "AgentTouch 设置"), ("en", "AgentTouch Settings")):
            H.LANG = lang
            page = H.settings_html()
            self.assertIn('<html lang="%s">' % lang, page)
            self.assertIn("<title>%s</title>" % title, page)
            self.assertIn('let L="%s";' % lang, page)
            self.assertNotIn("__T_JSON__", page)
            self.assertIn('"Needs You"', page)          # the en table rides along in both
            self.assertIn('"千问办公"', page)

    def test_backend_refusals_follow_lang(self):
        H.LANG = "zh"
        self.assertEqual(H.cpu_working_clean([])[1], "CPU 阈值要是一个 JSON 对象")
        self.assertEqual(H.proc_rules_clean({"nope": {}})[1], "没有这个席位：nope")
        H.LANG = "en"
        self.assertEqual(H.cpu_working_clean([])[1], "CPU thresholds must be a JSON object")
        self.assertEqual(H.proc_rules_clean({"nope": {}})[1], "No such seat: nope")
        self.assertEqual(H.cpu_working_clean({"claude": 101})[1], "claude: CPU threshold must be 0-100")

    # ② cfg
    def test_cfg_carries_lang_and_follows_config(self):
        self.assertEqual(H.LANGS, ("zh", "en"))
        H.LANG = "zh"
        self.assertEqual(H.cfg_msg()["lang"], "zh")
        H.LANG = "en"
        self.assertEqual(H.cfg_msg()["lang"], "en")
        with tempfile.TemporaryDirectory() as td:
            old = H.CONFIG_OVERRIDE
            try:
                H.CONFIG_OVERRIDE = Path(td) / "config.json"
                H.CONFIG_OVERRIDE.write_text('{"lang": "zh"}')
                H.load_overrides()
                self.assertEqual(H.cfg_msg()["lang"], "zh")
                H.CONFIG_OVERRIDE.write_text('{"lang": "en"}')
                H.load_overrides()
                self.assertEqual(H.cfg_msg()["lang"], "en")
                H.CONFIG_OVERRIDE.write_text('{"lang": "ja"}')
                H.load_overrides()
                self.assertEqual(H.LANG, "zh")          # unknown = zh
            finally:
                H.CONFIG_OVERRIDE = old

    def test_save_settings_whitelists_lang_and_pushes_cfg(self):
        sent = []
        saved = (H.CONFIG_OVERRIDE, H.push_msg, H.np_apply, H.prime_voices)
        with tempfile.TemporaryDirectory() as td:
            try:
                H.CONFIG_OVERRIDE = Path(td) / "config.json"
                H.CONFIG_OVERRIDE.write_text('{"voice_style": "tts"}')
                H.push_msg, H.np_apply, H.prime_voices = sent.append, (lambda: None), (lambda: None)
                r = H.save_settings({"lang": "en"})
                self.assertEqual(r, {"ok": True, "keys": ["lang"]})
                cfg = json.loads(H.CONFIG_OVERRIDE.read_text())
                self.assertEqual(cfg, {"voice_style": "tts", "lang": "en"})
                self.assertEqual(sent[-1]["lang"], "en")
                self.assertEqual(H.save_settings({"lang": "ja"})["keys"], [])
                self.assertEqual(H.LANG, "en")
            finally:
                H.CONFIG_OVERRIDE, H.push_msg, H.np_apply, H.prime_voices = saved

    def test_state_reports_lang(self):
        H.LANG = "en"
        self.assertEqual(H.state_snapshot()["lang"], "en")

    # ③ TTS
    def test_voice_follows_lang(self):
        self.assertEqual(H.TTS_VOICES, {"zh": "zh-CN-XiaoyiNeural", "en": "en-US-AriaNeural"})
        H.LANG = "zh"
        self.assertEqual(H.tts_voice(), "zh-CN-XiaoyiNeural")
        H.LANG = "en"
        self.assertEqual(H.tts_voice(), "en-US-AriaNeural")
        self.assertEqual(H.tts_voice("zh"), "zh-CN-XiaoyiNeural")

    def test_phrases_follow_lang_and_zh_is_unchanged(self):
        H.LANG = "zh"
        self.assertEqual(H.phrase_needs("claude"), "主人，Claude在等你哦！")
        self.assertEqual(H.phrase_done("qoderwork"), "千问干完活啦！")
        H.LANG = "en"
        self.assertEqual(H.phrase_needs("claude"), "Hey, Claude needs you!")
        self.assertEqual(H.phrase_done("codex"), "Codex is done!")
        self.assertEqual(H.phrase_done("qoderwork"), "Qwen is done!")
        self.assertEqual(sorted(H.SPEAK_NAMES_EN), sorted(H.AGENTS))

    def test_fortune_is_read_in_chinese_in_either_language(self):
        got = []
        saved = H.say
        try:
            H.say = lambda text, voice=None: got.append((text, voice)) or True
            for lang in H.LANGS:
                H.LANG = lang
                H.say_qian()
        finally:
            H.say = saved
        self.assertEqual([v for _, v in got], ["zh-CN-XiaoyiNeural"] * 2)
        self.assertTrue(all(t.startswith("今日签文。") for t, _ in got))

    def test_cache_key_separates_voices(self):
        src = inspect.getsource(H.tts_wav)
        self.assertIn('f"{voice}|{text}"', src)

    # ④ /test/lang
    def test_test_lang_pushes_one_cfg_and_leaves_config_alone(self):
        self.assertEqual(H.test_lang_msg("en"), {"t": "cfg", "lang": "en"})
        self.assertEqual(H.test_lang_msg("zh"), {"t": "cfg", "lang": "zh"})
        self.assertIsNone(H.test_lang_msg("ja"))
        sent = []
        saved = H.push_msg
        H.LANG = "zh"
        try:
            H.push_msg = sent.append
            r = H.test_lang("en")
            self.assertEqual(r, {"ok": True, "sent": {"t": "cfg", "lang": "en"}, "config_lang": "zh"})
            self.assertFalse(H.test_lang("ja")["ok"])
        finally:
            H.push_msg = saved
        self.assertEqual(sent, [{"t": "cfg", "lang": "en"}])
        self.assertEqual(H.LANG, "zh")

    def test_test_lang_endpoint_answers_json(self):
        sent = []
        saved = H.push_msg
        srv = ThreadingHTTPServer(("127.0.0.1", 0), H.HookHandler)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            H.push_msg = sent.append
            with urlopen("http://127.0.0.1:%d/test/lang/en" % srv.server_address[1], timeout=10) as r:
                d = json.loads(r.read())
        finally:
            H.push_msg = saved
            srv.shutdown(); srv.server_close(); th.join(5)
        self.assertTrue(d["ok"])
        self.assertEqual(sent, [{"t": "cfg", "lang": "en"}])

    def test_local_file_show_follows_lang(self):
        with tempfile.NamedTemporaryFile(suffix=".mp3") as f:
            H.LANG = "zh"
            self.assertEqual(H.au_resolve({"kind": "file", "src": f.name})[0]["show"], "本地文件")
            H.LANG = "en"
            self.assertEqual(H.au_resolve({"kind": "file", "src": f.name})[0]["show"], "Local file")


class SeatsInstalledTests(unittest.TestCase):
    """a fresh Mac with only some agents -> the rest read 'not installed'."""

    def test_only_what_is_there(self):
        with tempfile.TemporaryDirectory() as home:
            h = Path(home)
            (h / ".claude").mkdir()
            (h / "Applications" / "Qoder CN.app").mkdir(parents=True)
            with mock.patch.object(H.Path, "home", return_value=h), \
                 mock.patch.object(H.shutil, "which", return_value=None), \
                 mock.patch.dict(os.environ, {"PATH": ""}), \
                 mock.patch.object(H.Path, "exists", lambda self: str(self).startswith(home) and os.path.exists(self)):
                got = H.seats_installed()
        self.assertEqual(set(got), set(H.AGENTS))
        self.assertEqual({a for a, v in got.items() if v}, {"claude", "forest"})


class QoderBuildsTests(unittest.TestCase):
    """Second Mac 2026-09-26: international Qoder ("Qoder IDE.app", "Qoder.app",
    data in Application Support/Qoder and com.qoder.app.stable). Process lines
    below are copied from that Mac's ps output."""

    def _hits(self, args):
        return [
            a for a, r in H.PROC_RULES.items()
            if H.re.search(r["match"], args) and not (r["exclude"] and H.re.search(r["exclude"], args))]

    def test_international_process_lines(self):
        self.assertEqual(self._hits("/Applications/Qoder IDE.app/Contents/MacOS/Qoder"), ["qoder"])
        self.assertEqual(self._hits("/Applications/Qoder IDE.app/Contents/Frameworks/Qoder Helper (Renderer).app/"
                                    "Contents/MacOS/Qoder Helper (Renderer) --type=renderer"), ["qoder"])
        self.assertEqual(self._hits("/Applications/Qoder.app/Contents/MacOS/Qoder"), ["forest"])
        self.assertEqual(self._hits("/Applications/Qoder.app/Contents/Frameworks/Qoder Helper.app/Contents/MacOS/"
                                    "Qoder Helper --type=gpu-process --user-data-dir=/Users/x/Library/Application "
                                    "Support/com.qoder.app.stable"), ["forest"])
        # neighbours that must not light a Qoder seat
        self.assertEqual(self._hits("/Applications/QoderWork.app/Contents/MacOS/QoderWork"), [])
        self.assertEqual(self._hits("/Users/x/.qoder/bin/qoder-computer-use/Qoder Computer Use.app/Contents/"
                                    "SharedSupport/QoderComputerUseBridge.app/Contents/MacOS/QoderComputerUseBridge"), [])

    def test_domestic_lines_unchanged(self):
        self.assertEqual(self._hits("/Applications/Qoder CN IDE.app/Contents/MacOS/Qoder CN"), ["qoder"])
        self.assertEqual(self._hits("/Applications/Qoder CN.app/Contents/MacOS/Qoder CN"), ["forest"])

    def test_saved_old_defaults_do_not_pin_the_domestic_names(self):
        with tempfile.TemporaryDirectory() as td:
            cfgp = Path(td) / "config.json"
            cfgp.write_text(json.dumps({"proc_rules": {
                "qoder": {"match": r"Qoder CN IDE\.app", "exclude": None},
                "forest": {"match": r"Qoder CN\.app", "exclude": r"Qoder CN IDE\.app"},
                "claude": {"match": "my-claude", "exclude": None}}}))
            saved = {a: dict(r) for a, r in H.PROC_RULES.items()}
            with mock.patch.object(H, "CONFIG_OVERRIDE", cfgp):
                try:
                    H.load_overrides()
                    self.assertEqual(H.PROC_RULES["qoder"], H.DEFAULT_PROC_RULES["qoder"])
                    self.assertEqual(H.PROC_RULES["forest"], H.DEFAULT_PROC_RULES["forest"])
                    self.assertEqual(H.PROC_RULES["claude"]["match"], "my-claude")   # a real choice stays
                finally:
                    H.PROC_RULES.clear(); H.PROC_RULES.update(saved)

    def test_focus_app_follows_the_installed_build(self):
        apps = {"qoder": "Qoder CN IDE", "forest": "Qoder CN", "claude": "Warp"}
        intl = {"Qoder IDE", "Qoder"}
        with mock.patch.object(H, "app_installed", lambda n: n in intl):
            H.resolve_app_variants(apps)
        self.assertEqual(apps, {"qoder": "Qoder IDE", "forest": "Qoder", "claude": "Warp"})
        apps = {"qoder": "Qoder CN IDE", "forest": "My Qoder Fork"}
        with mock.patch.object(H, "app_installed", lambda n: False):   # neither build: leave it
            H.resolve_app_variants(apps)
        self.assertEqual(apps, {"qoder": "Qoder CN IDE", "forest": "My Qoder Fork"})

    def test_log_tail_reads_either_build(self):
        with tempfile.TemporaryDirectory() as td:
            cn, intl = Path(td) / "QoderCN" / "logs", Path(td) / "Qoder" / "logs"
            f = intl / "20260926" / "questWindow" / "agent.log"
            f.parent.mkdir(parents=True)
            f.write_text("x\n")
            lt = H.LogTail([cn, intl], "*/questWindow/agent.log")
            self.assertEqual(lt._newest(), f)


class QoderChatLogTests(unittest.TestCase):
    """The IDE's editor-window chat (智能体 / 专家团) logs to window<N>/agent.log
    keyed by a session UUID; the host only read questWindow/ task ids and sat
    on idle through a whole agent turn (second Mac, 2026-09-27). Samples are
    this Mac's 2026-08-27 lines."""
    SID = "4a74bc1e-dd9c-44ed-b1b2-b46f0c640246"

    def chat(self, st):
        return ('2026-08-27 21:54:54.891 [info] [ChatPanel.acpBlocks] {"sessionId":"%s",'
                '"progressLen":1,"blocksLen":0,"state":"%s","logReason":"state"}' % (self.SID, st))

    def setUp(self):
        self.q = {"tasks": {}, "done_until": 0.0, "needs_until": 0.0}

    def feed(self, *lines, t=1000.0):
        return [H.qoder_apply(ln, t, self.q) for ln in lines]

    def test_a_chat_turn_is_working_then_done(self):
        self.feed(self.chat("prompting"), self.chat("streaming"))
        self.assertEqual(self.q["tasks"][self.SID][0], "streaming")
        self.assertEqual(self.q["done_until"], 0.0)
        self.feed(self.chat("completed"))
        self.assertGreater(self.q["done_until"], 1000.0)

    def test_a_replayed_completed_is_not_a_done(self):
        self.feed(self.chat("completed"), self.chat("completed"))
        self.assertEqual(self.q["done_until"], 0.0)

    def test_suspended_is_needs_you_until_the_user_answers(self):
        self.feed(self.chat("streaming"), self.chat("suspended"))
        self.assertGreater(self.q["needs_until"], 1000.0)
        self.feed(self.chat("suspended"))                 # re-render: still waiting
        self.assertGreater(self.q["needs_until"], 1000.0)
        self.feed(self.chat("streaming"))                 # user_resume
        self.assertEqual(self.q["needs_until"], 0.0)

    def test_a_quest_line_still_keys_on_its_task_id(self):
        ln = ('[ChatPanel.acpBlocks] {"taskId":"task-93d9de539bc6406384f7","sessionId":'
              '"task-93d9de539bc6406384f7.session.execution","progressLen":2,"blocksLen":0,'
              '"state":"prompting","logReason":"session"}')
        self.assertEqual(self.feed(ln), ["prompting"])
        self.assertEqual(list(self.q["tasks"]), ["task-93d9de539bc6406384f7"])

    def test_lines_without_a_state_or_a_real_session_change_nothing(self):
        out = self.feed(
            "[ACPProgressStateMachine] State transition: streaming -> completed, trigger: "
            "chat_finish:success:200, sessionId: " + self.SID,
            '[usePendingTools] subscribe: {"sessionId":"blank_session_quest","hasPendingTools":false}',
            "[ACP] ACP client initialization completed successfully {}")
        self.assertEqual(out, [None, None, None])
        self.assertEqual(self.q["tasks"], {})

    def test_the_watcher_reads_the_editor_windows_too(self):
        src = inspect.getsource(H.poll_app_logs)
        self.assertIn("_qoder_win_tail.poll()", src)
        self.assertIn("qoder_apply(", src)
        self.assertIn("_qoder_win_tail.files", inspect.getsource(H.aggregate))
        self.assertIn("suspended", H.QODER_LIVE)


class CodexPermCardTests(unittest.TestCase):
    """Codex's request_permissions card fires no hook (2026-09-27, ChatGPT
    26.924 code mode): the rollout ends on the exec call until the card is
    answered. Line shapes from that day's rollout, paths and ids scrubbed."""

    CALL = json.dumps({"timestamp": "2026-09-27T10:14:07.560Z", "ordinal": 12, "type": "response_item",
                       "payload": {"type": "custom_tool_call", "status": "completed", "call_id": "call_A",
                                   "name": "exec",
                                   "input": 'text(await tools.request_permissions({permissions:{file_system:'
                                            '{write:["/tmp/t.txt"]}},reason:"x"}));\n'}})
    TOKENS = json.dumps({"ordinal": 13, "type": "token_usage_record", "payload": {"thread_id": "t"}})
    COUNT = json.dumps({"type": "event_msg", "payload": {"type": "token_count", "info": {}}})
    OUTPUT = json.dumps({"type": "response_item",
                         "payload": {"type": "custom_tool_call_output", "call_id": "call_A",
                                     "output": "Script running with cell ID 1\nWall time 31.0 seconds\nOutput:\n"}})
    ABORTED = json.dumps({"type": "event_msg", "payload": {"type": "turn_aborted"}})
    EXEC = json.dumps({"type": "response_item",
                       "payload": {"type": "custom_tool_call", "call_id": "call_C", "name": "exec",
                                   "input": 'text(await tools.exec_command({cmd:"ls"}))'}})
    FUNC = json.dumps({"type": "response_item",
                       "payload": {"type": "function_call", "name": "request_permissions",
                                   "arguments": "{}", "call_id": "call_B"}})

    def test_the_verdict(self):
        v = H.codex_rollout_pending
        self.assertEqual(v([self.CALL]), "call_A")
        self.assertEqual(v([self.CALL, self.TOKENS]), "call_A")              # what 18:14:07-18:16:42 looked like
        self.assertEqual(v(['{"cut-off head', self.CALL, self.TOKENS, self.COUNT]), "call_A")
        self.assertIsNone(v([self.CALL, self.TOKENS, self.OUTPUT]))          # answered
        self.assertIsNone(v([self.CALL, self.TOKENS, self.OUTPUT, self.COUNT]))
        self.assertIsNone(v([self.CALL, self.ABORTED]))                      # Esc: the turn is over
        self.assertIsNone(v([self.EXEC, self.TOKENS]))                       # an ordinary exec
        self.assertEqual(v([self.EXEC, self.FUNC]), "call_B")
        self.assertIsNone(v([]))
        self.assertIsNone(v([self.TOKENS]))

    def write(self, lines, age, pad=0):
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        p = Path(d) / "rollout-x.jsonl"
        with open(p, "w") as f:
            for _ in range(pad):
                f.write(json.dumps({"type": "response_item", "payload": {"type": "message", "x": "y" * 900}}) + "\n")
            f.write("\n".join(lines) + "\n")
        t = time.time() - age
        os.utime(p, (t, t))
        return p

    def pending(self, p):
        files = []
        H.newest_mtime(p.parent, files, time.time() - H.CODEX_PERM_TTL)
        return H.codex_perm_pending(files, time.time())

    def test_reading_the_rollouts(self):
        self.assertEqual(self.pending(self.write([self.CALL, self.TOKENS], 5)), "call_A")
        self.assertIsNone(self.pending(self.write([self.CALL, self.TOKENS], 1)))          # a rule may answer it at once
        self.assertIsNone(self.pending(self.write([self.CALL, self.TOKENS], H.CODEX_PERM_TTL + 5)))
        self.assertIsNone(self.pending(self.write([self.CALL, self.OUTPUT], 5)))
        big = self.write([self.CALL, self.TOKENS], 5, pad=400)                             # ~360 KB: tail read
        self.assertGreater(big.stat().st_size, H.CODEX_TAIL_BYTES)
        self.assertEqual(self.pending(big), "call_A")
        self.assertIn(big, H._codex_tail_cache)
        H._codex_tail_cache[big] = (H._codex_tail_cache[big][0], "cached")
        self.assertEqual(self.pending(big), "cached")                                     # unchanged file: not re-read

    def test_the_seat_and_the_board_tap(self):
        src = inspect.getsource(H.aggregate)
        i = src.index("perm = codex_perm_pending(fresh, now)")
        self.assertIn('perm == codex_notify.get("perm_done")', src[i:i + 200])
        self.assertIn("if needs or perm:", src[i:i + 600])
        self.assertIn("newest_mtime(CODEX_SESSIONS, fresh, now - CODEX_PERM_TTL)", src)
        d = inspect.getsource(H.decision_key)
        self.assertIn('codex_notify["perm_done"] = codex_notify.get("perm_call")', d)
        self.assertLess(d.index('codex_notify["perm_done"]'), d.index("inject(go)"))


class WindowLogsTests(unittest.TestCase):
    """WindowLogs follows every window<N>/agent.log of the newest launch dir."""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name) / "Qoder" / "logs"

    def tearDown(self):
        self.td.cleanup()

    def write(self, launch, win, text, mtime=None):
        f = self.root / launch / win / "agent.log"
        f.parent.mkdir(parents=True, exist_ok=True)
        with open(f, "a") as fh:
            fh.write(text)
        if mtime:
            os.utime(f, (mtime, mtime))
        return f

    def poll(self, w):
        if not getattr(w, "_cleanup", False):
            w._cleanup = True
            self.addCleanup(lambda: [e[0].close() for e in w.files.values()])
        w.checked = 0.0                                   # skip the 5 s rescan wait
        return w.poll()

    def test_history_is_skipped_and_every_window_is_followed(self):
        self.write("20260927T090000", "window1", "old 1\n")
        self.write("20260927T090000", "window2", "old 2\n")
        w = H.WindowLogs([self.root], "window*/agent.log")
        self.assertEqual(self.poll(w), [])                # opened at EOF
        self.write("20260927T090000", "window1", "new 1\n")
        self.write("20260927T090000", "window2", "new 2\nhalf")
        self.assertEqual(sorted(self.poll(w)), ["new 1", "new 2"])
        self.write("20260927T090000", "window2", " line\n")
        self.assertEqual(self.poll(w), ["half line"])

    def test_a_window_opened_later_is_read_from_its_start(self):
        self.write("20260927T090000", "window1", "old\n")
        w = H.WindowLogs([self.root], "window*/agent.log")
        self.poll(w)
        self.write("20260927T090000", "window3", "first\n")
        self.assertEqual(self.poll(w), ["first"])

    def test_a_relaunch_moves_to_the_new_launch_dir(self):
        self.write("20260926T090000", "window1", "yesterday\n", mtime=1000)
        w = H.WindowLogs([self.root], "window*/agent.log")
        self.poll(w)
        self.write("20260927T090000", "window1", "today\n")
        self.assertEqual(self.poll(w), ["today"])
        self.assertEqual(w.dir.name, "20260927T090000")

    def test_no_logs_at_all(self):
        w = H.WindowLogs([self.root], "window*/agent.log")
        self.assertEqual(self.poll(w), [])
        self.assertEqual(w.files, {})


class ScanAgentsTests(unittest.TestCase):
    """the 扫描本机 table on a Mac with the international Qoder."""

    def test_international_mac(self):
        with tempfile.TemporaryDirectory() as home:
            h = Path(home)
            for app in ("Qoder IDE", "Qoder", "QoderWork"):
                (h / "Applications" / (app + ".app")).mkdir(parents=True)
            ps = "/Users/x/Applications/Qoder.app/Contents/MacOS/Qoder\n/Applications/QoderWork.app/Contents/MacOS/QoderWork\n"
            fake = mock.Mock(stdout=ps)
            with mock.patch.object(H.Path, "home", return_value=h), \
                 mock.patch.object(H.shutil, "which", return_value=None), \
                 mock.patch.object(H.subprocess, "run", return_value=fake), \
                 mock.patch.dict(H.FOCUS_APPS, {"qoder": "Qoder IDE", "forest": "Qoder"}), \
                 mock.patch.object(H.Path, "exists", lambda self: str(self).startswith(home) and os.path.exists(self)):
                rows = {r["key"]: r for r in H.scan_agents()}
        self.assertEqual(set(rows), {"qoder-ide", "qoder", "qoderwork"})
        self.assertEqual(rows["qoder-ide"]["seat"], "qoder")
        self.assertEqual(rows["qoder"]["seat"], "forest")
        self.assertTrue(rows["qoder"]["running"])
        self.assertFalse(rows["qoder-ide"]["running"])
        self.assertTrue(rows["qoder"]["current"])
        self.assertIsNone(rows["qoderwork"]["seat"])

    def test_use_merges_into_config(self):
        with tempfile.TemporaryDirectory() as td:
            cfgp = Path(td) / "config.json"
            cfgp.write_text(json.dumps({"focus_apps": {"claude": "iTerm"}, "lang": "zh"}))
            saved = dict(H.FOCUS_APPS)
            with mock.patch.object(H, "CONFIG_OVERRIDE", cfgp):
                try:
                    self.assertTrue(H.agents_use("qoder", "Qoder IDE")["ok"])
                    self.assertFalse(H.agents_use("nope", "X")["ok"])
                finally:
                    H.FOCUS_APPS.clear(); H.FOCUS_APPS.update(saved)
            got = json.loads(cfgp.read_text())
            self.assertEqual(got["focus_apps"], {"claude": "iTerm", "qoder": "Qoder IDE"})
            self.assertEqual(got["lang"], "zh")


class AgentSetupTests(unittest.TestCase):
    """the guide an agent reads, with a live snapshot appended."""

    def test_guide_plus_snapshot(self):
        with mock.patch.object(H, "scan_agents", return_value=[
                {"name": "Qoder IDE", "seat": "qoder", "build": "intl", "where": "/Applications/Qoder IDE.app",
                 "running": True, "how": "log", "wired": False, "current": True}]):
            md = H.agent_setup_md()
        self.assertIn("## 二、边界", md)                     # the file itself
        self.assertIn("## 本机现状", md)                      # the live part
        self.assertIn("| Qoder IDE | qoder | intl |", md)
        self.assertNotIn("pass", md.split("## 本机现状")[1].lower())   # never echoes secrets

    def test_endpoints_named_in_the_guide_exist(self):
        guide = (HERE / "agent_setup.md").read_text()
        src = (HERE / "agentpet_host.py").read_text()
        for ep in re.findall(r"127\.0\.0\.1:8788(/[a-z/_-]+)", guide):
            ep = re.sub(r"/(qoder|claude|codex|forest|qoderwork)$", "/", ep)   # /test/select/<seat>
            self.assertIn('"%s' % ep, src, ep)


class OwnerTests(unittest.TestCase):
    """认领制: digest shared with the firmware, advertisement
    parsing, the connect/standby verdict and the owner state machine."""

    def setUp(self):
        self._saved = dict(H.board_owner), dict(H._owner_names), dict(H._claim)

    def tearDown(self):
        H.board_owner.clear(); H.board_owner.update(self._saved[0])
        H._owner_names.clear(); H._owner_names.update(self._saved[1])
        H._claim.clear(); H._claim.update(self._saved[2])

    def test_digest_is_fnv1a_like_the_firmware(self):
        # FNV-1a 32 reference values; main.cpp ownerDigest() is the same loop
        self.assertEqual(H.owner_digest("a"), 0xE40C292C)
        self.assertEqual(H.owner_digest("foobar"), 0xBF9CF968)
        self.assertEqual(H.owner_digest(""), 2166136261)
        fw = (HERE.parent / "firmware/src/main.cpp").read_text()
        self.assertIn("2166136261u", fw)
        self.assertIn("16777619u", fw)

    def test_adv_digest(self):
        self.assertIsNone(H.adv_owner_digest({}))                      # old firmware
        self.assertIsNone(H.adv_owner_digest(None))
        self.assertIsNone(H.adv_owner_digest({0x004C: b"A\x01\x01\x02\x03\x04"}))   # someone else's id
        self.assertIsNone(H.adv_owner_digest({0xFFFF: b"B\x01\x01\x02\x03\x04"}))
        self.assertEqual(H.adv_owner_digest({0xFFFF: b"A\x01\x00\x00\x00\x00"}), 0)
        self.assertEqual(H.adv_owner_digest({0xFFFF: bytearray(b"A\x01\x2c\x29\x0c\xe4")}), 0xE40C292C)

    def test_verdict(self):
        V = H.ble_owner_verdict
        self.assertEqual(V(None, 7, {}, False), "legacy")
        self.assertEqual(V(0, 7, {}, False), "connect")       # nobody's: first host adopts it
        self.assertEqual(V(7, 7, {}, False), "connect")       # ours
        self.assertEqual(V(7, 7, {}, True), "connect")        # claiming what we own = just connect
        self.assertEqual(V(0, 7, {}, True), "claim")
        self.assertEqual(V(9, 7, {}, False), "probe")         # someone's, name unknown: ask once
        self.assertEqual(V(9, 7, {9: "B"}, False), "standby") # someone's we know: leave it
        self.assertEqual(V(9, 7, {9: "B"}, True), "claim")

    def test_owner_messages(self):
        H.board_owner.update(state="", name="")
        H._claim["want"] = True
        H.owner_msg({"t": "owner", "mine": False, "id": "other-id", "name": "B 的 Mac"})
        self.assertEqual(H.board_owner["state"], "standby")
        self.assertEqual(H._owner_names[H.owner_digest("other-id")], "B 的 Mac")
        self.assertTrue(H._claim["want"])                     # still wanted: a refusal is not a claim answer
        H.owner_msg({"t": "owner", "mine": True, "id": "me", "name": "A"})
        self.assertEqual((H.board_owner["state"], H.board_owner["name"]), ("mine", "A"))
        self.assertFalse(H._claim["want"])
        H.owner_msg({"t": "released", "by": "B 的 Mac"})
        self.assertEqual((H.board_owner["state"], H.board_owner["name"]), ("standby", "B 的 Mac"))

    def test_hostinfo_line(self):
        ident = {"id": "abc", "name": "余的 Mac", "mdns": "yu-mac", "ip": "10.0.0.2", "port": 8737}
        with mock.patch.object(H, "my_identity", return_value=ident), \
             mock.patch.object(H, "host_identity", return_value=dict(ident, ip="10.0.0.9")):
            hi = H.hostinfo_msg()
            cl = H.hostinfo_msg(claim=True)
        self.assertTrue(hi.endswith(b"\n") and hi.count(b"\n") == 1)
        m = json.loads(hi)
        self.assertEqual(m["t"], "hostinfo")
        self.assertEqual(m["ip"], "10.0.0.9")                 # current address, not the one from start
        self.assertEqual(m["name"], "余的 Mac")
        self.assertEqual(json.loads(cl)["t"], "claim")
        self.assertLess(len(hi), 400)                         # well under the board's 4 KB line cap

    def test_lan_ip_skips_proxy_tun(self):
        self.assertTrue(H.is_lan_ip("10.0.0.107"))
        self.assertTrue(H.is_lan_ip("10.0.0.2"))
        self.assertTrue(H.is_lan_ip("172.20.1.1"))
        self.assertFalse(H.is_lan_ip("198.18.0.1"))           # Clash fake-ip TUN
        self.assertFalse(H.is_lan_ip("172.32.0.1"))
        self.assertFalse(H.is_lan_ip(""))

    def test_claim_card_answers(self):
        H.board_owner.update(state="", name="", ask_until=0.0, declined_at=0.0)
        H._claim.update(want=True, result="")
        H.owner_msg({"t": "owner", "mine": False, "pending": True, "sec": 30, "id": "", "name": ""})
        snap = H.owner_snapshot()
        self.assertEqual(snap["state"], "asking")
        self.assertTrue(28 <= snap["ask_s"] <= 30)
        self.assertEqual(H._claim["result"], "")              # still waiting on the tap
        H.owner_msg({"t": "owner", "mine": False, "declined": True, "why": "timeout", "id": "", "name": ""})
        self.assertEqual(H.board_owner["state"], "free")      # no owner, and it said no to us
        self.assertEqual(H._claim["result"], "timeout")
        self.assertFalse(H._claim["want"])                    # no second card from the next scan
        self.assertGreater(H.board_owner["declined_at"], 0)
        # the next scan leaves that ownerless board alone unless the user claims
        V = H.ble_owner_verdict
        self.assertEqual(V(0, 7, {}, False, declined=True), "standby")
        self.assertEqual(V(0, 7, {}, True, declined=True), "claim")
        # a refusal because it is owned is not a claim answer
        H._claim.update(want=True, result="")
        H.owner_msg({"t": "owner", "mine": False, "why": "owned", "id": "x", "name": "B"})
        self.assertEqual(H._claim["result"], "")
        self.assertEqual(H.board_owner["state"], "standby")

    def test_claim_returns_early_on_refusal(self):
        H.board_owner.update(state="standby", name="B")
        def refuse():
            time.sleep(0.2)
            H.owner_msg({"t": "owner", "mine": False, "why": "declined", "id": "x", "name": "B"})
        with mock.patch.object(H, "push_board_raw_claim"), mock.patch.object(H, "boards", [object()]):
            threading.Thread(target=refuse).start()
            t0 = time.time()
            r = H.board_claim()
        self.assertEqual(r, {"ok": False, "why": "declined"})
        self.assertLess(time.time() - t0, 3)
        self.assertFalse(H._claim["want"])

    def test_released_names_the_new_owner(self):
        H.owner_msg({"t": "released", "by": "B", "id": "bid"})
        self.assertEqual(H._owner_names[H.owner_digest("bid")], "B")   # no extra probe needed

    def test_already_mine_claim_is_instant(self):
        H.board_owner.update(state="mine", name="A")
        self.assertEqual(H.board_claim(), {"ok": True, "name": "A", "already": True})

    def test_owner_on_settings_data_and_endpoints(self):
        src = (HERE / "agentpet_host.py").read_text()
        for ep in ('"/board/owner"', '"/board/claim"'):
            self.assertIn(ep, src)
        self.assertIn('"owner": owner_snapshot()', src)


class ClaudeSessionTests(unittest.TestCase):
    """several Claude Code terminals — the board's session row and the
    session-exact routing behind it. Nothing here raises a window or types."""

    def setUp(self):
        self._saved = dict(H.claude_sessions)
        H.claude_sessions.clear()
        self._inject = mock.patch.object(H, "inject", lambda fn: None)
        self._push = mock.patch.object(H, "push_raw", lambda msg: None)
        self._inject.start()
        self._push.start()

    def tearDown(self):
        self._inject.stop()
        self._push.stop()
        H.claude_sessions.clear()
        H.claude_sessions.update(self._saved)

    def test_headers_become_session_fields(self):
        meta = H.sess_meta_from_headers({
            "X-AT-Pid": "4711", "X-AT-Tty": "ttys003 ", "X-AT-Term": "WarpTerminal",
            "X-AT-Bundle": "dev.warp.Warp-Stable", "X-AT-Warp": "warp://session/abc",
            "X-AT-Iterm": ""})
        self.assertEqual(meta, {"hpid": 4711, "tty": "/dev/ttys003", "term": "WarpTerminal",
                                "bundle": "dev.warp.Warp-Stable", "warp": "warp://session/abc"})
        self.assertEqual(H.sess_meta_from_headers({"X-AT-Pid": "x1", "X-AT-Tty": "??"}), {})

    def test_adapter_per_terminal(self):
        A = H.sess_adapter
        self.assertEqual(A({"term": "WarpTerminal", "warp": "warp://session/1f", "tty": "/dev/ttys1"}),
                         ("warp", "warp://session/1f"))
        self.assertEqual(A({"term": "Apple_Terminal", "tty": "/dev/ttys004", "bundle": "com.apple.Terminal"}),
                         ("terminal", "/dev/ttys004"))
        self.assertEqual(A({"term": "iTerm.app", "iterm": "w0t1p0:ABC-123"}), ("iterm", "ABC-123"))
        self.assertEqual(A({"term": "ghostty", "cwd": "/x/y"}), ("ghostty", "/x/y"))
        self.assertEqual(A({"term": "vscode", "bundle": "com.microsoft.VSCode"}),
                         ("app", "com.microsoft.VSCode"))
        self.assertEqual(A({}), (None, None))
        # Warp's URL is inherited by anything started from a Warp shell: a
        # Terminal.app session carrying it is still a Terminal.app session
        self.assertEqual(A({"term": "Apple_Terminal", "tty": "/dev/ttys2",
                            "warp": "warp://session/cad1"}), ("terminal", "/dev/ttys2"))
        self.assertEqual(A({"term": "tmux", "warp": "warp://session/cad1"}),
                         ("warp", "warp://session/cad1"))

    def test_title_from_transcript_tail(self):
        tail = ('{"type":"ai-title","aiTitle":"Old name"}\n'
                '{"type":"user","message":{"content":"ai-title in text"}}\n'
                '{"type":"ai-title","aiTitle":"✳ Claude Code 多终端窗口管理","sessionId":"s"}\n'
                '{"type":"ai-title","aiTi')          # cut-off last line
        self.assertEqual(H.sess_title_from_tail(tail), "Claude Code 多终端窗口管理")
        self.assertIsNone(H.sess_title_from_tail('{"type":"user"}\n'))

    def test_folder_labels_number_duplicates(self):
        recs = {"a": {"cwd": "/p/esptouch"}, "b": {"cwd": "/q/05-VibeWriter"},
                "c": {"cwd": "/p/esptouch"}, "d": {}}
        self.assertEqual(H.sess_dir_labels(["a", "b", "c", "d"], recs),
                         {"a": "esptouch", "b": "05-VibeWriter", "c": "esptouch·2", "d": "claude"})

    def test_policy(self):
        P = H.sess_policy
        order = ["a", "b", "c", "d"]
        idle = {s: "idle" for s in order}
        act = {"a": 1, "b": 5, "c": 2, "d": 3}
        # a fresh list shows the most recently active session
        self.assertEqual(P(order, [], idle, {}, None, {}, act, 0, 100)[0], "b")
        # a request in another session jumps there (and the Mac should follow)
        st = dict(idle, c="needs_you")
        self.assertEqual(P(order, order, st, idle, "a", {"c": 99}, act, 0, 100), ("c", 0.0, "c"))
        # a second request while the current one waits: stay (first come, first answered)
        st2 = dict(st, d="needs_you")
        self.assertEqual(P(order, order, st2, st, "c", {"c": 99, "d": 100}, act, 0, 101)[:2], ("c", 0.0))
        # answered: hand over 0.7 s later, not at once
        st3 = dict(st2, c="working")
        cur, relay, jumped = P(order, order, st3, st2, "c", {"d": 100}, act, 0, 102)
        self.assertEqual((cur, jumped), ("c", None))
        self.assertAlmostEqual(relay, 102 + H.SESS_RELAY_S)
        self.assertEqual(P(order, order, st3, st3, "c", {"d": 100}, act, relay, 102.8), ("d", 0.0, "d"))
        # typing in a tab on the Mac moves the board there
        self.assertEqual(P(order, order, idle, idle, "a", {}, act, 0, 100, typed="d")[0], "d")
        # the current session ends: the one after it, or before it at the end
        self.assertEqual(P(["a", "c", "d"], order, idle, idle, "b", {}, act, 0, 100)[0], "c")
        self.assertEqual(P(["a", "b", "c"], order, idle, idle, "d", {}, act, 0, 100)[0], "c")
        self.assertEqual(P([], order, {}, idle, "a", {}, {}, 0, 100), (None, 0.0, None))

    def test_board_line(self):
        recs = {}
        one = H.sess_build_msg(["a"], recs, {"a": "idle"}, "a", {}, {})
        self.assertEqual(one["list"], [])               # one session: the board draws nothing
        long = "题" * 40                                 # 120 bytes of UTF-8
        m = H.sess_build_msg(["aaaaaaaa-1", "bbbbbbbb-2"], recs,
                             {"aaaaaaaa-1": "working", "bbbbbbbb-2": "needs_you"},
                             "bbbbbbbb-2", {"aaaaaaaa-1": long}, {"aaaaaaaa-1": "esptouch"})
        self.assertEqual(m["cur"], "bbbbbbbb")
        self.assertEqual([i["id"] for i in m["list"]], ["aaaaaaaa", "bbbbbbbb"])
        self.assertLessEqual(len(m["list"][0]["ti"].encode()), H.SESS_TITLE_MAX)
        self.assertTrue(long.startswith(m["list"][0]["ti"]))
        self.assertEqual(m["list"][1], {"id": "bbbbbbbb", "ti": "", "dir": "", "st": "needs_you"})
        order = ["s%02d" % i for i in range(11)]
        m = H.sess_build_msg(order, recs, {}, "s00", {}, {})
        ids = [i["id"] for i in m["list"]]
        self.assertEqual(len(ids), H.SESS_MAX)
        self.assertIn("s00", ids)
        self.assertEqual(ids, sorted(ids))

    def test_approve_plan(self):
        now = 1000.0
        warp = {"pid": 1, "tty": "/dev/ttys1", "warp": "warp://session/a", "state": "needs_you",
                "needs_since": 5, "ts": now}
        code1 = {"pid": 2, "tty": "/dev/ttys2", "bundle": "com.microsoft.VSCode", "state": "needs_you",
                 "needs_since": 3, "ts": now}
        code2 = dict(code1, pid=3, tty="/dev/ttys3", state="idle")
        S = {"aaaa1111": warp, "bbbb2222": code1, "cccc3333": code2}
        self.assertEqual(H.approve_plan("aaaa1111", S, now), ("go", "aaaa1111"))
        self.assertEqual(H.approve_plan("cccc3333", S, now), ("drop", "cccc3333"))   # not waiting
        self.assertEqual(H.approve_plan("dddd4444", S, now), ("drop", None))         # gone
        self.assertEqual(H.approve_plan("bbbb2222", S, now), ("refuse", "bbbb2222")) # 2 in VS Code
        self.assertEqual(H.approve_plan(None, S, now), ("refuse", "bbbb2222"))       # oldest request
        self.assertEqual(H.approve_plan(None, {"aaaa1111": warp}, now), ("go", "aaaa1111"))
        self.assertEqual(H.approve_plan(None, {"aaaa1111": dict(warp, state="idle")}, now),
                         ("legacy", None))
        self.assertEqual(H.approve_plan(None, {"x": {"state": "needs_you"}}, now), ("legacy", None))

    def test_hook_records_terminal_and_requests(self):
        H.claude_hook("UserPromptSubmit", {"session_id": "s-1", "cwd": "/p/esptouch",
                                           "transcript_path": "/t/s-1.jsonl"},
                      {"hpid": 4242, "tty": "/dev/ttys009", "warp": "warp://session/z"})
        s = H.claude_sessions["s-1"]
        self.assertEqual((s["cwd"], s["hpid"], s["tty"], s["warp"], s["state"]),
                         ("/p/esptouch", 4242, "/dev/ttys009", "warp://session/z", "working"))
        H.claude_hook("Notification", {"session_id": "s-1", "message": "Claude needs your permission"})
        first = H.claude_sessions["s-1"]["needs_since"]
        H.claude_hook("Notification", {"session_id": "s-1", "message": "Claude needs your permission"})
        self.assertEqual(H.claude_sessions["s-1"]["needs_since"], first)   # not reset by a repeat

    def test_dead_process_leaves_at_once(self):
        now = time.time()
        H.claude_sessions["gone"] = {"state": "working", "ts": now, "done_until": 0, "pid": 999999}
        H.claude_sessions["here"] = {"state": "idle", "ts": now, "done_until": 0, "pid": os.getpid()}
        H.aggregate()
        self.assertNotIn("gone", H.claude_sessions)
        self.assertIn("here", H.claude_sessions)

    def test_long_tool_shell_keeps_working(self):
        """One long Bash call sends no hooks; its live shell holds 'working'."""
        now = time.time()
        old = now - H.CLAUDE_WORK_DECAY - 5
        H.claude_sessions["busy"] = {"state": "working", "ts": old, "done_until": 0, "pid": 4711}
        H.claude_sessions["quiet"] = {"state": "working", "ts": old, "done_until": 0, "pid": 4712}
        H.claude_sessions["ended"] = {"state": "idle", "ts": old, "done_until": 0, "pid": 4713}
        with mock.patch.object(H, "_tool_shell_parents", lambda: {4711, 4713}):
            H.sess_keep_busy(now)
        self.assertEqual(H.sess_state(H.claude_sessions["busy"], now), "working")
        self.assertEqual(H.sess_state(H.claude_sessions["quiet"], now), "idle")
        self.assertNotIn("busy_at", H.claude_sessions["ended"])   # after Stop: background shells don't count
        # the ps check is throttled to CLAUDE_BUSY_EVERY_S
        with mock.patch.object(H, "_tool_shell_parents", lambda: self.fail("ps again")):
            H.sess_keep_busy(now + 1)

    def test_tool_shell_parents_parses_ps(self):
        out = ("  31139 /bin/zsh -c source /Users/x/.claude/shell-snapshots/snapshot-zsh-1.sh && eval 'sleep 9'\n"
               "  31139 /Users/x/.local/bin/uv tool uvx mcp-server-fetch\n"
               "    1 /usr/sbin/cfprefsd agent\n")
        with mock.patch.object(H.subprocess, "run", lambda *a, **k: mock.Mock(stdout=out)):
            self.assertEqual(H._tool_shell_parents(), {31139})

    def test_reverse_follow_takes_any_session_terminal(self):
        self.assertEqual(H.front_agent("iTerm2|iTerm", {"claude": ("Warp", "iTerm")}), "claude")
        self.assertEqual(H.front_agent("Warp|Warp", {"claude": ("Warp", "iTerm")}), "claude")
        self.assertIsNone(H.front_agent("Safari|Safari", {"claude": ("Warp", "iTerm")}))
        # only terminals we can aim at a tab claim the seat: a session in a
        # VS Code terminal must not make every VS Code click switch seats
        now = time.time()
        H.claude_sessions["w"] = {"pid": 1, "tty": "/dev/ttys1", "ts": now,
                                  "warp": "warp://session/1", "bundle": "dev.warp.Warp-Stable"}
        H.claude_sessions["v"] = {"pid": 2, "tty": "/dev/ttys2", "ts": now,
                                  "bundle": "com.microsoft.VSCode"}
        with mock.patch.dict(H._bundle_names, {"dev.warp.Warp-Stable": "Warp",
                                               "com.microsoft.VSCode": "Visual Studio Code"}):
            self.assertEqual(H.sess_term_apps(), {"Warp"})

    def test_fake_row_never_routes_to_a_real_tab(self):
        now = time.time()
        H.claude_sessions["real-1"] = {"pid": 1, "tty": "/dev/ttys1", "ts": now}
        with mock.patch.dict(H._sess_fake, {"msg": {"t": "sess"}, "until": now + 30}), \
                mock.patch.object(H, "sess_cur", "real-1"):
            self.assertIsNone(H.sess_for_action("f0000001"))
            self.assertIsNone(H.sess_for_action())
        with mock.patch.object(H, "sess_cur", "real-1"):
            self.assertEqual(H.sess_for_action(), "real-1")

    def test_table_survives_a_restart_but_not_a_dead_process(self):
        now = time.time()
        d = tempfile.mkdtemp()
        with mock.patch.object(H, "SESS_FILE", Path(d) / "s.json"), \
                mock.patch.object(H, "_claude_pid_up", lambda pid: pid), \
                mock.patch.dict(H._sess_saved, {"sig": None, "at": 0.0}):
            H.claude_sessions["live"] = {"state": "idle", "ts": now, "pid": os.getpid(),
                                         "tty": "/dev/ttys1", "title": "T"}
            H.claude_sessions["dead"] = {"state": "idle", "ts": now, "pid": 999999,
                                         "tty": "/dev/ttys2"}
            H.claude_sessions["nopid"] = {"state": "idle", "ts": now}
            H.sess_save(now)
            H.claude_sessions.clear()
            H.sess_load()
            self.assertEqual(set(H.claude_sessions), {"live"})
            self.assertEqual(H.claude_sessions["live"]["title"], "T")

    def test_hook_command_sends_terminal_headers(self):
        sys.path.insert(0, str(HERE))
        import install_hooks
        c = install_hooks.cmd("PreToolUse")
        for h in ("X-AT-Pid: $PPID", "X-AT-Tty:", "X-AT-Warp: $WARP_FOCUS_URL", "X-AT-Bundle:"):
            self.assertIn(h, c)
        self.assertTrue(c.endswith("|| true"))
        self.assertIn("src=agentpet", c)


if __name__ == "__main__":
    unittest.main()
