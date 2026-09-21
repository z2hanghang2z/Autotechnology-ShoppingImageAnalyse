# -*- coding: utf-8 -*-
"""
标题组装与校验模块（可复用）

设计原则：
    1. 长度由「确定性组装」保证，不依赖模型数数（模型数不准长度）
    2. 支持两种计数口径：
         char   —— 每个字符都算 1（Python len）
         taobao —— 1 个汉字算 2 字符，其余（英文字母/数字/符号）算 1
    3. 所有规则来自 config/*.yaml，改规则不用改代码
    4. 必填词集中在标题最前面，后续截断不会误伤必填词
    5. 差异化：补足词跨商品不重复 + 关键词顺序按行轮换

主要函数：
    load_all_configs()          加载全部配置
    count_length(text, mode)    按口径计算长度
    normalize_model(spec)       机型规范化
    extract_model_from_path(p)  从路径提取机型
    build_base_title(...)       组装标题主体
    assemble_exact(...)         精确组装到目标长度
    title_similarity(a, b)      两条标题的相似度
    validate_title(...)         校验并返回问题清单
"""
import os
import re

try:
    import yaml
except ImportError:
    yaml = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(ROOT, "config")


def _load_yaml(name, default=None):
    path = os.path.join(CONFIG_DIR, name)
    if yaml and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or default or {}
    return default or {}


def load_all_configs():
    """加载标题相关的全部配置"""
    return {
        "rules": _load_yaml("title_rules.yaml"),
        "required": _load_yaml("required_words.yaml"),
        "forbidden": _load_yaml("forbidden_words.yaml"),
    }


def all_forbidden_words(forbidden_cfg):
    """把所有类别的违禁词拍平成一个列表"""
    words = []
    for category, items in (forbidden_cfg or {}).items():
        if isinstance(items, list):
            words.extend([str(x) for x in items])
    return words


# ---------------------------------------------------------------- 长度计算

def count_length(text, count_mode="char"):
    """
    按指定口径计算长度。
        char   —— 每个字符算 1
        taobao —— 汉字算 2，其他算 1（用户要求：1汉字=2字符，1英文=1字符）
    """
    if count_mode == "taobao":
        n = 0
        for ch in text:
            n += 2 if "\u4e00" <= ch <= "\u9fff" else 1
        return n
    return len(text)


def trim_to_weight(text, max_weight, count_mode="char"):
    """从尾部裁剪，直到长度不超过 max_weight"""
    out = text
    while out and count_length(out, count_mode) > max_weight:
        out = out[:-1]
    return out


# ---------------------------------------------------------------- 机型处理

def extract_attrs(row_data, material_group=None):
    """
    从「商品资料」（表格第一列）里提取硬参数：材质 / 系列 / 适配机型。

    资料可能是文件路径，也可能是结构化文本（如「液态素皮 云花系列 适配iPhone16」），
    两种都能解析。

    返回 {"series": ..., "material": ..., "model": ...}
    """
    raw = str(row_data or "").strip()
    if not raw:
        return {"series": "", "material": "", "model": ""}

    parts = [p.strip() for p in re.split(r"[\\/|,，;；、\t]+", raw) if p.strip()]

    # ---- 系列：找带「系列」字样的段 ----
    series = ""
    for p in parts:
        if "系列" in p:
            m = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{2,10}系列)", p)
            if m:
                series = m.group(1)
                break

    # ---- 材质：在全文里匹配材质库，取最长命中 ----
    material = ""
    mats = sorted([m for m in (material_group or []) if m], key=len, reverse=True)
    for m in mats:
        if m in raw:
            material = m
            break

    # ---- 机型 ----
    model = ""
    if "\\" in raw or "/" in raw:
        model = extract_model_from_path(raw)
    if not model:
        # 结构化文本：先看「适配机型 / 机型 / 型号」标签，再退回找 iphone/苹果 开头的段
        m = re.search(r"(?:适配机型|机型|型号|规格)\s*[:：]?\s*([A-Za-z\u4e00-\u9fff0-9 ]{2,24})", raw)
        if m:
            model = m.group(1).strip()
        else:
            for p in parts:
                if re.match(r"(?i)^(iphone|苹果)", p):
                    model = p
                    break

    return {"series": series, "material": material, "model": model}


_MODEL_TAIL_WORDS = ["支点", "手机壳", "保护套", "壳", "主图", "详情图", "款", "系列"]

# 机型 token：两位数字 + 可选后缀（18u / 17promax / 16pro / 15 / 14plus ...）
_MODEL_TOKEN_RE = re.compile(
    r"(\d{2})\s*(promax|pro|plus|mini|max|air|ultra|duo|se|u|e)?", re.I
)


