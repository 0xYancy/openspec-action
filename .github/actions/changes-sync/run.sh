#!/bin/bash
# run.sh - composite action 主入口
# 接收 openspec-f workflow 提取好的元数据 JSON 数组与 commit 信息，
# 对每条 change 完成：元数据同步 → 文档整合（如有文档变更）→ Notion 正文写入 → Slack 通知 → 日志输出
#
# 必需环境变量（由 action.yml 透传）：
#   METADATA           JSON 数组：[{change, title, assignee, status, priority, version, deadline, estimate, ID, type, progress}, ...]
#   CHANGED_FILES      空格分隔的本次 push 涉及的文件列表（来自 git diff）
#   REPO               owner/name
#   BRANCH             分支名
#   COMMIT_SHA         本次 commit SHA
#   BEFORE_SHA         上一个 commit SHA（用于 diff）
#
# Secrets（必需）：
#   NOTION_API_KEY、OPENROUTER_API_KEY、SLACK_WEBHOOK_URL
#
# 公开 ID（已硬编码默认值，可 env 覆盖）：
#   NOTION_VERSION、NOTION_TASK_DB_ID、NOTION_TASK_DS_ID、NOTION_VERSION_DS_ID、OPENROUTER_MODEL

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

: "${METADATA:?METADATA is required}"
: "${CHANGED_FILES:=}"
: "${REPO:?REPO is required}"
: "${BRANCH:?BRANCH is required}"
: "${COMMIT_SHA:?COMMIT_SHA is required}"
: "${BEFORE_SHA:=}"

# 公开 ID 默认值（与 sync-task.sh 保持一致）
NOTION_VERSION="${NOTION_VERSION:-2025-09-03}"
NOTION_TASK_DS_ID="${NOTION_TASK_DS_ID:-318bc3b8-8a84-802f-af8e-000b8589126c}"
export NOTION_VERSION NOTION_TASK_DS_ID

COMMIT_SHORT=$(echo "$COMMIT_SHA" | cut -c1-7)

# 初始化 step summary
SUMMARY_FILE="${GITHUB_STEP_SUMMARY:-/tmp/step-summary.md}"
{
  echo "## openspec changes 同步结果"
  echo ""
  echo "**${REPO}@${BRANCH}** · commit \`${COMMIT_SHORT}\`"
  echo ""
  echo "| change | 标题 | 结果 | 文档变更 | Notion |"
  echo "|--------|------|------|---------|--------|"
} >> "$SUMMARY_FILE"

# 解析 metadata 数组并逐条处理
COUNT=$(echo "$METADATA" | jq 'length')
if [[ "$COUNT" == "0" ]]; then
  echo "No changes to sync"
  echo "" >> "$SUMMARY_FILE"
  echo "_(本次 push 未涉及任何 change 元数据)_" >> "$SUMMARY_FILE"
  exit 0
fi

OVERALL_STATUS=0

