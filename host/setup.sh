#!/bin/bash
# Copyright (c) 2026 yuwentong
# Licensed under PolyForm Noncommercial 1.0.0 (see LICENSE)
# setup.sh -- install the AgentTouch (S3) host on this Mac, one command.
#
#   host/setup.sh                    install / repair; safe to re-run
#   host/setup.sh --import FILE      also take preferences from another Mac's
#                                    ~/.agentpet/config.json (the only file worth moving)
#   host/setup.sh --check            report only, change nothing
#   host/setup.sh --rebuild-app      recompile + re-sign AgentPetHost.app even if it exists
#                                    (re-signing voids the Bluetooth grant: re-add it afterwards)
#   host/setup.sh --pillow           install Pillow without asking (the user already said yes;
#                                    for runs without a terminal, e.g. by an AI agent)
#
# Re-running on an already set-up Mac changes nothing that works: the app shell
# is kept (a new signature would silently drop the Bluetooth
# permission), hooks and codex entries are only written when missing, files are
# copied only when they differ.
#
# What it cannot do (macOS keeps these for the user) is printed at the end:
# Bluetooth + Accessibility permission for AgentPetHost.app, trusting the codex hook,
# and -- when run without a terminal, where sudo cannot ask for a password --
# the firewall commands.

set -u
REPO=$(cd "$(dirname "$0")/.." && pwd)
SRC="$REPO/host"
DEPLOY="$HOME/.agentpet"
APP="$HOME/Applications/AgentPetHost.app"
LABEL=com.agentpet.host
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
PY=/usr/bin/python3                     # the only python the firewall lets in (CLAUDE.md 铁律)
PIP_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple
# pyobjc 11.1, not 12: 12.0 was yanked for claiming Python 3.9 support, so on
# /usr/bin/python3 (3.9) pip falls back to its source tarball, which a newer
# clang refuses to build (second Mac, 2026-09-26). 11.1 has 3.9 wheels for
# arm64 and x86_64; host tests pass on it. --only-binary keeps pip from ever
# compiling: a missing wheel fails fast instead of 16 errors deep.
PIP_PKGS="bleak==1.1.1 pyobjc-core==11.1 pyobjc-framework-Quartz==11.1 pyobjc-framework-Cocoa==11.1 pyobjc-framework-CoreBluetooth==11.1 pyobjc-framework-libdispatch==11.1 edge-tts==7.2.8"

CHECK=0; IMPORT=""; REBUILD=0; PILLOW=0
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1 ;;
    --import) IMPORT="${2:-}"; shift ;;
    --rebuild-app) REBUILD=1 ;;
    --pillow) PILLOW=1 ;;
    -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
    *) echo "setup: 不认识的参数 $1（--help 看用法）"; exit 1 ;;
  esac
  shift
