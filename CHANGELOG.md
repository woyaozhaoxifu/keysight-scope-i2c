# Changelog - Keysight I2C Test Skill

All notable changes to this skill will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.2] - 2026-09-28

### Fixed
- 🚨 **地址张冠李戴 —— 一次真实误归档（本 skill 迄今最严重的问题）**
  - 本机无 I2C 触发选件（`:TRIGger:MODE I2C` → `-224 Illegal parameter value`），
    只能退化为 SCL 边沿触发。此时 `--addr` **只用于命名目录，完全不参与触发**，
    抓到哪一帧随机 —— 实测连续三次分别抓到 `0x59 → 0x58 → 0x5B`
    （四个 PSU 地址在同一条总线上轮流通信）。
  - 后果：一帧 `addr7="0x58"` 的波形被按 `0x5A` 命名归档 → 复制进桌面图集 →
    还把完整度说明更新成「0x5A 8/8 齐」。而 `decode.json` 里 `addr7:"0x58"`
    一直白纸黑字写着，只是没人核对。
  - 修：新增**地址闸门** —— `decode` / `shots` 强制比对「实测 `addr7` == `--addr`」，
    不匹配即中止（`shots` 缺 `decode.json` 直接拒绝出图）；新增 `--allow-any-addr` 显式放行。
  - 新增 **`grab` 子命令**：反复 `arm 触发 → 取数 → 解码` 直到命中目标地址。
