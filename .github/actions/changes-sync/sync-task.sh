#!/bin/bash
# sync-task.sh - 从 stdin 读取单条 change JSON 元数据，创建或更新 Notion Task
# 用法: echo '{"change":"xxx","title":"xxx",...}' | ./sync-task.sh <repo> <branch>
#
# 凭据从环境变量读取（不再使用 config.json）：
#   NOTION_API_KEY        Notion integration token (必需，secret)
#   其余 ID 均为公开 ID，已硬编码默认值，可通过 env 覆盖

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

: "${NOTION_API_KEY:?NOTION_API_KEY is required}"

# 公开 ID 默认值（所有 openspec 仓库共用同一个 Notion Task 数据库）
NOTION_VERSION="${NOTION_VERSION:-2025-09-03}"
NOTION_TASK_DB_ID="${NOTION_TASK_DB_ID:-318bc3b8-8a84-8005-bc7e-c2ed7b4f6a40}"
NOTION_TASK_DS_ID="${NOTION_TASK_DS_ID:-318bc3b8-8a84-802f-af8e-000b8589126c}"
NOTION_VERSION_DS_ID="${NOTION_VERSION_DS_ID:-f9098417-055a-41f3-bdc1-1f777df6ea6c}"

NOTION_KEY="$NOTION_API_KEY"
DATABASE_ID="$NOTION_TASK_DB_ID"
TASK_DS_ID="$NOTION_TASK_DS_ID"
VERSION_DS_ID="$NOTION_VERSION_DS_ID"

REPO=$1
BRANCH=$2

# 从 stdin 读取 JSON 元数据
entry=$(cat)

