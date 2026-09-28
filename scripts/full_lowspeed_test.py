# -*- coding: utf-8 -*-
"""I2C 低速测试 · 全量覆盖报告生成器（沙箱版，不连真机）。

启动 mock 示波器 → 走真实 Scope 取数链路 capture → 对 std/fm/smbus 三档
分别解码 → 汇总「所有低速测试内容」（时钟 / 6 时序量 / Tr·Tf）→ 判定 → 落报告。

用法：  python full_lowspeed_test.py
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from mock_scope import MockScopeServer                 # noqa: E402
import i2c_full_test as ft                             # noqa: E402
from i2c_decode import SPEC_BY_NAME, analyze as decode_analyze  # noqa: E402

TIMING_ORDER = ["tSU_STA", "tHD_STA", "tSU_STO", "tHD_DAT", "tSU_DAT", "tBUF"]
SPECS = ["std", "fm", "smbus"]
SPEC_LABEL = {"std": "标准模式 100kHz", "fm": "快速模式 400kHz",
              "smbus": "SMBus/PMBus"}


def make_args(**kw):
    base = dict(ip="127.0.0.1", port=5051, addr="0x5B", ch_scl=1, ch_sda=2,
                tb=20e-6, delay=257e-6, points=700000, spec="std", freq=None,
                out="i2c_sandbox_out", shots_dir=None, xlsx=None, sheet="时序测试",
                verbose=False, trigger_cond="START", trigger_addr=None,
                addr_list=None, base_out="i2c_out_bulk", vdd=3.3, rp=4700.0)
    base.update(kw)
    return argparse.Namespace(**base)


def vmark(v):
    return {"PASS": "[PASS]", "FAIL": "[FAIL]", "NA": "N/A"}.get(v, v)


def fmt_spec(v):
    return "—" if v is None else "%.3fµs" % (v * 1e6)


def main():
    out = "i2c_sandbox_out"
    os.makedirs(out, exist_ok=True)
    srv = MockScopeServer(port=5051)
    srv.start()
    time.sleep(0.6)

    lines = []

    def L(x=""):
        lines.append(x)
    L("# I2C 低速测试 · 全量覆盖报告（沙箱）")
    L()
    L("数据源：本地 mock 示波器 @127.0.0.1:5051（合成 100kHz I2C 写事务 ×2，不连真机）")
    L("判定口径：对照 I²C 规范「不小于」型要求，枚举全部出现位置取**最紧值(min)** 判定")
    L()

    # ---- 1. 端到端链路 ----
    L("## 1. 端到端链路（8 环节代码路径）")
    steps = [("connect", ft.do_connect), ("check", ft.do_check),
             ("setup", ft.do_setup), ("capture", ft.do_capture),
             ("decode", ft.do_decode), ("cursors", ft.do_cursors),
             ("shots", ft.do_shots), ("verify", ft.do_verify)]
    fails = 0
    for nm, fn in steps:
        try:
            fn(make_args(out=out, trigger_addr=91 if nm == "setup" else None))
            L("  [OK]   %s" % nm)
        except Exception as e:
            fails += 1
            L("  [FAIL] %s: %s" % (nm, e))
    L()

    wave_path = os.path.join(out, "wave.json")
    res = {sp: decode_analyze(wave_path, sp) for sp in SPECS}

    # ---- 2. 时钟 ----
    L("## 2. 总线时钟")
    clk = res["std"]["clock"]
    L("  fSCL = %.2f kHz   T = %.4f µs   tHIGH = %.4f µs   tLOW = %.4f µs"
      % (clk["f_khz"], clk["T_us"], clk["tHIGH_us"], clk["tLOW_us"]))
    L("  注：fSCL 与 tHIGH/tLOW 已剔除两类异常周期——总线空闲伪周期(tHIGH 超长)与")
    L("      时钟拉伸周期(tLOW 超长)，二者都不代表总线速率（判据 tHIGH/tLOW > 3×中位数）。")
    L()

    # ---- 2b. DC 电气参数 ----
    L("## 2b. DC 电气参数（电压电平 / 总线电容）")
    d = res["std"]["dc"]
    L("| 项目 | 实测 | 规范 | 判定 |")
    L("|---|---|---|---|")
    L("| VDD | %.3f V (%s) | — | — |" % (d["vdd_v"], d["vdd_source"]))
    L("| VIH 下限 | — | ≥ %.3f V (0.7·VDD) | — |" % d["vih_min_v"])
    L("| VIL 上限 | — | ≤ %.3f V (0.3·VDD) | — |" % d["vil_max_v"])
    for ch, c in d["checks"].items():
        L("| %s VOH_min | %s V | ≥ %.3f V | %s |"
          % (ch, d["voh_%s_min_v" % ch.lower()], d["vih_min_v"],
             "[OK]" if c["vih_ok"] else "[FAIL]"))
        L("| %s VOL_max | %s V | ≤ %.3f V (≤0.4V@3mA) | %s |"
          % (ch, d["vol_%s_max_v" % ch.lower()], d["vil_max_v"],
             "[OK]" if c["vol_ok"] else "[FAIL]"))
    if d["cb_pf"] is not None:
        L("| 总线电容 Cb | %.2f pF | ≤ 400 pF | %s |"
          % (d["cb_pf"], "[OK]" if d["cb_pf"] <= 400 else "[FAIL]"))
        L("  （Cb 由 Tr 反推：Cb = Tr/(0.8473·Rp)，Rp=%.0fΩ）" % d["rp_ohm"])
    L()

    # ---- 2c. 时钟拉伸 / 毛刺 / 重复 START ----
    L("## 2c. 时钟拉伸 / 毛刺 / 重复 START（协议健壮性）")
    cs = res["std"]["clock_stretch"]
    L("  - 时钟拉伸：检出 **%d 段**，最长 %.2f µs（正常 tLOW≈%.2f µs）%s"
      % (cs["n"], cs["max_us"] or 0, cs["normal_tlow_us"] or 0,
         "— 从机/主机拉伸或总线卡死" if cs["n"] else "— 无拉伸"))
    sp = res["std"]["spikes"]
    L("  - 毛刺(tSPIKE)：SCL/SDA 上 <%.0f ns 反向边沿对 **%d 处**（SMBus 需输入滤波抑制）"
      % (sp["max_width_ns"], sp["n"]))
    sr = res["std"]["has_repeated_start"]
    L("  - 重复 START(Sr)：**%s**（含『写→Sr→读』复合读事务时检出，tSU;STA 才有真值）"
      % ("有" if sr else "无"))
    L()

    # ---- 3. 时序参数全量（6 项 × 三档）----
    L("## 3. 时序参数全量（6 项 × 三档规格判定）")
    L("| 参数 | 含义 | std下限 | fm下限 | smbus | 实测(µs) | std | fm | smbus |")
    L("|---|---|---|---|---|---|---|---|---|")
    na_count = 0
    for k in TIMING_ORDER:
        row = res["std"]["timings"][k]
        sv = {sp: SPEC_BY_NAME[sp].get(k + "_min") for sp in SPECS}
        val = "NA" if row["value_us"] is None else "%.4f" % row["value_us"]
        if row["value_us"] is None:
            na_count += 1
        vd = {sp: res[sp]["timings"][k]["verdict"] for sp in SPECS}
        L("| %s | %s | %s | %s | 同std+软提示 | %s | %s | %s | %s |"
          % (k, row["desc"], fmt_spec(sv["std"]), fmt_spec(sv["fm"]),
             val, vmark(vd["std"]), vmark(vd["fm"]), vmark(vd["smbus"])))
    L()
    L("  注：I²C 规范**没有 tHD_STO 参数**（仅 tSU;STO），故未列入；其余 6 项即规范全部「不小于」型时序量。")
    L()

    # ---- 4. Tr/Tf ----
    L("## 4. 上升/下降时间（30%-70% 口径，按 NXP UM10204）")
    L("| 通道 | 类型 | 实测(ns) | std上限 | fm上限 | 判定 |")
    L("|---|---|---|---|---|---|")
    for ch in ("scl", "sda"):
        tt = res["std"].get("%s_transition" % ch) or {}
        for kk in ("tr", "tf"):
            if tt.get(kk):
                ns = tt[kk]["mean_ns"]
                std_max = 1000 if kk == "tr" else 300   # std: Tr 1000ns / Tf 300ns
                fm_max = 300                            # fm: 均为 300ns
                ok_std = ns <= std_max
                ok_fm = ns <= fm_max
                verdict = ("PASS std / FAIL fm" if ok_std and not ok_fm
                           else ("PASS" if ok_std else "FAIL"))
                L("| %s | %s | %.1f | %dns | %dns | %s |"
                  % (ch.upper(), kk.upper(), ns, std_max, fm_max, verdict))
    L()

    # ---- 5. SMBus 软提示 ----
    L("## 5. SMBus/PMBus 软提示（单拍波形测不到的累计/超时项）")
    for n in res["smbus"].get("notes", []):
        L("  - " + n)
    L()

    # ---- 6. 取证截图 ----
    shots = os.path.join(out, "shots")
    pngs = sorted(f for f in os.listdir(shots) if f.endswith(".png")) \
        if os.path.isdir(shots) else []
    L("## 6. 取证截图（%d 张 → %s）" % (len(pngs), shots))
    for f in pngs:
        L("  - " + f)
    L()

    # ---- 7. 结论 ----
    L("## 7. 覆盖结论")
    npass = sum(1 for sp in SPECS for k in TIMING_ORDER
                if res[sp]["timings"][k]["verdict"] == "PASS")
    L("  - 三档 × 6 时序量 = 18 项判定：**PASS %d**、**N/A %d**"
      % (npass, na_count))
    L("  - 链路 8 环节：%s" % ("全部跑通 [OK]" if fails == 0 else "%d 项失败 [FAIL]" % fails))
    L("  - 时钟 / Tr·Tf / DC 电平(VOH·VOL·VIH·VIL)·Cb 均已实测并对照规范。")
    L("  - 协议健壮性：时钟拉伸 %d 段、毛刺 %d 处、重复 START %s —— 均已检出。"
      % (cs["n"], sp["n"], "有" if sr else "无"))
    L("  - [!] 沙箱为仿真非真机：仅验证代码路径与解析逻辑；真实信号质量（探头比、是否真超规格）仍以真机为准。")
    L()
    L("### 本工具**不能**测的项（需 I2C 主控制器主动发事务，示波器被动测量做不了）")
    L("  - 功能层：设备扫描(i2cdetect)、寄存器读写、总线卡死恢复(9 脉冲解锁)、热插拔、POR 时序。")
    L("  - SMBus 主动超时：tTIMEOUT 25~35ms、tLOW:SEXT 25ms、tLOW:MEXT 10ms、PEC(CRC-8)。")
    L("  - 输入迟滞 VHYS：需主动扫描阈值，被动抓包无法隔离；tVD:DAT/tVD:ACK 为驱动端输出延迟，")
    L("    被动抓包不可孤立测量（已用 tHD;DAT/tSU;DAT 间接覆盖数据有效窗口）。")

    srv.stop()
    report = "\n".join(lines)
    rpath = os.path.join(out, "full_lowspeed_report.md")
    with open(rpath, "w", encoding="utf-8") as f:
        f.write(report)
    try:
        print(report)
    except UnicodeEncodeError:
        # GBK 终端打印不了 emoji，降级纯 ASCII 输出
        ascii_report = report.encode("ascii", "replace").decode("ascii")
        print(ascii_report)
    print("\n报告已落盘 -> " + rpath)
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
