# -*- coding: utf-8 -*-
"""
从 Excel 中提取「单元格内图片 / 浮动图片」，建立「单元格 → 图片文件」映射。

背景：
    Excel 有两种贴图方式，都必须先提取才能喂给视觉模型（模型读不了 xlsx）：
      A. 单元格内图片（Excel 365「放置在单元格中」）
         —— 图片存于 xl/media/，通过 xl/richData/ 与单元格 vm 元数据关联
         —— ⚠️ openpyxl 读不到，必须直接解析 XML
      B. 浮动图片（传统贴图）
         —— 通过 xl/drawings/drawingN.xml 锚定到单元格
         —— openpyxl 可通过 ws._images 读到

用法:
    python extract_cell_images.py <xlsx路径> [输出目录]
"""
import os
import posixpath
import re
import sys
import zipfile
from xml.etree import ElementTree as ET

NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "xdr": "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "xlrd": "http://schemas.microsoft.com/office/spreadsheetml/2017/richdata",
}


def _local(tag):
    """去掉命名空间，只留标签名"""
    return tag.split("}")[-1] if "}" in tag else tag


def _find_all_local(root, name):
    """按标签名查找（忽略命名空间），避免各家命名空间不一致导致漏读"""
    return [e for e in root.iter() if _local(e.tag) == name]


def resolve_rel_target(rels_part, target):
    """
    把 relationships 里的相对 Target 解析成 zip 内的绝对路径。

    ⚠️ 重要：图片位置不止一处。实测同一份文件在不同工具保存后，
       图片可能位于 `xl/media/`，也可能位于 `xl/richData/media/`。
       必须按 rels 文件的相对位置解析，不能写死路径。

    例：rels_part = 'xl/richData/_rels/richValueRel.xml.rels'
        target    = 'media/image1.jpeg'          -> 'xl/richData/media/image1.jpeg'
        target    = '../media/image1.png'        -> 'xl/media/image1.png'
    """
    if not target:
        return ""
    if target.startswith("/"):
        return target.lstrip("/")
    base = posixpath.dirname(rels_part)          # xl/richData/_rels
    if posixpath.basename(base) == "_rels":
        base = posixpath.dirname(base)           # xl/richData
    return posixpath.normpath(posixpath.join(base, target))


def all_media_parts(names):
    """列出压缩包里所有图片部件（兼容 xl/media/ 与 xl/richData/media/ 两种布局）"""
    return sorted(n for n in names
                  if re.search(r"(^|/)media/[^/]+\.(png|jpe?g|gif|bmp|webp)$", n, re.I))


def _col_to_idx(col):
    n = 0
    for ch in col:
        n = n * 26 + (ord(ch.upper()) - 64)
    return n


def _parse_ref(ref):
    """'B3' -> ('B', 3)"""
    m = re.match(r"([A-Za-z]+)(\d+)", ref or "")
    return (m.group(1), int(m.group(2))) if m else (None, None)


