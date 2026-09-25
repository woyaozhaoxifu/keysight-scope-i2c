# -*- coding: utf-8 -*-
"""Tr / Tf 逐沿测量 —— 默认采用 I2C 规范口径 30%-70%（NXP UM10204）。

⚠️ 核心教训（2026-09）：I2C 规范的 Tr/Tf 定义基准是 **30%-70%**，不是 10%-90%。
   用 10%-90% 会把波形平坦尾部算进来，数值虚高约 2-3 倍，导致把合格板子误判成超规格。
   实测：同一批沿 10-90% 给 1797-1921ns（超规格），30-70% 给 611-660ns（合格）。

用法：
    python tr_measure.py --wave D:\\path\\wave.json [--ch1-name SCL --ch2-name SDA]
                         [--lo 30 --hi 70] [--limit-ns 1000]
    # 也支持 --lo 10 --hi 90 作对照，但会明确标注口径

支持波形 JSON 格式（与 i2c_decode.load_wave 一致）：
    {"T":[...], "Y1":[...], "Y2":[...]}  或 {"merged":{...}} 或 {"CH1":{"y":...},...}
"""
import argparse, json, sys
import numpy as np


def load_wave(path):
    """统一成 (t, ch1, ch2)。与 i2c_decode.load_wave 兼容。"""
    d = json.load(open(path))
    if "merged" in d:
        m = d["merged"]
        return np.array(m["t"], float), np.array(m["ch1"], float), np.array(m["ch2"], float)
    if "CH1" in d and isinstance(d["CH1"], dict) and "y" in d["CH1"]:
        a, b = d["CH1"], d["CH2"]
        n = min(len(a["y"]), len(b["y"]))
        t = np.array([(i - a["xref"]) * a["xinc"] + a["xorg"] for i in range(n)], float)
        return t, np.array(a["y"][:n], float), np.array(b["y"][:n], float)
    if "ch1_y" in d:
        x = d
        n = len(x["ch1_y"])
        t = np.array([x["t0"] + i * x["xinc"] for i in range(n)], float)
        return t, np.array(x["ch1_y"], float), np.array(x["ch2_y"], float)
    if "T" in d:
        return np.array(d["T"], float), np.array(d["Y1"], float), np.array(d["Y2"], float)
    raise ValueError("无法识别的波形 JSON 格式: %s" % path)


def edge_times(t, y, th):
    """所有穿越阈值 th 的时刻 + 方向 (1=上升, -1=下降)。"""
    out = []
    for i in range(1, len(y)):
        a, b = y[i - 1], y[i]
        if (a < th) != (b < th):
            dt = t[i] - t[i - 1]
            f = (th - a) / (b - a) if b != a else 0.0
            out.append((t[i - 1] + f * dt, 1 if b > a else -1, i))
    return out


