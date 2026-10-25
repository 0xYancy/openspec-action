#!/usr/bin/env python3
"""实现观察的受限写入步骤：校验 模型结论，只在 Store 开或更新草稿 PR。

用法：python3 write.py --store <dir> --store-repo <owner/repo> --base <主分支> --input <input.json>
                       --result <result.json> --state <state.json> --report <report.md> [--dry-run]

环境变量：GH_TOKEN（Store 的 GITHUB_TOKEN，只用于 git push 与 gh pr）；
         DEEPSEEK_API_KEY / DEEPSEEK_MODEL（为未登记的行为变化起草主 spec 改动）

- 只修改 openspec/changes/** 与 openspec/specs/**；任何其他路径的改动都会被撤销并在报告中列出。
- 未登记的行为变化写进 Store 唯一打开的补登汇总 PR（obs:patch）：按顺序基于汇总分支当前的主 spec 起草，
  delta 直接并入主 spec 并写 changelog，每个单元一个提交，PR 中不含 change；起草失败的单元单独开空壳补登 PR。
- 从不合并 PR，从不推送主分支。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "archive"))
from store_lib import dump_yaml, guarded_paths, load_yaml, parse_marker, render_marker, revert_paths  # noqa: E402
from archive import TBD_PURPOSE, changelog_path, delta_summary  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from draft import draft_one, load_capabilities  # noqa: E402

LABEL = "obs"
REJECTED_LABEL = "obs:rejected"
ARCHIVE_LABEL = "obs:archive"
REDO_LABEL = "obs:redo"
PATCH_LABEL = "obs:patch"


def run(store: Path, *args: str, check: bool = True, input_text: str | None = None) -> str:
    return subprocess.run(args, cwd=store, capture_output=True, text=True, check=check, input=input_text).stdout.strip()


def validate(result: dict, data: dict) -> tuple[dict, list[str]]:
    """丢弃引用不存在的 PR / change、被拒绝过的组合，以及重复出现的 PR。

    一个实现单元可以关联多个 change（直接推送的提交批次常同时实现几个需求），但同一组合只保留一次；
    已关联的单元不再出现在 unregistered / no_draft 中。
    """
    notes: list[str] = []
    merged = {item["pr"]: item for item in data["merged"]}
    changes = {item["change"]: item for item in data["changes"]}
    rejected = {(r["pr"], r.get("change")) for r in data.get("rejected") or []}
    seen: set[str] = set()
    paired: set[tuple[str, str]] = set()
    links: dict[str, dict] = {}
    for link in result.get("links") or []:
        name = link.get("change")
        if name not in changes:
            notes.append(f"忽略：模型关联了不存在或不可关联的 change `{name}`")
            continue
        for pr in link.get("prs") or []:
            ref = pr.get("pr")
            if ref not in merged:
                notes.append(f"忽略：{ref} 不在本次待匹配的 PR 中")
            elif (ref, name) in rejected:
                notes.append(f"忽略：{ref} 与 `{name}` 的关联已被需求侧拒绝")
            elif (ref, name) in paired:
                notes.append(f"忽略：{ref} 与 `{name}` 的关联重复")
            else:
                seen.add(ref)
                paired.add((ref, name))
                links.setdefault(name, {"change": name, "prs": [], "diffs": []})["prs"].append({**pr, **{k: merged[ref][k] for k in ("url", "title", "merged_at")}})
        target = links.get(name)
        if target:
            kept = {p["pr"] for p in target["prs"]}
            target["diffs"] += [d for d in link.get("diffs") or [] if d.get("pr") in kept]
    unregistered = []
    for item in result.get("unregistered") or []:
        ref = item.get("pr")
        if ref in merged and ref not in seen:
            seen.add(ref)
            unregistered.append({**item, **{k: merged[ref][k] for k in ("url", "title", "merged_at")}})
    no_draft = []
    for item in result.get("no_draft") or []:
        ref = item.get("pr")
        if ref in merged and ref not in seen:
            seen.add(ref)
            no_draft.append(item)
    for ref in merged:
        if ref not in seen:
            notes.append(f"{ref} 未被模型归类，下次运行重新判断")
    return {"links": list(links.values()), "unregistered": unregistered, "no_draft": no_draft}, notes


def link_body(change: dict, link: dict, store_repo: str) -> str:
    rows = "\n".join(
        f"| [{p['pr']}]({p['url']}) {p['title']} | {p['merged_at'][:10]} | {'提示信号：' if p.get('hint') else '推断：'}{p['basis']} | {p['confidence']} |"
        for p in link["prs"]
    )
    diffs = "\n".join(f"- **{d['scenario']}**（{d['pr']}）：{d['detail']}" for d in link["diffs"]) or "未发现差异。"
    return f"""## 摘要