for ((i=0; i<COUNT; i++)); do
  ENTRY=$(echo "$METADATA" | jq ".[$i]")
  CHANGE_NAME=$(echo "$ENTRY" | jq -r '.change')
  TITLE=$(echo "$ENTRY" | jq -r '.title // .change')
  ASSIGNEE=$(echo "$ENTRY" | jq -r '.assignee // empty')

  echo ""
  echo "──────────── $CHANGE_NAME ────────────"

  # 定位 change 目录（active 或 archive）
  # 同时兼容两种仓库布局：标准 openspec/changes/ 与根布局 changes/
  if [[ -d "openspec/changes/$CHANGE_NAME" ]]; then
    CHANGE_DIR="openspec/changes/$CHANGE_NAME"
  elif [[ -d "changes/$CHANGE_NAME" ]]; then
    CHANGE_DIR="changes/$CHANGE_NAME"
  elif [[ -d "openspec/changes/archive/$CHANGE_NAME" ]]; then
    CHANGE_DIR="openspec/changes/archive/$CHANGE_NAME"
  elif [[ -d "changes/archive/$CHANGE_NAME" ]]; then
    CHANGE_DIR="changes/archive/$CHANGE_NAME"
  else
    echo "  ✗ Change directory not found"
    echo "| $CHANGE_NAME | $TITLE | ✗ dir not found | — | — |" >> "$SUMMARY_FILE"
    OVERALL_STATUS=1
    continue
  fi

  # 判断本次 push 是否涉及该 change 目录下任意 .md 文件
  HAS_DOC_CHANGES=false
  CHANGED_DOCS=""
  for f in proposal.md design.md tasks.md tests.md; do
    if echo "$CHANGED_FILES" | grep -qE "(^| )$CHANGE_DIR/$f( |$)"; then
      HAS_DOC_CHANGES=true
      CHANGED_DOCS="${CHANGED_DOCS}${f}, "
    fi
  done
  CHANGED_DOCS=${CHANGED_DOCS%, }

  # 判断 Notion 中是否已存在该 task（用于决定是否首次创建时拉取所有文档）
  CHANGE_ID=$(echo "$ENTRY" | jq -r '.ID // empty')
  EXISTING_PAGE=""
  if [[ -n "$CHANGE_ID" ]]; then
    EXISTING_PAGE=$(curl -s -X POST "https://api.notion.com/v1/data_sources/$NOTION_TASK_DS_ID/query" \
      -H "Authorization: Bearer $NOTION_API_KEY" \
      -H "Notion-Version: $NOTION_VERSION" \
      -H "Content-Type: application/json" \
      -d "$(jq -n --arg id "$CHANGE_ID" '{"filter":{"property":"ID","rich_text":{"equals":$id}}}')" \
      | jq -r '.results[0].id // empty')
  fi

  # 决定是否需要整合文档：
  # - Notion 不存在 → 必须整合（首次创建携带正文）
  # - 已存在 + 有文档变更 → 整合
  # - 已存在 + 无文档变更 → 跳过
  NEED_INTEGRATE=false
  if [[ -z "$EXISTING_PAGE" ]]; then
    NEED_INTEGRATE=true
    echo "  → New task, will integrate docs"
  elif [[ "$HAS_DOC_CHANGES" == "true" ]]; then
    NEED_INTEGRATE=true
    echo "  → Doc changes detected: $CHANGED_DOCS"
  else
    echo "  → Metadata-only update"
  fi

  CONTENT=""
  if [[ "$NEED_INTEGRATE" == "true" ]]; then
    if INTEGRATED=$(bash "$SCRIPT_DIR/llm-integrate.sh" "$CHANGE_DIR"); then
      CONTENT="$INTEGRATED"
    else
      echo "  ⚠ Integration failed, syncing metadata only"
    fi
  fi

  # 注入 content 字段后调 sync-task.sh
  if [[ -n "$CONTENT" ]]; then
    PAYLOAD=$(echo "$ENTRY" | jq --arg c "$CONTENT" '. + {content: $c}')
  else
    PAYLOAD="$ENTRY"
  fi

  SYNC_LOG=$(echo "$PAYLOAD" | bash "$SCRIPT_DIR/sync-task.sh" "$REPO" "$BRANCH" 2>&1)
  SYNC_RC=$?
  echo "$SYNC_LOG"

  if (( SYNC_RC != 0 )); then
    echo "| $CHANGE_NAME | $TITLE | ✗ sync failed | ${CHANGED_DOCS:-—} | — |" >> "$SUMMARY_FILE"
    OVERALL_STATUS=1
    continue
  fi

  # 提取最终的 page id（sync-task.sh 通过 stderr 输出 PAGE_ID=...）
  PAGE_ID=$(echo "$SYNC_LOG" | grep -oE 'PAGE_ID=[a-z0-9-]+' | tail -1 | cut -d= -f2 || true)
  [[ -z "$PAGE_ID" ]] && PAGE_ID="$EXISTING_PAGE"
  PAGE_URL=""
  [[ -n "$PAGE_ID" ]] && PAGE_URL="https://www.notion.so/$(echo "$PAGE_ID" | tr -d '-')"

  # 判定结果文案
  if [[ -z "$EXISTING_PAGE" ]]; then
    RESULT="✓ created"
  elif [[ "$NEED_INTEGRATE" == "true" ]]; then
    RESULT="✓ updated (+content)"
  else
    RESULT="✓ updated"
  fi

  PAGE_LINK="—"
  [[ -n "$PAGE_URL" ]] && PAGE_LINK="[link]($PAGE_URL)"
  echo "| $CHANGE_NAME | $TITLE | $RESULT | ${CHANGED_DOCS:-—} | $PAGE_LINK |" >> "$SUMMARY_FILE"

  # Slack 通知：仅当本次 push 真的改了该 change 的文档
  if [[ "$HAS_DOC_CHANGES" == "true" ]]; then
    SUMMARY_TEXT=""
    if [[ -n "$BEFORE_SHA" ]]; then
      # stderr 直通到 workflow 日志,便于排查重试 / 空 diff 等情况
      SUMMARY_TEXT=$(bash "$SCRIPT_DIR/llm-summarize.sh" "$CHANGE_NAME" "$BEFORE_SHA" "$COMMIT_SHA" || echo "")
    else
      echo "  ⚠ summarize skipped: BEFORE_SHA empty (likely first push on branch)" >&2
    fi
    [[ -z "$SUMMARY_TEXT" ]] && SUMMARY_TEXT="• 文档已更新（自动摘要不可用）"

    bash "$SCRIPT_DIR/slack-notify.sh" \
      "$REPO" \
      "$BRANCH" \
      "$COMMIT_SHORT" \
      "$TITLE" \
      "$CHANGED_DOCS" \
      "$ASSIGNEE" \
      "$PAGE_URL" \
      "$SUMMARY_TEXT" || true
  fi
done

echo ""
echo "Done."
exit $OVERALL_STATUS
