#!/usr/bin/env python3
"""Store 的 PR 检查：需求状态约束、元数据字段、受保护文件。

用法：python3 check.py --store <dir> --base-ref <origin/main> --author <PR 作者>

- 主分支上的活动 change 只能是已批准或已实现（Store 已取消草稿状态）；
- 归档目录中的 change 必须是已归档；
- 不允许出现执行类字段（progress、estimate、actual_days）；
- 自动化（机器人）提交的 PR 不得改动受保护文件。
结果写入 stdout（Markdown），有问题时退出码为 1。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from store_lib import load_changes, path_allowed  # noqa: E402

ACTIVE_OK = {"已批准", "已实现"}
FORBIDDEN_FIELDS = ("progress", "estimate", "actual_days")
BOTS = ("github-actions[bot]", "app/github-actions")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", required=True)
    parser.add_argument("--base-ref", required=True)
    parser.add_argument("--author", default="")
    args = parser.parse_args()
    store = Path(args.store).resolve()

    changed = subprocess.run(
        ["git", "-C", str(store), "diff", "--name-only", f"{args.base_ref}...HEAD"], capture_output=True, text=True, check=True
    ).stdout.split()
    touched = {p.split("/")[2] for p in changed if p.startswith("openspec/changes/") and p.count("/") >= 3 and p.split("/")[2] != "archive"}
    touched_archive = {p.split("/")[3] for p in changed if p.startswith("openspec/changes/archive/") and p.count("/") >= 4}

    problems: list[str] = []
    for change in load_changes(store):
        name = Path(change["dir"]).name
        if change["archived"]:
            if name in touched_archive and change["status"] != "已归档":
                problems.append(f"`{change['dir']}`：归档目录中的 change 状态应为「已归档」，当前为「{change['status'] or '空'}」")
        elif change["status"] not in ACTIVE_OK:
            hint = "；Store 已取消草稿状态，请改为「已批准」" if change["status"] == "草稿" else ""
            problems.append(f"`{change['dir']}`：状态「{change['status'] or '空'}」不能合并进主分支{hint}")
        if name in touched or name in touched_archive:
            bad = [f for f in FORBIDDEN_FIELDS if f in change["meta"]]
            if bad:
                problems.append(f"`{change['dir']}`：Store 中的 change 不维护执行类字段 {', '.join(bad)}")
    if args.author in BOTS:
        blocked = [p for p in changed if not path_allowed(p)]
        if blocked:
            problems.append("自动化 PR 改动了受保护文件：" + "、".join(f"`{p}`" for p in blocked))

    if problems:
        print("### Store 检查未通过\n\n" + "\n".join(f"- {p}" for p in problems))
        return 1
    print("### Store 检查通过\n\n需求状态与元数据符合约束。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
