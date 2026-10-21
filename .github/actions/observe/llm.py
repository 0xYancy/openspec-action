#!/usr/bin/env python3
"""实现观察的匹配步骤：把 prepare.py 生成的输入交给 DeepSeek，输出符合 schema.json 的结论。

用法：python3 llm.py --input <input.json> --out <result.json>

环境变量：
  DEEPSEEK_API_KEY  必需
  DEEPSEEK_MODEL    可选，默认 deepseek-v4-flash

结论的合法性（PR 是否在输入中、change 是否存在、是否命中被拒组合）由 write.py 校验，这里只保证输出是 JSON 对象。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ENDPOINT = "https://api.deepseek.com/chat/completions"


def call(messages: list[dict], key: str, model: str) -> str:
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": 0,
            "max_tokens": 8192,
        }
    ).encode()
    request = urllib.request.Request(
        ENDPOINT, data=body, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read())["choices"][0]["message"]["content"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if not key:
        print("缺少 DEEPSEEK_API_KEY", file=sys.stderr)
        return 1
    model = os.environ.get("DEEPSEEK_MODEL") or "deepseek-v4-flash"
    system = (
        (HERE / "prompt.md").read_text(encoding="utf-8")
        + "\n\n只输出一个 JSON 对象，结构必须符合以下 JSON schema：\n"
        + (HERE / "schema.json").read_text(encoding="utf-8")
    )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": Path(args.input).read_text(encoding="utf-8")},
    ]

    last_error = ""
    for attempt in range(3):
        try:
            content = call(messages, key, model)
            result = json.loads(content)
            if isinstance(result, dict):
                Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"✓ {model} 返回结论：links {len(result.get('links') or [])}，"
                      f"unregistered {len(result.get('unregistered') or [])}，no_draft {len(result.get('no_draft') or [])}")
                return 0
            last_error = "返回的不是 JSON 对象"
        except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, TimeoutError) as error:
            last_error = str(error)
        print(f"第 {attempt + 1} 次调用失败：{last_error}", file=sys.stderr)
        time.sleep(5 * (attempt + 1))
    print(f"DeepSeek 调用失败：{last_error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