done

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
did()  { printf '  \033[36m+\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
bad()  { printf '  \033[31m✗\033[0m %s\n' "$*"; }
step() { printf '\n\033[1m%s\033[0m\n' "$*"; }
# run a change, or just say it in --check mode
doit() { if [ $CHECK = 1 ]; then warn "（--check）会做：$1"; return 1; fi; return 0; }

MANUAL=()      # things only the user can do, printed at the end
FAIL=0

# ---------------------------------------------------------------------------
step "1. 基础工具"
if ! xcode-select -p >/dev/null 2>&1 || [ ! -x "$PY" ] || ! "$PY" -c 1 >/dev/null 2>&1; then
  bad "没有 Xcode 命令行工具（/usr/bin/python3、clang 都靠它）"
  if doit "弹出安装窗口"; then xcode-select --install 2>/dev/null; fi
  echo "  装完再跑一次 setup.sh"; exit 1
fi
ok "命令行工具 $(xcode-select -p)"
ok "python $("$PY" -c 'import sys;print("%d.%d.%d"%sys.version_info[:3])')（${PY}）"
ok "macOS $(sw_vers -productVersion)"

# ---------------------------------------------------------------------------
step "2. Python 依赖（装在 $PY 的 --user 里）"
missing=$("$PY" - <<'EOF'
import importlib.util as u
need = {"bleak": "bleak", "Quartz": "pyobjc-framework-Quartz", "AppKit": "pyobjc-framework-Cocoa",
        "edge_tts": "edge-tts"}
print(" ".join(p for m, p in need.items() if u.find_spec(m) is None))
EOF
)
if [ -z "$missing" ]; then
  ok "bleak / pyobjc / edge-tts 都在"
else
  warn "缺：$missing"
  if doit "pip install --user $PIP_PKGS"; then
    echo "  （过程记在 /tmp/agentpet_setup_pip.log）"
    if "$PY" -m pip install --user -q --disable-pip-version-check --no-warn-script-location --only-binary=:all: \
         -i "$PIP_INDEX" $PIP_PKGS >/tmp/agentpet_setup_pip.log 2>&1; then did "已安装"
    else bad "pip 失败（网络？换源或挂代理再试；tail /tmp/agentpet_setup_pip.log）"; FAIL=1; fi
  fi
fi
# Pillow does one job: bake the Chinese fonts from this Mac's own system fonts
# (the firmware's table before the first build, the card's full set when the
# board first joins over Wi-Fi) -- no font data ships with the code. Asked,
# not assumed; 11.3.0 is the last Pillow with wheels for Python 3.9.
PIL_PKG="pillow==11.3.0"
if "$PY" -c 'import PIL' 2>/dev/null; then ok "Pillow（生成中文字库用）"
else
  warn "缺 Pillow：只用来拿这台 Mac 的系统字体生成中文字库（编译固件、往卡上推字库都要它）"
  ans=n
  if [ $CHECK = 0 ]; then
    if [ $PILLOW = 1 ]; then ans=y          # --pillow: the user already agreed
    elif [ -t 0 ]; then read -r -p "  现在装 Pillow 吗？[Y/n] " ans; ans=${ans:-y}; fi
  fi
  case "$ans" in
    y|Y|yes|YES)
      if "$PY" -m pip install --user -q --disable-pip-version-check --no-warn-script-location --only-binary=:all: \
           -i "$PIP_INDEX" $PIL_PKG >>/tmp/agentpet_setup_pip.log 2>&1; then did "Pillow 已安装"
      else bad "Pillow 没装上（tail /tmp/agentpet_setup_pip.log）"; FAIL=1; fi ;;
    *) MANUAL+=("装 Pillow（生成中文字库要它，编译固件前必须装）：$PY -m pip install --user $PIL_PKG（或重跑 host/setup.sh --pillow）") ;;
  esac
fi

# ---------------------------------------------------------------------------
step "3. host 文件 → $DEPLOY"
[ $CHECK = 1 ] || mkdir -p "$DEPLOY"
for f in agentpet_host.py almanac_bank.json agent_setup.md gen_almanac_font.py pet codex_notify.sh codex_permission_hook.sh; do
  if cmp -s "$SRC/$f" "$DEPLOY/$f"; then ok "$f 一致"
  elif doit "拷 $f"; then
    cp "$SRC/$f" "$DEPLOY/$f.tmp" && mv -f "$DEPLOY/$f.tmp" "$DEPLOY/$f" && did "$f"
    CHANGED_HOST=1
  fi
done
[ $CHECK = 1 ] || chmod +x "$DEPLOY/pet" "$DEPLOY/codex_notify.sh" "$DEPLOY/codex_permission_hook.sh"
# /agent-setup reports where this repo is (and `pet use s3` on a dev machine)
if ! grep -qx "s3=$REPO" "$DEPLOY/repos" 2>/dev/null && doit "记下仓库位置"; then
  { grep -v '^s3=' "$DEPLOY/repos" 2>/dev/null; echo "s3=$REPO"; } > "$DEPLOY/repos.tmp" && mv -f "$DEPLOY/repos.tmp" "$DEPLOY/repos"
  did "repos: s3=$REPO"
