# -*- coding: utf-8 -*-
"""
Ollama 视觉模型并发压测
逐级提升并发数，找出显卡的实际并发极限。
模型配置从 config/model_config.yaml 读取。

用法: python stress_concurrency.py <图片目录> [并发级别,逗号分隔]
示例: python stress_concurrency.py "C:/Users/fucker/Pictures/Screenshots" 1,2,3,4,5
"""
import base64
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

from ollama_client import load_config

CFG = load_config()
OLLAMA_URL = CFG["model"]["base_url"].rstrip("/") + "/api/chat"
PS_URL = CFG["model"]["base_url"].rstrip("/") + "/api/ps"
MODEL = CFG["model"]["name"]
THINK = CFG["inference"].get("think", False)

SYSTEM = CFG.get("system_prompt", "")
PROMPT = "这是什么商品？颜色、图案、摄像头开孔数量分别是什么？简短回答。"


def find_images(folder: str) -> list:
    exts = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
    files = []
    for name in sorted(os.listdir(folder)):
        if name.lower().endswith(exts):
            files.append(os.path.join(folder, name))
    return files


def encode_image(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


_IMG_CACHE = {}


def get_b64(path: str) -> str:
    if path not in _IMG_CACHE:
        _IMG_CACHE[path] = encode_image(path)
    return _IMG_CACHE[path]


def one_call(image_path: str) -> dict:
    """单次模型调用，返回耗时与结果"""
    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": PROMPT, "images": [get_b64(image_path)]},
        ],
        "stream": False,
        "think": THINK,
        "options": {"temperature": CFG["inference"].get("temperature", 0.1)},
    }
    req = urllib.request.Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:
            r = json.loads(resp.read().decode("utf-8"))
        return {
            "ok": True,
            "elapsed": round(time.time() - start, 2),
            "tokens": r.get("eval_count", 0),
            "image": os.path.basename(image_path),
        }
    except Exception as e:
        return {
            "ok": False,
            "elapsed": round(time.time() - start, 2),
            "error": f"{type(e).__name__}: {e}",
            "image": os.path.basename(image_path),
        }


def gpu_mem() -> str:
    """通过 Ollama /api/ps 读取模型实际占用的显存（跨显卡品牌通用）"""
    return ollama_loaded()


def ollama_loaded() -> str:
    """查看当前加载的模型实例及其显存占用"""
    try:
        with urllib.request.urlopen(PS_URL, timeout=10) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        models = d.get("models", [])
        if not models:
            return "无已加载模型"
        parts = []
        for m in models:
            vram = m.get("size_vram", 0)
            total = m.get("size", 0)
            gpu_pct = round(vram / total * 100, 1) if total else 0
            parts.append(
                f"{m.get('name')} | VRAM {round(vram/1024/1024, 1)}MB "
                f"({gpu_pct}% 在显存) | 处理器 {m.get('details',{}).get('quantization_level','')}"
            )
        return "; ".join(parts)
    except Exception as e:
        return f"读取失败: {e}"


def run_level(images: list, concurrency: int) -> dict:
    """跑一个并发级别"""
    print(f"\n{'='*70}")
    print(f"【并发级别 {concurrency}】图片数 {len(images)}")
    print(f"  压测前: {gpu_mem()}")
    print(f"{'-'*70}")

    start = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(one_call, img): img for img in images}
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            if r["ok"]:
                print(f"  ✓ {r['image']:<20} {r['elapsed']:>7.2f}s  {r['tokens']} tokens")
            else:
                print(f"  ✗ {r['image']:<20} {r['elapsed']:>7.2f}s  失败: {r['error'][:80]}")
    wall = round(time.time() - start, 2)

    ok_count = sum(1 for r in results if r["ok"])
    fail_count = len(results) - ok_count
    avg = round(sum(r["elapsed"] for r in results if r["ok"]) / ok_count, 2) if ok_count else 0
    total_tokens = sum(r.get("tokens", 0) for r in results)

    peak_mem = gpu_mem()

    print(f"{'-'*70}")
    print(f"  总墙钟耗时: {wall}s  |  平均单次: {avg}s  |  成功 {ok_count} / 失败 {fail_count}")
    print(f"  吞吐: {round(ok_count/wall, 2)} 图/秒  |  总 tokens {total_tokens}")
    print(f"  压测后: {peak_mem}")

    return {
        "concurrency": concurrency,
        "wall": wall, "ok": ok_count, "fail": fail_count,
        "avg": avg, "throughput": round(ok_count / wall, 2) if wall else 0,
        "gpu_mem": peak_mem, "max_single": max((r["elapsed"] for r in results if r["ok"]), default=0),
    }


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else r"C:\Users\fucker\Pictures\Screenshots"
    levels = [int(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2 else [1, 2, 3, 4, 5]

    images = find_images(folder)
    if not images:
        print(f"目录中没有找到图片: {folder}")
        return

    print("=" * 70)
    print("Ollama 视觉模型并发压测")
    print("=" * 70)
    print(f"模型: {MODEL}")
    print(f"图片目录: {folder}")
    print(f"图片数量: {len(images)} -> {[os.path.basename(i) for i in images]}")
    print(f"并发级别: {levels}")
    print(f"空闲显存: {gpu_mem()}")

    # 预热：先跑一次，避免首次加载模型权重污染第一级测试
    print(f"\n[预热] 先跑一次让模型加载进显存...")
    warm = one_call(images[0])
    print(f"[预热完成] {warm['elapsed']}s | 显存: {gpu_mem()}")
    print(f"[预热后实例] {ollama_loaded()}")

    summary = []
    for lv in levels:
        try:
            summary.append(run_level(images, lv))
        except Exception as e:
            print(f"  并发级别 {lv} 异常: {e}")
            break

    print(f"\n{'='*70}")
    print("【压测汇总】")
    print(f"{'并发':<6}{'墙钟(s)':<10}{'平均(s)':<10}{'最大(s)':<10}{'吞吐(图/s)':<12}{'成功':<6}{'失败':<6}{'显存'}")
    for s in summary:
        print(f"{s['concurrency']:<6}{s['wall']:<10}{s['avg']:<10}{s['max_single']:<10}"
              f"{s['throughput']:<12}{s['ok']:<6}{s['fail']:<6}{s['gpu_mem']}")
    print("=" * 70)


if __name__ == "__main__":
    main()