def trans_time(t, y, i50, L, H, lo_pct, hi_pct, rising=True, max_pts=None):
    """在 i50（50% 穿越点下标）所在单调段内量 lo%->hi% 时间（秒）。

    关键1：只在 i50 向两侧的单调段内找穿越点，避免翻到下一个沿。
    关键2：搜索范围受 max_pts 限制（默认取记录长度的 5%）。
           否则当信号在阈值附近抖动时，回溯会一路走到上一个周期，
           算出"10µs 的下降时间"这种荒谬值（实测遇过 318 个假值）。
    """
    if rising:
        Vlo = L + lo_pct * (H - L)
        Vhi = L + hi_pct * (H - L)
    else:
        Vlo = H - lo_pct * (H - L)   # 下降时 lo% 位于高处
        Vhi = H - hi_pct * (H - L)

    if max_pts is None:
        max_pts = max(64, len(y) // 20)   # 默认 5% 记录长度

    def ip(i, vth):
        if i <= 0 or y[i] == y[i - 1]:
            return t[i]
        f = (vth - y[i - 1]) / (y[i] - y[i - 1])
        return t[i - 1] + f * (t[i] - t[i - 1])

    if rising:
        a = i50
        while a > 0 and y[a] > Vlo and (i50 - a) < max_pts:
            a -= 1
        b = i50
        while b < len(y) - 1 and y[b] < Vhi and (b - i50) < max_pts:
            b += 1
        ta = ip(a + 1, Vlo) if a + 1 < len(y) else t[a]
        tb = ip(b, Vhi) if b - 1 >= 0 else t[b]
    else:
        a = i50
        while a > 0 and y[a] < Vlo and (i50 - a) < max_pts:
            a -= 1
        b = i50
        while b < len(y) - 1 and y[b] > Vhi and (b - i50) < max_pts:
            b += 1
        ta = ip(a + 1, Vlo) if a + 1 < len(y) else t[a]
        tb = ip(b, Vhi) if b - 1 >= 0 else t[b]
    return (tb - ta)


def measure(t, y, lo_pct, hi_pct, max_ns=3000.0, t_min=None):
    """返回 (rising_list, falling_list)，元素为 (时刻, 时间ns)。

    max_ns：单次转换时间的合理上限（默认 3000 ns）。
            I²C 标准/快速模式下 Tr ≤1000ns、Tf ≤300ns，超 3000ns 的必是
            "阈值抖动导致回溯跨周期"或"记录边界残段"的误判，直接丢弃。
    """
    H, L = float(y.max()), float(y.min())
    if H - L < 0.5:
        return [], []      # 无信号
    th = (H + L) / 2
    E = edge_times(t, y, th)
    ups, dns = [], []
    for tt, d, i in E:
        if t_min is not None and tt < t_min:
            continue
        v = trans_time(t, y, i, L, H, lo_pct, hi_pct, rising=(d > 0)) * 1e9
        if v <= 0 or v > max_ns:
            continue       # 边界/空闲段误判
        (ups if d > 0 else dns).append((tt, v))
    return ups, dns


def summarize(name, vals, limit_ns, label_pct="30%-70%"):
    if not vals:
        print("  %-6s : 无有效沿" % name)
        return
    v = np.array([x[1] for x in vals])
    # 离群剔除：I2C 的 Tr/Tf 是总线固有特性，同一批沿应高度一致。
    # 用中位数 ± 3×MAD 剔除边界/空闲段误判（它们通常是 ms 级或数万 ns）。
    med = np.median(v)
    mad = np.median(np.abs(v - med))
    if mad > 0:
        keep = np.abs(v - med) <= 3.0 * 1.4826 * mad
        v_clean = v[keep]
        n_out = len(v) - len(v_clean)
    else:
        v_clean, n_out = v, 0
    flag = "PASS" if v_clean.max() <= limit_ns else "**超规格**"
    extra = "  剔除离群 %d 个" % n_out if n_out else ""
    print("  %-6s : n=%3d  min=%7.1f  中位=%7.1f  max=%7.1f ns  (限 %g ns)  -> %s%s"
          % (name, len(v_clean), v_clean.min(), np.median(v_clean),
             v_clean.max(), limit_ns, flag, extra))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", required=True, help="波形 JSON 路径")
    ap.add_argument("--ch1-name", default="SCL")
    ap.add_argument("--ch2-name", default="SDA")
    ap.add_argument("--lo", type=float, default=30.0, help="低电平百分比（默认 30，I2C 规范口径）")
    ap.add_argument("--hi", type=float, default=70.0, help="高电平百分比（默认 70，I2C 规范口径）")
    ap.add_argument("--limit-ns", type=float, default=1000.0, help="Tr 限值 ns（标准模式 1000）")
    ap.add_argument("--limit-ns-fall", type=float, default=300.0, help="Tf 限值 ns（标准模式 300）")
    ap.add_argument("--t-min-us", type=float, default=None,
                    help="只统计晚于该时刻的沿（us）。不指定时自动跳过记录开头 1%% 的残段")
    ap.add_argument("--list", action="store_true", help="同时逐沿打印")
    a = ap.parse_args()

    t, c1, c2 = load_wave(a.wave)
    lo, hi = a.lo / 100.0, a.hi / 100.0
    if a.t_min_us is not None:
        t_min = a.t_min_us * 1e-6
    else:
        # 默认跳过记录开头 1%：触发点在记录中，开头常含半截沿（会被误算成 ms 级）
        t_min = t[0] + 0.01 * (t[-1] - t[0])

    print("波形: %s" % a.wave)
    print("记录: n=%d  [%.2f, %.2f] us  步长=%.2f ns" %
          (len(t), t[0] * 1e6, t[-1] * 1e6, (t[1] - t[0]) * 1e9))
    if abs(a.lo - 30) > 1e-9 or abs(a.hi - 70) > 1e-9:
        print("⚠️  口径 = %.0f%%-%.0f%%  —— 非 I2C 规范口径（规范是 30%%-70%%），仅供对照！"
              % (a.lo, a.hi))
    else:
        print("口径 = 30%%-70%% ✅ I2C 规范口径（NXP UM10204）")
    print()

    for nm, y in ((a.ch1_name, c1), (a.ch2_name, c2)):
        H, L = float(y.max()), float(y.min())
        print("%s: 高=%.3f 低=%.3f 峰峰=%.3f V" % (nm, H, L, H - L))
        ups, dns = measure(t, y, lo, hi, t_min=t_min)
        summarize("Tr", ups, a.limit_ns)
        summarize("Tf", dns, a.limit_ns_fall)
        if a.list:
            for tt, v in ups:
                print("    上升 @%10.3f us  Tr=%7.1f ns" % (tt * 1e6, v))
        print()


if __name__ == "__main__":
    main()
