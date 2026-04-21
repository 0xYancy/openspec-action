#!/bin/bash
# run.sh - composite action 主入口
# 基于 CHANGED_FILES 自行识别本次涉及的 change 目录，并直接从每个目录的 .openspec.yaml
# 加载元数据（PyYAML 解析 + python json.dumps 输出，规避所有特殊字符转义问题）。
# 对每条 change 完成：元数据同步 → 文档按分类直接搬运（如有文档变更）→ Notion 正文写入 → Slack 通知 → 日志输出
#
# 必需环境变量（由 action.yml 透传）：
#   CHANGED_FILES      空格分隔的本次 push 涉及的文件列表（来自 git diff）
#   REPO               owner/name
#   BRANCH             分支名
#   COMMIT_SHA         本次 commit SHA
#   BEFORE_SHA         上一个 commit SHA（用于 diff）
#
# 可选环境变量：
#   METADATA           旧版上游 workflow 传入的 JSON 数组；保留兼容但不再作为信源使用
#
# Secrets（必需）：
#   NOTION_API_KEY、OPENROUTER_API_KEY、SLACK_WEBHOOK_URL
#
# 公开 ID（已硬编码默认值，可 env 覆盖）：
#   NOTION_VERSION、NOTION_TASK_DB_ID、NOTION_TASK_DS_ID、NOTION_VERSION_DS_ID、OPENROUTER_MODEL

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

: "${METADATA:=}"
: "${CHANGED_FILES:=}"
: "${REPO:?REPO is required}"
: "${BRANCH:?BRANCH is required}"
: "${COMMIT_SHA:?COMMIT_SHA is required}"
: "${BEFORE_SHA:=}"

# 确保 BEFORE_SHA 可访问（GitHub Actions 默认 shallow clone，需要按需 fetch）
# 如果 fetch 失败，fallback 到 HEAD~1，避免 archive 移动检测失效
if [[ -n "$BEFORE_SHA" ]] && ! [[ "$BEFORE_SHA" =~ ^0+$ ]]; then
  if ! git cat-file -e "$BEFORE_SHA" 2>/dev/null; then
    echo "→ BEFORE_SHA $BEFORE_SHA not in local repo, attempting fetch..."
    if ! git fetch --depth=2 origin "$BEFORE_SHA" 2>/dev/null; then
      echo "  ⚠ fetch failed, fallback to HEAD~1"
      if git rev-parse HEAD~1 >/dev/null 2>&1; then
        BEFORE_SHA=$(git rev-parse HEAD~1)
        echo "  → using HEAD~1: $BEFORE_SHA"
      else
        # 尝试加深历史一层
        git fetch --deepen=1 origin 2>/dev/null || true
        if git rev-parse HEAD~1 >/dev/null 2>&1; then
          BEFORE_SHA=$(git rev-parse HEAD~1)
          echo "  → after deepen, using HEAD~1: $BEFORE_SHA"
        else
          echo "  ⚠ could not resolve any usable BEFORE_SHA, archive detection may be inaccurate"
        fi
      fi
    fi
  fi
fi

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

# 从 CHANGED_FILES 抽取本次涉及的 change 名字（按出现顺序去重）
# 这种方式纯字符串操作，免疫上游 JSON 组装时的特殊字符转义问题
CHANGE_NAMES=()
declare -A SEEN_CHANGE
for f in $CHANGED_FILES; do
  if [[ "$f" =~ ^(openspec/)?changes/(archive/)?([^/]+)/ ]]; then
    name="${BASH_REMATCH[3]}"
    if [[ -z "${SEEN_CHANGE[$name]:-}" ]]; then
      CHANGE_NAMES+=("$name")
      SEEN_CHANGE[$name]=1
    fi
  fi
done

