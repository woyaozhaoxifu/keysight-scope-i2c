#!/usr/bin/env python
"""扫描示波器采集内存全段，锁定真实 I2C 活动区段。

原理：停止态下改 :TIMebase:DELay（水平位置）= "内存回看"，不触发重采。
      InfiniiVision 的采集内存通常远大于屏幕窗口
      （实测 DSO-X 6004A：ACQuire:POINts 2e6 @ 250 MSa/s ≈ 8 ms，而屏幕只有 100 us）。

判据：真实 I2C 信号的逻辑低会跨 3 V（min 掉到 1 V 以下）；
      总线空闲段是"恒高 + ~0.2 V 噪声"，**却能产生几千个假边沿**。
      => 用幅度 min 判定，**绝不要用边沿数**。

内存边界判据有两条，**必须都用**：
  左沿：设很小的 DELay 被钳位（回读与设定差 > 半步）+ 错误队列 -222"Data out of range"。
  右沿：**不能只靠钳位**。超出数据范围时设定值照收、回读也一致，
        但 :WAVeform:POINts? MAX 返回 0（错误队列 +109,"No Data For Operation"）。
        实测：内存只到 +500 us 时，DELay=+600 us 被正常接受；
        此时若照发 :WAVeform:DATA?，仪器**不回定长块**，读取会一直等到超时
        ——现象是"扫描跑了几分钟一行输出都没有"。
        本脚本已内置 POINts==0 拦截 + 连续两窗无数据即提前结束。

用法：
  python mem_scan.py --start -3600 --stop 4000 --step 100 --out D:\\tmp\\scan.json
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scope import ScopeError, open_scope

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ACTIVE_V = 1.0   # min 低于此值 => 出现真实逻辑低 => 总线有活动


def scan(s, start_us, stop_us, step_us, chs=(1, 2), verbose=True):
    orig_delay = s.qf(":TIMebase:DELay?", 0.0)
    s.cmd(":WAVeform:POINts:MODE MAXimum")

    def snap():
        d = s.fetch(chs[0])
        return d["xorg"], d["y"][:200]

    xo0, _ = snap()
    wins = []
    clamped = []
    pos = start_us
    nodata_run = 0
    while pos <= stop_us + 1e-9:
        s.cmd(":TIMebase:DELay " + repr(float(pos * 1e-6)))
        rd = s.qf(":TIMebase:DELay?", None)
        rd_us = None if rd is None else rd * 1e6
        # 设定值与回读差超过半步 => 被仪器钳位 => 已到内存边界
        if rd_us is not None and abs(rd_us - pos) > step_us * 0.5:
            clamped.append((pos, rd_us))
        rec = {"d_us": pos, "delay_us": rd_us, "ch": {}}
        for c in chs:
            try:
                # ★ 必须先确认该位置有数据。水平位置超出采集内存时
                #   :WAVeform:POINts? MAX 返回 0（错误队列 +109,"No Data For Operation"），
                #   若照发 :WAVeform:DATA? 仪器不回定长块，blk() 会一直等到超时
                #   （实测卡死 5 分钟，现象是"扫描无输出"）。
                if s.qf(":WAVeform:POINts? MAX", 0) <= 0:
                    raise ScopeError("该位置无波形数据")
                y = s.fetch(c, timeout=20.0)["y"]
            except Exception as e:
                rec["ch"][str(c)] = {"min": None, "max": None, "edges": 0,
                                     "active": False, "no_data": True,
                                     "note": "%s: %s" % (type(e).__name__, e)}
                continue
            mn, mx = min(y), max(y)
            thr = (mn + mx) / 2.0
            ne = sum(1 for k in range(1, len(y)) if (y[k - 1] - thr) * (y[k] - thr) < 0)
            rec["ch"][str(c)] = {"min": round(mn, 4), "max": round(mx, 4),
                                 "edges": ne, "active": mn < ACTIVE_V}
        rec["active"] = any(rec["ch"][str(c)]["active"] for c in chs)
        rec["no_data"] = all(rec["ch"][str(c)].get("no_data") for c in chs)
        wins.append(rec)
        # 连续 2 个窗口无数据 => 已越过采集内存右沿，继续扫只是白等
        nodata_run = nodata_run + 1 if rec["no_data"] else 0
        if verbose:
            if rec["no_data"]:
                print("  d=%+7.0fus -> delay=%+10.3fus  [无数据]  已超出采集内存"
                      % (pos, rd_us if rd_us is not None else float("nan")), flush=True)
            else:
                print("  d=%+7.0fus -> delay=%+10.3fus  [%s]  " % (
                    pos, rd_us if rd_us is not None else float("nan"),
                    "活动" if rec["active"] else "空闲")
                    + " | ".join("CH%d [%.2f,%.2f] e=%-5d" % (
                        c, rec["ch"][str(c)]["min"], rec["ch"][str(c)]["max"],
                        rec["ch"][str(c)]["edges"]) for c in chs), flush=True)
        if nodata_run >= 2:
            if verbose:
                print("  -> 连续 2 个窗口无数据，判定已到内存右沿，提前结束", flush=True)
            break
        pos += step_us

    s.cmd(":TIMebase:DELay " + repr(float(orig_delay)))
    xo1, _ = snap()
    # ★ 重采判据：不要用采样点值做指纹（fetch 会改 :WAVeform:POINts，
    #   抽取倍率一变点值就不同，必然误判）。正解见下两条。
    pure = (xo0 == xo1)
    d1 = s.fetch(chs[0])
    d2 = s.fetch(chs[0])
    stable = (d1["y"] == d2["y"])
    return {
        "orig_delay_us": orig_delay * 1e6,
        "xorg_before_us": xo0 * 1e6, "xorg_after_us": xo1 * 1e6,
        "xorg_identical": pure, "same_pos_repeatable": stable,
        "no_recapture": bool(pure and stable),
        "clamped": clamped, "windows": wins,
    }


def segments(wins, step_us):
    """把连续 active 的窗口合并成区段。

    兼容两种窗口字段名：本脚本用 d_us，早期脚本产物用 set_us。
    """
    segs = []
    for w in wins:
        if not w["active"]:
            continue
        c = w.get("d_us", w.get("set_us"))
        if c is None:
            continue
        lo = c - step_us / 2.0
        hi = c + step_us / 2.0
        if segs and abs(lo - segs[-1][1]) <= step_us * 0.6:
            segs[-1][1] = hi
        else:
            segs.append([lo, hi])
    return segs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="<SCOPE_IP>")
    ap.add_argument("--start", type=float, default=-3600, help="起始 delay (us)")
    ap.add_argument("--stop", type=float, default=4000, help="终止 delay (us)")
    ap.add_argument("--step", type=float, default=100, help="步进 (us)")
    ap.add_argument("--ch", default="1,2")
    ap.add_argument("--out", default=None)
    a = ap.parse_args()
    chs = tuple(int(x) for x in a.ch.split(",") if x.strip())

    s = open_scope(a.host, timeout=30.0)
    print("扫描 %s : %+.0f ... %+.0f us, 步进 %.0f us, 通道 %s"
          % (a.host, a.start, a.stop, a.step, chs))
    r = scan(s, a.start, a.stop, a.step, chs)
    segs = segments(r["windows"], a.step)
    print()
    print("内存边界（钳位点）:", r["clamped"] if r["clamped"] else "未触及（扫描范围在内存内）")
    nd = [w["d_us"] for w in r["windows"] if w.get("no_data")]
    if nd:
        print("无数据窗口（已越过内存右沿）: %+.0f ... %+.0f us"
              % (min(nd), max(nd)))
    print("未触发重采 = %s   (XORigin 逐位回到原值: %s / 同位置可重复: %s)"
          % (r["no_recapture"], r["xorg_identical"], r["same_pos_repeatable"]))
    print("活动区段 (%d 段):" % len(segs))
    for lo, hi in segs:
        print("   %+9.1f ... %+9.1f us   (宽 %.1f us)" % (lo, hi, hi - lo))
    if not segs:
        print("   （整段无真实 I2C 活动 —— 总线全程空闲）")
    print("ERRQ:", s.err())
    s.close()
    if a.out:
        r["segments_us"] = segs
        json.dump(r, open(a.out, "w"), indent=1)
        print("已存", a.out)


if __name__ == "__main__":
    main()
