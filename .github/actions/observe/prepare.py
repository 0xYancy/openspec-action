#!/usr/bin/env python3
"""实现观察的确定性前置步骤：收集事实，生成模型输入。

用法：python3 prepare.py --store <dir> --store-repo <owner/repo> --state <state.json> --out <dir>

环境变量：
  STORE_TOKEN   Store 仓库的 GITHUB_TOKEN（读取 Store 中的观察 PR）
  ORG_TOKENS    app_token.py 的输出 JSON（各组织只读 token 与被跳过的组织）
  LOOKBACK_DAYS 没有位置记录时回看的天数，默认 14

产出（--out 目录）：
  input.json    模型输入：候选 change、待匹配的实现单元（已合并 PR 与直接推送的提交批次）
  signals.json  进行中信号：打开中的 PR 与其推断关联的 change
  summary.json  运行摘要：各仓库读取结果、被跳过的组织、待匹配数量
  state.json    新的位置记录（只前移读取成功的仓库）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "shared"))
from store_lib import (  # noqa: E402
    GitHubError,
    change_text,
    gh_request,
    list_store_obs_prs,
    load_changes,
    load_draft_changes,
    load_repos,
)

CANDIDATE_STATUS = {"已批准", "已实现"}
BOT_AUTHORS = re.compile(r"(dependabot|renovate|github-actions)", re.I)
TECH_TITLE = re.compile(r"^(chore|build|ci|test|tests|style)(\([^)]*\))?!?:", re.I)
TECH_LABELS = {"dependencies", "deps", "ci", "tests", "test", "chore"}
TECH_FILE = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|go\.sum|poetry\.lock|\.github/|"
    r"__tests__/|tests?/|.*\.(test|spec)\.[a-z]+$|\.gitignore$|\.editorconfig$|"
    # 开发侧的 OpenSpec 与 Agent 工具链（需求以 Store 为准）
    r"openspec/|\.claude/|\.agents/|\.codex/|AGENTS\.md$|CLAUDE\.md$)"
)
MAX_PATCH = 6000
CST = dt.timezone(dt.timedelta(hours=8))


def iso(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str) -> dt.datetime:
    return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def pr_ref(repo: str, number: int) -> str:
    return f"{repo}#{number}"


def technical_reason(pr: dict, files: list[dict]) -> str | None:
    if BOT_AUTHORS.search((pr.get("user") or {}).get("login") or ""):
        return "机器人提交"
    if TECH_TITLE.match(pr.get("title") or ""):
        return "标题为技术类前缀"
    if {label["name"].lower() for label in pr.get("labels") or []} & TECH_LABELS:
        return "技术类标签"
    if files and all(TECH_FILE.search(f["filename"]) for f in files):
        return "只改动依赖、测试或 CI 文件"
    return None


def hints_for(text: str, changes: list[dict], store_repo: str) -> list[dict]:
    found = []
    for change in changes:
        keys = [change["name"]]
        if change["meta"].get("ID"):
            keys.append(str(change["meta"]["ID"]))
        for key in keys:
            if key and key in text:
                found.append({"change": change["name"], "signal": f"提及 {key}"})
                break
    if store_repo and store_repo in text:
        found.append({"change": None, "signal": f"链接到 Store {store_repo}"})
    return found


def collect_handled(changes: list[dict], obs_prs: list[dict]) -> tuple[set[str], list[dict], dict[str, dict]]:
    """已处理的实现 PR、被拒绝的 (PR, change) 组合，以及各 change 打开中的观察草稿 PR。

    已处理：主分支某个 change 的 implementations 中已有，或任一观察 PR（打开中或已关闭）的标记中列出过。
    被拒绝：需求侧关闭（未合并）并加上 obs:rejected 标签的观察 PR 中的组合。
    """
    handled: set[str] = set()
    for change in changes:
        for item in change["implementations"]:
            if isinstance(item, dict) and item.get("pr"):
                handled.add(str(item["pr"]))
    rejected: list[dict] = []
    open_drafts: dict[str, dict] = {}
    for pr in obs_prs:
        refs = [str(r) for r in pr["marker"].get("prs") or []]
        if pr["state"] == "closed" and not pr["merged"] and "obs:rejected" in pr["labels"]:
            rejected += [{"pr": r, "change": pr["marker"].get("change")} for r in refs]
        handled.update(refs)
        if pr["state"] == "open" and pr["marker"].get("change"):
            open_drafts[pr["marker"]["change"]] = {"number": pr["number"], "url": pr["url"], "branch": pr["branch"]}
    return handled, rejected, open_drafts


def fetch_pulls(repo: str, branch: str, token: str, since: dt.datetime) -> tuple[list[dict], list[dict]]:
    merged, opened = [], []
    for page in range(1, 11):
        items = gh_request(
            f"/repos/{repo}/pulls?state=closed&base={branch}&sort=updated&direction=desc&per_page=100&page={page}", token
        ) or []
        for pr in items:
            if pr.get("merged_at") and parse_time(pr["merged_at"]) > since:
                merged.append(pr)
        if len(items) < 100 or (items and parse_time(items[-1]["updated_at"]) <= since):
            break
    for page in range(1, 4):
        items = gh_request(f"/repos/{repo}/pulls?state=open&base={branch}&per_page=100&page={page}", token) or []
        opened += items
        if len(items) < 100:
            break
    return merged, opened


def fetch_pr_shas(repo: str, pulls: list[dict], token: str) -> set[str]:
    """已合并 PR 的提交与合并提交：直接推送的提交要排除它们。"""
    shas: set[str] = set()
    for pr in pulls:
        if pr.get("merge_commit_sha"):
            shas.add(pr["merge_commit_sha"])
        for page in range(1, 4):
            items = gh_request(f"/repos/{repo}/pulls/{pr['number']}/commits?per_page=100&page={page}", token) or []
            shas.update(c["sha"] for c in items)
            if len(items) < 100:
                break
    return shas


def fetch_commits(repo: str, branch: str, token: str, since: dt.datetime) -> list[dict]:
    """观察分支上 since 之后的提交，按时间从旧到新。"""
    commits = []
    for page in range(1, 11):
        items = gh_request(f"/repos/{repo}/commits?sha={branch}&since={iso(since)}&per_page=100&page={page}", token) or []
        commits += items
        if len(items) < 100:
            break
    return sorted(commits, key=lambda c: c["commit"]["committer"]["date"])


def commit_batches(commits: list[dict], pr_shas: set[str]) -> list[list[dict]]:
    """把同一天（北京时间）内连续的直接推送合成一个实现单元；PR 的提交会切断连续。"""
    batches: list[list[dict]] = []
    current: list[dict] = []
    day = None
    for commit in commits:
        if commit["sha"] in pr_shas:
            if current:
                batches.append(current)
            current, day = [], None
            continue
        commit_day = parse_time(commit["commit"]["committer"]["date"]).astimezone(CST).date()
        if current and commit_day != day:
            batches.append(current)
            current = []
        current.append(commit)
        day = commit_day
    if current:
        batches.append(current)
    return batches


def batch_ref(repo: str, batch: list[dict]) -> str:
    first, last = batch[0]["sha"][:7], batch[-1]["sha"][:7]
    return f"{repo}@{first}" if first == last else f"{repo}@{first}..{last}"


def batch_files(repo: str, batch: list[dict], token: str) -> tuple[list[dict], str]:
    """区间的改动文件与链接：有父提交时用 compare，否则取单个提交。"""
    first, last = batch[0], batch[-1]
    parents = first.get("parents") or []
    if parents:
        data = gh_request(f"/repos/{repo}/compare/{parents[0]['sha']}...{last['sha']}", token) or {}
        url = f"https://github.com/{repo}/compare/{parents[0]['sha'][:12]}...{last['sha'][:12]}"
        return data.get("files") or [], url
    data = gh_request(f"/repos/{repo}/commits/{last['sha']}", token) or {}
    return data.get("files") or [], last["html_url"]


def batch_technical(batch: list[dict], files: list[dict]) -> str | None:
    authors = {((c.get("author") or {}).get("login") or c["commit"]["author"].get("name") or "") for c in batch}
    if authors and all(BOT_AUTHORS.search(a) for a in authors):
        return "机器人提交"
    messages = [c["commit"]["message"].split("\n")[0] for c in batch if len(c.get("parents") or []) <= 1]
    if messages and all(TECH_TITLE.match(m) for m in messages):
        return "提交说明均为技术类前缀"
    if files and all(TECH_FILE.search(f["filename"]) for f in files):
        return "只改动依赖、测试或 CI 文件"
    return None


def fetch_files(repo: str, number: int, token: str) -> list[dict]:
    files = []
    for page in range(1, 4):
        items = gh_request(f"/repos/{repo}/pulls/{number}/files?per_page=100&page={page}", token) or []
        files += items
        if len(items) < 100:
            break
    return files


def summarize_files(files: list[dict]) -> tuple[list[str], str]:
    names = [f"{f['status']} {f['filename']} (+{f['additions']}/-{f['deletions']})" for f in files]
    patch, used = [], 0
    for f in files:
        if TECH_FILE.search(f["filename"]) or not f.get("patch"):
            continue
        chunk = f"--- {f['filename']}\n{f['patch']}"
        if used + len(chunk) > MAX_PATCH:
            chunk = chunk[: max(0, MAX_PATCH - used)] + "\n…（已截断）"
        patch.append(chunk)
        used += len(chunk)
        if used >= MAX_PATCH:
            break
    return names, "\n".join(patch)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--store", required=True)
    parser.add_argument("--store-repo", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    store = Path(args.store).resolve()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    lookback = now - dt.timedelta(days=int(os.environ.get("LOOKBACK_DAYS") or 14))

    state_path = Path(args.state)
    state = json.loads(state_path.read_text()) if state_path.is_file() else {}
    state.setdefault("repos", {})
    org_tokens = json.loads(os.environ.get("ORG_TOKENS") or "{}")
    tokens, skipped_orgs = org_tokens.get("tokens") or {}, org_tokens.get("skipped") or {}

    repos = load_repos(store)
    changes = load_changes(store)
    candidates = [c for c in changes if not c["archived"] and c["status"] in CANDIDATE_STATUS]

    store_token = os.environ.get("STORE_TOKEN", "")
    obs_prs = list_store_obs_prs(args.store_repo, store_token) if store_token else []
    handled, rejected, open_drafts = collect_handled(changes, obs_prs)
    # 需求草稿 PR 里的 change 也作为候选：关联结果以评论提示在草稿 PR 上，不写入主分支
    draft_changes, commented = [], []
    if store_token:
        try:
            draft_changes, commented = load_draft_changes(store, args.store_repo, store_token, out / "drafts")
        except GitHubError as error:
            print(f"读取需求草稿 PR 失败，本次不把草稿作为候选：{error}", file=sys.stderr)
    handled.update(commented)
    candidates += draft_changes

    repo_results, merged_items, signals = [], [], []
    for repo in repos:
        slug, org = repo["slug"], repo["slug"].split("/")[0]
        if org not in tokens:
            repo_results.append({"repo": slug, "ok": False, "error": skipped_orgs.get(org, "缺少该组织的只读凭据")})
            continue
        token = tokens[org]
        entry = state["repos"].get(slug) or {}
        # 没有位置记录（首次运行或缓存丢失）时回看 LOOKBACK_DAYS；已处理的 PR 由 handled 去重，结果幂等。
        since = parse_time(entry["since"]) if entry.get("since") else lookback
        try:
            merged, opened = fetch_pulls(slug, repo["branch"], token, since)
            new_count = 0
            for pr in sorted(merged, key=lambda p: p["merged_at"]):
                ref = pr_ref(slug, pr["number"])
                if ref in handled or ref in (entry.get("technical") or []):
                    continue
                files = fetch_files(slug, pr["number"], token)
                reason = technical_reason(pr, files)
                if reason:
                    entry.setdefault("technical", []).append(ref)
                    continue
                names, patch = summarize_files(files)
                text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}\n{pr['head']['ref']}"
                merged_items.append(
                    {
                        "kind": "pr",
                        "pr": ref,
                        "url": pr["html_url"],
                        "title": pr.get("title") or "",
                        "body": (pr.get("body") or "")[:3000],
                        "branch": pr["head"]["ref"],
                        "merged_at": pr["merged_at"],
                        "files": names[:80],
                        "patch": patch,
                        "hints": hints_for(text, candidates, args.store_repo),
                    }
                )
                new_count += 1
            # 不经 PR 直接推送到观察分支的提交
            pr_shas = fetch_pr_shas(slug, merged, token)
            direct = 0
            for batch in commit_batches(fetch_commits(slug, repo["branch"], token, since), pr_shas):
                ref = batch_ref(slug, batch)
                if ref in handled or ref in (entry.get("technical") or []):
                    continue
                files, url = batch_files(slug, batch, token)
                reason = batch_technical(batch, files)
                if reason:
                    entry.setdefault("technical", []).append(ref)
                    continue
                names, patch = summarize_files(files)
                messages = "\n".join(f"- {c['sha'][:7]} {c['commit']['message'].strip()}" for c in batch)
                title = batch[-1]["commit"]["message"].split("\n")[0]
                merged_items.append(
                    {
                        "kind": "commits",
                        "pr": ref,
                        "url": url,
                        "title": title + (f"（共 {len(batch)} 个提交）" if len(batch) > 1 else ""),
                        "body": messages[:3000],
                        "branch": repo["branch"],
                        "merged_at": batch[-1]["commit"]["committer"]["date"],
                        "files": names[:80],
                        "patch": patch,
                        "hints": hints_for(messages, candidates, args.store_repo),
                    }
                )
                direct += 1
            new_count += direct
            for pr in opened:
                ref = pr_ref(slug, pr["number"])
                text = f"{pr.get('title') or ''}\n{pr.get('body') or ''}\n{pr['head']['ref']}"
                hinted = [h["change"] for h in hints_for(text, candidates, args.store_repo) if h["change"]]
                covering = [c["name"] for c in candidates if c["status"] == "已批准" and slug in c["repositories"]]
                if hinted:
                    links, basis = hinted, "提示信号"
                elif len(covering) == 1:
                    links, basis = covering, "推断：该仓库只涉及这一个已批准 change"
                else:
                    links, basis = [], None
                for change in links:
                    signals.append({"change": change, "pr": ref, "url": pr["html_url"], "title": pr.get("title") or "", "basis": basis})
            entry["since"] = iso(now)
            entry["technical"] = (entry.get("technical") or [])[-500:]
            state["repos"][slug] = entry
            repo_results.append({"repo": slug, "ok": True, "merged": len(merged), "direct": direct, "new": new_count, "open": len(opened)})
        except GitHubError as error:
            repo_results.append({"repo": slug, "ok": False, "error": str(error)})

    candidate_view = []
    for change in candidates:
        candidate_view.append(
            {
                "change": change["name"],
                "title": change["meta"].get("title"),
                "id": change["meta"].get("ID"),
                "status": change["status"],
                "repositories": change["repositories"],
                **({"draft_pr": change["draft_pr"]} if change.get("draft_pr") else {}),
                **change_text(store, change),
            }
        )
    input_data = {
        "store": args.store_repo,
        "changes": candidate_view,
        "merged": merged_items,
        "rejected": rejected,
        "open_drafts": open_drafts,
    }
    (out / "input.json").write_text(json.dumps(input_data, ensure_ascii=False, indent=2))
    (out / "signals.json").write_text(
        json.dumps({"store": args.store_repo, "observed_at": iso(now), "open": signals, "drafts": {k: {"number": v["number"], "url": v["url"]} for k, v in open_drafts.items()}, "repos": repo_results, "skipped_orgs": skipped_orgs}, ensure_ascii=False, indent=2)
    )
    (out / "summary.json").write_text(
        json.dumps({"repos": repo_results, "skipped_orgs": skipped_orgs, "to_match": len(merged_items), "signals": len(signals)}, ensure_ascii=False, indent=2)
    )
    (out / "state.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))
    print(json.dumps({"to_match": len(merged_items), "signals": len(signals), "repos": len(repo_results), "draft_candidates": len(draft_changes)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
