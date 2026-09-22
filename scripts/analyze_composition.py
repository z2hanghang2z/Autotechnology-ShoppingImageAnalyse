# -*- coding: utf-8 -*-
"""商品名称组装比例分析工具

用途：看 100 字符的标题里，各组成部分实际占了多少、比例是否合理。
     调整配置（词库优先级、模板）后可以用它复核效果。

用法：
    python analyze_composition.py [分析行数，默认 4]
"""
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extract_cell_images import extract
from ollama_client import load_config
from title_builder import (
    collect_bank_words, count_length, load_all_configs,
)
from title_generator import generate_one
from xlsx_safe_writer import read_sheet_cells

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
XLSX = os.path.join(ROOT, "referexcel", "work.xlsx")
MODE = "taobao"
W = lambda s: count_length(s or "", MODE)

cfg = load_config()
confs = load_all_configs()
req, rules = confs["required"], confs["rules"]
banks = collect_bank_words(req)
padding = req.get("padding_pool") or []

items, _ = extract(XLSX, os.path.join(ROOT, "output", "cell_images"))
cells = read_sheet_cells(XLSX, "xl/worksheets/sheet1.xml")

limit = int(sys.argv[1]) if len(sys.argv) > 1 else 4
rows = sorted({i["row"] for i in items})[:limit]
totals = defaultdict(float)
n = 0

for row in rows:
    img = [i for i in items if i["row"] == row][0]["path"]
    data = cells.get(f"A{row}", "")
    r = generate_one(cfg, confs, img, data, rotate=row - 1)
    seg = r["segments"]
    used = r["used"] or []

    parts = {
        "机型段": W(seg.get("model_prefix")),
        "核心词段": W(seg.get("core_suffix")),
        "特征词": W(seg.get("feature")),
        "材质词": W(seg.get("material")),
    }
    for w in used:
        if w in banks:
            parts["运营词库"] = parts.get("运营词库", 0) + W(w)
        elif w in padding:
            parts["补足词"] = parts.get("补足词", 0) + W(w)
        else:
            parts["模型特征词"] = parts.get("模型特征词", 0) + W(w)

    total = W(r["title"])
    print("=" * 74)
    print(f"第 {row} 行  总长 {total} 字符")
    print(f"  {r['title']}")
    print("-" * 74)
    for k in ["机型段", "核心词段", "特征词", "材质词", "模型特征词", "运营词库", "补足词"]:
        v = parts.get(k, 0)
        if v:
            bar = "█" * max(1, round(v / 2))
            print(f"  {k:<8}{v:>3} 字符  {v/total*100:>5.1f}%  {bar}")
            totals[k] += v
    n += 1
    print()

print("=" * 74)
print(f"平均占比（{n} 行）")
print("-" * 74)
grand = sum(totals.values())
for k in ["机型段", "核心词段", "特征词", "材质词", "模型特征词", "运营词库", "补足词"]:
    v = totals.get(k, 0)
    if v:
        print(f"  {k:<8}{v/n:>5.1f} 字符  {v/grand*100:>5.1f}%")
print(f"  {'合计':<8}{grand/n:>5.1f} 字符")
