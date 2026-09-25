# -*- coding: utf-8 -*-
"""Mock Keysight InfiniiVision 示波器（TCP，可配端口）。

用途：无真机时做「沙箱验证」——在本地起一个最小 SCPI 子集服务器，
返回合成 I2C 波形与截图 PNG，让 scope.py / i2c_full_test.py 的在线子命令
（connect / check / setup / capture / shots / verify）能端到端跑通，
验证代码链路不崩、产出合理。

启动：  python mock_scope.py [port]        # 默认 5051
测试：  python sandbox_test.py            # 起 mock 跑全套在线链路

★ 这是「仿真」不是「真机」，只验证代码路径与数据解析；数值合规性以真机为准。
"""
import socket
import sys
import threading
import time

import numpy as np

IDLE_HI = 3.3
IDLE_LO = 0.0
YINC = 0.001   # 1 mV/码（y = yinc * raw，mock 用 yorg=0/yref=0）
XINC = 1e-9    # 1 ns/点
NPTS = 700000  # 700 µs 满记录（容纳 拉伸事务 + Sr 复合读事务 + 间隔，1ns/点）
RAMP = 50e-9   # 50 ns 过渡（远小于 100 kHz 周期，Tr/Tf 算出来远小于规范上限）

# 一张最小合法 PNG（截图验证不解析内容，只验证取数+落盘链路）
_PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01'
        b'\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01'
        b'\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82')


