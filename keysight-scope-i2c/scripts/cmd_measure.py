# -*- coding: utf-8 -*-
"""在指定时基档下扫描水平位置、抓测量面板读数并截图。

这就是用户口中的"左右微调一下画面就出数"的可操作化：
面板显示"未完成"多半是刷新滞后的瞬时状态，换个 :TIMebase:POSition 就恢复。

用法：
    python cmd_measure.py --host <SCOPE_IP> \
        --timebase 10e-6,5e-6 --positions 18,22,26,30,34,38,42,46,50 \
        --out D:\\_scope_tmp\\shots

输出：
    <out>/<时基档>_pos<位置>.png   每个候选窗口的屏幕截图
    <out>/measure_result.json      全部读数与选定窗口

只读原则：只改 :TIMebase:SCALe/POSition，收尾还原原值；不动通道量程、不动触发。
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scope import Scope, DEFAULT_CH1, DEFAULT_CH2  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="<SCOPE_IP>")
    ap.add_argument("--port", type=int, default=5025)
    ap.add_argument("--timebase", default="10e-6",
                    help="时基档，秒；多个用逗号分隔，如 10e-6,5e-6")
    ap.add_argument("--positions", default="18,22,26,30,34,38,42,46,50",
                    help="水平位置候选，单位 us；逗号分隔")
    ap.add_argument("--out", default=r"<WORKDIR>\shots")
    ap.add_argument("--ch1", default=",".join(DEFAULT_CH1))
    ap.add_argument("--ch2", default=",".join(DEFAULT_CH2))
    ap.add_argument("--settle", type=float, default=1.0,
                    help="每次改档后的等待秒数（面板刷新慢，别设太小）")
    ap.add_argument("--shot-all", action="store_true",
                    help="给每个候选位置都截图（默认只截选定的那个）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只看状态不写任何东西")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    log_path = os.path.join(args.out, "measure_log.txt")
    log = open(log_path, "w", encoding="utf-8")

    def P(*a):
        t = " ".join(str(x) for x in a)
        print(t, flush=True)
        log.write(t + "\n")
        log.flush()

    ch1 = [x for x in args.ch1.split(",") if x]
    ch2 = [x for x in args.ch2.split(",") if x]
    tbs = [float(x) for x in args.timebase.split(",") if x]
    positions = [float(x) for x in args.positions.split(",") if x]

    sc = Scope(args.host, args.port, timeout=12)
    P("IDN:", sc.idn())
    P("探头倍率:", sc.probes())

    orig_td = sc.q(":TIMebase:SCALe?").strip()
    orig_pos = sc.q(":TIMebase:POSition?").strip()
    orig_run = sc.q(":OPERegister:CONDition?").strip()
    P("原状态: 时基=%s 位置=%s 运行态=%s" % (orig_td, orig_pos, orig_run))

    if args.dry_run:
        P("dry-run，未做任何修改。")
        sc.close()
        log.close()
        return

    result = {"orig": {"timebase": orig_td, "position": orig_pos,
                       "run_state": orig_run},
              "ch1": ch1, "ch2": ch2, "windows": {}}

    try:
        sc.cmd(":STOP")           # 停止态：改时基/位置只缩放已有记录，不丢数据
        time.sleep(0.5)
        sc.add_meas(ch1=ch1, ch2=ch2, clear=True)

        for tb in tbs:
            tag = "td%gus" % (tb * 1e6)
            P("")
            P("=== %s ===" % tag)
            sc.cmd(":TIMebase:SCALe %.10g" % tb)
            time.sleep(args.settle)

            # 先重试一遍：面板"未完成"经常是刷新滞后，重读一次就出数
            best = None
            all_res = {}
            for attempt in range(2):
                pos_us, res = sc.scan_position(positions, settle=args.settle)
                for p, r in res.items():
                    nok = sum(1 for v in r.values() if v is not None)
                    if best is None or nok > best[1]:
                        best = (p, nok, r)
                    all_res[p] = r
                P("  第%d轮扫描最佳: pos=%+g us (%d 项有效)"
                  % (attempt + 1, pos_us, sum(1 for v in res.get(pos_us, {}).values()
                                              if v is not None)))
                if best and best[1] >= len(ch1) + len(ch2) - 1:
                    break

            # 选定窗口：把候选里有效项最多的那个位置恢复回去
            sc.cmd(":TIMebase:POSition %.10gE-6" % best[0])
            time.sleep(args.settle)
            final = sc.read_meas_batch()

            P("  >> 选定 pos=%+g us  有效 %d/%d"
              % (best[0], sum(1 for v in final.values() if v is not None), len(final)))
            for k, v in final.items():
                P("     %-18s = %s" % (k, "无有效读数" if v is None else "%.9g" % v))

            shot = os.path.join(args.out, "%s_pos%g.png" % (tag, best[0]))
            n = sc.screenshot(shot)
            P("     截图 %s (%d B)" % (os.path.basename(shot), n))

            result["windows"][tag] = {"timebase_s": tb, "position_us": best[0],
                                      "meas": final, "screenshot": shot,
                                      "all_positions": {
                                          str(p): v for p, v in all_res.items()}}

        if args.shot_all:
            for tb in tbs:
                for p in positions:
                    sc.cmd(":TIMebase:SCALe %.10g" % tb)
                    sc.cmd(":TIMebase:POSition %.10gE-6" % p)
                    time.sleep(args.settle)
                    fn = os.path.join(args.out, "td%dus_pos%d.png"
                                      % (tb * 1e6, p))
                    sc.screenshot(fn)
            P("已给全部候选位置截图。")

    finally:
        # 收尾还原：时基与位置回到原值
        try:
            sc.cmd(":TIMebase:SCALe %s" % orig_td)
            sc.cmd(":TIMebase:POSition %s" % orig_pos)
        except Exception as e:
            P("还原失败（需人工确认）:", e)
        P("")
        P("错误队列:", sc.err())
        sc.close()
        json_path = os.path.join(args.out, "measure_result.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        P("结果已存:", json_path)
        P("日志:", log_path)
        log.close()


if __name__ == "__main__":
    main()
