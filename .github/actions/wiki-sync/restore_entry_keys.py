#!/usr/bin/env python3
"""把 Agent 改写后的页面中的条目标识注释恢复为骨架中的原值。

用法：python3 restore_entry_keys.py <骨架页面> <改写后页面>

迭代记录页的每个条目由「## 标题」与下一行的「<!-- entry: ... -->」组成，二者都由骨架确定、不允许 Agent 改动。
Agent 偶尔会“修正”注释中的标识，导致校验判定条目不在范围内。按标题把注释恢复为骨架原值；标题不在骨架中的条目保持原样，交给校验报告。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ENTRY = re.compile(r"^<!-- entry: .+ -->$")


def entry_comments(lines: list[str]) -> dict[str, str]:
    return {lines[i]: lines[i + 1] for i in range(len(lines) - 1) if lines[i].startswith("## ") and ENTRY.match(lines[i + 1])}


def restore(skeleton: str, page: str) -> tuple[str, int]:
    expected = entry_comments(skeleton.split("\n"))
    lines = page.split("\n")
    fixed = 0
    for i in range(len(lines) - 1):
        want = expected.get(lines[i])
        if want and ENTRY.match(lines[i + 1]) and lines[i + 1] != want:
            lines[i + 1] = want
            fixed += 1
    return "\n".join(lines), fixed


def main() -> int:
    skeleton, page = Path(sys.argv[1]), Path(sys.argv[2])
    text, fixed = restore(skeleton.read_text(encoding="utf-8"), page.read_text(encoding="utf-8"))
    if fixed:
        page.write_text(text, encoding="utf-8")
        print(f"{page.name}：恢复了 {fixed} 处被改动的条目标识")
    return 0


if __name__ == "__main__":
    sys.exit(main())
