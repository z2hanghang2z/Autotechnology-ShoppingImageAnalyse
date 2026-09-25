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
    required_extra_keywords,
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
banks = collect_bank_words(req, apply_quota=False)   # 全量，否则被限量的词统计不到
padding = req.get("padding_pool") or []

items, _ = extract(XLSX, os.path.join(ROOT, "output", "cell_images"))
cells = read_sheet_cells(XLSX, "xl/worksheets/sheet1.xml")

limit = int(sys.argv[1]) if len(sys.argv) > 1 else 4
rows = sorted({i["row"] for i in items})[:limit]
totals = defaultdict(float)
limit = int(sys.argv[1]) if len(sys.argv) > 1 else 4
rows = sorted({i["row"] for i in items})[:limit]

CATS = ["机型段", "核心元素", "必填词段", "特征词", "材质词",
        "机型附加词", "模型特征词", "运营词库", "补足词"]

totals = defaultdict(float)
positions = defaultdict(list)
n = 0

for row in rows:
    img = [i for i in items if i["row"] == row][0]["path"]
    a_col = cells.get(f"A{row}", "")
    d_col = cells.get(f"D{row}", "")          # ★ 机型列（旧版漏传，导致取不到机型）
    r = generate_one(cfg, confs, img, a_col, rotate=row - 1, models_text=d_col)
    seg, used = r["segments"] or {}, (r["used"] or [])
    core_els = r.get("core_elements") or []
    extra = required_extra_keywords((r.get("attrs") or {}).get("models") or [], req)
    title = r["title"] or ""
    total = W(title)
    if not total:
        print(f"第 {row} 行 生成失败: {r['issues']}")
        continue

    parts = {
        "机型段": W(seg.get("model_0")),
        "必填词段": W(seg.get("core_word")) + W(seg.get("new_word")) + W(seg.get("protect_word")),
        "特征词": W(seg.get("feature")),
        "材质词": W(seg.get("material")),
    }
    for w in used:
        if w in core_els:
            k = "核心元素"
        elif w in extra:
            k = "机型附加词"
        elif w in banks:
            k = "运营词库"
        elif w in padding:
            k = "补足词"
        else:
            k = "模型特征词"
        parts[k] = parts.get(k, 0) + W(w)

    # 位置：该类第一个词在标题里的字符位置 → 百分比
    probes = {
        "机型段": seg.get("model_0"),
        "核心元素": core_els[0] if core_els else "",
        "必填词段": seg.get("core_word"),
        "特征词": (seg.get("feature") or "")[:6],
        "材质词": seg.get("material"),
        "模型特征词": next((w for w in used if w not in core_els and w not in extra
                            and w not in banks and w not in padding), ""),
        "运营词库": next((w for w in used if w in banks), ""),
        "补足词": next((w for w in used if w in padding), ""),
    }
    print("=" * 78)
    print(f"第 {row} 行  总长 {total} 字符")
    print(f"  {title}")
    print("-" * 78)
    for k in CATS:
        v = parts.get(k, 0)
        if not v:
            continue
        p = probes.get(k) or ""
        pos = ""
        if p:
            idx = title.find(p)
            if idx >= 0:
                pct = idx / len(title) * 100
                positions[k].append(pct)
                pos = f"  起始 {pct:>4.0f}%"
        bar = "█" * max(1, round(v / 2))
        print(f"  {k:<7}{v:>3} 字符 {v / total * 100:>5.1f}%{pos}  {bar}")
        totals[k] += v
    n += 1

print("=" * 78)
print(f"平均占比（{n} 行）")
print("-" * 78)
grand = sum(totals.values())
for k in CATS:
    v = totals.get(k, 0)
    if not v:
        continue
    avg_pos = f"  平均位置 {sum(positions[k]) / len(positions[k]):>4.0f}%" if positions.get(k) else ""
    print(f"  {k:<7}{v / n:>5.1f} 字符 {v / grand * 100:>5.1f}%{avg_pos}")
print(f"  {'合计':<7}{grand / n:>5.1f} 字符")
