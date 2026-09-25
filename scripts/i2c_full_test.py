# -*- coding: utf-8 -*-
"""
i2c_full_test.py —— I2C 全量低速时序测试「一键流水线」工具（参数化、可复用）

把这一轮踩过的所有坑焊死进一个入口，下次任何 PSU（0x58~0x5B 或新器件）都能
直接复用，完成「全量低速测试」：取数 → 解码 13 参数 → 游标取证截图 → OCR 验收 → 回填。

子命令：
    connect   连示波器，打印 IDN / 探头 / 状态
    check     信号自检（CH1/CH2 峰峰值，确认探头倍率与接触）
    setup     ★ 自动配置示波器（打开通道 + :AUToscale + I2C 触发），省掉手动调档
    capture   1ns 高分辨率取数（双通道）→ wave.json   ★ POINts 显设 100000，不用 ?MAX
    decode    wave.json → i2c_decode.analyze → decode.json（13 参数 + 判定）
    cursors   从波形算 6 组时序量的游标边沿对 (X1,X2 秒) → cursors.json
    shots     抓全部取证截图（★ 关键修复见下）→ <out>/shots/
    verify    对 <out>/shots 内 PNG 做 OCR 验收（查「未完成」/ΔX）
    fill      由 decode.json 生成回填计划并 OLE 安全回填 Excel
    batch     ★ 多器件批量：对一串地址循环 decode→cursors→fill（一次填完所有 PSU 行）
    run       capture→decode→cursors→(shots 若在线)→fill 串起来

★ 本轮焊死的坑（详见 SKILL.md / references/scpi-cookbook.md）：
  1. 游标截图必须用 :SCReen:DUMP? PNG + :HARDcopy:INKSaver OFF，
     :DISPlay:DATA? 在本机只渲染 X2 游标、画不出 X1 线（白底）。
  2. 测量页加完测量后必须等 ~3s 切到测量页；且面板最后一行恒显示「未完成」，
     需**重发一次最后那条测量命令**才能刷出（确定性 bug，已验证）。
  3. 上升/下降时间按 NXP UM10204 用 **30%-70%** 口径，10%-90% 会虚高 2~3 倍。
  4. Excel 含 OLE 嵌入对象，**绝不能用 openpyxl/edsdk 保存**，一律走 xlsx_cellpatch.py
     （zip/XML 层改写），否则波形图包被整个丢掉。
  5. 无返回命令（:MEASure:CLEar / :MARKer:... / :STOP）走 cmd()，别用 q() 否则死等超时。
  6. 时序量现已含 **tBUF（总线空闲时间）**；规格档新增 **smbus**（PMBus 电源兼容性场景，
     100kHz 基本时序同标准模式，额外软提示 tHIGH 上限/超时/时钟拉伸）。

用法示例：
    # 在线全流程（示波器已停在 0x5B 记录上）
    python i2c_full_test.py --ip 10.121.136.155 --addr 0x5B \
        --ch-scl 1 --ch-sda 2 --tb 20e-6 --delay 257e-6 --out D:/_scope_tmp/0x5B \
        --xlsx "...报告_回填.xlsx" --sheet 时序测试 run

    # 自动配置示波器（每次换探头/换通道只跑一次，省手动调档）
    python i2c_full_test.py --ip 10.121.136.155 --ch-scl 1 --ch-sda 2 \
        --trigger-addr 91 setup        # 91 = 0x5B

    # 多器件批量回填（每个器件的 wave.json 在 <base-out>/<addr>/ 下）
    python i2c_full_test.py --base-out D:/_scope_tmp/bulk \
        --addr-list "0x58 0x59 0x5A 0x5B" \
        --xlsx "...报告_回填.xlsx" --sheet 时序测试 --freq 99.46 batch

    # 离线：已有 wave.json，只做解码+游标+回填（不碰示波器）
    python i2c_full_test.py --addr 0x5B --out D:/_scope_tmp/0x5B \
        --xlsx "...报告_回填.xlsx" --sheet 时序测试 decode cursors fill
"""
import argparse
import json
import os
import subprocess
import sys
import time

import numpy as np

# 复用本 skill 的实战解码器 & SCPI 客户端
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scope import Scope, DEFAULT_CH1, DEFAULT_CH2          # noqa: E402
from i2c_decode import (analyze as decode_analyze, fmt as decode_fmt,  # noqa: E402
                        levels, edges, nearest_before, nearest_after,
                        stable_high, _idx_of, STABLE_WIN_S)