COUNT=${#CHANGE_NAMES[@]}
if [[ "$COUNT" == "0" ]]; then
  echo "No changes to sync"
  echo "" >> "$SUMMARY_FILE"
  echo "_(本次 push 未涉及任何 change 目录)_" >> "$SUMMARY_FILE"
  exit 0
fi

OVERALL_STATUS=0

for CHANGE_NAME in "${CHANGE_NAMES[@]}"; do
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
    echo "| $CHANGE_NAME | — | ✗ dir not found | — | — |" >> "$SUMMARY_FILE"
    OVERALL_STATUS=1
    continue
  fi

  # 从 .openspec.yaml 加载元数据（PyYAML 解析 + json.dumps 自动转义所有特殊字符）
  if ! ENTRY=$(python3 "$SCRIPT_DIR/load-metadata.py" "$CHANGE_DIR" 2>&1); then
    echo "  ✗ Failed to load metadata: $ENTRY"
    echo "| $CHANGE_NAME | — | ✗ metadata failed | — | — |" >> "$SUMMARY_FILE"
    OVERALL_STATUS=1
    continue
  fi

  TITLE=$(echo "$ENTRY" | jq -r '.title // .change')
  ASSIGNEE=$(echo "$ENTRY" | jq -r '.assignee // empty')

  # 判断本次 push 是否涉及该 change 目录下任意 .md 文件（排除纯目录移动）
  # archive 操作只是把目录从 changes/xxx 移到 changes/archive/xxx，文件内容不变，
  # 但 git diff --name-only 会把新旧路径都列出来，导致误判为"文档变更"。
  # 解决方式：逐文件比较当前内容与 BEFORE_SHA 中同名文件的内容哈希，只有真正改过的才算。
  HAS_DOC_CHANGES=false
  CHANGED_DOCS=""

  # 构建 BEFORE_SHA 中该 change 可能存在的所有旧路径
  OLD_PATHS=()
  for prefix in "openspec/changes" "changes" "openspec/changes/archive" "changes/archive"; do
    OLD_PATHS+=("${prefix}/${CHANGE_NAME}")
  done

  for f in proposal.md design.md tasks.md tests.md; do
    if echo "$CHANGED_FILES" | grep -qE "(^| )$CHANGE_DIR/$f( |$)"; then
      # 文件出现在 diff 列表中，但需要检查内容是否真的变了
      CONTENT_CHANGED=true

      if [[ -n "$BEFORE_SHA" ]]; then
        CUR_HASH=$(git hash-object "$CHANGE_DIR/$f" 2>/dev/null || true)
        if [[ -n "$CUR_HASH" ]]; then
          for old_dir in "${OLD_PATHS[@]}"; do
            OLD_HASH=$(git rev-parse "$BEFORE_SHA:${old_dir}/${f}" 2>/dev/null || true)
            if [[ "$CUR_HASH" == "$OLD_HASH" ]]; then
              CONTENT_CHANGED=false
              break
            fi
          done
        fi
      fi

      if [[ "$CONTENT_CHANGED" == "true" ]]; then
        HAS_DOC_CHANGES=true
        CHANGED_DOCS="${CHANGED_DOCS}${f}, "
      fi
    fi
  done
  CHANGED_DOCS=${CHANGED_DOCS%, }

  if [[ "$HAS_DOC_CHANGES" == "false" ]] && echo "$CHANGED_FILES" | grep -qE "(^| )(openspec/)?changes/(archive/)?$CHANGE_NAME/"; then
    echo "  → Directory move detected (content unchanged), skipping doc re-sync"
  fi

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
    # 直接按文档分类拼接 md 文件，不经过 LLM 整理
    DOC_LABELS=("proposal.md:%toggle% 需求提案" "design.md:%toggle% 设计方案" "tasks.md:%toggle% 任务拆分" "tests.md:%toggle% 测试验证")
    for entry_label in "${DOC_LABELS[@]}"; do
      fname="${entry_label%%:*}"
      heading="${entry_label#*:}"
      fpath="$CHANGE_DIR/$fname"
      if [[ -f "$fpath" ]]; then
        doc_body=$(cat "$fpath")
        # 跳过空文件或纯模板占位
        if [[ -n "${doc_body// /}" ]]; then
          CONTENT="${CONTENT}${heading}
${doc_body}

"
        fi
      fi
    done
    if [[ -z "${CONTENT// /}" ]]; then
      echo "  ⚠ No document content found in $CHANGE_DIR"
      CONTENT=""
    else
      echo "  → Assembled docs: $(echo "${DOC_LABELS[*]}" | tr ' ' '\n' | while IFS=: read f _; do [[ -f "$CHANGE_DIR/$f" ]] && echo -n "$f "; done)"
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

  # 从 sync 日志中提取元数据变更详情（sync-task.sh 输出格式: META_DIFF=field|old|new）
  # 用 grep 整行匹配再 sed 提取，避免值含空格时被截断
  META_DIFFS=$(echo "$SYNC_LOG" | grep '^META_DIFF=' | sed 's/^META_DIFF=//' || true)

  # Slack 通知：文档变更 或 元数据变更 都发
  if [[ "$HAS_DOC_CHANGES" == "true" || -n "$META_DIFFS" ]]; then
    SUMMARY_TEXT=""
    if [[ "$HAS_DOC_CHANGES" == "true" ]]; then
      if [[ -n "$BEFORE_SHA" ]]; then
        # stderr 直通到 workflow 日志,便于排查重试 / 空 diff 等情况
        SUMMARY_TEXT=$(bash "$SCRIPT_DIR/llm-summarize.sh" "$CHANGE_NAME" "$BEFORE_SHA" "$COMMIT_SHA" || echo "")
      else
        echo "  ⚠ summarize skipped: BEFORE_SHA empty (likely first push on branch)" >&2
      fi
      [[ -z "$SUMMARY_TEXT" ]] && SUMMARY_TEXT="• 文档已更新（自动摘要不可用）"
    fi

    bash "$SCRIPT_DIR/slack-notify.sh" \
      "$REPO" \
      "$BRANCH" \
      "$COMMIT_SHORT" \
      "$TITLE" \
      "$CHANGED_DOCS" \
      "$ASSIGNEE" \
      "$PAGE_URL" \
      "$SUMMARY_TEXT" \
      "$META_DIFFS" || true
  fi
done

echo ""
echo "Done."
exit $OVERALL_STATUS