fi
if [ -s "$DEPLOY/host_id" ]; then ok "本机身份 host_id $(cut -c1-8 "$DEPLOY/host_id")…"
elif doit "生成 host_id"; then uuidgen | tr 'A-Z' 'a-z' > "$DEPLOY/host_id"; did "host_id $(cut -c1-8 "$DEPLOY/host_id")…"; fi

if [ -n "$IMPORT" ]; then
  if [ ! -f "$IMPORT" ]; then bad "--import 找不到 $IMPORT"; FAIL=1
  elif ! "$PY" -c 'import json,sys;json.load(open(sys.argv[1]))' "$IMPORT" 2>/dev/null; then bad "$IMPORT 不是合法 JSON"; FAIL=1
  elif cmp -s "$IMPORT" "$DEPLOY/config.json"; then ok "config.json 已是导入的那份"
  elif doit "导入偏好 $IMPORT"; then
    [ -f "$DEPLOY/config.json" ] && cp "$DEPLOY/config.json" "$DEPLOY/config.json.bak-$(date +%Y%m%d%H%M%S)"
    cp "$IMPORT" "$DEPLOY/config.json"; did "偏好已导入（focus_apps 是按旧 Mac 的 app 名写的，不对就去设置页「席位」改）"
    CHANGED_HOST=1
  fi
fi

# ---------------------------------------------------------------------------
step "4. 外壳 AgentPetHost.app（launchd 下拿蓝牙权限的唯一办法）"
have_app=0
[ -x "$APP/Contents/MacOS/agentpet-host" ] && grep -q NSBluetoothAlwaysUsageDescription "$APP/Contents/Info.plist" 2>/dev/null && have_app=1
if [ $have_app = 1 ] && [ $REBUILD = 0 ]; then
  ok "已存在，保留（重签会让蓝牙授权失效；要重建用 --rebuild-app）"
elif doit "编译 agentpet_wrap.c、组装并签名 $APP"; then
  tmp=$(mktemp -d)
  if clang -O2 -o "$tmp/agentpet-host" "$SRC/agentpet_wrap.c"; then
    mkdir -p "$APP/Contents/MacOS"
    mv -f "$tmp/agentpet-host" "$APP/Contents/MacOS/agentpet-host"
    cat > "$APP/Contents/Info.plist" <<'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>com.agentpet.host</string>
  <key>CFBundleName</key><string>AgentPetHost</string>
  <key>CFBundleExecutable</key><string>agentpet-host</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSUIElement</key><true/>
  <key>NSBluetoothAlwaysUsageDescription</key>
  <string>AgentTouch 桌宠通过蓝牙与 ESP32 板子通信</string>
</dict>
</plist>
EOF
    if codesign --force -s - "$APP" 2>/dev/null; then did "已编译并 ad-hoc 签名"
    else bad "codesign 失败"; FAIL=1; fi
    NEW_APP=1
  else
    bad "clang 编译失败"; FAIL=1
  fi
  rm -rf "$tmp"
fi
# Both grants belong to the shell (bundle id com.agentpet.host; python inherits
# them), verified on the dev Mac 2026-09-24. The TCC databases are readable only
# when the terminal has Full Disk Access -- if not, just ask the user to look.
tcc() {  # tcc <db> <service> -> 2 granted / 0 denied / "" none / "?" unreadable
  sqlite3 "$1" "select auth_value from access where service='$2' and client='com.agentpet.host'" 2>/dev/null || echo "?"
}
BT=$(tcc "$HOME/Library/Application Support/com.apple.TCC/TCC.db" kTCCServiceBluetoothAlways)
AX=$(tcc "/Library/Application Support/com.apple.TCC/TCC.db" kTCCServiceAccessibility)
if [ "${NEW_APP:-0}" = 0 ] && [ "$BT" = 2 ]; then ok "蓝牙权限已给 AgentPetHost"
else MANUAL+=("系统设置 → 隐私与安全性 → 蓝牙：点 + 添加 ${APP}（列表里已有 AgentPetHost 就先 - 删掉再加）"); fi
if [ "${NEW_APP:-0}" = 0 ] && [ "$AX" = 2 ]; then ok "辅助功能权限已给 AgentPetHost（批准/回车敲键靠它）"
else MANUAL+=("系统设置 → 隐私与安全性 → 辅助功能：点 + 添加 ${APP} 并打开（板上点「批准」、听写后回车要靠它敲键；已有就先删再加）"); fi

