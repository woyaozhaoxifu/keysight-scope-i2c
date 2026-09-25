#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
合成已知答案波形回归验证器（不连真机）
=====================================
用途：验收 / 自检任何 I2C 测试代码（自己写的或别人/别的 Agent 生成的）。
原理：造一段「标准答案完全已知」的 I2C 波形，喂进被测实现，逐项对照真值。
      不需要示波器，纯函数级验证，秒级出结果。

用法：
    # 只打印真值，供手工对照
    python synthetic_verify.py

    # 指向任意实现目录（含 timing_test.py / voltage_test.py 等同名模块），自动对拍
    python synthetic_verify.py --suite "D:/path/to/impl"

    # 加毛刺 / 换速度模式 / 换占空比 / 记录从总线中间开始
    python synthetic_verify.py --suite <dir> --glitch 200ns --mode fast --duty 30 --mid-start

为什么需要它（实测教训）：
    一份"看起来完整"的 I2C 测试套件，12 项时序里可能只有 2 项真的在测量，
    其余是返回 0 的桩函数 —— 其中一半恒 PASS（假合格）、一半恒 FAIL（假不合格），
    主程序还可能压根跑不完。**不做已知答案对拍，这些一个都发现不了。**

五条红线（对拍时重点看）：
    1. fSCL 是否差 2 倍（用半周期当周期的经典错法）
    2. Tr/Tf 是否四个量都测（SCL/SDA × 上升/下降），口径是否 30%-70%
    3. 未实现的项必须报 N/A，**绝不允许用恒 0 或桩函数报 PASS**
    4. 记录起点相位改变（--mid-start）或插入毛刺（--glitch）后结果是否稳定
    5. 数据不足时是否报 N/A（而不是 fSCL=0 还判 PASS）
