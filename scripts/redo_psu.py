# -*- coding: utf-8 -*-
"""
redo_psu.py —— 单个 PSU 实验「重测 → 标准取证图集」一键脚本。

为什么有它（2026-09-24 实机复盘教训）：
  1) 0x59 那次采集把 CH2 垂直档位设成 500 mV/div，SDA 高电平(≈3.3 V)远超屏幕量程
     被**削顶**——导出的 5 万个 CH2 采样里 3.3 万个顶到数字满量程，示波器对 CH2 最大
     电平直接返回「无信号」，SDA 的上升/下降时间**不能作为判定依据**，只能重测。
     → 本脚本把「档位/削顶自检」做成取数后的**强制关卡**，档位不对当场报错并给出改法。
  2) 每个实验要出 8 张标准取证图（对齐参考报告内嵌 zip 与 0x59/0x5B 图集口径），
     手工逐张调时基/位置/游标极易漏项；本脚本一键出齐 8+1 张。

标准图命名（与 0x59 / 0x5B 图集一致）：
  00_原始完整采集_未改触发未重新采集.png
  01b_{addr}地址总览_右侧测量数据.png
  02b_CLK时钟_右侧测量数据.png
  03b_SDA数据线_右侧测量数据.png
  04a_Thd_dat_数据保持_游标.png      (tHD;DAT)
  04b_Tsu_dat_数据建立_游标.png      (tSU;DAT)
  05_Tsu_sta_START建立_游标.png      (tSU;STA)
  06_Thd_sta_START保持_游标.png      (tHD;STA)
  07_Tsu_sto_STOP建立_游标.png       (tSU;STO)

用法:
    # 1) 开测前预检（★ 必做）：确认示波器在线、通道档位不削顶、信号大小合适
    python redo_psu.py --addr 0x5A --out "D:/Desktop/I2C四个实验图/0x5A" precheck

    # 2) 全流程：原屏备份 → 1ns 取数 → 削顶自检 → 解码 → 游标 → 标准 8+1 张图
    python redo_psu.py --addr 0x5A --out "D:/Desktop/I2C四个实验图/0x5A" all

    # 3) 已有 wave.json，只补截图（示波器在线）
    python redo_psu.py --addr 0x5A --out "..." shots

    # 4) 只取数 + 解码（不截图）
    python redo_psu.py --addr 0x5A --out "..." capture decode
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from scope import Scope, ScopeError                       # noqa: E402
from i2c_decode import analyze as decode_analyze, fmt as decode_fmt   # noqa: E402
from i2c_full_test import (compute_cursor_pairs,          # noqa: E402
                           MEAS_CH1, MEAS_CH2, load_wave_json)

# ---- 标准取证图命名（key → 文件名模板）----
SHOT_NAMES = {
    "overview": "01b_%s地址总览_右侧测量数据.png",
    "clk":      "02b_CLK时钟_右侧测量数据.png",
    "sda":      "03b_SDA数据线_右侧测量数据.png",
    "tHD_DAT":  "04a_Thd_dat_数据保持_游标.png",
    "tSU_DAT":  "04b_Tsu_dat_数据建立_游标.png",
    "tSU_STA":  "05_Tsu_sta_START建立_游标.png",
    "tHD_STA":  "06_Thd_sta_START保持_游标.png",
    "tSU_STO":  "07_Tsu_sto_STOP建立_游标.png",
}
SHOT_ORDER = ["tHD_DAT", "tSU_DAT", "tSU_STA", "tHD_STA", "tSU_STO"]


# ============================ 档位 / 削顶自检 ============================

def channel_window(scope, ch):
    """返回 (scale, offset, lo, hi)：示波器垂直 8 格，中心 = offset。"""
    scale = scope.qf(":CHANnel%d:SCALe?" % ch, 0.0) or 0.0
    offs = scope.qf(":CHANnel%d:OFFSet?" % ch, 0.0) or 0.0
    return scale, offs, offs - 4.0 * scale, offs + 4.0 * scale


def clipping_check(scope, ch, d):
    """对一个通道做削顶/贴边/量程自检。

    返回 dict(ok, level, msg)。level ∈ {'ok','clip_top','clip_bot','too_small'}
    判据：数字化后若信号触到屏幕上下沿（量程边界），说明超出垂直档 → 削顶。
    """
    scale, offs, lo, hi = channel_window(scope, ch)
    y = d["y"]
    ymin, ymax = min(y), max(y)
    span = hi - lo
    if span <= 0:
        return {"ok": False, "level": "unknown",
                "msg": "CH%d 读不到档位(scale=%g)" % (ch, scale)}
    top_margin = (hi - ymax) / span      # 顶部余量（占满屏比例）
    bot_margin = (ymin - lo) / span      # 底部余量
    sig_ratio = (ymax - ymin) / span     # 信号占屏比

    tag = "CH%d[%g V/div, 偏置 %+g V, 量程 %.2f..%.2f V]" % (
        ch, scale, offs, lo, hi)
    if top_margin < 0.01:
        return {"ok": False, "level": "clip_top",
                "msg": "%s 顶部削顶：信号最高 %.3f V 已顶到量程上限 %.3f V"
                       % (tag, ymax, hi)}
    if bot_margin < 0.01:
        return {"ok": False, "level": "clip_bot",
                "msg": "%s 底部削顶：信号最低 %.3f V 已顶到量程下限 %.3f V"
                       % (tag, ymin, lo)}
    if sig_ratio < 0.15:
        return {"ok": False, "level": "too_small",
                "msg": "%s 信号仅占屏 %.0f%%（档位偏大，量化精度差）"
                       % (tag, sig_ratio * 100)}
    return {"ok": True, "level": "ok",
            "msg": "%s 正常：信号 %.3f..%.3f V，占屏 %.0f%%，上下余量 %.0f%%/%.0f%%"
                   % (tag, ymin, ymax, sig_ratio * 100,
                      top_margin * 100, bot_margin * 100)}


def clip_fix_hint(ch, ymin, ymax):
    """给档位调整建议。"""
    vpp = max(ymax - ymin, 1e-9)
    want = vpp * 1.35 / 8.0        # 留 35% 余量
    nice = [0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0]
    pick = nice[-1]
    for s in nice:
        if s >= want:
            pick = s
            break
    return ("  → 建议把 CH%d 档位设为 ~%g V/div（当前信号峰峰 %.2f V）"
            "，命令：:CHANnel%d:SCALe %g" % (ch, pick, vpp, ch, pick))


# ============================ 取数 ============================

def apply_vertical(scope, args):
    """按 --ch-scale / --ch-offset 重设 SCL/SDA 两个通道的垂直档位。

    为什么要这个开关（2026-09-28 实测）：
      DC 电气判定（尤其 VOL≤0.4 V）需要足够的量化分辨率。2 V/div 时满量程 16 V、
      8 bit → LSB ≈ 67 mV，是 0.4 V 门限的 17%，读数只能落在 67 mV 的格子上，
      两条线的 VOL 都被判成 INDET（读数分别 0.3861 / 0.3439 V，与门限差 <1 LSB）。
      判 VOL 应改用 ≤0.5 V/div（满量程 4 V、LSB ≈ 16 mV），把门限测清楚。
    """
    if args.ch_scale is None:
        return
    for ch in (args.ch_scl, args.ch_sda):
        scope.cmd(":CHANnel%d:SCALe %E" % (ch, args.ch_scale))
        if args.ch_offset is not None:
            scope.cmd(":CHANnel%d:OFFSet %E" % (ch, args.ch_offset))
        time.sleep(0.12)
    print("[档位] CH%d/CH%d -> %g V/div%s"
          % (args.ch_scl, args.ch_sda, args.ch_scale,
             "" if args.ch_offset is None else "，offset %g V" % args.ch_offset))


def precheck(scope, args):
    print("=" * 78)
    print("连接 & 档位预检")
    print("=" * 78)
    print("IDN :", scope.idn())
    print("探头:", scope.probes())
    st = scope.state()
    for k in ("tb_scale", "tb_delay", "acquire", "trigger", "running"):
        if k in st:
            print("  %-10s: %s" % (k, st[k]))
    print("错误队列:", scope.err())

    allok = True
    for ch in (args.ch_scl, args.ch_sda):
        try:
            # 窗口至少覆盖几个 I2C 时钟周期，否则可能整段落在总线空闲里、
            # 误判成「信号太小 / 档位偏大」。
            d = scope.fetch(ch, args.precheck_points)
        except ScopeError as e:
            print("\nCH%d 取数失败: %s" % (ch, e))
            allok = False
            continue
        r = clipping_check(scope, ch, d)
        print("\n%s" % ("[OK]  " + r["msg"] if r["ok"] else "[!!]  " + r["msg"]))
        if not r["ok"]:
            allok = False
            print(clip_fix_hint(ch, min(d["y"]), max(d["y"])))
    print("\n预检结论：%s" % ("可以开测 ✅" if allok else "先修档位再测 ❌"))
    return 0 if allok else 2


def capture_wave(scope, args):
    """停止态取数，自适应 points（SRATe×RANGe → 原生 1 ns/点）。"""
    scope.cmd(":STOP")
    time.sleep(0.4)
    if args.tb:
        scope.cmd(":TIMebase:SCALe %E" % args.tb)
        time.sleep(0.2)
    if args.delay is not None:
        scope.cmd(":TIMebase:DELay %E" % args.delay)
        time.sleep(0.3)
    d1 = scope.fetch(args.ch_scl, args.points)
    d2 = scope.fetch(args.ch_sda, args.points)
    n = len(d1["y"])
    T = [(i - d1["xref"]) * d1["xinc"] + d1["xorg"] for i in range(n)]
    return {"T": T, "Y1": d1["y"], "Y2": d2["y"]}, d1, d2


def do_capture(args):
    scope = Scope(args.ip, args.port, timeout=20)
    try:
        apply_vertical(scope, args)
        wave, d1, d2 = capture_wave(scope, args)
        os.makedirs(args.out, exist_ok=True)
        path = os.path.join(args.out, "wave.json")
        json.dump(wave, open(path, "w", encoding="utf-8"))
        print("波形已存 -> %s  (%d 点, 跨度 %.2f µs, %.3f ns/点)"
              % (path, len(wave["T"]),
                 (wave["T"][-1] - wave["T"][0]) * 1e6, d1["xinc"] * 1e9))

        # ★ 削顶强制自检
        print("\n--- 削顶 / 档位自检（取数后强制关卡）---")
        worst = None
        for ch, d in ((args.ch_scl, d1), (args.ch_sda, d2)):
            r = clipping_check(scope, ch, d)
            print(("[OK]  " if r["ok"] else "[!!]  ") + r["msg"])
            if not r["ok"]:
                print(clip_fix_hint(ch, min(d["y"]), max(d["y"])))
                worst = r
        if worst:
            print("\n⚠️  档位不合格：这批数据**不能作为电气判定依据**，请调档后重采。")
            if args.strict_clip:
                print("    (--strict-clip 已开启，中止)")
                return 3
        else:
            print("\n档位自检通过 ✅")
        return 0
    finally:
        scope.close()


# ============================ 重新触发抓取 ============================

def arm_trigger(scope, args, settle=None):
    """配边沿触发 → 重新单次采集 → 停住。是否真抓到由调用方看波形判定。

    为什么需要它（2026-09-28 实机教训）：
      本机没有 I2C 触发选件，只能按 SCL 边沿触发。而 `capture_wave` 是
      「STOP 后读当前采集内存」——**不重新触发就永远是同一帧**。
      要命中目标器件那一帧，必须重新 arm 触发反复抓。

    ⚠️ 判「触发是否完成」**绝不能查 `:TRIGger:STATus?`**：本机 RUN 态下该查询
       挂满 socket 超时（pitfalls C5），20 轮 × 2s 也等不到 "STOP"，
       现场表现是「每次都报未等到触发」—— 首次实现就这么错了。
       改为固定等待后 `:STOP`，由调用方检查这一屏能否解出 I2C 事务。
    """
    settle = args.arm_settle if settle is None else settle
    scope.cmd(":TRIGger:MODE EDGE")
    scope.cmd(":TRIGger:EDGE:SOURce CHANnel%d" % args.ch_scl)
    scope.cmd(":TRIGger:EDGE:SLOPe FALLing")
    scope.cmd(":TRIGger:EDGE:LEVel %E" % args.trig_level)
    scope.cmd(":TRIGger:SWEep NORMal")
    if args.tb:
        scope.cmd(":TIMebase:SCALe %E" % args.tb)
    if args.delay is not None:
        scope.cmd(":TIMebase:DELay %E" % args.delay)
    scope.cmd(":RUN")
    time.sleep(settle)
    scope.cmd(":SINGle")
    time.sleep(args.arm_wait_s)
    scope.cmd(":STOP")
    time.sleep(0.3)
    return True


def do_grab(args):
    """反复 arm 触发 + 取数 + 解码，直到抓到 --addr 指定的那一帧。

    本机无 I2C 触发选件 → 边沿触发抓到哪帧随机 → 只能靠重试命中。
    命中后 wave.json / decode.json 都留在 --out，可直接接着跑 cursors / shots。
    """
    want = parse_addr(args.addr)
    os.makedirs(args.out, exist_ok=True)
    wpath = os.path.join(args.out, "wave.json")
    scope = Scope(args.ip, args.port, timeout=20)
    seen = []
    try:
        apply_vertical(scope, args)
        for attempt in range(1, args.max_tries + 1):
            print("\n=== 尝试 %d/%d：arm 触发 → 取数 → 解码 ===" % (attempt, args.max_tries))
            arm_trigger(scope, args)
            wave, d1, d2 = capture_wave(scope, args)
            json.dump(wave, open(wpath, "w", encoding="utf-8"))
            dec = decode_analyze(wpath, args.spec, vdd=args.vdd, rp=args.rp)
            got_s = dec.get("addr7")
            nb = len(dec.get("bytes") or [])
            if not nb:
                print("  这一屏没解出 I2C 事务（可能没触发到），重试")
                continue
            print("  抓到 first_byte=%s  addr7=%s  帧长=%d 字节  fSCL=%.2f kHz"
                  % (dec.get("first_byte"), got_s, nb,
                     (dec.get("clock") or {}).get("f_khz", 0) or 0))
            if got_s:
                seen.append(got_s)
            if got_s and int(got_s, 16) == want:
                json.dump(dec, open(os.path.join(args.out, "decode.json"), "w",
                                    encoding="utf-8"),
                          ensure_ascii=False, indent=2)
                print("\n[OK] 命中目标 0x%02X（第 %d 次尝试）-> wave.json / decode.json 已就位"
                      % (want, attempt))
                print("     接下来：redo_psu.py --addr %s --out %s cursors shots"
                      % (args.addr, args.out))
                return 0
            print("  [X] 不是目标（要 0x%02X），重新触发 ..." % want)
        print("\n[X] %d 次尝试都没抓到 0x%02X" % (args.max_tries, want))
        print("     期间抓到的地址: %s" % (", ".join(seen) if seen else "(无)"))
        print("     处置：确认器件是否在通信 / 加大 --max-tries / 核对 --addr")
        return 2
    finally:
        scope.close()


# ============================ 截图 ============================

def do_shots(args):
    wpath = os.path.join(args.out, "wave.json")
    if not os.path.exists(wpath):
        print("找不到 %s，请先 capture" % wpath)
        return 1
    # ★ 出图前复核地址：截图是最终交付物，宁可不出图也不能张冠李戴
    dec = load_dec(os.path.join(args.out, "decode.json"))
    if dec is None:
        print("[X] 缺少 decode.json，无法核对地址 —— 出图前必须先 decode（并过地址闸门）")
        return 1
    if not addr_gate(dec, args, "shots"):
        print("\n[X] 地址闸门未通过，拒绝出图。")
        return 2
    wave = load_wave_json(wpath)
    t, y1, y2 = wave["T"], wave["Y1"], wave["Y2"]
    pairs = compute_cursor_pairs(t, y1, y2)
    os.makedirs(args.out, exist_ok=True)

    scope = Scope(args.ip, args.port, timeout=20)
    try:
        made = []

        def restore_tb(tag=""):
            """每张图前确保时基没被改动。

            实测（2026-09-28, DSO-X 6004A 10.121.136.251）：首次调用 screenshot_best
            探测 :SCReen:DUMP? 会**超时 15 s 并把仪器时基搞乱**（200µs/div → 50µs/div），
            后果是 ① 波形被截断 ② 游标位置被钳到窗口边缘（X1=X2=250µs、ΔX=0，
            屏幕提示「控制已达到极限」）。所以每次出图前都重置一遍。
            """
            if not args.tb:
                return
            cur = scope.qf(":TIMebase:SCALe?", 0.0)
            if abs(cur - args.tb) > args.tb * 1e-6:
                scope.cmd(":TIMebase:SCALe %E" % args.tb)
                time.sleep(0.3)
                print("       [fix] 时基被改动 %.3g -> %.3g s/div，已重置 %s"
                      % (cur, args.tb, tag))

        # 0) 原始完整屏幕备份（未加测量 / 未设游标）
        p00 = os.path.join(args.out, "00_原始完整采集_未改触发未重新采集.png")
        if os.path.exists(p00) and not args.overwrite:
            print("  [skip] %s 已存在（--overwrite 可覆盖）" % os.path.basename(p00))
        else:
            shot_path = scope.screenshot_best(p00)
            made.append(p00)
            print("  [00] 原始完整采集 -> %s  (截图路径: %s)" % (os.path.basename(p00), shot_path))
            if shot_path == "DISPlay:DATA":
                print("       [!] 本机 :SCReen:DUMP? 不可用，已降级为 :DISPlay:DATA?（白底、X1 不渲染）")
                print("           -> 报告里必须注明，不得当作完整游标图使用")
        scope.clear_markers()

        # 1) 地址总览（地址段 + 右侧测量面板）
        if args.tb:
            scope.cmd(":TIMebase:SCALe %E" % args.tb)
            time.sleep(0.2)
        if args.overview_delay is not None:
            scope.cmd(":TIMebase:DELay %E" % args.overview_delay)
        time.sleep(0.4)
        restore_tb("01b")
        scope.add_meas(ch1=MEAS_CH1, ch2=MEAS_CH2, clear=True)
        f = os.path.join(args.out, SHOT_NAMES["overview"] % args.addr)
        scope.screenshot_best(f)
        made.append(f)
        print("  [01b] 地址总览 -> %s" % os.path.basename(f))
        scope.clear_markers()

        # 2) CLK / SDA 各自测量页
        for tag, ch, meas in (("clk", args.ch_scl, MEAS_CH1),
                              ("sda", args.ch_sda, MEAS_CH2)):
            restore_tb(tag)
            scope.add_meas(ch1=meas if ch == args.ch_scl else [],
                           ch2=meas if ch == args.ch_sda else [], clear=True)
            f = os.path.join(args.out, SHOT_NAMES[tag])
            scope.screenshot_best(f)
            made.append(f)
            print("  [%s] -> %s" % (tag, os.path.basename(f)))
        scope.clear_markers()

        # 3) 5 张时序游标图
        for key in SHOT_ORDER:
            if key not in pairs:
                print("  [skip] 无 %s 游标对（本段无对应事件）" % key)
                continue
            x1, x2 = pairs[key]
            restore_tb(key)
            scope.set_markers(x1 * 1e6, x2 * 1e6)
            f = os.path.join(args.out, SHOT_NAMES[key])
            scope.screenshot_best(f)
            made.append(f)
            print("  [%s] X1=%.4f X2=%.4f ΔX=%.4f µs -> %s"
                  % (key, x1 * 1e6, x2 * 1e6, (x2 - x1) * 1e6,
                     os.path.basename(f)))
            scope.clear_markers()
        print("\n截图完成 %d 张 -> %s" % (len(made), args.out))
        return 0
    finally:
        scope.clear_markers()
        scope.close()


def snapshot_original(args):
    """在改动显示前，先把仪器当前屏幕存为 00 备份。"""
    scope = Scope(args.ip, args.port, timeout=20)
    try:
        p00 = os.path.join(args.out, "00_原始完整采集_未改触发未重新采集.png")
        os.makedirs(args.out, exist_ok=True)
        if os.path.exists(p00) and not args.overwrite:
            print("  [skip] 00 备份已存在（--overwrite 可覆盖）")
            return 0
        shot_path = scope.screenshot_best(p00)
        print("  [00] 原始完整采集 -> %s  (截图路径: %s)"
              % (os.path.basename(p00), shot_path))
        if shot_path == "DISPlay:DATA":
            print("       [!] 本机 :SCReen:DUMP? 不可用，已降级为 :DISPlay:DATA?")
        return 0
    finally:
        scope.close()


# ============================ 地址闸门 ============================

def parse_addr(s):
    """'0x5A' / '5A' / '90'(十进制) → int。"""
    s = str(s).strip()
    if s.lower().startswith("0x"):
        return int(s, 16)
    try:
        return int(s)
    except ValueError:
        return int(s, 16)


def load_dec(path):
    if not os.path.exists(path):
        return None
    try:
        return json.load(open(path, encoding="utf-8"))
    except Exception:
        return None


def addr_gate(dec, args, where="decode"):
    """★ 地址闸门：实测地址必须等于 --addr，否则拒绝继续出图。

    为什么设这道闸（2026-09-28 实机教训，一次真实误归档）：
      本机 DSO-X 6004A(10.121.136.251) **没有 I2C 触发选件**——
      `:TRIGger:MODE I2C` 返回 `-224 Illegal parameter value`，只能退化成
      SCL 边沿触发。此时 `--addr` **只用于命名目录/文件名，不参与触发**，
      抓到总线上哪一帧纯属随机（0x58 与 0x5A 都在跑）。
      当日即因此把一帧 **0x58** 的波形存进 `0x5A/` 目录、复制进桌面图集、
      还更新了说明文档——而 `decode.json` 里 `addr7=0x58` 写得清清楚楚，
      只是没人核对。张冠李戴的取证数据比没有数据更坏，故设强制闸门。
    """
    if getattr(args, "allow_any_addr", False):
        print("[!] --allow-any-addr 已开启，跳过地址校验（%s）" % where)
        return True
    want = parse_addr(args.addr)
    got_s = dec.get("addr7")
    if got_s is None:
        print("[X] 解码结果缺 addr7 字段，无法校验地址（%s）" % where)
        return False
    got = int(got_s, 16)
    ok = (got == want)
    print("\n%s 地址闸门(%s): 目标 %s (0x%02X) | 实测 first_byte=%s addr7=%s (0x%02X) -> %s"
          % ("[OK]" if ok else "[X]", where, args.addr, want,
             dec.get("first_byte"), got_s, got, "通过" if ok else "不匹配"))
    if not ok:
        print("     [X] 抓到的不是目标器件：本机无 I2C 触发选件，边沿触发抓到哪帧是随机的。")
        print("     处置：① 重新 arm 触发再抓（grab_addr 子命令会自动重试）；")
        print("           ② 核对 --addr 是否写错；③ 确实要接受任意帧时才加 --allow-any-addr。")
    return ok


# ============================ 解码 / 游标 ============================

def do_decode(args):
    wpath = os.path.join(args.out, "wave.json")
    if not os.path.exists(wpath):
        print("找不到 %s" % wpath)
        return 1
    dec = decode_analyze(wpath, args.spec, vdd=args.vdd, rp=args.rp)
    print(decode_fmt(dec))
    json.dump(dec, open(os.path.join(args.out, "decode.json"), "w",
                        encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\n解码结果 -> %s" % os.path.join(args.out, "decode.json"))
    if not addr_gate(dec, args, "decode"):
        return 2
    return 0


def do_cursors(args):
    wpath = os.path.join(args.out, "wave.json")
    wave = load_wave_json(wpath)
    pairs = compute_cursor_pairs(wave["T"], wave["Y1"], wave["Y2"])
    out = {k: {"x1": v[0], "x2": v[1], "x1_us": v[0] * 1e6,
               "x2_us": v[1] * 1e6, "dx_us": (v[1] - v[0]) * 1e6}
           for k, v in pairs.items()}
    json.dump(out, open(os.path.join(args.out, "cursors.json"), "w",
                        encoding="utf-8"), ensure_ascii=False, indent=2)
    print("游标边沿对 -> %s" % os.path.join(args.out, "cursors.json"))
    for k, v in out.items():
        print("  %-9s X1=%.4f  X2=%.4f  ΔX=%.4f µs"
              % (k, v["x1_us"], v["x2_us"], v["dx_us"]))
    return 0


# ============================ 主入口 ============================

def main():
    ap = argparse.ArgumentParser(
        description="单个 PSU 实验重测 → 标准取证图集")
    ap.add_argument("--ip", default="10.121.136.155")
    ap.add_argument("--port", type=int, default=5025)
    ap.add_argument("--addr", default="0x5A", help="器件地址，如 0x5A")
    ap.add_argument("--ch-scl", type=int, default=1)
    ap.add_argument("--ch-sda", type=int, default=2)
    ap.add_argument("--tb", type=float, default=None,
                    help="时基(秒)；不传则不改仪器当前时基")
    ap.add_argument("--delay", type=float, default=None,
                    help="水平延迟(秒)；不传则不改")
    ap.add_argument("--overview-delay", type=float, default=None,
                    help="01b 地址总览图的水平延迟(秒)")
    ap.add_argument("--points", type=int, default=None,
                    help="取数点数；不传则自适应 SRATe×RANGe（原生 1ns/点）")
    ap.add_argument("--precheck-points", type=int, default=50000,
                    help="预检窗口点数（默认 50000 ≈ 50µs，覆盖几个 I2C 周期）")
    ap.add_argument("--spec", choices=["std", "fm", "smbus"], default="std")
    ap.add_argument("--vdd", type=float, default=None)
    ap.add_argument("--rp", type=float, default=None)
    ap.add_argument("--out", default=r"D:\Desktop\I2C四个实验图\0x5A")
    ap.add_argument("--overwrite", action="store_true",
                    help="允许覆盖已存在的 00 备份")
    ap.add_argument("--strict-clip", action="store_true",
                    help="削顶自检不通过时中止后续流程")
    ap.add_argument("--allow-any-addr", action="store_true",
                    help="跳过地址闸门（仅在确实要接受任意帧时使用）")
    ap.add_argument("--reuse-wave", action="store_true",
                    help="all 流程中复用已有 wave.json，不重新触发抓取")
    ap.add_argument("--trig-level", type=float, default=1.65,
                    help="边沿触发阈值(V)，默认 1.65 ≈ 3.3V 总线中点")
    ap.add_argument("--arm-settle", type=float, default=1.0,
                    help="arm 后等总线跑起来的秒数（默认 1.0）")
    ap.add_argument("--ch-scale", type=float, default=None,
                    help="重设 SCL/SDA 两个通道的垂直档位(V/div)；DC 电气测量建议 0.5")
    ap.add_argument("--ch-offset", type=float, default=None,
                    help="配合 --ch-scale 的垂直偏置(V)，如 1.65（3.3V 总线中点）")
    ap.add_argument("--arm-wait-s", type=float, default=2.0,
                    help="单次采集的固定等待秒数（默认 2.0；不用 :TRIGger:STATus? 轮询）")
    ap.add_argument("--max-tries", type=int, default=15,
                    help="grab 模式最多重新触发次数（默认 15）")
    ap.add_argument("cmd", nargs="+",
                    choices=["precheck", "capture", "decode", "cursors",
                             "shots", "snapshot", "grab", "all"])
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)
    cmds = args.cmd
    rc = 0

    if "all" in cmds:
        rc = precheck_and_all(args)
        return rc

    if "precheck" in cmds:
        sc = Scope(args.ip, args.port, timeout=20)
        try:
            rc = precheck(sc, args)
        finally:
            sc.close()
        if rc != 0:
            return rc
    if "snapshot" in cmds:
        rc = snapshot_original(args)
        if rc != 0:
            return rc
    if "grab" in cmds:
        rc = do_grab(args)
        if rc != 0:
            return rc
    if "capture" in cmds:
        rc = do_capture(args)
        if rc != 0:
            return rc
    if "decode" in cmds:
        rc = do_decode(args)
        if rc != 0:
            return rc
    if "cursors" in cmds:
        rc = do_cursors(args)
        if rc != 0:
            return rc
    if "shots" in cmds:
        rc = do_shots(args)
        if rc != 0:
            return rc
    return rc


def precheck_and_all(args):
    # 1) 预检
    sc = Scope(args.ip, args.port, timeout=20)
    try:
        rc = precheck(sc, args)
    finally:
        sc.close()
    if rc != 0 and args.strict_clip:
        print("预检不合格且 --strict-clip 开启，中止。")
        return rc
    # 2) 原屏备份（改显示前）
    snapshot_original(args)
    # 3) ★ 重新触发抓取「目标地址那一帧」
    #    本机无 I2C 触发选件 → 边沿触发抓到哪帧随机，必须重试命中；
    #    命中不了就中止，绝不拿别的地址的帧当目标器件出图。
    if not args.reuse_wave:
        rc = do_grab(args)
        if rc != 0:
            print("\n[X] 未能抓到 --addr 指定的那一帧，中止（不出图，避免张冠李戴）。")
            return rc
    # 4) 取数复核 + 削顶自检（对刚抓到的这一帧）
    rc = do_capture(args)
    if rc != 0 and args.strict_clip:
        return rc
    # 5) 解码（含地址闸门）
    if do_decode(args) != 0:
        return 1
    # 6) 游标
    do_cursors(args)
    # 7) 截图
    return do_shots(args)


if __name__ == "__main__":
    raise SystemExit(main())