# ---------------------------------------------------------------------------
step "5. 开机自启 LaunchAgent"
want=$(cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>KeepAlive</key>
	<true/>
	<key>Label</key>
	<string>$LABEL</string>
	<key>ProgramArguments</key>
	<array>
		<string>$APP/Contents/MacOS/agentpet-host</string>
	</array>
	<key>RunAtLoad</key>
	<true/>
	<key>StandardErrorPath</key>
	<string>/tmp/agentpet_host.log</string>
	<key>StandardOutPath</key>
	<string>/tmp/agentpet_host.log</string>
</dict>
</plist>
EOF
)
# compare what launchd would run, not formatting (the dev Mac's plist was written by hand)
cur=$(/usr/libexec/PlistBuddy -c 'Print :ProgramArguments:0' "$PLIST" 2>/dev/null)
if [ "$cur" = "$APP/Contents/MacOS/agentpet-host" ]; then
  ok "$PLIST 指向外壳"
elif doit "写 $PLIST"; then
  mkdir -p "$(dirname "$PLIST")"
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null
  printf '%s\n' "$want" > "$PLIST"; did "已写"
  RELOAD=1
fi

# ---------------------------------------------------------------------------
step "6. pet 命令"
if [ -n "${AGENTPET_BIN_DIR:-}" ]; then BIN=$AGENTPET_BIN_DIR       # tests / unusual layouts
elif [ -L /opt/homebrew/bin/pet ] || { [ -d /opt/homebrew/bin ] && [ -w /opt/homebrew/bin ]; }; then BIN=/opt/homebrew/bin
elif [ -L /usr/local/bin/pet ] || { [ -d /usr/local/bin ] && [ -w /usr/local/bin ]; }; then BIN=/usr/local/bin
else BIN="$HOME/.local/bin"; fi
if [ "$(readlink "$BIN/pet" 2>/dev/null)" = "$DEPLOY/pet" ]; then
  ok "pet → $DEPLOY/pet"
elif doit "软链 $BIN/pet"; then
  mkdir -p "$BIN" && ln -sf "$DEPLOY/pet" "$BIN/pet" && did "$BIN/pet"
fi
case ":$PATH:" in *":$BIN:"*) ;; *) warn "$BIN 不在 PATH 里：在 ~/.zshrc 加一行 export PATH=\"$BIN:\$PATH\"" ;; esac

# ---------------------------------------------------------------------------
step "7. Claude Code hooks"
n=$(grep -o 'src=agentpet' "$HOME/.claude/settings.json" 2>/dev/null | wc -l | tr -d ' ')
# older hooks lack the session headers (several Claude Code windows): re-run
if [ "$n" -ge 7 ] && grep -q 'X-AT-Pid' "$HOME/.claude/settings.json"; then ok "~/.claude/settings.json 已有 $n 条"
elif doit "install_hooks.py"; then "$PY" "$SRC/install_hooks.py" | sed 's/^/    /'; did "已装"; fi

# ---------------------------------------------------------------------------
step "8. Codex（ChatGPT.app）"
if [ ! -d "$HOME/.codex" ] && [ ! -d /Applications/ChatGPT.app ] && ! command -v codex >/dev/null; then
  ok "本机没有 Codex，跳过"
else
  mkdir -p "$HOME/.codex" 2>/dev/null
  # 8a. PermissionRequest hook (approval -> needs_you); merge, keep other hooks
  r=$("$PY" - "$HOME/.codex/hooks.json" "$DEPLOY/codex_permission_hook.sh" "$CHECK" <<'EOF'
import json, sys, os, shutil, time
path, cmd, check = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
d = {}
if os.path.exists(path):
    d = json.load(open(path))
ents = d.setdefault("hooks", {}).setdefault("PermissionRequest", [])
if any("codex_permission_hook.sh" in h.get("command", "") for e in ents for h in e.get("hooks", [])):
    print("have"); sys.exit()
if check:
    print("would"); sys.exit()
if os.path.exists(path):
    shutil.copy(path, path + ".bak-" + time.strftime("%Y%m%d%H%M%S"))
ents.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5,
                        "statusMessage": "AgentTouch needs_you"}]})