观察到 {len(link['prs'])} 个已合并 PR 实现了 `{change['change']}`（{change.get('title') or ''}）。合并本 PR 后，change 状态改为已实现，关联记录写入 `implementations`。

## 关联的实现 PR
| PR | 合并时间 | 匹配依据 | 置信度 |
|---|---|---|---|
{rows}

## 实现与需求的差异
{diffs}

## 审阅提示
- 置信度为「低」的关联请重点确认。
- 审阅后点击 Ready for review 运行 Store 检查，检查通过后再合并。
- 匹配错误时关闭本 PR 并加上 `{REJECTED_LABEL}` 标签，之后不会再提出相同的关联。
- 已经验证过、不需要再测试时，加上 `{ARCHIVE_LABEL}` 标签：本分支会追加归档提交，合并本 PR 即直接完成验收归档。
"""


def unregistered_body(item: dict, fallback_reason: str = "") -> str:
    return f"""## 摘要
[{item['pr']}]({item['url']}) {item['title']} 改变了产品行为，但没有关联到任何已批准的 change。

## 被改变的行为
{item['behavior']}

## 建议
{item['suggestion']}

{f"未能自动更新主 spec（{fallback_reason}），" if fallback_reason else ""}本 PR 预置了一个补登 change：代码已合入，状态预填为已实现，实现记录已写入 `implementations`。补写 proposal、specs 与负责人等元数据，按需改名 change 目录，点击 Ready for review 通过 Store 检查后即可合并；若这是已知行为或无需登记，关闭本 PR 并加上 `{REJECTED_LABEL}` 标签。
"""


def patch_header(note: str = "") -> str:
    return f"""## 补登汇总
实现观察发现以下实现改变了产品行为，但没有关联到任何已批准的 change。本 PR 已按这些实现直接更新主 spec，每个实现单元一个提交；不经过 change 流程，也不进 Notion，合并即生效。
{chr(10) + note + chr(10) if note else ""}
## 审阅提示
- 在 Files changed 中核对主 spec 的改动是否与已上线的行为一致；需要调整时直接在本分支修改 `openspec/specs/`。
- 某个单元不需要写进规范时，在本分支回退它对应的提交，之后不会再提出。
- 整个 PR 都不需要时，关闭并加上 `{REJECTED_LABEL}` 标签。
- 之后的观察会继续追加到本 PR；追加时若本 PR 已标记就绪，会改回草稿，确认后重新点击 Ready for review。

## 补登
"""


def unit_section(item: dict, draft: dict, summaries: dict[str, list[str]]) -> str:
    specs = "\n".join(f"- **{cap}**：" + "；".join(line.lstrip("- ") for line in lines) for cap, lines in summaries.items())
    return f"""
### [{item['pr']}]({item['url']}) {draft.get('title') or item['title']}
**被改变的行为**：{item['behavior']}

