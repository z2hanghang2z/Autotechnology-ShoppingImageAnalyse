# -*- coding: utf-8 -*-
"""
Ollama 客户端公共模块
统一从 config/model_config.yaml 读取模型配置，避免模型名散落在各处。

用法:
    from ollama_client import load_config, chat_with_image, encode_image

    cfg = load_config()
    result = chat_with_image(cfg, "图片路径", "提示词")
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request

try:
    import yaml
except ImportError:
    yaml = None

# 默认配置路径：项目根目录下的 config/model_config.yaml
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CONFIG_PATH = os.path.join(_ROOT, "config", "model_config.yaml")

# 兜底默认值（配置文件缺失时使用）
_FALLBACK = {
    "model": {"name": "qwen3.5:9b", "base_url": "http://127.0.0.1:11434"},
    "inference": {"temperature": 0.0, "stream": False, "timeout": 900,
                  "concurrency": 3, "think": False},
    "system_prompt": "你是一个商品图片识别助手。只依据图片中真实可见的内容作答，无法判断时说明\"不确定\"，严禁编造。",
}


def load_config(path: str = None) -> dict:
    """加载配置文件，缺失时回退到默认值"""
    path = path or DEFAULT_CONFIG_PATH
    cfg = dict(_FALLBACK)
    if yaml and os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        # 浅合并
        for k, v in loaded.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k] = {**cfg[k], **v}
            else:
                cfg[k] = v
    return cfg


def encode_image(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def health_check(cfg: dict, timeout: int = 5) -> bool:
    """检查 Ollama 服务是否可用"""
    try:
        url = cfg["model"]["base_url"].rstrip("/") + "/api/tags"
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def chat_with_image(cfg: dict, image_path: str, prompt: str,
                    system_prompt: str = None, timeout: int = None,
                    retries: int = None) -> dict:
    """
    单图问答，返回 {ok, elapsed, content, thinking, tokens, raw, attempts}

    内置网络异常重试（指数退避）——实测 Ollama 服务可能中途崩溃，
    报 502 Bad Gateway 或 RemoteDisconnected，无重试会导致整批任务失败。
    """
    m = cfg["model"]
    inf = cfg["inference"]
    sys_p = system_prompt if system_prompt is not None else cfg.get("system_prompt", "")
    max_retries = inf.get("max_retries", 3) if retries is None else retries
    backoff = inf.get("retry_backoff", 2)

    options = {"temperature": inf.get("temperature", 0.0)}
    if inf.get("num_predict"):
        options["num_predict"] = inf["num_predict"]

    payload = {
        "model": m["name"],
        "messages": [
            {"role": "system", "content": sys_p},
            {"role": "user", "content": prompt, "images": [encode_image(image_path)]},
        ],
        "stream": inf.get("stream", False),
        "options": options,
    }
    # thinking 模型：显式传 think 参数（False 可提速约 8 倍）
    if "think" in inf:
        payload["think"] = inf["think"]

    url = m["base_url"].rstrip("/") + "/api/chat"
    last_err = ""
    start = time.time()

    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or inf.get("timeout", 900)) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
            msg = raw.get("message", {})
            return {
                "ok": True,
                "elapsed": round(time.time() - start, 2),
                "content": msg.get("content", ""),
                "thinking": msg.get("thinking", "") or "",
                "tokens": raw.get("eval_count", 0),
                "raw": raw,
                "attempts": attempt,
            }
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            if attempt < max_retries:
                wait = backoff ** attempt
                print(f"      [重试 {attempt}/{max_retries - 1}] {last_err[:60]} → 等待 {wait}s")
                time.sleep(wait)

    return {
        "ok": False,
        "elapsed": round(time.time() - start, 2),
        "error": last_err,
        "content": "",
        "thinking": "",
        "tokens": 0,
        "raw": {},
        "attempts": max_retries,
    }


def list_models(cfg: dict = None) -> list:
    """列出已安装模型"""
    cfg = cfg or load_config()
    url = cfg["model"]["base_url"].rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8")).get("models", [])
    except Exception:
        return []


if __name__ == "__main__":
    c = load_config()
    print(f"配置文件: {DEFAULT_CONFIG_PATH}")
    print(f"当前模型: {c['model']['name']}")
    print(f"服务地址: {c['model']['base_url']}")
    print(f"并发设置: {c['inference']['concurrency']}")
    print("\n已安装模型:")
    for m in list_models(c):
        print(f"  - {m['name']} ({round(m['size']/1024/1024/1024, 2)} GB)")
