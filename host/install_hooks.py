#!/usr/bin/env python3
# Copyright (c) 2026 yuwentong
# Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
"""Install (or remove) AgentTouch hooks into ~/.claude/settings.json.

The hooks POST each Claude Code lifecycle event to the local AgentTouch host
(127.0.0.1:8788). `curl -m 2 ... || true` keeps hooks instant and harmless
when the host isn't running.

Usage:
  python3 install_hooks.py           # install / update
  python3 install_hooks.py --remove  # remove AgentTouch hooks
"""
import json
import shutil
import sys
import time
from pathlib import Path

SETTINGS = Path.home() / ".claude" / "settings.json"
MARK = "agentpet"   # identifies our hook commands
EVENTS = ["PreToolUse", "PostToolUse", "UserPromptSubmit", "Notification",
          "Stop", "SessionStart", "SessionEnd"]

def cmd(event):
    return (f"curl -s -m 2 -X POST --data-binary @- "
            f"'http://127.0.0.1:8788/hook/claude/{event}?src={MARK}' || true")

def main():
    remove = "--remove" in sys.argv
    data = {}
    if SETTINGS.exists():
        backup = SETTINGS.with_suffix(f".json.bak-{time.strftime('%Y%m%d%H%M%S')}")
        shutil.copy(SETTINGS, backup)
        print("backup:", backup)
        data = json.loads(SETTINGS.read_text() or "{}")

    hooks = data.setdefault("hooks", {})
    for ev in EVENTS:
        entries = hooks.get(ev, [])
        # drop any previous agentpet hooks
        for entry in entries:
            entry["hooks"] = [h for h in entry.get("hooks", [])
                              if MARK not in h.get("command", "")]
        entries = [e for e in entries if e.get("hooks")]
        if not remove:
            entries.append({"hooks": [{"type": "command", "command": cmd(ev)}]})
        if entries:
            hooks[ev] = entries
        else:
            hooks.pop(ev, None)
    if not hooks:
        data.pop("hooks", None)

    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(("removed from " if remove else "installed into ") + str(SETTINGS))

if __name__ == "__main__":
    main()