**主 spec 改动**：{draft.get('summary') or ''}
{specs}
"""


def merge_draft(store: Path, item: dict, draft: dict, now: dt.datetime) -> tuple[dict[str, list[str]], str]:
    """把起草的 delta 并入主 spec：借用 openspec archive 合并临时 change，再删除临时 change，只保留主 spec 改动，
    并为每个能力追加一条补登 changelog。返回 (能力 → 改动摘要, 失败原因)。"""
    name = f"obs-patch-{ref_suffix(item['pr'])}"
    directory = store / "openspec" / "changes" / name
    (directory / "specs").mkdir(parents=True, exist_ok=True)
    (directory / ".openspec.yaml").write_text(dump_yaml({"schema": "store", "status": "已实现"}), encoding="utf-8")
    (directory / "proposal.md").write_text(f"## 说明\n\n{draft.get('summary') or item['behavior']}\n", encoding="utf-8")
    summaries = {}
    for delta in draft["deltas"]:
        spec = directory / "specs" / delta["capability"] / "spec.md"
        spec.parent.mkdir(parents=True, exist_ok=True)
        spec.write_text(delta["markdown"], encoding="utf-8")
        summaries[delta["capability"]] = delta_summary(spec)
    result = subprocess.run(["openspec", "archive", name, "--yes"], cwd=store, capture_output=True, text=True)
    for leftover in [directory, *(store / "openspec" / "changes" / "archive").glob(f"*-{name}")]:
        if leftover.exists():
            subprocess.run(["rm", "-rf", str(leftover)], check=True)
    if result.returncode != 0:
        return summaries, "合并 delta 失败：" + (result.stdout + result.stderr).strip()[-300:]
    for delta in draft["deltas"]:
        capability = delta["capability"]
        spec = store / "openspec" / "specs" / capability / "spec.md"
        if delta["purpose"] and spec.is_file():
            spec.write_text(TBD_PURPOSE.sub(delta["purpose"], spec.read_text(encoding="utf-8"), count=1), encoding="utf-8")
        path = changelog_path(spec.parent)
        text = path.read_text(encoding="utf-8") if path.is_file() else "# Spec Changelog\n"
        entry = (f"\n## {now.date().isoformat()} · 补登 · {item['pr']}\n\n"
                 f"**标题**：{draft.get('title') or item['title']}\n"
                 f"**操作**：实现观察补登（{item['url']}）\n"
                 "**摘要**：\n" + "\n".join(summaries[capability]) + "\n")
        path.write_text(text.rstrip("\n") + "\n" + entry, encoding="utf-8")
        check = subprocess.run(["openspec", "validate", capability, "--type", "spec", "--strict"], cwd=store, capture_output=True, text=True)
        if check.returncode != 0:
            return summaries, f"`{capability}` 合并后未通过严格校验：" + (check.stdout + check.stderr).strip()[-300:]
    return summaries, ""


def ensure_labels(store: Path) -> None:
    for name, color, desc in ((LABEL, "0e8a16", "实现观察起草的 PR"), (REJECTED_LABEL, "b60205", "需求侧拒绝的观察匹配"),
                              (ARCHIVE_LABEL, "5319e7", "合并本 PR 时直接完成验收归档"),
                              (REDO_LABEL, "fbca04", "关闭后按当前流程重新观察其中的实现"),
                              (PATCH_LABEL, "1d76db", "补登汇总：未登记的行为变化直接更新主 spec")):
        run(store, "gh", "label", "create", name, "--color", color, "--description", desc, "--force", check=False)


def start_branch(store: Path, base: str, branch: str, existing: bool) -> None:
    if existing:
        run(store, "git", "fetch", "origin", branch)
        run(store, "git", "checkout", "-B", branch, f"origin/{branch}")
    else:
        run(store, "git", "checkout", "-B", branch, f"origin/{base}")


def commit_and_push(store: Path, branch: str, message: str) -> list[str]:
    allowed, denied = guarded_paths(store)
    if denied:
        revert_paths(store, denied)
    if not allowed:
        return denied
    run(store, "git", "add", "--", *allowed)
    run(store, "git", "commit", "-m", message)
    # obs/* 分支只由自动化写入；上次开 PR 失败时残留的同名分支直接覆盖
    run(store, "git", "push", "--force", "origin", f"HEAD:refs/heads/{branch}")
    return denied


def apply_link(store: Path, args, change: dict, link: dict, open_draft: dict | None, now: dt.datetime) -> str:
    meta_path = store / "openspec" / "changes" / change["change"] / ".openspec.yaml"
    branch = open_draft["branch"] if open_draft else f"obs/{change['change']}-{now:%Y%m%d%H%M}"
    start_branch(store, args.base, branch, bool(open_draft))
    meta = load_yaml(meta_path)
    if meta.get("status") == "已批准":
        meta["status"] = "已实现"
    impls = meta.get("implementations") or []
    known = {i.get("pr") for i in impls if isinstance(i, dict)}
    for p in link["prs"]:
        if p["pr"] not in known:
            impls.append({"pr": p["pr"], "url": p["url"], "merged_at": p["merged_at"], "basis": p["basis"], "hint": bool(p.get("hint")), "confidence": p["confidence"]})
    meta["implementations"] = impls
    meta_path.write_text(dump_yaml(meta), encoding="utf-8")
    denied = commit_and_push(store, branch, f"obs: {change['change']} 关联 {len(link['prs'])} 个实现 PR")
    refs = [p["pr"] for p in link["prs"]]
    if open_draft:
        body = run(store, "gh", "pr", "view", str(open_draft["number"]), "--json", "body", "--jq", ".body")
        marker = parse_marker(body) or {"change": change["change"], "prs": []}
        marker["prs"] = sorted(set(marker.get("prs") or []) | set(refs))
        body = re.sub(r"<!--\s*openspec-obs:.*?-->", "", body, flags=re.S).rstrip()
        body += f"\n\n## 追加（{now:%Y-%m-%d %H:%M} UTC）\n\n" + link_body(change, link, args.store_repo) + "\n" + render_marker(marker)
        run(store, "gh", "pr", "edit", str(open_draft["number"]), "--body-file", "-", input_text=body)
        url = open_draft["url"]
    else:
        body = link_body(change, link, args.store_repo) + "\n" + render_marker({"change": change["change"], "prs": refs})
        url = run(store, "gh", "pr", "create", "--draft", "--base", args.base, "--head", branch, "--label", LABEL,
                  "--title", f"obs: {change.get('title') or change['change']} 关联 {len(refs)} 个实现 PR", "--body-file", "-", input_text=body)
    note = f"- `{change['change']}`：{', '.join(refs)} → {url}"
    if denied:
        note += f"（已拒绝写入：{', '.join(denied)}）"
    return note


def apply_draft_comment(store: Path, change: dict, link: dict) -> str:
    """change 还不在主分支（所在 PR 未合并）：只在该 PR 上评论提示，不改需求内容与状态。"""
    rows = "\n".join(
        f"| [{p['pr']}]({p['url']}) {p['title']} | {p['merged_at'][:10]} | {'提示信号：' if p.get('hint') else '推断：'}{p['basis']} | {p['confidence']} |"
        for p in link["prs"]
    )
    diffs = "\n".join(f"- **{d['scenario']}**（{d['pr']}）：{d['detail']}" for d in link["diffs"]) or "未发现差异。"
    refs = [p["pr"] for p in link["prs"]]
    body = f"""## 实现观察：发现可能对应本需求的实现

