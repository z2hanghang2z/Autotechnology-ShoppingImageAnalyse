# -*- coding: utf-8 -*-
"""
商品名称生成模块

职责：
    1. 读取「第一列」的商品资料（材质 / 系列 / 适配机型）
    2. 让视觉模型看「第二列」的主图，输出特征关键词池
    3. 用 title_builder 做确定性组装，得到精确长度的商品名称
    4. 校验必填词 / 违禁词 / 机型唯一性

分工原则（重要）：
    模型只负责"看图能看出来的"——颜色、图案、风格、图上文字；
    材质 / 系列 / 机型等硬参数一律从商品资料读取，绝不交给模型猜。
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ollama_client import chat_with_image
from title_builder import (
    all_forbidden_words,
    assemble_exact,
    build_segments,
    count_length,
    extract_attrs,
    normalize_model,
    pick_material,
    strip_model_keywords,
    validate_title,
)

SYSTEM_PROMPT = (
    "你是淘宝手机壳类目的资深运营，擅长挖掘高搜索量的商品关键词。"
    "你只依据图片中真实可见的内容输出，绝不编造。"
    "你严格按照要求的 JSON 格式输出，不添加任何多余文字。"
)

PROMPT_TEMPLATE = """请仔细观察这张手机壳图片，输出 JSON 格式的分析结果。

【输出结构】
{{
  "observed": {{
    "颜色": "图中真实可见的颜色",
    "图案": "图中真实可见的图案元素",
    "风格": "整体风格（如可爱/简约/复古/潮酷等）"
  }},
  "keywords": ["关键词1", "关键词2", "..."]
}}

【keywords 硬性要求】
1. 提供 18-24 个适合淘宝手机壳标题的关键词或短语
2. 每个关键词 2 到 6 个字符
3. 覆盖这些维度：工艺、功能、风格、适用人群、使用场景
4. 必须包含图中真实可见的特征（颜色、图案元素）
5. 【严禁】包含材质词，材质由商品资料提供，不要写
6. 【严禁】包含任何机型信息（iPhone、苹果、三星、华为、小米、折叠屏等），机型由商品资料提供
7. 可以包含通用风格词（如 ins风、Q版、3D立体），但【严禁】自创任何英文品牌名、
   英文单词串或看起来像品牌名的字母组合（例如 timeLUCKYEH 这类无意义字母串）
8. 【严禁】堆砌同前缀词。例如不要同时给出"撞色款""撞色设计""撞色风格"，
   也不要给出只换尾字的词，只保留最有代表性的一个
9. 【严禁】包含违禁词：最、第一、顶级、唯一、100%、万能、永久、官方、正品、授权、正版
10. 【严禁】包含图片上的水印文字（如 wf、wj）
11. 【严禁】包含其他类目词（钢化膜、充电器、数据线、耳机、支架）
12. 关键词之间语义不要重复，每个词都应是独立的信息点