"""

import argparse
import os
import sys
import numpy as np

VDD = 3.3


# ---------------------------------------------------------------- 波形生成
def _rc_wave(edges, n, fs, tr, vdd=VDD):
    """按边沿时刻生成含有限上升/下降时间的方波（RC 充电曲线）。

    约定：edges[k] = (t_k, lev_k) 表示「t_k 时刻电平变为 lev_k」，
    edges[0] 是 t=0 的初始状态（不是跳变）。
    30%-70% 口径下，t(30%)→t(70%) ≈ 0.8473 * tau。
    """
    tau = tr / 0.8473
    wave = np.zeros(n + int(10 * tr * fs) + 10)
    L = len(wave)
    # 初始段 [t0, t1) 恒为 lev0（★ 不能当作一次跳变，否则整段波形相位全错）
    wave[:min(int(edges[1][0] * fs), L)] = edges[0][1] * vdd
    for i in range(1, len(edges) - 1):
        t_i, lev_i = edges[i]
        t_n, lev_n = edges[i + 1]
        prev = edges[i - 1][1]
        i0, i1 = int(t_i * fs), min(int(t_n * fs), L)
        if i1 <= i0:
            continue
        t = np.arange(i1 - i0) * (1.0 / fs)
        if lev_i == prev:
            wave[i0:i1] = lev_i * vdd
        else:
            tgt, src = lev_i * vdd, prev * vdd
            wave[i0:i1] = tgt + (src - tgt) * np.exp(-t / tau)
    wave[int(edges[-1][0] * fs):] = edges[-1][1] * vdd
    return wave[:n].copy()


def build(mode="standard", duty=50, tr=300e-9, n_cycles=40, fs=100e6,
          glitch=None, mid_start=False):
    """生成 SCL/SDA 波形 + 标准答案。

    Returns: dict(scl, sda, xinc, truth, feasible)
    """
    period = 10e-6 if mode == "standard" else 2.5e-6   # 100 kHz / 400 kHz
    duty = duty / 100.0
    hi_t, lo_t = period * duty, period * (1 - duty)

    # --- SCL
    edges, t, level = [(0.0, 0 if not mid_start else 1)], 0.0, (0 if not mid_start else 1)
    for _ in range(n_cycles):
        t += hi_t if level == 1 else lo_t
        level = 1 - level
        edges.append((t, level))
    n = int((t + 20e-6) * fs)
    scl = _rc_wave(edges, n, fs, tr)

    # --- SDA：数据只在 SCL 低电平期间变化（合法 I2C），首尾插 START/STOP
    sda_edges = [(0.0, 1)]
    for c in range(n_cycles):
        tc = c * period
        sda_edges.append((tc + hi_t + 0.2e-6, 1 if c % 2 == 0 else 0))
    sda_edges.append((n_cycles * period + 2e-6, 0))   # SDA 下降于 SCL 高 → START
    sda_edges.append((n_cycles * period + 4e-6, 1))   # SDA 上升于 SCL 高 → STOP
    sda = _rc_wave(sda_edges, n, fs, tr)

    # --- 可选：插入真实毛刺（窄脉冲）
    if glitch:
        g = int(glitch * fs)
        pos = int((n_cycles // 2) * period + hi_t * 0.5 * fs) - g // 2
        scl[pos:pos + g] = VDD if scl[pos] < VDD / 2 else 0.0

    if mid_start:
        # 把记录整体左移，使首沿变为下降沿（考验边沿配对是否与相位无关）
        shift = int(0.3 * period * fs)
        scl = np.roll(scl, -shift)
        sda = np.roll(sda, -shift)

    truth = {
        "fSCL": 1.0 / period,
        "tHIGH": hi_t,
        "tLOW": lo_t,
        "Tr(SCL)": tr,
        "Tf(SCL)": tr,
        "Tr(SDA)": tr,
        "Tf(SDA)": tr,
        "VCC": VDD,
        "mode": mode,
        "duty": duty,
    }
    return dict(scl=scl, sda=sda, xinc=1.0 / fs, truth=truth)


def feasible(xinc, truth):
    """标注哪些量在当前采样率下物理上测不了（避免用不可测项下结论）。"""
    nyq = 2 * xinc
    notes = []
    if truth["tHIGH"] < 20 * xinc:
        notes.append("tHIGH")
    if truth["Tr(SCL)"] < 20 * xinc:
        notes.append("Tr/Tf")
    return notes


# ---------------------------------------------------------------- 对拍
def _expected_seconds(param, truth):
    """把被测参数名映射到「以秒为单位的期望值」；fSCL 返回频率 Hz。"""
    p = param.upper()
    if p.startswith("FSCL"):
        return truth["fSCL"]
    if p.startswith("THIGH"):
        return truth["tHIGH"]
    if p.startswith("TLOW"):
        return truth["tLOW"]
    if p.startswith("TR"):
        return truth["Tr(SCL)"]
    if p.startswith("TF"):
        return truth["Tf(SCL)"]
    return None


def _to_unit(value_s, unit):
    """秒 → 被测实现所声明的单位。"""
    u = (unit or "").strip().lower()
    if u in ("ns",):
        return value_s * 1e9
    if u in ("us", "µs"):
        return value_s * 1e6
    if u in ("ms",):
        return value_s * 1e3
    return value_s


def compare(suite_dir, wave):
    """把波形喂给被测实现，逐项对照真值。"""
    sys.path.insert(0, suite_dir)
    print(f"\n被测实现: {suite_dir}")
    truth = wave["truth"]
    try:
        from i2c_spec_reference import I2CMode
        from timing_test import TimingTest
    except Exception as e:
        print(f"  !!! 无法导入被测模块: {type(e).__name__}: {e}")
        return 1

    mode = I2CMode.FAST_MODE if truth["mode"] == "fast" else I2CMode.STANDARD_MODE
    tt = TimingTest(mode)
    if hasattr(tt, "set_sample_rate"):
        tt.set_sample_rate(1.0 / wave["xinc"])
    try:
        results = tt.measure_all(wave["scl"], wave["sda"], wave["xinc"])
    except Exception as e:
        print(f"  !!! 测量过程崩溃: {type(e).__name__}: {e}")
        return 1

    print(f"\n{'参数':<10}{'标准答案':>14}{'实测值':>14}{'判定':>7}   结论")
    print("-" * 74)
    bad = 0
    for r in results:
        unit = getattr(r, "unit", "") or ("Hz" if r.param.upper().startswith("FSCL") else "ns")
        exp_s = _expected_seconds(r.param, truth)
        exp = _to_unit(exp_s, unit) if exp_s else None

        verdict = "—"
        if exp is None:
            if r.value == 0 and r.status == "PASS":
                verdict = "⚠ 恒 0 报 PASS（假合格）"
                bad += 1
        elif r.value == 0:
            verdict = "⚠ 疑似桩函数（恒 0）"
            if r.status == "PASS":
                verdict += " 且报 PASS（假合格）"
            bad += 1
        else:
            ratio = r.value / exp if exp else 0
            if abs(ratio - 1) < 0.15:
                verdict = "✅ 与真值一致"
                # 值与真值一致，却与实现自己打印的规格区间自相矛盾 → 判据有 bug
                lo, hi = getattr(r, "min_spec", None), getattr(r, "max_spec", None)
                if r.status == "FAIL" and lo is not None and hi is not None and lo <= r.value <= hi:
                    verdict += " ⚠ 与自身规格区间矛盾（判据 bug）"
                    bad += 1
            elif 1.5 < ratio < 2.5:
                verdict = "❌ 疑似差 2 倍（半周期当周期）"
                bad += 1
            else:
                verdict = f"❌ 偏差 {(ratio-1)*100:+.0f}%"
                bad += 1

        exp_s_str = f"{exp:.4g} {unit}" if exp else "—"
        print(f"{r.param:<10}{exp_s_str:>14}{r.value:>14.4f}{r.status:>7}   {verdict}")

    print("-" * 74)
    print(f"可疑项: {bad} 个")
    return 0


def main():
    ap = argparse.ArgumentParser(description="合成已知答案波形回归验证器")
    ap.add_argument("--suite", help="被测实现目录（含 timing_test.py 等）")
    ap.add_argument("--mode", choices=["standard", "fast"], default="standard")
    ap.add_argument("--duty", type=float, default=50, help="SCL 高电平占空比 %%")
    ap.add_argument("--tr", default="300ns", help="上升/下降时间，如 300ns")
    ap.add_argument("--glitch", default=None, help="插入毛刺宽度，如 200ns")
    ap.add_argument("--mid-start", action="store_true", help="记录从总线中间开始（首沿为下降沿）")
    args = ap.parse_args()

    def _t(s):
        return float(s[:-2]) * 1e-9 if s.endswith("ns") else float(s)

    wave = build(mode=args.mode, duty=args.duty, tr=_t(args.tr),
                 glitch=_t(args.glitch) if args.glitch else None,
                 mid_start=args.mid_start)

    t = wave["truth"]
    print("=" * 70)
    print("合成波形标准答案（供对照）")
    print("=" * 70)
    print(f"  I2C 模式      : {t['mode']}（{t['fSCL']/1000:.0f} kHz）")
    print(f"  fSCL          : {t['fSCL']:.0f} Hz")
    print(f"  tHIGH / tLOW  : {t['tHIGH']*1e9:.0f} ns / {t['tLOW']*1e9:.0f} ns（占空比 {t['duty']*100:.0f}%）")
    print(f"  Tr/Tf (30-70%): {t['Tr(SCL)']*1e9:.0f} ns（SCL 与 SDA 四个量相同）")
    print(f"  VCC           : {t['VCC']:.1f} V")
    print(f"  采样间隔      : {wave['xinc']*1e9:.1f} ns/点，共 {len(wave['scl'])} 点")
    if args.glitch:
        print(f"  ⚠ 已插入 {args.glitch} 毛刺：正确实现应检出（>tSP 且非有效时钟）")
    if args.mid_start:
        print("  ⚠ 记录从总线中间开始：tHIGH/tLOW 必须与首沿相位无关")
    notes = feasible(wave["xinc"], t)
    if notes:
        print(f"  ⚠ 当前采样率下 {'/'.join(notes)} 逼近分辨率极限，判定需谨慎")

    if args.suite:
        return compare(args.suite, wave)
    print("\n（未指定 --suite，仅输出标准答案）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
