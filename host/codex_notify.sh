#!/bin/sh
# Copyright (c) 2026 yuwentong
# Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
# AgentTouch codex notify wrapper.
# Codex runs `notify` from ~/.codex/config.toml at each turn end and passes the
# JSON payload as the last argument; this copies it to the AgentTouch host
# (turn-complete -> done). Deploy copy lives in ~/.agentpet/ (setup.sh).
#
# Two ways it gets wired, setup.sh accepts either:
#  - ChatGPT.app owns `notify` (SkyComputerUseClient) and chains us through its
#    own `--previous-notify` -- nothing to forward, Sky already ran.
#  - We own `notify`: setup.sh saved whatever was there before in
#    ~/.agentpet/codex_notify_orig (one argv element per line) and we run it
#    first, so the original handler keeps working.

# NB: not "${1:-{}}" — POSIX sh ends that expansion at the first `}`, so a
# non-empty $1 gains a trailing brace and the host rejects the JSON.
for payload; do :; done
[ -n "$payload" ] || payload='{}'

ORIG="$HOME/.agentpet/codex_notify_orig"
if [ -s "$ORIG" ]; then
  # original handlers can linger for minutes -- background, never wait
  (
    set --
    while IFS= read -r a; do set -- "$@" "$a"; done < "$ORIG"
    exec "$@" "$payload"
  ) >/dev/null 2>&1 &
fi

curl -s -m 2 -X POST -H 'Content-Type: application/json' \
     --data "$payload" http://127.0.0.1:8788/hook/codex/notify >/dev/null 2>&1
exit 0
