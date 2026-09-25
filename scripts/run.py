# -*- coding: utf-8 -*-
"""
============================================================
 电商商品名称自动生成 · 单一入口
============================================================

一条命令跑完整个流程：
    读 Excel → 提取单元格内商品主图 → 视觉模型识别 → 组装商品名称 → 写回原表

【表格约定】
    第一列   商品资料（材质 / 系列 / 适配机型等信息，支持文件路径或结构化文本）
    第二列   商品主图（Excel「放置在单元格中」的内嵌图片）
    第三列   生成结果写这里（即图片右侧单元格）

【用法】
    python run.py <表格路径>
    python run.py <表格路径> --dry-run        # 只生成不写文件
    python run.py <表格路径> --limit 5        # 只处理前 5 行
    python run.py --check                     # 只检查环境是否就绪

【规则与词库】全部在 config/*.yaml，改配置不用改代码
    config/model_config.yaml    模型 / 温度 / 并发 / thinking 开关 / 重试
    config/title_rules.yaml     长度口径 / 组装模板 / 差异化 / 校验开关
    config/required_words.yaml  必填词 / 材质库 / 补足词池 / 英文白名单
    config/forbidden_words.yaml 违禁词库（分类维护）

【安全】写回前自动备份；全程不破坏图片（zip 级定点写入）
"""
import argparse
import os
import sys
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from extract_cell_images import extract
from ollama_client import health_check, load_config
from title_builder import (
    check_pattern_models_separated,
    check_word_conflicts,
    collect_bank_words,
    count_length,
    load_all_configs,
    pairwise_similarity,
)
from title_generator import generate_one
from xlsx_safe_writer import (
    idx_to_col,
    list_sheets,
    col_to_idx,
    parse_ref,
    read_sheet_cells,
    write_cells,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 商品资料列（第一列）与图片列的默认值
DEFAULT_DATA_COL = "A"
DEFAULT_NAME_HEADERS = ["商品名称", "商品名", "标题", "宝贝标题"]


def pick_sheet(xlsx, want=None):
    sheets = list_sheets(xlsx)
    if not sheets:
        raise RuntimeError("文件里没有工作表")
    if want:
        for name, path in sheets:
            if name == want:
                return name, path
        print(f"  ⚠ 未找到工作表「{want}」，改用第一个：{sheets[0][0]}")
    return sheets[0]


def find_header(cells, candidates, max_scan_rows=8):
    """按表头文字定位列，返回 (列字母, 行号, 命中的表头)"""
    for row in range(1, max_scan_rows + 1):
        for ref, val in cells.items():
            col, r = parse_ref(ref)
            if r != row or not val:
                continue
            v = str(val).strip()
            for cand in candidates:
                if v == cand or cand in v:
                    return col, row, v
    return None, None, None


def resolve_col(arg, cells, header_candidates, default_col=None):
    """列定位：支持直接给列字母、给表头文字，或走默认"""
    if arg:
        a = str(arg).strip()
        if len(a) <= 3 and a.isalpha():
            return a.upper(), f"指定列 {a.upper()}"
        col, _, hit = find_header(cells, [a])
        if col:
            return col, f'表头「{hit}」'
        print(f"  ⚠ 未找到表头「{a}」，改用默认")
    col, _, hit = find_header(cells, header_candidates)
    if col:
        return col, f'表头「{hit}」'
    return default_col, "默认"


def do_check():
    """只检查环境"""
    cfg = load_config()
    confs = load_all_configs()
    print("=" * 70)
    print("环境自检")
    print("=" * 70)
    print(f"模型        : {cfg['model']['name']}")
    print(f"服务地址    : {cfg['model']['base_url']}")
    print(f"think 开关  : {cfg['inference'].get('think')}")
    print(f"温度        : {cfg['inference'].get('temperature')}")
    ok = health_check(cfg)
    print(f"服务连通性  : {'✅ 正常' if ok else '❌ 不可用'}")
    if not ok:
        print()
        print('  请先启动 Ollama：')
        print('  "C:\\Users\\fucker\\AppData\\Local\\Programs\\Ollama\\ollama.exe" serve')
    lcfg = confs["rules"].get("length") or {}
    print(f"目标长度    : {lcfg.get('target')} 字符（口径 {lcfg.get('count_mode')}）")
    req = confs["required"]
    print(f"必填词      : {'、'.join(req.get('must_include', []))}")
    print(f"材质库      : {len(req.get('material_group', []))} 个")
    print(f"补足词池    : {len(req.get('padding_pool', []))} 个")
    from title_builder import all_forbidden_words
    print(f"违禁词库    : {len(all_forbidden_words(confs['forbidden']))} 个")
    print("=" * 70)
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(
        description="电商商品名称自动生成（单一入口）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("xlsx", nargs="?", help="待上架商品 Excel 路径")
    ap.add_argument("--sheet", default=None, help="工作表名（默认第一个）")
    ap.add_argument("--data-col", default=DEFAULT_DATA_COL,
                    help=f"商品资料列（默认 {DEFAULT_DATA_COL}）")
    ap.add_argument("--model-col", default=None,
                    help="机型列（默认读 required_words.yaml 的 model_column，通常是 D）")
    ap.add_argument("--name-col", default=None,
                    help="商品名称列：列字母或表头文字（默认写在图片右侧单元格）")
    ap.add_argument("--out", default=None, help="输出路径（默认原地写入并自动备份）")
    ap.add_argument("--img-dir", default=None, help="提取图片的存放目录")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 行")
    ap.add_argument("--dry-run", action="store_true", help="只生成不写文件")
    ap.add_argument("--check", action="store_true", help="只检查环境是否就绪")
    args = ap.parse_args()

    if args.check:
        return do_check()
    if not args.xlsx:
        ap.print_help()
        print("\n错误：请提供待上架商品 Excel 路径（或加 --check 只做环境自检）")
        return 1

    xlsx = os.path.abspath(args.xlsx)
    out = os.path.abspath(args.out) if args.out else xlsx
    img_dir = args.img_dir or os.path.join(ROOT, "output", "cell_images")

    cfg = load_config()
    confs = load_all_configs()
    rules, required_cfg = confs["rules"], confs["required"]
    lcfg = rules.get("length") or {}
    target = int(lcfg.get("target", 100))
    mode = lcfg.get("count_mode", "char")
    mode_desc = "1汉字=2字符、1英文=1字符" if mode == "taobao" else "每字符=1"
    diff_cfg = rules.get("differentiation", {}) or {}

    print("=" * 74)
    print("电商商品名称自动生成")
    print("=" * 74)
    print(f"输入文件  : {xlsx}")
    print(f"模型      : {cfg['model']['name']}  "
          f"(think={cfg['inference'].get('think')}, temp={cfg['inference'].get('temperature')})")
    print(f"目标长度  : 恰好 {target} 字符（{mode}：{mode_desc}）")
    print(f"差异化    : {'开启' if diff_cfg.get('enabled') else '关闭'}")
    print(f"输出      : {'原地写入（自动备份）' if out == xlsx else out}")
    print("=" * 74)

    if not os.path.exists(xlsx):
        print(f"❌ 文件不存在: {xlsx}")
        return 1
    if not health_check(cfg):
        print("❌ Ollama 服务不可用，请先启动：")
        print('   "C:\\Users\\fucker\\AppData\\Local\\Programs\\Ollama\\ollama.exe" serve')
        return 1
    print("✓ 环境就绪")

    # 配置自检：必填词与违禁词不能互相冲突
    conflicts = check_word_conflicts(required_cfg, confs["forbidden"])
    if conflicts:
        print("\n⚠️  配置冲突（同一个词既要求出现、又被列为违禁）：")
        for rw, f in conflicts[:10]:
            print(f"     「{rw}」 与违禁词「{f}」冲突")
        print("   请到 config/ 下修正后再运行，否则这些行会一直判定不合格。")
        return 1

    # 配置自检：模板里机型槽位不能相邻（否则机型会连成一串）
    patterns = (rules.get("structure") or {}).get("patterns") or []
    bad_pat = check_pattern_models_separated(patterns)
    if bad_pat:
        print("\n⚠️  组装模板配置有误：以下模板的机型槽位直接相邻，会导致机型连成一串：")
        for i in bad_pat:
            print(f"     第 {i + 1} 个模板: {patterns[i]}")
        print("   请在相邻的机型槽位之间插入一个关键词槽位（如 keywords_a）。")
        return 1

    print("✓ 词库配置无冲突")
    print("✓ 组装模板机型间隔正常\n")

    # ---------- 1. 提取单元格内图片 ----------
    items, notes = extract(xlsx, img_dir)
    for n in notes:
        print(f"  · {n}")
    if not items:
        print("\n❌ 未在单元格中发现图片。")
        print("   仅支持 Excel「放置在单元格中」的内嵌图片或浮动图片；")
        print("   若图片是 =IMAGE() 链接公式，需联网获取，请改为本地图片。")
        return 1

    row_img = {}
    for it in items:
        if it.get("row") and it["row"] not in row_img:
            row_img[it["row"]] = it
    rows = sorted(row_img.keys())
    if args.limit:
        rows = rows[: args.limit]
    print(f"\n  共 {len(items)} 张图，本次处理 {len(rows)} 行: {rows}\n")

    # ---------- 2. 定位列 ----------
    sheet_name, sheet_path = pick_sheet(xlsx, args.sheet)
    cells = read_sheet_cells(xlsx, sheet_path)
    print(f"工作表    : {sheet_name}")

    img_col_counter = Counter(it.get("col") for it in items if it.get("col"))
    img_col = img_col_counter.most_common(1)[0][0] if img_col_counter else "B"
    default_name_col = idx_to_col(col_to_idx(img_col) + 1)

    ncol, nsrc = resolve_col(args.name_col, cells, DEFAULT_NAME_HEADERS,
                             default_col=default_name_col)
    mcol = (args.model_col or required_cfg.get("model_column") or "D").upper()
    print(f"商品资料列: {args.data_col.upper()} 列")
    print(f"机型列    : {mcol} 列")
    print(f"图片列    : {img_col} 列")
    print(f"名称写入列: {ncol} 列  ← {nsrc}"
          f"{'（图片右侧）' if ncol == default_name_col else ''}")

    if ncol in (img_col, mcol):
        print(f"\n❌ 名称列（{ncol}）与图片列/机型列冲突，拒绝写入。")
        return 1

    # ---------- 3. 逐行生成 ----------
    print("\n" + "=" * 74)
    print("开始生成")
    print("=" * 74)

    updates, results = {}, []
    skipped_rows = []          # 机型解析失败、被跳过的行
    used_padding = set()
    avoid_repeat = bool(diff_cfg.get("avoid_repeat_padding", True))
    do_rotate = bool(diff_cfg.get("rotate_keywords", True))
    # 差异化只跟踪「运营词库」用过的词（模型特征词是商品特有的，不参与去重）
    # apply_quota=False：这里要**全量**词库，否则被限量的词（如精准词）统计不到
    bank_set = set(collect_bank_words(required_cfg, apply_quota=False))
    t0 = time.time()

    for i, row in enumerate(rows, start=1):
        it = row_img[row]
        data_ref = f"{args.data_col.upper()}{row}"
        model_ref = f"{mcol}{row}"
        row_data = cells.get(data_ref) or ""
        models_text = cells.get(model_ref) or ""
        print(f"[{i}/{len(rows)}] 第 {row} 行 | 图片 {os.path.basename(it['path'])}")
        if row_data:
            shown = str(row_data)
            print(f"    资料 {data_ref} = {shown[:60]}{'...' if len(shown) > 60 else ''}")
        if models_text:
            print(f"    机型 {model_ref} = {models_text}")
        else:
            print(f"    机型 {model_ref} = （空，将从商品资料里解析）")

        res = generate_one(
            cfg, confs, it["path"], row_data,
            exclude_padding=used_padding if avoid_repeat else None,
            rotate=(i - 1) if do_rotate else 0,
            models_text=models_text,
        )
        # 差异化：只记录「运营词库」用掉的词，供后续商品避开
        used_padding |= {w for w in (res["used"] or []) if w in bank_set}

        attrs = res["attrs"]
        ms = attrs.get("models") or []
        print(f"    解析 → 机型 {('/'.join(ms)) if ms else '（无）'} | "
              f"材质 {attrs.get('material') or '（无）'} | "
              f"系列 {attrs.get('series') or '（无）'}")

        ref = f"{ncol}{row}"
        title = res["title"]

        # ★ 机型解析失败的行：不写 C 列，只报错。
        #   绝不用「适用于苹果iPhone」这类编造的机型占位。
        if not title:
            print(f"    → {ref} ⛔ 未生成（C 列不写入）")
            if res["issues"]:
                print(f"      ⚠ {'；'.join(res['issues'])}")
            results.append((row, ref, "", res["issues"]))
            skipped_rows.append((row, ref, res["issues"]))
            continue

        print(f"    → {ref} [{count_length(title, mode)}字符/{len(title)}字] "
              f"{res['elapsed']}s")
        print(f"      {title}")
        if res["issues"]:
            print(f"      ⚠ {'；'.join(res['issues'])}")
        else:
            print("      ✅ 校验通过")

        updates[ref] = title
        results.append((row, ref, title, res["issues"]))

    total = round(time.time() - t0, 2)

    # ---------- 4. 差异化报告 ----------
    titles = [r[2] for r in results if r[2]]
    if titles and diff_cfg.get("report_similarity", True) and len(titles) > 1:
        pairs = pairwise_similarity(titles)
        warn = float(diff_cfg.get("similarity_warn", 0.85))
        fail = float(diff_cfg.get("similarity_fail", 0.95))
        print("\n" + "-" * 74)
        print("【差异化报告】")
        print(f"  共 {len(titles)} 条 | 完全相同: "
              f"{sum(1 for _, _, s in pairs if s >= 1.0)} 对")
        high = [(a, b, s) for a, b, s in pairs if s >= warn]
        if high:
            print(f"  ⚠ 相似度 ≥ {warn} 的标题对:")
            for a, b, s in high[:8]:
                flag = "❌ 不合格" if s >= fail else "⚠ 建议关注"
                print(f"    行{results[a][0]} ↔ 行{results[b][0]}  {s:.3f}  {flag}")
        else:
            print(f"  ✅ 无相似度 ≥ {warn} 的标题对")
        print(f"  最高 {pairs[0][2]:.3f} | "
              f"平均 {round(sum(s for _, _, s in pairs) / len(pairs), 3)}")
        print("-" * 74)

    # ---------- 4.5 跳过行汇总 ----------
    if skipped_rows:
        print("\n" + "-" * 74)
        print(f"【已跳过 {len(skipped_rows)} 行：机型解析失败，C 列未写入】")
        for row, ref, iss in skipped_rows:
            print(f"  第 {row} 行（{ref}）：{'；'.join(iss)}")
        print("  → 请在该行机型列补充完整机型后重跑，不要留空。")
        print("-" * 74)

    # ---------- 5. 安全写回 ----------
    print("\n" + "=" * 74)
    if args.dry_run:
        print("[dry-run] 未写入文件")
    else:
        count, wnotes = write_cells(xlsx, out, {sheet_name: updates})
        for n in wnotes:
            print(f"  {n}")
        print(f"  已写入 {count} 个单元格 → {out}")

    ok = sum(1 for r in results if not r[3])
    print(f"\n完成 {ok}/{len(results)} 全部校验通过 | 总耗时 {total}s")
    if skipped_rows:
        print(f"⚠ 有 {len(skipped_rows)} 行因机型解析失败被跳过，C 列未写入")
    print("=" * 74)
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
