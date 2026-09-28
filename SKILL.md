---
name: keysight-scope-i2c
description: 通过 SCPI（TCP 5025）远程操作 Keysight/Agilent InfiniiVision 示波器（DSO-X 6000X/3000T/2000X 等 WinCE 机型），完成 I2C 总线时序测量的端到端工作流——定位目标窗口、1ns 高分辨率取数与双通道拼接、I2C 事务解码（START/地址/ACK/STOP）、13 个时序参数（含 tBUF 总线空闲）计算与规格判定、Tr/Tf 按 30%-70% 规范口径逐沿测量、游标闭环复核、示波器截图，最后把结果回填进带 OLE 嵌入对象的 Excel 测试报告。支持标准/快速/SMBus(PMBus)三档规格（电源兼容性测试场景）。当用户要测 I2C 时序（fSCL/tHIGH/tLOW/tSU;STA/tHD;STA/tSU;STO/tSU;DAT/tHD;DAT/tBUF/Tr/Tf）、算上升下降时间、判 PASS/FAIL、排查 Tr 超规格，或要求把实测值写回测试报告表格时使用。也用于**验收别人（同事或别的 Agent）生成的 I2C 测试代码是否可信** —— 用合成已知答案波形对拍，不需要真机，5 分钟出结论（含五条红线：fSCL 差 2 倍、Tr/Tf 漏测、桩函数报 PASS、记录起点翻转 tHIGH/tLOW、数据不足仍判 PASS）。内含两条最贵的教训：Tr/Tf 必须用 30%-70% 口径（用 10%-90% 会虚高 2-3 倍、把合格判成超规格），以及含 OLE 嵌入对象的 xlsx 绝不能用本地 Office 编辑器或 openpyxl 保存（会静默摧毁嵌入包）。提效手段：一键自动配置示波器（Auto Scale + I2C 触发）、多器件批量回填（batch）、参数化流水线（i2c_full_test.py）。
agent_created: true
version: "1.0.0"
---

# Keysight 示波器 I2C 时序测量（端到端）

从「示波器屏幕上有一屏波形」到「测试报告表格里填好实测值」的完整闭环。
适用于电源/主板兼容性测试里 I2C 总线的时序取证与报告回填。

## 触发场景

- 「测 I2C 时序」「抓 SDA/SCL」「I2C 兼容性」「tSU;STA 多少」「Tr 是多少」
- 「对照规格判定」PASS/FAIL、排查 Tr 超规格
- 「把实测值回填到报告里」「填进时序测试表」
- 给了一个示波器 IP + 要测的器件地址（如 0x58 / 0x59 / 0x5A）

## 一次性准备

| 项 | 说明 |
|---|---|
| 连接 | `socket` 直连 TCP **5025**（SCPI raw socket，不需要 VISA/IVI 驱动） |
| Python | 3.9+，需 `numpy`；OCR 校验需 `rapidocr-onnxruntime`（自带模型，不需系统 tesseract） |
| 仪器 | Keysight/Agilent InfiniiVision 6000X / 3000T / 2000X 等同指令集机型 |
| 通道约定 | **CH1 = SCL，CH2 = SDA**（可改，脚本里是参数） |

先跑自检确认环境正常：

```bash
python scripts/selftest.py          # 纯函数自测，33 项。报"不合格"之前必须先跑这个
python scripts/scope.py <IP>        # 只读摸底：IDN、时基、通道、采样率（不改任何设置）
```

**快速启动（推荐）**：根目录的 `run_i2c_test.py` 是统一入口，自动把 `scripts/` 加入 `sys.path`，不需要 cd。所有连真机的子命令都要求显式传 `--ip`（不再有隐式默认值）：

```bash
python run_i2c_test.py --help                    # 列出全部子命令

# 判定前必跑（不连真机）
python run_i2c_test.py selftest                  # 纯函数自测 33 项
python run_i2c_test.py sandbox                   # 沙箱端到端（不连真机）
python run_i2c_test.py full                      # 全量低速覆盖报告（不连真机）

# 连真机
python run_i2c_test.py connect --ip <IP>         # 连接示波器，显示状态
python run_i2c_test.py check --ip <IP>           # 信号摸底：峰峰值、频率
python run_i2c_test.py test --ip <IP> --addr 0x5B --spec std  # 完整流水线
python run_i2c_test.py batch --ip <IP> --addr 0x58,0x59,0x5A  # 多地址批量（逗号分隔）
```

## 规范口径 ⚠️ 读这一节能省你两小时

**I²C 的 Tr / Tf 定义基准是 30%–70%（NXP UM10204），不是 10%–90%。**

| 口径 | 同一批上升沿实测 | 规格 | 结论 |
|---|---|---|---|
| 10%–90%（错） | 1797–1921 ns | ≤1000 ns | ❌ 误判超规格 |
| **30%–70%（对）** | **611–660 ns** | ≤1000 ns | ✅ 合格 |

10%–90% 会把波形**平坦的尾部**也算进上升时间，数值虚高 2–3 倍。
**看到 Tr 偏大 2–3 倍且波形是 RC 充电曲线时，第一件事是检查自己用的口径，而不是查上拉电阻、更不是去问硬件工程师。**

同理，仪器测量面板的「阈值类型」若显示 `30% / 50%`，那也**不是**规范口径，面板读数不能直接当 Tr 用。

判 Tr 超规格前的自查顺序（**按此顺序，别跳步**）：

1. **口径是不是 30%–70%** ← 80% 的「Tr 超规格」死在这里
2. 换位置复测多个沿，看分布是否极窄（窄 → 总线固有，数据可信）
3. 带宽限制是否真关：`:CHANnel<N>:BANDwidth?` 应返回 **+1.00000000E+009**（1 GHz）
4. 是否全新采集（**冻结记录上改任何采集设置都是无效操作**）
5. 采样率是否足够（1 ns/点看 2 µs 的沿绰绰有余）