def _clean_model_token(digits, suffix):
    """把「18」「u」拼成 iPhone18U"""
    s = digits
    if suffix:
        sfx = suffix.lower()
        # 复合后缀特殊处理
        cap = {"promax": "ProMax", "promini": "ProMini"}.get(sfx)
        if not cap:
            cap = sfx[0].upper() + sfx[1:]
        s += cap
    return "iPhone" + s


def _has_model_token(s):
    """这一段里是否含机型特征（数字+后缀，或 iphone/苹果 前缀）"""
    if not s:
        return False
    if re.match(r"(?i)^(iphone|苹果)", s.strip()):
        return True
    return bool(_MODEL_TOKEN_RE.search(s))


def _looks_like_path(s):
    """
    判断是不是文件路径。
    难点：斜杠既可能是路径分隔符，也可能是机型分隔符（如「15/16promax」）。
    判据：若按斜杠切开后**每一段都能解析出机型**，则认为是机型列表，不是路径。
    """
    if not s:
        return False
    if "\\" in s:                      # Windows 路径
        return True
    if re.match(r"^[A-Za-z]:", s):     # 盘符
        return True
    if "/" not in s:
        return False
    segs = [p.strip() for p in s.split("/") if p.strip()]
    if len(segs) >= 2 and all(_has_model_token(p) for p in segs):
        return False                   # 全是机型 → 机型列表
    return s.count("/") >= 2


def split_models(spec):
    """
    把机型文本拆成**多个机型**。

    用户约定：路径文本中出现的疑似机型都算适配机型，都要进商品名称。
    例：
        '苹果18u17promax'   -> ['iPhone18U', 'iPhone17ProMax']
        '苹果15/16promax'   -> ['iPhone15', 'iPhone16ProMax']
        'iPhone Duo'        -> ['iPhoneDuo']
        'iphone17pro支点'    -> ['iPhone17Pro']
    """
    if not spec:
        return []
    s = str(spec).strip()
    if not s:
        return []
    # 若是路径，先取出机型段
    if _looks_like_path(s):
        s = extract_model_from_path(s)
        if not s:
            return []

    # 去掉「苹果 / iPhone」前缀与常见尾部修饰词
    body = re.sub(r"^(苹果|iPhone|iphone|IPHONE)", "", s)
    changed = True
    while changed:
        changed = False
        for w in _MODEL_TAIL_WORDS:
            if body.endswith(w) and len(body) > len(w):
                body = body[: -len(w)]
                changed = True

    models, seen = [], set()
    for m in _MODEL_TOKEN_RE.finditer(body):
        model = _clean_model_token(m.group(1), m.group(2))
        if model not in seen:
            seen.add(model)
            models.append(model)

    # 没匹配到数字机型（如 iPhoneDuo 这类无数字写法），退化为整段处理；
    # 但必须看起来像机型才认，避免把目录名（如 images）当成机型
    if not models and body:
        s2 = re.sub(r"[\s/]+", "", body)
        plausible = (
            s2
            and s2.isascii()
            and (len(s2) <= 4 or re.fullmatch(r"(?i)(duo|air|ultra|se|mini|max|plus|fold)", s2))
        )
        if plausible:
            s2 = re.sub(r"(?i)(promax|pro|plus|mini|max|air|ultra|duo|u)",
                        lambda mo: mo.group(0)[0].upper() + mo.group(0)[1:].lower(), s2)
            s2 = s2.replace("Promax", "ProMax").replace("Promini", "ProMini")
            models.append("iPhone" + s2)
    return models


def format_model_group(models):
    """
    把多个机型合成标题里的一段。

    ⚠️ 用户要求：商品名称中**不能出现标点符号**（含 `/`），
       所以多机型直接连写，不加任何分隔符：
           ['iPhone18U', 'iPhone17ProMax'] -> 'iPhone18U17ProMax'

    只保留第一个 iPhone 前缀，避免「iPhone」重复出现。
    """
    models = [m for m in (models or []) if m]
    if not models:
        return ""
    if len(models) == 1:
        return models[0]
    out = [models[0]]
    for m in models[1:]:
        out.append(re.sub(r"^iPhone", "", m))
    return "".join(out)


def normalize_model(spec):
    """
    机型规范化（返回可用于标题的整段文本，支持多机型）。

        iphone16          -> iPhone16
        iPhone 15 Pro     -> iPhone15Pro
        苹果18u17promax    -> iPhone18U/17ProMax      ← 多机型
        iPhone Duo        -> iPhoneDuo
    """
    models = split_models(spec)
    return format_model_group(models)


