# -*- coding: utf-8 -*-
"""
xlsx 安全写入模块 —— 在保留「单元格内图片 / 浮动图片 / 打印设置 / 元数据」的前提下写入单元格值。

为什么需要这个模块？
    openpyxl 保存 xlsx 时会**丢弃所有它不认识的部件**，实测会丢失：
        xl/media/*（图片本体）、xl/richData/*（单元格内图片关联）、
        xl/metadata.xml、xl/printerSettings/*
    因此凡是表里贴了图的 Excel，都不能用 openpyxl 保存。

为什么不用 ElementTree 整体解析？
    ET 会吞掉 xmlns 声明，序列化时改写命名空间前缀，
    导致 `mc:Ignorable="x14ac xr xr2 xr3"` 引用的前缀变成未声明 → Excel 报 XML 错误。
    实测这条路不可靠。

本模块的做法（字符串级外科手术）：
    1. 直接以 zip 为单位操作
    2. 只对目标工作表的 sheetN.xml 做**定点字符串替换/插入**
    3. 其余字节原样搬运
    4. 写完后用 XML 解析器校验良构性，不通过就报错，绝不写出坏文件

用法:
    from xlsx_safe_writer import write_cells, read_sheet_cells
    write_cells("in.xlsx", "out.xlsx", {"Sheet1": {"C1": "标题"}})
"""
import os
import re
import shutil
import zipfile
from xml.etree import ElementTree as ET

XML_DECL = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'


# ---------------------------------------------------------------- 基础工具

def col_to_idx(col):
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n


def idx_to_col(idx):
    s = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        s = chr(65 + rem) + s
    return s


def parse_ref(ref):
    m = re.match(r"([A-Za-z]+)(\d+)", ref or "")
    return (m.group(1), int(m.group(2))) if m else (None, None)


def _local(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def _esc(text):
    """XML 文本转义"""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;"))


# ---------------------------------------------------------------- 读取

def read_shared_strings(xlsx_path):
    """读取共享字符串表，返回 [文本, ...]（按索引）"""
    z = zipfile.ZipFile(xlsx_path)
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    out = []
    for si in root:
        if _local(si.tag) != "si":
            continue
        out.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))
    return out


def list_sheets(xlsx_path):
    """返回 [(sheet名, sheet内部路径), ...]"""
    z = zipfile.ZipFile(xlsx_path)
    names = z.namelist()
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = {}
    if "xl/_rels/workbook.xml.rels" in names:
        rr = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        for rel in rr:
            rels[rel.get("Id")] = rel.get("Target")
    out = []
    for sh in wb.iter():
        if _local(sh.tag) != "sheet":
            continue
        rid = sh.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")
        target = rels.get(rid, "")
        path = ("xl/" + target.lstrip("/")) if not target.startswith("xl/") else target
        path = path.replace("xl/xl/", "xl/")
        out.append((sh.get("name"), path))
    return out


def read_sheet_cells(xlsx_path, sheet_path, resolve_shared=True):
    """
    读取工作表的单元格文本值，返回 {单元格: 值}

    resolve_shared=True 时会解析共享字符串（t="s"），
    否则 t="s" 的单元格只会返回字符串索引。
    """
    z = zipfile.ZipFile(xlsx_path)
    root = ET.fromstring(z.read(sheet_path))
    shared = read_shared_strings(xlsx_path) if resolve_shared else []
    cells = {}
    for c in root.iter():
        if _local(c.tag) != "c":
            continue
        ref = c.get("r")
        if not ref:
            continue
        ctype = c.get("t")
        val = None
        for child in c:
            if _local(child.tag) == "v":
                val = child.text
            elif _local(child.tag) == "is":
                ts = [t.text or "" for t in child.iter() if _local(t.tag) == "t"]
                val = "".join(ts)
        if val is None:
            continue
        if ctype == "s" and shared:
            try:
                val = shared[int(val)]
            except (ValueError, IndexError):
                pass
        cells[ref] = val
    return cells


# ---------------------------------------------------------------- 定点写入

_CELL_RE = r'<c\s+r="{ref}"(?:\s[^>]*?)?(?:\s*/>|>[\s\S]*?</c>)'
_ROW_RE = r'<row\s+r="{row}"(?:\s[^>]*?)?(?:\s*/>|>[\s\S]*?</row>)'
_SHEETDATA_RE = r'<sheetData\s*/>|<sheetData(?:\s[^>]*)?>[\s\S]*?</sheetData>'