1–5 全过且仍超规格，才谈物理原因（上拉阻值 / 走线电容 / 探头地线过长 / 探头比不匹配）。

## 完整工作流

### 步骤 1 · 只读摸底（绝不先改设置）

```bash
python scripts/scope.py <IP>
```

读回 `*IDN?`、`:TIMebase:SCALe?`、`:TIMebase:POSition?`、各通道 `:DISPlay?/SCALe?/OFFSet?/PROBe?`、`:ACQuire:POINts?/:SRATe?`。
先把现场摸清楚再动手 —— 尤其**用户说「已经截好图了」时，仪器大概率在 STOP 冻结态，发 `:RUN` 会立刻覆盖那屏波形，不可恢复**。

**先确认输入端真有信号：** 4 通道各取 3000 点，看 `y[min,max]`。全 ~0V 平线 → 探头脱落 / DUT 掉电，此时任何「测量」都是噪声，不要继续、更不要凭噪声出数。

**空闲 I²C 总线也应该是高电平**（上拉电阻把 SCL/SDA 拉到 VDD，3.3 V 或 1.8 V）。
把档位放到 `1 V/div`、offset ≈ 2 V 再读一次，就能区分三种情况：

| 读到的电平 | 含义 | 处置 |
|---|---|---|
| 稳定高电平（≈VDD） | 探头夹对了，总线只是**空闲** | DUT 没在发事务 → 需触发主机发一帧，或检查器件是否在跑 |
| 0 V / 噪声地板（<0.2 V） | **探头没夹到点 / 没接地 / 板子没上电** | 查接线与供电，别急着测 |
| 明显 50 Hz 工频纹波 | 地线没接好 | 先接好探头地 |

⚠️ **`:TRIGger:STATus?` 在 RUN 态下会挂满 socket 超时**（2026-09-28 实测，STOP 态秒回）。
配合 `q()` 默认重试 3 次 → 60 s 超时 = **假死 3 分钟**，现场看着像仪器掉线。
已内置 `Scope.q_short()` 兜住（`state()` 已改走它，从 180 s 降到 3.5 s）。
**判断"有没有信号"看波形边沿数，别依赖触发状态。** 详见 `references/pitfalls.md` C5。

### 步骤 2 · 1ns 高分辨率取数

```bash
python scripts/i2c_workflow.py --ip <IP> --tb 20e-6 --delay 0 --out wave.json
```

关键三条：

- **显设 `:WAVeform:POINts 100000`**（1 ns/点）。**不要信 `:WAVeform:POINts MAXimum?`** —— 它只回屏幕当前显示点数的上限（如 2173 = 46 ns/点），差 46 倍，是「波形不准」的根因。
- **取数前先查 `:WAVeform:POINts?`，`<= 0` 立刻报错退出**（DELay 把窗口推出采集内存时返回 0，硬等数据块会卡十几分钟）。
- **数值参数一律用 `repr(float(x))` 传**。用 `"%.10gE-6" % 40e-6` 会拼出 `4e-05E-6`，触发 `-131 Invalid suffix`，而且 `:TIMebase:POSition` 会**静默失效**（位置不变，看起来像「没反应」）。

**记录长度 = 10 × 时基**，且触发点固定在记录正中 50%。所以 73 µs/div 的记录是 730 µs、100 µs/div 只有 1 ms。

| 时基 | 采样率 | 记录长度 | 覆盖范围 |
|---|---|---|---|
| 21 µs/div | 1 GSa/s | 210 µs | [−105, +105] |
| 100 µs/div | 1 GSa/s | 1 ms | [−500, +500] |
| 500 µs/div | 200 MSa/s (5ns) | 5 ms | [−2500, +2500] |
| 1 ms/div | 100 MSa/s (10ns) | 10 ms | [−5000, +5000] |

规律：`记录长度 = 10 × 时基`，`采样率 = min(1 GSa/s, 1e6 / (10 × 时基))`。

**STOP 常落在窗外**：判据是记录**末段 SCL 还在翻转**（末 60 µs 内仍有跳变）→ 事务被截断。正解是**放慢时基把记录拉长**，而不是断言「总线没有 STOP」。

### 步骤 3 · 解码事务结构 + 测 12 参数

```bash
python scripts/i2c_decode.py --wave wave.json --spec std
```

输出：事务结构（START / 字节 / ACK / 重复START / STOP）、fSCL、tHIGH、tLOW、
tSU;STA、tHD;STA、tSU;STO、tSU;DAT、tHD;DAT、**tBUF（总线空闲时间，STOP→下一个 START）**，
逐项 PASS/FAIL/NA。

**`i2c_decode.py` 的 Tr/Tf 现已改用 30%–70% 口径**（2026-09-24 修，原为 10%–90% 且带上升时间窗口 bug，会把 SCL 上升沿算成 null）。现在解码器输出的 Tr/Tf 与 `tr_measure.py` 同源，可直接用；判 Tr 超规格仍建议以 `tr_measure.py` 逐沿统计 + MAD 离群剔除为基准。详见 `references/pitfalls.md` A2/A3。

### 步骤 3.1 · 规格档选择（标准 / 快速 / SMBus）

`--spec` 三选一，**电源兼容性测试（PSU 走 PMBUS）用 `smbus`**：

| 档 | 适用 | 时序判定 |
|---|---|---|
| `std`（默认） | I²C 标准模式 100 kHz | tHIGH≥4.0 / tLOW≥4.7 / tSU;STA≥4.7 / tHD;STA≥4.0 / tSU;STO≥4.0 / tSU;DAT≥250ns / tBUF≥4.7µs / Tr≤1000ns / Tf≤300ns |
| `fm` | I²C 快速模式 400 kHz | 上述对应收紧（tLOW≥1.3 / tSU;STA≥0.6 / tBUF≥1.3 / Tr≤300ns …） |
| `smbus` | **SMBus/PMBus 100 kHz 类（PSU 场景）** | 基本时序同 `std`；**额外软提示** tHIGH 上限 50µs、tTIMEOUT 25–35ms、tLOW:SEXT/MEXT、tSPIKE 50ns |

