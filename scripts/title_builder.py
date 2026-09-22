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


_MODEL_TAIL_WORDS = [
    "支点", "手机壳", "保护套", "壳", "主图", "详情图", "款", "系列",
]
# ⚠️ 「典藏版 / 限量版」这类**版本名不算尾部修饰词**，必须保留：
#    用户明确「华为puraXMax/华为puraXMax典藏版」是**两个机型**，
#    标题里两个都要出现（典藏版是独立 SKU，属正常适配机型）。

# ============================================================
# 品牌表
# ============================================================
# 背景：本类目不只卖苹果壳，机型列会出现华为等其它品牌。
#       品牌必须按**实际机型**判定，绝不能所有商品都写「苹果」。
#       真实配置在 config/required_words.yaml → brands；
#       这里只是 required_cfg 缺失时的兜底，避免函数签名被 cfg 污染。
_FALLBACK_BRANDS = {
    "苹果": {
        "aliases": ["苹果", "iPhone", "iphone", "IPHONE", "Iphone"],
        "title_template_single": "适用于苹果iPhone{model}",
        "title_template": "适用于苹果{model}",
        "title_template_2": "iPhone{model}",
        "title_template_n": "{model}",
        "brand_words": ["苹果", "iPhone"],
        "require_brand_words": True,
    },
    "华为": {
        "aliases": ["华为", "huawei", "HUAWEI", "Huawei", "HW", "hw"],
        "title_template_single": "适用华为{model}",
        "title_template": "适用华为{model}",
        "title_template_2": "{model}",
        "title_template_n": "{model}",
        "brand_words": ["华为"],
        "require_brand_words": True,
    },
}

# 机型后缀别名兜底（真实配置在 required_words.yaml → model_suffix_aliases）
_FALLBACK_SUFFIX_ALIASES = {
    "pm": "ProMax", "p": "Pro", "promax": "ProMax", "pro": "Pro",
    "plus": "Plus", "mini": "Mini", "max": "Max", "air": "Air",
    "ultra": "Ultra", "duo": "Duo", "se": "SE", "u": "U", "e": "E",
}

_DEFAULT_BRAND = "苹果"


def _get_brands(required_cfg=None):
    """取品牌表（配置优先，缺失时用兜底表）"""
    table = (required_cfg or {}).get("brands")
    return table if isinstance(table, dict) and table else _FALLBACK_BRANDS


def _get_suffix_aliases(required_cfg=None):
    """取机型后缀别名表（配置优先，缺失时用兜底表）"""
    table = (required_cfg or {}).get("model_suffix_aliases")
    return table if isinstance(table, dict) and table else _FALLBACK_SUFFIX_ALIASES


def _build_model_token_re(aliases):
    """按别名表生成机型 token 正则（长别名优先，避免 pro 抢在 promax 前面）"""
    keys = sorted((k for k in aliases if k), key=len, reverse=True)
    alt = "|".join(re.escape(k) for k in keys)
    return re.compile(r"(\d{2})\s*(" + alt + r")?", re.I)


_MODEL_TOKEN_RE = _build_model_token_re(_FALLBACK_SUFFIX_ALIASES)


def _cap_suffix(sfx, aliases):
    """后缀规范化：pm -> ProMax、promax -> ProMax、u -> U"""
    if not sfx:
        return ""
    return aliases.get(sfx.lower()) or (sfx[0].upper() + sfx[1:])


def _brand_aliases(required_cfg=None):
    """全部品牌别名，按长度降序（长别名优先，避免「iphone」被「ip」之类抢先）"""
    out = []
    for cfg in _get_brands(required_cfg).values():
        out += [a for a in (cfg.get("aliases") or []) if a]
    return sorted(set(out), key=len, reverse=True)


def _has_brand(s, required_cfg=None):
    """文本里是否出现任何品牌别名"""
    t = str(s or "").lower()
    return any(a.lower() in t for a in _brand_aliases(required_cfg))


def strip_brand_prefix(s, required_cfg=None):
    """
    剥掉机型字符串**开头**的品牌前缀。
        iPhone18U  -> 18U
        苹果18U     -> 18U
        华为PuraXMax -> PuraXMax
    """
    t = str(s or "").strip()
    changed = True
    while changed:
        changed = False
        for a in _brand_aliases(required_cfg):
            if len(t) > len(a) and t.lower().startswith(a.lower()):
                t = t[len(a):]
                changed = True
                break
    return t


