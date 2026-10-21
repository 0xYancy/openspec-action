#!/usr/bin/env python3
"""实现观察的受限写入步骤：校验 模型结论，只在 Store 开或更新草稿 PR。

用法：python3 write.py --store <dir> --store-repo <owner/repo> --base <主分支> --input <input.json>
                       --result <result.json> --state <state.json> --report <report.md> [--dry-run]

环境变量：GH_TOKEN（Store 的 GITHUB_TOKEN，只用于 git push 与 gh pr）

- 只修改 openspec/changes/**；任何其他路径的改动都会被撤销并在报告中列出。
- 从不合并 PR，从不推送主分支。
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from store_lib import dump_yaml, guarded_paths, load_yaml, parse_marker, render_marker, revert_paths  # noqa: E402

LABEL = "obs"
REJECTED_LABEL = "obs:rejected"


def run(store: Path, *args: str, check: bool = True, input_text: str | None = None) -> str:
    return subprocess.run(args, cwd=store, capture_output=True, text=True, check=check, input=input_text).stdout.strip()


def validate(result: dict, data: dict) -> tuple[dict, list[str]]:
    """丢弃引用不存在的 PR / change、被拒绝过的组合，以及重复出现的 PR。"""
    notes: list[str] = []
    merged = {item["pr"]: item for item in data["merged"]}
    changes = {item["change"]: item for item in data["changes"]}
    rejected = {(r["pr"], r.get("change")) for r in data.get("rejected") or []}
    seen: set[str] = set()
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
            elif ref in seen:
                notes.append(f"忽略：{ref} 被重复归类")
            else:
                seen.add(ref)
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
            unregistered.append({**item, **{k: merged[ref][k] for k in ("url", "title")}})
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
"""


def unregistered_body(item: dict) -> str:
    return f"""## 摘要
[{item['pr']}]({item['url']}) {item['title']} 改变了产品行为，但没有关联到任何已批准的 change。

## 被改变的行为
{item['behavior']}

## 建议
{item['suggestion']}

本 PR 预置了一个草稿 change：补写 specs 并把状态改为已批准，点击 Ready for review 通过 Store 检查后即可合并；若这是已知行为或无需登记，关闭本 PR 并加上 `{REJECTED_LABEL}` 标签。
"""


def ensure_labels(store: Path) -> None:
    for name, color, desc in ((LABEL, "0e8a16", "实现观察起草的 PR"), (REJECTED_LABEL, "b60205", "需求侧拒绝的观察匹配")):
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
    run(store, "git", "push", "origin", f"HEAD:refs/heads/{branch}")
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
    """草稿 change 还不在主分支：只在它的草稿 PR 上评论提示，不改需求内容与状态。"""
    rows = "\n".join(
        f"| [{p['pr']}]({p['url']}) {p['title']} | {p['merged_at'][:10]} | {'提示信号：' if p.get('hint') else '推断：'}{p['basis']} | {p['confidence']} |"
        for p in link["prs"]
    )
    diffs = "\n".join(f"- **{d['scenario']}**（{d['pr']}）：{d['detail']}" for d in link["diffs"]) or "未发现差异。"
    refs = [p["pr"] for p in link["prs"]]
    body = f"""## 实现观察：发现可能对应本草稿的实现

本草稿 `{change['change']}` 还未批准，实现观察不会修改它。以下实现已合入观察分支，可能就是本需求的实现，供确认需求时参考；批准合并后，观察会按正常流程关联。

| 实现 | 时间 | 匹配依据 | 置信度 |
|---|---|---|---|
{rows}

### 实现与需求的差异
{diffs}

{render_marker({"change": change["change"], "prs": refs})}
"""
    number = change["draft_pr"]["number"]
    run(store, "gh", "pr", "comment", str(number), "--body-file", "-", input_text=body)
    return f"- `{change['change']}`（草稿）：{', '.join(refs)} → 已在 {change['draft_pr']['url']} 评论提示"


def ref_repo(ref: str) -> str:
    """实现单元标识所在仓库：`owner/repo#12`（PR）或 `owner/repo@abc1234..def5678`（直接推送的提交）。"""
    return re.split(r"[#@]", ref, maxsplit=1)[0]


def ref_suffix(ref: str) -> str:
    """用于分支与 change 名：PR 编号，或提交区间末个提交的短 SHA。"""
    tail = re.split(r"[#@]", ref, maxsplit=1)[1]
    return tail.split("..")[-1].lower()


def apply_unregistered(store: Path, args, item: dict, now: dt.datetime) -> str:
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
        "ID": f"{now:%Y%m%d}-{int(now.timestamp()) & 0xFFFF:04x}",
        "type": "feature",
        "priority": "",
        "assignee": "",
        "version": "",
        "deadline": "",
        "repositories": [repo],
        "tier": "light",
        "status": "草稿",
    }
    (directory / ".openspec.yaml").write_text(dump_yaml(meta), encoding="utf-8")
    (directory / "proposal.md").write_text(
        f"## 说明\n\n实现观察发现 [{item['pr']}]({item['url']}) 改变了产品行为：{item['behavior']}\n\n建议：{item['suggestion']}\n\n"
        "## 验收场景\n\n- **AC-1** 待需求侧补写。\n\n## 涉及规范\n\n### 新增特性\n\n### 改动特性\n",
        encoding="utf-8",
    )
    denied = commit_and_push(store, branch, f"obs: 未登记的行为变化 {item['pr']}")
    body = unregistered_body(item) + "\n" + render_marker({"change": None, "prs": [item["pr"]]})
    url = run(store, "gh", "pr", "create", "--draft", "--base", args.base, "--head", branch, "--label", LABEL,
              "--title", f"obs: 未登记的行为变化 {item['pr']}", "--body-file", "-", input_text=body)
    return f"- {item['pr']} → {url}" + (f"（已拒绝写入：{', '.join(denied)}）" if denied else "")


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
            target = "（草稿，将在草稿 PR 评论）" if changes[link["change"]].get("draft_pr") else ""
            lines.append(f"- 将关联 `{link['change']}`{target}：" + "、".join(f"{p['pr']}（{p['confidence']}，{p['basis']}）" for p in link["prs"]))
            lines += [f"  - 差异：{d['scenario']}（{d['pr']}）{d['detail']}" for d in link["diffs"]]
        lines += [f"- 将起草未登记行为变化：{u['pr']}：{u['behavior']}" for u in checked["unregistered"]]
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
        for item in checked["unregistered"]:
            try:
                lines.append(apply_unregistered(store, args, item, now))
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