def _make_signal(n, xinc, idle, edges, ramp=RAMP):
    """由 (t, target) 转折列表生成电平序列，转折处用 ramp 斜坡过渡。
    ★ 每个边沿过渡后必须把电平保持到下一个边沿（否则除斜坡瞬间外全是 idle）。"""
    y = np.full(n, idle, dtype=float)
    ramp_pts = max(2, int(ramp / xinc))
    cur = idle
    for (te, target) in sorted(edges, key=lambda e: e[0]):
        idx = int(round(te / xinc))
        r0 = max(0, idx - ramp_pts // 2)
        r1 = min(n, idx + ramp_pts // 2)
        if r1 > r0:
            seq = np.arange(r0, r1)
            frac = (seq - r0) / max(1, (r1 - r0))
            y[r0:r1] = cur * (1 - frac) + target * frac
        y[r1:] = target          # ★ 保持到下一个边沿（被后续边沿的斜坡覆盖）
        cur = target
    return y


def _gen_transaction(t0, addr_byte, data_byte, scl_e, sda_e, stretch_us=0.0):
    """在 t0 处生成一笔 I2C 写事务（START + 地址字节 + ACK + 数据字节 + ACK + STOP）。
    返回事务结束时刻。SCL=100kHz（周期 10µs，tHIGH 5.3µs / tLOW 4.7µs）。
    stretch_us>0 时，在地址字节 ACK 之后把下一个 SCL 低段延长 stretch_us——
    模拟从机时钟拉伸（clock stretching），供拉伸检测测试。"""
    sda_e.append((t0, IDLE_LO))                         # START：SCL 高时 SDA↓
    bitstream = [(addr_byte >> (7 - i)) & 1 for i in range(8)] + [0]   # 地址 + ACK
    bitstream += [(data_byte >> (7 - i)) & 1 for i in range(8)] + [0]  # 数据 + ACK
    clk_t = t0
    for idx, bit in enumerate(bitstream):
        scl_e.append((clk_t, IDLE_HI))                  # SCL↑（高段开始）
        low = 4.7e-6 + (stretch_us if idx == 8 else 0.0)  # 地址 ACK 后拉伸
        scl_e.append((clk_t + 5.3e-6, IDLE_LO))         # SCL↓（低段开始）
        # SDA 在 SCL 低中段变化（tSU_DAT / tHD_DAT 取此几何，均远 > 规范下限）
        sda_e.append((clk_t + 5.3e-6 + 2.4e-6, IDLE_HI if bit else IDLE_LO))
        clk_t += 10e-6 + (stretch_us if idx == 8 else 0.0)
    scl_e.append((clk_t, IDLE_HI))                      # STOP 前 SCL 回到高
    # STOP：SCL 高电平期间 SDA↑；tSU;STO = SDA↑ 距上个 SCL 上升 ≈ 4.2µs
    #（> std 规范 4.0µs 下限，PASS；原 2.4µs 会误判 FAIL）
    sda_e.append((clk_t + 4.2e-6, IDLE_HI))
    return clk_t + 4.2e-6


def _gen_sr_transaction(t0, addr_byte, reg_byte, data_byte, scl_e, sda_e):
    """复合读事务：START→addr(写)→reg→Sr→addr(读)→data→NACK→STOP。
    产生重复 START(Sr) 让 tSU;STA 成真值、has_repeated_start=True。SCL=100kHz。"""
    sda_e.append((t0, IDLE_LO))                         # START：SCL 高时 SDA↓
    clk_t = t0
    # ---- 写阶段：addr(写, 8+ACK) + reg(8+ACK) ----
    for val, ack in ((addr_byte, 0), (reg_byte, 0)):
        for i in range(8):
            bit = (val >> (7 - i)) & 1
            scl_e.append((clk_t, IDLE_HI)); scl_e.append((clk_t + 5.3e-6, IDLE_LO))
            sda_e.append((clk_t + 7.7e-6, IDLE_HI if bit else IDLE_LO))
            clk_t += 10e-6
        scl_e.append((clk_t, IDLE_HI)); scl_e.append((clk_t + 5.3e-6, IDLE_LO))  # ACK
        sda_e.append((clk_t + 7.7e-6, IDLE_HI if ack else IDLE_LO))
        clk_t += 10e-6
    # ★ Sr 几何（关键：Sr 的 SDA↑/↓ 都不能落在 SCL 高电平期，否则被判 STOP）：
    #   步骤① SCL 低段内先 SDA↑（空闲高，为 Sr 做准备）；
    #   步骤② SCL↑ 进入高段；
    #   步骤③ SCL 高段内 SDA↓ 完成重复 START，tSU;STA = SCL↑→SDA↓ >4.7µs。
    sda_e.append((clk_t - 0.5e-6, IDLE_HI))   # ① SDA↑（SCL 仍低，不会判 STOP）
    scl_e.append((clk_t, IDLE_HI))            # ② SCL↑ 高段开始
    sda_e.append((clk_t + 5.0e-6, IDLE_LO))   # ③ Sr：SDA↓（SCL 高 5.0µs）tSU;STA=5.0µs>4.7
    clk_t += 10e-6                            # 前导拍结束，进入读阶段首拍
    # ---- 读阶段：addr(读, 8+ACK) + data(8+主机NACK) ----
    for val, ack in ((addr_byte | 1, 0), (data_byte, 1)):
        for i in range(8):
            bit = (val >> (7 - i)) & 1
            scl_e.append((clk_t, IDLE_HI)); scl_e.append((clk_t + 5.3e-6, IDLE_LO))
            sda_e.append((clk_t + 7.7e-6, IDLE_HI if bit else IDLE_LO))
            clk_t += 10e-6
        scl_e.append((clk_t, IDLE_HI)); scl_e.append((clk_t + 5.3e-6, IDLE_LO))  # ACK/NACK
        sda_e.append((clk_t + 7.7e-6, IDLE_HI if ack else IDLE_LO))
        clk_t += 10e-6
    scl_e.append((clk_t, IDLE_HI))                      # STOP 前 SCL 回到高
    sda_e.append((clk_t + 4.2e-6, IDLE_HI))             # STOP：SCL 高时 SDA↑
    return clk_t + 4.2e-6


def gen_i2c_waveforms(n=NPTS, xinc=XINC, addr=0xA0, data=0x55):
    """生成 SCL/CH1、SDA/CH2 两路 WORD 二进制波形，覆盖低速测试全要素：
      · 事务1：普通写事务 + 从机时钟拉伸（地址 ACK 后 SCL 低延长 30µs）
      · 事务2：复合读事务（含重复 START Sr），让 tSU;STA 成真值、has_repeated_start=True
      · 空闲段注入 20ns SDA 毛刺（测试 tSPIKE 尖峰计数）
    使 tSU_STA/tHD_STA/tSU_STO/tSU_DAT/tHD_DAT 都可算，tBUF≈30µs 可验证。"""
    scl_e, sda_e = [], []
    # 事务1：普通写 + 从机时钟拉伸（地址 ACK 后低段延长 30µs）
    _gen_transaction(20e-6, addr, data, scl_e, sda_e, stretch_us=30e-6)
    # 事务2：复合读（含 Sr），紧接事务1 STOP(≈234.2µs) 之后 26µs 总线空闲 → tBUF≈26µs。
    # ⚠️ 必须晚于事务1结束，否则边沿交错，第二笔 START 解不出。
    _gen_sr_transaction(260e-6, addr, 0x10, 0xAA, scl_e, sda_e)
    # 毛刺：在事务1 的 SCL 低段（27µs 处 SCL 为低，不会误触发 START/STOP）注入 20ns SDA 尖峰
    sda_e.append((27e-6, IDLE_HI))
    sda_e.append((27.02e-6, IDLE_LO))
    scl = _make_signal(n, xinc, IDLE_HI, scl_e)
    sda = _make_signal(n, xinc, IDLE_HI, sda_e)

    def to_words(v):
        raw = np.clip(np.round((v - 0.0) / YINC), 0, 65535).astype("<u2")
        return raw.tobytes()

    return {"CHANnel1": to_words(scl), "CHANnel2": to_words(sda)}


_WAVES = gen_i2c_waveforms()


def _block(payload):
    """IEEE488.2 定长块：'#' + 位数 + 长度 + 数据 + 2 字节结尾（blk() 会吃掉）。"""
    s = "%d" % len(payload)
    return ("#%d%s" % (len(s), s)).encode() + payload + b"\n\n"


class _Handler(threading.Thread):
    def __init__(self, conn, server):
        super().__init__(daemon=True)
        self.conn = conn
        self.srv = server

    def run(self):
        try:
            buf = b""
            while True:
                try:
                    d = self.conn.recv(65536)
                except Exception:
                    break
                if not d:
                    break
                buf += d
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    self.dispatch(line.decode("ascii", "replace").strip())
        finally:
            try:
                self.conn.close()
            except Exception:
                pass

    def _send(self, s):
        try:
            self.conn.sendall((s + "\n").encode())
        except Exception:
            pass

    def dispatch(self, cmd):
        if not cmd:
            return
        # 注意：Scope 发出的 SCPI 是固定混合大小写（如 :WAVeform:POINts?），
        # 这里一律按「原样命令」匹配，不做 upper()，否则查询会静默不回。
        srv = self.srv
        has_q = "?" in cmd
        # ---- 块数据 ----
        if "DATA?" in cmd or "DUMP?" in cmd:
            if "WAV" in cmd:
                data = _WAVES.get(srv.source, _WAVES["CHANnel1"])
                pts = min(srv.points, len(data) // 2)
                self.conn.sendall(_block(data[:pts * 2]))
            else:
                self.conn.sendall(_block(_PNG))
            return
        # ---- 设置类（静默，但维护少量状态）----
        if ":WAVeform:SOURce" in cmd:
            srv.source = cmd.split()[-1]          # 保留原样，匹配 _WAVES 键
            return
        if ":WAVeform:POINts" in cmd and not has_q:
            try:
                srv.points = max(1, min(NPTS, int(cmd.split()[-1])))
            except Exception:
                pass
            return
        if ":TRIGger:MODE" in cmd and not has_q:
            srv.trigger_mode = cmd.split()[-1].upper()
            return
        if ":MARKer:MODE" in cmd and not has_q:
            srv.marker_mode = cmd.split()[-1].upper()
            return
        # 其余无返回的仪器命令（AUToscale / MEASure:* / RUN / STOP / HARDcopy /
        # MARKer:X* / CHANnel:DISPlay / TIMebase:*），全部静默
        if not has_q:
            return
        # ---- 查询类（带 ?）----
        if cmd == "*IDN?":
            self._send("MockKeysight,DSO-X 6004A,MOCK0001,1.0"); return
        if ":SYSTem:ERRor?" in cmd:
            self._send('0,"No error"'); return
        if ":CHANnel" in cmd and ":PROBe?" in cmd:
            self._send("10"); return
        if ":CHANnel" in cmd and ":DISPlay?" in cmd:
            self._send("1"); return
        if ":CHANnel" in cmd and ":SCALe?" in cmd:
            self._send("0.5"); return
        if ":CHANnel" in cmd and ":OFFSet?" in cmd:
            self._send("0"); return
        if ":CHANnel" in cmd and ":COUPling?" in cmd:
            self._send("DC"); return
        if ":CHANnel" in cmd and ":BWLimit?" in cmd:
            self._send("0"); return
        if ":CHANnel" in cmd and ":IMPedance?" in cmd:
            self._send("ONEMeg"); return
        if ":TIMebase:SCALe?" in cmd:
            self._send("2e-5"); return
        if ":TIMebase:POSition?" in cmd:
            self._send("0"); return
        if ":TIMebase:RANGe?" in cmd:
            self._send("7e-4"); return
        if ":TIMebase:MODE?" in cmd:
            self._send("MAIN"); return
        if ":ACQuire:TYPE?" in cmd:
            self._send("NORMAL"); return
        if ":ACQuire:SRATe?" in cmd:
            self._send("1e9"); return
        if ":ACQuire:POINts?" in cmd:
            self._send(str(srv.points)); return
        if ":TRIGger:MODE?" in cmd:
            self._send(srv.trigger_mode); return
        if ":TRIGger:SWEep?" in cmd:
            self._send("AUTO"); return
        if ":TRIGger:STATus?" in cmd:
            self._send("TRIGGERED"); return
        if ":OPERegister:CONDition?" in cmd:
            self._send("0"); return
        if ":MARKer:MODE?" in cmd:
            self._send(srv.marker_mode); return
        if ":MEASure:SOURce?" in cmd:
            self._send("CHAN1"); return
        if ":WAVeform:POINts?" in cmd:
            self._send(str(srv.points)); return
        if ":WAVeform:XINCrement?" in cmd:
            self._send("%.6e" % XINC); return
        if ":WAVeform:XORigin?" in cmd:
            self._send("0"); return
        if ":WAVeform:XREFerence?" in cmd:
            self._send("0"); return
        if ":WAVeform:YINCrement?" in cmd:
            self._send("%.6e" % YINC); return
        if ":WAVeform:YORigin?" in cmd:
            self._send("0"); return
        if ":WAVeform:YREFerence?" in cmd:
            self._send("0"); return
        # 未识别查询：回一个空值（避免阻塞 q() 的读循环）
        self._send(""); return


class MockScopeServer(threading.Thread):
    def __init__(self, host="127.0.0.1", port=5051):
        super().__init__(daemon=True)
        self.host = host
        self.port = port
        self.source = "CHANnel1"
        self.points = NPTS
        self.trigger_mode = "EDGE"
        self.marker_mode = "OFF"
        self._sock = None
        self._stop = False

    def run(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self.host, self.port))
        self._sock.listen(8)
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except Exception:
                break
            _Handler(conn, self).start()

    def stop(self):
        self._stop = True
        try:
            self._sock.close()
        except Exception:
            pass


if __name__ == "__main__":
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 5051
    srv = MockScopeServer(port=p)
    srv.start()
    print("Mock scope listening on 127.0.0.1:%d  (Ctrl+C 退出)" % p)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        srv.stop()