⚠️ SMBus 的 **tHIGH 有上限 50µs**（I²C 没有）—— 用于主机判总线空闲；若本段 SCL 高电平出现 >50µs，工具会告警（可能总线被时钟拉伸卡死）。
⚠️ **tTIMEOUT / tLOW:SEXT / tLOW:MEXT / tSPIKE 不在单条波形里测**（累计/超时/尖峰抑制性质），`smbus` 档只指出「需协议层/长时间抓包验证」，不伪造判定。详见 `references/i2c_spec.md` SMBus 章节。

**先确认器件身份再谈数据**：解首字节得到 7 位地址（如 `0xB4` → 地址 `0x5A` + WRITE），核对 ACK 分组有无错位。地址不对，后面全白算。

### 步骤 4 · Tr/Tf 单独按 30%–70% 复算 ⭐

```bash
python scripts/tr_measure.py --wave wave.json                  # 默认 30%-70%，含 MAD 离群剔除
python scripts/tr_measure.py --wave wave.json --list           # 逐沿打印
python scripts/tr_measure.py --wave wave.json --lo 10 --hi 90  # 对照用，会明确警告口径非规范
```

输出 `n / min / 中位 / max` 四件套与限值判定。**判 Tr/Tf 合格与否一律以本脚本为准。**

它的两个关键实现细节（自己写脚本时容易踩）：

- **只在 50% 穿越点（i50）所在单调段内向两侧找 30%/70% 穿越点**。简写的「向前找第一个低于 Vlo、向后找第一个高于 Vhi」在含下降沿的窗口里会**翻到下一个沿**，算出 `-2730 ns`、`78014 ns` 这种荒谬值。
- **搜索范围要限制**（默认记录长度的 5%）。信号在阈值附近抖动时，不限制会一路回溯到上一个周期，凭空产生 300+ 个假值。
- 离群剔除用 `中位数 ± 3 × 1.4826 × MAD`。分布极窄（实测 556 个沿 645.6–711.4 ns）→ 总线固有特性，数据可信。

### 步骤 5 · 游标闭环复核（别省）

把本地算出的 30% / 70% 穿越时刻写进仪器游标，回读 `:MARKer:XDELta?`，与本地计算逐位比对。

```python
s.cmd(':MARKer:MODE MANual'); s.cmd(':MARKer:SOURce CHANnel2')
s.cmd(':MARKer:X1Position ' + repr(t30))
s.cmd(':MARKer:X2Position ' + repr(t70))
print(s.q(':MARKer:XDELta?'))     # 应等于本地算的 (t70 - t30)，误差 0 ns
```

这一步能抓出「本地解析正确但换算错了」这类隐蔽错误，并且截图本身就是给报告的可视化证据。

**最可靠的读数来源**：时序量用游标 `:MARKer:...:XDELta?`；电平量用 `:MEASure:RESults?`。
STOP 冻结态下测量面板刷新极慢（最后加的那项常显示「未完成」），此时以 `:MEASure:RESults?` 的真实值为准。

### 步骤 6 · 截图取证

```bash
python scripts/scope.py <IP> --shot out.png      # 或走 i2c_workflow / scope.py 的 screenshot()
```

逐张对应参考图的时基 / 水平位置 / 垂直档复现。**收尾务必 `:MARKer:MODE OFF`**，别把游标线留在交付截图里。

#### ⚠️ 连机先确认 `:SCReen:DUMP?` 到底通不通（机台相关，2026-09-28 实测）

| 路径 | 效果 | 实测可用性 |
|---|---|---|
| `:SCReen:DUMP?`（`screenshot_dump()`） | **黑底整屏**，X1/X2/ΔX/1÷ΔX 全渲染 | ✅ `.155` 机可用；❌ **`.251` 机 RUN/STOP 两态均超时（60 s / 30 s 无字节）** |
| `:DISPlay:DATA?`（`screenshot()`） | 白底，**只渲染 X2，X1 不画** | ✅ 两机均可用 |

**别假定 `:SCReen:DUMP?` 一定可用。** 连机后先各试一次并记录结果；若该路径不通，
取证图改用 `:DISPlay:DATA?`，并在报告里如实标注「本机不支持 X1 渲染」，
**不要谎称游标已经画上去了**。兜底方案：本地自绘标注图 + 标注「数据重建图」。

#### ⚠️ 拍完必须 OCR 验收右侧面板 —— "文件存下来了" ≠ "面板有数"（2026-09-24 实测）

| 症状 | 真相 | 处置 |
|---|---|---|
| 面板显示**「摘要」页**（采集方式/通道参数），零测量值 | 面板是**按需渲染**的，命令发了不代表会画 | 先 `:MARKer:MODE OFF` → `:MEASure:CLEar` → 逐条加测量 → **sleep 3s**；面板即切到「测量」页 |
| 面板**最后一行恒显示「未完成」**（`RESults` 里其实有值） | 确定性 bug，不是 flaky（等 18s / 连拍 6 次 / 改条数顺序均复现） | ✅ **加完后重发一次最后那条测量命令**（如 `:MEASure:FALLtime CHANnel2`）→ 全部行出值 |
| 手动游标**只画出一条线**、面板只有 `X2(1):` | 本机 **X1 恒不渲染**（13 种变体全试过：设/查顺序、`X1Y1source`/`X2Y2source` 各组合、OFF→MANual、Y 位置、等待、重连） | 仪器状态其实**全对**（`X1Position?`/`X2Position?`/`XDELta?` 回读准确）。拿不到双游标图就**如实说明**，别谎称已设；兜底是自绘标注图并**标注"数据重建图"** |