def _make_cell_xml(ref, text, style=None):
    """构造一个 inlineStr 单元格（不依赖 sharedStrings），可保留原样式索引"""
    s_attr = f' s="{style}"' if style else ""
    return (f'<c r="{ref}"{s_attr} t="inlineStr"><is>'
            f'<t xml:space="preserve">{_esc(text)}</t></is></c>')


def _replace_or_insert_in_row(row_xml, cell_xml, ref, style=None):
    """在 <row> 内部按列序插入/替换单元格（保留原有样式）"""
    col, _ = parse_ref(ref)

    # 已存在同坐标单元格 → 直接替换（沿用其样式）
    m = re.search(_CELL_RE.format(ref=re.escape(ref)), row_xml)
    if m:
        old = m.group(0)
        sm = re.search(r'\ss="(\d+)"', old)
        if sm:
            cell_xml = _make_cell_xml(ref, _extract_text(cell_xml), sm.group(1))
        return row_xml[:m.start()] + cell_xml + row_xml[m.end():]

    # 否则按列序插入
    target_idx = col_to_idx(col)
    pos = None
    for cm in re.finditer(r'<c\s+r="([A-Za-z]+)\d+"', row_xml):
        ccol = cm.group(1)
        if col_to_idx(ccol) > target_idx:
            pos = cm.start()
            break
    if pos is not None:
        return row_xml[:pos] + cell_xml + row_xml[pos:]

    # 追加到 </row> 之前
    end = row_xml.rfind("</row>")
    if end != -1:
        return row_xml[:end] + cell_xml + row_xml[end:]
    # 自闭合 <row .../> → 展开
    m2 = re.match(r'^(<row\s[^>]*?)\s*/>$', row_xml.strip())
    if m2:
        return m2.group(1) + ">" + cell_xml + "</row>"
    return row_xml


def _extract_text(cell_xml):
    """从已构造的单元格 XML 里取回文本"""
    m = re.search(r'<t[^>]*>([\s\S]*?)</t>', cell_xml)
    if not m:
        return ""
    return (m.group(1).replace("&lt;", "<").replace("&gt;", ">")
            .replace("&amp;", "&"))


def _set_cell_in_sheet(xml, ref, text):
    """在整张工作表的 XML 字符串里设置某个单元格"""
    cell_xml = _make_cell_xml(ref, text)
    col, row = parse_ref(ref)
    if not col:
        raise ValueError(f"非法单元格引用: {ref}")

    # 1) 目标行已存在
    m = re.search(_ROW_RE.format(row=row), xml)
    if m:
        new_row = _replace_or_insert_in_row(m.group(0), cell_xml, ref)
        return xml[:m.start()] + new_row + xml[m.end():]

    # 2) 需要新建行
    sd = re.search(_SHEETDATA_RE, xml)
    if not sd:
        raise ValueError("未找到 <sheetData>，无法写入")
    sd_text = sd.group(0)
    if sd_text.endswith("/>"):
        # <sheetData/> → 展开
        new_sd = sd_text[:-2] + f'><row r="{row}">{cell_xml}</row></sheetData>'
        return xml[:sd.start()] + new_sd + xml[sd.end():]

    # 在 sheetData 内按行号插入新行
    new_row = f'<row r="{row}">{cell_xml}</row>'
    inner_start = sd_text.find(">") + 1
    inner_end = sd_text.rfind("</sheetData>")
    inner = sd_text[inner_start:inner_end]
    pos = None
    for rm in re.finditer(r'<row\s+r="(\d+)"', inner):
        if int(rm.group(1)) > row:
            pos = rm.start()
            break
    if pos is None:
        inner_new = inner + new_row
    else:
        inner_new = inner[:pos] + new_row + inner[pos:]
    new_sd = sd_text[:inner_start] + inner_new + sd_text[inner_end:]
    return xml[:sd.start()] + new_sd + xml[sd.end():]