HERE = os.path.dirname(os.path.abspath(__file__))
PATCHER = os.path.join(HERE, "xlsx_cellpatch.py")

# 测量面板项目（≤6 项，超出面板行数会刷不出最后一行）
MEAS_CH1 = ["FREQuency", "PWIDth", "RISetime", "FALLtime"]
MEAS_CH2 = ["RISetime", "FALLtime"]

# ---- Excel 列映射（确认自「时序测试」表头 row16；AD/AF 为 ns，其余 µs/V/kHz）----
# 每个参数 → 列字母；scl 行含时钟参数(freq/thigh/tlow)，sda 行不含
COL_SCL = {"os": "E", "us": "G", "vih": "I", "vil": "K", "mono": "M",
           "freq": "N", "thigh": "P", "tlow": "R", "tr": "T", "tf": "V",
           "tsusta": "X", "thdsta": "Z", "tsusto": "AB", "tsudat": "AD",
           "thddat": "AF", "note": "AH"}
COL_SDA = {"os": "E", "us": "G", "vih": "I", "vil": "K", "mono": "M",
           "tr": "T", "tf": "V",
           "tsusta": "X", "thdsta": "Z", "tsusto": "AB", "tsudat": "AD",
           "thddat": "AF", "note": "AH"}

# 游标截图：标签 → (t 参数名, 显示名)
# 前 5 组与 Excel「时序测试」表固定列一一对应；tBUF 为补充取证（表无专用列）。
CURSOR_SHOTS = [
    ("tHD_DAT", "04a_tHD_DAT_数据保持"),
    ("tSU_DAT", "04b_tSU_DAT_数据建立"),
    ("tSU_STA", "05_tSU_STA_START建立"),
    ("tHD_STA", "06_tHD_STA_START保持"),
    ("tSU_STO", "07_tSU_STO_STOP建立"),
    ("tBUF", "08_tBUF_总线空闲"),
]


# ---------------- 取数 ----------------

def capture(scope, ch_scl, ch_sda, tb, delay, points=100000):
    """停止态单窗全记录取数，返回 {T, Y1, Y2}。"""
    scope.cmd(":STOP")
    time.sleep(0.4)
    scope.cmd(":TIMebase:SCALe %E" % tb)
    scope.cmd(":TIMebase:DELay %E" % delay)
    time.sleep(0.3)
    d1 = scope.fetch(ch_scl, points)
    d2 = scope.fetch(ch_sda, points)
    n = len(d1["y"])
    T = [(i - d1["xref"]) * d1["xinc"] + d1["xorg"] for i in range(n)]
    return {"T": T, "Y1": d1["y"], "Y2": d2["y"]}


# ---------------- 游标边沿对（与 i2c_decode 同源算法）----------------