**量化判定法**（比肉眼可靠）：数**游标色像素**——本机游标色 `(255,153,0)`，一条全高虚线 ≈ **228 px**；
把 X1/X2 拉到相距 400µs(≈388px) 排除重叠，仍只有一条即坐实。

**截图像素取证**（回应"数字对不上"）：竖直网格线间距 64px = 1 格 = 500ns → **128 px/µs**；
量两走线下降沿像素差即可反推 ΔX（实测 CH1 x=314.5 / CH2 x=353.8 → 39.3px → 310.3ns，与数据侧 310.25ns 误差 <1%）。

**口径差必须写进报告**：面板测量是**单边沿/单窗口**读数，工程最劣值是**全录制最劣**，两者本就不同；
且波形分析用 **30–70%**（NXP UM10204），示波器面板默认 **10–90%** → 面板上升/下降时间天然偏大（实测约 2.1×）。

### 步骤 7 · 回填 Excel 报告 ⭐ 分两条路，选错会毁文件

先探测文件里有没有 OLE 嵌入对象：

```bash
python scripts/xlsx_cellpatch.py --file report.xlsx --sheet 时序测试 --plan plan.json --out probe.xlsx
```

它会自己检测并打印 `OLE 嵌入对象: [...]`。按结果选路：

| 文件情况 | 用什么 | 为什么 |
|---|---|---|
| **含 `xl/embeddings/`**（嵌了波形图/附件包） | ✅ `xlsx_cellpatch.py` | 逐条复制 zip，只替换目标 sheet 的 XML，OLE 一字节不动 |
| 无 OLE 的普通表 | `fill_excel.py`（基于本地 Office 编辑器） | 有异步打开等待、备份、回读校验、file_id 多候选重试 |

**🚨 含 OLE 的文件绝不能走编辑器（实测）：**

| 手段 | 后果 |
|---|---|
| 本地 Office 编辑器 open→写→**save** | **OLE 包全灭**：embeddings 2→0，条目 158→164，体积 4 151 834→3 924 098 B |
| `openpyxl.load_workbook` 后 `save` | 同样丢绘图/媒体：3.94 MB→3.53 MB，掉 410 KB |
| ✅ zip/XML 层逐条复制改写 | 条目数、embeddings 完全不变，体积只多出写入的文本字节 |

`xlsx_cellpatch.py` 的 plan 格式：

```json
{"edits": [{"ref": "R23", "value": "5.952"},
           {"ref": "AH23", "text": "2026-09-24 复测：30%-70% 口径 ..."}]}
```

数值走 `<v>`，长文本走 `t="inlineStr"`（不动 `sharedStrings.xml`；文本里的 `<` `&` 会被自动转义）。

**回填的六个坑：**

1. **合并单元格：值必须写在合并块左上格**，否则 Excel 显示空白。常见形态是「每 2 物理列合并成 1 显示列」（E:F、G:H …）。
2. **用正则从 sheet XML 抠格时注意自闭合形态**。正确写法：
   `re.findall(r'<c r="([A-Z]+\d+)"([^>]*?)(?:/>|>(.*?)</c>)', xml, re.S)`
   写错会把相邻格揉在一起，凭空得出「值右移了一列」的假结论。（真踩过，白查半小时。）
3. **本地 Office 编辑器的 `sheet_id` 是每个文件独立分配的**，不能跨文件沿用（同一报告的两个版本实测分别是 `00002f` 和 `000024`）。换文件先取当前值。
4. **`col` 是 0-based，OOXML 列号是 1-based**：`OOXML列号 = col + 1`。（`row` 同理，所以 Excel 里看到的是 `row + 1` 行。）
5. **编辑器打开过文件就占句柄** → `os.replace` / `rename` 抛 `PermissionError [WinError 5]`（原子替换需独占），而 `shutil.copy2` 覆盖**能成功**。收尾用 copy2。
6. **回读拿到的是单元格显示值**，会被数字格式舍入（写 5.4053、2 位小数格式，回读是 5.41 —— 不是写错）。而且解析回读串**必须用 `csv` 模块**，`split(",")` 会被 Notes 里的逗号打断，把「写对了」误报成「不一致」。

**数值口径看列头。** 若列头写的是 **Min**，就填全记录最小值（用 `tr_measure.py` 的 30%–70% 统计取 min），不要填中位数。

### 步骤 8 · 收尾与交付

- **还原仪器**：`:TIMebase:SCALe` / `:POSition` 回到原值，`:MARKer:MODE OFF`。
- **抽干错误队列**：循环 `:SYSTem:ERRor?` 读到 `+0,"No error"`。
- **记录采集条件进 Notes**：时基、位置、探头倍率、通道映射、口径（30%–70%）、n/min/中位/max、削顶自查结果、游标互印结果。
- 报告用自包含 HTML（内嵌证据截图 + 每项读数出处 + 差异说明），表格回填后**回读校验**并报告「全部一致 / 存在不一致」。

## 步骤 9 · 一键全量流水线（推荐：新器件 / 复测复用）⭐

把上面步骤 2–8 拆的坑焊死进**一个参数化入口** `scripts/i2c_full_test.py`，
下次任何 PSU（0x58~0x5B 或新器件）直接复用，完成「全量低速测试」：
**取数 → 解码 12 参数 → 游标取证截图 → OCR 验收 → OLE 安全回填**。

### 子命令

