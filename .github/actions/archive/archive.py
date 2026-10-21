#!/usr/bin/env python3
"""验收归档：归档 change、合并 delta、写 changelog、置为已归档，并生成归档 PR 正文。

用法：python3 archive.py --store <dir> --store-repo <owner/repo> --change <name> --body <out.md>

环境变量：GH_TOKEN（读取 Store 中已合并的观察 PR，用于汇总尚未处理的差异）

退出码非 0 表示不能归档（原因写在 stdout）。不执行 git 操作。
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from store_lib import GitHubError, dump_yaml, list_store_obs_prs, load_yaml  # noqa: E402

ARCHIVABLE = {"已批准", "已实现"}
SECTION = re.compile(r"^## (ADDED|MODIFIED|REMOVED|RENAMED) Requirements\s*$")
REQUIREMENT = re.compile(r"^### Requirement:\s*(.+?)\s*$")
OP_LABEL = {"ADDED": "新增", "MODIFIED": "修改", "REMOVED": "移除", "RENAMED": "重命名"}


def delta_summary(spec: Path) -> list[str]:
    items, section = [], None
    for line in spec.read_text(encoding="utf-8").splitlines():
        match = SECTION.match(line)
        if match:
            section = match.group(1)
            continue
        match = REQUIREMENT.match(line)
        if match and section:
            items.append(f"- [{OP_LABEL[section]}] 需求：{match.group(1)}")
    return items


def append_changelog(store: Path, capability: str, meta: dict, change: str, items: list[str]) -> None:
    path = store / "openspec" / "specs" / capability / "changelog.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.is_file() else "# Spec Changelog\n"
    entry = (
        f"\n## {meta.get('created') or dt.date.today().isoformat()} · {meta.get('ID') or '-'} · {change}\n\n"
        f"**标题**：{meta.get('title') or change}\n"
        "**操作**：delta spec 同步（验收归档）\n"
        "**摘要**：\n" + "\n".join(items) + "\n"
    )
    path.write_text(text.rstrip("\n") + "\n" + entry, encoding="utf-8")


TBD_PURPOSE = re.compile(r"^TBD - created by archiving change .*$", re.M)


def fill_purposes(store: Path, proposal: str, capabilities: list[str]) -> list[str]:
    """用 proposal「涉及规范」中的说明替换新能力的 TBD Purpose；返回仍需人工补写或扩写的能力。"""
    missing = []
    for capability in capabilities:
        spec = store / "openspec" / "specs" / capability / "spec.md"
        if not spec.is_file():
            continue
        text = spec.read_text(encoding="utf-8")
        if not TBD_PURPOSE.search(text):
            continue
        match = re.search(rf"\*\*(.+?)\*\*（标识 `{re.escape(capability)}`）[：:]\s*(.+)", proposal)
        purpose = f"{match.group(1)}：{match.group(2).strip()}" if match else ""
        if purpose:
            spec.write_text(TBD_PURPOSE.sub(purpose, text, count=1), encoding="utf-8")
        # strict 校验要求 Purpose 至少 50 字符，proposal 的一句话说明常常不够
        if len(purpose) < 50:
            missing.append(capability)
    return missing


def diffs_from_obs(store_repo: str, change: str) -> list[str]:
    token = os.environ.get("GH_TOKEN", "")
    if not token:
        return []
    found = []
    try:
        prs = list_store_obs_prs(store_repo, token)
    except GitHubError as error:
        return [f"- （读取观察 PR 失败：{error}）"]
    for pr in prs:
        if not pr["merged"] or pr["marker"].get("change") != change:
            continue
        body = subprocess.run(["gh", "pr", "view", str(pr["number"]), "--repo", store_repo, "--json", "body", "--jq", ".body"],
                              capture_output=True, text=True).stdout
        for block in re.findall(r"## 实现与需求的差异\n(.*?)(?=\n## |\Z)", body, re.S):
            lines = [l for l in block.strip().splitlines() if l.startswith("- ")]
            found += [f"{l}（来自 {pr['url']}）" for l in lines]
    return found


def main() -> int:
    parser = argparse.ArgumentParser()
    for name in ("--store", "--store-repo", "--change", "--body"):
        parser.add_argument(name, required=True)
    args = parser.parse_args()
    store = Path(args.store).resolve()
    change_dir = store / "openspec" / "changes" / args.change
    meta = load_yaml(change_dir / ".openspec.yaml")
    if not change_dir.is_dir():
        print(f"不能归档：活动 change 中没有 `{args.change}`")
        return 1
    if meta.get("status") not in ARCHIVABLE:
        print(f"不能归档：`{args.change}` 的状态是「{meta.get('status') or '空'}」，只有已批准或已实现的 change 可以验收归档")
        return 1

    specs = sorted((change_dir / "specs").glob("*/spec.md")) if (change_dir / "specs").is_dir() else []
    summaries = {spec.parent.name: delta_summary(spec) for spec in specs}
    diffs = diffs_from_obs(args.store_repo, args.change)
    proposal = (change_dir / "proposal.md").read_text(encoding="utf-8") if (change_dir / "proposal.md").is_file() else ""

    result = subprocess.run(["openspec", "archive", args.change, "--yes"], cwd=store, capture_output=True, text=True)
    if result.returncode != 0:
        print("openspec archive 失败：\n" + (result.stdout + result.stderr)[-2000:])
        return 1
    archived = sorted((store / "openspec" / "changes" / "archive").glob(f"*-{args.change}"))
    if not archived:
        print("openspec archive 未产生归档目录")
        return 1
    archived_dir = archived[-1]
    archived_meta = load_yaml(archived_dir / ".openspec.yaml")
    archived_meta["status"] = "已归档"
    (archived_dir / ".openspec.yaml").write_text(dump_yaml(archived_meta), encoding="utf-8")
    for capability, items in summaries.items():
        if items:
            append_changelog(store, capability, archived_meta, args.change, items)

    missing_purpose = fill_purposes(store, proposal, list(summaries))

    impls = [i for i in archived_meta.get("implementations") or [] if isinstance(i, dict)]
    impl_lines = "\n".join(f"- [{i.get('pr')}]({i.get('url')})：{i.get('basis') or ''}（置信度 {i.get('confidence') or '-'}）" for i in impls)
    spec_lines = "\n".join(f"**{cap}**\n" + "\n".join(items) for cap, items in summaries.items() if items) or "本 change 没有 delta spec。"
    body = f"""## 摘要
需求侧验收通过，归档 `{args.change}`（{archived_meta.get('title') or ''}）。合并本 PR 后，delta 并入主 specs，change 状态为已归档。

## 主 specs 改动
{spec_lines}

{"**需要补写 Purpose**：" + "、".join(f"`openspec/specs/{c}/spec.md`" for c in missing_purpose) + " 是新能力，proposal 中没有它的说明或说明不足 50 字，请在本 PR 中补写 Purpose，否则 Store 检查不通过。" + chr(10) if missing_purpose else ""}
## 关联的实现 PR
{impl_lines or '未观察到关联的实现 PR（允许直接归档）。'}

## 尚未处理的差异
{chr(10).join(diffs) if diffs else '观察中没有记录差异。'}

请逐条确认上述差异是否接受；需要调整需求时，在本 PR 中直接修改对应的主 spec。确认后点击 Ready for review 运行 Store 检查，通过后合并。

## changelog 预览
{chr(10).join(f'- `openspec/specs/{cap}/changelog.md` 追加一条' for cap, items in summaries.items() if items) or '无'}
"""
    Path(args.body).write_text(body, encoding="utf-8")
    print(f"已归档到 {archived_dir.relative_to(store)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