- 🚨 **DC 电气判定：假 FAIL 与不可判定，同一处两重错**
  - **假 FAIL**：VOL/VOH 取 P98/P2，余量仅 2%，而边沿过渡点实测占 **4.6%** →
    实测把 VOL 报成 **1.457 V**，而稳态实测只有 **0.118 V**。
  - **不可判定被硬判**：2 V/div 满量程 16 V、8 bit → **LSB ≈ 67 mV**，
    是 `VOL ≤ 0.4 V` 门限的 17%；32 万个低电平采样点里只有 **14 个不同取值**。
    实测 SDA VOL = 0.4109 V 与门限只差 **11 mV < 1 LSB** —— 判 PASS/FAIL 都不成立。
  - 修：① VOL/VOH 改取 **P90 / P10**（另存 P50 典型值供对照）；
    ② 从采样值反推 LSB（唯一取值相邻间隔中位数，**分通道各估再取中位**，
    混估会算小：42.1 mV vs 66.9 mV）；③ 引入**三级判定** PASS/FAIL/**INDET**
    （`|实测 − 门限| < 1 LSB` 即不可判定）；④ `lsb_v > 10% × 最小门限` 时打印 `[!]` 提示换档。
- `redo_psu.py arm_trigger()` 首版用 `:TRIGger:STATus?` 轮询判「触发完成」→ 本机该查询必挂
  （pitfalls C5），20 轮 × 2 s 全部超时，现场表现为「每次都报未等到触发」。
  改为固定等待 + `:STOP`，由"这一屏能否解出 I2C 事务"判定有效性。
- `snapshot_original()` 中 `return 0` 之后跟着截图调用（**死代码**）→ `snapshot`
  子命令其实从未截图。已修。

### Added
- `redo_psu.py` 新增 `--ch-scale` / `--ch-offset`：一键把 SCL/SDA 切到小档位做 DC 电气测量
  （0.5 V/div → 满量程 4 V、LSB ≈ 16 mV）。
- 新增 `grab` 子命令（见上）；`all` 流程改为
  `precheck → snapshot → grab → capture → decode → cursors → shots`，
  命中不了目标地址即中止、不出图。
- `--reuse-wave`：确实想复用已有 wave.json 时跳过重新触发。
- pitfalls 新增 **C7（地址张冠李戴）**、**C8（垂直档位不够 → DC 判定不可靠）**；
  SKILL.md 新增「步骤 10 · 单器件复测 + 标准取证图集」。

### Changed
- ⚠️ **更正 [1.0.1] 的错误记录**：那里写的「真机 0x5A 首字节 0xB4 已核实」，
  其对应的 9 张取证图其实是 **0x58** 那一帧（即上条误归档）。
  已重抓真正的 0x5A 帧（`first_byte=0xB4`、6 字节、fSCL 97.49 kHz），
  重新出具 9 张图并覆盖桌面图集。

### Verified
- `selftest.py` 33 项 OK（skip 2）；`sandbox_test.py` 端到端全过。
- 真机重抓：`grab` 命中 0x5A（`first_byte=0xB4`）；地址闸门按预期拦下 0x58 帧；
  9 张图重新落盘并覆盖交付目录。

## [1.0.1] - 2026-09-28

### Fixed
- 🚨 **`:TRIGger:STATus?` 在 RUN 态下挂满 socket 超时 → 脚本假死**
  - 实测（DSO-X 6004A `10.121.136.251`, FW 07.55）：RUN 且未触发时该查询不返回，
    6 s 超时 → 6.0 s 才吐 `<timeout>`；STOP 态下秒回。
  - 配合 `q()` 默认 `retries=3` + 60 s 超时 = **卡 180 s**，现场表现为"跑了 5 分钟一行输出都没有"。
  - 新增 `Scope.q_short(c, timeout, retries)`，`state()` 已改走它：**180 s → 3.5 s**。
- 🚨 **`:SCReen:DUMP?` 不可用的机台会让出图变成「空游标图」**（2026-09-28 第二次踩）
  - 首次探测该命令会**超时 15 s 并把仪器时基搅乱**（实测 200 µs/div → 50 µs/div）。
  - 后果：① 波形被截断 ② 游标位置被钳到窗口边缘（`X1=X2=250 µs`、`ΔX=0.0 s`，
    屏幕提示「控制已达到极限」）→ 5 张游标图全是废图。
  - 修：`redo_psu.py do_shots()` 每次出图前调 `restore_tb()` 校回时基，被改动即打印 `[fix]`。

### Added
- **`Scope.screenshot_best(path)`**：自动选择可用截图路径 —— `:SCReen:DUMP?`（黑底整屏、
  X1/X2/ΔX 全渲染）优先，不可用则降级 `:DISPlay:DATA?`（白底、**只出 X2**）并打印醒目警示；
  可用性只探测一次并缓存到类变量。
- 摸底新增**电平诊断**判据（`1 V/div` + offset 2 V 读稳态电平）：
  稳定高电平 = 探头接对但总线空闲；0 V/噪声地板 = 没夹到点/没上电；50 Hz 纹波 = 地线没接好。
- **`:TRIGger:MODE I2C` 选件缺失的降级路径已实测确认**：无选件时机型返回
  `-224 Illegal parameter value` + 3×`-113 Undefined header`；改用
  `:TRIGger:EDGE:SOURce CHANnel1 / SLOPe FALLing / LEVel 1.65` + 解码首字节确认器件地址。

### Known Issues（机台相关，写进文档避免误判）
- `:SCReen:DUMP?` 在 `10.121.136.251` 上 **RUN / STOP 两态均超时**（60 s / 30 s 无字节），
  该路径（唯一能渲染 X1 游标的）在本机不可用；退回 `:DISPlay:DATA?`（白底、只出 X2）。
  文档已要求连机后先验证，不可用的路径 **如实标注，不得谎称游标已渲染**。

### Verified
- `selftest.py` 33 项 OK（skip 2）；`sandbox_test.py` 端到端全过（9 张取证截图落盘）。
- **真机端到端（0x5A 重测）**：首字节 `0xB4` → 地址 0x5A 已核实；13 项时序量全 PASS；
  出图 9 张，游标面板 ΔX 与数据侧计算逐项吻合（`04a` 297.5 ns ↔ 0.2975 µs、
  `05` 5.104 µs ↔ 5.104 µs）。
- ⚠️ **上一条已被 [1.0.2] 更正**：实际归档的那 9 张图是 **0x58** 那一帧
  （边沿触发随机命中，且没人复核 `addr7`），并非 0x5A。原文保留，以记录当时的认知状态。

## [1.0.0] - 2026-09-25

### Added
- **完整 I2C 时序测试框架**：端到端支持 Keysight 示波器 I2C 测量
  - SCPI 远程控制（TCP 5025）
  - 1ns 高分辨率波形取数
  - I2C 事务解码（START/STOP/Sr/地址/ACK/NACK）
  - 13 个时序参数测量与规格判定
  - Tr/Tf 按 30%-70% 规范口径（NXP UM10204）
  - Excel 报告安全回填（含 OLE 嵌入对象保护）
  - OCR 截图验收

- **三档规格支持**
  - `std`: I2C 标准模式 100 kHz
  - `fm`: I2C 快速模式 400 kHz
  - `smbus`: SMBus/PMBus（电源兼容性测试场景）

- **完整工具链**
  - `scope.py`: SCPI 客户端（含 `Scope` 类、`ScopeError`、`open_scope`、波形取数、截图、测量面板）
  - `i2c_decode.py`: I2C 事务解码器（START/Sr/STOP/地址/ACK + 时序参数 + 规格判定）
  - `i2c_full_test.py`: 一键流水线（connect/check/capture/decode/cursors/shots/verify/fill/run）
  - `tr_measure.py`: Tr/Tf 逐沿测量（30%-70% 口径，MAD 离群剔除）
  - `i2c_workflow.py`: 连接 → 1ns 取数 → 存 JSON → 调解码器出结果
  - `selftest.py`: 纯函数自测（33 项，含 DC/拉伸/毛刺/Sr）
  - `mock_scope.py`: 仿真示波器（TCP 5051，含时钟拉伸/毛刺/重复START/PNG 截图）
  - `sandbox_test.py`: 沙箱端到端验证（起 mock 跑全套链路，断言时序合理性）
  - `synthetic_verify.py`: 合成已知答案波形对拍器（含五条红线检查）
  - `mem_scan.py`: 采集内存全段扫描（定位真实 I2C 活动区，跳过空闲段）
  - `cmd_measure.py`: 逐时基档扫水平位置、抓测量面板读数并截图
  - `cmd_dump.py`: 多窗口拼接导出整段波形 + 验证缩放是否触发重采
  - `xlsx_cellpatch.py`: 含 OLE 的 xlsx 安全改写（zip/XML 层，保留嵌入对象）
  - `fill_excel.py`: 普通 xlsx 回填（tencent-local-office-edit EDSDK 路线）
  - `full_lowspeed_test.py`: 全量低速测试覆盖报告（DC + 时钟 + 6 时序量×三档 + Tr/Tf + 拉伸/毛刺/Sr）
  - `redo_psu.py`: PSU 复测脚本（取数后强制削顶自检 + 标准 8+1 张取证图一键出齐）

- **技术文档**
  - `SKILL.md`: 完整技能说明（含 YAML frontmatter、文件索引、快速启动入口，457 行）
  - `README.md`: 能力总览与快速开始（94 行）
  - `references/i2c_spec.md`: I2C 规格表（92 行）
  - `references/pitfalls.md`: 实测坑位汇编（187 行）
  - `references/scpi-cookbook.md`: SCPI 命令手册（360+ 行）

- **快速启动**
  - `run_i2c_test.py`: 统一快速启动入口（argparse 子命令结构，9 子命令；`--ip` 强制显式传参，无隐式默认值）
  - `requirements.txt`: Python 依赖清单
  - `VERSION`: 版本跟踪
  - `CHANGELOG.md`: 变更日志

### Features
- **自动化配置**: `setup` 子命令一键 AutoScale + I2C 触发
- **批量测试**: `batch` 子命令支持多器件地址批量回填
- **游标取证**: `cursors` + `shots` 子命令生成可审计截图
- **离线验证**: 沙箱模式无需真机即可验证代码链路
- **已知答案对拍**: `synthetic_verify.py` 验收任意 I2C 测试实现

### Coverage
| 层 | 内容 | 状态 |
|---|---|---|
| 电气 DC | VOH/VOL/VIH/VIL、Cb | ✅ |
| 时序 | fSCL/tHIGH/tLOW、tSU;STA/tHD;STA/tSU;STO/tHD;DAT/tSU;DAT/tBUF、Tr/Tf | ✅ |
| 协议逻辑 | 地址/R-W、ACK/NACK、Sr、时钟拉伸、毛刺检测 | ✅ |
| 功能/健壮 | 设备扫描、寄存器读写、总线卡死恢复 | ⚠️ 需 I2C 主控制器 |

### Resolved Issues
- **A1**: Tr/Tf 口径修正（10%-90% → 30%-70%）
- **A2**: 上升时间窗口 bug 修复（固定小窗 → 动态 6000 样本窗）
- **A3**: 边沿插值使用逐点实际步长（非均匀采样兼容）
- **B1**: POINts MAX 偏小问题（显式设 100000）
- **B2**: DELay 超内存 POINts=0 卡死问题
- **B3**: 双重指数格式拼写错误
- **B4**: 拼接数据步长不统一导致 START 误判
- **C1**: 冻结记录上改设置无效
- **C2**: 带宽限制按钮高亮 ≠ 生效
- **C3**: PROBe 改不动（部分 WinCE 固件限制）
- **D1**: RUN 覆盖用户冻结波形
- **E1**: 含 OLE 的 xlsx 不能用编辑器保存
- **E2-E7**: 合并单元格、sheet_id、句柄占用等 Excel 坑

### Documentation
- 完整工作流步骤说明（8 步）
- 规范口径说明（30%-70%）
- 三条不可越的底线
- 判 Tr 超规格前的自查顺序（5 步）
- 五条红线（合成验证标准）

### Dependencies
```
numpy>=1.20.0               # 核心依赖
openpyxl>=3.0.0             # Excel 操作（含 OLE 保护路径）
rapidocr-onnxruntime>=1.2.0 # OCR 截图验收（可选，推荐；自带模型无需 tesseract）
```

### Supported Instruments
- Keysight/Agilent InfiniiVision DSO-X 6000X 系列
- Keysight/Agilent InfiniiVision MSO-X 3000T 系列
- Keysight/Agilent InfiniiVision DSO-X 2000X 系列
- 其他同 SCPI 指令集机型

### Known Limitations
- `tVD;DAT` / `tVD;ACK`（驱动端输出延迟）无法测量；`tHD;DAT`/`tSU;DAT` 间接覆盖数据有效窗口
- `VHYS`（输入迟滞）需主动扫描阈值
- SMBus `tTIMEOUT` / `tLOW:SEXT` / `tLOW:MEXT`（累计/超时项）需协议层/长时间抓包验证
- 手动游标 X1 在部分固件上可能不渲染（但数据全对）
- `fill_excel.py` 回填依赖 `tencent-local-office-edit`（Windows 特有外部 CLI，Python pip 装不了；另有一条含 OLE 的 zip/XML 层补丁路线 `xlsx_cellpatch.py` 纯 Python，无需此 CLI）
