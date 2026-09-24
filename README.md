# Keysight 示波器 I2C 时序测量

通过 SCPI 远程操作 Keysight/Agilent InfiniiVision 示波器，完成 I2C 总线时序测量的端到端工作流：
从「屏幕上有一屏波形」到「测试报告表格里填好实测值」。

## 这个 skill 解决什么

- 示波器上抓到了 I2C 波形，要测 fSCL / tHIGH / tLOW / tSU;STA / tHD;STA / tSU;STO / tSU;DAT / tHD;DAT / Tr / Tf
- 要对照规格判 PASS/FAIL，或排查「Tr 超规格」
- 要把实测值回填进 Excel 测试报告（尤其是嵌了波形图附件的报告）

## 两条最贵的教训（都在里面）

1. **Tr/Tf 必须用 30%–70% 口径。** I²C 规范（NXP UM10204）定义如此。
   用 10%–90% 会把波形平坦尾部算进去，**数值虚高 2–3 倍，把合格板子判成超规格**。
   同一批数据：10–90% 给 1797–1921 ns（判超规格），30–70% 给 611–660 ns（合格）。

2. **含 OLE 嵌入对象的 xlsx 绝不能用本地 Office 编辑器或 openpyxl 保存。**
   实测编辑器 open→写→save 之后 `xl/embeddings/*` **全部消失**（2→0，体积掉 228 KB），
   openpyxl 重存也会掉绘图/媒体。必须走 zip/XML 层逐条复制改写。

## 安装

把 `keysight-scope-i2c/` 整个目录放到 skills 目录下：

| 作用域 | 路径 |
|---|---|
| 用户级（所有项目可用） | `~/.workbuddy/skills/keysight-scope-i2c/` |
| 项目级（随仓库共享） | `<repo>/.workbuddy/skills/keysight-scope-i2c/` |

依赖：Python 3.9+、`numpy`。可选 `rapidocr-onnxruntime`（面板读数 OCR 校验）。
`fill_excel.py` 依赖本机的本地 Office 编辑能力（无此能力时用 `xlsx_cellpatch.py` 的纯标准库路线）。

## 目录结构

```
keysight-scope-i2c/
├── SKILL.md                      # 主工作流：8 步 + 口径 + 回填 + 三条底线
├── references/
│   ├── i2c_spec.md               # I²C 标准/快速模式规格表与判定口径
│   ├── scpi-cookbook.md          # SCPI 命令速查、正确/错误形式、错误码表
│   └── pitfalls.md               # 全部实测坑位汇编（按类别组织）
└── scripts/
    ├── scope.py                  # 核心库：连接、查询、取波形、截图、测量面板
    ├── i2c_decode.py             # 事务解码 + 12 个时序量 + 规格判定
    ├── tr_measure.py             # ★ 30%–70% 规范口径 Tr/Tf 逐沿测量
    ├── i2c_workflow.py           # 一键端到端取数 → 解码
    ├── selftest.py               # 纯函数自测（24 项），判定前的强制关卡
    ├── mem_scan.py               # 扫描采集内存，锁定目标区段
    ├── cmd_measure.py            # 按参考图设档、扫位置出数、逐张截图
    ├── cmd_dump.py               # 多窗口拼接导出；验证缩放是否触发重采
    ├── xlsx_cellpatch.py         # ★ 含 OLE 的 xlsx 安全改写（zip/XML 层）
    └── fill_excel.py             # 普通 xlsx 回填（备份→写→保存→回读校验）
```

## 快速上手

```bash
cd scripts

python selftest.py                                   # ① 先自检环境（24 项）
python scope.py <SCOPE_IP>                           # ② 只读摸底，不改任何设置
python i2c_workflow.py --ip <SCOPE_IP> --tb 20e-6 --out wave.json   # ③ 取数 + 解码
python tr_measure.py --wave wave.json                # ④ Tr/Tf 按 30%-70% 复算
python tr_measure.py --wave wave.json --list         #    逐沿明细

python xlsx_cellpatch.py --file report.xlsx --sheet 时序测试 --plan plan.json  # ⑤ 回填
```

## 实测环境

Scripts verified on Keysight/Agilent InfiniiVision **DSO-X 6004A**（WinCE 内核，固件 07.55 系列），
SCPI over TCP **5025**。同指令集机型（6000X / 3000T / 2000X）应可直接使用。

## License

MIT