只输出 JSON，不要任何解释文字，不要 markdown 代码块。"""


# ---------------------------------------------------------------- 解析

def extract_json(text):
    """从模型输出中稳健地提取 JSON"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    start, end = t.find("{"), t.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(t[start:end + 1])
        except Exception:
            return None
    return None


def _has_bad_ascii(kw, ascii_cfg):
    """检测模型自创的无意义英文串（如 timeLUCKYEH 假品牌名）"""
    if not ascii_cfg or not ascii_cfg.get("enabled"):
        return False
    max_run = int(ascii_cfg.get("max_ascii_run", 3))
    wl = [str(w).lower() for w in (ascii_cfg.get("whitelist") or [])]
    for m in re.finditer(r"[A-Za-z]+", kw):
        run = m.group(0)
        if len(run) <= max_run:
            continue
        low = run.lower()
        if any(w and (w in low or low in w) for w in wl):
            continue
        return True
    return False


def filter_keywords(keywords, forbidden_words, rules, prefix_text="",
                    reserved_words=None, ascii_cfg=None):
    """
    过滤模型给出的关键词：
        去违禁词 / 去机型词 / 去材质词 / 去自创英文串 / 去近义堆砌 /
        去含标点符号的（用户要求商品名称不能有标点）/ 去空白
    """
    from title_builder import is_near_duplicate, find_punctuation

    out, seen = [], []
    reserved = [w for w in (reserved_words or []) if w]

    for kw in keywords or []:
        kw = str(kw).strip()
        kw = re.sub(r"[\s,，、;；/|]+", "", kw)
        if not kw or kw in seen:
            continue
        if find_punctuation(kw):          # 含标点 → 丢弃
            continue
        if any(fw and fw in kw for fw in forbidden_words):
            continue
        if strip_model_keywords(kw, rules) != kw:
            continue
        if any(rw in kw for rw in reserved):
            continue
        if prefix_text and kw in prefix_text:
            continue
        if _has_bad_ascii(kw, ascii_cfg):
            continue
        if is_near_duplicate(kw, seen):
            continue
        if not (2 <= len(kw) <= 8):
            continue
        seen.append(kw)
        out.append(kw)
    return out


# ---------------------------------------------------------------- 生成

def generate_one(cfg, confs, image_path, row_data, exclude_padding=None, rotate=0):
    """
    为单个商品生成名称。

    image_path —— 商品主图路径（第二列）
    row_data   —— 商品资料原文（第一列），内含材质 / 系列 / 适配机型
    exclude_padding —— 本批次已被其他商品用过的补足词（差异化）
    rotate     —— 轮换偏移（行号），用于组装模板与关键词顺序

    返回 dict：
        {title, issues, elapsed, tokens, attempts, used, attrs}
    """
    rules = confs["rules"]
    required_cfg = confs["required"]
    forbidden_cfg = confs["forbidden"]

    lcfg = rules.get("length") or {}
    target = int(lcfg.get("target", 100))
    count_mode = lcfg.get("count_mode", "char")
    padding_pool = required_cfg.get("padding_pool", []) or []
    materials = required_cfg.get("material_group", []) or []
    fwords = all_forbidden_words(forbidden_cfg)
    ascii_cfg = required_cfg.get("ascii_filter", {}) or {}
    parity_filler = required_cfg.get("parity_filler", " ")
    max_attempts = int((rules.get("retry") or {}).get("max_attempts", 3))
    stcfg = rules.get("structure", {}) or {}
    patterns = stcfg.get("patterns") or []

    # ---------- 1. 从商品资料里取硬参数（材质 / 系列 / 机型）----------
    attrs = extract_attrs(row_data, materials)
    model_name = normalize_model(attrs.get("model") or "")

    # 材质：优先用商品资料里的，取不到才让模型看图判断
    material = attrs.get("material") or ""

    total_elapsed, total_tokens = 0.0, 0
    last_title, last_issues, last_used = "", [], []
    feedback = ""

    for attempt in range(1, max_attempts + 1):
        prompt = PROMPT_TEMPLATE
        if feedback:
            prompt += f"\n\n【上次的问题，请修正】\n{feedback}"

        r = chat_with_image(cfg, image_path, prompt, system_prompt=SYSTEM_PROMPT)
        total_elapsed = round(total_elapsed + r["elapsed"], 2)
        if not r["ok"]:
            last_issues = [f"模型调用失败: {r['error']}"]
            feedback = "上次调用失败，请重新输出 JSON。"
            continue
        total_tokens += r["tokens"]

        data = extract_json(r["content"])
        if not data:
            last_issues = ["模型未返回合法 JSON"]
            feedback = "上次输出不是合法 JSON，请只输出 JSON 对象。"
            continue

        raw_keywords = data.get("keywords", []) or []
        # 商品资料没给材质时，才用模型判断的结果
        if not material:
            material = pick_material(data.get("material", ""),
                                     " ".join(raw_keywords), required_cfg)

        segments = build_segments(model_name, material, required_cfg)
        prefix_text = "".join(segments.values())
        # 必填词 + 材质词 + 机型（含多机型的每一段）都不得在关键词里重复出现
        reserved = list(required_cfg.get("must_include", []) or []) + materials
        reserved += [p for p in model_name.split("/") if p]
        reserved.append(model_name)
        kws = filter_keywords(raw_keywords, fwords, rules,
                              prefix_text=prefix_text, reserved_words=reserved,
                              ascii_cfg=ascii_cfg)

        # 组装模板按行轮换
        if patterns and stcfg.get("rotate_patterns", True):
            pattern = patterns[rotate % len(patterns)]
        elif patterns:
            pattern = patterns[0]
        else:
            pattern = stcfg.get("default_pattern")

        title, exact, used = assemble_exact(
            segments, kws, padding_pool, target,
            count_mode=count_mode, exclude_words=exclude_padding,
            rotate=rotate, pattern=pattern,
        )

        # 奇偶兜底：仍差 1 个字符时补一个不可见字符
        if not exact:
            gap = target - count_length(title, count_mode)
            if gap == 1 and parity_filler:
                title = title + parity_filler
                exact = count_length(title, count_mode) == target

        issues = validate_title(title, model_name, rules, required_cfg, forbidden_cfg)
        if not exact:
            issues.append("无法精确凑满目标长度")

        last_title, last_issues, last_used = title, issues, used
        if not issues:
            break
        feedback = "；".join(issues)

    return {
        "title": last_title,
        "issues": last_issues,
        "elapsed": total_elapsed,
        "tokens": total_tokens,
        "attempts": attempt,
        "used": last_used,
        "attrs": {**attrs, "model": model_name, "material": material},
    }
