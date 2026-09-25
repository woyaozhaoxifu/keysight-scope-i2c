# -*- coding: utf-8 -*-
"""Keysight InfiniiVision 示波器的 SCPI 客户端（TCP 5025）。

这些设计点都是实测踩坑换来的，改动前先读 ../references/scpi-cookbook.md：

* 无返回命令一律走 cmd()。用 q() 去发 :MEASure:CLEar 这类无返回命令会死等到超时。
* 波形必须显式设 :WAVeform:POINts:MODE MAXimum；RAW 模式只给抽取后的少量点
  （实测 500us 记录 RAW 只给 4807 点，MAXimum 给 125000 点，精度差 26 倍）。
* 加测量项的形式是 :MEASure:<类型> CHANnel<n>；:MEASure:ITEM 在本机型返回
  -113 Undefined header，解析出来是 0 项。
* :MEASure:RESults? 批量读比逐项 :MEASure:X? CHANnel1 快几十倍，逐项读会卡死。
* 无效读数在 :MEASure:RESults? 里是 9.9E+37，必须显式判掉，否则会被当成"合格"。
* :WAVeform:DATA? 只返回**屏幕窗口**的数据，屏幕外记录要用多窗口拼接（见 cmd_dump.py）。

用法：
    from scope import Scope, INVALID
    sc = Scope("<SCOPE_IP>")
    print(sc.idn(), sc.probes())
    d = sc.fetch(1)            # dict: t0/xinc/pts/y/t0/t1
    sc.screenshot(r"D:\\tmp\\a.png")
    sc.close()
"""

import socket
import struct
import time

# :MEASure:RESults? 用这个值表示"该项当前无有效读数"（面板上显示为"未完成"）
INVALID = 9.9e37
INVALID_TOL = 1e30  # 绝对值超过这个量级一律当作无效

DEFAULT_CH1 = ["FREQuency", "PWIDth", "NWIDth", "RISetime", "FALLtime",
               "VMAX", "VMIN", "VHIGH", "VLOW"]
DEFAULT_CH2 = ["RISetime", "FALLtime", "VMAX", "VMIN", "VHIGH", "VLOW"]


class ScopeError(RuntimeError):
    pass