```
connect   连示波器，打印 IDN / 探头 / 状态
check     信号自检（CH1/CH2 峰峰值，确认探头倍率与接触）
setup     ★ 自动配置示波器（打开通道 + :AUToscale + I2C 触发），省掉手动调档
capture   1ns 高分辨率取数（双通道）→ wave.json        ★ POINts 显设 100000
decode    wave.json → i2c_decode.analyze → decode.json（13 参数 + 判定）
cursors   从波形算 6 组时序量的游标边沿对 (X1,X2 秒) → cursors.json
shots     抓全部取证截图（★ 用 Scope.screenshot_dump 才能画出 X1）→ <out>/shots/
verify    对 <out>/shots 内 PNG 做 OCR 验收（查「未完成」/ΔX）
fill      由 decode.json 生成回填计划并 OLE 安全回填 Excel
batch     ★ 多器件批量：对一串地址循环 decode→cursors→fill（一次填完所有 PSU 行）
run       capture→decode→cursors→(shots 若在线)→fill 串起来
```

### 用法

```bash
# 在线全流程（示波器已停在目标器件记录上）
python scripts/i2c_full_test.py --ip 10.121.136.155 --addr 0x5B \
    --ch-scl 1 --ch-sda 2 --tb 20e-6 --delay 257e-6 --out D:/_scope_tmp/0x5B \
    --xlsx "...报告_回填.xlsx" --sheet 时序测试 --freq 99.46 run

# ★ 自动配置示波器（每次换探头/换通道/换器件只跑一次，省手动调档）
python scripts/i2c_full_test.py --ip 10.121.136.155 --ch-scl 1 --ch-sda 2 \
    --trigger-addr 91 setup          # 91 = 0x5B 的 7 位地址

# ★ 多器件批量回填（每个器件的 wave.json 在 <base-out>/<addr>/ 下；一次填完 PSU0~PSU3）
python scripts/i2c_full_test.py --base-out D:/_scope_tmp/bulk \
    --addr-list "0x58 0x59 0x5A 0x5B" \
    --xlsx "...报告_回填.xlsx" --sheet 时序测试 --freq 99.46 batch

# 离线：已有 wave.json，只做解码+游标+回填（不碰示波器）
python scripts/i2c_full_test.py --addr 0x5B --out D:/_scope_tmp/0x5B \
    --xlsx "...报告_回填.xlsx" --sheet 时序测试 --freq 99.46 decode cursors fill
```

### 关键参数与已焊死的坑

- **`--freq`**：频率(kHz)覆盖。省略则用解码均值；本次 0x5B 用户指定填**面板读数 99.46**（解码均值为 95.45，二者差因面板是单窗读数、工具是全记录最劣，属正常）。
- **`--spec`**：`std`(默认) / `fm` / `smbus`（PSU/PMBus 场景，基本时序同 std + 软提示 tHIGH 上限/超时/时钟拉伸）。
- **`--xlsx` + `--sheet 时序测试`**：含 OLE 嵌入对象时，`fill` 一律走 `xlsx_cellpatch.py`（zip/XML 层），OLE 一字节不丢。
- **`--addr`**：器件地址（如 `0x5B`，Excel B 列是大写 `0X5B`，工具大小写兼容自动匹配行）。
- **`--trigger-addr` / `--trigger-cond`**：`setup` 用——I2C 触发地址(7 位)与条件(START/STARTR/STOP/ADDR/NACK)。
  ⚠️ **并非每台机都装了 I2C 触发选件**：实测 DSO-X 6004A `10.121.136.251` 执行
  `:TRIGger:MODE I2C` 返回 `-224,"Illegal parameter value"` + 3×`-113,"Undefined header"`，
  `setup` 会自动降级为**边沿触发**（SCL 下降沿 + 电平≈VDD/2）。
  这种情况下**无法按地址触发**，只能：① `:SINGle` 抓一屏 → ② **解码首字节确认器件地址**
  （如 `0xB4` → 0x5A+WRITE），抓到目标器件再往下走；抓错就重抓。
  `:TRIGger:MODE?` 返回 `EDGE` 即说明走的是降级路径。
- **`--addr-list` / `--base-out`**：`batch` 用——地址空格列表 + 各器件 wave.json 根目录（子目录为 `<addr>`）。
- 游标截图走 `Scope.screenshot_dump()`（`:SCReen:DUMP?` + `:HARDcopy:INKSaver OFF`，黑底、X1/X2/ΔX 全渲染）；白底 `:DISPlay:DATA?` 画不出 X1，已弃用。
- 测量面板加完测量后 `add_meas` 默认 `fix_last_row=True`：等 ~3 s 切到测量页 → 重发最后一条测量命令刷出「未完成」那行。
- `cursors` 用的边沿对算法与 `decode` 判定**同源**，且 tHD_DAT / tSU_DAT 取**全记录最小 dx = 工程最劣值**（与报告填值口径一致）。

### 🚀 提效工作流（一次测完所有 PSU）

电源兼容性测试常要对 PSU0~PSU3（0x58~0x5B）逐个测。最优顺序：

1. **`setup` 一次**（配好通道/量程/I2C 触发）—— 之后换器件不用再手动调档。
2. 对每个器件：把示波器停在它的记录上 → `capture --out D:/_scope_tmp/bulk/<addr>`。
3. 全部抓完后**离线一次 `batch`**：`--addr-list "0x58 0x59 0x5A 0x5B" --base-out D:/_scope_tmp/bulk ... batch` → 自动循环 decode→cursors→fill，一次填完报告所有行（OLE 安全）。

→ 单器件复测也直接 `run`；新器件只改 `--addr` 即可复用，无需重学任何流程。

### 离线验证结论（0x5B，wave_5b.json 实测）

`decode` 13 参数全 PASS（含 tBUF，单笔事务无 STOP→START 对时判 NA）；`cursors` 6 组 X1/X2/ΔX（前 5 组）与手工 `cursors_5b.json` **逐项吻合到 4 位小数**；
`fill` 在 Excel 副本上回填 **29 处**（SCL 行 16 + SDA 行 13），与已验证映射逐项一致，OLE 2 嵌入对象完好、158 条目、体积不变。
`batch` 在 4 地址副本上端到端验证：4/4 回填、OLE 完好。`tBUF` 修复（按时刻排序事件）有回归测试锁定。
→ 工具已具备「下次任何 PSU 直接复用」的可靠性。

