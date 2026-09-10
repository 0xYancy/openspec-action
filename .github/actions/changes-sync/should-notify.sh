#!/bin/bash
# 判断一次 push 是否包含目标分支第一父链上的直接 change 文档改动。
# 返回 0 表示应发送 Slack，返回 1 表示纯 merge、fast-forward 分支晋级、
# 无文档改动或无法安全判断。

set -uo pipefail

BEFORE_SHA="${1:-}"
AFTER_SHA="${2:-}"
TARGET_BRANCH="${3:-}"
NULL_SHA="0000000000000000000000000000000000000000"

if [[ -z "$AFTER_SHA" ]] || ! git cat-file -e "$AFTER_SHA^{commit}" 2>/dev/null; then
  echo "  ⚠ Slack notification skipped: commit SHA is unavailable" >&2
  exit 1
fi

BASE_SHA="$BEFORE_SHA"
if [[ -z "$BASE_SHA" || "$BASE_SHA" == "$NULL_SHA" ]] || ! git cat-file -e "$BASE_SHA^{commit}" 2>/dev/null; then
  if git rev-parse --verify origin/main^{commit} >/dev/null 2>&1; then
    BASE_SHA=$(git merge-base origin/main "$AFTER_SHA" 2>/dev/null || true)
  else
    BASE_SHA=""
  fi
fi

if [[ -z "$BASE_SHA" ]]; then
  echo "  ⚠ Slack notification skipped: push base could not be resolved" >&2
  exit 1
fi

# actions/checkout 默认可能只建立当前分支的远端引用。尽力刷新全部远端分支，
# 以识别把 develop/feature 上已有提交 fast-forward 到目标分支的晋级操作。
if [[ -n "$TARGET_BRANCH" ]] && git remote get-url origin >/dev/null 2>&1; then
  git fetch --quiet --no-tags origin '+refs/heads/*:refs/remotes/origin/*' 2>/dev/null || \
    echo "  ⚠ remote branches could not be refreshed; fast-forward promotion detection may be incomplete" >&2
fi

if [[ -n "$TARGET_BRANCH" ]]; then
  while IFS= read -r ref; do
    [[ -n "$ref" ]] || continue
    [[ "$ref" == "refs/remotes/origin/$TARGET_BRANCH" ]] && continue
    [[ "$ref" == "refs/remotes/origin/HEAD" ]] && continue

    if git merge-base --is-ancestor "$AFTER_SHA" "$ref" 2>/dev/null; then
      echo "  → Slack notification skipped: commit already exists on ${ref#refs/remotes/origin/}; treating push as fast-forward branch promotion" >&2
      exit 1
    fi
  done < <(git for-each-ref --format='%(refname)' refs/remotes/origin)
fi

while IFS= read -r commit; do
  [[ -n "$commit" ]] || continue

  read -r -a commit_and_parents <<< "$(git rev-list --parents -n 1 "$commit")"
  parent_count=$((${#commit_and_parents[@]} - 1))
  if (( parent_count > 1 )); then
    continue
  fi

  if (( parent_count == 0 )); then
    changed=$(git diff-tree --root --no-commit-id --name-only -r --diff-filter=ACMRT "$commit")
  else
    changed=$(git diff-tree --no-commit-id --name-only -r --diff-filter=ACMRT "${commit_and_parents[1]}" "$commit")
  fi

  if echo "$changed" | grep -qE '^(openspec/)?changes/(archive/)?[^/]+/(proposal|design|tasks|tests)\.md$'; then
    exit 0
  fi
done < <(git rev-list --first-parent --reverse "$BASE_SHA..$AFTER_SHA")

echo "  → Slack notification skipped: no direct change document commit in push" >&2
exit 1
