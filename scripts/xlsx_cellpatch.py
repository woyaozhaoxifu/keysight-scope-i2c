# -*- coding: utf-8 -*-
"""在 zip/XML 层改写 xlsx 单元格 —— 专为「文件里嵌了 OLE 对象(波形图包)」而写。

为什么不用 edsdk / fill_excel.py / openpyxl：
  * **edsdk 保存会摧毁 OLE 包**。实测 DSO 报告（含 oleObject1/2.bin + vmlDrawing）：
    经 edsdk open→写一格→save 之后，xl/embeddings/* 全部消失，
    条目 158→164、体积 4 151 834→3 924 098 B，改用普通 drawing 顶替。
    —— 凡是文件里有 xl/embeddings/，**禁止**用本地 Office 编辑器保存。
  * openpyxl 重存同样会丢（实测 3.94MB→3.53MB，掉 410KB 绘图/媒体）。
  * 本脚本逐条复制 zip、只替换目标 sheet 的 XML，OLE/VML/媒体一个字节不动。

用法：
    python xlsx_cellpatch.py --file B.xlsx [--sheet 时序测试] --plan plan.json [--out C.xlsx]

plan.json:
{
  "edits": [
    {"ref": "R23",  "value": "5.952"},          # 数值
    {"ref": "AH23", "text": "2026-09-24 复测..."} # 文本(写成 inlineStr)
  ]
}
--out 省略则原地改（先自动备份 .BAK_时间戳）。

⚠️ 合并单元格：值**必须写在合并块左上格**，否则 Excel 显示会异常。
   本脚本只按 ref 精确改写既有格；如目标格当前是自闭合 <c .../>，会自动补 <v>。
"""

import argparse
import os
import re
import shutil
import zipfile
from datetime import datetime

EMB_PAT = re.compile(r'^xl/embeddings/')


def esc(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def cell_re(ref):
    return re.compile(
        r'<c r="%s"(?:(?P<a1>[^>/]*)/>|(?P<a2>[^>]*)>(?P<inner>.*?)</c>)' % re.escape(ref),
        re.S)


def set_value(xml, ref, val):
    m = cell_re(ref).search(xml)
    if not m:
        return xml, 'MISSING(格不存在)'
    attr = re.sub(r'\s+t="[^"]*"', '', m.group('a1') or m.group('a2') or '')
    new = '<c r="%s"%s><v>%s</v></c>' % (ref, attr, val)
    return xml.replace(m.group(0), new, 1), 'ok'


def set_text(xml, ref, text):
    m = cell_re(ref).search(xml)
    if not m:
        return xml, 'MISSING(格不存在)'
    attr = re.sub(r'\s+t="[^"]*"', '', m.group('a1') or m.group('a2') or '')
    new = ('<c r="%s"%s t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
           % (ref, attr, esc(text)))
    return xml.replace(m.group(0), new, 1), 'ok'


def find_sheet_path(zf, sheet_name):
    """按显示名找 worksheet XML 路径；sheet_name 为空则返回 None。"""
    if not sheet_name:
        return None
    wb = zf.read('xl/workbook.xml').decode('utf-8')
    sheets = re.findall(r'<sheet[^>]*name="([^"]+)"[^>]*r:id="([^"]+)"', wb)
    rels = zf.read('xl/_rels/workbook.xml.rels').decode('utf-8')
    m = dict(re.findall(r'Id="([^"]+)"[^>]*Target="([^"]+)"', rels))
    for nm, rid in sheets:
        if nm.strip() == sheet_name.strip():
            t = m.get(rid)
            if t:
                return t.lstrip('/') if t.startswith('xl/') else 'xl/' + t.lstrip('/')
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', required=True)
    ap.add_argument('--sheet', default=None, help='工作表显示名，如「时序测试」')
    ap.add_argument('--plan', required=True)
    ap.add_argument('--out', default=None, help='省略则原地改(先备份)')
    a = ap.parse_args()

    import json
    plan = json.load(open(a.plan, encoding='utf-8'))
    edits = plan['edits']
    src = os.path.abspath(a.file)
    inplace = not a.out
    dst = src if inplace else os.path.abspath(a.out)

    zf = zipfile.ZipFile(src)
    names = zf.namelist()
    emb = [n for n in names if EMB_PAT.match(n)]
    print('源文件: %s  (%d B, %d 条)' % (src, os.path.getsize(src), len(names)))
    print('OLE 嵌入对象: %s' % (emb if emb else '无'))
    if emb:
        print('  → 检测到 OLE, 走 zip/XML 层改写(编辑器保存会摧毁它们)')

    sheet_path = find_sheet_path(zf, a.sheet)
    if not sheet_path:
        cands = [n for n in names if re.match(r'xl/worksheets/sheet\d+\.xml$', n)]
        print('!! 未定位到工作表, 可用 worksheet:', cands)
        return 2
    print('工作表 XML: %s' % sheet_path)

    xml = zf.read(sheet_path).decode('utf-8')
    log = []
    for e in edits:
        if 'text' in e:
            xml, st = set_text(xml, e['ref'], e['text'])
            log.append('  %-7s text(%d字) %s' % (e['ref'], len(e['text']), st))
        else:
            xml, st = set_value(xml, e['ref'], e['value'])
            log.append('  %-7s = %-10s %s' % (e['ref'], e['value'], st))
    print('改写明细:'); [print(x) for x in log]

    if inplace:
        stamp = datetime.now().strftime('%Y%m%d_%H%M')
        bak = src.replace('.xlsx', '.BAK_%s.xlsx' % stamp)
        if not os.path.exists(bak):
            shutil.copy2(src, bak)
        print('已备份: %s' % os.path.basename(bak))

    tmp = dst + '.tmp'
    zout = zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED)
    for item in zf.infolist():
        data = zf.read(item.filename)
        if item.filename == sheet_path:
            data = xml.encode('utf-8')
        zout.writestr(item, data)
    zout.close()
    zf.close()
    # ★ 用 copy2 而非 os.replace：若该文件被编辑器/Excel 持有句柄，
    #   os.replace(原子替换需独占) 会抛 WinError 5 拒绝访问，copy2 则能成功。
    shutil.copy2(tmp, dst)
    os.remove(tmp)

    zv = zipfile.ZipFile(dst)
    emb2 = [n for n in zv.namelist() if EMB_PAT.match(n)]
    print('\n=== 校验 ===')
    print('OLE: %s -> %s  %s' % (emb, emb2, '完好 OK' if emb2 == emb else '丢失 FAIL'))
    print('条目: %d -> %d' % (len(names), len(zv.namelist())))
    print('体积: %d -> %d' % (os.path.getsize(src), os.path.getsize(dst)))
    sx = zv.read(sheet_path).decode('utf-8')
    for e in edits:
        m = cell_re(e['ref']).search(sx)
        print('  %-7s %s' % (e['ref'], (m.group(0)[:100] if m else '(缺)')))
    zv.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
