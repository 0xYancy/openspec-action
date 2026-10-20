#!/usr/bin/env python3
"""实现观察的确定性前置步骤：收集事实，生成 Agent 输入。

用法：python3 prepare.py --store <dir> --store-repo <owner/repo> --state <state.json> --out <dir>

环境变量：
  STORE_TOKEN   Store 仓库的 GITHUB_TOKEN（读取 Store 中的观察 PR）
  ORG_TOKENS    app_token.py 的输出 JSON（各组织只读 token 与被跳过的组织）
  LOOKBACK_DAYS 没有位置记录时回看的天数，默认 14

产出（--out 目录）：
  input.json    Agent 输入：候选 change、待匹配的已合并 PR
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
    load_repos,
)

CANDIDATE_STATUS = {"已批准", "已实现"}
BOT_AUTHORS = re.compile(r"(dependabot|renovate|github-actions)", re.I)
TECH_TITLE = re.compile(r"^(chore|build|ci|test|tests|style)(\([^)]*\))?!?:", re.I)
TECH_LABELS = {"dependencies", "deps", "ci", "tests", "test", "chore"}
TECH_FILE = re.compile(
    r"(^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|Cargo\.lock|go\.sum|poetry\.lock|\.github/|"
    r"__tests__/|tests?/|.*\.(test|spec)\.[a-z]+$|\.gitignore$|\.editorconfig$)"
)
MAX_PATCH = 6000


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
            repo_results.append({"repo": slug, "ok": True, "merged": len(merged), "new": new_count, "open": len(opened)})
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
    print(json.dumps({"to_match": len(merged_items), "signals": len(signals), "repos": len(repo_results)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