def extract(xlsx_path, out_dir):
    """返回 (图片列表, 说明)，图片列表元素为 dict(cell, sheet, path, media)"""
    z = zipfile.ZipFile(xlsx_path)
    names = z.namelist()
    results = []
    notes = []

    os.makedirs(out_dir, exist_ok=True)

    # ---------- 1. 工作表 → 单元格 → 图片（richData 单元格内图片）----------
    # richValueRel.xml 的 rel 顺序 ↔ richValueRel.xml.rels 的 rId
    RICH_RELS = "xl/richData/_rels/richValueRel.xml.rels"
    rid_to_media = {}
    if RICH_RELS in names:
        root = ET.fromstring(z.read(RICH_RELS))
        for rel in _find_all_local(root, "Relationship"):
            rid_to_media[rel.get("Id")] = resolve_rel_target(RICH_RELS, rel.get("Target"))

    rich_rels = []
    if "xl/richData/richValueRel.xml" in names:
        root = ET.fromstring(z.read("xl/richData/richValueRel.xml"))
        for rel in _find_all_local(root, "rel"):
            rid = rel.get(f"{{{NS['r']}}}id") or rel.get("id")
            rich_rels.append(rid_to_media.get(rid))

    # metadata.xml: valueMetadata 第 N 项 → richValue 索引
    vm_to_rv = {}
    if "xl/metadata.xml" in names:
        root = ET.fromstring(z.read("xl/metadata.xml"))
        vm_nodes = _find_all_local(root, "valueMetadata")
        if vm_nodes:
            for i, bk in enumerate(_find_all_local(vm_nodes[0], "bk"), start=1):
                rcs = _find_all_local(bk, "rc")
                if rcs and rcs[0].get("v") is not None:
                    vm_to_rv[i] = int(rcs[0].get("v"))

    # 遍历每张工作表，找带 vm 属性的单元格
    for sheet_file in [n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]:
        sheet_name = os.path.basename(sheet_file).replace(".xml", "")
        root = ET.fromstring(z.read(sheet_file))
        for c in _find_all_local(root, "c"):
            vm = c.get("vm")
            if not vm:
                continue
            rv_idx = vm_to_rv.get(int(vm))
            if rv_idx is None or rv_idx >= len(rich_rels):
                continue
            media = rich_rels[rv_idx]
            if not media or media not in names:
                continue
            col, r = _parse_ref(c.get("r"))
            results.append({
                "sheet": sheet_name,
                "cell": c.get("r"),
                "col": col,
                "row": r,
                "media": media,
                "kind": "单元格内图片",
            })
    if results:
        notes.append(f"发现 {len(results)} 张「单元格内图片」（richData 机制）")

    # ---------- 2. 浮动图片（drawings）----------
    float_imgs = []
    for sheet_file in [n for n in names if re.match(r"xl/worksheets/sheet\d+\.xml$", n)]:
        sheet_base = os.path.basename(sheet_file)
        rels_path = f"xl/worksheets/_rels/{sheet_base}.rels"
        if rels_path not in names:
            continue
        rels_root = ET.fromstring(z.read(rels_path))
        for rel in _find_all_local(rels_root, "Relationship"):
            if "drawing" not in (rel.get("Type") or ""):
                continue
            draw_path = resolve_rel_target(rels_path, rel.get("Target"))
            if draw_path not in names:
                continue
            draw_rels = f"xl/drawings/_rels/{os.path.basename(draw_path)}.rels"
            img_map = {}
            if draw_rels in names:
                dr = ET.fromstring(z.read(draw_rels))
                for r2 in _find_all_local(dr, "Relationship"):
                    img_map[r2.get("Id")] = resolve_rel_target(draw_rels, r2.get("Target"))
            droot = ET.fromstring(z.read(draw_path))
            for anchor in list(droot):
                tag = _local(anchor.tag)
                if tag not in ("oneCellAnchor", "twoCellAnchor", "absoluteAnchor"):
                    continue
                froms = _find_all_local(anchor, "from")
                cell = None
                if froms:
                    cols = _find_all_local(froms[0], "col")
                    rows = _find_all_local(froms[0], "row")
                    if cols and rows:
                        ci = int(cols[0].text)
                        ri = int(rows[0].text) + 1
                        col_letters = ""
                        ci2 = ci + 1
                        while ci2 > 0:
                            ci2, rem = divmod(ci2 - 1, 26)
                            col_letters = chr(65 + rem) + col_letters
                        cell = f"{col_letters}{ri}"
                blips = _find_all_local(anchor, "blip")
                if not blips:
                    continue
                embed = blips[0].get(f"{{{NS['r']}}}embed") or blips[0].get("embed")
                media = img_map.get(embed)
                if media and media in names:
                    float_imgs.append({
                        "sheet": os.path.basename(sheet_file).replace(".xml", ""),
                        "cell": cell, "col": _parse_ref(cell)[0],
                        "row": _parse_ref(cell)[1],
                        "media": media, "kind": "浮动图片",
                    })
    if float_imgs:
        notes.append(f"发现 {len(float_imgs)} 张「浮动图片」（drawings 机制）")
    results.extend(float_imgs)

    # ---------- 3. 导出图片文件 ----------
    for i, item in enumerate(results, start=1):
        ext = os.path.splitext(item["media"])[1] or ".png"
        fname = f"{item['cell'] or 'float' + str(i)}_{os.path.basename(item['media'])}"
        out_path = os.path.join(out_dir, fname)
        with open(out_path, "wb") as f:
            f.write(z.read(item["media"]))
        item["path"] = out_path

    all_media = all_media_parts(names)
    notes.append(f"压缩包内共 {len(all_media)} 个图片部件"
                 f"{'（位于 xl/richData/media/）' if any('richData/media' in m for m in all_media) else '（位于 xl/media/）'}")
    return results, notes


if __name__ == "__main__":
    xlsx = sys.argv[1] if len(sys.argv) > 1 else \
        r"C:\Users\fucker\Desktop\work\work0\referexcel\Todo.xlsx"
    out = sys.argv[2] if len(sys.argv) > 2 else \
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "output", "cell_images")
    items, notes = extract(xlsx, out)
    print("=" * 72)
    print(f"源文件: {xlsx}")
    print(f"输出目录: {out}")
    print("=" * 72)
    for n in notes:
        print(f"  · {n}")
    print()
    print("【单元格 → 图片 映射】")
    if not items:
        print("  （未发现任何图片）")
    for it in items:
        size = round(os.path.getsize(it["path"]) / 1024, 1)
        print(f"  {it['cell'] or '(未锚定)':>6}  {it['kind']:<12} "
              f"{os.path.basename(it['path']):<24} {size} KB")
    print()
    print("【openpyxl 视角对照（证明读不到单元格图片）】")
    try:
        import openpyxl
        wb = openpyxl.load_workbook(xlsx)
        ws = wb.active
        print(f"  工作表: {wb.sheetnames}")
        for r in range(1, 5):
            for c in range(1, 4):
                v = ws.cell(r, c).value
                if v is not None:
                    print(f"  {ws.cell(r, c).coordinate} = {v!r}")
        print(f"  ws._images 数量: {len(getattr(ws, '_images', []))} "
              f"（浮动图可读，单元格内图片读不到）")
    except Exception as e:
        print(f"  openpyxl 读取失败: {e}")
