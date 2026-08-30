#!/usr/bin/env python3
"""TTFB 实测脚本（D3 调研）：真实调用 commandcode API，测不同 prompt 大小的首字延迟。

用法:
  cd /Users/andyhsu/codehouse/MuMuAINovel
  backend/.venv/bin/python scripts/ttfb_probe.py            # 默认 4 档
  backend/.venv/bin/python scripts/ttfb_probe.py --sizes 50000,200000,500000,800000
  backend/.venv/bin/python scripts/ttfb_probe.py --stream   # 流式测 TTFB（推荐，模拟真实生成）

安全: 输出只含延迟/状态码/错误类别，不含任何内容与密钥。
"""
import argparse
import json
import os
import time
import urllib.request
import urllib.error
import sys

BASE_URL = "https://api.commandcode.ai/provider/v1/chat/completions"
API_KEY = os.environ.get("COMMAND_CODE_API_KEY", "")
MODEL = "deepseek/deepseek-v4-flash"

def build_prompt(chars: int) -> str:
    """生成指定字符数的中文 prompt（占位文本，模拟全书上下文）。"""
    unit = "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏闰余成岁律吕调阳云腾致雨露结为霜。"
    repeat = chars // len(unit) + 1
    return (unit * repeat)[:chars]

def make_request(prompt: str, stream: bool):
    """发起请求，返回 (ttfb_ms, status, err_kind, total_ms)。"""
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 16,  # 只要首 token，输出最小
        "stream": stream,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {API_KEY}",
        "User-Agent": "curl/8.0 (TTFB-probe)",  # 网关可能拦截 urllib 默认 UA
    }
    data = json.dumps(payload).encode("utf-8")
    start = time.monotonic()
    req = urllib.request.Request(BASE_URL, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            first_byte = time.monotonic()
            ttfb = (first_byte - start) * 1000
            if stream:
                # 读第一个 chunk 才算真正 TTFB
                chunk = resp.read(1)
                ttfb = (time.monotonic() - start) * 1000
            total = (time.monotonic() - start) * 1000
            return ttfb, resp.status, None, total
    except urllib.error.HTTPError as e:
        return (time.monotonic() - start) * 1000, e.code, f"HTTP {e.code}", (time.monotonic() - start) * 1000
    except Exception as e:
        return (time.monotonic() - start) * 1000, None, type(e).__name__, (time.monotonic() - start) * 1000

def main():
    parser = argparse.ArgumentParser(description="TTFB 实测（D3）")
    parser.add_argument("--sizes", default="50000,200000,500000,800000", help="prompt 字符数，逗号分隔")
    parser.add_argument("--stream", action="store_true", help="流式模式（默认非流式）")
    parser.add_argument("--runs", type=int, default=1, help="每档重复次数")
    args = parser.parse_args()

    if not API_KEY:
        print("❌ 未找到 COMMAND_CODE_API_KEY 环境变量（应来自 ~/.zshrc）")
        sys.exit(1)

    sizes = [int(x) for x in args.sizes.split(",")]
    mode = "流式" if args.stream else "非流式"
    print(f"=== TTFB 实测（{mode}）model={MODEL} ===")
    print(f"{'prompt字符':>10} | {'TTFB(ms)':>10} | {'状态':>6} | 备注")
    print("-" * 60)

    for chars in sizes:
        prompt = build_prompt(chars)
        for run in range(args.runs):
            ttfb, status, err, total = make_request(prompt, args.stream)
            note = f"err={err}" if err else (f"共{total:.0f}ms" if args.stream else "")
            print(f"{chars:>10} | {ttfb:>10.0f} | {str(status):>6} | {note}")

    print("\n✅ 完成")

if __name__ == "__main__":
    main()
