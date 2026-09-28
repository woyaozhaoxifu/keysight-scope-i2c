#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Keysight I2C Timing Test - Quick Start Launcher
统一入口，自动把 scripts/ 加入 sys.path，无需 cd。

用法示例：
    python run_i2c_test.py --help              # 列出全部子命令
    python run_i2c_test.py selftest             # 判定前必须先跑，纯函数自测（33 项）
    python run_i2c_test.py sandbox              # 不连真机，沙箱端到端验证
    python run_i2c_test.py full                 # 全量低速覆盖报告（不连真机）
    python run_i2c_test.py connect --ip 10.121.136.251
    python run_i2c_test.py check --ip 10.121.136.251
    python run_i2c_test.py test --ip 10.121.136.251 --addr 0x5B --spec std
    python run_i2c_test.py batch --ip 10.121.136.251 --addr 0x58,0x59,0x5A,0x5B
    python run_i2c_test.py batch --ip 10.121.136.251 --addr 0x58 --addr 0x59 --addr 0x5A

注：示波器 IP 以现场机台为准（本文件示例用当前机台 10.121.136.251；
    历史机台 10.121.136.155 已离线）。
"""
import argparse
import os
import subprocess
import sys
import textwrap

SCRIPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scripts")
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


def run(script_name, extra_args=None):
    args = [sys.executable, os.path.join(SCRIPT_DIR, script_name)]
    if extra_args:
        args.extend(extra_args)
    return subprocess.run(args, capture_output=False).returncode


def run_selftest():
    print("=" * 60)
    print("纯函数自测（33 项）—— 判定前必须先跑")
    print("=" * 60)
    return run("selftest.py")


def run_sandbox():
    print("=" * 60)
    print("沙箱验证：起 mock 示波器，端到端跑通在线链路")
    print("=" * 60)
    return run("sandbox_test.py")


def run_full():
    print("=" * 60)
    print("全量低速测试覆盖报告：DC + 时钟 + 6×三档 + Tr/Tf + 拉伸/毛刺/Sr")
    print("=" * 60)
    return run("full_lowspeed_test.py")


def main():
    parser = argparse.ArgumentParser(
        prog="run_i2c_test.py",
        description="Keysight I2C 低速时序测试 — 统一快速启动入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(__doc__),
    )
    sub = parser.add_subparsers(dest="cmd", title="子命令", required=True)

    # ── selftest ──────────────────────────────────────────────
    p = sub.add_parser("selftest", help="纯函数自测（33 项），判定前必须先跑")
    p.add_argument("-v", "--verbose", action="store_true", help="详细输出")

    # ── sandbox ───────────────────────────────────────────────
    p = sub.add_parser("sandbox", help="沙箱端到端验证（不连真机）")

    # ── full ──────────────────────────────────────────────────
    p = sub.add_parser("full", help="全量低速测试覆盖报告（不连真机）")

    # ── connect / check / setup ────────────────────────────────
    for cmd, help_text in [
        ("connect", "连接示波器并显示状态"),
        ("check",   "信号自检（CH1/CH2 峰峰值、频率）"),
        ("setup",   "自动配置示波器（开通道 + AutoScale + I2C 触发）"),
    ]:
        p = sub.add_parser(cmd, help=help_text)
        p.add_argument("--ip", "-i", required=True, help="示波器 IP 地址")
        if cmd == "setup":
            p.add_argument("--ch-scl", type=int, default=1, help="SCL 通道（默认 1）")
            p.add_argument("--ch-sda", type=int, default=2, help="SDA 通道（默认 2）")
            p.add_argument("--trigger-addr", type=int, default=None,
                           help="I2C 触发地址（7 位，如 0x5B）")

    # ── test / run ─────────────────────────────────────────────
    for cmd in ("test", "run"):
        p = sub.add_parser(cmd, help="完整流水线（capture → decode → cursors → shots → fill）")
        p.add_argument("--ip", "-i", required=True, help="示波器 IP 地址")
        p.add_argument("--addr", "-a", required=True, help="器件地址（如 0x5B）")
        p.add_argument("--spec", "-s", choices=["std", "fm", "smbus"], default="std",
                       help="规格档（默认 std）")
        p.add_argument("--out", "-o", default=None,
                       help="输出目录（默认 ./i2c_out/<addr>）")
        p.add_argument("--xlsx", default=None, help="Excel 报告文件路径")
        p.add_argument("--sheet", default="时序测试", help="Excel 工作表名（默认 时序测试）")
        p.add_argument("--ch-scl", type=int, default=1)
        p.add_argument("--ch-sda", type=int, default=2)

    # ── batch ──────────────────────────────────────────────────
    p = sub.add_parser("batch", help="多器件地址批量测试，回填 Excel（每地址独立目录）")
    p.add_argument("--ip", "-i", required=True, help="示波器 IP 地址")
    p.add_argument("--addr", "-a", action="append", required=True,
                   dest="addrs", help="器件地址（可多次指定，或用逗号分隔，如 --addr 0x58,0x59）")
    p.add_argument("--spec", "-s", choices=["std", "fm", "smbus"], default="std")
    p.add_argument("--base-out", default=None, help="输出根目录（默认 ./i2c_out_bulk）")
    p.add_argument("--xlsx", default=None, help="Excel 报告文件路径")
    p.add_argument("--sheet", default="时序测试", help="Excel 工作表名")
    p.add_argument("--ch-scl", type=int, default=1)
    p.add_argument("--ch-sda", type=int, default=2)

    args = parser.parse_args()

    # ── selftest / sandbox / full（无需 IP） ───────────────────
    if args.cmd == "selftest":
        return run_selftest()
    if args.cmd == "sandbox":
        return run_sandbox()
    if args.cmd == "full":
        return run_full()

    # ── connect / check / setup ────────────────────────────────
    if args.cmd in ("connect", "check"):
        return run("i2c_full_test.py", ["--ip", args.ip, args.cmd])

    if args.cmd == "setup":
        extra = ["--ch-scl", str(args.ch_scl), "--ch-sda", str(args.ch_sda)]
        if args.trigger_addr is not None:
            extra.extend(["--trigger-addr", str(args.trigger_addr)])
        extra.append("setup")
        return run("i2c_full_test.py", ["--ip", args.ip] + extra)

    # ── test / run ─────────────────────────────────────────────
    if args.cmd in ("test", "run"):
        extra = ["--ip", args.ip, "--addr", args.addr,
                 "--spec", args.spec,
                 "--ch-scl", str(args.ch_scl), "--ch-sda", str(args.ch_sda),
                 args.cmd]
        if args.out:
            extra.extend(["--out", args.out])
        if args.xlsx:
            extra.extend(["--xlsx", args.xlsx, "--sheet", args.sheet])
        return run("i2c_full_test.py", extra)

    # ── batch ──────────────────────────────────────────────────
    if args.cmd == "batch":
        # 支持 --addr 0x58,0x59,0x5A（逗号分隔）或多次 --addr 0x58 --addr 0x59
        all_addrs = []
        for a in args.addrs:
            all_addrs.extend(x.strip() for x in a.split(",") if x.strip())
        addr_list = " ".join(all_addrs)
        base_out = args.base_out or os.path.join(os.getcwd(), "i2c_out_bulk")
        extra = [
            "--ip", args.ip,
            "--base-out", base_out,
            "--addr-list", addr_list,
            "--spec", args.spec,
            "--ch-scl", str(args.ch_scl), "--ch-sda", str(args.ch_sda),
            "batch",
        ]
        if args.xlsx:
            extra.extend(["--xlsx", args.xlsx, "--sheet", args.sheet])
        return run("i2c_full_test.py", extra)

    return 0


if __name__ == "__main__":
    sys.exit(main())