## 步骤 10 · 单器件复测 + 标准取证图集（`scripts/redo_psu.py`）⭐

器件复测（如 0x5A 补图）走这个脚本：一键出 **9 张标准图**（对齐 0x59/0x5B 图集口径），
并串起 削顶闸门 + 地址闸门 + 游标闭环。

### 子命令

```
precheck   连接 + 档位/削顶预检（★ 开测前必做）
snapshot   改显示前先备份当前屏幕为 00
grab       ★ 反复 arm 触发→取数→解码，直到命中 --addr 那一帧（无 I2C 触发选件时必需）
capture    STOP 后读当前采集内存取数（★ 这一条不会重新触发）
decode     wave.json → decode.json（DC 电气 + 时序判定 + 地址闸门）
cursors    算 5 组游标边沿对
shots      出 9 张标准取证图（每张出图前自动校回时基）
all        precheck → snapshot → grab → capture → decode → cursors → shots
```

### ★ 地址闸门：没有 I2C 触发选件时，`--addr` 只是"文件名"

**实测（2026-09-28，DSO-X 6004A `10.121.136.251`）**：本机 `:TRIGger:MODE I2C` 返回
`-224 Illegal parameter value`（**无 I2C 触发选件**），只能退化为 SCL 边沿触发。
此时 `--addr` **完全不参与触发**，抓到哪一帧是随机的 —— 实测连续三次分别抓到
`0x59 → 0x58 → 0x5B`（四个 PSU 地址都在同一条总线上轮流通信）。

❗**真实误归档**：当日因此把一帧 0x58 的波形按 0x5A 命名归档、复制进桌面图集、
还更新了完整度说明文档 —— 而 `decode.json` 里 `addr7: "0x58"` 白纸黑字写着，只是没人核对。
**张冠李戴的取证数据比没有数据更坏。**

**闸门（已焊死）**：`decode` / `shots` 都比对「实测 `addr7` == `--addr`」，不匹配即中止；
`shots` 若缺 `decode.json` 直接拒绝出图。确要接受任意帧才加 `--allow-any-addr`。

```bash
# 正确流程：让 grab 自动重试直到命中目标地址
python scripts/redo_psu.py --ip <IP> --addr 0x5A --out D:/_scope_tmp/0x5A \
    --tb 200e-6 grab cursors shots
```

### ★ DC 电气要单独用小档位抓

`VOL ≤ 0.4 V @3 mA` 需要足够的量化分辨率。2 V/div 时满量程 16 V、8 bit → **LSB ≈ 67 mV**，
是门限的 17%，读数只能落在 67 mV 的格子上 → 判定为 **INDET（不可判定）**，
既不能算合格也不能算超标。测 VOL 改用 `--ch-scale 0.5 --ch-offset 1.65`
（满量程 4 V、LSB ≈ 16 mV）：

```bash
python scripts/redo_psu.py --ip <IP> --addr 0x5A --out D:/_scope_tmp/0x5A_dc \
    --tb 200e-6 --ch-scale 0.5 --ch-offset 1.65 grab
```

### 电压判定三级：PASS / FAIL / INDET

- VOL/VOH 取**低/高电平样本的 P90 / P10**（稳态最坏值），同时另存 P50 典型值。
  **不要用 P98/P2** —— 边沿过渡点实测占 4.6%，2% 的余量兜不住
  （曾把 VOL 报成 1.457 V，而稳态实测只有 0.118 V）。
- `|实测 − 门限| < 1 LSB` → **INDET**（提示换小档位），而不是硬判 PASS/FAIL。
- LSB 由采样值反推（唯一取值相邻间隔的中位数，**分通道各估再取中位**；
  混通道估会把步长算小：合并 42.1 mV vs 分通道 66.9 mV）。

---

## 验收别人生成的测试代码（不连真机，5 分钟出结论）

任何 I2C 测试代码 —— 自己写的、同事写的、**别的 Agent 生成的** —— 在信它的数之前，先跑对拍：

```bash
python scripts/synthetic_verify.py --suite "<被测实现目录>"        # 逐项对照真值
python scripts/synthetic_verify.py --suite "<目录>" --duty 30 --mid-start   # ★ 决定性实验
python scripts/synthetic_verify.py --suite "<目录>" --mode fast --glitch 200ns
```

它造一段**标准答案完全已知**的波形（fSCL / tHIGH / tLOW / Tr / Tf / VCC 全部由你指定），喂进被测实现，逐项标出「与真值一致 / 差 2 倍 / 疑似桩函数 / 恒 0 却报 PASS」。

### 五条红线（实测全部踩过，一条不过就不能用）

| # | 红线 | 实测踩中的后果 |
|---|---|---|
| 1 | **fSCL 是否差 2 倍** | 用相邻边沿差（半周期）当周期，100 kHz 报成 200 kHz，**且判 PASS** |
| 2 | **Tr/Tf 四个量都要测**（SCL/SDA × 上升/下降），口径必须 30%–70% | 只给 SCL 出 Tr、SDA 出 Tf，漏检 SCL 下降时间超规格 1000 ns 仍报 PASS |
| 3 | **未实现的项必须报 N/A**，禁止用恒 0 或桩函数报 PASS | 12 项时序里 9 项是返回 0 的桩，其中一半恒 PASS（假合格）、一半恒 FAIL（假不合格） |
| 4 | **结论必须与记录起点相位无关** | ★ 见下 |
| 5 | **数据不足要报 N/A**，不是 fSCL=0 还判 PASS | 边沿不足时输出 0 Hz + PASS |

