# -*- coding: utf-8 -*-
"""纯函数自测：换算、边沿插值、START 判据、规格判定、测量结果解析。

跑法： python selftest.py
报"不合格/超规格"之前先跑这个，确认不是自己算错了再下结论。
实测经验：仪器面板 1.51 us 与本地 4 ns 重算 1.78 us 属同量级（不是算错），
而 42.03 kHz 那种差一倍以上的数才是窗口取错。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scope import parse_results, INVALID          # noqa: E402
from i2c_decode import (pct, edges, stable_high, levels,      # noqa: E402
                        nearest_before, nearest_after, _idx_of)


def t_of(i, xinc, xorg, xref):
    return (i - xref) * xinc + xorg


def v_of(raw, yinc, yorg, yref):
    return yinc * (raw - yref) + yorg


def cross(v_a, v_b, t_a, t_b, thr):
    if v_b == v_a:
        return t_a
    return t_a + (thr - v_a) / (v_b - v_a) * (t_b - t_a)


def is_start(scl_before_high, sda_kind, guard_s=200e-9):
    """I2C START 判据：SDA 下降 + 跳变前 SCL 已稳定为高 >= guard。
    guard 的作用：只判"跳变前 SCL 是高"会把 SCL 刚落后 168 ns 的普通数据位
    变化误判成重复 START（实测真实反例）。"""
    return scl_before_high >= guard_s and sda_kind == "F"


def verdict(value, spec_min=None, spec_max=None):
    if value is None:
        return "NA"
    if spec_min is not None and value < spec_min:
        return "FAIL"
    if spec_max is not None and value > spec_max:
        return "FAIL"
    return "PASS"


class TestTimeAxis(unittest.TestCase):
    def test_time_axis_matches_instrument(self):
        # 真值：125000 点、4 ns/点、原点 -65.48365 us（实测记录之一）
        xinc, xorg, xref = 4e-9, -65.48365e-6, 0.0
        self.assertAlmostEqual(t_of(0, xinc, xorg, xref), -65.48365e-6, places=12)
        self.assertAlmostEqual(t_of(125000, xinc, xorg, xref), 434.51635e-6, places=12)
        # 记录长度必须等于 时基档 x 10 格
        self.assertAlmostEqual(125000 * xinc, 500e-6, places=12)

    def test_voltage_scale(self):
        yinc, yorg, yref = 130.7531e-6, 2.225, 32768
        self.assertAlmostEqual(v_of(32768, yinc, yorg, yref), 2.225, places=9)
        self.assertAlmostEqual(v_of(32768 + 8690, yinc, yorg, yref),
                               2.225 + 8690 * yinc, places=9)


class TestEdges(unittest.TestCase):
    def setUp(self):
        # 构造方波：0->3 V 上升 @1us，3->0 下降 @3us，步长 4 ns
        self.dt = 4e-9
        self.t = [i * self.dt for i in range(2500)]
        self.y = []
        for tt in self.t:
            self.y.append(3.0 if 1e-6 <= tt < 3e-6 else 0.0)

    def test_edge_interpolation(self):
        self.assertAlmostEqual(cross(0.0, 3.0, 0.0, 1e-6, 1.5), 0.5e-6, places=12)
        self.assertAlmostEqual(cross(0.0, 3.0, 0.0, 1e-6, 2.31), 0.77e-6, places=12)

    def test_edges_found(self):
        E = edges(self.t, self.y, 1.5, 0.36)
        kinds = [k for k, _, _ in E]
        self.assertEqual(kinds, ["R", "F"])
        self.assertAlmostEqual(E[0][1], 1e-6, places=6)
        self.assertAlmostEqual(E[1][1], 3e-6, places=6)

    def test_levels_and_stable_high(self):
        L, H = pct(self.y, .02), pct(self.y, .98)
        self.assertAlmostEqual(L, 0.0, places=9)
        self.assertAlmostEqual(H, 3.0, places=9)
        # SCL 高电平期间(2us)稳定高；低电平期间(0.5us)不是
        self.assertTrue(stable_high(self.t, self.y, 2e-6, 1.5, 0.3e-6, self.dt))
        self.assertFalse(stable_high(self.t, self.y, 0.5e-6, 1.5, 0.3e-6, self.dt))


class TestEdgeSelection(unittest.TestCase):
    """回归测试：取值必须取"最接近的边沿"，不能取"第一个满足条件的"。
    踩过的坑——用 next() 取第一个，tSU;STO 被算成 385.79us（整帧跨度）、
    tHD;DAT 被算成 20.96us；改成最近边沿后回到 5.574us / 314.3ns。"""

    def test_nearest_before_picks_closest_not_earliest(self):
        E1 = [("R", -11.1e-6, 0), ("F", 5.0e-6, 1),
              ("R", 10.5e-6, 2), ("R", 21.0e-6, 3)]
        # STOP 在 26.6us：正确的 tSU;STO 应从 21.0us 那个上升沿量起
        self.assertAlmostEqual(nearest_before(26.6e-6, E1), 21.0e-6, places=12)
        self.assertAlmostEqual(nearest_before(-11.0e-6, E1), -11.1e-6, places=12)
        self.assertIsNone(nearest_before(-20e-6, E1))

    def test_nearest_after_picks_closest(self):
        E1 = [("R", 10.5e-6, 0), ("F", 12.0e-6, 1), ("F", 22.0e-6, 2)]
        self.assertAlmostEqual(nearest_after(11e-6, E1, "F"), 12.0e-6, places=12)
        self.assertAlmostEqual(nearest_after(11e-6, E1), 12.0e-6, places=12)
        # 11us 之后没有上升沿了 -> None（别硬凑一个数）
        self.assertIsNone(nearest_after(11e-6, E1, "R"))
        self.assertIsNone(nearest_after(30e-6, E1))

    def test_min_is_the_governing_value(self):
        # 数据保持 27 个候选里最小的是 314.3ns —— 判定要用这个，不是平均值
        vals = sorted([40.95e-6, 0.3143e-6, 5.1e-6])
        self.assertAlmostEqual(vals[0], 0.3143e-6, places=12)
        self.assertEqual(verdict(vals[0], spec_min=0.0), "PASS")


class TestStartGuard(unittest.TestCase):
    def test_start_guard_rejects_late_fall(self):
        # 实测真实反例：SDA 下降时 SCL 已落了 168 ns -> 不是重复 START
        self.assertFalse(is_start(scl_before_high=0.0, sda_kind="F"))
        # 普通数据位变化（SCL 已低）-> 不是 START
        self.assertFalse(is_start(scl_before_high=-1e-6, sda_kind="F"))
        # 真正的 START：SCL 高稳 3 us 后 SDA 下降
        self.assertTrue(is_start(scl_before_high=3e-6, sda_kind="F"))
        # SDA 上升且 SCL 高 -> 是 STOP，不是 START
        self.assertFalse(is_start(scl_before_high=3e-6, sda_kind="R"))
        # 边界：恰好等于 guard
        self.assertTrue(is_start(scl_before_high=200e-9, sda_kind="F"))
        self.assertFalse(is_start(scl_before_high=199e-9, sda_kind="F"))


class TestVerdict(unittest.TestCase):
    def test_verdict_bounds(self):
        self.assertEqual(verdict(1500, spec_max=1000), "FAIL")   # Tr 1.5us > 1us
        self.assertEqual(verdict(148, spec_max=300), "PASS")
        self.assertEqual(verdict(4.28, spec_min=4.0), "PASS")
        self.assertEqual(verdict(4.225, spec_min=4.7), "FAIL")
        self.assertEqual(verdict(None, spec_min=4.7), "NA")      # 无事件不得编数
        self.assertEqual(verdict(0.0, spec_min=0.0), "PASS")


class TestParseResults(unittest.TestCase):
    def test_invalid_readings_become_none(self):
        # 面板"未完成"的项在 RESults 里是 9.9E+37
        s = "FREQ,9.9E+37,0,0,0,0,0,PWID,4.28E-06,0,0,0,0,0"
        r = parse_results(s)
        self.assertIsNone(r["FREQ"])
        self.assertAlmostEqual(r["PWID"], 4.28e-6)

    def test_timeout_string(self):
        self.assertEqual(parse_results("<timeout>"), {})

    def test_real_line(self):
        s = ("FREQ(CH1),4.20300E+04,0.0,0.0,0.0,0.0,0.0,"
             "PWID(CH1),4.28000E-06,0.0,0.0,0.0,0.0,0.0")
        r = parse_results(s)
        self.assertEqual(len(r), 2)
        self.assertAlmostEqual(r["PWID(CH1)"], 4.28e-6)


class TestExcelHelpers(unittest.TestCase):
    def test_value_type_inference(self):
        try:
            from fill_excel import infer_type
        except Exception as e:                       # 依赖缺失时跳过
            self.skipTest("fill_excel 不可导入: %s" % e)
        self.assertEqual(infer_type(4.28), "NUMBER")
        self.assertEqual(infer_type("99.52 kHz"), "STRING")
        self.assertEqual(infer_type("=SUM(A1:A2)"), "FORMULA")
        self.assertEqual(infer_type(True), "BOOL")


class TestMemSegments(unittest.TestCase):
    """采集内存扫描：活动区段的合并逻辑（决定"哪一段有 I2C 活动"的结论）。"""

    def setUp(self):
        try:
            from mem_scan import segments
        except Exception as e:
            self.skipTest("mem_scan 不可导入: %s" % e)
        self.segments = segments

    @staticmethod
    def _w(c, act, key="d_us"):
        return {key: c, "active": act}

    def test_adjacent_windows_merge_into_one(self):
        ws = [self._w(-50, True), self._w(50, True)]
        segs = self.segments(ws, 100)
        self.assertEqual(len(segs), 1)
        self.assertAlmostEqual(segs[0][0], -100.0)
        self.assertAlmostEqual(segs[0][1], 100.0)

    def test_gap_splits_into_two(self):
        ws = [self._w(-300, True), self._w(-200, False),
              self._w(0, True), self._w(100, True)]
        segs = self.segments(ws, 100)
        self.assertEqual(len(segs), 2)
        self.assertAlmostEqual(segs[0][0], -350.0)
        self.assertAlmostEqual(segs[1][1], 150.0)

    def test_all_idle_returns_nothing(self):
        ws = [self._w(x, False) for x in range(0, 500, 100)]
        self.assertEqual(self.segments(ws, 100), [])

    def test_legacy_set_us_key_compatible(self):
        """早期脚本产物用 set_us 字段，也要能算。"""
        ws = [self._w(0, True, key="set_us"), self._w(100, True, key="set_us")]
        self.assertEqual(len(self.segments(ws, 100)), 1)

    def test_real_scan_77_windows_gives_one_segment(self):
        """2026-09-23 实测数据复算：8 ms 全扫应只有 1 段活动区，约 -50 … +450 us。"""
        import json
        p = r"<WORKDIR>\mem_scan1.json"
        if not os.path.exists(p):
            self.skipTest("无实测数据文件 %s" % p)
        d = json.load(open(p, encoding="utf-8"))
        ws = d["windows"]
        self.assertEqual(len(ws), 77)
        for w in ws:
            # ★ 判据用幅度（min < 1 V），不用边沿数：空闲段有几千个假边沿
            w["active"] = (w["ch1_min"] < 1.0) or (w["ch2_min"] < 1.0)
        segs = self.segments(ws, 100)
        self.assertEqual(len(segs), 1)
        self.assertAlmostEqual(segs[0][0], -50.0, places=6)
        self.assertAlmostEqual(segs[0][1], 450.0, places=6)


class TestNonUniformTimebase(unittest.TestCase):
    """★ 回归：拼接数据的时间步长可能**不统一**。

    实测反例（2026-09-23）：cmd_dump 拼接出的 wave_merged.json，
    第一窗口 6 ns（9896 点）、其余窗口 12 ns（74988 点）。
    当时 `edges()` 用 `dt = t[1] - t[0]`（= 6 ns）当全局步长做插值，
    `stable_high()` 用 `(tt - t[0]) / dt` 定位索引——
    索引偏大一倍，SCL 高电平判定整个错位，
    **把 SCL 低电平期间的普通数据位跳变全部误判成 START**，
    凭空造出 15 个 START / 13 个 STOP，并算出 tSU;STA = 0.1666 us（假的 FAIL）。
    修复后同一份数据给出 2 个 START，tSU;STA = 5.0990 us PASS。
    """

    @staticmethod
    def _nonuniform(seg1_n=500, seg2_n=500, dt1=6e-9, dt2=12e-9):
        t, acc = [], -seg1_n * dt1
        for _ in range(seg1_n):
            t.append(acc); acc += dt1
        for _ in range(seg2_n):
            t.append(acc); acc += dt2
        return t

    def test_idx_of_handles_nonuniform_steps(self):
        """索引定位必须按时间轴二分，不能按首个步长做除法。"""
        t = self._nonuniform()
        n = len(t)
        # 末点与首点：按时间定位
        self.assertEqual(_idx_of(t, t[0]), 0)
        self.assertEqual(_idx_of(t, t[-1]), n - 1)
        # 抽查第 800 点附近的定位
        self.assertEqual(_idx_of(t, t[800]), 800)
        self.assertEqual(_idx_of(t, t[800] + 1e-9), 800)
        self.assertEqual(_idx_of(t, t[800] - 1e-9), 799)
        # 越界保护
        self.assertEqual(_idx_of(t, t[-1] + 1e-3), n - 1)
        self.assertEqual(_idx_of(t, t[0] - 1e-3), 0)

    def test_stable_high_locates_by_time_not_division(self):
        """SCL 在索引 200..400 为高；用时间查询必须得到正确结论。"""
        t = self._nonuniform()
        y1 = [0.2] * len(t)
        for i in range(200, 400):
            y1[i] = 3.2
        # 索引 300 处（段内高电平中点）
        w = 0.1e-6                      # 100 ns 检查窗（必须 > 0，否则区间为空）
        self.assertTrue(stable_high(t, y1, t[300], 1.7, w))
        # 索引 100 处（低电平区）
        self.assertFalse(stable_high(t, y1, t[100], 1.7, w))
        # 索引 800 处（第二段，低电平）
        self.assertFalse(stable_high(t, y1, t[800], 1.7, w))

    def test_no_false_start_on_nonuniform_timebase(self):
        """SDA 在 SCL 低电平期间下降 -> 绝不能判成 START（这是真实翻车点）。"""
        t = self._nonuniform()
        y1 = [0.2] * len(t)
        for i in range(200, 400):
            y1[i] = 3.2                 # 唯一一段 SCL 高
        y2 = [3.2] * len(t)
        for i in range(500, len(t)):    # 索引 500 处下降，此时 SCL 已低
            y2[i] = 0.2
        E2 = edges(t, y2, 1.7, 0.2)
        falls = [x for k, x, i in E2 if k == "F"]
        self.assertEqual(len(falls), 1, "应只识别出 1 个 SDA 下降沿")
        starts = [x for x in falls if stable_high(t, y1, x, 1.7, 0.2e-6)]
        self.assertEqual(starts, [],
                         "非均匀步长下把 SCL 低电平期的 SDA 下降误判成了 START")

    def test_edge_interpolation_uses_local_step(self):
        """边沿穿越时刻要用该点的实际步长插值，否则时间会偏一半。"""
        t = self._nonuniform()
        y = [0.0] * len(t)
        for i in range(701, len(t)):    # 第二段（12 ns 步长）内的单个上升沿
            y[i] = 3.0
        E = edges(t, y, 1.5, 0.0)
        self.assertEqual(len(E), 1)
        k, tt, i = E[0]
        self.assertEqual(k, "R")
        expect = t[700] + 0.5 * (t[701] - t[700])
        self.assertAlmostEqual(tt, expect, places=15)


def _load_real_merged():
    p = r"<WORKDIR>\now_dump\wave_merged.json"
    if not os.path.exists(p):
        return None
    import json
    return json.load(open(p, encoding="utf-8"))["merged"]


class TestRealMergedRegression(unittest.TestCase):
    """用 2026-09-23 实测拼接数据（步长 6ns+12ns 混合）做端到端回归。"""

    def setUp(self):
        self.m = _load_real_merged()
        if self.m is None:
            self.skipTest("无实测拼接数据 D:\\_scope_tmp\\now_dump\\wave_merged.json")

    def test_real_data_has_two_starts_zero_stops(self):
        t, y1, y2 = self.m["t"], self.m["ch1"], self.m["ch2"]
        L1, H1, L2, H2 = levels(y1, y2)
        E1 = edges(t, y1, (L1 + H1) / 2, .12 * (H1 - L1))
        E2 = edges(t, y2, (L2 + H2) / 2, .12 * (H2 - L2))
        T1 = (L1 + H1) / 2
        starts, stops = [], []
        for k, tt, i in E2:
            if stable_high(t, y1, tt, T1, 0.2e-6):
                (starts if k == "F" else stops).append(tt)
        # 修复前这里会是 15 / 13（全是假的）
        self.assertEqual(len(starts), 2, "START 数应为 2（修复前误报 15）")
        self.assertEqual(len(stops), 0, "STOP 数应为 0（修复前误报 13）")
        self.assertLess(starts[0] * 1e6, -80.0)      # 起始条件在空闲段末尾
        self.assertGreater(starts[1] * 1e6, 130.0)   # 重复 START
        # tSU;STA = 第二个 START 之前最近的 SCL 上升沿到该 START
        prev = nearest_before(starts[1], E1)
        self.assertIsNotNone(prev)
        self.assertAlmostEqual((starts[1] - prev) * 1e6, 5.1, delta=0.3)


if __name__ == "__main__":
    print("INVALID 常量 = %g（:MEASure:RESults? 里表示『未完成』）" % INVALID)
    print("=" * 60)
    unittest.main(verbosity=2)
