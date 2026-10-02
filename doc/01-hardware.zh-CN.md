# 硬件

[English](01-hardware.md) · 简体中文

AgentTouch 跑在一块现成的开发板上：微雪 ESP32-S3-Touch-AMOLED-2.16。本页讲板上的器件、引脚表、三颗键，以及外壳四条边和屏幕显示内容的对应关系。

## 板子

一块 2.16 英寸方形 AMOLED 开发板，自带小外壳，板上有触摸、喇叭、两只麦克风、运动传感器、实时时钟、电池充电和 microSD 卡槽。AgentTouch 全都用上了，不用另接任何线。

| 部件 | 芯片 / 规格 | AgentTouch 用它做什么 |
|---|---|---|
| 主控 | ESP32-S3R8，双核 Xtensa LX7，240 MHz | 全部 |
| 内存 | 8 MB 八线 PSRAM（片内叠封）、16 MB QSPI flash | 帧缓冲、两个固件槽 |
| 无线 | 2.4 GHz Wi-Fi（b/g/n）、蓝牙 5 LE | 连 Mac |
| 屏幕 | 2.16 英寸 AMOLED，480 × 480，CO5300 驱动，QSPI | 表情和各页 |
| 触摸 | CST9220 电容触摸，I2C | 点击和滑动 |
| 喇叭 | ES8311 编解码、NS4150B 功放 | 提示音、说话、播客 |
| 麦克风 | ES7210 ADC，两只 MEMS 麦，相距约 39 mm | 用板麦听写 |
| 运动 | QMI8658 六轴 IMU | 哪条边朝下、拿起、摇晃、扣桌睡觉 |
| 时钟 | PCF85063 RTC | Mac 连上之前时间也是对的 |
| 电源 | AXP2101 PMU | 电池、充电、中键 |
| 存储 | microSD 卡槽 | 字库、黄历、播客、固件更新 |
| USB | USB-C，接 ESP32-S3 原生 USB | 第一次烧录、串口日志 |
| 电池 | MX1.25 两针接口，3.7 V 锂电池 | 不插线也能用 |

无线用板载贴片天线，另有 IPEX 座可接外置天线（切换要挪一颗电阻）。

## 引脚

固件里同一份表在 `firmware/src/pins.h`。

| 分组 | 信号 | GPIO | 备注 |
|---|---|---|---|
| 屏幕 | QSPI D0–D3 | 4, 5, 6, 7 | CO5300 |
| 屏幕 | QSPI CLK | 38 | |
| 屏幕 | CS | 12 | |
| 屏幕 | RESET | 39 | |
| 触摸 | INT | 11 | 下降沿 |
| 触摸 | RESET | 40 | |
| I2C | SDA | 15 | 共享总线，见下 |
| I2C | SCL | 14 | |
| 音频 | I2S MCLK | 42 | ES8311 与 ES7210 共用 |
| 音频 | I2S BCLK | 9 | 共用 |
| 音频 | I2S LRCK | 45 | 共用 |
| 音频 | I2S DOUT | 8 | ESP32 → ES8311（播放） |
| 音频 | I2S DIN | 10 | ES7210 → ESP32（麦克风） |
| 音频 | PA_CTRL | 46 | 喇叭功放使能，高电平开 |
| microSD | CLK / SCK | 2 | |
| microSD | CMD / MOSI | 1 | |
| microSD | D0 / MISO | 3 | |
| microSD | CS | 41 | 仅 SPI 模式 |
| 按键 | 左键（BOOT） | 0 | 低有效，启动配置脚 |
| 按键 | 右键（IO18） | 18 | 低有效，10 kΩ 上拉 |
| IMU | INT1 / INT2 | 17 / 21 | 未使用 |
| RTC | INT | 13 | 未使用 |
| 电源 | SYS_OUT | 16 | 不是按键线 |
| USB | D− / D+ | 19 / 20 | 原生 USB，勿他用 |
| UART0 | TX / RX | 43 / 44 | 下载与日志 |