### ★ 红线 4 单独说：记录起点会翻转结论

用 `--duty 30 --mid-start` 造两段**物理上完全相同**的波形，只把记录起点挪到总线中间（首沿由上升沿变下降沿）。真值 tHIGH=3000 ns / tLOW=7000 ns：

| 判定者 | 正常起点 | 记录从中间开始 |
|---|---|---|
| 正确实现 | 2996 / 7003 ns | **3000 / 6987 ns**（应与相位无关） |
| 有缺陷实现（实测） | 2984 / 7004 ns ✅ | **6987 / 3000 ns → tHIGH/tLOW 完全互换**，且错值带 PASS 交付 |

**根因**：把所有边沿混在一个列表里、再用 `range(0, len(edges), 2)` 固定步长配对 —— 一旦首沿是下降沿（或中途插入一个毛刺），后续配对奇偶翻转，tHIGH 拿到 tLOW 的数。
**正确写法**：边沿带类型，**按类型配对**（`rise→fall` 算 tHIGH、`fall→rise` 算 tLOW、相邻 `rise→rise` 取倒数算 fSCL），绝不做固定步长配对。

> 现场采集时记录起点由触发位置决定，谁也无法保证首沿一定是上升沿 —— 这个缺陷在真实使用中**随机发生**，且错误结论会带着 PASS 一起交付。

### 考官必须先自证

对拍器自己也得验：`control_test.py` 用本 skill 的成熟解码器回读同一批合成波形（50%/30% 占空比、400 kHz、中间起点四种场景），四种全部一致才算「考官可信」。**先证明自己没错，再判别人。**

## 沙箱验证（不连真机，端到端跑通在线链路）⭐

**场景**：示波器不在手边（出差 / 离线 / 没授权选件），但又想确认 `i2c_full_test.py` 的在线子命令（connect/check/setup/capture/decode/cursors/shots/verify）代码路径没崩、波形能解出合理时序。

`scripts/mock_scope.py` 起一个**本地 TCP 仿真示波器**（最小 SCPI 子集 + 合成 I2C 波形 + 最小合法 PNG），让 `Scope` 客户端不连真机就能取数/截图；`scripts/sandbox_test.py` 起 mock 跑全套在线链路并做合理性断言。

```bash
cd scripts
python sandbox_test.py          # 起 mock(5051) → 端到端跑 8 个环节 → 断言 + 取证截图
python full_lowspeed_test.py    # 全量覆盖报告：DC + 时钟 + 6 时序量×三档 + Tr/Tf + 拉伸/毛刺/Sr
python selftest.py             # 纯函数自测（解码器/边沿/START 判据/规格判定/新分析块）
```

沙箱到底验证了什么：

- **全链路不崩**：connect/check/setup/capture/decode/cursors/shots/verify 八个环节代码路径全跑通，截图落盘（`i2c_sandbox_out/shots/` 9 张）。
- **波形能解出合理时序**：合成了「普通写事务（含从机时钟拉伸）+ 复合读事务（含重复 START）」+ 空闲段毛刺（窗口 700 µs），断言 `fSCL≈98 kHz`、`n_start/n_stop` 合理、`tSU_STA` 有真值、`tBUF≈26 µs`，游标边沿对与解码同源。
- **DC 电气 / 协议健壮性也一并验**：VDD=3.3 V 时 VOH/VOL/VIH/VIL 全 PASS、Cb≈4.8 pF（由 Tr 反推）、时钟拉伸检出 1 段、毛刺检出 1 处、重复 START 检出。
- **顺带修了四个真机也会中招的坑**（沙箱暴露的）：
  1. 取数窗口被截断——`capture` 默认 `points` 不足会切掉靠后的 STOP/事务；沙箱把记录窗口扩到 700 µs 覆盖全部事务。
  2. `fSCL` 被空闲段稀释——解码器对「含 SCL 下降沿的活动周期」求平均，但 STOP 后 SCL 一直高、下一笔首拍只有下降沿没有上升沿，这段**空闲伪周期**（tHIGH 几十 µs）会把 100 kHz 报成 ~90 kHz。
  3. **`fSCL` 被时钟拉伸周期稀释**——从机把 SCL 拉低几十 µs 的那一拍 tLOW 超长，也会拉低平均频率。已把剔除判据扩为「tHIGH 或 tLOW > 3×各自中位数」，两类异常周期一并剔除。
  4. **DC 电平取极值会假 FAIL**——斜坡过渡点（50 ns 内穿过阈值）混进 min/max，会把 VOL 当成 1.58 V、VOH 当成 1.72 V，判定全 ❌。已改为取低电平样本的 98 分位（VOL）、高电平样本的 2 分位（VOH）代表稳态电平。

⚠️ **沙箱是仿真，不是真机**：只验证代码路径与数据解析逻辑；数值合规（Tr/Tf 是否真超规格、探头比是否匹配）以真机为准。

## 低速测试覆盖范围与边界 ⚠️（2026-09-25 补齐）

I2C 低速测试**不止时序量**，按规范分四层。本工具（Keysight 示波器**被动测量 + 解码**）的覆盖情况：

| 层 | 内容 | 本工具 | 说明 |
|---|---|---|---|
| ① 电气 DC | VOH/VOL/VIH/VIL、总线电容 Cb | ✅ 可测 | 由波形电平 + Tr 反推；`--vdd/--rp` 可选 |
| ② 时序 | fSCL/tHIGH/tLOW、tSU;STA/tHD;STA/tSU;STO/tHD;DAT/tSU;DAT/tBUF、Tr/Tf | ✅ 可测 | 全部已实现并对照三档规格判定 |
| ③ 协议逻辑 | 地址/R-W、ACK/NACK、重复 START、时钟拉伸 | ✅ 可测 | 由解码器从波形解出；毛刺计数对应 tSPIKE |
| ④ 功能/健壮 | 设备扫描、寄存器读写、总线卡死恢复、热插拔、POR | ❌ **做不了** | 需 I2C **主控制器主动发事务**，被动抓包无此能力 |