def extract_model_from_path(path):
    """
    从商品图片的文件夹路径里提取机型。
    剔除「盘符/序号前缀/日期前缀/系列编号/图片名」等段落，取最后剩下的那一段。
    """
    if not path:
        return ""
    parts = re.split(r"[\\/]+", str(path))
    keep = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if re.fullmatch(r"[A-Za-z]:", p):
            continue
        if re.match(r"^\d{1,8}\s*[-_]", p):
            continue
        if re.match(r"^[A-Za-z]{1,3}\d{1,3}\s*[-_]", p):
            continue
        if re.search(r"(主图|详情图|SKU图|sku图|白底图)", p):
            continue
        keep.append(p)
    return keep[-1] if keep else ""


def strip_model_keywords(text, rules):
    """从文本中剥离所有机型关键词，保证机型在标题中只出现一次"""
    if not text:
        return ""
    patterns = (rules.get("model_keyword", {}) or {}).get("detect_patterns", [])
    out = text
    for p in patterns:
        try:
            out = re.sub(p, "", out)
        except re.error:
            continue
    out = re.sub(r"[\s,，、;；/|]+", "", out)
    return out.strip()


# ---------------------------------------------------------------- 组装

def pick_material(model_material, keyword_text, required_cfg):
    """确定材质词：优先模型选定 → 否则从关键词文本匹配 → 都没有返回空"""
    materials = required_cfg.get("material_group", []) or []
    if model_material:
        mm = str(model_material).strip()
        for m in materials:
            if m.lower() in mm.lower() or mm.lower() in m.lower():
                return m
    text = (keyword_text or "").lower()
    for m in materials:
        if m.lower() in text:
            return m
    return ""


def build_segments(model_name, material, required_cfg):
    """
    构造固定段（承载全部必填词），返回 {槽位名: 文本}。

    槽位说明：
        model_prefix —— 适用苹果{机型}（提供「苹果」「iPhone」「机型」）
        core_suffix  —— 新款手机壳防摔（提供「新款」「手机壳」「防摔」）
        material     —— 材质词（可能为空）
    """
    seg = {}
    tpl = required_cfg.get("model_template", "适用苹果{model}")
    seg["model_prefix"] = tpl.format(model=model_name) if model_name else "适用苹果iPhone"
    seg["core_suffix"] = required_cfg.get("core_template", "新款手机壳防摔")
    seg["material"] = material or ""
    return seg


def build_base_title(model_name, material, keywords, required_cfg):
    """兼容旧接口：按默认顺序拼出标题主体"""
    seg = build_segments(model_name, material, required_cfg)
    parts = [seg["model_prefix"], seg["core_suffix"]]
    if seg["material"]:
        parts.append(seg["material"])
    seen = set()
    for kw in keywords or []:
        kw = (kw or "").strip()
        if not kw or kw in seen:
            continue
        seen.add(kw)
        parts.append(kw)
    return "".join(parts)


def join_text(a, b):
    """
    拼接两段文本。用户要求商品名称中不能出现标点符号，
    所以**不加任何分隔符**，直接连写。
    """
    if not a:
        return b or ""
    if not b:
        return a
    return a + b


def can_join(a, b):
    """
    判断两段能不能直接相连（不加分隔符）。

    若边界两侧都是英文字母/数字，连写会变成「iPhone17Proins风」这种粘连，
    搜索引擎会切错词。既然不能用分隔符，就改为**跳过**这类关键词。
    """
    if not a or not b:
        return True
    return not (a[-1].isascii() and a[-1].isalnum()
                and b[0].isascii() and b[0].isalnum())


# 兼容旧调用名
def smart_join(a, b):
    return join_text(a, b)


# 商品名称中禁止出现的标点符号（用户要求：名称里不能有标点，/ 也不行）
# 注意：不含空白字符——空格只作为「奇偶补齐」的隐形兜底，见 required_words.yaml
PUNCT_RE = re.compile(
    r"[，。、；：？！“”‘’（）【】《》〈〉「」『』…—～·"
    r",.;:!?\"'()\[\]{}<>/\\|@#$%^&*_+=~`\-]"
)


def find_punctuation(text):
    """返回文本里出现的标点符号列表（用于校验）"""
    return sorted(set(PUNCT_RE.findall(text or "")))