def detect_brand(spec, required_cfg=None):
    """
    在机型文本里识别品牌，返回品牌名（如「苹果」「华为」）。

    传 list 时按整体文本判定（一个商品只有一个品牌）。
    都没命中则返回 default_brand（默认苹果，兼容 18U 这类裸机型写法）。
    """
    if isinstance(spec, (list, tuple)):
        s = " ".join(str(x) for x in spec)
    else:
        s = str(spec or "")
    default = (required_cfg or {}).get("default_brand") or _DEFAULT_BRAND
    if not s:
        return default
    low = s.lower()
    best_name, best_len = None, -1
    for name, cfg in _get_brands(required_cfg).items():
        for a in (cfg.get("aliases") or []):
            if a and a.lower() in low and len(a) > best_len:
                best_name, best_len = name, len(a)
    return best_name or default


def brand_words_of(brand, required_cfg=None):
    """取某品牌在标题里承载的品牌词（用于重复出现检查 / 关键词去重）"""
    cfg = _get_brands(required_cfg).get(brand) or {}
    return [w for w in (cfg.get("brand_words") or []) if w]


# 机型列里可能混入材质，用这些分隔符切开（实测写法「素皮、华为puraXMax/…」）
_MATERIAL_SPLIT_RE = re.compile(r"[、,，;；]")


def strip_material_prefix(spec, required_cfg=None):
    """
    剥掉机型列里的材质前缀，只留下机型部分。

    实测机型列常写成「素皮、华为puraXMax/华为puraXMax典藏版」——
    材质 + 顿号 + 机型列表。材质不是机型，必须先剔除，
    否则品牌识别和机型解析都会被带偏（这正是 Duo 解析不出来的原因）。

    判据：按分隔符切开后，只保留「像机型」的段
          （含品牌别名，或含数字机型 token）。
    """
    if not spec:
        return ""
    s = str(spec).strip()
    if not _MATERIAL_SPLIT_RE.search(s):
        return s
    parts = [p.strip() for p in _MATERIAL_SPLIT_RE.split(s) if p.strip()]
    if len(parts) < 2:
        return s
    keep = [
        p for p in parts
        if _has_brand(p, required_cfg) or _MODEL_TOKEN_RE.search(p)
    ]
    return "".join(keep)


def _has_model_token(s, required_cfg=None):
    """这一段里是否含机型特征（数字+后缀，或品牌前缀）"""
    if not s:
        return False
    if _has_brand(s, required_cfg):
        return True
    rex = _MODEL_TOKEN_RE if not required_cfg else \
        _build_model_token_re(_get_suffix_aliases(required_cfg))
    return bool(rex.search(s))



def _looks_like_path(s, required_cfg=None):
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
    if len(segs) >= 2 and all(_has_model_token(p, required_cfg) for p in segs):
        return False                   # 全是机型 → 机型列表
    return s.count("/") >= 2


def _parse_apple_segment(body, required_cfg=None):
    """苹果机型片段 → ['iPhone18U', ...]（一段里可能含多个机型）"""
    aliases = _get_suffix_aliases(required_cfg)
    rex = _build_model_token_re(aliases)
    out = []
    for m in rex.finditer(body):
        out.append("iPhone" + m.group(1) + _cap_suffix(m.group(2), aliases))
    if out:
        return out
    # 无数字机型（Duo / Air / SE / Ultra …）；
    # 必须看起来像机型才认，避免把目录名（如 images）当成机型
    t = re.sub(r"[\s/]+", "", body)
    if not t or not t.isascii():
        return []
    if len(t) > 4 and not re.fullmatch(
        r"(?i)(duo|air|ultra|se|mini|max|plus|fold|pro|promax)", t
    ):
        return []
    return ["iPhone" + _cap_suffix(t, aliases)]


def _parse_segment(seg, brand, required_cfg=None):
    """
    解析单个机型片段，返回该片段含有的全部机型（带品牌前缀）。

    苹果片段可能一次含多个机型（如 '苹果18u17promax' → 18U + 17ProMax），
    所以返回列表。
    """
    body = strip_brand_prefix(seg, required_cfg)
    # 剥尾部修饰词（典藏版 / 手机壳 / 系列 …）
    changed = True
    while changed:
        changed = False
        for w in _MODEL_TAIL_WORDS:
            if body.endswith(w) and len(body) > len(w):
                body = body[: -len(w)]
                changed = True
    if not body:
        return []

    if brand == _DEFAULT_BRAND:
        return _parse_apple_segment(body, required_cfg)

    # 非苹果品牌：整段当机型名。
    # 华为等机型是字母数字混排（puraXMax / mate60 / nova12），
    # 没法用「两位数字 + 后缀」的苹果正则去切，切了反而会切出垃圾。
    t = re.sub(r"[\s/]+", "", body)
    if not t:
        return []
    return [brand + t[0].upper() + t[1:]]


