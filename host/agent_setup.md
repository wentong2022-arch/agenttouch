# AgentTouch：让你（AI agent）把这台 Mac 上的 agent 配到桌宠上

> 这份说明是写给 AI agent 看的。用户把一句话贴给了你，意思是：「按这里的步骤，把这台 Mac 上装的 AI agent 配到 AgentTouch 桌宠上，做完告诉我结果」。
> 文末「本机现状」一节是 host 在你读取这一刻现场生成的，以它为准。

## 一、这是什么

- **AgentTouch** 是一块 ESP32 桌面小屏，用一张脸显示 AI agent 的工作状态：空闲 / 工作中 / 等你确认 / 完成。
- **host** 是这台 Mac 上的常驻服务，只监听本机 `http://127.0.0.1:8788`。它判断各家 agent 的状态，再经蓝牙或 Wi-Fi 推给板子。
- **板子上有五个固定席位**，名字和颜色写在固件里，改不了：

| 席位 id | 板上名字 | 默认对应 |
|---|---|---|
| `claude` | CLAUDE | Claude Code（命令行，状态来自 hooks） |
| `codex` | CODEX | Codex / ChatGPT.app（状态来自 notify + hooks.json） |
| `qoder` | QODER | Qoder IDE（国内版 `Qoder CN IDE`、国际版 `Qoder IDE`，读日志） |
| `qoderwork` | QWEN | 千问办公 QwenWork（读它的 agents.db） |
| `forest` | FOREST | Qoder 桌面版（国内版 `Qoder CN`、国际版 `Qoder`，读日志） |

- 每个席位有这些设置：切到这个席位时拉到前台的 **app 名**（`focus_apps`）、拉到前台后按的**聚焦键**（`focus_keys`）、板上点「批准 / 拒绝」时按的键（`approve_keys` / `reject_keys`），以及高级的**进程识别规则**（`proc_rules`）。

## 二、边界（必须遵守）

1. **只通过下面列出的 HTTP 接口改配置。** 不要直接编辑 `~/.agentpet/` 里的 `.py` 文件或仓库代码，不要碰固件，不要运行 `pet use`。
2. **不要重建或重签** `~/Applications/AgentPetHost.app`：一重签，蓝牙和辅助功能权限就会悄悄失效。
3. **系统设置里的权限**（蓝牙、辅助功能）只能请用户自己点，你做不了，也不要尝试用脚本改 TCC。
4. **不要替用户输入任何密码。** 板子的 Wi-Fi 密码请用户自己在设置页「板子 Wi-Fi」里填。
5. **会敲键盘的测试先问用户**（`/test/approve`、`/test/key/...`）：它们会往前台窗口里真的按键。
6. 五个席位之外的 agent（例如 QoderWork、Cursor、Claude 桌面 app），板子上暂时**没有位置**。如实告诉用户「识别到了，但暂时放不进席位」，不要占用别的席位去冒充，也不要自己写解析代码。

## 三、步骤

### 1. 看现状

```bash
curl -s http://127.0.0.1:8788/state          # host 版本、板子在线（ble/boards）、各席位此刻状态 agents
curl -s http://127.0.0.1:8788/agents/scan    # 本机扫到的 agent：席位、装没装、在不在跑、状态来源接好没有
curl -s http://127.0.0.1:8788/settings/data  # 当前全部设置（focus_apps、各种 keys、proc_rules、installed）
curl -s http://127.0.0.1:8788/fonts/status   # 板子 microSD 卡上的中文字库：state=ok 四个都在
```

`/agents/scan` 每一行的字段：`seat`（null = 暂无席位）、`build`（cn 国内版 / intl 国际版）、`where`（装在哪）、`running`、`how`（状态来源：hooks / codex / log / db / null=只能看开没开）、`wired`（状态来源接好没有）、`current`（是不是这个席位当前拉起的 app）。

### 2. 对照判断

