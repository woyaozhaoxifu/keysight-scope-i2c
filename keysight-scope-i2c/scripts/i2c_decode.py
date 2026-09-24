# -*- coding: utf-8 -*-
"""从导出的波形 JSON 里解码 I2C 事务并算时序量。

输入 JSON 支持两种格式（自动识别）：
  * cmd_dump.py dump 产出的 {merged: {t, ch1, ch2}, windows: [...]}
  * 简单格式 {t0, xinc, ch1/ch2_y 或 y} / {CH1:{xinc,xorg,xref,y}, CH2:{...}}

输出：事务结构（START/STOP/字节/ACK）、时钟统计、五个时序量的**所有出现位置**、
规格判定。判定只对"本段真实存在的事件"出结论；本段没有的事件一律 NA，
**绝不用别的边沿硬凑一个数**。

用法：
    python i2c_decode.py --wave D:\\_scope_tmp\\wave_merged.json \
        --json D:\\_scope_tmp\\decode.json
"""

import argparse
import json
import math
import os
import sys

# I2C Standard-mode / Fast-mode 规格（秒）。按被测总线实际速率选一组。
SPEC_STD = {          # Standard-mode 100 kHz
    "f_min": 0.0, "f_max": 100e3,
    "tHIGH_min": 4.0e-6, "tLOW_min": 4.7e-6,
    "tSU_STA_min": 4.7e-6, "tHD_STA_min": 4.0e-6,
    "tSU_STO_min": 4.0e-6, "tSU_DAT_min": 250e-9, "tHD_DAT_min": 0.0,
    "tr_max": 1000e-9, "tf_max": 300e-9,
}
SPEC_FM = {           # Fast-mode 400 kHz
    "f_min": 0.0, "f_max": 400e3,
    "tHIGH_min": 0.6e-6, "tLOW_min": 1.3e-6,
    "tSU_STA_min": 0.6e-6, "tHD_STA_min": 0.6e-6,
    "tSU_STO_min": 0.6e-6, "tSU_DAT_min": 100e-9, "tHD_DAT_min": 0.0,
    "tr_max": 300e-9, "tf_max": 300e-9,
}

# START 判据的防抖余量：要求 SDA 跳变前 SCL 已稳定为高这么久。
# 太小会把"SCL 刚落 168 ns 的普通数据位变化"误判成重复 START（实测踩过）。
GUARD_S = 200e-9
STABLE_WIN_S = 0.3e-6   # 判"稳定高/低"的回看窗；⚠️ 不能按 us 写死，粗采样时会失准


# ---------------- 载入 ----------------

def load_wave(path):
    """统一成 (t[], ch1[], ch2[])。"""
    d = json.load(open(path))
    if "merged" in d:
        m = d["merged"]
        return m["t"], m["ch1"], m["ch2"]
    if "CH1" in d and isinstance(d["CH1"], dict) and "y" in d["CH1"]:
        a, b = d["CH1"], d["CH2"]
        n = min(len(a["y"]), len(b["y"]))
        t = [(i - a["xref"]) * a["xinc"] + a["xorg"] for i in range(n)]
        return t, a["y"][:n], b["y"][:n]
    if "ch1_y" in d:
        x = d
        n = len(x["ch1_y"])
        t = [x["t0"] + i * x["xinc"] for i in range(n)]
        return t, x["ch1_y"], x["ch2_y"]
    if "T" in d:
        return d["T"], d["Y1"], d["Y2"]
    raise ValueError("无法识别的波形 JSON 格式: %s" % path)


# ---------------- 基础 ----------------

def pct(y, p):
    z = sorted(y)
    return z[min(len(z) - 1, max(0, int(p * len(z))))]


def levels(y1, y2):
    L1, H1 = pct(y1, .02), pct(y1, .98)
    L2, H2 = pct(y2, .02), pct(y2, .98)
    return L1, H1, L2, H2