def _resolve_pattern(pattern, segments, keywords):
    """
    把模板展开成有序的「段」列表：
        [("text", 文本), ("kwlist", [关键词...]), ...]
    支持 keywords / keywords_a / keywords_b 三种关键词槽位。
    """
    kws = list(keywords or [])
    kw_slots = {}
    if "keywords_a" in pattern or "keywords_b" in pattern:
        mid = (len(kws) + 1) // 2
        kw_slots["keywords_a"] = kws[:mid]
        kw_slots["keywords_b"] = kws[mid:]
    if "keywords" in pattern:
        kw_slots["keywords"] = kws

    out = []
    for slot in pattern:
        if slot in kw_slots:
            out.append(("kwlist", kw_slots[slot]))
        elif slot == "padding":
            out.append(("padding", None))
        else:
            txt = segments.get(slot, "")
            if txt:
                out.append(("text", txt))
    return out


def assemble_exact(segments, keywords, padding_pool, target,
                   count_mode="char", exclude_words=None, rotate=0, pattern=None):
    """
    按模板精确组装到 target 长度（按 count_mode 口径），且绝不切断词语。

    segments —— 固定段字典（见 build_segments）
    pattern  —— 槽位顺序列表；None 时用默认顺序

    步骤：
        1. 按模板顺序铺固定段，关键词插到对应的关键词槽（放不下就跳过）
        2. 剩余缺口用补足词池做子集和，找出正好填满的组合
        3. 无法精确命中时，从实际用上的关键词末尾逐步去掉再重试（调整长度奇偶性）
        4. 仍不行则取最接近的组合

    返回 (标题, 是否精确命中, 使用的词列表)
    """
    W = lambda s: count_length(s, count_mode)
    exclude = set(exclude_words or {})

    if not pattern:
        pattern = ["model_prefix", "core_suffix", "material", "keywords", "padding"]

    kws = list(keywords or [])
    if rotate and kws:
        r = rotate % len(kws)
        kws = kws[r:] + kws[:r]

    # 按模板铺开（关键词槽内做贪心筛选）
    layout = _resolve_pattern(pattern, segments, kws)

    # ★ 关键：固定段（机型/核心词/材质）承载必填词，必须优先保证放得下。
    #   先把所有固定段的总长度算出来，加关键词时给它留够余量，
    #   否则模板把固定段放在后面时，前面的关键词会吃光预算导致必填词被截断。
    total_text_w = sum(W(v) for k, v in layout if k == "text")

    base = ""
    used = []
    text_used = 0
    for kind, val in layout:
        if kind == "text":
            base = join_text(base, val)
            text_used += W(val)
        elif kind == "kwlist":
            remaining_text = total_text_w - text_used   # 后面还没放的固定段长度
            for kw in val:
                if not kw or kw in used:
                    continue
                # 会造成英文粘连的词直接跳过（不能用分隔符，只能不选它）
                if not can_join(base, kw):
                    continue
                cand = join_text(base, kw)
                if W(cand) + remaining_text <= target:
                    base = cand
                    used.append(kw)

    def _fill(trial_words):
        """给定「本次采用的关键词」，按模板重建标题并尝试补满缺口"""
        tw = set(trial_words)
        t = ""
        for kind, val in layout:
            if kind == "text":
                t = join_text(t, val)
            elif kind == "kwlist":
                for kw in val:
                    if kw in tw and can_join(t, kw):
                        t = join_text(t, kw)
        gap = target - W(t)
        if gap < 0:
            out = trim_to_weight(t, target, count_mode)
            return out, W(out) == target, list(trial_words)
        if gap == 0:
            return t, True, list(trial_words)

        # 补足词池：排除已用与近义重复；跨商品用过的排后面（差异化）
        cand = []
        for w in (padding_pool or []):
            if not w or w in trial_words:
                continue
            if W(w) > gap:
                continue
            if not can_join(t, w):          # 会造成英文粘连 → 不用
                continue
            if is_near_duplicate(w, trial_words) or is_near_duplicate(w, cand):
                continue
            cand.append(w)
        preferred = [w for w in cand if w not in exclude]
        fallback = [w for w in cand if w in exclude]
        cand = preferred + fallback
        if rotate and cand:
            r = rotate % len(cand)
            cand = cand[r:] + cand[:r]

        dp = {0: []}
        for w in cand:
            L = W(w)
            for s in sorted(dp.keys(), reverse=True):
                ns = s + L
                if ns <= gap and ns not in dp:
                    dp[ns] = dp[s] + [w]
            if gap in dp:
                break
        if gap in dp:
            fill = dp[gap]
            out = t
            for w in fill:
                out = join_text(out, w)
            return out, True, list(trial_words) + fill

        best = max(dp.keys()) if dp else 0
        tail = dp.get(best, [])
        t2 = t
        for w in tail:
            t2 = join_text(t2, w)
        need = target - W(t2)
        if need > 0:
            for w in cand:
                if w in trial_words or w in tail:
                    continue
                piece = trim_to_weight(w, need, count_mode)
                if piece and len(piece) >= 2 and W(piece) == need and can_join(t2, piece):
                    return join_text(t2, piece), True, list(trial_words) + tail
        return t2, W(t2) == target, list(trial_words) + tail

    best_result = None
    for drop in range(0, min(len(used), 6) + 1):
        trial = used[: len(used) - drop] if drop else used
        title, exact, final_used = _fill(trial)
        if exact:
            return title, True, final_used
        if best_result is None or W(title) > W(best_result[0]):
            best_result = (title, exact, final_used)

    if best_result:
        return best_result
    return "".join(v for k, v in layout if k == "text"), False, []


