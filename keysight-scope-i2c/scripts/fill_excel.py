# -*- coding: utf-8 -*-
"""把实测值回填进本地 Excel 报告（封装 tencent-local-office-edit 的 edsdk CLI）。

把踩过的坑都包进来了：
  * file_id 到底是 `D:\\a\\b.xlsx` 还是 `D://a//b.xlsx` 不确定 -> 多候选自动重试。
    用错形式会报 "workbook is not open"，看着像没打开，其实是 key 没对上。
  * 编辑器实例会被回收 -> 写失败时自动重新 open_file 再试。
  * 写之前先备份，写之后必须回读校验（不校验等于没写）。

用法：
    # 1) 看实例与文件 id 候选
    python fill_excel.py fileid --file "D:<WORKDIR>\\report.xlsx"

    # 2) 读一段，确认列号
    python fill_excel.py read --file "D:<WORKDIR>\\report.xlsx" \
        --sheet-id 00002f --rows 17:21 --cols 0:12

    # 3) 按计划回填（先备份 -> 写 -> 保存 -> 回读校验）
    python fill_excel.py write --plan plan.json

plan.json 格式：
{
  "file": "D:\\<WORKDIR>\\\\报告.xlsx",
  "sheet_id": "00002f",
  "backup": true,
  "edits": [
    {"row": 18, "col": 5, "value": "99.52 kHz"},
    {"row": 18, "col": 6, "value": 4.28}
  ]
}
value_type 可省略，按值自动推断：数字->NUMBER，=开头->FORMULA，
true/false->BOOL，其余->STRING。
"""

import argparse
import csv as _csv
import io
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime

PYTHON = sys.executable
EDS_DIR = os.environ.get(
    "EDS_SKILL_DIR",
    r"D:\workbuddy\resources\app.asar.unpacked\resources\plugins"
    r"\workbuddy-builtin\skills\tencent-local-office-edit")
EDS = os.path.join(EDS_DIR, "edsdk.py")