def split_models(spec, required_cfg=None):
    """
    把机型文本拆成**多个机型**，返回值带品牌前缀：
        苹果 -> ['iPhone18U', 'iPhone17ProMax']
        华为 -> ['华为PuraXMax']

    用户约定：机型列里出现的疑似机型都算适配机型，都要进商品名称。

    例：
        '苹果18u17promax'                        -> ['iPhone18U','iPhone17ProMax']
        '苹果15/16promax'                        -> ['iPhone15','iPhone16ProMax']
        'iPhone Duo'                             -> ['iPhoneDuo']
        '素皮、华为puraXMax/华为puraXMax典藏版'    -> ['华为PuraXMax']
        '透明、iPhone18Pro/17ProMax/16/18pm'      -> ['iPhone18Pro','iPhone17ProMax','iPhone16','iPhone18ProMax']
        '硅胶、iPhone18ProMax/苹果17Pro/17pm/18p' -> ['iPhone18ProMax','iPhone17Pro','iPhone17ProMax','iPhone18Pro']
    """
    if not spec:
        return []
    s = str(spec).strip()
    if not s:
        return []

    # 1. 剥掉「材质、」前缀。
    #    实测机型列写成「素皮、iPhoneDuo」，材质前缀会挡住品牌识别，
    #    导致整段解析失败（这就是 Duo 丢失的原因）。
    s = strip_material_prefix(s, required_cfg)
    if not s:
        return []

    # 2. 若是路径，先取出机型段
    if _looks_like_path(s, required_cfg):
        s = extract_model_from_path(s)
        if not s:
            return []

    # 3. 整段先判一次品牌（一个商品只有一个品牌）
    brand = detect_brand(s, required_cfg)

    # 4. 切成候选段；切不动就整段交给 token 正则扫
    segs = [p.strip() for p in re.split(r"[/\s、]+", s) if p.strip()]
    if not segs:
        return []

    models, seen = [], set()
    for seg in segs:
        for m in _parse_segment(seg, brand, required_cfg):
            if m and m not in seen:
                seen.add(m)
                models.append(m)
    return models


def format_model_group(models, required_cfg=None):
    """
    把多个机型合成标题里的一段。

    ⚠️ 用户要求：商品名称中**不能出现标点符号**（含 `/`），
       所以多机型直接连写，不加任何分隔符：
           ['iPhone18U', 'iPhone17ProMax'] -> 'iPhone18U17ProMax'

    只保留第一个品牌前缀，避免品牌词重复出现。
    """
    models = [m for m in (models or []) if m]
    if not models:
        return ""
    if len(models) == 1:
        return models[0]
    out = [models[0]]
    for m in models[1:]:
        out.append(strip_brand_prefix(m, required_cfg))
    return "".join(out)


