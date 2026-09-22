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


def check_word_conflicts(required_cfg, forbidden_cfg):
    """
    检查配置冲突：同一个词同时出现在「必填/条件必含/词库」与「违禁词」里。

    这类冲突会让程序自相矛盾（一边强制加、一边判定违规），
    实测踩过一次（「支架」既在禁止词库、又在条件必含词里）。
    返回 [(被要求出现的词, 命中的违禁词), ...]
    """
    fw = [w for w in all_forbidden_words(forbidden_cfg) if w]

    required_words = []
    required_words += [str(x) for x in (required_cfg.get("must_include") or [])]
    required_words += [str(x) for x in (required_cfg.get("material_group") or [])]
    for rule in (required_cfg.get("conditional_words") or []):
        if isinstance(rule, dict) and rule.get("must_include"):
            required_words.append(str(rule["must_include"]))
    banks = required_cfg.get("word_banks") or {}
    for v in banks.values():
        if isinstance(v, list):
            required_words += [str(x) for x in v]

    conflicts, seen = [], set()
    for rw in required_words:
        for f in fw:
            if rw and f and (f == rw or f in rw):
                key = (rw, f)
                if key not in seen:
                    seen.add(key)
                    conflicts.append(key)
    return conflicts


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


def collect_bank_words(required_cfg):
    """
    按优先级收集四类运营词库的词：
        主推词 → 搜索词 → 卖点词 → 精准词
    返回去重后的有序列表。顺序决定组装时的取用优先级。
    """
    banks = required_cfg.get("word_banks") or {}
    order = banks.get("priority") or [
        "main_words", "search_words", "selling_words", "precise_words"
    ]
    out = []
    for key in order:
        for w in (banks.get(key) or []):
            w = str(w).strip()
            if w and w not in out:
                out.append(w)
    return out


def detect_features(row_data, model_keywords, required_cfg):
    """
    判定商品具备哪些特征，返回**必须包含**的特征词列表。

    判定来源（任一命中即算具备）：
        detect_in_data     —— 商品资料（表格第一列）文本
        detect_in_keywords —— 视觉模型看图输出的关键词

    例：资料里含「支点」→ 判定为支架 → 返回 ['支架']
        模型关键词里含「磁吸」→ 判定为磁吸 → 返回 ['磁吸magsafe']
    """
    rules = required_cfg.get("conditional_words") or []
    data_text = str(row_data or "")
    kw_text = " ".join(str(k) for k in (model_keywords or []))
    out = []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        hit = False
        for w in (rule.get("detect_in_data") or []):
            if w and w in data_text:
                hit = True
                break
        if not hit:
            for w in (rule.get("detect_in_keywords") or []):
                if w and w in kw_text:
                    hit = True
                    break
        if hit:
            word = str(rule.get("must_include") or "").strip()
            if word and word not in out:
                out.append(word)
    return out