**明确测不了的**：tVD;DAT / tVD;ACK（驱动端输出延迟，被动抓包无法孤立测量，已用 tHD;DAT/tSU;DAT 间接覆盖数据有效窗口）、输入迟滞 VHYS（需主动扫阈值）、SMBus 主动超时项（tTIMEOUT 25~35 ms / tLOW:SEXT 25 ms / tLOW:MEXT 10 ms / PEC）—— 这些要配 I2C 主控 / 总线分析仪。

## 三条不可越的底线

1. **测不了就说测不了（NA）**，绝不用别的边沿凑数、绝不编造读数。
2. **数据被污染时（探头比不匹配 / 窗口含空闲段）不覆盖原值**，只写 Notes 并给复测方案。
3. **下「不合格」结论前先跑 `selftest.py`**，确认不是自己算错。

## 工具箱

> 完整索引见上方 **[文件索引](#文件索引)**，按用途分类列出所有脚本和参考文档。

## 文件索引

### 根目录文件

| 文件 | 说明 |
|---|---|
| `run_i2c_test.py` | 统一快速启动入口（selftest / sandbox / full / connect / check / test / batch / run / setup） |
| `requirements.txt` | Python 依赖：`numpy>=1.20.0`, `openpyxl>=3.0.0`, `rapidocr-onnxruntime>=1.2.0` |
| `VERSION` | 当前版本 `__version__ = "1.0.0"` |
| `CHANGELOG.md` | 完整变更日志（覆盖矩阵、依赖、Resolved Issues） |

### scripts/

| 脚本 | 用途 |
|---|---|
| `scope.py` | 连接/重连、查询、块读、取波形、截图、测量面板、位置扫描（含 `--shot`） |
| `i2c_full_test.py` | **一键全量流水线**（connect/check/capture/decode/cursors/shots/verify/fill/run），把全部坑焊死进参数化入口，新器件/复测直接复用 |
| `i2c_decode.py` | 事务解码 + 6 个时序量 + **DC 电气(VIH/VIL/VOH/VOL/Cb)** + **时钟拉伸/毛刺(tSPIKE)/重复START** + 规格判定（Tr/Tf 30%–70% 口径） |
| `tr_measure.py` | **30%–70% 规范口径** Tr/Tf 逐沿测量（含 MAD 离群剔除） |
| `i2c_workflow.py` | 一键：连接 → 1ns 取数 → 存 JSON → 调解码器出结果 |
| `selftest.py` | 纯函数自测（33 项，含 DC/拉伸/毛刺/Sr 新分析块），判定前的强制关卡 |
| `mem_scan.py` | 扫描整个采集内存，判「空闲 vs 真实活动」，先锁定目标区段 |
| `cmd_measure.py` | 按参考图设时基、扫水平位置出数、逐张截图 |
| `cmd_dump.py` | 多窗口拼接导出整段波形；验证缩放是否触发重新采集 |
| `xlsx_cellpatch.py` | **含 OLE 的 xlsx** 安全改写（zip/XML 层，保留嵌入对象） |
| `fill_excel.py` | 普通 xlsx 回填（编辑器路线：备份→写→保存→回读校验） |
| `synthetic_verify.py` | **合成已知答案波形对拍器**：不连真机，验收任意 I2C 测试实现（含五条红线检查、相位无关性实验） |
| `mock_scope.py` | **沙箱仿真示波器**：本地 TCP(5051) 最小 SCPI 子集 + 合成 I2C 波形（含时钟拉伸/重复START/毛刺）+ PNG，不连真机即可喂 `Scope` 客户端 |
| `sandbox_test.py` | **沙箱端到端验证**：起 mock 跑全套在线链路(connect→verify)并断言时序/DC/拉伸/毛刺合理、截图落盘 |
| `full_lowspeed_test.py` | **全量低速测试覆盖报告**：DC + 时钟 + 6 时序量×三档 + Tr/Tf + 拉伸/毛刺/Sr 一次跑全并判定，落 MD 报告 |
| `redo_psu.py` | PSU 复测脚本（取数后强制**削顶自检**；档位不对当场报错；然后一键出齐 8+1 张标准取证图，与 0x59/0x5B 图集口径对齐） |

### references/

| 文件 | 说明 |
|---|---|
| `i2c_spec.md` | I²C 标准/快速模式完整规格表、判定口径（含 SMBus 额外参数） |
| `scpi-cookbook.md` | SCPI 命令全表、正确/错误形式对照、错误码表、采集快照对照值 |
| `pitfalls.md` | 全部实测坑位汇编（A: 测量口径, B: 采集, C: 仪器状态, D: 现场纪律, E: Excel 报告, F: 环境） |

## 详细参考

按需查阅：

- **[references/i2c_spec.md](references/i2c_spec.md)** —— 规格表、口径对比表、SMBus 额外参数
- **[references/scpi-cookbook.md](references/scpi-cookbook.md)** —— SCPI 命令对照表、错误码表
- **[references/pitfalls.md](references/pitfalls.md)** —— 6 类坑位实测经验（采集、仪器状态、Excel 报告等）

## 环境纪律（实测教训）

1. **Git-Bash 的 `/tmp` ≠ Windows Python 的 `/tmp`** —— Bash 里读得到，Python 里会去 `C:\tmp\` 报 FileNotFoundError。脚本内一律写绝对路径。
2. **不要用 heredoc 写含引号/中文的 Python**，转义会被吃掉。落盘再跑。
3. **必须 `python -u`** —— 进程被超时杀掉时缓冲的 stdout 会整段丢失，看起来像「什么都没输出」。
4. socket timeout 设 8–20 s，大块传输 120–180 s。
