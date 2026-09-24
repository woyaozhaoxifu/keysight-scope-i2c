# SCPI 命令速查与报错原文

面向 Keysight/Agilent InfiniiVision（DSO-X / MSO-X 2000X / 3000T / 6000X，WinCE 内核机型）。
以下命令与报错均在实机上验证过（DSO-X 6004A 一类机型，固件 07.55 系列）。

---

## 0. 怎么确认是这类机器

```bash
curl -s -I http://<ip>/ | head -5      # Server: Microsoft-WinCE/7.00
```
```
*IDN?  ->  AGILENT TECHNOLOGIES,DSO-X 6004A,<序列号>,07.55.xxxxxxxx
```

端口：`5025` SCPI socket（首选）、`5024` telnet SCPI、`80` Web UI、`111` portmapper、`4880` VXI-11。
`5025` 和 `5024` 通常都开着，**直接用 5025，不用去点网页**。

---

## 1. 只读状态

```
*IDN?            *OPT?
:CHANnel<n>:DISPlay?  :SCALe?  :OFFSet?  :RANGe?  :COUPling?
:CHANnel<n>:BWLimit?  :BANDwidth?  :PROBe?  :UNits?  :IMPedance?
:TIMebase:SCALe?  :TIMebase:RANGe?  :TIMebase:POSition?  :TIMebase:MODE?  :TIMebase:REFerence?
:TRIGger:MODE?  :TRIGger:SWEep?  :TRIGger:STATus?  :TRIGger:EDGE:SOURce?  :TRIGger:EDGE:LEVel?
:ACQuire:TYPE?  :ACQuire:SRATe?  :ACQuire:POINts?  :ACQuire:COUNt?
:OPERegister:CONDition?          # 运行/停止位
:MARKer:MODE?
:MEASure:SOURce?
:SYSTem:ERRor?                   # 读一次弹一个错误，要循环读到 "No error"
```

**`:CHANnel<n>:PROBe?` 返回 `+1.0000000E+00` / `+10.000000E+00`**（字符串，要转 float）。
1:1 探头挂开漏总线会引入几十到上百 pF，把上升沿拉长到 µs 级；10:1 只有约 12 pF。
**上升时间异常偏大时先查这里。**

⚠️ **`:CHANnel<n>:BWLimit?` 才是带宽限制的真值**（`0`=限制关闭/全带宽，`1`=20MHz 限制开）。
`:CHANnel<n>:BANDwidth?` 返回 `+1.00000000E+009`（1 GHz）是最直观的确认。
**面板上「带宽限制 20MHz」按钮绿色高亮只是「可点击」指示，不代表限制已开启** —— 别据此误判。

---

## 2. 波形读取

```
:WAVeform:SOURce CHANnel<n>
:WAVeform:FORMat WORD            # 16bit 二进制；ASCii 慢且精度差
:WAVeform:BYTeorder LSBFirst
:WAVeform:UNSigned 1
:WAVeform:POINts:MODE MAXimum    # ★ 关键，别用 RAW
:WAVeform:POINts? MAX            # ⚠️ 这个值偏小，见下方警告
:WAVeform:POINts 100000
:WAVeform:POINts?
:WAVeform:XINCrement?  :WAVeform:XORigin?  :WAVeform:XREFerence?
:WAVeform:YINCrement?  :WAVeform:YORigin?  :WAVeform:YREFerence?
:WAVeform:DATA?                  # IEEE488.2 定长块
```

| 设置 | 同样的 500 µs 记录 | 每秒采样点 | 精度 |
|---|---|---|---|
| `POINts:MODE RAW` | 4807 点 | 104 ns/点 | 差 26 倍 |
| `POINts:MODE MAXimum` | 125000 点 | **4 ns/点** | 时序判定必须用这个 |