normalize_status() {
  local raw="$1"
  local change_path="$2"
  local value
  value=$(printf '%s' "$raw" | tr -d '"' | xargs)

  # 注：openspec 框架仍输出"草稿/产品设计中/待开发/开发中/完成",
  # Notion 侧选项已迁移为"待启动/需求探索中/待开发/开发中/完成",此处做翻译
  case "$value" in
    ""|null|NULL|未设置)
      if [[ "$change_path" == archive/* ]]; then
        echo "完成"
      else
        echo "待启动"
      fi
      ;;
    草稿|待启动|draft|Draft)
      echo "待启动"
      ;;
    产品设计中|需求探索中|设计中|design|Design)
      echo "需求探索中"
      ;;
    待开发|todo|TODO|To-Do)
      echo "待开发"
      ;;
    进行中|开发中|in-progress|In-Progress|in_progress|doing|Doing)
      echo "开发中"
      ;;
    完成|已完成|done|Done|completed|Completed)
      echo "完成"
      ;;
    *)
      echo "$value"
      ;;
  esac
}

normalize_priority() {
  local raw="$1"
  local value
  value=$(printf '%s' "$raw" | tr -d '"' | xargs)

  case "$value" in
    ""|null|NULL|未设置)
      echo "P1"
      ;;
    P0|高|High|high)
      echo "P0"
      ;;
    P1|中|Medium|medium)
      echo "P1"
      ;;
    P2|低|Low|low)
      echo "P2"
      ;;
    *)
      echo "$value"
      ;;
  esac
}

# 读取字段
change=$(echo "$entry" | jq -r '.change')
id=$(echo "$entry" | jq -r '.ID')
title=$(echo "$entry" | jq -r '.title')
type=$(echo "$entry" | jq -r '.type // empty')
[[ -z "$type" ]] && type="feature"
raw_status=$(echo "$entry" | jq -r '.status')
raw_priority=$(echo "$entry" | jq -r '.priority')
status=$(normalize_status "$raw_status" "$change")
priority=$(normalize_priority "$raw_priority")
estimate=$(echo "$entry" | jq -r '.estimate // empty')
assignee=$(echo "$entry" | jq -r '.assignee')
version=$(echo "$entry" | jq -r '.version')
content=$(echo "$entry" | jq -r '.content // empty')
repo_name=$(echo "$REPO" | cut -d'/' -f2)

# 查找 Version ID（结合 repo 名称消歧义）
version_id=""
if [[ -n "$version" ]]; then
  version_results=$(curl -s -X POST "https://api.notion.com/v1/data_sources/$VERSION_DS_ID/query" \
    -H "Authorization: Bearer $NOTION_KEY" \
    -H "Notion-Version: $NOTION_VERSION" \
    -H "Content-Type: application/json" \
    -d "$(jq -n --arg v "$version" '{"filter":{"property":"版本号","title":{"contains":$v}}}')")
  ver_count=$(echo "$version_results" | jq '.results | length')
  if [[ "$ver_count" -eq 1 ]]; then
    version_id=$(echo "$version_results" | jq -r '.results[0].id')
  elif [[ "$ver_count" -gt 1 ]]; then
    repo_kw=$(echo "$repo_name" | sed 's/^bifrost-//' | tr '[:upper:]' '[:lower:]')
    version_id=$(echo "$version_results" | jq -r --arg kw "$repo_kw" '
      .results[] |
      . as $page |
      ([$page.properties | to_entries[] | .value |
        if type == "object" then
          (.title?         // [] | map(.plain_text? // "") | join("")),
          (.rich_text?     // [] | map(.plain_text? // "") | join("")),
          (.select?        | .name? // ""),
          (.multi_select?  // [] | map(.name) | join(""))
        else "" end
      ] | join(" ") | ascii_downcase) |
      if contains($kw) then $page.id else empty end
    ' | head -1)
    [[ -z "$version_id" ]] && version_id=$(echo "$version_results" | jq -r '.results[0].id')
  fi
fi

# 查找 Assignee ID
assignee_id=""
if [[ -n "$assignee" ]]; then
  assignee_lower=$(echo "$assignee" | tr '[:upper:]' '[:lower:]')
  assignee_id=$(curl -s "https://api.notion.com/v1/users" \
    -H "Authorization: Bearer $NOTION_KEY" \
    -H "Notion-Version: $NOTION_VERSION" \
    | jq -r --arg a "$assignee_lower" \
      '.results[] | select((.name | ascii_downcase) == $a or (.name | ascii_downcase | gsub(" "; "")) == $a or (.name | ascii_downcase | split(" ")[0]) == $a) | .id' \
    | head -1)
fi

# 查找是否已存在
existing=$(curl -s -X POST "https://api.notion.com/v1/data_sources/$TASK_DS_ID/query" \
  -H "Authorization: Bearer $NOTION_KEY" \
  -H "Notion-Version: $NOTION_VERSION" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg id "$id" '{"filter":{"property":"ID","rich_text":{"equals":$id}}}')" \
  | jq -r '.results[0].id // empty')

# ── 已存在：增量更新 ──────────────────────────────────────────
if [[ -n "$existing" ]]; then
  echo "Checking: $id (exists: $existing)"

  current=$(curl -s "https://api.notion.com/v1/pages/$existing" \
    -H "Authorization: Bearer $NOTION_KEY" \
    -H "Notion-Version: $NOTION_VERSION")

  cur_title=$(echo "$current"    | jq -r '.properties.Title.title[0].plain_text // empty')
  cur_status=$(echo "$current"   | jq -r '.properties.Status.status.name // empty')
  cur_type=$(echo "$current"     | jq -r '.properties.Type.select.name // empty')
  cur_priority=$(echo "$current" | jq -r '.properties.Priority.select.name // empty')
  cur_estimate=$(echo "$current" | jq -r '.properties.Estimate.number // empty')
  cur_version=$(echo "$current"  | jq -r '.properties.Version.relation[0].id // empty')
  cur_assignee=$(echo "$current" | jq -r '.properties.Assignee.people[0].id // empty')
  cur_branch=$(echo "$current"   | jq -r '.properties.Branch.rich_text[0].plain_text // empty')

  update_props='{}'
  changed_fields=()

  if [[ "$cur_title" != "$title" ]]; then
    update_props=$(echo "$update_props" | jq --arg v "$title" \
      '. + {"Title": {"title": [{"text": {"content": $v}}]}}')
    changed_fields+=("title")
  fi
  if [[ "$cur_status" != "$status" && "$cur_status" != "完成" ]]; then
    update_props=$(echo "$update_props" | jq --arg v "$status" \
      '. + {"Status": {"status": {"name": $v}}}')
    changed_fields+=("status")
  fi
  if [[ "$cur_type" != "$type" ]]; then
    update_props=$(echo "$update_props" | jq --arg v "$type" \
      '. + {"Type": {"select": {"name": $v}}}')
    changed_fields+=("type")
  fi
  if [[ "$cur_priority" != "$priority" ]]; then
    update_props=$(echo "$update_props" | jq --arg v "$priority" \
      '. + {"Priority": {"select": {"name": $v}}}')
    changed_fields+=("priority")
  fi
  if [[ "$cur_estimate" != "$estimate" && -z "$cur_estimate" && -n "$estimate" ]]; then
    update_props=$(echo "$update_props" | jq --argjson v "$estimate" \
      '. + {"Estimate": {"number": $v}}')
    changed_fields+=("estimate")
  fi
  if [[ "$cur_version" != "$version_id" && -n "$version_id" ]]; then
    update_props=$(echo "$update_props" | jq --arg v "$version_id" \
      '. + {"Version": {"relation": [{"id": $v}]}}')
    changed_fields+=("version")
  fi
  if [[ "$cur_assignee" != "$assignee_id" && -n "$assignee_id" ]]; then
    update_props=$(echo "$update_props" | jq --arg v "$assignee_id" \
      '. + {"Assignee": {"people": [{"id": $v}]}}')
    changed_fields+=("assignee")
  fi
  if [[ "$cur_branch" != "$BRANCH" ]]; then
    update_props=$(echo "$update_props" | jq --arg v "$BRANCH" \
      '. + {"Branch": {"rich_text": [{"text": {"content": $v}}]}}')
    changed_fields+=("branch")
  fi

  if [[ ${#changed_fields[@]} -eq 0 && -z "$content" ]]; then
    echo "  → No changes"
    # 输出 page id 供调用方收集
    echo "PAGE_ID=$existing" >&2
    exit 0
  fi

  # 更新元数据字段
  if [[ ${#changed_fields[@]} -gt 0 ]]; then
    patch_result=$(curl -s -X PATCH "https://api.notion.com/v1/pages/$existing" \
      -H "Authorization: Bearer $NOTION_KEY" \
      -H "Notion-Version: $NOTION_VERSION" \
      -H "Content-Type: application/json" \
      -d "$(jq -n --argjson props "$update_props" '{"properties": $props}')")

    if echo "$patch_result" | jq -e '.object == "page"' > /dev/null 2>&1; then
      echo "  ✓ Updated (${changed_fields[*]})"
    else
      echo "  ✗ Update failed: $(echo "$patch_result" | jq -r '.message // "unknown error"')"
      exit 1
    fi
  fi

  # 更新文档内容：清空旧 blocks 再写入新内容
  if [[ -n "$content" ]]; then
    old_blocks=$(curl -s "https://api.notion.com/v1/blocks/$existing/children?page_size=100" \
      -H "Authorization: Bearer $NOTION_KEY" \
      -H "Notion-Version: $NOTION_VERSION" \
      | jq -r '.results[].id')
    for bid in $old_blocks; do
      curl -s -X DELETE "https://api.notion.com/v1/blocks/$bid" \
        -H "Authorization: Bearer $NOTION_KEY" \
        -H "Notion-Version: $NOTION_VERSION" > /dev/null
    done

    blocks=$(echo "$content" | python3 "$SCRIPT_DIR/md2blocks.py")
    content_result=$(curl -s -X PATCH "https://api.notion.com/v1/blocks/$existing/children" \
      -H "Authorization: Bearer $NOTION_KEY" \
      -H "Notion-Version: $NOTION_VERSION" \
      -H "Content-Type: application/json" \
      -d "{\"children\": $blocks}")
    if echo "$content_result" | jq -e '.object == "list"' > /dev/null 2>&1; then
      echo "  ✓ Updated content"
    else
      echo "  ✗ Failed to update content: $(echo "$content_result" | jq -r '.message // "unknown error"')"
    fi
  fi
  echo "PAGE_ID=$existing" >&2
  exit 0
fi

# ── 不存在：新建 ──────────────────────────────────────────────
echo "Creating: $id - $title"

props=$(jq -n \
  --arg title "$title" \
  --arg id "$id" \
  --arg status "$status" \
  --arg priority "$priority" \
  --arg repo "$repo_name" \
  --arg type "$type" \
  --arg branch "$BRANCH" \
  '{
    "Title":    {"title":     [{"text": {"content": $title}}]},
    "ID":       {"rich_text": [{"text": {"content": $id}}]},
    "Status":   {"status":    {"name": $status}},
    "Type":     {"select":    {"name": $type}},
    "Priority": {"select":    {"name": $priority}},
    "Repo":     {"select":    {"name": $repo}},
    "Branch":   {"rich_text": [{"text": {"content": $branch}}]}
  }')

[[ -n "$estimate" ]] && \
  props=$(echo "$props" | jq --argjson e "$estimate" '. + {"Estimate": {"number": $e}}')
[[ -n "$version_id" ]] && \
  props=$(echo "$props" | jq --arg v "$version_id" '. + {"Version": {"relation": [{"id": $v}]}}')
[[ -n "$assignee_id" ]] && \
  props=$(echo "$props" | jq --arg a "$assignee_id" '. + {"Assignee": {"people": [{"id": $a}]}}')

result=$(curl -s -X POST "https://api.notion.com/v1/pages" \
  -H "Authorization: Bearer $NOTION_KEY" \
  -H "Notion-Version: $NOTION_VERSION" \
  -H "Content-Type: application/json" \
  -d "$(jq -n --arg db "$DATABASE_ID" --argjson props "$props" \
    '{"parent": {"database_id": $db}, "properties": $props}')")

page_id=$(echo "$result" | jq -r '.id // empty')

if [[ -z "$page_id" ]]; then
  echo "  ✗ Failed: $(echo "$result" | jq -r '.message // .error // "unknown error"')"
  exit 1
fi

echo "  ✓ Created: $page_id"

# 新建时追加文档内容
if [[ -n "$content" ]]; then
  blocks=$(echo "$content" | python3 "$SCRIPT_DIR/md2blocks.py")
  patch_result=$(curl -s -X PATCH "https://api.notion.com/v1/blocks/$page_id/children" \
    -H "Authorization: Bearer $NOTION_KEY" \
    -H "Notion-Version: $NOTION_VERSION" \
    -H "Content-Type: application/json" \
    -d "{\"children\": $blocks}")
  if echo "$patch_result" | jq -e '.object == "list"' > /dev/null 2>&1; then
    echo "  ✓ Added content"
  else
    echo "  ✗ Failed to add content: $(echo "$patch_result" | jq -r '.message // "unknown error"')"
  fi
fi

echo "PAGE_ID=$page_id" >&2
