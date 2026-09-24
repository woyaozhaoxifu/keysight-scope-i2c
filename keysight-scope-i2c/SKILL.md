---
name: keysight-scope-i2c
description: 通过 SCPI（TCP 5025）远程操作 Keysight/Agilent InfiniiVision 示波器（DSO-X 6000X/3000T/2000X 等 WinCE 机型），完成 I2C 总线时序测量的端到端工作流——定位目标窗口、1ns 高分辨率取数与双通道拼接、I2C 事务解码（START/地址/ACK/STOP）、12 个时序参数计算与规格判定、Tr/Tf 按 30%-70% 规范口径逐沿测量、游标闭环复核、示波器截图，最后把结果回填进带 OLE 嵌入对象的 Excel 测试报告。当用户要测 I2C 时序（fSCL/tHIGH/tLOW/tSU;STA/tHD;STA/tSU;STO/tSU;DAT/tHD;DAT/Tr/Tf）、算上升下降时间、判 PASS/FAIL、排查 Tr 超规格，或要求把实测值写回测试报告表格时使用。内含两条最贵的教训：Tr/Tf 必须用 30%-70% 口径（用 10%-90% 会虚高 2-3 倍、把合格判成超规格），以及含 OLE 嵌入对象的 xlsx 绝不能用本地 Office 编辑器或 openpyxl 保存（会静默摧毁嵌入包）。
agent_created: true
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
python scripts/selftest.py          # 纯函数自测，24 项。报"不合格"之前必须先跑这个
python scripts/scope.py <IP>        # 只读摸底：IDN、时基、通道、采样率（不改任何设置）
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
tSU;STA、tHD;STA、tSU;STO、tSU;DAT、tHD;DAT，逐项 PASS/FAIL/NA。

**注意 `i2c_decode.py` 内置的 Tr/Tf 是 10%–90% 口径**，不能用它的 Tr/Tf 值做判定 —— 一律走下一步。

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

## 三条不可越的底线

1. **测不了就说测不了（NA）**，绝不用别的边沿凑数、绝不编造读数。
2. **数据被污染时（探头比不匹配 / 窗口含空闲段）不覆盖原值**，只写 Notes 并给复测方案。
3. **下「不合格」结论前先跑 `selftest.py`**，确认不是自己算错。

## 工具箱

| 脚本 | 用途 |
|---|---|
| `scripts/scope.py` | 连接/重连、查询、块读、取波形、截图、测量面板、位置扫描（含 `--shot`） |
| `scripts/i2c_decode.py` | 事务解码 + 12 个时序量 + 规格判定 |
| `scripts/tr_measure.py` | **30%–70% 规范口径** Tr/Tf 逐沿测量（含 MAD 离群剔除） |
| `scripts/i2c_workflow.py` | 一键：连接 → 1ns 取数 → 存 JSON → 调解码器出结果 |
| `scripts/selftest.py` | 纯函数自测（24 项），判定前的强制关卡 |
| `scripts/mem_scan.py` | 扫描整个采集内存，判「空闲 vs 真实活动」，先锁定目标区段 |
| `scripts/cmd_measure.py` | 按参考图设时基、扫水平位置出数、逐张截图 |
| `scripts/cmd_dump.py` | 多窗口拼接导出整段波形；验证缩放是否触发重新采集 |
| `scripts/xlsx_cellpatch.py` | **含 OLE 的 xlsx** 安全改写（zip/XML 层，保留嵌入对象） |
| `scripts/fill_excel.py` | 普通 xlsx 回填（编辑器路线：备份→写→保存→回读校验） |

## 详细参考

- **[references/i2c_spec.md](references/i2c_spec.md)** —— I²C 标准/快速模式完整规格表、判定口径
- **[references/scpi-cookbook.md](references/scpi-cookbook.md)** —— SCPI 命令全表、正确/错误形式对照、错误码表、采集快照对照值
- **[references/pitfalls.md](references/pitfalls.md)** —— 全部实测坑位汇编（含 `:PROBe` 改不动、面板 BWLimit 高亮假象、拼接数据步长不统一等）

## 环境纪律（实测教训）

1. **Git-Bash 的 `/tmp` ≠ Windows Python 的 `/tmp`** —— Bash 里读得到，Python 里会去 `C:\tmp\` 报 FileNotFoundError。脚本内一律写绝对路径。
2. **不要用 heredoc 写含引号/中文的 Python**，转义会被吃掉。落盘再跑。
3. **必须 `python -u`** —— 进程被超时杀掉时缓冲的 stdout 会整段丢失，看起来像「什么都没输出」。
4. socket timeout 设 8–20 s，大块传输 120–180 s。