def eds_call(tool, args_obj=None, kv=None, timeout=120):
    """调一次 edsdk.py call <tool>。返回 (ok, 原始输出)。"""
    cmd = [PYTHON, EDS, "call", tool]
    tmp = None
    if args_obj is not None:
        tmp = os.path.join(os.environ.get("TEMP", "."),
                           "_eds_args_%d.json" % int(time.time() * 1000))
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(args_obj, f, ensure_ascii=False)
        cmd += ["--json-file", tmp]
    if kv:
        cmd += ["%s=%s" % (k, v) for k, v in kv.items()]
    try:
        p = subprocess.run(cmd, cwd=EDS_DIR, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout)
        return (p.returncode == 0), (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return False, "<timeout>"
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def json_of(out):
    """从 edsdk 输出里抠出第一个 JSON 对象/数组。"""
    for open_ch, close_ch in (("[", "]"), ("{", "}")):
        i = out.find(open_ch)
        while i != -1:
            depth = 0
            for j in range(i, len(out)):
                if out[j] == open_ch:
                    depth += 1
                elif out[j] == close_ch:
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(out[i:j + 1])
                        except ValueError:
                            break
            i = out.find(open_ch, i + 1)
    return None


# ---------------- file_id 候选 ----------------

def file_id_candidates(path):
    """给定 Windows 路径，生成 edsdk 可能接受的 file_id 形式（按可能性排序）。"""
    p = os.path.abspath(path)
    fwd = p.replace("\\", "/")            # <WORKDIR>/a.xlsx
    dbl = p.replace("\\", "//")           # D://WORKDIR//a.xlsx
    drive = os.path.splitdrive(p)[0].rstrip(":")  # D
    if drive:
        rest = p[len(os.path.splitdrive(p)[0]) + 1:]
        dbl = drive + "//" + rest.replace("\\", "//")
    out = []
    for c in (p, dbl, fwd, dbl.replace("//", "/")):
        if c and c not in out:
            out.append(c)
    return out


def do_fileid(args):
    ok, out = eds_call("get_pool_status")
    print("=== get_pool_status ===")
    print(out[:2000])
    print("\n=== 该文件的 file_id 候选（按顺序试） ===")
    for c in file_id_candidates(args.file):
        print("  ", repr(c))


def do_read(args):
    r = args.rows.split(":")
    c = args.cols.split(":")
    sr, er = int(r[0]), int(r[1])
    sc_, ec = int(c[0]), int(c[1])
    ids = file_id_candidates(args.file)
    last = ""
    for fid in ids:
        eds_call("open_file", {"file_path": os.path.abspath(args.file)})
        # ★ open_file 是异步的（返回 "open started ... chunks streaming via /stream"）。
        #   不等它完成就查询会得到 "workbook is not open"，看着像文件打不开。
        time.sleep(3)
        ok, out = eds_call("sheet_get_cell_data", {
            "file_id": fid, "sheet_id": args.sheet_id,
            "start_row": sr, "start_col": sc_,
            "end_row": er, "end_col": ec, "return_csv": True})
        d = json_of(out)
        csv = (d or {}).get("csv_data") if isinstance(d, dict) else None
        if csv:
            print("file_id=%r" % fid)
            for i, line in enumerate(csv.split("\n")):
                print("%3d| %s" % (sr + i, line))
            return
        last = out
    print("读取失败。最后一次输出：\n", last[:1500])


# ---------------- 写 ----------------

def infer_type(v):
    if isinstance(v, bool):
        return "BOOL"
    if isinstance(v, (int, float)):
        return "NUMBER"
    s = str(v)
    if s.startswith("="):
        return "FORMULA"
    if s.strip().lower() in ("true", "false"):
        return "BOOL"
    return "STRING"


def to_cell(edit):
    t = (edit.get("type") or infer_type(edit["value"])).upper()
    cell = {"row": int(edit["row"]), "col": int(edit["col"]), "value_type": t}
    v = edit["value"]
    if t == "NUMBER":
        cell["number_value"] = float(v)
    elif t == "BOOL":
        cell["bool_value"] = bool(v) if isinstance(v, bool) else \
            str(v).strip().lower() == "true"
    elif t == "FORMULA":
        cell["formula"] = str(v)
    else:
        cell["string_value"] = str(v)
    return cell


def readback(file_id, sheet_id, edits):
    """回读刚写的格子，返回 {('r','c'): 值}。"""
    rows = [int(e["row"]) for e in edits]
    cols = [int(e["col"]) for e in edits]
    ok, out = eds_call("sheet_get_cell_data", {
        "file_id": file_id, "sheet_id": sheet_id,
        "start_row": min(rows), "start_col": min(cols),
        "end_row": max(rows), "end_col": max(cols), "return_csv": True})
    d = json_of(out)
    csv = (d or {}).get("csv_data") if isinstance(d, dict) else None
    got = {}
    if csv:
        # ★ 必须用 csv 模块解析：Notes 这类单元格本身就含逗号，
        #   用 line.split(",") 会错位，把"写对了"误报成"不一致"。
        grid = list(_csv.reader(io.StringIO(csv)))
        for e in edits:
            r, c = int(e["row"]), int(e["col"])
            try:
                got[(r, c)] = grid[r - min(rows)][c - min(cols)]
            except IndexError:
                got[(r, c)] = None
    return got


def _same(want, have):
    """比较期望值与回读值。返回 (是否一致, 附注)。

    ★ 回读走的是**单元格显示值**（会被单元格的数字格式舍入），
      所以 5.4053 写进 2 位小数的列，回读是 5.41 —— 这不是写错。
      数字按"能对上的最短精度"判定，并标注格式舍入。
    """
    a = (want or "").strip()
    b = (have or "").strip()
    if a == b:
        return True, ""
    try:
        fa, fb = float(a), float(b)
    except ValueError:
        return False, ""
    if abs(fa - fb) < 1e-12:
        return True, ""
    # 回读被格式舍入：按回读值的显示精度重新舍入期望值再比
    dec = len(b.split(".")[1]) if "." in b else 0
    if round(fa, dec) == fb:
        return True, "  OK(单元格格式舍入到 %d 位小数)" % dec
    if abs(fa - fb) <= max(abs(fb) * 1e-4, 1e-9):
        return True, "  OK(数值容差内)"
    return False, ""


def do_write(args):
    plan = json.load(open(args.plan, encoding="utf-8"))
    path = os.path.abspath(plan["file"])
    sheet_id = plan["sheet_id"]
    edits = plan["edits"]

    print("目标文件:", path)
    if not os.path.exists(path):
        print("文件不存在，退出。")
        return 1

    # 1) 备份
    if plan.get("backup", True):
        stamp = datetime.now().strftime("%Y%m%d_%H%M")
        root, ext = os.path.splitext(path)
        bak = "%s.BAK_%s%s" % (root, stamp, ext)
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
            print("已备份:", bak)
        else:
            print("备份已存在:", bak)

    # ★ OLE 安全闸门：本工具底层是本地 Office 编辑器，保存时会重建 xlsx，
    #   把 xl/embeddings/* 下的 OLE 嵌入对象（如波形图 zip 包）整个丢掉。
    #   实测：embeddings 2 -> 0、条目 158 -> 164、体积掉 228KB、被普通 drawing 顶替。
    #   这类文件必须改用 xlsx_cellpatch.py（zip/XML 层逐条复制改写）。
    if not plan.get("force_ole"):
        try:
            import zipfile as _zf
            with _zf.ZipFile(path) as _z:
                _emb = [n for n in _z.namelist() if n.startswith("xl/embeddings/")]
            if _emb:
                print("⛔ 检测到 OLE 嵌入对象: %s" % _emb)
                print("   本工具(edsdk)保存会摧毁它们 —— 请改用 xlsx_cellpatch.py（zip/XML 层）。")
                print('   确需强行覆盖请在 plan.json 加 "force_ole": true')
                return 3
        except Exception as _e:
            print("(OLE 检测跳过: %s)" % _e)

    cells = [to_cell(e) for e in edits]
    print("待写 %d 个单元格" % len(cells))

    # 2) 打开 + 写入（多 file_id 候选自动重试）
    last = ""
    for fid in file_id_candidates(path):
        eds_call("open_file", {"file_path": path})
        time.sleep(3)
        args_obj = {"file_id": fid, "sheet_id": sheet_id, "values": cells}
        ok, out = eds_call("sheet_set_range_value", args_obj)
        if ok and "error" not in out.lower():
            print("写入成功，使用 file_id=%r" % fid)
            eds_call("save_file", {"file_id": fid})
            time.sleep(1.5)
            got = readback(fid, sheet_id, edits)
            ok_all = True
            print("\n回读校验：")
            for e in edits:
                key = (int(e["row"]), int(e["col"]))
                want = str(e["value"])
                have = got.get(key)
                match, note = _same(want, have)
                ok_all = ok_all and match
                print("  r%02d c%02d 期望=%-24s 实读=%-24s %s%s"
                      % (key[0], key[1], want[:24], (have or "")[:24],
                         "OK" if match else "不一致", note))
            print("\n结果:", "全部一致 ✅" if ok_all else "存在不一致 ⚠️（请人工复核）")
            return 0 if ok_all else 2
        last = out
        if "not open" in out.lower() or "file_id" in out.lower():
            print("  file_id=%r 不行，换下一个形式重试" % fid)
        else:
            print("  写入失败: %s" % out[:300])
    print("全部 file_id 形式都失败。最后输出：\n", last[:1500])
    return 1


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p1 = sub.add_parser("fileid")
    p1.add_argument("--file", required=True)
    p1.set_defaults(func=do_fileid)

    p2 = sub.add_parser("read")
    p2.add_argument("--file", required=True)
    p2.add_argument("--sheet-id", required=True)
    p2.add_argument("--rows", default="0:20", help="起:止（0-based，含止）")
    p2.add_argument("--cols", default="0:20")
    p2.set_defaults(func=do_read)

    p3 = sub.add_parser("write")
    p3.add_argument("--plan", required=True)
    p3.set_defaults(func=do_write)

    args = ap.parse_args()
    if not os.path.exists(EDS):
        print("找不到 edsdk.py：%s\n可设环境变量 EDS_SKILL_DIR 指定目录。" % EDS)
        return 2
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
