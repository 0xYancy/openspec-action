#!/usr/bin/env python3
"""把 Agent 改写后的页面中的条目标识注释与条目出处行恢复为骨架中的原值。

用法：python3 restore_entry_keys.py <骨架页面> <改写后页面>

迭代记录页的每个条目由「## 标题」、下一行的「<!-- entry: ... -->」和末尾的「来源：[[change:...]]」组成，三者都由骨架确定、不允许 Agent 改动。
Agent 偶尔会“修正”注释中的标识或改坏出处写法，导致校验失败。按标题把注释与出处行恢复为骨架原值；标题不在骨架中的条目保持原样，交给校验报告。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ENTRY = re.compile(r"^<!-- entry: .+ -->$")
SOURCE = "来源："


def entry_comments(lines: list[str]) -> dict[str, str]:
    return {lines[i]: lines[i + 1] for i in range(len(lines) - 1) if lines[i].startswith("## ") and ENTRY.match(lines[i + 1])}


def entry_sources(lines: list[str]) -> dict[str, str]:
    """每个条目标题 → 条目内的出处行。"""
    sources, title = {}, None
    for line in lines:
        if line.startswith("## "):
            title = line
        elif title and line.startswith(SOURCE):
            sources[title] = line
    return sources


def restore(skeleton: str, page: str) -> tuple[str, int]:
    skeleton_lines = skeleton.split("\n")
    expected, sources = entry_comments(skeleton_lines), entry_sources(skeleton_lines)
    lines = page.split("\n")
    fixed, title = 0, None
    for i, line in enumerate(lines):
        if line.startswith("## "):
            title = line
            want = expected.get(line)
            if want and i + 1 < len(lines) and ENTRY.match(lines[i + 1]) and lines[i + 1] != want:
                lines[i + 1] = want
                fixed += 1
        # 只恢复迭代条目（骨架中带条目标识的标题）的出处；概览页章节的出处由 Agent 撰写
        elif title in expected and line.startswith(SOURCE) and title in sources and line != sources[title]:
            lines[i] = sources[title]
            fixed += 1
    return "\n".join(lines), fixed


def main() -> int:
    skeleton, page = Path(sys.argv[1]), Path(sys.argv[2])
    text, fixed = restore(skeleton.read_text(encoding="utf-8"), page.read_text(encoding="utf-8"))
    if fixed:
        page.write_text(text, encoding="utf-8")
        print(f"{page.name}：恢复了 {fixed} 处被改动的条目标识或出处")
    return 0


if __name__ == "__main__":
    sys.exit(main())
