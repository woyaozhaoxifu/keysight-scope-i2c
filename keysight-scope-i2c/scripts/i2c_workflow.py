#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
i2c_workflow.py —— I2C 时序测量正确流程（自包含，可独立运行）

固化踩坑后验证过的流程：
  1. SCPI 连 Keysight InfiniiVision（TCP 5025）
  2. 1ns 高分辨率取数（显设 :WAVeform:POINts 100000，不用 ?MAX）
  3. 无数据判据（POINts? <= 0 即报错，不卡 18s）
  4. 解码 + 测 12 参数 —— 委托 keysight-scope-scpi/i2c_decode.py（实战 24/24 回归）
  5. 规格判定 + 导出 JSON

依赖：
  - 连接/截图：本文件自带精简 SCPI 客户端。
  - 解码/测量：keysight-scope-scpi skill 的 i2c_decode.py（已含逐点步长、二分定位、
    最紧值判定、不凑数等修复）。若未安装该 skill，会给出明确报错。
  - numpy（取数与波形处理）。

用法：
  python i2c_workflow.py --ip <SCOPE_IP> --tb 20e-6 --delay 395.5e-6 \
        --ch-scl 1 --ch-sda 2 --out wave.json
  # 多窗口拼接（1ns 分辨率，覆盖整段内存）：
  python i2c_workflow.py --ip <SCOPE_IP> --stitch \
        --tb 20e-6 --delays -459e-6,0e-6,459e-6 --out stitched.json