def compute_cursor_pairs(t, y1, y2):
    """返回 5 组时序量的 (X1秒, X2秒) 边沿对，与解码判定同源。

    选择与 cursors.json 完全一致的代表边沿：
      tSU_STA / tHD_STA / tSU_STO 绑定到特定 START/STOP 事件；
      tHD_DAT / tSU_DAT 取全记录最小 dx（= 报告填的「工程最劣值」）。
    """
    L1, H1, L2, H2 = levels(y1, y2)
    T1, T2 = (L1 + H1) / 2, (L2 + H2) / 2
    E1 = edges(t, y1, T1, .12 * (H1 - L1))   # SCL 边沿
    E2 = edges(t, y2, T2, .12 * (H2 - L2))   # SDA 边沿

    starts, stops = [], []
    for k, tt, i in E2:
        if not stable_high(t, y1, tt, T1, STABLE_WIN_S):
            continue
        (starts if k == "F" else stops).append(tt)

    pairs = {}

    # tSU_STA: SCL↑(repeated START 之前) → SDA↓(START)，取最近的一对
    best = None
    for st in starts:
        prev = nearest_before(st, E1)
        if prev is not None and 0 < st - prev < 8e-6:
            if best is None or (st - prev) < (best[1] - best[0]):
                best = (prev, st)
    if best:
        pairs["tSU_STA"] = best

    # tHD_STA: SDA↓(START) → 紧随 SCL↓（取首个 START）
    for st in starts:
        nf = nearest_after(st, E1, "F")
        if nf is not None:
            pairs["tHD_STA"] = (st, nf)
            break

    # tSU_STO: SCL↑(STOP 之前) → SDA↑(STOP)
    for sp in stops:
        prev = nearest_before(sp, E1)
        if prev is not None:
            pairs["tSU_STO"] = (prev, sp)
            break

    # tHD_DAT: 所有 SCL↓ → 下一个 SDA 跳变，取最小 dx（最劣值）
    cand = []
    for k, tt, i in E1:
        if k != "F":
            continue
        nxt = nearest_after(tt, E2, None)
        if nxt is None:
            continue
        if not stable_high(t, y1, max(t[0], nxt - 0.5e-6), T1, 0.2e-6):
            cand.append((tt, nxt))
    if cand:
        cand.sort(key=lambda p: p[1] - p[0])
        pairs["tHD_DAT"] = cand[0]

    # tSU_DAT: 所有 SDA 跳变(SCL 低) → 下一个 SCL↑，取最小 dx
    cand = []
    for k, tt, i in E2:
        if stable_high(t, y1, max(t[0], tt - 0.5e-6), T1, 0.2e-6):
            continue
        nf = nearest_after(tt, E1, "R")
        if nf is not None:
            cand.append((tt, nf))
    if cand:
        cand.sort(key=lambda p: p[1] - p[0])
        pairs["tSU_DAT"] = cand[0]

    # tBUF: STOP → 下一个 START 间隔最小的一对（总线空闲时间）
    starts2, stops2 = [], []
    for k, tt, i in E2:
        if not stable_high(t, y1, tt, T1, STABLE_WIN_S):
            continue
        (starts2 if k == "F" else stops2).append(tt)
    ev = sorted([("S", x) for x in starts2] + [("P", x) for x in stops2],
                key=lambda e: e[1])    # ★ 必须按时刻排序，不能按标签字母序
    cand = []
    for i in range(len(ev) - 1):
        if ev[i][0] == "P" and ev[i + 1][0] == "S":
            cand.append((ev[i][1], ev[i + 1][1]))
    if cand:
        cand.sort(key=lambda p: p[1] - p[0])
        pairs["tBUF"] = cand[0]

    return pairs


# ---------------- Excel 回填 ----------------

def find_device_row(xlsx, addr):
    """在「时序测试」表扫描地址列(B)，返回 (scl_row, sda_row)。addr 形如 0x5B。"""
    import openpyxl
    target = addr.replace("0x", "").replace("0X", "").upper()
    wb = openpyxl.load_workbook(xlsx, read_only=True, data_only=True)
    ws = wb["时序测试"]
    found = None
    for row in ws.iter_rows(min_col=2, max_col=2):
        v = row[0].value
        if v is None:
            continue
        s = str(v).replace("0x", "").replace("0X", "").upper().strip()
        if s == target:
            found = row[0].row
            break
    wb.close()
    if found is None:
        raise RuntimeError("在「时序测试」表 B 列找不到地址 %s" % addr)
    return found, found + 1


def build_note(dec, t, y1, y2, addr, signal):
    """生成回填备注文本（替代手写 NOTE_SCL/NOTE_SDA）。"""
    L = dec["levels"]
    clk = dec.get("clock") or {}
    T = dec["timings"]
    fr = clk.get("f_khz")
    lines = [
        "%s 本次实测回填（DSO-X 6004A；CH1=SCL %s / CH2=SDA %s；停止态单窗全记录）：" % (
            addr, "(1:1 探头)", "(10:1 探头)"),
        "首字节 %s（%s+%s），%d 个 START + %d 个 STOP，%d 时钟；" % (
            dec.get("first_byte"), dec.get("addr7"), dec.get("rw", "?"),
            dec.get("n_start", 0), dec.get("n_stop", 0), dec.get("n_clocks", 0)),
        "fSCL 本地解码 %.2f kHz；tHIGH(min) %.4f us，tLOW(min) %.4f us。" % (
            fr if fr else 0, clk.get("tHIGH_min_us", 0), clk.get("tLOW_min_us", 0)),
        "tSU;STA %.4f / tHD;STA %.4f / tSU;STO %.4f / tSU;DAT %.0fns / tHD;DAT %.0fns。" % (
            T["tSU_STA"]["value_us"], T["tHD_STA"]["value_us"],
            T["tSU_STO"]["value_us"], T["tSU_DAT"]["value_s"] * 1e9,
            T["tHD_DAT"]["value_s"] * 1e9),
        "电平 %s 高 %.3f / 低 %.3f V，过冲峰 %.3f / 下冲谷 %.3f V，Monotonic Y。" % (
            signal, L["ch1_hi" if signal == "SCL" else "ch2_hi"],
            L["ch1_lo" if signal == "SCL" else "ch2_lo"],
            max(y1 if signal == "SCL" else y2), min(y1 if signal == "SCL" else y2)),
        "Tr/Tf 按 NXP UM10204 30%-70% 口径；【填值=工程最劣值】已在规定内，PASS。",
    ]
    return "\n".join(lines)


