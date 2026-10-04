# 排错

[English](04-troubleshooting.md) · 简体中文

常见问题和解决办法。先跑 `pet status`：它会告诉你 host 在不在、板子怎么连着。

## 连接

### 板子连不上（`ble=False`）

- 板子开着吗？按一下中键。
- `AgentPetHost.app` 有没有**蓝牙**权限？在 系统设置 → 隐私与安全性 → 蓝牙 里添加（列表里已有旧的 AgentPetHost 就先删掉），然后 `pet restart`。
- 是不是正跟着别的 Mac？跑 `pet claim`，再到板上点「连接」。
- 扣在桌上就是睡着了，翻过来。

### 系统蓝牙设置里看不到板子

正常。板子不和 macOS 配对，host 自己直接连。

### 蓝牙通了，Wi-Fi 不通（`boards: 0`）

- 在设置页「板子」一节加一个网络。板子只能看到 2.4 GHz 网络。
- 防火墙要放行 `/usr/bin/python3`：板子经 8737 端口连 Mac。重跑 `host/setup.sh`，看它的输出。

### 8788 没有响应

`pet status`，显示 OFF 就 `pet on`。日志在 `/tmp/agentpet_host.log`（`pet log`）。

## agent

### 批准或听写没反应，或者打进了别的窗口

- `AgentPetHost.app` 要有**辅助功能**权限，才能拉前台窗口、替你按键。
- `curl -s http://127.0.0.1:8788/test/front` 会显示 host 认为谁在前台、它会拉起哪个 app。在设置页「席位」里改对应的 app。
- 听写需要一个按住 fn 就能语音输入的输入法。
- 同时开几个 Claude Code 窗口时：`curl -s http://127.0.0.1:8788/test/sess/list` 列出 host 看到的会话，以及它怎么跳到每个标签页（`adapter`；`app` 表示只能拉起 app）。如果系统问「是否允许 `AgentPetHost` 控制终端 / iTerm2 / Ghostty」时你点了不允许，到「系统设置 → 隐私与安全性 → 自动化」里打开。hooks 要在重跑一次 `host/setup.sh` 之后才会带上终端信息。

### Claude Code 席位状态一直不变

可能缺 hooks。再跑一次 `host/setup.sh`，缺了它会补进 `~/.claude/settings.json`。

### Codex 从不显示「等你批准」

- Codex 会问一次是否信任新 hook：选信任。
- Codex 会自己审查低风险的请求并直接放行，不来问你，这类请求不会弹卡。

## 编译与烧录

### 编译时提示缺 Pillow

`host/setup.sh --pillow`，或 `/usr/bin/python3 -m pip install --user pillow==11.3.0`。

### 库下载很慢

在 `firmware/platformio.local.ini` 里把 `lib_deps` 指到本地 zip（写法见 `platformio.ini` 里的注释）。只覆盖 `lib_deps`。

### 烧录找不到串口

换一根能传数据的线。按住左键（BOOT）再插线，然后重新烧。

### 免线升级出了问题

`curl -s "http://127.0.0.1:8788/ota?rollback=1"` 回到上一版固件。板子完全起不来就用 USB 线烧。

## 板子上

### 字显示成方框

卡上字库缺了：没插卡、卡不是 FAT32，或者字库还在推。看设置页「总览 → 卡上字库」。

### 点它没反应

- 是不是睡着了（扣在桌上）？睡着时点屏幕特意不唤醒，翻过来或拿起来。
- 钉住的页面不跟着摆放换页。长按左键 1 秒解除。

### 死机了

按住中键：3 秒后固件关机；固件卡死时，大约 6 秒电源芯片也会强制断电。再按一下开机。

## 还是不行？

提一个 [issue](https://github.com/wentong2022-arch/agenttouch/issues)，附上 `pet status` 的输出和 `/tmp/agentpet_host.log` 的最后几行。
