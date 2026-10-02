# 安装

[English](02-install.md) · 简体中文

从一块白板到能用的桌宠：装 Mac 端、第一次烧固件、认主、配 Wi-Fi、卡上字库。全程约 20 分钟，大部分时间在等第一次编译。

想交给编程 agent 来装？让它照 [`host/agent_install.md`](../host/agent_install.md) 做，步骤和这里一样，需要你动手时它会停下来。

## 要准备什么

| 东西 | 说明 |
|---|---|
| 板子 | 微雪 [ESP32-S3-Touch-AMOLED-2.16](https://docs.waveshare.net/ESP32-S3-Touch-AMOLED-2.16) |
| microSD 卡 | **FAT32**（32 GB 以下的卡一般出厂就是）。字库、黄历、播客都放在卡上；exFAT 挂载不了。 |
| USB-C 数据线 | 只有第一次烧录要用。只能充电的线看不到串口。 |
| Mac | 装好 Xcode 命令行工具（`xcode-select --install`） |
| PlatformIO Core | `brew install platformio`，或 `pip3 install --user platformio` |
| Wi-Fi | 2.4 GHz。可选但推荐；板子看不到 5 GHz 网络。 |

## 1. 装 Mac 端（host）

```bash
git clone https://github.com/wentong2022-arch/agenttouch.git && cd agenttouch
host/setup.sh --check     # 可选：只报告会改什么
host/setup.sh             # 安装；可以重复跑
```

`setup.sh` 会装好：

- `/usr/bin/python3` 用的 Python 依赖
- `~/Applications/AgentPetHost.app`：一个很小的外壳，让 host 能用蓝牙
- 开机自启的 LaunchAgent 和 `pet` 命令
- Claude Code hooks 和 Codex hooks

它会问你装不装 **Pillow**，请装：Pillow 用这台 Mac 自带的系统字体生成中文字形，编译固件和卡上字库都要它。没有终端时（比如让 agent 来跑），用 `host/setup.sh --pillow`。

它还会让防火墙放行 `/usr/bin/python3`，板子才能经 Wi-Fi 连进来；这一步要输管理员密码。

最后它会列出几件只能你自己做的事：

1. 系统设置 → 隐私与安全性 → **蓝牙** → 添加 `~/Applications/AgentPetHost.app`。
2. 同一处 → **辅助功能** → 添加 `AgentPetHost.app`（拉前台窗口、替你按键要靠它）。
3. 如果你用 Codex：Codex 下次启动问是否信任新 hook 时，选信任。

在没有终端的环境里跑时它不会要密码，防火墙命令也会列进这份清单。

做完重启 host 并检查：

```bash
pet restart
pet status
```

## 2. 烧固件

用数据线插上板子，卡要插着。

```bash
cd firmware
pio run -e amoled216 -t upload
```

第一次编译会下载 ESP32 工具链和库，要几分钟。编译前会先用系统字体生成 `firmware/src/almanac_font.h`（约 1 秒）。这个文件不进 git，每台 Mac 自己生成。

找不到串口时，按住左键（BOOT）再插线，然后重试。

## 3. 让板子认这台 Mac

烧完板子会重启并通过蓝牙广播。host 找到它后，板子上会弹「连到这台 Mac？」，**点绿色的「连接」**。

- 错过了或点了拒绝：在 Mac 上跑 `pet claim`（或在设置页「板子」一节点「让板子连这台」），再到板上点一次「连接」。
- 一块板只跟一台 Mac。想换到另一台，在那台上同样操作，并在板上确认。

## 4. 给板子配 Wi-Fi

打开 <http://127.0.0.1:8788/settings> →「板子」。从板子搜到的网络里选一个，填密码。最多记 4 个，板子会连信号最强的那个。

- 只有蓝牙也能用：状态、批准、听写都走蓝牙。Wi-Fi 用来推字库、黄历、播客、语音播报和免线升级。
- 密码经蓝牙明文发给板子，只在添加网络时传一次。介意的话请在自己家里操作。

## 5. 卡上字库（自动）

固件里只带黄历最常用的字，其余的字都从卡上的四个字库文件读。

板子连上 Wi-Fi、插着卡时，host 会把缺的字库生成出来（约 10 秒）再推上卡（约 16 MB，5 分钟左右）。进度在设置页「总览 → 卡上字库」，或者：

```bash
curl -s http://127.0.0.1:8788/fonts/status
curl -s "http://127.0.0.1:8788/fonts/push?force=1"   # 重新生成并推全部四个
```

字形取自 macOS 自带的宋体、冬青黑体（Hiragino Sans GB）和 Menlo。想换字体，改 `host/gen_almanac_font.py` 顶部的三个路径。

## 6. 检查

```bash
pet status                                   # host 在跑、板子已连
curl -s http://127.0.0.1:8788/state          # "ble": true、"boards": 1、"sd": {...}
```

接下来：[把 agent 配到席位上](03-usage.zh-CN.md#席位)，再看看[手势](03-usage.zh-CN.md)。

## 更新

固件不用插线（host 经 Wi-Fi 把固件写上卡，板子刷到另一个 app 槽后重启，约 50 秒）：

```bash
cd firmware && pio run -e amoled216 && cp .pio/build/amoled216/firmware.bin ~/.agentpet/fw.bin
curl -s http://127.0.0.1:8788/ota
curl -s "http://127.0.0.1:8788/ota?rollback=1"   # 回到上一版
```

Mac 端：`git pull` 后再跑一次 `host/setup.sh`，它会把有变化的文件拷进 `~/.agentpet/` 并重启 host。

## 本地覆盖

两个永远不进 git 的文件：

- `firmware/platformio.local.ini`：库下载慢时把 `lib_deps` 指到本地 zip（写法见 `platformio.ini` 里的注释）。只覆盖 `lib_deps`，别动 `platform`。
- `firmware/src/config.local.h`：把某个 Wi-Fi 和你的 Mac 直接编进固件（`WIFI_SSID`、`WIFI_PASS`、`HOST_MDNS_NAME`、`HOST_FALLBACK_IP`）。一般用不到，Wi-Fi 在设置页配。改完先 `touch firmware/src/config.h`，否则不会重编。

## 关掉或卸载

```bash
pet off                                  # 停掉 host（下次登录还会自动起来）
/usr/bin/python3 host/install_hooks.py --remove   # 卸掉 Claude Code hooks
```

## 恢复出厂固件

微雪官方仓库的 `firmware/` 下有出厂整片镜像（`FactoryOnly-*.bin`），从地址 0 烧：

```bash
esptool --port /dev/cu.usbmodem* write-flash 0x0 ESP32-S3-Touch-AMOLED-2.16-FactoryOnly-260318.bin
```