json.dump(d, open(path, "w"), indent=2, ensure_ascii=False)
print("did")
EOF
)
  case "$r" in
    have) ok "hooks.json 已有 PermissionRequest" ;;
    would) warn "（--check）会做：写 hooks.json" ;;
    did) did "hooks.json 已加 PermissionRequest"
         MANUAL+=("Codex 下次启动会问是否信任新 hook（PermissionRequest → codex_permission_hook.sh）：选信任") ;;
    *) bad "hooks.json 处理失败：$r"; FAIL=1 ;;
  esac
  # 8b. notify (turn end -> done). Accept either wiring, see codex_notify.sh
  r=$("$PY" - "$HOME/.codex/config.toml" "$DEPLOY/codex_notify.sh" "$DEPLOY/codex_notify_orig" "$CHECK" <<'EOF'
import json, sys, os, re, shutil, time
path, ours, orig_file, check = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "1"
text = open(path).read() if os.path.exists(path) else ""
lines = text.splitlines(keepends=True)
# notify must be a top-level key: before the first [table]
top_end = next((i for i, l in enumerate(lines) if l.lstrip().startswith("[")), len(lines))
idx = next((i for i in range(top_end) if re.match(r"\s*notify\s*=", lines[i])), None)
if idx is not None and "codex_notify.sh" in lines[idx]:
    print("have"); sys.exit()
if check:
    print("would"); sys.exit()
if os.path.exists(path):
    shutil.copy(path, path + ".bak-" + time.strftime("%Y%m%d%H%M%S"))
new = "notify = " + json.dumps([ours]) + "\n"
if idx is not None:
    try:   # TOML basic-string arrays are JSON; anything fancier we refuse to guess at
        prev = json.loads(lines[idx].split("=", 1)[1].strip())
    except ValueError:
        print("unparsed"); sys.exit()
    open(orig_file, "w").write("".join(str(a) + "\n" for a in prev))
    lines[idx] = new
    print("replaced")
else:
    lines.insert(0, new)
    print("did")
open(path, "w").write("".join(lines))
EOF
)
  case "$r" in
    have) ok "config.toml 的 notify 已接到 codex_notify.sh" ;;
    would) warn "（--check）会做：config.toml 的 notify 接 codex_notify.sh" ;;
    did) did "config.toml 加了 notify" ;;
    replaced) did "config.toml 的 notify 换成 codex_notify.sh（原来的存在 codex_notify_orig，照样会先跑）" ;;
    unparsed) warn "config.toml 的 notify 格式看不懂，没动它；手动把它改成 notify = [\"$DEPLOY/codex_notify.sh\"]" ;;
    *) bad "config.toml 处理失败：$r"; FAIL=1 ;;
  esac
fi

# ---------------------------------------------------------------------------
step "9. 可选件（缺了只是那项功能不亮）"
if command -v media-control >/dev/null 2>&1 || [ -x /opt/homebrew/bin/media-control ]; then ok "media-control（正在播放页）"
elif command -v brew >/dev/null 2>&1; then
  if doit "brew install media-control"; then
    brew tap ungive/media-control >/dev/null 2>&1; brew install media-control >/dev/null 2>&1 \
      && did "media-control" || warn "media-control 没装上（正在播放页不会亮）：brew tap ungive/media-control && brew install media-control"
  fi
else warn "没有 Homebrew：正在播放页不会亮（要的话装 brew 后 brew tap ungive/media-control && brew install media-control）"; fi