def edges(t, y, thr, hyst):
    """返回 [(方向, 时刻秒, 索引)]，穿越时刻用**该点实际步长**线性插值。

    🚨 不要用 `dt = t[1] - t[0]` 当全局步长！
    拼接数据（mem_scan/cmd_dump 的多窗口合并）步长可能**不统一**
    （实测 DSO-X 6004A：第一窗口 6 ns、其余窗口 12 ns），
    一旦拿首段步长当全局步长，所有边沿时刻都会算错，
    实测后果：把 SCL 低电平期间的普通数据位跳变**全部误判成 START**，
    凭空造出 15 个 START / 13 个 STOP，并算出 tSU;STA=0.1666us（假的 FAIL）。
    """
    out = []
    st = 1 if y[0] > thr else 0
    for i in range(1, len(y)):
        seg = t[i] - t[i - 1]          # ★ 逐点实际步长
        if st == 0 and y[i] > thr + hyst:
            y0, y1 = y[i - 1], y[i]
            f = (thr - y0) / (y1 - y0) if y1 != y0 else 0.0
            out.append(("R", t[i - 1] + f * seg, i))
            st = 1
        elif st == 1 and y[i] < thr - hyst:
            y0, y1 = y[i - 1], y[i]
            f = (thr - y0) / (y1 - y0) if y1 != y0 else 0.0
            out.append(("F", t[i - 1] + f * seg, i))
            st = 0
    return out


def _idx_of(t, tt):
    """在时间轴上定位 tt 对应采样点索引（即最后一个 <= tt 的点）。

    用二分查找而不是 `(tt - t[0]) / dt` —— 后者在非均匀步长下会算错索引。
    """
    import bisect
    i = bisect.bisect_right(t, tt) - 1
    return max(0, min(len(t) - 1, i))


def stable_high(t, y, tt, thr, win_s, dt=None):
    """tt 时刻前后 win_s 内 SCL 是否一直是高。

    按时间轴二分定位（支持非均匀步长），不再依赖 dt。
    """
    a = _idx_of(t, tt - win_s)
    b = _idx_of(t, tt + win_s)
    if a >= b:
        return False
    return all(v > thr for v in y[a:b + 1])


def sample(t, y, tt, thr, dt=None):
    return 1 if y[_idx_of(t, tt)] > thr else 0


def nearest_before(tt, E):
    """E 里**最接近且早于** tt 的边沿时刻。

    ⚠️ 别用 next(...) —— 那取的是时间上最早的那个，会把
    "最后一个 SCL 上升 -> STOP" 算成整帧跨度（曾算出 385.79us）。"""
    return max((t2 for k2, t2, j in E if t2 < tt), default=None)


def nearest_after(tt, E, kind=None):
    """E 里**最接近且晚于** tt 的边沿时刻；kind 限 'R'/'F'，None 表示不限。"""
    return min((t2 for k2, t2, j in E
                if (kind is None or k2 == kind) and t2 > tt), default=None)


# ---------------- 主分析 ----------------