def normalize_model(spec, required_cfg=None):
    """
    机型规范化（返回可用于标题的整段文本，支持多机型）。

        iphone16          -> iPhone16
        iPhone 15 Pro     -> iPhone15Pro
        苹果18u17promax    -> iPhone18U17ProMax     ← 多机型
        iPhone Duo        -> iPhoneDuo
        华为puraXMax       -> 华为PuraXMax
    """
    models = split_models(spec, required_cfg)
    return format_model_group(models, required_cfg)


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

    models —— 机型列表（带品牌前缀，如 ['iPhone18','iPhone17'] 或 ['华为PuraXMax']）
              不传则从 model_name 拆

    品牌由机型列**实际内容**决定（config → brands）：
        苹果 -> model_0「适用于苹果18」 model_1「iPhone17」 其余裸写
        华为 -> model_0「适用华为PuraXMax」 其余裸写

    ★ 解析不到机型时 model_0 留空，**绝不编造品牌**——
      之前这里写死 tpl.format(model="iPhone")，导致华为商品被套上「适用于苹果iPhone」。

    槽位说明：
        model_0      第 1 个机型（带品牌）
        model_1      第 2 个机型（苹果带「iPhone」，其它品牌裸写）
        model_2...   第 3 个起的裸机型（穿插用）
        core_word    手机壳
        new_word     新款
        protect_word 防摔
        material     材质词（可能为空）
        feature      条件特征词（如「磁吸magsafe」「支架」，可能为空）
    """
    seg = {}
    if not models:
        models = split_models(model_name, required_cfg) if model_name else []

    brands = _get_brands(required_cfg)
    # 品牌：优先按机型列表判定，没有机型就按原始文本判（用于报错提示）
    brand = detect_brand(models or model_name, required_cfg)
    bcfg = brands.get(brand) or brands.get(_DEFAULT_BRAND) or {}

    tpl = bcfg.get("title_template", "适用于苹果{model}")
    tpl2 = bcfg.get("title_template_2", "iPhone{model}")
    tpln = bcfg.get("title_template_n", "{model}")
    # 单机型专用模板：苹果要一次带齐「苹果」「iPhone」
    tpl1 = bcfg.get("title_template_single") or tpl

    def _naked(m):
        return strip_brand_prefix(m, required_cfg)

    if models:
        if len(models) == 1:
            # 单机型：用专用模板，苹果要一次带齐「苹果」「iPhone」
            seg["model_0"] = tpl1.format(model=_naked(models[0]))
        else:
            seg["model_0"] = tpl.format(model=_naked(models[0]))
            seg["model_1"] = tpl2.format(model=_naked(models[1]))
            for i, m in enumerate(models[2:], start=2):
                seg[f"model_{i}"] = tpln.format(model=_naked(m))
    else:
        # ★ 绝不编造机型 / 品牌。留空，由调用方明确报错并跳过该行。
        seg["model_0"] = ""

    seg["brand"] = brand

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

    # 1b) ★ 机型比模板里的槽位多时，补足槽位。
    #     每个模板的 model_n 数量是固定的（1 个 model_n 只能放 1 个机型），
    #     机型一多就会「有机型但没槽位」，导致该机型被整个丢掉。
    #     补位时**每个机型前面插一个关键词槽**，保证机型之间不相邻（机型不连写）。
    have = {s for s in pattern if re.fullmatch(r"model_\d+", s)}
    missing, i = [], 0
    while f"model_{i}" in segments:
        if f"model_{i}" not in have:
            missing.append(f"model_{i}")
        i += 1
    if missing:
        insert_at = len(pattern)
        for j, slot in enumerate(pattern):
            if slot in ("feature", "material", "padding"):
                insert_at = j
                break
        extra = []
        for name in missing:
            extra += ["keywords", name]
        pattern[insert_at:insert_at] = extra

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
        """
        按模板铺开；关键词按槽位顺序从词池里取。

        ★ 被跳过的词会**留到下一个槽位再试**，而不是直接丢弃。
          原因：商品名称不能有分隔符，所以「英文粘连」要靠 `can_join()` 拦截
          （机型段结尾是数字，后面直接跟 `ins风` 会粘成 `18ins风`）。
          但**奇数长度的词基本都以英文开头**（`ins风` `Q版` `3D立体`），
          一旦排在机型段后面就被拦掉；若直接丢弃，就永远凑不出奇数缺口，
          最后只能塞空格 —— 而用户明确要求名称里不能有空格。
        """
        t = ""
        text_done = 0
        picked = []
        queue = [w for w in trial_words if w]
        for kind, val in layout:
            if kind == "text":
                t = join_text(t, val)
                text_done += W(val)
                continue
            if kind != "kw":
                continue
            remaining_text = total_text_w - text_done
            rest, chosen = [], None
            for i, kw in enumerate(queue):
                if kw in picked:
                    continue
                if not can_join(t, kw):
                    rest.append(kw)          # 本槽位放不下 → 下个槽位再试
                    continue
                cand = join_text(t, kw)
                if W(cand) + remaining_text <= target:
                    chosen = kw
                    rest += queue[i + 1:]
                    break
                rest.append(kw)
            if chosen:
                t = join_text(t, chosen)
                picked.append(chosen)
            queue = rest
        return t, picked

    def _pad(t, picked, gap):
        """
        给定已铺好的标题 t 与已用词 picked，把长度补到 target。
        返回 (标题, 是否精确, 用词列表)。
        """
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

    def _fill(trial_words):
        """
        给定关键词池，铺开标题并补满长度。

        ★ 奇偶修复（用户要求：名称中绝不能有空格）
          汉字算 2、英文算 1，总长度存在**奇偶约束**。
          当缺口是奇数（尤其只差 1 个字符）时，补足词池里可能没有任何词能填
          （最短的词也有 3~4 字符）。这时**不塞空格**，而是
          **放弃一个已用关键词后重建** —— 总长度奇偶随之翻转，再用补足词凑满。
        """
        t, picked = _build(trial_words)
        gap = target - W(t)
        best = _pad(t, picked, gap)
        if best[1]:
            return best

        # ★ 奇偶修复 A：缺口是奇数 → 「已用词总长」必须是奇数才凑得满。
        #   若贪心取到的词恰好全是偶数长度，就把**奇数长度的词提到词池最前面**，
        #   让贪心一定取到它（它们通常排在末尾，正常取不到）。
        if gap % 2 == 1 and trial_words:
            odd_first = sorted(trial_words, key=lambda w: W(w) % 2 == 0)
            t3, p3 = _build(odd_first)
            r = _pad(t3, p3, target - W(t3))
            if r[1]:
                return r
            if W(r[0]) > W(best[0]):
                best = r

        # ★ 奇偶修复 B：逐个放弃一个已用词，重建后重试（从后往前，尽量保留靠前的词）
        for i in range(len(picked) - 1, -1, -1):
            sub = picked[:i] + picked[i + 1:]
            if not sub:
                continue
            t2, picked2 = _build(sub)
            r = _pad(t2, picked2, target - W(t2))
            if r[1]:
                return r
            if W(r[0]) > W(best[0]):
                best = r
        return best

    # ★ 候选尝试序列
    #   目标长度是精确值，而汉字算 2、英文算 1 —— 总长度存在**奇偶约束**：
    #   只靠「补足词子集和」凑不出奇数缺口（例如差 1 个字符时，没有任何词 ≤1）。
    #   对策：改变**实际采用的词组合**来翻转奇偶，而不是在末尾塞一个空格。
    #   （用户明确要求：名称中绝不能有空格，必须用中文或英文填满）
    _, picked0 = _build(kws)

    trials = [list(kws)]
    # a) 逐步去掉末尾关键词
    for drop in range(1, min(len(kws), 6) + 1):
        trials.append(kws[: len(kws) - drop])
    # b) 逐个移除「实际被用上的词」—— 后续槽位会取到别的词，总长度随之变化
    for w in reversed(picked0):
        trials.append([x for x in kws if x != w])
    # c) 用「未用词」替换「已用词」，且两者长度奇偶不同 —— 直接翻转总长度奇偶
    unused = [w for w in kws if w and w not in picked0]
    swaps = 0
    for w_out in reversed(picked0):
        if swaps >= 40:
            break
        for w_in in unused:
            if swaps >= 40:
                break
            if (W(w_in) - W(w_out)) % 2 == 0:
                continue
            tw = list(kws)
            try:
                tw[tw.index(w_out)] = w_in
            except ValueError:
                continue
            trials.append(tw)
            swaps += 1

    best_result = None
    for trial in trials:
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
        ms = list(models) if models else split_models(model_name, required_cfg)
        # 每个机型都要出现（第 1 个带品牌、其余裸写，所以统一剥掉品牌前缀再比对）
        for m in ms:
            suf = strip_brand_prefix(m, required_cfg)
            if suf and suf not in title:
                issues.append(f"机型「{m}」未出现")

        # 品牌词：本品牌的必须出现（require_brand_words），且各只能出现 1 次
        actual_brand = detect_brand(ms or model_name, required_cfg)
        brands = _get_brands(required_cfg)
        bcfg = brands.get(actual_brand) or {}
        require_bw = bcfg.get("require_brand_words", True)
        for w in (bcfg.get("brand_words") or []):
            c = title.count(w)
            if require_bw and c == 0:
                issues.append(f"缺少品牌词「{w}」（{actual_brand}商品必须出现）")
            elif c > 1:
                issues.append(f"「{w}」出现 {c} 次（要求仅 1 次）")

        # ★ 不能出现其它品牌的品牌词（华为商品里冒出「苹果」「iPhone」＝虚假品牌宣称）
        for other, ocfg in brands.items():
            if other == actual_brand:
                continue
            for w in (ocfg.get("brand_words") or []):
                if w and w in title:
                    issues.append(
                        f"出现非本商品品牌词「{w}」（实际品牌：{actual_brand}）"
                    )

    return issues