if system_profiler SPAudioDataType 2>/dev/null | grep -q 'BlackHole 2ch'; then ok "BlackHole 2ch（板麦听写）"
else warn "没有 BlackHole 2ch：板麦听写不可用，右键听写用 Mac 麦。要的话 brew install --cask blackhole-2ch（会要开机密码）"; fi
if [ -x "$DEPLOY/mic_sink" ] && [ "$DEPLOY/mic_sink" -nt "$SRC/mic_sink.swift" ]; then ok "mic_sink"
elif doit "swiftc mic_sink"; then
  swiftc -O -o "$DEPLOY/mic_sink" "$SRC/mic_sink.swift" 2>/dev/null && did "mic_sink 已编译" || warn "mic_sink 编译失败（只影响板麦听写）"
fi

# ---------------------------------------------------------------------------
step "10. 防火墙（板子要从 Wi-Fi 连进 8737）"
FW=/usr/libexec/ApplicationFirewall/socketfilterfw
if ! $FW --getglobalstate 2>/dev/null | grep -q 'enabled'; then ok "防火墙没开"
else
  real="$("$PY" -c 'import sys;print(sys.prefix)')/Resources/Python.app"
  apps=$($FW --listapps 2>/dev/null)
  need=()
  for p in /usr/bin/python3 "$real"; do
    [ -e "$p" ] || continue
    echo "$apps" | grep -qF "$p" && ok "已放行 $p" || need+=("$p")
  done
  if [ ${#need[@]} -gt 0 ]; then
    if ! [ -t 0 ]; then
      # no terminal (an AI agent's shell): sudo cannot ask for the password, so
      # hand the exact commands to the user instead of failing a good install
      warn "没有终端，输不了管理员密码：放行留到最后给你手动做"
      for p in "${need[@]}"; do
        MANUAL+=("防火墙放行 $p（板子要从 Wi-Fi 连进 8737；要管理员密码，请在终端里自己运行）：sudo $FW --add \"$p\" && sudo $FW --unblockapp \"$p\"")
      done
    elif doit "sudo 放行 ${need[*]}"; then
      echo "  放行要管理员密码："
      for p in "${need[@]}"; do sudo $FW --add "$p" >/dev/null && sudo $FW --unblockapp "$p" >/dev/null && did "已放行 $p" || { bad "没放行 $p"; FAIL=1; }; done
    fi
  fi
fi

# ---------------------------------------------------------------------------
step "11. 启动"
if [ $CHECK = 1 ]; then :
elif ! launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || [ "${RELOAD:-0}" = 1 ]; then
  launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null && did "host 已启动"
elif [ "${CHANGED_HOST:-0}" = 1 ] || [ "${NEW_APP:-0}" = 1 ]; then
  launchctl kickstart -k "$DOMAIN/$LABEL" && did "host 已重启（文件有更新）"
else ok "host 在跑，没改东西就不重启"; fi
if [ $CHECK = 0 ]; then
  for i in 1 2 3 4 5 6 7 8 9 10; do curl -s -m 1 http://127.0.0.1:8788/state >/dev/null && break; sleep 1; done
fi
st=$(curl -s -m 2 http://127.0.0.1:8788/state)
if [ -n "$st" ]; then
  echo "$st" | "$PY" -c '
import json,sys
d=json.load(sys.stdin)
print("  \033[32m✓\033[0m 8788 在答：product=%s host=%s  ble=%s  tcp=%s" % (d.get("product"), d.get("host_version"), d.get("ble"), d.get("boards")))'
else bad "8788 没回应：pet log 看日志"; FAIL=1; fi

# ---------------------------------------------------------------------------
if [ ${#MANUAL[@]} -gt 0 ]; then
  step "还要你手动做（macOS 不让脚本代劳）"
  i=1; for m in "${MANUAL[@]}"; do echo "  $i. $m"; i=$((i+1)); done
  echo "  做完：pet restart，再 pet status 看 ble=True（板子要在旁边、且没被别的 Mac 连着）"
fi
echo
[ $FAIL = 0 ] && echo "setup: 完成" || { echo "setup: 有失败项（上面 ✗）"; exit 1; }
