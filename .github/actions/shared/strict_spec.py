#!/usr/bin/env python3
"""对单个主 spec 做 openspec 严格校验，但「Purpose 少于 50 个字符」只作提醒。

中文 Purpose 常常不足 50 个字符也已完整明确。Purpose 缺失或为空（ERROR）、仍是 TBD/TODO 占位（WARNING）
以及其余警告照常失败。用法：strict_spec.py <capability>（在 Store 根目录运行）。
退出码 0 为通过；输出逐行列出问题，提醒以「提醒：」开头。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

BRIEF = "Purpose section is too brief"


def check(capability: str, cwd: str = ".") -> tuple[bool, list[str], list[str]]:
    """返回 (是否通过, 失败原因, 提醒)。"""
    proc = subprocess.run(["openspec", "validate", capability, "--type", "spec", "--strict", "--json"],
                          cwd=cwd, capture_output=True, text=True, env={**os.environ, "OPENSPEC_TELEMETRY": "0"})
    try:
        items = json.loads(proc.stdout)["items"]
    except (ValueError, KeyError):
        return False, [(proc.stdout + proc.stderr).strip()[-300:] or "openspec validate 无输出"], []
    failures, notes = [], []
    for item in items:
        for issue in item.get("issues") or []:
            text = f"{item.get('id')}: [{issue.get('level')}] {issue.get('message')}"
            if issue.get("level") == "WARNING" and str(issue.get("message", "")).startswith(BRIEF):
                notes.append(f"提醒：{item.get('id')} 的 Purpose 少于 50 个字符，内容完整时可以忽略")
            elif issue.get("level") in ("ERROR", "WARNING"):
                failures.append(text)
    return not failures, failures, notes


def main() -> int:
    ok, failures, notes = check(sys.argv[1])
    print("\n".join(failures + notes) or f"{sys.argv[1]}: 通过")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