def analyze(wave_path, spec_name="std"):
    t, y1, y2 = load_wave(wave_path)
    spec = SPEC_STD if spec_name == "std" else SPEC_FM
    dt = t[1] - t[0]
    L1, H1, L2, H2 = levels(y1, y2)
    T1, T2 = (L1 + H1) / 2, (L2 + H2) / 2
    E1 = edges(t, y1, T1, .12 * (H1 - L1))
    E2 = edges(t, y2, T2, .12 * (H2 - L2))

    dec = {"levels": {"ch1_lo": L1, "ch1_hi": H1, "ch2_lo": L2, "ch2_hi": H2,
                      "thr_ch1": T1, "thr_ch2": T2},
           "span_us": (t[-1] - t[0]) * 1e6, "dt_ns": dt * 1e9,
           "n_edges": {"scl": len(E1), "sda": len(E2)}}

    # --- START / STOP ---
    starts, stops = [], []
    for k, tt, i in E2:
        hi = stable_high(t, y1, tt, T1, STABLE_WIN_S, dt)
        if not hi:
            continue
        (starts if k == "F" else stops).append(tt)
    dec["start_us"] = [round(x * 1e6, 4) for x in starts]
    dec["stop_us"] = [round(x * 1e6, 4) for x in stops]
    dec["n_start"] = len(starts)
    dec["n_stop"] = len(stops)
    dec["has_repeated_start"] = len(starts) >= 2

    # --- 字节解码（每个 SCL 上升沿的高电平中点采样 SDA）---
    bits = []
    for k, tt, i in E1:
        if k != "R":
            continue
        nf = next((t2 for k2, t2, j in E1 if k2 == "F" and t2 > tt), None)
        if nf is None:
            break
        mid = (tt + nf) / 2
        bits.append((tt, sample(t, y2, mid, T2, dt)))
    bs = "".join(str(b) for _, b in bits)
    dec["n_clocks"] = len(bits)
    dec["bits"] = bs
    groups = []
    for k in range(0, len(bs) // 9 * 9, 9):
        g = bs[k:k + 9]
        groups.append({"byte": int(g[:8], 2), "hex": "%02X" % int(g[:8], 2),
                       "ack": g[8] == "0"})
    dec["bytes"] = groups
    if groups:
        b0 = groups[0]["byte"]
        dec["first_byte"] = "0x%02X" % b0
        dec["addr7"] = "0x%02X" % (b0 >> 1)
        dec["rw"] = "READ" if b0 & 1 else "WRITE"

    # --- 时钟统计 ---
    R = [tt for k, tt, i in E1 if k == "R"]
    F = [tt for k, tt, i in E1 if k == "F"]
    if len(R) > 1:
        per = [R[i + 1] - R[i] for i in range(len(R) - 1)]
        hi, lo = [], []
        for i in range(len(R) - 1):
            f = [x for x in F if R[i] < x < R[i + 1]]
            if f:
                hi.append(f[0] - R[i])
                lo.append(R[i + 1] - f[0])
        dec["clock"] = {
            "n_periods": len(per),
            "f_khz": 1e-3 / (sum(per) / len(per)),
            "T_us": sum(per) / len(per) * 1e6,
            "T_min_us": min(per) * 1e6, "T_max_us": max(per) * 1e6,
            "tHIGH_us": sum(hi) / len(hi) * 1e6 if hi else None,
            "tHIGH_min_us": min(hi) * 1e6 if hi else None,
            "tLOW_us": sum(lo) / len(lo) * 1e6 if lo else None,
            "tLOW_min_us": min(lo) * 1e6 if lo else None,
        }

    # --- 上升/下降时间（10%-90%）---
    def trans_times(y, E, lo, hi, label):
        res = {"R": [], "F": []}
        for k, tt, ix in E:
            a = lo + 0.1 * (hi - lo)
            b = lo + 0.9 * (hi - lo)
            rng = range(max(1, ix - 400), min(len(y), ix + 800)) if k == "R" \
                else range(max(1, ix - 200), min(len(y), ix + 400))
            c1 = c2 = None
            for i in rng:
                seg = t[i] - t[i - 1]      # ★ 逐点实际步长，不用全局 dt
                if k == "R":
                    if c1 is None and y[i - 1] < a <= y[i]:
                        c1 = t[i - 1] + (a - y[i - 1]) / (y[i] - y[i - 1]) * seg
                    if c2 is None and y[i - 1] < b <= y[i]:
                        c2 = t[i - 1] + (b - y[i - 1]) / (y[i] - y[i - 1]) * seg
                else:
                    if c1 is None and y[i - 1] > a >= y[i]:
                        c1 = t[i - 1] + (a - y[i - 1]) / (y[i] - y[i - 1]) * seg
                    if c2 is None and y[i - 1] > b >= y[i]:
                        c2 = t[i - 1] + (b - y[i - 1]) / (y[i] - y[i - 1]) * seg
                if c1 is not None and c2 is not None:
                    break
            if c1 is not None and c2 is not None:
                res[k].append(abs(c2 - c1))
        out = {}
        for k, nm in (("R", "tr"), ("F", "tf")):
            v = res[k]
            out[nm] = None if not v else {
                "n": len(v), "min_ns": min(v) * 1e9,
                "mean_ns": sum(v) / len(v) * 1e9, "max_ns": max(v) * 1e9}
        dec["%s_transition" % label] = out
        return out

    trans_times(y1, E1, L1, H1, "scl")
    trans_times(y2, E2, L2, H2, "sda")

    # --- 五个时序量：枚举**所有**出现位置 ---
    # ⚠️ 踩过的坑：只取"第一个满足条件的边沿"会得到毫无意义的数
    #    （曾算出 tSU;STO=385.79us、tHD;DAT=20.96us）。
    #    正确做法：枚举每个时钟周期的候选值，判定用**最紧值(min)**——
    #    I2C 这些参数都是"不小于"型要求，最紧的那个才决定合不合格。
    timings = {}

    def record_multi(name, desc, values, spec_min=None, spec_max=None):
        vals = sorted(values)
        e = {"desc": desc, "n": len(vals),
             "spec_min_us": None if spec_min is None else spec_min * 1e6,
             "spec_max_ns": None if spec_max is None else spec_max * 1e9,
             "verdict": "NA", "value_s": None, "value_us": None, "value_ns": None}
        if vals:
            e.update({"value_s": vals[0], "value_us": vals[0] * 1e6,
                      "value_ns": vals[0] * 1e9,
                      "min_s": vals[0], "median_s": vals[len(vals) // 2],
                      "max_s": vals[-1]})
            ok = True
            if spec_min is not None and vals[0] < spec_min:
                ok = False
            if spec_max is not None and vals[0] > spec_max:
                ok = False
            e["verdict"] = "PASS" if ok else "FAIL"
        timings[name] = e

    # tSU;STA: 重复START 之前最近的 SCL 上升 -> SDA 下降
    vals = []
    for st in starts:
        prev = nearest_before(st, E1)
        if prev is not None and 0 < st - prev < 8e-6:
            vals.append(st - prev)
    record_multi("tSU_STA", "SCL 上升 -> SDA 下降（重复 START 建立时间）", vals,
                 spec_min=spec["tSU_STA_min"])

    # tHD;STA: SDA 下降(START) -> 紧随的 SCL 下降
    vals = []
    for st in starts:
        nf = nearest_after(st, E1, "F")
        if nf is not None:
            vals.append(nf - st)
    record_multi("tHD_STA", "SDA 下降(START) -> SCL 下降", vals,
                 spec_min=spec["tHD_STA_min"])

    # tSU;STO: STOP 之前最近的 SCL 上升 -> SDA 上升
    vals = []
    for sp in stops:
        prev = nearest_before(sp, E1)
        if prev is not None:
            vals.append(sp - prev)
    record_multi("tSU_STO", "SCL 上升 -> SDA 上升(STOP)", vals,
                 spec_min=spec["tSU_STO_min"])

    # tHD;DAT: SCL 下降 -> 紧随的 SDA 跳变（该周期数据不变则顺延到下个周期）
    vals = []
    for k, tt, i in E1:
        if k != "F":
            continue
        nxt = nearest_after(tt, E2, None)
        if nxt is None:
            continue
        if not stable_high(t, y1, max(t[0], nxt - 0.5e-6), T1, 0.2e-6, dt):
            vals.append(nxt - tt)
    record_multi("tHD_DAT", "SCL 下降 -> 下一个 SDA 跳变（数据保持）", vals,
                 spec_min=spec["tHD_DAT_min"])

    # tSU;DAT: SDA 跳变（SCL 为低）-> 紧随的 SCL 上升
    vals = []
    for k, tt, i in E2:
        if stable_high(t, y1, max(t[0], tt - 0.5e-6), T1, 0.2e-6, dt):
            continue
        nf = nearest_after(tt, E1, "R")
        if nf is not None:
            vals.append(nf - tt)
    record_multi("tSU_DAT", "SDA 跳变 -> 下一个 SCL 上升（数据建立）", vals,
                 spec_min=spec["tSU_DAT_min"])

    dec["timings"] = timings
    dec["spec_used"] = spec_name

    # 本段可测性总结
    dec["notes"] = []
    if not dec["has_repeated_start"]:
        dec["notes"].append(
            "本段只出现 %d 个 START，没有重复 START -> tSU;STA 无对应事件，判 NA。"
            "要测它需要一段含『写命令码 -> 重复 START -> 读』的复合读事务。" % dec["n_start"])
    if dec["timings"]["tSU_STA"]["verdict"] == "NA" and dec["has_repeated_start"]:
        dec["notes"].append("有重复 START 但未匹配到合法的 SCL-上升→SDA-下降 对，请人工核对波形。")
    return dec


def fmt(dec, path_out=None):
    L = dec["levels"]
    lines = []
    lines.append("采样步长 = %.2f ns   记录跨度 = %.2f us" % (dec["dt_ns"], dec["span_us"]))
    lines.append("CH1(SCL) %.3f~%.3f V 阈值 %.3f | CH2(SDA) %.3f~%.3f V 阈值 %.3f"
                 % (L["ch1_lo"], L["ch1_hi"], L["thr_ch1"],
                    L["ch2_lo"], L["ch2_hi"], L["thr_ch2"]))
    lines.append("边沿数: SCL=%d SDA=%d" % (dec["n_edges"]["scl"], dec["n_edges"]["sda"]))
    lines.append("")
    lines.append("START %d 个: %s" % (dec["n_start"], dec["start_us"]))
    lines.append("STOP  %d 个: %s" % (dec["n_stop"], dec["stop_us"]))
    lines.append("重复 START: %s" % ("有" if dec["has_repeated_start"] else "无"))
    lines.append("")
    lines.append("时钟数 = %d" % dec["n_clocks"])
    if dec.get("bytes"):
        lines.append("字节: %s" % " ".join(
            "%s(%s)" % (g["hex"], "ACK" if g["ack"] else "NACK") for g in dec["bytes"]))
        lines.append("首字节 %s -> 7 位地址 %s + %s"
                     % (dec["first_byte"], dec["addr7"], dec["rw"]))
    if dec.get("clock"):
        c = dec["clock"]
        lines.append("频率 = %.2f kHz (T=%.4f us)  tHIGH=%.4f us  tLOW=%.4f us"
                     % (c["f_khz"], c["T_us"], c["tHIGH_us"], c["tLOW_us"]))
    for ch in ("scl", "sda"):
        tt = dec.get("%s_transition" % ch)
        if tt:
            for k in ("tr", "tf"):
                if tt[k]:
                    lines.append("%s %s = %.1f ns (n=%d, %.1f..%.1f)"
                                 % (ch.upper(), k, tt[k]["mean_ns"], tt[k]["n"],
                                    tt[k]["min_ns"], tt[k]["max_ns"]))
    lines.append("")
    lines.append("=== 时序量（枚举全部出现位置，判定用最紧值）===")
    for k, v in dec["timings"].items():
        if v["value_s"] is None:
            lines.append("  %-9s NA          本段无对应事件，不得填数" % k)
        else:
            unit = "us" if abs(v["value_us"]) >= 0.1 else "ns"
            f_ = 1e6 if unit == "us" else 1e9
            lines.append("  %-9s %.4f %s  [%s]  n=%d  范围 %.4f..%.4f %s"
                         % (k, v["value_s"] * f_, unit, v["verdict"], v["n"],
                            v["min_s"] * f_, v["max_s"] * f_, unit))
    if dec.get("notes"):
        lines.append("")
        for n in dec["notes"]:
            lines.append("注: " + n)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", required=True)
    ap.add_argument("--json", default=None)
    ap.add_argument("--spec", choices=["std", "fm"], default="std")
    args = ap.parse_args()

    dec = analyze(args.wave, args.spec)
    text = fmt(dec)
    print(text)
    out = args.json or (os.path.splitext(args.wave)[0] + "_decode.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(dec, f, ensure_ascii=False, indent=2)
    print("\n解码结果已存: %s" % out)


if __name__ == "__main__":
    main()