- `wired:false` 且 `how` 是 `hooks` 或 `codex`：hooks 没装。在仓库目录里跑一次 `host/setup.sh`（仓库位置见文末「本机现状」；脚本可以重复跑，已经装好的项不会改动）。
- `wired:false` 且 `how` 是 `log` 或 `db`：请用户打开那个 app 用一次，日志或数据库会自己生成，不用你做任何事。
- 同一个席位的国内版和国际版都装了：问用户平时用哪个，再用第 3 步选定。
- 某个席位对应的 app 用户没装，或者不用：什么都不用做。没开的席位板子上本来就不显示。
- `/fonts/status` 的 `state`：`ok` 不用管；空或 `wait` = 板子还没经 Wi-Fi 连上，请用户在设置页「板子 Wi-Fi」配网（密码让用户自己填），连上后 host 会自动生成并推字库（约 5 分钟），你不用做什么；`nocard` = 请用户给板子插一张 microSD 卡（必备，黄历页很多字要从卡上读）；`nopil` = 缺 Pillow（只用来拿这台 Mac 的系统字体生成字库），**先问用户同意**，再运行 `/usr/bin/python3 -m pip install --user pillow==11.3.0`，然后 `curl -s http://127.0.0.1:8788/fonts/push`；`error` = 把 `why` 原样告诉用户。

### 3. 修改（只用这几个接口）

**指定某个席位拉起哪个 app**（合并写入，不影响其他席位）：
```bash
curl -s -X POST http://127.0.0.1:8788/agents/use -d '{"seat":"qoder","app":"Qoder IDE"}'
```
app 名就是 `/Applications` 里的名字去掉 `.app`。

**改按键和其他设置**：`POST /settings/save`，请求体是 JSON，只放要改的键。
```bash
curl -s -X POST http://127.0.0.1:8788/settings/save \
  -d '{"approve_keys":{"claude":"enter"},"reject_keys":{"claude":"esc"}}'
```
- 按键写法：`修饰键+…+键`，全部小写，不能有空格。修饰键只能用 `cmd` `shift` `alt` `option` `ctrl`；键只能用 `a`–`z`、`0`–`9`、`enter` `tab` `space` `esc`。例如 `cmd+enter`、`ctrl+shift+i`。空字符串 `""` 表示不按键。
- `approve_keys` / `reject_keys` / `focus_keys` 都是按席位的字典，只传要改的那几个席位即可。
- **`proc_rules` 要谨慎**：一保存，就会把已存的全部自定义规则整体替换掉。要改的话，先从 `/settings/data` 拿到完整的 `proc_rules`，只改其中一个席位，再把整份发回来。每条规则是 `{"match": 正则, "exclude": 正则或 null}`，匹配 `ps -axo command` 的每一行。等于默认值的规则不会被存下来。默认规则已经同时认 Qoder 的国内版和国际版，一般不需要改。
- 保存返回 `{"ok":false,"why":...}` 时，把原因原样告诉用户，不要硬试。

### 4. 自检

```bash
curl -s http://127.0.0.1:8788/agents/scan                  # 再看一遍：current / wired 是否如预期
curl -s http://127.0.0.1:8788/test/front                   # 此刻前台 app 被认成哪个席位（follow_cand）
curl -s http://127.0.0.1:8788/test/select/qoder            # 模拟在板上滑到 qoder：Mac 会把对应 app 拉到前台
curl -s http://127.0.0.1:8788/test/nudge                   # 板子弹出名牌和四个小灯 3 秒（请用户看板子）
```

- 请用户点进某个 agent 的窗口，停 1 秒：板子应该自动跟到那个席位。
- 请用户在某个 agent 里让它干点活：板子表情应该变成「工作中」，干完变「完成」。

### 5. 汇报给用户

用一张表列出每个席位：对应哪个 app、状态来源接好没有、自检结果（通过 / 没通过 / 需要你看板子）。再单独列三样：卡上字库状态，「识别到但暂无席位」的 agent，以及需要用户自己做的事（点系统权限、打开某个 app 用一次、插卡、配 Wi-Fi、看板子）。

## 四、出了问题

- `curl` 连不上 8788：host 没开。请用户在终端运行 `pet status`；显示已关，就运行 `pet on`。
- `/state` 里 `ble:false` 且 `boards:0`：板子没连上。可能是板子没开，或者正连着别的 Mac（蓝牙一次只能连一台）。
- host 日志：`tail -80 /tmp/agentpet_host.log`。