🚨 **`POINts? MAXimum` 返回的是「屏幕当前显示点数的上限」，不是内存能给的最高分辨率。**
实测它只回 2173 点（46 ns/点），而显设 `:WAVeform:POINts 100000` 能拿到 **1 ns/点**，差 46 倍 ——
这正是「取回的波形看起来不准 / 游标对不上参考图」的根因。**一律显式设置点数，不要用 `?MAX`。**

⚠️ `POINts? MAXimum` 还与当前 source 绑定：source 停在未显示的通道时返回小得离谱的值（实测 +8333），
设好 `:WAVeform:SOURce CHANnel<n>` 之后才正常。

🚨 **取数前先查 `:WAVeform:POINts?`，`<= 0` 立刻退出。**
DELay 把窗口推出已采集内存时返回 `0`，此时 `:WAVeform:XORigin?` 会卡约 18 秒超时，
硬等数据块会卡十几分钟。**不要硬等。**

换算：
```
t[i] = (i - XREFerence) * XINCrement + XORigin
v[i] = YINCrement * (raw[i] - YREFerence) + YORigin
```
自洽性校验：`XINCrement * 点数` 应等于 `时基档 × 10 格`（125000 × 4ns = 500 µs = 50µs/div × 10 ✓）。

定长块格式：`#` → 1 位数字 n → n 位长度 L → L 字节数据 → 结尾换行。
（实测 PNG 截图约 27–40 KB，BMP 约 1.5 MB，波形 125000 点 WORD 约 250 KB。）

🚨 **`:WAVeform:DATA?` 只给当前屏幕窗口**。屏幕外的记录要滑动 `:TIMebase:POSition` 逐窗口取回再拼接。

---

## 3. 截图

```
:DISPlay:DATA? PNG       # 800x600 PNG，约 27-40 KB
:HARDcopy:INKSaver OFF   # 关掉墨迹节省，颜色更准确
:SCReen:DUMP? PNG        # 等价写法
```
块格式同上。

---

## 4. 测量面板（最易出问题）

### 加测量项 —— 正确与错误形式

```
✅  :MEASure:CLEar                    # 无返回，必须用 send 不能用 query
    :MEASure:FREQuency CHANnel1
    :MEASure:PWIDth   CHANnel1
    :MEASure:NWIDth   CHANnel1
    :MEASure:RISetime CHANnel1
    :MEASure:FALLtime CHANnel1
    :MEASure:VMAX     CHANnel2
    :MEASure:VMIN     CHANnel2

❌  :MEASure:ITEM FREQuency,CHANnel1
    -> ERR: -113 Undefined header
```
用错形式时**不会报错中断**，只是解析出来是 0 项，很容易以为「仪表没数」。

### 读值 —— 逐项慢，批量快

```
慢： :MEASure:FREQuency? CHANnel1      # 每项要几秒；扫 10 个位置会卡死
快： :MEASure:RESults?                 # 一次拿全部，首选
```
`RESults?` 返回逗号分隔，**每组 7 个字段**（标签, 值, +5 个统计字段）：
```
FREQ(CH1),4.20300E+04,0.0,0.0,0.0,0.0,0.0,PWID(CH1),4.28000E-06,...
```
**无效读数 = `9.9E+37`**（面板上就显示「未完成」）。

⚠️ **测量面板最多同时挂 10 项，超出静默丢弃。**
实测加 13 项（CH1×8 + CH2×5），`RESults?` 只回 10 项，**最先加的 3 项无声消失**，
保留的是**最后加的 10 项**。→ 按视图拆批，每批 ≤10 项。

不可靠的键名（别硬凑）：
```
:MEASure:VHIGH? / :MEASure:VLOW?   ->  -113 Undefined header
```

### 「未完成 / 无数据」的两种成因（重要）

| 现象 | 成因 | 处理 |
|---|---|---|
| 面板一片「未完成」 | 屏幕刷新滞后的**瞬时状态** | 改一下 `:TIMebase:POSition` 就恢复 |
| STOP 态下最后加的项长期「未完成」 | 停止态面板刷新极慢，**实测约需 30–35 s** | 截图前留 ≥35 s；或直接用 `RESults?` 的真实值 |
| 有数但是错数 | 测量窗口框错了 —— 混进了总线空闲段 | 缩小时基，让窗口里只剩要测的那一段 |