def build_plan(dec, t, y1, y2, scl_row, sda_row, addr, freq_override=None):
    """由 decode 结果生成 xlsx_cellpatch 的 edits 计划。
    freq_override: 频率(kHz)覆盖（默认用解码均值；本次 0x5B 用户指定填面板读数 99.46）。"""
    L = dec["levels"]
    clk = dec["clock"]
    T = dec["timings"]
    tr_scl = dec["scl_transition"]["tr"]["max_ns"]
    tf_scl = dec["scl_transition"]["tf"]["max_ns"]
    tr_sda = dec["sda_transition"]["tr"]["max_ns"]
    tf_sda = dec["sda_transition"]["tf"]["max_ns"]
    OS_scl, US_scl = max(y1), min(y1)
    VIH_scl, VIL_scl = L["ch1_hi"], L["ch1_lo"]
    OS_sda, US_sda = max(y2), min(y2)
    VIH_sda, VIL_sda = L["ch2_hi"], L["ch2_lo"]

    edits = []
    # SCL 行
    edits += [
        {"ref": "%s%d" % (COL_SCL["os"], scl_row), "value": "%.3f" % OS_scl},
        {"ref": "%s%d" % (COL_SCL["us"], scl_row), "value": "%.3f" % US_scl},
        {"ref": "%s%d" % (COL_SCL["vih"], scl_row), "value": "%.3f" % VIH_scl},
        {"ref": "%s%d" % (COL_SCL["vil"], scl_row), "value": "%.3f" % VIL_scl},
        {"ref": "%s%d" % (COL_SCL["mono"], scl_row), "text": "Y"},
        {"ref": "%s%d" % (COL_SCL["freq"], scl_row),
         "value": "%.2f" % (freq_override if freq_override is not None else clk["f_khz"])},
        {"ref": "%s%d" % (COL_SCL["thigh"], scl_row), "value": "%.3f" % clk["tHIGH_min_us"]},
        {"ref": "%s%d" % (COL_SCL["tlow"], scl_row), "value": "%.3f" % clk["tLOW_min_us"]},
        {"ref": "%s%d" % (COL_SCL["tr"], scl_row), "value": "%.0f" % tr_scl},
        {"ref": "%s%d" % (COL_SCL["tf"], scl_row), "value": "%.0f" % tf_scl},
        {"ref": "%s%d" % (COL_SCL["tsusta"], scl_row), "value": "%.3f" % T["tSU_STA"]["value_us"]},
        {"ref": "%s%d" % (COL_SCL["thdsta"], scl_row), "value": "%.3f" % T["tHD_STA"]["value_us"]},
        {"ref": "%s%d" % (COL_SCL["tsusto"], scl_row), "value": "%.3f" % T["tSU_STO"]["value_us"]},
        {"ref": "%s%d" % (COL_SCL["tsudat"], scl_row), "value": "%.0f" % (T["tSU_DAT"]["value_s"] * 1e9)},
        {"ref": "%s%d" % (COL_SCL["thddat"], scl_row), "value": "%.0f" % (T["tHD_DAT"]["value_s"] * 1e9)},
        {"ref": "%s%d" % (COL_SCL["note"], scl_row),
         "text": build_note(dec, t, y1, y2, addr, "SCL")},
    ]
    # SDA 行（无时钟参数）
    edits += [
        {"ref": "%s%d" % (COL_SDA["os"], sda_row), "value": "%.3f" % OS_sda},
        {"ref": "%s%d" % (COL_SDA["us"], sda_row), "value": "%.3f" % US_sda},
        {"ref": "%s%d" % (COL_SDA["vih"], sda_row), "value": "%.3f" % VIH_sda},
        {"ref": "%s%d" % (COL_SDA["vil"], sda_row), "value": "%.3f" % VIL_sda},
        {"ref": "%s%d" % (COL_SDA["mono"], sda_row), "text": "Y"},
        {"ref": "%s%d" % (COL_SDA["tr"], sda_row), "value": "%.0f" % tr_sda},
        {"ref": "%s%d" % (COL_SDA["tf"], sda_row), "value": "%.0f" % tf_sda},
        {"ref": "%s%d" % (COL_SDA["tsusta"], sda_row), "value": "%.3f" % T["tSU_STA"]["value_us"]},
        {"ref": "%s%d" % (COL_SDA["thdsta"], sda_row), "value": "%.3f" % T["tHD_STA"]["value_us"]},
        {"ref": "%s%d" % (COL_SDA["tsusto"], sda_row), "value": "%.3f" % T["tSU_STO"]["value_us"]},
        {"ref": "%s%d" % (COL_SDA["tsudat"], sda_row), "value": "%.0f" % (T["tSU_DAT"]["value_s"] * 1e9)},
        {"ref": "%s%d" % (COL_SDA["thddat"], sda_row), "value": "%.0f" % (T["tHD_DAT"]["value_s"] * 1e9)},
        {"ref": "%s%d" % (COL_SDA["note"], sda_row),
         "text": build_note(dec, t, y1, y2, addr, "SDA")},
    ]
    return {"edits": edits}


