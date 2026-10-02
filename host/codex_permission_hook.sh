#!/bin/sh
# Copyright (c) 2026 yuwentong
# Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
# AgentTouch: forward Codex PermissionRequest hook payloads to the host
# (needs_you signal on the board). Reads the JSON from stdin, ships it in the
# background, and prints nothing — empty stdout means the normal approval
# flow continues untouched. Debug copy kept while the link is being verified.
payload=$(cat)
printf '%s\n' "$payload" >> /tmp/codex_permission_hook.log
curl -s --max-time 2 -X POST -H 'Content-Type: application/json' \
  -d "$payload" 'http://127.0.0.1:8788/hook/codex/permission?src=agentpet' \
  >/dev/null 2>&1 &
exit 0
