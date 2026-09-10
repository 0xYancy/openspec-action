#!/bin/bash
# 判断一次 push 是否包含目标分支第一父链上的直接 change 文档改动。
# 返回 0 表示应发送 Slack，返回 1 表示纯 merge、无文档改动或无法安全判断。

set -uo pipefail

BEFORE_SHA="${1:-}"
AFTER_SHA="${2:-}"
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