典型实例：50 µs/div 全屏 500 µs 时 `FREQuency` 读 **42.03 kHz**；
缩到 10 µs/div 只框住地址字节的 9 个时钟，读数变 **99.52 kHz** —— 后者才真。
**这就是「左右微调一下」的物理含义。**

---

## 5. 游标

```
:MARKer:MODE MANual              # 初始是 OFF；不先开，设位置不生效
:MARKer:X1Y1source CHANnel1
:MARKer:X2Y2source CHANnel1
:MARKer:X1Position 34.503e-6
:MARKer:X2Position 34.817e-6
:MARKer:XDELta?                  # 读 X1->X2 时间差（时序量最可靠来源）
:MARKer:X1Position?  :MARKer:X2Position?
:MARKer:MODE OFF                 # 收尾必做，别把游标线留在交付截图里
```

**游标页的数值显示最可靠**：面板（测量页）刷新慢且会显示「无边沿」，
而光标页的 `X1 / X2 / ΔX / 1÷ΔX` 立刻显示且稳定。需要「带读数的证据截图」时优先用光标页 ——
`1÷ΔX` 还能直接当频率读数（量相邻两个 SCL 上升沿时）。
`:MARKer:Y1Position?` 返回值不可靠，**别拿它判电平**。

---

## 6. 水平缩放 / 位置与「重新采集」陷阱

```
:TIMebase:SCALe  10e-6     # 10 us/div，屏幕总窗 = 10 格 = 100 us
:TIMebase:POSition 42e-6   # 屏幕中心时刻
:STOP                      # 停止态：改上面两个只缩放已有记录
:ACQuire:COUNt?            # 采集计数，可判总线是否还在跑
```

- **`:TIMebase:DELay` 在 STOP 态只做取景平移，不会移动记录本身**；要换窗口只能改时基（或改触发点）。
- 拉大 `SCALe` 能「看见」屏幕外的数据（前提：记录本身比屏幕长）。
  实测 20→50→100 µs/div 时 SCL 边沿数 30→56→74，确实能看到更多。
- ⚠️ **拉大到超出已采跨度会触发重新采集**。判别方法：原档位取波形 → 拉大 → 缩回 → 再取 → 逐点比对。
  - 逐点一致 → 纯缩放，安全
  - 有差异（实测最大差 3.18 V）→ 触发了重采，**之前读的数和现在的不能混用**
  工具：`cmd_dump.py verify --probe-timebase 20e-6`

🚨 **数值参数不要拼双重指数**：
```python
"%.10gE-6" % 40e-6        # -> "4e-05E-6"   ❌ 触发 -131 Invalid suffix，且设置静默失效
repr(float(40e-6))        # -> "4e-05"      ✅ 纯秒数字
```
`-131` 出现时 `:TIMebase:POSition` **不会改位置**（读数看着「没反应」），
但 `:MARKer:X1Position` 恰好能被容错解析 —— 所以同一个脚本里可能「游标对了、位置没动」，极易误判。

---

## 7. 错误码速查

| 错误 | 含义 | 常见成因 |
|---|---|---|
| `-113 Undefined header` | 未定义头 | `:MEASure:ITEM`、`:MEASure:VHIGH?` 这类不存在的命令 |
| `-131 Invalid suffix` | 后缀非法 | 数值参数拼成了 `4e-05E-6` 这种双重指数 |
| `-221 Settings conflict` | 设置冲突 | 参数超出当前档位允许范围 |
| `+0,"No error"` | 无错误 | 正常 |
| `workbook is not open`（非 SCPI） | 编辑器实例 key 不对 | file_id 形式不匹配，见 SKILL.md 步骤 7 |

错误队列是**先进先出**，一个 `:SYSTem:ERRor?` 只弹一个，要循环读到 `No error` 才是干净的。
前面探命令留下的残留错误不代表当前操作失败 —— 判断「这次操作是否成功」要**看返回值**，不要只看错误队列。

