# -*- coding: utf-8 -*-
"""沙箱验证：无真机时端到端跑通 I2C 在线测试链路。

启动本地 mock 示波器（mock_scope.MockScopeServer），连它跑
connect / check / setup / capture / decode / cursors / shots / verify，
断言代码链路不崩、波形能解出合理时序，并生成取证截图。

用法：  python sandbox_test.py
"""
import argparse
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from mock_scope import MockScopeServer          # noqa: E402
import i2c_full_test as ft                       # noqa: E402


def make_args(**kw):
    base = dict(ip="127.0.0.1", port=5051, addr="0x5B", ch_scl=1, ch_sda=2,
                tb=20e-6, delay=257e-6, points=700000, spec="std", freq=None,
                out="i2c_sandbox_out", shots_dir=None, xlsx=None, sheet="时序测试",
                verbose=False, trigger_cond="START", trigger_addr=None,
                addr_list=None, base_out="i2c_out_bulk", vdd=3.3, rp=4700.0)
    base.update(kw)
    return argparse.Namespace(**base)


def main():
    srv = MockScopeServer(port=5051)
    srv.start()
    time.sleep(0.6)  # 等 accept 就绪

    out = "i2c_sandbox_out"
    os.makedirs(out, exist_ok=True)

    fails = [0]

    def check(name, fn):
        try:
            fn()
            print("  [OK]   %s" % name)
        except Exception as e:
            fails[0] += 1
            print("  [FAIL] %s: %s" % (name, e))

    print("=== 沙箱验证：mock 示波器 @127.0.0.1:5051 ===")
    check("connect（IDN/探头/状态）", lambda: ft.do_connect(make_args()))
    check("check（CH1/CH2 峰峰值）", lambda: ft.do_check(make_args()))
    check("setup（AutoScale + I2C 触发）", lambda: ft.do_setup(make_args(trigger_addr=91)))
    check("capture（取数→wave.json）", lambda: ft.do_capture(make_args(out=out)))
    check("decode（13 参数+判定）", lambda: ft.do_decode(make_args(out=out)))
    check("cursors（5 组游标边沿对）", lambda: ft.do_cursors(make_args(out=out)))
    check("shots（取证截图×N）", lambda: ft.do_shots(make_args(out=out)))
    check("verify（OCR 验收）", lambda: ft.do_verify(make_args(out=out)))

    srv.stop()

    # ---- 合理性断言 ----
    dec = json.load(open(os.path.join(out, "decode.json"), encoding="utf-8"))
    clk = dec["clock"]
    assert clk["f_khz"] > 50, "fSCL 异常偏低"
    assert dec["n_start"] >= 1 and dec["n_stop"] >= 1, "未解出 START/STOP"
    T = dec["timings"]
    assert T["tHD_STA"]["value_us"] is not None, "tHD_STA 未算出"
    assert T["tSU_STO"]["value_us"] is not None, "tSU_STO 未算出"
    # tSU_STA：本次 mock 含『写→Sr→读』复合读事务，Sr 真实验证 → tSU_STA 应能算出且 PASS
    assert T["tSU_STA"]["value_us"] is not None, "tSU_STA 未算出（Sr 事务未生效）"
    assert dec["has_repeated_start"] is True, "未检测到重复 START(Sr)"
    print("  [OK]   重复 START(Sr) 检测 = True（复合读事务生效）")
    # 时钟拉伸：事务1 地址 ACK 后从机延长 SCL 低 30µs → 应检出 ≥1 段
    assert dec["clock_stretch"]["n"] >= 1, "未检出时钟拉伸"
    print("  [OK]   时钟拉伸检出 %d 段（最长 %.2f µs，正常 tLOW≈%.2f µs）"
          % (dec["clock_stretch"]["n"], dec["clock_stretch"]["max_us"],
             dec["clock_stretch"]["normal_tlow_us"]))
    # 毛刺：空闲段注入的 20ns SDA 尖峰 → 应计数 ≥1
    assert dec["spikes"]["n"] >= 1, "未检出 SDA 毛刺"
    print("  [OK]   毛刺(tSPIKE)检出 %d 处（<50ns 反向边沿对）" % dec["spikes"]["n"])
    # DC 电气：VDD=3.3V 时 VIH/VIL/VOL 应全 PASS
    dc = dec["dc"]
    for ch, c in dc["checks"].items():
        assert c["vih_ok"] and c["vil_ok"] and c["vol_ok"], "%s DC 判定 FAIL" % ch
    assert dc["cb_pf"] is not None, "Cb 未反推（缺 --rp）"
    assert dc["cb_pf"] <= 400.0, "Cb 反推超 400pF 规范"
    print("  [OK]   DC 电气：VDD=%.2fV VIH/VIL/VOL 全 PASS；Cb≈%.2f pF（≤400pF）"
          % (dc["vdd_v"], dc["cb_pf"]))
    # tBUF：mock 两笔事务间隔 ≈26µs
    if T["tBUF"]["value_us"] is not None:
        assert abs(T["tBUF"]["value_us"] - 25.8) < 1.5, "tBUF 应为≈26µs"
        print("  [OK]   tBUF ≈ %.3f µs（双事务间隔符合预期）" % T["tBUF"]["value_us"])
    else:
        print("  [WARN] tBUF 为 NA（记录内仅单事务）")

    # 截图落盘核对
    shots_dir = os.path.join(out, "shots")
    pngs = sorted(f for f in os.listdir(shots_dir) if f.endswith(".png")) \
        if os.path.isdir(shots_dir) else []
    print("\n=== 结果 ===")
    print("fSCL      = %.2f kHz" % clk["f_khz"])
    print("START/STOP= %d / %d" % (dec["n_start"], dec["n_stop"]))
    print("tSU_STA   = %.4f µs（含 Sr 复合读事务，真实验证）" % T["tSU_STA"]["value_us"])
    print("tHD_STA   = %.4f µs" % T["tHD_STA"]["value_us"])
    print("tSU_STO   = %.4f µs" % T["tSU_STO"]["value_us"])
    print("tSU_DAT   = %.4f µs" % T["tSU_DAT"]["value_us"])
    print("tHD_DAT   = %.4f µs" % T["tHD_DAT"]["value_us"])
    print("时钟拉伸   = %d 段（最长 %.2f µs）" % (dec["clock_stretch"]["n"],
                                                dec["clock_stretch"]["max_us"]))
    print("毛刺(tSPIKE)= %d 处" % dec["spikes"]["n"])
    print("DC Cb     ≈ %.2f pF（VDD=%.2fV）" % (dc["cb_pf"], dc["vdd_v"]))
    print("取证截图   = %d 张 -> %s" % (len(pngs), shots_dir))

    if fails[0] == 0:
        print("\n✅ 沙箱验证全部通过：在线链路（含 mock 取数/截图）代码路径无崩溃，"
              "波形解出合理 I2C 时序。")
        return 0
    print("\n⚠️ 沙箱验证 %d 项失败" % fails[0])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
