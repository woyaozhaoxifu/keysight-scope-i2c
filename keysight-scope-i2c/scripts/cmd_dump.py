# -*- coding: utf-8 -*-
"""多窗口拼接导出整段波形 + 验证"缩放是否触发重新采集"。

为什么需要拼接：:WAVeform:DATA? 只返回**当前屏幕窗口**的数据。
10 us/div 时窗口约 100 us，而记录可能有 ms 级，所以要按 :TIMebase:POSition
滑动窗口逐段取回，再按时间轴拼起来（重复覆盖区丢弃旧数据）。

为什么需要 verify：拉大 :TIMebase:SCALe 能看到屏幕外的数据（前提是记录本身比屏幕长），
但**拉大到超出已采跨度会触发重新采集**，此时画面会变、测量值也会变。
判别方法：取原始档位波形 -> 拉大 -> 缩回 -> 再取一次，逐点比对。
  逐点一致  => 纯缩放，安全
  有差异    => 触发了重采，之前的读数不能和现在的混用

用法：
    # 导出 -50..350us 的整段波形（窗口 100us、步进 50us）
    python cmd_dump.py dump --host <SCOPE_IP> \
        --positions -50,0,50,100,150,200,250,300,350 --out D:\\_scope_tmp

    # 验证拉到 20us/div 再缩回是否重采
    python cmd_dump.py verify --host <SCOPE_IP> --probe-timebase 20e-6
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scope import Scope  # noqa: E402


def do_dump(sc, args):
    os.makedirs(args.out, exist_ok=True)
    log = open(os.path.join(args.out, "dump_log.txt"), "w", encoding="utf-8")

    def P(*a):
        t = " ".join(str(x) for x in a)
        print(t, flush=True)
        log.write(t + "\n")
        log.flush()

    orig_td = sc.q(":TIMebase:SCALe?").strip()
    orig_pos = sc.q(":TIMebase:POSition?").strip()
    P("原状态 时基=%s 位置=%s" % (orig_td, orig_pos))
    sc.cmd(":STOP")
    time.sleep(0.5)

    positions = [float(x) for x in args.positions.split(",") if x]
    windows = []
    try:
        for p in positions:
            sc.cmd(":TIMebase:POSition %.10gE-6" % p)
            time.sleep(args.settle)
            d1 = sc.fetch(1)
            d2 = sc.fetch(2)
            windows.append({
                "pos_us": p,
                "ch1": {k: d1[k] for k in ("pts", "xinc", "xorg", "xref",
                                           "yinc", "yorg", "yref", "t0", "t1")},
                "ch2": {k: d2[k] for k in ("pts", "xinc", "xorg", "xref",
                                           "yinc", "yorg", "yref", "t0", "t1")},
                "ch1_y": d1["y"], "ch2_y": d2["y"],
            })
            P("win pos=%+8.2fus  CH1 pts=%d xinc=%.1fns t=[%.3f..%.3f]us"
              % (p, d1["pts"], d1["xinc"] * 1e9, d1["t0"] * 1e6, d1["t1"] * 1e6))
    finally:
        sc.cmd(":TIMebase:SCALe %s" % orig_td)
        sc.cmd(":TIMebase:POSition %s" % orig_pos)
        sc.close()

    # 拼接：按 t 排序，同一时刻保留先出现的（先出现的是"原始档位"取的，可信）
    merged_t, merged1, merged2 = [], [], []
    for w in sorted(windows, key=lambda x: x["ch1"]["t0"]):
        w1 = w["ch1"]
        n = len(w["ch1_y"])
        for i in range(n):
            t = (i - w1["xref"]) * w1["xinc"] + w1["xorg"]
            if merged_t and t <= merged_t[-1]:
                continue
            merged_t.append(t)
            merged1.append(w["ch1_y"][i])
            merged2.append(w["ch2_y"][i])

    payload = {"merged": {"t": merged_t, "ch1": merged1, "ch2": merged2,
                          "n": len(merged_t),
                          "t_start": merged_t[0] if merged_t else None,
                          "t_end": merged_t[-1] if merged_t else None,
                          "dt": (merged_t[1] - merged_t[0]) if len(merged_t) > 1 else None},
               "windows": [{k: v for k, v in w.items()
                            if k not in ("ch1_y", "ch2_y")} for w in windows]}
    out = os.path.join(args.out, args.output)
    with open(out, "w") as f:
        json.dump(payload, f)
    P("合并点数=%d  时间 %.3f..%.3f us  步长=%.1f ns"
      % (len(merged_t), merged_t[0] * 1e6, merged_t[-1] * 1e6,
         (merged_t[1] - merged_t[0]) * 1e9 if len(merged_t) > 1 else 0))
    P("已存:", out, "  日志:", os.path.join(args.out, "dump_log.txt"))
    log.close()
    return out


def do_verify(sc, args):
    """取原始档位 -> 拉大到目标档位 -> 缩回 -> 再取，逐点比对。"""
    orig_td = sc.qf(":TIMebase:SCALe?")
    orig_pos = sc.qf(":TIMebase:POSition?")
    print("原状态 时基=%g 位置=%g" % (orig_td, orig_pos))
    sc.cmd(":STOP")
    time.sleep(0.5)

    before = sc.fetch(1)
    print("原始档位取回: pts=%d xinc=%.1fns 窗口=[%.3f..%.3f]us"
          % (before["pts"], before["xinc"] * 1e9,
             before["t0"] * 1e6, before["t1"] * 1e6))

    sc.cmd(":TIMebase:SCALe %.10g" % (args.probe_timebase / 10.0))
    time.sleep(1.5)
    wider = sc.fetch(1)
    print("拉大到 %.6g s/div 后: pts=%d xinc=%.1fns 窗口=[%.3f..%.3f]us"
          % (args.probe_timebase / 10.0, wider["pts"], wider["xinc"] * 1e9,
             wider["t0"] * 1e6, wider["t1"] * 1e6))

    sc.cmd(":TIMebase:SCALe %s" % orig_td)
    sc.cmd(":TIMebase:POSition %s" % orig_pos)
    time.sleep(1.5)
    after = sc.fetch(1)
    print("缩回原档位后: pts=%d xinc=%.1fns 窗口=[%.3f..%.3f]us"
          % (after["pts"], after["xinc"] * 1e9,
             after["t0"] * 1e6, after["t1"] * 1e6))

    n = min(len(before["y"]), len(after["y"]))
    diffs = [abs(before["y"][i] - after["y"][i]) for i in range(n)]
    mx = max(diffs)
    ndiff = sum(1 for d in diffs if d > 0.05)
    print("")
    print("逐点比对: 点数=%d  最大差=%.4f V  超过50mV的点=%d (%.2f%%)"
          % (n, mx, ndiff, 100.0 * ndiff / n if n else 0))
    if mx < 0.05:
        print("结论: 逐点一致 -> 拉大的那一次是【纯缩放】读的同一段记录，安全。")
    else:
        print("结论: 存在差异 -> 拉大时【触发了重新采集】。"
              "拉大后取到的数据属于新的采集，不能与原始读数混用！")
    print("总线还有活动吗(采集计数):", sc.q(":ACQuire:COUNt?"))
    sc.close()
    return mx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["dump", "verify"])
    ap.add_argument("--host", default="<SCOPE_IP>")
    ap.add_argument("--port", type=int, default=5025)
    ap.add_argument("--out", default=r"<WORKDIR>")
    ap.add_argument("--positions", default="-50,0,50,100,150,200,250,300,350")
    ap.add_argument("--output", default="wave_merged.json")
    ap.add_argument("--settle", type=float, default=0.9)
    ap.add_argument("--probe-timebase", type=float, default=20e-6)
    args = ap.parse_args()

    sc = Scope(args.host, args.port, timeout=180)
    if args.cmd == "dump":
        do_dump(sc, args)
    elif args.cmd == "verify":
        do_verify(sc, args)
    else:
        sc.close()


if __name__ == "__main__":
    main()