取数后会对 wave.json 调 i2c_decode.analyze，打印 12 参数 + 判定，并写 <out>_decode.json。
"""
import sys, os, socket, time, argparse, json

import numpy as np

# 复用 keysight-scope-scpi 的实战解码器
_SCOPE_SKILL = os.path.normpath(os.path.join(
    os.path.dirname(__file__), "..", "..", "keysight-scope-scpi", "scripts"))
if os.path.isdir(_SCOPE_SKILL) and _SCOPE_SKILL not in sys.path:
    sys.path.insert(0, _SCOPE_SKILL)
try:
    from i2c_decode import analyze as decode_analyze, fmt as decode_fmt  # noqa
except ImportError:
    sys.stderr.write(
        "缺少依赖：请先安装 keysight-scope-scpi skill（含 scripts/i2c_decode.py）。\n"
        "该解码器含逐点步长、二分定位、最紧值判定等修复，本 skill 直接复用。\n")
    decode_analyze = None


# ---------------- 精简 SCPI 客户端（独立可运行） ----------------
class Scope:
    def __init__(self, ip, port=5025, timeout=40.0):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.s.settimeout(timeout)
        self.s.connect((ip, port))
        idn = self.q("*IDN?").strip()
        if not idn:
            raise RuntimeError("示波器无响应")
        print("连接成功:", idn)

    def w(self, cmd):
        self.s.sendall((cmd + "\n").encode("ascii"))

    def q(self, cmd):
        self.w(cmd)
        buf = b""
        while True:
            try:
                chunk = self.s.recv(4096)
            except socket.timeout:
                break
            if not chunk:
                break
            buf += chunk
            if b"\n" in buf or len(buf) > 2_000_000:
                break
        return buf.decode("ascii", "ignore").strip()

    def _read_block(self):
        hdr = b""
        while len(hdr) < 2:
            hdr += self.s.recv(1)
        if hdr[0:1] != b"#":
            return hdr
        n = int(hdr[1:2])
        size = int(self.s.recv(n))
        data = b""
        while len(data) < size:
            data += self.s.recv(min(65536, size - len(data)))
        return data

    def screenshot(self, path):
        self.w(":HARDcopy:INKSaver OFF")
        self.w(":SCReen:DUMP? PNG")
        data = self._read_block()
        if isinstance(data, bytes) and data[:4] == b"\x89PNG":
            with open(path, "wb") as f:
                f.write(data)
            print("截图 ->", path)
        else:
            print("截图失败（非 PNG）")

    def close(self):
        try:
            self.s.close()
        except Exception:
            pass


# ---------------- 1ns 高分辨率取数 ----------------
def capture_channel(scope, chan, tb, delay, points=100000):
    """取单通道波形，返回 (t_ndarray, v_ndarray)。"""
    scope.w(":WAVeform:SOURCE CHANnel%d" % chan)
    scope.w(":WAVeform:POINts %d" % points)      # 关键：显设，不用 ?MAX
    scope.w(":WAVeform:FORMat ASCii")
    scope.w(":TIMebase:SCALe %E" % tb)
    scope.w(":TIMebase:DELay %E" % delay)
    time.sleep(0.3)
    pts = int(scope.q(":WAVeform:POINts?"))        # 关键：无数据判据
    if pts <= 0:
        raise RuntimeError("窗口无数据（DELay 推出内存）：pts=%d，请调整 delay" % pts)
    scope.w(":WAVeform:POINts %d" % pts)
    raw = scope.q(":WAVeform:DATA?")
    yvals = np.array([float(x) for x in raw.replace("#", "").split(",") if x.strip() != ""],
                     dtype=float)
    xorig = float(scope.q(":WAVeform:XORigin?"))
    xinc = float(scope.q(":WAVeform:XINCrement?"))
    t = xorig + xinc * np.arange(len(yvals))
    return t, yvals


def stitch(scope, chan, tb, delays, points=100000):
    """多窗口拼接，按时间轴排序去重。"""
    segs = [capture_channel(scope, chan, tb, d, points) for d in delays]
    t_all = np.concatenate([s[0] for s in segs])
    v_all = np.concatenate([s[1] for s in segs])
    order = np.argsort(t_all)
    t_all, v_all = t_all[order], v_all[order]
    keep = np.concatenate(([True], np.diff(t_all) > 0))
    return t_all[keep], v_all[keep]


def save_wave_json(t_scl, v_scl, t_sda, v_sda, path):
    """存成 i2c_decode.load_wave 支持的 {T, Y1, Y2} 格式。"""
    out = {
        "T": t_scl.tolist(),
        "Y1": v_scl.tolist(),
        "Y2": v_sda.tolist(),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f)
    print("波形已存 ->", path)


# ---------------- 主流程 ----------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="<SCOPE_IP>")
    ap.add_argument("--tb", type=float, default=20e-6)
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--delays", default=None, help="逗号分隔多窗口 DELay，用于拼接")
    ap.add_argument("--ch-scl", type=int, default=1)
    ap.add_argument("--ch-sda", type=int, default=2)
    ap.add_argument("--out", default="i2c_capture.json")
    ap.add_argument("--spec", choices=["std", "fm"], default="std")
    args = ap.parse_args()

    if decode_analyze is None:
        sys.exit(2)

    scope = Scope(args.ip)
    try:
        if args.delays:
            ds = [float(x) for x in args.delays.split(",")]
            t_scl, v_scl = stitch(scope, args.ch_scl, args.tb, ds)
            t_sda, v_sda = stitch(scope, args.ch_sda, args.tb, ds)
        else:
            t_scl, v_scl = capture_channel(scope, args.ch_scl, args.tb, args.delay)
            t_sda, v_sda = capture_channel(scope, args.ch_sda, args.tb, args.delay)
    finally:
        scope.close()

    save_wave_json(t_scl, v_scl, t_sda, v_sda, args.out)

    # 委托实战解码器
    dec = decode_analyze(args.out, args.spec)
    print("\n" + decode_fmt(dec))
    dec_out = os.path.splitext(args.out)[0] + "_decode.json"
    with open(dec_out, "w", encoding="utf-8") as f:
        json.dump(dec, f, ensure_ascii=False, indent=2)
    print("\n解码结果已存 ->", dec_out)


if __name__ == "__main__":
    main()
