#!/bin/bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HELPER="$SCRIPT_DIR/../.github/actions/changes-sync/should-notify.sh"
TEST_ROOT=$(mktemp -d)
TEST_REMOTE=$(mktemp -d)
trap 'rm -rf "$TEST_ROOT" "$TEST_REMOTE"' EXIT

git -C "$TEST_ROOT" init -q -b main
git -C "$TEST_ROOT" config user.name "OpenSpec Test"
git -C "$TEST_ROOT" config user.email "openspec-test@example.com"
git -C "$TEST_ROOT" config commit.gpgsign false
echo "base" > "$TEST_ROOT/README.md"
git -C "$TEST_ROOT" add README.md
git -C "$TEST_ROOT" commit -q -m "base"
BASE=$(git -C "$TEST_ROOT" rev-parse HEAD)

assert_notify() {
  local label="$1"
  local before="$2"
  local after="$3"
  local branch="${4:-}"
  if ! git -C "$TEST_ROOT" -c advice.detachedHead=false --work-tree="$TEST_ROOT" \
    --git-dir="$TEST_ROOT/.git" diff --quiet HEAD HEAD 2>/dev/null; then
    echo "FAIL: $label left a dirty fixture" >&2
    exit 1
  fi
  if ! (cd "$TEST_ROOT" && bash "$HELPER" "$before" "$after" "$branch"); then
    echo "FAIL: $label should notify" >&2
    exit 1
  fi
  echo "PASS: $label"
}

assert_skip() {
  local label="$1"
  local before="$2"
  local after="$3"
  local branch="${4:-}"
  if (cd "$TEST_ROOT" && bash "$HELPER" "$before" "$after" "$branch"); then
    echo "FAIL: $label should skip" >&2
    exit 1
  fi
  echo "PASS: $label"
}

# 直接文档提交应通知。
git -C "$TEST_ROOT" checkout -q -b direct "$BASE"
mkdir -p "$TEST_ROOT/openspec/changes/direct"
echo "proposal" > "$TEST_ROOT/openspec/changes/direct/proposal.md"
git -C "$TEST_ROOT" add openspec/changes/direct/proposal.md
git -C "$TEST_ROOT" commit -q -m "direct document change"
DIRECT=$(git -C "$TEST_ROOT" rev-parse HEAD)
assert_notify "direct document commit" "$BASE" "$DIRECT"

# 已存在于其他远端分支的提交被 fast-forward 到目标分支时应跳过。
git -C "$TEST_REMOTE" init -q --bare
git -C "$TEST_ROOT" remote add origin "$TEST_REMOTE"
git -C "$TEST_ROOT" push -q origin "$DIRECT:refs/heads/develop"
assert_skip "fast-forward branch promotion" "$BASE" "$DIRECT" "main"
git -C "$TEST_ROOT" remote remove origin
git -C "$TEST_ROOT" update-ref -d refs/remotes/origin/develop

# 纯 merge 的第一父链只有 merge commit，应跳过。
git -C "$TEST_ROOT" checkout -q -b merge-source "$BASE"
mkdir -p "$TEST_ROOT/openspec/changes/merged"
echo "proposal" > "$TEST_ROOT/openspec/changes/merged/proposal.md"
git -C "$TEST_ROOT" add openspec/changes/merged/proposal.md
git -C "$TEST_ROOT" commit -q -m "source document change"
git -C "$TEST_ROOT" checkout -q -B pure-merge "$BASE"
git -C "$TEST_ROOT" merge -q --no-ff merge-source -m "merge source"
PURE_MERGE=$(git -C "$TEST_ROOT" rev-parse HEAD)
assert_skip "pure merge" "$BASE" "$PURE_MERGE"

# 同一 push 的第一父链含直接文档提交和 merge commit 时应通知一次。
git -C "$TEST_ROOT" checkout -q -B mixed "$BASE"
mkdir -p "$TEST_ROOT/openspec/changes/mixed"
echo "proposal" > "$TEST_ROOT/openspec/changes/mixed/proposal.md"
git -C "$TEST_ROOT" add openspec/changes/mixed/proposal.md
git -C "$TEST_ROOT" commit -q -m "mixed direct document change"
git -C "$TEST_ROOT" merge -q --no-ff merge-source -m "mixed merge"
MIXED=$(git -C "$TEST_ROOT" rev-parse HEAD)
assert_notify "mixed direct and merge push" "$BASE" "$MIXED"

# 首次 push 使用 origin/main merge-base 回退，分支直接文档提交仍应通知。
git -C "$TEST_ROOT" update-ref refs/remotes/origin/main "$BASE"
assert_notify "first branch push" "0000000000000000000000000000000000000000" "$DIRECT"

# 仅元数据直接提交不应触发文档通知。
git -C "$TEST_ROOT" checkout -q -B metadata-only "$BASE"
mkdir -p "$TEST_ROOT/openspec/changes/metadata-only"
echo "status: 草稿" > "$TEST_ROOT/openspec/changes/metadata-only/.openspec.yaml"
git -C "$TEST_ROOT" add openspec/changes/metadata-only/.openspec.yaml
git -C "$TEST_ROOT" commit -q -m "metadata only"
METADATA_ONLY=$(git -C "$TEST_ROOT" rev-parse HEAD)
assert_skip "metadata-only commit" "$BASE" "$METADATA_ONLY"