def build_segments(model_name, material, required_cfg, feature_words=None, models=None):
    """
    构造固定段（承载全部必填词），返回 {槽位名: 文本}。

    models —— 机型列表（规范化后，如 ['iPhone18','iPhone17','iPhone16']）
              不传则从 model_name 拆

    槽位说明：
        model_0      适用于苹果{第1个机型}（提供「苹果」）
        model_1      iPhone{第2个机型}（提供「iPhone」）
        model_2...   第 3 个起的裸机型（穿插用）
        core_word    手机壳
        new_word     新款
        protect_word 防摔
        material     材质词（可能为空）
        feature      条件特征词（如「磁吸magsafe」「支架」，可能为空）
    """
    seg = {}
    if not models:
        models = split_models(model_name) if model_name else []

    tpl = required_cfg.get("model_template", "适用于苹果{model}")
    tpl_ip = required_cfg.get("model_template_iphone", "iPhone{model}")

    def _suffix(m):
        return re.sub(r"^iPhone", "", m or "")

    if not models:
        seg["model_0"] = tpl.format(model="iPhone")
    elif len(models) == 1:
        # 只有一个机型时，第一段同时带上「苹果」和「iPhone」
        seg["model_0"] = tpl.format(model=models[0])
    else:
        seg["model_0"] = tpl.format(model=_suffix(models[0]))
        seg["model_1"] = tpl_ip.format(model=_suffix(models[1]))
        for i, m in enumerate(models[2:], start=2):
            seg[f"model_{i}"] = _suffix(m)

    # 必填词拆位（对齐真实标题的分布：手机壳 22%、新款 48%、防摔 74%）
    seg["core_word"] = required_cfg.get("core_word", "手机壳")
    seg["new_word"] = required_cfg.get("new_word", "新款")
    seg["protect_word"] = required_cfg.get("protect_word", "防摔")
    # 组合式 + 旧槽位名，兼容老模板
    seg["core_suffix"] = required_cfg.get("core_template", "新款手机壳防摔")
    seg["model_prefix"] = seg["model_0"]

    seg["material"] = material or ""
    fw = [w for w in (feature_words or []) if w]
    seg["feature"] = "".join(fw) if fw else ""
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


def check_pattern_models_separated(patterns):
    """
    检查组装模板里是否有「两个机型槽位直接相邻」的情况。

    相邻会导致机型连成一串，例如：
        适用于苹果18iPhone1716ProMax手机壳…
    正确写法是中间夹一个关键词槽位，让机型穿插在属性词之间。

    返回有问题的模板下标列表（空 = 全部合规）。
    """
    bad = []
    for i, pat in enumerate(patterns or []):
        prev_is_model = False
        for slot in (pat or []):
            is_model = str(slot).startswith("model_")
            if is_model and prev_is_model:
                bad.append(i)
                break
            prev_is_model = is_model
    return bad


def _bigrams(s):
    """取 2 字组合集合，用于判断两词是否语义重叠"""
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