def _bigrams(s):
    s = s or ""
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def is_similar(a, b, threshold=0.5):
    """2 字组合重叠度，判断两词是否语义重复"""
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return False
    return len(ba & bb) / min(len(ba), len(bb)) >= threshold


def is_near_duplicate(kw, existing, min_prefix=2):
    """判断关键词是否与已有词重复（共享前缀 / 互相包含 / 2字组合重叠）"""
    for e in existing:
        if not e or not kw:
            continue
        if kw in e or e in kw:
            return True
        n = min(len(kw), len(e), min_prefix)
        if n >= min_prefix and kw[:n] == e[:n]:
            return True
        if is_similar(kw, e):
            return True
    return False


# ---------------------------------------------------------------- 差异化

def title_similarity(a, b):
    """两条标题的相似度（0~1），用于差异化检测"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # 用最长公共子串占比近似（避免引入额外依赖）
    from difflib import SequenceMatcher
    return round(SequenceMatcher(None, a, b).ratio(), 4)


def pairwise_similarity(titles):
    """返回 [(行号A, 行号B, 相似度), ...]，按相似度降序"""
    pairs = []
    for i in range(len(titles)):
        for j in range(i + 1, len(titles)):
            pairs.append((i, j, title_similarity(titles[i], titles[j])))
    pairs.sort(key=lambda x: -x[2])
    return pairs


# ---------------------------------------------------------------- 校验

def validate_title(title, model_name, rules, required_cfg, forbidden_cfg):
    """校验标题，返回问题列表（空列表 = 全部通过）"""
    issues = []
    vcfg = rules.get("validate", {}) or {}
    lcfg = rules.get("length", {}) or {}
    mode = lcfg.get("count_mode", "char")

    if vcfg.get("check_length", True):
        target = int(lcfg.get("target", 100))
        actual = count_length(title, mode)
        if lcfg.get("exact", True):
            if actual != target:
                issues.append(f"长度不符: 实际 {actual}，要求 {target}（口径 {mode}）")
        else:
            tol = int(lcfg.get("tolerance", 0))
            if abs(actual - target) > tol:
                issues.append(f"长度超差: 实际 {actual}，目标 {target}±{tol}")

    if vcfg.get("check_required", True):
        for w in required_cfg.get("must_include", []) or []:
            if w not in title:
                issues.append(f"缺少必填词「{w}」")
        materials = required_cfg.get("material_group", []) or []
        if materials and not any(m in title for m in materials):
            issues.append(f"缺少材质词（需含其一：{'/'.join(materials[:5])}...）")

    if vcfg.get("check_punctuation", True):
        punct = find_punctuation(title)
        if punct:
            issues.append(f"含标点符号「{''.join(punct)}」（商品名称禁止出现标点）")

    if vcfg.get("check_forbidden", True):
        for w in all_forbidden_words(forbidden_cfg):
            if w and w in title:
                issues.append(f"含违禁词「{w}」")

    if vcfg.get("check_model_once", True) and model_name:
        cnt = title.count(model_name)
        if cnt > 1:
            issues.append(f"机型「{model_name}」出现 {cnt} 次（要求仅 1 次）")
        if cnt == 0:
            issues.append(f"机型「{model_name}」未出现")
        # 多机型：合并后第一段带 iPhone 前缀，后续段不带，
        # 所以检查后续段时要先剥掉 iPhone 前缀
        models = split_models(model_name)
        for m in models[1:]:
            suffix = re.sub(r"^iPhone", "", m)
            if suffix and suffix not in title:
                issues.append(f"机型「{m}」未出现")
        for brand in ["iPhone", "苹果"]:
            c = title.count(brand)
            if c > 1:
                issues.append(f"「{brand}」出现 {c} 次（要求仅 1 次）")

    return issues
