"""Store 自动化共用的读取、GitHub 请求、写入白名单与 PR 标记。"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

API = os.environ.get("GITHUB_API_URL", "https://api.github.com")

# 自动化只允许写入这些路径；其余（workflow、CODEOWNERS、框架文件、身份文件）一律拒绝。
WRITE_ALLOW = ["openspec/changes/**", "openspec/specs/**", "wiki/**"]
WRITE_DENY = [".github/**", ".openspec-store/**", "openspec/schemas/**", "openspec/framework.lock", "openspec/repos.yaml", ".claude/**", ".agents/**"]

MARKER = re.compile(r"<!--\s*openspec-obs:\s*(\{.*?\})\s*-->", re.S)
PR_REF = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)$")


# ---------------------------------------------------------------- Store 读取


def load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def load_repos(store: Path) -> list[dict]:
    repos = []
    for entry in load_yaml(store / "openspec" / "repos.yaml").get("repos") or []:
        match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", str(entry.get("remote") or ""))
        if match:
            repos.append({**entry, "slug": match.group(1), "branch": entry.get("branch") or "main"})
    return repos


def load_changes(store: Path, include_archive: bool = True) -> list[dict]:
    root = store / "openspec" / "changes"
    dirs = [d for d in sorted(root.iterdir()) if d.is_dir() and d.name != "archive"] if root.is_dir() else []
    if include_archive and (root / "archive").is_dir():
        dirs += [d for d in sorted((root / "archive").iterdir()) if d.is_dir()]
    changes = []
    for directory in dirs:
        meta = load_yaml(directory / ".openspec.yaml")
        changes.append(
            {
                "name": directory.name,
                "dir": directory.relative_to(store).as_posix(),
                "archived": directory.parent.name == "archive",
                "meta": meta,
                "status": str(meta.get("status") or ""),
                "repositories": [str(r) for r in meta.get("repositories") or []],
                "implementations": meta.get("implementations") or [],
            }
        )
    return changes


def change_text(store: Path, change: dict, limit: int = 12000) -> dict:
    directory = store / change["dir"]
    proposal = (directory / "proposal.md").read_text(encoding="utf-8") if (directory / "proposal.md").is_file() else ""
    specs = []
    for spec in sorted((directory / "specs").glob("*/spec.md")) if (directory / "specs").is_dir() else []:
        specs.append(f"### specs/{spec.parent.name}/spec.md\n{spec.read_text(encoding='utf-8')}")
    return {"proposal": proposal[:limit], "specs": "\n\n".join(specs)[:limit]}


# ---------------------------------------------------------------- GitHub


class GitHubError(Exception):
    pass


def gh_request(path: str, token: str, method: str = "GET", body: dict | None = None) -> object:
    url = path if path.startswith("http") else f"{API}{path}"
    req = urllib.request.Request(
        url,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode() or "null")
    except urllib.error.HTTPError as error:
        raise GitHubError(f"HTTP {error.code} {urllib.parse.urlsplit(url).path}") from None
    except urllib.error.URLError as error:
        raise GitHubError(f"网络错误 {urllib.parse.urlsplit(url).path}：{error.reason}") from None


def list_store_obs_prs(repo: str, token: str) -> list[dict]:
    """Store 中全部带观察标记的 PR（打开中与已关闭）。"""
    result = []
    for page in range(1, 11):
        items = gh_request(f"/repos/{repo}/pulls?state=all&per_page=100&page={page}", token) or []
        for pr in items:
            marker = parse_marker(pr.get("body") or "")
            if marker is None:
                continue
            result.append(
                {
                    "number": pr["number"],
                    "state": pr["state"],
                    "merged": bool(pr.get("merged_at")),
                    "branch": pr["head"]["ref"],
                    "labels": [label["name"] for label in pr.get("labels") or []],
                    "marker": marker,
                    "url": pr["html_url"],
                }
            )
        if len(items) < 100:
            break
    return result


# ---------------------------------------------------------------- 标记与引用


def parse_marker(body: str) -> dict | None:
    match = MARKER.search(body)
    if not match:
        return None
    try:
        data = json.loads(match.group(1))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def render_marker(data: dict) -> str:
    return f"<!-- openspec-obs: {json.dumps(data, ensure_ascii=False, sort_keys=True)} -->"


def parse_pr_ref(ref: str) -> tuple[str, int] | None:
    match = PR_REF.match(ref.strip())
    return (match.group(1), int(match.group(2))) if match else None


# ---------------------------------------------------------------- 写入白名单


def path_allowed(rel: str) -> bool:
    rel = rel.lstrip("./")
    if ".." in Path(rel).parts:
        return False
    if any(fnmatch.fnmatch(rel, pattern) for pattern in WRITE_DENY):
        return False
    return any(fnmatch.fnmatch(rel, pattern) for pattern in WRITE_ALLOW)


def guarded_paths(store: Path, base: str = "HEAD") -> tuple[list[str], list[str]]:
    """返回工作区相对 base 的改动文件中 (允许的, 被拒绝的)。"""
    out = subprocess.run(
        ["git", "-C", str(store), "status", "--porcelain", "--untracked-files=all"],
        capture_output=True, text=True, check=True,
    ).stdout
    allowed, denied = [], []
    for line in out.splitlines():
        path = line[3:].split(" -> ")[-1].strip('"')
        (allowed if path_allowed(path) else denied).append(path)
    return allowed, denied


def revert_paths(store: Path, paths: list[str]) -> None:
    for path in paths:
        subprocess.run(["git", "-C", str(store), "checkout", "--", path], capture_output=True)
        target = store / path
        if target.exists() and subprocess.run(
            ["git", "-C", str(store), "ls-files", "--error-unmatch", path], capture_output=True
        ).returncode != 0:
            target.unlink()


def dump_yaml(data: dict) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000)
