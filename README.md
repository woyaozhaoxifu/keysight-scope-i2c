# Keysight I2C Timing Test Skill

面向 **Keysight / Agilent InfiniiVision 系列示波器**（TCP 5025 SCPI，DSO-X 6000X / 3000T / 2000X 等 WinCE 机型）的 **I2C 时序测量与合规判定**工具集。

把原本要在示波器屏幕上手动点测量、手动卡光标、手动比对规格书的活儿，变成**一条命令自动跑完**：取数 → 解码 → 13 类参数 → 对照规格库判定 → 截图取证 → 回填 Excel。

## 能力总览

### 1. 自动取数
连示波器 → 设时基/通道 → 抓 SCL/SDA 波形（WORD 数据）+ 截屏取证，全走 SCPI 脚本。

### 2. 协议解码
从波形自动解出：START/STOP、**重复 START(Sr)**、字节流、**7 位地址 + R/W 位**、**ACK/NACK**、时钟统计。

### 3. 时序参数（对照三档规格自动判 PASS/FAIL）
`fSCL` / `tHIGH` / `tLOW` / `tSU;STA` / `tHD;STA` / `tSU;STO` / `tHD;DAT` / `tSU;DAT` / `tBUF` / `Tr` / `Tf`（30%–70% 口径，按 NXP UM10204）。

规格库：`std`（标准模式 100 kHz）、`fm`（快速模式 400 kHz）、`smbus`（SMBus / PMBus）。

### 4. DC 电气参数
`VOH` / `VOL` / `VIH`(≥0.7·VDD) / `VIL`(≤0.3·VDD) 判定，以及由 Tr 反推**总线电容 Cb = Tr/(0.8473·Rp)**（≤400 pF 规范）。

### 5. 协议健壮性
- **时钟拉伸**检测（SCL 低电平超长段）
- **毛刺 / tSPIKE** 检测（<50 ns 反向边沿对）
- **重复 START(Sr)** 真实验证

### 6. 截图取证 + OCR 验收 + 批量回填
自动截测量面板/各类游标图并 OCR 验收；多从机地址批量跑并安全回填 Excel（含 OLE 保持）。

### 7. 一对一流水线
`i2c_full_test.py` 参数化一条龙：connect → check → setup → capture → decode → cursors → shots → verify（+ fill / run）。

### 8. 无真机沙箱验证 ⭐
不连示波器也能端到端验证在线链路与解析逻辑：
- `scripts/mock_scope.py` —— 本地 TCP(5051) 仿真示波器（最小 SCPI 子集 + 合成 I2C 波形 + PNG）
- `scripts/sandbox_test.py` —— 起 mock 跑全套在线链路并做合理性断言
- `scripts/full_lowspeed_test.py` —— 生成「低速测试全量覆盖报告」

## 快速开始

```bash
cd scripts

# 1) 判定前必须先跑：纯函数自测（33 项）
python selftest.py

# 2) 无真机沙箱：端到端跑通在线链路
python sandbox_test.py

# 3) 全量覆盖报告（不连真机）
python full_lowspeed_test.py

# 4) 连真机一键流水线
python i2c_full_test.py --ip <示波器IP> --addr 0x5B --spec std --out D:/_scope_tmp/0x5B
```

## 覆盖范围与边界

| 层 | 内容 | 本工具 |
|---|---|---|
| ① 电气 DC | VOH/VOL/VIH/VIL、Cb | ✅ 可测 |
| ② 时序 | 时钟 + 6 时序量 + Tr/Tf | ✅ 可测 |
| ③ 协议逻辑 | 地址/R-W、ACK/NACK、Sr、时钟拉伸 | ✅ 可测 |
| ④ 功能/健壮 | 设备扫描、寄存器读写、总线卡死恢复、热插拔、POR | ❌ 需 I2C 主控制器主动发事务 |

**明确测不了**：`tVD;DAT` / `tVD;ACK`（驱动端输出延迟，被动抓包无法孤立测量）、输入迟滞 `VHYS`、SMBus 主动超时项（`tTIMEOUT` / `tLOW:SEXT` / `tLOW:MEXT` / PEC）—— 这些要配 I2C 主控 / 总线分析仪。

⚠️ **沙箱是仿真，不是真机**：只验证代码路径与解析逻辑；数值合规以真机为准。

## 三条不可越的底线

1. **测不了就说测不了（NA）**，绝不用别的边沿凑数、绝不编造读数。
2. **数据被污染时（探头比不匹配 / 窗口含空闲段）不覆盖原值**，只写 Notes 并给复测方案。
3. **下「不合格」结论前先跑 `selftest.py`**，确认不是自己算错。

## 环境

- Python 3.10+（用到 `statistics`、`bisect`）
- 依赖：`numpy`；可选 `rapidocr-onnxruntime`（截图 OCR 验收）、`openpyxl`（Excel 回填）
- 真机：Keysight/Agilent InfiniiVision 系列，SCPI over TCP 5025

## 目录

```
SKILL.md                 能力说明与完整工作流
scripts/                 全部脚本
references/              I²C 规格表 / SCPI 手册 / 坑位汇编
```