class Scope:
    """一个 TCP 5025 的 SCPI 会话。断线会自动重连（每次最多重试 3 次）。"""

    def __init__(self, host, port=5025, timeout=10.0, verbose=False):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.verbose = verbose
        self.s = None
        self.reconnect()

    # ---------- 连接 ----------

    def reconnect(self):
        for _ in range(3):
            try:
                if self.s:
                    self.s.close()
            except Exception:
                pass
            try:
                self.s = socket.create_connection((self.host, self.port),
                                                  timeout=self.timeout)
                self.s.settimeout(self.timeout)
                return
            except Exception:
                time.sleep(1.0)
        raise ScopeError("无法连接 %s:%d" % (self.host, self.port))

    def close(self):
        try:
            if self.s:
                self.s.close()
        except Exception:
            pass
        self.s = None

    # ---------- 收发 ----------

    def cmd(self, c):
        """发送无返回命令。**发这类命令绝不能用 q()**，否则死等到超时。"""
        if self.verbose:
            print("  >", c)
        for _ in range(2):
            try:
                self.s.sendall((c + "\n").encode())
                return
            except Exception:
                self.reconnect()
        raise ScopeError("发送失败: %s" % c)

    def q(self, c, retries=3):
        """查询。超时/断线会重连重试；全失败返回 '<timeout>' 而不是抛异常，
        方便批量脚本继续跑完剩下的项目。"""
        if self.verbose:
            print("  ?", c)
        for _ in range(retries):
            try:
                self.s.sendall((c + "\n").encode())
                buf = b""
                while not buf.endswith(b"\n"):
                    d = self.s.recv(65536)
                    if not d:
                        break
                    buf += d
                    if len(buf) > 4 * 1024 * 1024:
                        break
                return buf.decode("ascii", "replace").strip()
            except Exception:
                try:
                    self.reconnect()
                except Exception:
                    return "<timeout>"
        return "<timeout>"

    def qf(self, c, default=None):
        """查询并转 float。无效读数/超时返回 default。"""
        s = self.q(c)
        try:
            v = float(s)
        except (TypeError, ValueError):
            return default
        if abs(v) > INVALID_TOL:
            return default
        return v

    def blk(self):
        """读 IEEE488.2 定长块：'#' + 1位位数n + n位长度L + L字节数据。"""
        h = b""
        while not h.endswith(b"#"):
            h += self._recv_exact(1)
        n = int(self._recv_exact(1).decode())
        ln = b""
        while len(ln) < n:
            ln += self._recv_exact(n - len(ln))
        length = int(ln.decode())
        data = bytearray()
        while len(data) < length:
            chunk = self.s.recv(min(262144, length - len(data)))
            if not chunk:
                break
            data += chunk
        try:
            self.s.recv(2)  # 结尾换行
        except Exception:
            pass
        return bytes(data)

    def _recv_exact(self, n):
        buf = b""
        while len(buf) < n:
            c = self.s.recv(n - len(buf))
            if not c:
                raise ScopeError("连接在块读取中断开")
            buf += c
        return buf

    # ---------- 只读状态 ----------

    def idn(self):
        return self.q("*IDN?")

    def probes(self):
        """返回 {1:'1', 2:'10', ...} 探头倍率。1:1 探头挂在开漏总线上会把上升沿
        拉长到 us 级，读数异常偏大时先查这里，别急着判 DUT 不合格。"""
        return {ch: self.q(":CHANnel%d:PROBe?" % ch) for ch in (1, 2, 3, 4)}

    def state(self):
        """一次拿全状态快照（只读，不改仪器）。"""
        st = {"idn": self.idn()}
        for ch in (1, 2, 3, 4):
            st["ch%d" % ch] = {
                "disp": self.q(":CHANnel%d:DISPlay?" % ch),
                "scale": self.qf(":CHANnel%d:SCALe?" % ch),
                "offset": self.qf(":CHANnel%d:OFFSet?" % ch),
                "coupling": self.q(":CHANnel%d:COUPling?" % ch),
                "bwlimit": self.q(":CHANnel%d:BWLimit?" % ch),
                "probe": self.q(":CHANnel%d:PROBe?" % ch),
                "impedance": self.q(":CHANnel%d:IMPedance?" % ch),
            }
        st["timebase"] = {
            "scale": self.qf(":TIMebase:SCALe?"),
            "position": self.qf(":TIMebase:POSition?"),
            "range": self.qf(":TIMebase:RANGe?"),
            "mode": self.q(":TIMebase:MODE?"),
        }
        st["acquire"] = {
            "type": self.q(":ACQuire:TYPE?"),
            "srate": self.qf(":ACQuire:SRATe?"),
            "points": self.qf(":ACQuire:POINts?"),
        }
        st["trigger"] = {
            "mode": self.q(":TRIGger:MODE?"),
            "sweep": self.q(":TRIGger:SWEep?"),
            "status": self.q(":TRIGger:STATus?"),
        }
        st["running"] = self.q(":OPERegister:CONDition?")
        st["marker_mode"] = self.q(":MARKer:MODE?")
        st["meas_src"] = self.q(":MEASure:SOURce?")
        return st

    def err(self):
        """抽干错误队列，返回错误列表（不含 'No error'）。"""
        out = []
        for _ in range(20):
            e = self.q(":SYSTem:ERRor?")
            if not e or e.startswith("<") or "No error" in e:
                break
            out.append(e)
        return out

    # ---------- 波形 ----------

    def fetch(self, ch, points=None, timeout=None):
        """取一个通道的原始波形。

        返回 dict: ch/pts/xinc/xorg/xref/yinc/yorg/yref/y(电压列表)/t0/t1
        t0、t1 是该窗口的起止时刻（秒）。注意 t 轴与仪器一致：
            t[i] = (i - xref) * xinc + xorg
        ⚠️ 只覆盖**当前屏幕窗口**，屏幕外记录请用多窗口拼接。
        """
        old = self.s.gettimeout() if timeout is None else self.s.gettimeout()
        if timeout:
            self.s.settimeout(timeout)
        try:
            self.cmd(":WAVeform:SOURce CHANnel%d" % ch)
            self.cmd(":WAVeform:FORMat WORD")
            self.cmd(":WAVeform:BYTeorder LSBFirst")
            self.cmd(":WAVeform:UNSigned 1")
            self.cmd(":WAVeform:POINts:MODE MAXimum")   # ★ 必须 MAXimum，别用 RAW
            if points is None:
                # ★ 不要直接用 POINts? MAX —— 实测本机它只返回 2173（46 ns/点），
                #   而显式设 100000 能拿到 1 GSa/s 原生分辨率（1 ns/点），差 46 倍。
                #   理论上限 = SRATe × RANge（= 屏幕窗口内的原生采样点数）。
                sr = self.qf(":ACQuire:SRATe?", 0.0) or 0.0
                rng = self.qf(":TIMebase:RANge?", 0.0) or 0.0
                want = int(sr * rng) if (sr > 0 and rng > 0) else 0
                mx = int(self.qf(":WAVeform:POINts? MAX", 0) or 0)
                points = max(want, mx) if want else (mx or 125000)
            self.cmd(":WAVeform:POINts %d" % int(points))
            pts = int(self.qf(":WAVeform:POINts?", 0))
            if pts <= 0:
                # 实测：水平位置超出采集内存（例：内存 1 ms 而 DELay 设到 +600 us）时，
                # POINts? 返回 0，错误队列里是 +109,"No Data For Operation"。
                # 此时若照发 :WAVeform:DATA?，仪器**不回定长块**，blk() 会一直等直到超时
                # （实测卡死 5 分钟）。必须在这里就拦住。
                # ⚠️ 同一情况下查 :WAVeform:XORigin? / :WAVeform:XINCrement? 会**挂到超时**
                #   （实测 18 s），所以"有没有数据"只能靠 POINts?，别去查 XORigin。
                raise ScopeError(
                    "该位置无波形数据（:WAVeform:POINts? = 0）——"
                    "水平位置已超出采集内存。先把 :TIMebase:DELay 挪回有数据的区间再取。")
            xinc = self.qf(":WAVeform:XINCrement?")
            xorg = self.qf(":WAVeform:XORigin?")
            xref = self.qf(":WAVeform:XREFerence?", 0.0) or 0.0
            yinc = self.qf(":WAVeform:YINCrement?")
            yorg = self.qf(":WAVeform:YORigin?")
            yref = self.qf(":WAVeform:YREFerence?", 0.0) or 0.0
            self.cmd(":WAVeform:DATA?")
            raw = self.blk()
        finally:
            self.s.settimeout(old)
        n = len(raw) // 2
        vals = struct.unpack("<%dH" % n, raw[:n * 2])
        y = [round(yinc * (v - yref) + yorg, 4) for v in vals]
        return {
            "ch": ch, "pts": pts, "got": n,
            "xinc": xinc, "xorg": xorg, "xref": xref,
            "yinc": yinc, "yorg": yorg, "yref": yref,
            "y": y, "t0": (0 - xref) * xinc + xorg,
            "t1": (n - 1 - xref) * xinc + xorg,
            "span": (n - 1) * xinc,
        }

    @staticmethod
    def time_axis(d):
        """由 fetch 结果生成时间轴（秒）。"""
        n = len(d["y"])
        return [(i - d["xref"]) * d["xinc"] + d["xorg"] for i in range(n)]

    # ---------- 截图 ----------

    def screenshot(self, path, fmt="PNG"):
        """把示波器当前屏幕存成图片（:DISPlay:DATA?）。PNG 约 27-40 KB。

        ⚠️ 本机实测：这条路径抓的图**不渲染游标 X1 线**（只出 X2），且是**白底**
        （与 :SCReen:DUMP? 相反）。要抓完整游标面板请用 screenshot_dump()。
        """
        self.cmd(":DISPlay:DATA? %s" % fmt)
        data = self.blk()
        with open(path, "wb") as f:
            f.write(data)
        return len(data)

    def screenshot_dump(self, path, inksaver_off=True):
        """抓**整屏**（含右侧软键/光标面板），用 :SCReen:DUMP?。

        ★ 关键修复：:DISPlay:DATA? 在本机只渲染 X2 游标、X1 线不画；
          :SCReen:DUMP? 抓的是真实屏幕缓冲，X1/X2/ΔX/1÷ΔX 全出。
          0x59 全套游标图（X1(2)/X2(2)/ΔX/1÷ΔX 完整）即此路径；
          0x5B 用 :DISPlay:DATA? 仅出 X2。
        ★ HARDcopy:INKSaver 开着是白底、关掉是黑底。为和 0x59 一致默认关。
        """
        if inksaver_off:
            self.cmd(":HARDcopy:INKSaver OFF")
            time.sleep(0.15)
        self.cmd(":SCReen:DUMP? PNG")
        data = self.blk()
        with open(path, "wb") as f:
            f.write(data)
        return len(data)

    # ---------- 测量面板 ----------

    def add_meas(self, ch1=None, ch2=None, clear=True, fix_last_row=True):
        """往面板的『测量』列表里加项目。

        ★ 正确形式是 :MEASure:<类型> CHANnel<n>（无问号 = 设置）。
          :MEASure:ITEM ... 在本机型上是 -113 未定义头，别用。
        ★ fix_last_row：加完后**重发一次最后那条测量命令**——本机面板最后一行
          恒显示「未完成」（RESults 里其实有值），重发即可刷出。已确定性验证
          （连拍 6 次固定最后一行未完成，重发最后一条后 6 行全出值）。
        ★ 摘要页→测量页约 3s，必须等。
        """
        if clear:
            self.cmd(":MEASure:CLEar")   # 无返回，必须用 cmd()
            time.sleep(0.2)
        last = None
        for it in (ch1 or []):
            c = ":MEASure:%s CHANnel1" % it
            self.cmd(c)
            last = c
            time.sleep(0.06)
        for it in (ch2 or []):
            c = ":MEASure:%s CHANnel2" % it
            self.cmd(c)
            last = c
            time.sleep(0.06)
        time.sleep(3.0)                  # ★ 等面板从摘要页切到测量页
        if fix_last_row and last:
            self.cmd(last)               # 重发最后一条，刷出最后一行
            time.sleep(0.5)
        time.sleep(0.2)

    # ---------- 游标（时序量取证） ----------

    def set_markers(self, t1_us, t2_us, src1="CHANnel1", src2="CHANnel1"):
        """设双游标（手动模式），t 单位 us。用于时序量取证截图。

        两条游标都设到同一通道(默认 CH1/SCL)即可，取证只关心 ΔX（时间间隔）；
        若想让幅度读数与两路对应，可分别传 src1=CHANnel1 / src2=CHANnel2。
        """
        self.cmd(":MARKer:MODE MANual")
        time.sleep(0.2)
        self.cmd(":MARKer:X1Y1source %s" % src1)
        self.cmd(":MARKer:X2Y2source %s" % src2)
        self.cmd(":MARKer:X1Position %.10gE-6" % t1_us)
        self.cmd(":MARKer:X2Position %.10gE-6" % t2_us)
        time.sleep(0.3)

    def clear_markers(self):
        self.cmd(":MARKer:MODE OFF")
        time.sleep(0.2)

    # ---------- 自动配置（提效核心：省掉手动调档） ----------

    def autoscale(self):
        """一键自动量程（Keysight :AUToscale）：自动设 V/div、偏移、时基、触发。
        ⚠️ 执行后触发模式会被改成「边沿」，需再调 setup_i2c_trigger 设回 I2C 触发。"""
        self.cmd(":AUToscale")

    def setup_i2c_trigger(self, ch_scl, ch_sda, addr=None, condition="START"):
        """配置 I2C 硬件触发（需 DSO-I2C 选件授权）。

        condition: START(启动) | STARTR(重复启动) | STOP | ADDR(指定地址) | NACK …
        addr: 7 位地址(0-127)，仅 condition=ADDR 时用。
        返回 :SYSTem:ERRor? 队列 —— 若选件未授权会回 -2xx 报错，调用方据此降级。"""
        self.cmd(":TRIGger:MODE I2C")
        self.cmd(":TRIGger:I2C:SCL CHANnel%d" % ch_scl)
        self.cmd(":TRIGger:I2C:SDA CHANnel%d" % ch_sda)
        self.cmd(":TRIGger:I2C:CONDition %s" % condition.upper())
        if addr is not None and condition.upper() == "ADDR":
            self.cmd(":TRIGger:I2C:ADDRess %d" % int(addr))
        return self.err()

    def auto_setup_i2c(self, ch_scl, ch_sda, addr=None, condition="START"):
        """自动配置示波器用于 I2C 捕获（提效核心）：
        1) 打开两通道显示；2) :AUToscale 自动量程；3) 配置 I2C 触发。
        返回错误队列（用于判断 DSO-I2C 选件是否授权）。"""
        self.cmd(":CHANnel%d:DISPlay ON" % ch_scl)
        self.cmd(":CHANnel%d:DISPlay ON" % ch_sda)
        self.autoscale()
        time.sleep(1.0)
        return self.setup_i2c_trigger(ch_scl, ch_sda, addr, condition)

    def read_meas(self, ch1=None, ch2=None):
        """逐项查询（慢，每项要几秒，只在项目很少时用）。"""
        out = {}
        for it in (ch1 or []):
            out["CH1:" + it] = self.qf(":MEASure:%s? CHANnel1" % it)
        for it in (ch2 or []):
            out["CH2:" + it] = self.qf(":MEASure:%s? CHANnel2" % it)
        return out

    def read_meas_batch(self):
        """一次 :MEASure:RESults? 取回全部读数（**首选**）。

        返回 {标签: 值或 None}。标签形如 'FREQ(CH1)'。无效读数为 None。
        格式：逗号分隔，每组 7 个字段（标签,值,+5 个统计字段）。
        """
        s = self.q(":MEASure:RESults?")
        return parse_results(s)

    # ---------- 水平位置扫描（"左右微调"的可操作化）----------

    def scan_position(self, positions_us, ch1=None, ch2=None, settle=1.0,
                      verbose=True):
        """在若干水平位置之间扫描，记录每个位置下测量面板的有效项数。

        实测：面板显示"未完成"多半是屏幕刷新滞后的瞬时状态，换个位置就恢复；
        而如果窗口里混进了总线空闲段，读数是"有效但错"的（频率会被算成几十 kHz），
        所以扫描只能筛掉"无读数"，正确性仍要靠窗口内容判断（见 SKILL.md 第 5 节）。

        返回 (best_position_us, {pos: {标签: 值}})
        """
        res = {}
        best = None
        for p in positions_us:
            self.cmd(":TIMebase:POSition %.10gE-6" % p)
            time.sleep(settle)
            r = self.read_meas_batch()
            nok = sum(1 for v in r.values() if v is not None)
            res[p] = r
            if verbose:
                pretty = " ".join("%s=%.6g" % (k, v)
                                  for k, v in r.items() if v is not None)
                print("  pos=%+8.2fus  有效 %2d/%d  %s" % (p, nok, len(r), pretty))
            if best is None or nok > best[1]:
                best = (p, nok, r)
        return (best[0] if best else None), res


def parse_results(s):
    """解析 :MEASure:RESults? 的返回串 -> {标签: 值或 None}。"""
    if not s or s.startswith("<"):
        return {}
    f = [x.strip() for x in s.split(",")]
    out = {}
    i = 0
    while i + 6 < len(f):
        name = f[i]
        val = None
        try:
            v = float(f[i + 1])
            if abs(v) <= INVALID_TOL:
                val = v
        except ValueError:
            val = None
        if name:
            out[name] = val
        i += 7
    return out


def open_scope(host="<SCOPE_IP>", port=5025, timeout=10.0, verbose=False):
    """便捷入口。"""
    return Scope(host, port, timeout, verbose)


if __name__ == "__main__":
    import json
    import sys
    host = sys.argv[1] if len(sys.argv) > 1 else "<SCOPE_IP>"
    sc = Scope(host)
    print("IDN:", sc.idn())
    print("探头:", sc.probes())
    print(json.dumps(sc.state(), ensure_ascii=False, indent=2))
    print("错误队列:", sc.err())
    sc.close()