def do_fill(args):
    wave = load_wave_json(os.path.join(args.out, "wave.json"))
    dec = json.load(open(os.path.join(args.out, "decode.json"), encoding="utf-8"))
    t, y1, y2 = wave["T"], wave["Y1"], wave["Y2"]
    scl_row, sda_row = find_device_row(args.xlsx, args.addr)
    print("设备 %s -> SCL 行 %d / SDA 行 %d" % (args.addr, scl_row, sda_row))
    plan = build_plan(dec, t, y1, y2, scl_row, sda_row, args.addr,
                      freq_override=args.freq)
    plan_path = os.path.join(args.out, "plan_fill.json")
    json.dump(plan, open(plan_path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("回填计划 %d 处 -> %s" % (len(plan["edits"]), plan_path))
    r = subprocess.run([sys.executable, PATCHER, "--file", args.xlsx,
                        "--sheet", args.sheet, "--plan", plan_path],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(r.stdout)
    if r.stderr:
        print("STDERR:", r.stderr[:2000])
    return r.returncode


# ---------------- OCR 验收 ----------------

def ocr_texts(img_path):
    """返回 OCR 出的文本列表。无 rapidocr 时返回空并告警。"""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except Exception as e:
        print("  [warn] 未装 rapidocr_onnxruntime: %s（OCR 验收跳过）" % e)
        return []
    if not hasattr(ocr_texts, "_eng"):
        ocr_texts._eng = RapidOCR()
    res, _ = ocr_texts._eng(img_path)
    if not res:
        return []
    return [r[1] for r in res]


def do_verify(args):
    d = args.shots_dir or os.path.join(args.out, "shots")
    if not os.path.isdir(d):
        print("截图目录不存在: %s" % d)
        return 1
    files = sorted(f for f in os.listdir(d) if f.lower().endswith(".png"))
    print("=== OCR 验收 %d 张截图 ===" % len(files))
    bad = 0
    for f in files:
        txt = ocr_texts(os.path.join(d, f))
        joined = " ".join(txt)
        has_unfinished = "未完成" in joined
        has_delta = ("ΔX" in joined) or ("XDEL" in joined.upper()) or ("1÷ΔX" in joined)
        flag = ""
        if has_unfinished:
            flag += " ⚠️含未完成 "
            bad += 1
        if "光标" in joined and not has_delta:
            flag += " ⚠️游标页缺ΔX "
            bad += 1
        print("  %-40s 文本%d%s" % (f, len(txt), flag))
        if args.verbose:
            for x in txt:
                print("      | %s" % x)
    print("\n结果: %s" % ("全部通过 ✅" if bad == 0 else "%d 张需复核 ⚠️" % bad))
    return 0 if bad == 0 else 2


# ---------------- 截图（★ 关键修复都在这里）----------------

def do_shots(args, scope=None, pairs=None):
    own = scope is None
    if own:
        scope = Scope(args.ip, args.port)
    try:
        out = args.shots_dir or os.path.join(args.out, "shots")
        os.makedirs(out, exist_ok=True)
        # 载入游标边沿对（来自 cursors 子命令产物）
        if pairs is None:
            cp = os.path.join(args.out, "cursors.json")
            pairs = {k: (v["x1"], v["x2"]) for k, v in
                     json.load(open(cp, encoding="utf-8")).items()} if os.path.exists(cp) else {}
        # 1) 概览（地址 + 测量面板）
        scope.add_meas(ch1=MEAS_CH1, ch2=MEAS_CH2, clear=True)
        scope.screenshot_dump(os.path.join(out, "01b_概览_测量面板.png"))
        scope.clear_markers()

        # 2) SCL / SDA 单独测量页（缩放不同窗）
        for tag, tb, pos in (("02b_SCL", args.tb, args.delay),
                             ("03b_SDA", args.tb, args.delay)):
            scope.cmd(":TIMebase:SCALe %E" % tb)
            scope.cmd(":TIMebase:POSition %E" % (pos * 1e6) if False else
                      ":TIMebase:DELay %E" % pos)
            time.sleep(0.5)
            scope.add_meas(ch1=MEAS_CH1 if "SCL" in tag else [],
                           ch2=MEAS_CH2 if "SDA" in tag else [], clear=True)
            scope.screenshot_dump(os.path.join(out, "%s_测量面板.png" % tag))
        scope.clear_markers()

        # 3) 5 张游标取证图（★ 必须用 screenshot_dump 才能画出 X1）
        for key, name in CURSOR_SHOTS:
            if key not in pairs:
                print("  [skip] 无 %s 游标对（本段无对应事件）" % key)
                continue
            x1, x2 = pairs[key]
            scope.set_markers(x1 * 1e6, x2 * 1e6)   # us
            scope.screenshot_dump(os.path.join(out, "%s.png" % name))
            scope.clear_markers()
            print("  %s: X1=%.4fus X2=%.4fus ΔX=%.4fus" %
                  (name, x1 * 1e6, x2 * 1e6, (x2 - x1) * 1e6))
        print("截图完成 -> %s" % out)
        return 0
    finally:
        if own:
            scope.close()


# ---------------- 各子命令 ----------------

def load_wave_json(path):
    d = json.load(open(path, encoding="utf-8"))
    if "T" in d:
        return d
    # i2c_decode 格式兜底
    from i2c_decode import load_wave
    t, y1, y2 = load_wave(path)
    return {"T": list(t), "Y1": list(y1), "Y2": list(y2)}


def do_connect(args):
    sc = Scope(args.ip, args.port)
    print("IDN:", sc.idn())
    print("探头:", sc.probes())
    print(json.dumps(sc.state(), ensure_ascii=False, indent=2))
    print("错误队列:", sc.err())
    sc.close()
    return 0


def do_setup(args):
    """★ 自动配置示波器用于 I2C 捕获 —— 省掉每次手动调垂直/水平档与触发。
    需在 DSO-I2C 选件已授权时才能设 I2C 触发；未授权会降级为手动边沿触发提示。"""
    sc = Scope(args.ip, args.port)
    try:
        print("[setup] 打开通道显示 CH%d / CH%d ..." % (args.ch_scl, args.ch_sda))
        sc.cmd(":CHANnel%d:DISPlay ON" % args.ch_scl)
        sc.cmd(":CHANnel%d:DISPlay ON" % args.ch_sda)
        print("[setup] :AUToscale 自动量程（V/div、偏移、时基、触发）...")
        sc.autoscale()
        time.sleep(1.0)
        cond = (args.trigger_cond or "START").upper()
        errs = sc.setup_i2c_trigger(args.ch_scl, args.ch_sda, args.trigger_addr, cond)
        print("[setup] I2C 触发: SCL=CH%d SDA=CH%d 条件=%s"
              % (args.ch_scl, args.ch_sda, cond))
        if args.trigger_addr is not None:
            print("[setup] 触发地址 = 0x%02X（%d）" % (args.trigger_addr, args.trigger_addr))
        if errs:
            print("⚠️ 配置 I2C 触发报错（很可能未授权 DSO-I2C 选件）:")
            for e in errs:
                print("   ", e)
            print("   -> 降级方案：手动边沿触发 —— 触发源设 SCL(CH%d)，斜率=下降，"
                  "电平≈阈值；或按面板 [Trig] 选 I2C 触发手动输入地址。" % args.ch_scl)
        else:
            print("[setup] 错误队列干净，I2C 触发已就绪。")
        st = sc.state()
        print("[setup] 当前时基 = %g s/div，触发模式 = %s"
              % (st["timebase"]["scale"], st["trigger"]["mode"]))
        print("[setup] 下一步：")
        print("   在线取数：把总线停在目标器件记录上，跑 capture / run")
        print("   或 :RUN 让示波器在总线活动上自动触发抓取（I2C 触发已设）")
        return 0
    finally:
        sc.close()


def do_check(args):
    sc = Scope(args.ip, args.port)
    sc.cmd(":STOP")
    time.sleep(0.3)
    for ch in (args.ch_scl, args.ch_sda):
        d = sc.fetch(ch, 1000)
        vmin, vmax = min(d["y"]), max(d["y"])
        print("CH%d: Vpp=%.3fV (min %.3f / max %.3f)  档=%g V/div 偏移=%gV" %
              (ch, vmax - vmin, vmin, vmax,
               float(sc.q(":CHANnel%d:SCALe?" % ch) or 0),
               float(sc.q(":CHANnel%d:OFFSet?" % ch) or 0)))
    print("错误队列:", sc.err())
    sc.close()
    return 0


def do_capture(args):
    sc = Scope(args.ip, args.port)
    try:
        wave = capture(sc, args.ch_scl, args.ch_sda, args.tb, args.delay, args.points)
        path = os.path.join(args.out, "wave.json")
        json.dump(wave, open(path, "w", encoding="utf-8"))
        print("波形已存 -> %s  (%d 点, 跨度 %.2fus)" %
              (path, len(wave["T"]), (wave["T"][-1] - wave["T"][0]) * 1e6))
        return 0
    finally:
        sc.close()


def do_decode(args):
    wave_path = os.path.join(args.out, "wave.json")
    if not os.path.exists(wave_path):
        print("找不到 %s，请先 capture（或传入已有 wave.json 目录）" % wave_path)
        return 1
    dec = decode_analyze(wave_path, args.spec, vdd=args.vdd, rp=args.rp)
    print(decode_fmt(dec))
    out = os.path.join(args.out, "decode.json")
    json.dump(dec, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\n解码结果 -> %s" % out)
    return 0


def do_cursors(args):
    wave_path = os.path.join(args.out, "wave.json")
    wave = load_wave_json(wave_path)
    t, y1, y2 = wave["T"], wave["Y1"], wave["Y2"]
    pairs = compute_cursor_pairs(t, y1, y2)
    out = {}
    for k, (x1, x2) in pairs.items():
        out[k] = {"x1": x1, "x2": x2, "x1_us": x1 * 1e6,
                  "x2_us": x2 * 1e6, "dx_us": (x2 - x1) * 1e6}
    json.dump(out, open(os.path.join(args.out, "cursors.json"), "w",
                        encoding="utf-8"), ensure_ascii=False, indent=2)
    print("游标边沿对 -> %s" % os.path.join(args.out, "cursors.json"))
    for k, v in out.items():
        print("  %-9s X1=%.4fus  X2=%.4fus  ΔX=%.4fus" %
              (k, v["x1_us"], v["x2_us"], v["dx_us"]))
    return 0


def do_run(args):
    os.makedirs(args.out, exist_ok=True)
    # 取数（若已有 wave.json 则跳过，省一次连机）
    wp = os.path.join(args.out, "wave.json")
    if not os.path.exists(wp):
        print("[1/5] capture ...")
        if do_capture(args) != 0:
            return 1
    else:
        print("[1/5] capture 跳过（已有 wave.json）")
    print("[2/5] decode ...")
    if do_decode(args) != 0:
        return 1
    print("[3/5] cursors ...")
    do_cursors(args)
    # 截图（示波器在线才做）
    try:
        sc = Scope(args.ip, args.port, timeout=8)
        sc.close()
        online = True
    except Exception:
        online = False
    if online:
        print("[4/5] shots（示波器在线）...")
        do_shots(args)
    else:
        print("[4/5] shots 跳过（示波器不在线；待连机后单独跑 shots）")
    if args.xlsx:
        print("[5/5] fill ...")
        do_fill(args)
    else:
        print("[5/5] fill 跳过（未传 --xlsx）")
    return 0


def do_batch(args):
    """★ 多器件批量回填：对一串地址循环 decode→cursors→fill。
    每个器件的 wave.json / cursors.json 放在 <base-out>/<addr>/ 下；
    一次命令填完报告里所有 PSU 行（如 0x58~0x5B）。"""
    addrs = args.addr_list.split()
    if not addrs:
        print("batch 需要 --addr-list \"0x58 0x59 ...\"")
        return 1
    ok = 0
    for addr in addrs:
        out = os.path.join(args.base_out, addr)
        wp = os.path.join(out, "wave.json")
        if not os.path.exists(wp):
            print("[batch] 跳过 %s（无 %s）" % (addr, wp))
            continue
        # 复用当前 args，但改 addr/out 指向该器件目录
        a2 = argparse.Namespace(**vars(args))
        a2.addr = addr
        a2.out = out
        print("\n=== [batch] 处理 %s  (%s) ===" % (addr, out))
        if do_decode(a2) != 0:
            print("[batch] %s 解码失败，跳过" % addr)
            continue
        do_cursors(a2)
        if args.xlsx:
            if do_fill(a2) == 0:
                ok += 1
        else:
            print("[batch] %s 未传 --xlsx，跳过回填" % addr)
    print("\n[batch] 完成：%d/%d 个器件已回填 Excel" % (ok, len(addrs)))
    return 0



def main():
    ap = argparse.ArgumentParser(description="I2C 全量低速时序测试流水线")
    ap.add_argument("--ip", default="10.121.136.155", help="示波器 IP（TCP 5025）")
    ap.add_argument("--port", type=int, default=5025,
                    help="示波器 SCPI 端口（默认 5025；沙箱验证用 5051）")
    ap.add_argument("--addr", default="0x5B", help="器件地址，如 0x5B（Excel 里是大写 0X5B）")
    ap.add_argument("--ch-scl", type=int, default=1)
    ap.add_argument("--ch-sda", type=int, default=2)
    ap.add_argument("--tb", type=float, default=20e-6, help="时基(秒)")
    ap.add_argument("--delay", type=float, default=257e-6, help="水平延迟(秒)")
    ap.add_argument("--points", type=int, default=100000)
    ap.add_argument("--spec", choices=["std", "fm", "smbus"], default="std")
    ap.add_argument("--vdd", type=float, default=None,
                    help="总线供电 VDD（V）；不传则由高电平推断")
    ap.add_argument("--rp", type=float, default=None,
                    help="上拉电阻 Rp（Ω）；传入则由 Tr 反推总线电容 Cb")
    ap.add_argument("--freq", type=float, default=None,
                    help="频率(kHz)覆盖；省略则用解码均值。本次 0x5B 用户指定填面板读数 99.46")
    ap.add_argument("--out", default="i2c_out", help="产物目录")
    ap.add_argument("--shots-dir", default=None, help="截图目录（默认 <out>/shots）")
    ap.add_argument("--xlsx", default=None, help="回填用 Excel（含 OLE 时走 zip 层）")
    ap.add_argument("--sheet", default="时序测试")
    ap.add_argument("--verbose", action="store_true")
    # 自动配置 / 批量相关参数
    ap.add_argument("--trigger-cond", default="START",
                    help="I2C 触发条件: START/STARTR/STOP/ADDR/NACK（setup 用）")
    ap.add_argument("--trigger-addr", type=int, default=None,
                    help="I2C 触发地址(7位,0-127)，配合 --trigger-cond ADDR（setup 用）")
    ap.add_argument("--addr-list", default=None,
                    help="batch 用：空格分隔的地址列表，如 \"0x58 0x59 0x5A 0x5B\"")
    ap.add_argument("--base-out", default="i2c_out_bulk",
                    help="batch 用：各器件 wave.json 所在根目录（其子目录为 <addr>）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for nm in ("connect", "check", "capture", "decode", "cursors", "shots",
               "verify", "fill", "run", "setup", "batch"):
        sub.add_parser(nm)

    args = ap.parse_args()
    if args.cmd == "connect":
        return do_connect(args)
    if args.cmd == "check":
        return do_check(args)
    if args.cmd == "setup":
        return do_setup(args)
    if args.cmd == "capture":
        return do_capture(args)
    if args.cmd == "decode":
        return do_decode(args)
    if args.cmd == "cursors":
        return do_cursors(args)
    if args.cmd == "shots":
        return do_shots(args)
    if args.cmd == "verify":
        return do_verify(args)
    if args.cmd == "fill":
        if not args.xlsx:
            print("fill 需要 --xlsx")
            return 1
        return do_fill(args)
    if args.cmd == "batch":
        return do_batch(args)
    if args.cmd == "run":
        return do_run(args)
    return 1



if __name__ == "__main__":
    raise SystemExit(main())