---

## 8. 采集内存回看（最重要的省时机制）

### 8.1 采集内存远大于屏幕窗口

**内存跨度不是常数，每次扫之前先算：`内存 = :ACQuire:POINts? / :ACQuire:SRATe?`**

同一台机器上实测到的两种配置（用户改过采集设置后就变了）：

| 查询 | 配置 A | 配置 B |
|---|---|---|
| `:ACQuire:POINts?` | `2000000` | `1000000` |
| `:ACQuire:SRATe?` | `+2.50000000E+008`（250 MSa/s） | `+1.00000000E+009`（1 GSa/s） |
| → 内存跨度 | **8 ms** | **1 ms** |
| `:TIMebase:RANge?` | `+100.0E-06` | `+100.0E-06`（**不变**） |
| `:WAVeform:POINts?` | `+25000`（各 MODE 相同），4 ns/点 | `+12500`，8 ns/点 |

**屏幕窗口只有 100 µs → 始终只占内存的 1/80 ~ 1/10。只看屏幕会漏掉大部分数据。**

### 8.2 记录身份必须靠原始电压解码，不要信文件名/标签

同一台示波器**换档位或重采后记录会变**，桌面上的旧截图/说明可能是**另一次采集**。
判据（按优先级）：

1. 首 8 个 SCL 高相位中点的 **SDA 原始电压**（阈值取高/低电平中点）→ 逐位拼出地址。相近的地址只差 1 位，别靠「看着像」。
2. ACK 位是否全 0（分组错位时 ACK 会变成 1，是很好的自检）。
3. SCL 高电平时长（两次采集若一个 4.08 µs、一个 5.5 µs，根本不是同一段）。
4. 是否含重复 START（`SDA 跳变前 SCL 已高电平保持 ≥200 ns` 才算，防抖别省）。

### 8.3 双判据交叉验证（救命流程）

任何「是/不是」的判断（是否重采、是否有 STOP、地址对不对）都要有**两条独立判据**同时成立才下结论。

---

## 9. 拼接数据的时间步长陷阱（会导致假 FAIL）

🚨 **多窗口拼接后时间步长可能不统一 —— 任何「索引↔时间」换算都不能用固定步长。**

实测拼接结果：第一窗口 **6 ns**（9896 点）、其余窗口 **12 ns**（74988 点）。
若代码用 `dt = t[1] - t[0]`（= 6 ns）当全局步长插值、或用 `(tt - t[0]) / dt` 定位索引，
**索引会偏一倍**，SCL 高电平判定整个错位 →
**把 SCL 低电平期间的普通数据位跳变全部误判成 START**，凭空造出 **15 个 START / 13 个 STOP**，
并算出 `tSU;STA = 0.1666 µs`（假的 FAIL）。

✅ **正解**：
- 边沿检测用**逐点步长** `t[i] - t[i-1]` 插值；
- 电平判定 / 采样时刻用**时间轴二分**定位（`bisect`）。
修复后同一份数据给出 2 个 START、`tSU;STA = 5.0990 µs` PASS。

---

## 10. 截图读数的有效性判据

### 10.1 垂直削顶会伪造「更快的上升时间」

CH 垂直档过小会削顶，读出的 Tr 比真实快（如 500 mV/div 削顶读 820 ns vs 1 V/div 未削顶 1340 ns）。
**选足够大的 V/div，确认波形不过顶不过底（贴顶/贴底占比 0%）再测 Tr/Tf。**

### 10.2 参考图与新采集的读数不可直接相减

不同机台/不同探头/不同采集条件之间的读数差异属正常离散，只判各自是否合格，不做差值结论。

### 10.3 报告里写读数必须带「出处」

每条读数标注来源（游标 ΔX / `:MEASure:RESults?` / 面板），
并记录采集条件（时基、位置、探头倍率、口径）。否则数字无法追溯、无法复现。