def _update_dimension_str(xml, refs):
    """更新 <dimension ref="A1:XX99">，保证 Excel 正确识别数据范围"""
    max_col, max_row = 0, 0
    for ref in refs:
        col, row = parse_ref(ref)
        if col:
            max_col = max(max_col, col_to_idx(col))
            max_row = max(max_row, row)
    if not max_col:
        return xml
    m = re.search(r'<dimension\s+ref="([A-Za-z]+\d+):([A-Za-z]+)(\d+)"', xml)
    if not m:
        m2 = re.search(r'<dimension\s+ref="([A-Za-z]+\d+)"', xml)
        if m2:
            start = m2.group(1)
            return xml[:m2.start()] + f'<dimension ref="{start}:{idx_to_col(max_col)}{max_row}"' + xml[m2.end():]
        return xml
    old_col = col_to_idx(m.group(2))
    old_row = int(m.group(3))
    new_col = max(max_col, old_col)
    new_row = max(max_row, old_row)
    new_ref = f'{m.group(1)}:{idx_to_col(new_col)}{new_row}'
    return xml[:m.start()] + f'<dimension ref="{new_ref}"' + xml[m.end():]


def _assert_wellformed(xml, label=""):
    """校验 XML 良构性，不通过就抛错——绝不写出坏文件"""
    try:
        ET.fromstring(xml.encode("utf-8"))
    except ET.ParseError as e:
        raise RuntimeError(f"生成的 XML 不良构（{label}）：{e}") from e


def write_cells(xlsx_in, xlsx_out, updates, backup=True, verify=True):
    """
    在保留全部其他部件的前提下写入单元格。

    updates: {工作表名: {单元格引用: 值}}
    返回: (写入条数, 说明列表)
    """
    notes = []
    sheets = dict(list_sheets(xlsx_in))
    zin = zipfile.ZipFile(xlsx_in)
    parts = {n: zin.read(n) for n in zin.namelist()}
    zin.close()

    modified = {}
    count = 0

    for sheet_name, cell_map in updates.items():
        if not cell_map:
            continue
        sheet_path = sheets.get(sheet_name)
        if not sheet_path or sheet_path not in parts:
            notes.append(f"⚠ 未找到工作表「{sheet_name}」，跳过")
            continue

        raw = parts[sheet_path].decode("utf-8")
        original = raw
        # 记录目标单元格是否本来就挂着图片元数据（vm），防止破坏图片
        for ref in cell_map:
            m = re.search(r'<c\s+r="%s"(?:\s[^>]*)?>' % re.escape(ref), raw)
            if m and ' vm="' in m.group(0):
                raise ValueError(f"单元格 {ref} 关联着内嵌图片，拒绝覆盖以免破坏图片")

        for ref, val in cell_map.items():
            raw = _set_cell_in_sheet(raw, ref, val)
            count += 1
        raw = _update_dimension_str(raw, list(cell_map.keys()))

        if verify:
            _assert_wellformed(raw, f"{sheet_name}")
            notes.append(f"✓ 工作表「{sheet_name}」XML 良构校验通过")

        modified[sheet_path] = raw.encode("utf-8")
        notes.append(f"✓ 工作表「{sheet_name}」写入 {len(cell_map)} 个单元格")

        # 报告改动范围（应只有极少量字节变化）
        diff = sum(1 for a, b in zip(original, raw) if a != b)
        notes.append(f"✓ 改动字节数 {diff}（原 {len(original)}，新 {len(raw)}）")

    if not modified:
        return 0, notes

    if backup and os.path.abspath(xlsx_in) == os.path.abspath(xlsx_out):
        import time as _t
        stamp = _t.strftime("%Y%m%d_%H%M%S")
        base, ext = os.path.splitext(xlsx_in)
        bak = f"{base}_backup_{stamp}{ext}"
        shutil.copy2(xlsx_in, bak)
        notes.append(f"✓ 已备份原文件: {os.path.basename(bak)}")

    with zipfile.ZipFile(xlsx_out, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in parts.items():
            zout.writestr(name, modified.get(name, data))

    zout = zipfile.ZipFile(xlsx_out)
    out_names = zout.namelist()
    # 图片可能位于 xl/media/ 或 xl/richData/media/，两处都要数
    media = [n for n in out_names
             if re.search(r"(^|/)media/[^/]+\.(png|jpe?g|gif|bmp|webp)$", n, re.I)]
    rich = [n for n in out_names if n.startswith("xl/richData/")]
    notes.append(f"✓ 输出文件保留 {len(media)} 个图片、{len(rich)} 个 richData 部件")
    return count, notes


if __name__ == "__main__":
    import sys
    src = sys.argv[1]
    print("工作表列表:")
    for name, path in list_sheets(src):
        print(f"  {name} -> {path}")