`{change['change']}` 还未合并进主分支，实现观察不会修改它。以下实现已合入观察分支，可能就是本需求的实现，供确认需求时参考；合并后，观察会按正常流程关联。

| 实现 | 时间 | 匹配依据 | 置信度 |
|---|---|---|---|
{rows}

### 实现与需求的差异
{diffs}

{render_marker({"change": change["change"], "prs": refs})}
"""
    number = change["draft_pr"]["number"]
    run(store, "gh", "pr", "comment", str(number), "--body-file", "-", input_text=body)
    return f"- `{change['change']}`（未合并）：{', '.join(refs)} → 已在 {change['draft_pr']['url']} 评论提示"


def ref_repo(ref: str) -> str:
    """实现单元标识所在仓库：`owner/repo#12`（PR）或 `owner/repo@abc1234..def5678`（直接推送的提交）。"""
    return re.split(r"[#@]", ref, maxsplit=1)[0]


def ref_suffix(ref: str) -> str:
    """用于分支与 change 名：PR 编号，或提交区间末个提交的短 SHA。"""
    tail = re.split(r"[#@]", ref, maxsplit=1)[1]
    return tail.split("..")[-1].lower()


def new_change_id(store: Path, now: dt.datetime, used: set[str]) -> str:
    """生成 Store 内唯一的 change ID（YYYYMMDD-xxxx）。Notion 按 ID 认任务，重复会让多个 change 写进同一条任务。"""
    taken = set(used)
    for meta_path in (store / "openspec" / "changes").glob("**/.openspec.yaml"):
        value = (load_yaml(meta_path) or {}).get("ID")
        if value:
            taken.add(str(value))
    while True:
        candidate = f"{now:%Y%m%d}-{secrets.token_hex(2)}"
        if candidate not in taken:
            used.add(candidate)
            return candidate


def prepare_patch_branch(store: Path, args, patch_pr: dict | None, now: dt.datetime) -> tuple[str, dict | None, str]:
    """检出补登汇总分支：已有汇总 PR 时把它的提交重放到最新主分支上；重放冲突时不动原 PR，另起新分支。
    返回 (分支, 沿用的汇总 PR 或 None, 说明)。"""
    fresh = f"obs/patch-{now:%Y%m%d%H%M}"
    if not patch_pr:
        start_branch(store, args.base, fresh, False)
        return fresh, None, ""
    branch = patch_pr["branch"]
    run(store, "git", "fetch", "origin", branch)
    commits = run(store, "git", "rev-list", "--reverse", "--no-merges", f"origin/{args.base}..origin/{branch}").split()
    run(store, "git", "checkout", "-B", branch, f"origin/{args.base}")
    for commit in commits:
        picked = subprocess.run(["git", "cherry-pick", "--keep-redundant-commits", commit], cwd=store, capture_output=True, text=True)
        if picked.returncode != 0:
            subprocess.run(["git", "cherry-pick", "--abort"], cwd=store, capture_output=True)
            start_branch(store, args.base, fresh, False)
            return fresh, None, (f"> 原补登汇总 PR #{patch_pr['number']} 的提交无法重放到最新主分支（与主分支的改动冲突），"
                                 "本次补登另开在本 PR。请先处理原 PR。")
    return branch, patch_pr, ""


def apply_patch_rollup(store: Path, args, items: list[dict], units: dict[str, dict], patch_pr: dict | None,
                       now: dt.datetime) -> tuple[list[str], list[tuple[dict, str]]]:
    """按顺序把未登记的行为变化补登进汇总分支。返回 (报告行, [(退回空壳补登的单元, 原因)])。"""
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    model = os.environ.get("DEEPSEEK_MODEL") or "deepseek-v4-flash"
    if not key:
        return [], [(item, "缺少 DEEPSEEK_API_KEY") for item in items]
    branch, pr, note = prepare_patch_branch(store, args, patch_pr, now)
    applied, fallbacks = [], []
    for item in items:
        try:
            # 基于汇总分支当前的主 spec 起草：前面单元的补登已经在其中，MODIFIED 不会覆盖它们
            draft = draft_one(item, units.get(item["pr"]) or {}, load_capabilities(store), key, model)
        except Exception as error:  # noqa: BLE001 —— 起草失败只退回空壳补登，不中断观察
            draft = {"error": f"起草失败：{error}"[:300]}
        reason = draft.get("error", "")
        if not reason:
            summaries, reason = merge_draft(store, item, draft, now)
        if reason:
            run(store, "git", "reset", "-q", "--hard")
            run(store, "git", "clean", "-fdq", "--", "openspec")
            fallbacks.append((item, reason))
            continue
        allowed, denied = guarded_paths(store)
        if denied:
            revert_paths(store, denied)
        run(store, "git", "add", "--", *allowed)
        run(store, "git", "commit", "-m", f"obs: 补登 {item['pr']}：{draft.get('title') or item['title']}"[:120])
        applied.append((item, draft, summaries))
    if not applied:
        return [], fallbacks
    # obs/* 分支只由自动化写入；重放到最新主分支后需要覆盖
    run(store, "git", "push", "--force", "origin", f"HEAD:refs/heads/{branch}")
    sections = "".join(unit_section(item, draft, summaries) for item, draft, summaries in applied)
    refs = [item["pr"] for item, _, _ in applied]
    if pr:
        view = json.loads(run(store, "gh", "pr", "view", str(pr["number"]), "--json", "body,isDraft"))
        body = re.sub(r"<!--\s*openspec-obs:.*?-->\s*", "", view["body"] or "", flags=re.S).rstrip() + "\n" + sections
        body += "\n" + render_marker({"change": None, "kind": "patch", "prs": list(pr.get("prs") or []) + refs})
        run(store, "gh", "pr", "edit", str(pr["number"]), "--body-file", "-", input_text=body)
        if not view.get("isDraft"):
            run(store, "gh", "pr", "ready", str(pr["number"]), "--undo")
            run(store, "gh", "pr", "comment", str(pr["number"]), "--body",
                f"实现观察追加了 {len(applied)} 条补登，本 PR 已改回草稿；确认新增内容后重新点击 Ready for review。")
        url = pr["url"]
    else:
        body = patch_header(note) + sections + "\n" + render_marker({"change": None, "kind": "patch", "prs": refs})
        url = run(store, "gh", "pr", "create", "--draft", "--base", args.base, "--head", branch, "--label", LABEL,
                  "--label", PATCH_LABEL, "--title", f"obs: 补登汇总（{now:%Y-%m-%d} 起）", "--body-file", "-", input_text=body)
    return [f"- {item['pr']} → {url}（补登汇总，已更新主 spec：{'、'.join(summaries)}）" for item, _, summaries in applied], fallbacks


def apply_unregistered(store: Path, args, item: dict, now: dt.datetime, used_ids: set[str], reason: str) -> str:
    """起草失败的未登记变化：单独开预置空壳补登 change 的 PR。"""
    repo = ref_repo(item["pr"])
    name = f"obs-{repo.split('/')[1].lower()}-{ref_suffix(item['pr'])}"
    branch = f"obs/{name}"
    start_branch(store, args.base, branch, False)
    directory = store / "openspec" / "changes" / name
    directory.mkdir(parents=True, exist_ok=True)
    meta = {
        "schema": "store",
        "created": now.date().isoformat(),
        "title": f"补登：{item['title']}"[:80],
        "ID": new_change_id(store, now, used_ids),
        "type": "feature",
        "priority": "",
        "assignee": "",
        "version": "",
        "deadline": "",
        "repositories": [repo],
        "tier": "light",
        # 补登的是已合入的实现：直接预填已实现与实现记录，需求侧只需补写需求与元数据。
        "status": "已实现",
        "implementations": [
            {"pr": item["pr"], "url": item["url"], "merged_at": item.get("merged_at") or "",
             "basis": f"补登：{item['behavior']}"[:200], "hint": False, "confidence": "高"}
        ],
    }
    (directory / ".openspec.yaml").write_text(dump_yaml(meta), encoding="utf-8")
    (directory / "proposal.md").write_text(
        f"## 说明\n\n实现观察发现 [{item['pr']}]({item['url']}) 改变了产品行为：{item['behavior']}\n\n建议：{item['suggestion']}\n\n"
        "## 验收场景\n\n- **AC-1** 待需求侧补写。\n\n## 涉及规范\n\n### 新增特性\n\n### 改动特性\n",
        encoding="utf-8",
    )
    denied = commit_and_push(store, branch, f"obs: 未登记的行为变化 {item['pr']}")
    body = unregistered_body(item, reason) + "\n" + render_marker({"change": None, "prs": [item["pr"]]})
    url = run(store, "gh", "pr", "create", "--draft", "--base", args.base, "--head", branch, "--label", LABEL,
              "--title", f"obs: 未登记的行为变化 {item['pr']}", "--body-file", "-", input_text=body)
    return f"- {item['pr']} → {url}（空壳补登：{reason}）" + (f"（已拒绝写入：{', '.join(denied)}）" if denied else "")


def main() -> int:
    parser = argparse.ArgumentParser()
    for name in ("--store", "--store-repo", "--base", "--input", "--result", "--state", "--report"):
        parser.add_argument(name, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    store = Path(args.store).resolve()
    data = json.loads(Path(args.input).read_text())
    result_path = Path(args.result)
    result = json.loads(result_path.read_text()) if result_path.is_file() and result_path.read_text().strip() else {}
    state_path = Path(args.state)
    state = json.loads(state_path.read_text()) if state_path.is_file() else {"repos": {}}
    now = dt.datetime.now(dt.timezone.utc)

    checked, notes = validate(result, data)
    changes = {c["change"]: c for c in data["changes"]}

    # 无需起草的判断写入缓存，下次运行不再交给模型。
    for item in checked["no_draft"]:
        repo = ref_repo(item["pr"])
        state.setdefault("repos", {}).setdefault(repo, {}).setdefault("technical", []).append(item["pr"])
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))

    lines = [f"## 实现观察{'（dry-run，未写入）' if args.dry_run else ''}", ""]
    if args.dry_run:
        for link in checked["links"]:
            target = "（未合并，将在其 PR 评论）" if changes[link["change"]].get("draft_pr") else ""
            lines.append(f"- 将关联 `{link['change']}`{target}：" + "、".join(f"{p['pr']}（{p['confidence']}，{p['basis']}）" for p in link["prs"]))
            lines += [f"  - 差异：{d['scenario']}（{d['pr']}）{d['detail']}" for d in link["diffs"]]
        target = f"追加到补登汇总 PR #{data['patch_pr']['number']}" if data.get("patch_pr") else "新开补登汇总 PR"
        lines += [f"- 将补登未登记行为变化（{target}）：{u['pr']}：{u['behavior']}" for u in checked["unregistered"]]
    else:
        if checked["links"] or checked["unregistered"]:
            run(store, "git", "config", "user.name", "github-actions[bot]")
            run(store, "git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
            run(store, "git", "fetch", "origin", args.base)
            ensure_labels(store)
        for link in checked["links"]:
            try:
                change = changes[link["change"]]
                if change.get("draft_pr"):
                    lines.append(apply_draft_comment(store, change, link))
                else:
                    lines.append(apply_link(store, args, change, link, data["open_drafts"].get(link["change"]), now))
            except subprocess.CalledProcessError as error:
                lines.append(f"- `{link['change']}` 写入失败：{(error.stderr or '').strip()[:300]}")
        fallbacks: list[tuple[dict, str]] = []
        if checked["unregistered"]:
            units = {u["pr"]: u for u in data.get("merged") or []}
            try:
                rollup, fallbacks = apply_patch_rollup(store, args, checked["unregistered"], units, data.get("patch_pr"), now)
                lines += rollup
            except subprocess.CalledProcessError as error:
                lines.append(f"- 补登汇总写入失败：{(error.stderr or '').strip()[:300]}")
        used_ids: set[str] = set()
        for item, reason in fallbacks:
            try:
                lines.append(apply_unregistered(store, args, item, now, used_ids, reason))
            except subprocess.CalledProcessError as error:
                lines.append(f"- {item['pr']} 写入失败：{(error.stderr or '').strip()[:300]}")
    lines += [f"- 无需起草：{n['pr']}（{n['reason']}）" for n in checked["no_draft"]]
    lines += [f"- {note}" for note in notes]
    if len(lines) == 2:
        lines.append("本次没有需要处理的已合并 PR。")
    Path(args.report).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