中键没有 GPIO，它接在 AXP2101 的 PWRON 脚上（见[按键](#按键)）。

## I2C 总线

板上所有 I2C 芯片共用一条总线：SDA = GPIO15，SCL = GPIO14。

| 地址 | 芯片 | 作用 |
|---|---|---|
| 0x18 | ES8311 | 喇叭编解码 |
| 0x34 | AXP2101 | 电源管理 |
| 0x40–0x43 | ES7210 | 麦克风 ADC（地址由 AD0/AD1 脚电平决定，固件四个都探） |
| 0x51 | PCF85063 | 实时时钟 |
| 0x5A | CST9220 | 触摸 |
| 0x6B | QMI8658 | IMU |

往这条总线上加 I2C 器件，要避开这些地址。

## 按键

三颗键在上边，从左到右：

| 键 | 接线 | 在 AgentTouch 里 |
|---|---|---|
| 左键（BOOT） | GPIO0，低有效 | 短按：静音开 / 关。长按 1 秒：钉住 / 解除钉住屏上这条页带 |
| 中键（PWR） | AXP2101 PWRON | 关机时：开机。短按：报告现状 3 秒。长按 3 秒：关机 |
| 右键（IO18） | GPIO18，低有效 | 按住说话（听写） |

完整手势见[使用](03-usage.zh-CN.md)；钉住见[屏幕规则](06-screen-rules.zh-CN.md)。

- **中键**只能经 PMU 读到：固件打开 AXP2101 的 PWRON 按下 / 松开中断，经 I2C 轮询。按中键时 GPIO16（SYS_OUT）不会变。
- **6 秒断电**：PWRON 按住 6 秒，AXP2101 会自己切断电源，这块板上关不掉。所以中键永远不能做按住类功能。它顺带是死机时的急停；固件正常运行时，按到 3 秒就先干净关机了。
- **开机时的左键**：GPIO0 是启动配置脚，芯片复位时按住左键会进下载模式。

## 改固件须知

- 屏幕供电：屏的电源轨是 AXP2101 的 ALDO3。固件先起 PMU、打开 ALDO3，再碰屏。
- 旋转：屏用 MADCTL `0xA0` 旋转；触摸坐标做了 XY 互换加 X 镜像（`firmware/src/config.h` 里的 `TOUCH_*`）。
- 帧缓冲：整帧 480 × 480 RGB565（约 450 KB）是 PSRAM 里的画布，经 QSPI 刷屏。屏占用 SPI2。
- 音频：ES8311 与 ES7210 共用一个 I2S 口全双工，16 kHz，MCLK 4.096 MHz。ES7210 不开 TDM，所以左声道是 MIC1、右声道是 MIC2。它的第三路输入接喇叭输出做回声参考，第四路空着。
- microSD：固件先试原生 SDMMC 1 线模式，不行再退到 SPI3 上的 SD-SPI。有些卡只在原生模式下应答。卡必须是 FAT32。
- RTC：PCF85063 由 AXP2101 的 RTC LDO 供电，关机后这路不断（约 40 µA），所以关机不丢时间；只有电池没电或拔掉才丢。VBACKUP 只接了电容。芯片里存的是本地时间。
- IMU：只用加速度计（±4 g，125 Hz，每秒读 25 次）。陀螺仪和两根中断线都没用。
- 充电：固件设为 4.2 V 截止、500 mA 充电电流。
- Flash：16 MB，用现成的 `default_16MB.csv` 分区表。两个 app 槽让免线更新写进空闲槽，也能回滚。

## 链接

- 微雪文档：[English](https://docs.waveshare.com/ESP32-S3-Touch-AMOLED-2.16) · [中文](https://docs.waveshare.net/ESP32-S3-Touch-AMOLED-2.16)
- 微雪示例代码：[github.com/waveshareteam/ESP32-S3-Touch-AMOLED-2.16](https://github.com/waveshareteam/ESP32-S3-Touch-AMOLED-2.16)
- [原理图（PDF）](https://files.waveshare.com/wiki/ESP32-S3-Touch-AMOLED-2.16/ESP32-S3-Touch-AMOLED-2.16-Schematic.pdf)

## 外壳四边与传感器轴向

正对屏幕、三颗键在上时：

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

| 边 | 上面有什么 |
|---|---|
| 上边 | 三颗键：BOOT、PWR、IO18 |
| 下边 | USB-C |
| 左侧边 | microSD 卡槽，MIC2 孔（靠上角） |
| 右侧边 | 喇叭格栅（下半段），MIC1 孔（靠上角） |

**IMU 轴向**（按板上丝印）：+X 指向右侧边，+Y 指向上边（三颗键），+Z 垂直屏幕朝外。静止时，哪根轴朝上，加速度计就在那根轴上读到约 +1 g。

### 摆放 → 页面

| 摆放 | 加速度计 | sector | 显示 |
|---|---|---|---|
| 立着，三键朝上 | +Y 朝上 | 0 | 脸页（左右滑 = 切席位） |
| 左侧边（microSD）朝下 | +X 朝上 | 3 | 钟表带：钟表；左滑 = Mac 正在播放，右滑 = 板上播客 |
| 右侧边（喇叭）朝下 | −X 朝上 | 1 | 黄历带：黄历和日历，左右滑都在两页间切换 |
| 三键朝下 | −Y 朝上 | 2 | 不用：保持上一页 |
| 平放，屏朝上 | +Z 朝上 | — | 保持上一页 |
| 屏朝下 | −Z 朝上 | — | 睡觉 |

- 新摆放要重力稳定 0.7 秒才算（拿起后 0.9 秒）；扣桌要 0.8 秒。拿在手里时显示什么，见[屏幕规则](06-screen-rules.zh-CN.md)。
- 英文界面下，黄历带先落在日历页。
- 映射在 `PAGES_BY_ORIENT`（`firmware/src/config.h`）和 `imuPoll()`（`firmware/src/main.cpp`）。

### 为什么这样分，以及用哪只麦

侧边朝下时，那条边上的麦孔会被桌面盖住；右侧边朝下时喇叭也对着桌面。所以要放播客的钟表带放在左侧边朝下，喇叭和 MIC1 都朝上。黄历只在点一下时念几秒签。

听写用板麦时（设置页「语音听写」），用哪只麦按摆放的 sector 选，不看实时重力：

| 摆放 | 麦 |
|---|---|
| 右侧边朝下（sector 1，黄历带） | MIC2，在左侧边 |
| 其他摆放 | MIC1，在右侧边 |
