# -*- coding: utf-8 -*-
"""探测 Mistral API key 的可用性与限流额度。

    python test_key.py <api_key>
    python test_key.py --key-file mistral_keys.json --index 0 --reasoning
    python test_key.py --all --workers 8

相对旧版修复:
  - 旧版结尾 `return r.status_code == 200 if "chat_status" in out else False` 里的 r 可能来自上一个
    /models 请求, 甚至在两个请求都抛异常时未绑定直接 NameError; 改为直接读 out["chat_status"]
  - --all 逐个串行探测 97 个 key 要跑很久, 改为线程池并发
"""
import argparse
import concurrent.futures
import csv
import json
import os
import sys
import time

import requests

API_BASE = "https://api.mistral.ai/v1"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
DEFAULT_KEYS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mistral_keys.json")


def load_keys(path: str) -> list[dict]:
    if os.path.splitext(path)[1].lower() == ".csv":
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else [data]


def rate_headers(headers) -> dict:
    h = {k.lower(): v for k, v in headers.items()}

    def num(name):
        try:
            return int(h.get(name, -1))
        except (TypeError, ValueError):
            return -1

    return {"limit_tok": num("x-ratelimit-limit-tokens-minute"),
            "remaining_tok": num("x-ratelimit-remaining-tokens-minute"),
            "limit_req": num("x-ratelimit-limit-req-minute"),
            "remaining_req": num("x-ratelimit-remaining-req-minute")}


def _auth(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
            "User-Agent": UA}


def test_one(api_key: str, model: str, probe_models: bool = True) -> tuple[bool, dict]:
    out: dict = {"model": model, "chat_status": 0}

    if probe_models:
        try:
            r = requests.get(f"{API_BASE}/models", headers=_auth(api_key), timeout=30)
            out["models_status"] = r.status_code
            try:
                out["models_count"] = len(r.json().get("data", []))
            except ValueError:
                out["models_count"] = -1
        except requests.RequestException as e:
            out["models_error"] = str(e)[:100]

    payload = {"model": model, "messages": [{"role": "user", "content": "say hi"}],
               "max_tokens": 5}
    started = time.time()
    try:
        r = requests.post(f"{API_BASE}/chat/completions", headers=_auth(api_key),
                          json=payload, timeout=60)
        out["chat_status"] = r.status_code
        out["chat_ms"] = int((time.time() - started) * 1000)
        out["ratelimit"] = rate_headers(r.headers)
        if r.status_code == 200:
            body = r.json()
            out["content"] = body["choices"][0]["message"].get("content")
            out["usage"] = body.get("usage", {})
        else:
            out["body"] = r.text[:200]
    except requests.RequestException as e:
        out["chat_error"] = f"{type(e).__name__}: {e}"[:120]
    except (ValueError, KeyError, IndexError) as e:
        out["chat_error"] = f"响应解析失败: {e}"[:120]

    return out["chat_status"] == 200, out


def test_reasoning(api_key: str, model: str) -> dict:
    payload = {"model": model,
               "messages": [{"role": "user", "content": "What is 17*23? think step by step"}],
               "reasoning_effort": "high", "max_tokens": 60}
    try:
        r = requests.post(f"{API_BASE}/chat/completions", headers=_auth(api_key),
                          json=payload, timeout=90)
        content = r.json()["choices"][0]["message"].get("content")
        return {"status": r.status_code, "content_type": type(content).__name__,
                "chunk_types": [c.get("type") for c in content if isinstance(c, dict)]
                if isinstance(content, list) else None}
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        return {"error": f"{type(e).__name__}: {e}"[:120]}


def probe_all(keys: list[dict], model: str, workers: int) -> int:
    results: list[tuple[int, str, bool, dict]] = []

    def run(item):
        i, rec = item
        ok, detail = test_one(rec.get("api_key", ""), model, probe_models=False)
        return i, rec.get("email", "?"), ok, detail

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for i, email, ok, detail in ex.map(run, enumerate(keys)):
            results.append((i, email, ok, detail))
            rl = detail.get("ratelimit", {})
            print(f"[{i + 1}/{len(keys)}] {'OK  ' if ok else 'FAIL'} {email}"
                  f"  status={detail.get('chat_status')}"
                  f"  tok={rl.get('remaining_tok', '?')}/{rl.get('limit_tok', '?')}"
                  f"  req={rl.get('remaining_req', '?')}/{rl.get('limit_req', '?')}"
                  f"  {detail.get('chat_ms', '-')}ms", flush=True)

    results.sort()
    bad = [r for r in results if not r[2]]
    print(f"\n[result] 可用 {len(results) - len(bad)} / 失效 {len(bad)} / 共 {len(results)}")
    if bad:
        print("失效账号:")
        for i, email, _, detail in bad:
            reason = detail.get("chat_error") or (detail.get("body") or "")[:80]
            print(f"  [{i}] {email}  status={detail.get('chat_status')}  {reason}")
    return 0 if not bad else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="探测 Mistral key 的限额与可用性")
    p.add_argument("api_key", nargs="?", default=None)
    p.add_argument("--model", default="mistral-small-latest")
    p.add_argument("--key-file", default=DEFAULT_KEYS)
    p.add_argument("--index", type=int, default=0)
    p.add_argument("--all", action="store_true", help="并发探测 key 文件里的全部账号")
    p.add_argument("--workers", type=int, default=8, help="--all 时的并发数")
    p.add_argument("--reasoning", action="store_true", help="额外打印 thinking 格式样例")
    args = p.parse_args(argv)

    if args.api_key:
        api_key, label = args.api_key, "(命令行)"
    else:
        try:
            keys = load_keys(args.key_file)
        except (OSError, json.JSONDecodeError) as e:
            print(f"读不了 {args.key_file}: {e}", file=sys.stderr)
            return 2
        if not keys:
            print(f"{args.key_file} 里没有账号", file=sys.stderr)
            return 2
        if args.all:
            return probe_all(keys, args.model, max(1, args.workers))
        if not 0 <= args.index < len(keys):
            print(f"--index 越界 (共 {len(keys)} 个)", file=sys.stderr)
            return 2
        api_key = keys[args.index].get("api_key", "")
        label = keys[args.index].get("email", "?")

    print(f"testing {label}")
    ok, detail = test_one(api_key, args.model)
    print(json.dumps(detail, ensure_ascii=False, indent=2))
    if args.reasoning:
        print("--- reasoning 样例 ---")
        print(json.dumps(test_reasoning(api_key, args.model), ensure_ascii=False, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