def _resolve_pattern(pattern, segments, keywords):
    """
    把模板展开成有序的「段」列表：
        [("text", 文本), ("kw", None), ("padding", None)]

    - 每个 ("kw", None) 是一个**关键词槽位**，组装时按顺序从同一个词池里取词，
      所以同一个词不会被重复放进标题
    - `model_n` 按出现顺序展开成 model_2 / model_3 / …（机型不够时自动跳过）
    - 条件特征词（feature）若不在模板里，自动插到核心词段之后
    """
    pattern = list(pattern or [])

    # 1) 展开 model_n
    pat, idx = [], 2
    for slot in pattern:
        if slot == "model_n":
            pat.append(f"model_{idx}")
            idx += 1
        else:
            pat.append(slot)
    pattern = pat

    # 2) 条件特征词自动补位
    if segments.get("feature") and "feature" not in pattern:
        pat2, inserted = [], False
        for slot in pattern:
            pat2.append(slot)
            if slot in ("core_word", "core_suffix") and not inserted:
                pat2.append("feature")
                inserted = True
        if not inserted:
            pat2.insert(0, "feature")
        pattern = pat2

    # 3) 展开成 layout
    out = []
    for slot in pattern:
        if slot in ("kw", "keywords", "keywords_a", "keywords_b"):
            out.append(("kw", None))
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

    ★ 关键词槽位依次消费同一个词池 —— 每个词只会被用一次，不会重复出现。
    ★ 固定段（机型/手机壳/新款/防摔/材质/特征）必须放得下，加关键词时会预留余量。

    返回 (标题, 是否精确命中, 使用的词列表)
    """
    W = lambda s: count_length(s, count_mode)
    exclude = set(exclude_words or {})

    if not pattern:
        pattern = ["model_0", "model_1", "core_word", "new_word",
                   "protect_word", "keywords", "feature", "material", "padding"]

    kws = list(keywords or [])
    if rotate and kws:
        r = rotate % len(kws)
        kws = kws[r:] + kws[:r]

    layout = _resolve_pattern(pattern, segments, kws)
    # 固定段总长度：用来给还没放的关键词预留余量，保证必填词不被截断
    total_text_w = sum(W(v) for k, v in layout if k == "text")

    def _build(trial_words):
        """按模板铺开；关键词从 trial_words 里按槽位顺序依次取"""
        t = ""
        text_done = 0
        picked, qi = [], 0
        for kind, val in layout:
            if kind == "text":
                t = join_text(t, val)
                text_done += W(val)
            elif kind == "kw":
                remaining_text = total_text_w - text_done
                while qi < len(trial_words):
                    kw = trial_words[qi]
                    qi += 1
                    if not kw or kw in picked:
                        continue
                    if not can_join(t, kw):
                        continue
                    cand = join_text(t, kw)
                    if W(cand) + remaining_text <= target:
                        t = cand
                        picked.append(kw)
                        break
        return t, picked

    def _fill(trial_words):
        """给定采用的关键词，重建标题并尝试补满缺口"""
        t, picked = _build(trial_words)
        gap = target - W(t)
        if gap < 0:
            out = trim_to_weight(t, target, count_mode)
            return out, W(out) == target, picked
        if gap == 0:
            return t, True, picked

        # 补足词池：排除已用与近义重复；跨商品用过的排后面（差异化）
        cand = []
        for w in (padding_pool or []):
            if not w or w in picked:
                continue
            if W(w) > gap:
                continue
            if not can_join(t, w):
                continue
            if is_near_duplicate(w, picked) or is_near_duplicate(w, cand):
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
            for st in sorted(dp.keys(), reverse=True):
                ns = st + L
                if ns <= gap and ns not in dp:
                    dp[ns] = dp[st] + [w]
            if gap in dp:
                break
        if gap in dp:
            fill = dp[gap]
            out = t
            for w in fill:
                out = join_text(out, w)
            return out, True, picked + fill

        best = max(dp.keys()) if dp else 0
        tail = dp.get(best, [])
        t2 = t
        for w in tail:
            t2 = join_text(t2, w)
        need = target - W(t2)
        if need > 0:
            for w in cand:
                if w in picked or w in tail:
                    continue
                piece = trim_to_weight(w, need, count_mode)
                if piece and len(piece) >= 2 and W(piece) == need and can_join(t2, piece):
                    return join_text(t2, piece), True, picked + tail
        return t2, W(t2) == target, picked + tail

    best_result = None
    # 依次尝试：先用全部关键词 → 逐步去掉末尾关键词（调整长度奇偶性）
    for drop in range(0, min(len(kws), 6) + 1):
        trial = kws[: len(kws) - drop] if drop else kws
        title, exact, final_used = _fill(trial)
        if exact:
            return title, True, final_used
        if best_result is None or W(title) > W(best_result[0]):
            best_result = (title, exact, final_used)

    if best_result:
        return best_result
    return "", False, []


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

def validate_title(title, model_name, rules, required_cfg, forbidden_cfg, models=None):
    """
    校验标题，返回问题列表（空列表 = 全部通过）。

    models —— 机型列表（来自表格机型列）。多机型时逐个检查是否都出现了。
    """
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

    if vcfg.get("check_model_once", True):
        ms = list(models) if models else split_models(model_name)
        # 每个机型都要出现（第 1 个带「苹果」、第 2 个带「iPhone」、其余裸写，
        # 所以统一剥掉 iPhone 前缀再比对）
        for m in ms:
            suf = re.sub(r"^iPhone", "", m)
            if suf and suf not in title:
                issues.append(f"机型「{m}」未出现")
        # 「苹果」「iPhone」各只能出现一次
        for brand in ["iPhone", "苹果"]:
            c = title.count(brand)
            if c > 1:
                issues.append(f"「{brand}」出现 {c} 次（要求仅 1 次）")

    return issues
