#!/usr/bin/env python3
"""实现观察的匹配步骤：把 prepare.py 生成的输入交给 DeepSeek，输出符合 schema.json 的结论。

用法：python3 llm.py --input <input.json> --out <result.json>

环境变量：
  DEEPSEEK_API_KEY  必需
  DEEPSEEK_MODEL    可选，默认 deepseek-v4-flash

结论的合法性（PR 是否在输入中、change 是否存在、是否命中被拒组合）由 write.py 校验，这里只保证输出是 JSON 对象。

实现单元较多时分批调用，避免单次输出超出 max_tokens 被截断；每批只带与本批仓库相关或被提示信号指向的候选 change，
避免输入超出上下文。各批结论按 change 合并。
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
# 每批最多的实现单元数与单元 JSON 总长度（字符）。
BATCH_UNITS = 6
BATCH_CHARS = 60000


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
    data = json.loads(Path(args.input).read_text(encoding="utf-8"))
    batches = split_units(data.get("merged") or [])
    changes_chars = len(json.dumps(data.get("changes") or [], ensure_ascii=False))
    print(f"候选 change {len(data.get('changes') or [])} 个（{changes_chars} 字符），实现单元 {len(data.get('merged') or [])} 个，分 {len(batches)} 批匹配")
    results = []
    for index, units in enumerate(batches, 1):
        changes = relevant_changes(data.get("changes") or [], units)
        result = match(system, {**data, "changes": changes, "merged": units}, key, model, f"第 {index}/{len(batches)} 批（候选 {len(changes)} 个）")
        if result is None:
            return 1
        results.append(result)
    merged = settle_archived(merge_results(results), {c.get("change") for c in data.get("changes") or [] if c.get("archived")})
    Path(args.out).write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✓ {model} 返回结论：links {len(merged['links'])}，"
          f"unregistered {len(merged['unregistered'])}，no_draft {len(merged['no_draft'])}")
    return 0


def split_units(units: list[dict]) -> list[list[dict]]:
    """按数量与长度把实现单元分批；没有单元时仍返回一个空批，保持单次调用的行为。"""
    batches: list[list[dict]] = [[]]
    size = 0
    for unit in units:
        length = len(json.dumps(unit, ensure_ascii=False))
        if batches[-1] and (len(batches[-1]) >= BATCH_UNITS or size + length > BATCH_CHARS):
            batches.append([])
            size = 0
        batches[-1].append(unit)
        size += length
    return batches


def unit_repo(unit: dict) -> str:
    """实现单元标识形如 owner/repo#12 或 owner/repo@abc..def。"""
    return unit.get("pr", "").split("#")[0].split("@")[0]


def relevant_changes(changes: list[dict], units: list[dict]) -> list[dict]:
    """涉及本批仓库、被本批提示信号指向，或没有登记涉及仓库的活动 change（已归档且未登记仓库的只在被提示时带上）。"""
    repos = {unit_repo(unit) for unit in units}
    hinted = {hint.get("change") for unit in units for hint in unit.get("hints") or []}
    return [
        c for c in changes
        if repos & set(c.get("repositories") or []) or c.get("change") in hinted or (not c.get("repositories") and not c.get("archived"))
    ]


def merge_results(results: list[dict]) -> dict:
    links: dict[str, dict] = {}
    merged = {"links": [], "unregistered": [], "no_draft": []}
    for result in results:
        for link in result.get("links") or []:
            entry = links.setdefault(link.get("change"), {"change": link.get("change"), "prs": [], "diffs": []})
            entry["prs"] += link.get("prs") or []
            entry["diffs"] += link.get("diffs") or []
        merged["unregistered"] += result.get("unregistered") or []
        merged["no_draft"] += result.get("no_draft") or []
    merged["links"] = list(links.values())
    return merged


def settle_archived(result: dict, archived: set[str]) -> dict:
    """关联到已归档 change 的实现单元归入 no_draft：需求已经归档，不再起草关联 PR。"""
    links = []
    for link in result["links"]:
        if link.get("change") not in archived:
            links.append(link)
            continue
        for pr in link.get("prs") or []:
            result["no_draft"].append({"pr": pr.get("pr"), "reason": f"已归档 change `{link['change']}` 已覆盖：{pr.get('basis', '')}"})
    result["links"] = links
    return result


def match(system: str, data: dict, key: str, model: str, label: str) -> dict | None:
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(data, ensure_ascii=False, indent=2)},
    ]
    last_error = ""
    for attempt in range(3):
        try:
            result = json.loads(call(messages, key, model))
            if isinstance(result, dict):
                return result
            last_error = "返回的不是 JSON 对象"
        except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, TimeoutError) as error:
            last_error = str(error)
        print(f"{label}第 {attempt + 1} 次调用失败：{last_error}", file=sys.stderr)
        time.sleep(5 * (attempt + 1))
    print(f"DeepSeek 调用失败（{label}）：{last_error}", file=sys.stderr)
    return None


if __name__ == "__main__":
    sys.exit(main())
