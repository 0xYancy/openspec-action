#!/bin/bash
# slack-notify.sh - 向 Slack 发送一条 change 更新通知（文档变更 / 元数据变更 / 两者兼有）
# 优先使用 Bot Token (chat.postMessage)，fallback 到 Webhook
# 用法: ./slack-notify.sh <repo> <branch> <commit-short> <change-title> <changed-files-csv> <assignee> <notion-page-url> <summary-text> [meta-changed-fields]
# 凭据: SLACK_BOT_TOKEN + SLACK_CHANNEL (优先) 或 SLACK_WEBHOOK_URL (兼容)

set -euo pipefail

REPO=$1
BRANCH=$2
COMMIT_SHORT=$3
TITLE=$4
CHANGED_FILES=$5
ASSIGNEE=$6
NOTION_URL=$7
SUMMARY=$8
META_FIELDS=${9:-}

SLACK_CHANNEL="C0ARQG0J3LM"

if [[ -z "${SLACK_BOT_TOKEN:-}" && -z "${SLACK_WEBHOOK_URL:-}" ]]; then
  echo "  ⚠ Slack notify skipped: neither SLACK_BOT_TOKEN nor SLACK_WEBHOOK_URL set" >&2
  exit 0
fi

# assignee → Slack User ID 映射
declare -A SLACK_IDS=(
  [Yancy]="U0212LDM1LP"
  [Bayacat]="U0212LDM1LP"
  [Lurpis]="U082N83QPK3"
  [Edwin]="U020RGW3XBR"
  [Tyrone]="U021HEVMS7L"
  [SuperDupont]="U02THS5LJH3"
  [Tiebing]="U05K4RG1QMB"
  [Damian]="U072X5B9PAR"
  [Janpo]="U08J6UJ3X8Q"
  [Bonnie]="U02298KLM97"
  [chencheng]="U021CF4NA02"
  [Ares]="U047E07BT9N"
  [Ningbo]="U03Q0J9FJGH"
  [Wonder]="U073BQX4X4G"
  [Kai]="U0A5JSTVDNF"
  [Gemma]="U073JSDQWSV"
)

# 模糊匹配 assignee → Slack User ID
# 优先精确匹配，然后大小写不敏感匹配，最后子串匹配
resolve_slack_id() {
  local input="$1"
  # 精确匹配
  [[ -n "${SLACK_IDS[$input]:-}" ]] && echo "${SLACK_IDS[$input]}" && return
  # 大小写不敏感匹配
  local input_lower
  input_lower=$(echo "$input" | tr '[:upper:]' '[:lower:]')
  for key in "${!SLACK_IDS[@]}"; do
    if [[ "$(echo "$key" | tr '[:upper:]' '[:lower:]')" == "$input_lower" ]]; then
      echo "${SLACK_IDS[$key]}" && return
    fi
  done
  # 子串匹配（input 包含 key 或 key 包含 input）
  for key in "${!SLACK_IDS[@]}"; do
    local key_lower
    key_lower=$(echo "$key" | tr '[:upper:]' '[:lower:]')
    if [[ "$key_lower" == *"$input_lower"* || "$input_lower" == *"$key_lower"* ]]; then
      echo "${SLACK_IDS[$key]}" && return
    fi
  done
}

MENTION=""
if [[ -n "$ASSIGNEE" ]]; then
  MATCHED_ID=$(resolve_slack_id "$ASSIGNEE")
  if [[ -n "$MATCHED_ID" ]]; then
    MENTION="<@${MATCHED_ID}> <@U0A5JSTVDNF>"
  else
    MENTION="<@U0A5JSTVDNF> (assignee: $ASSIGNEE)"
  fi
fi

# 元数据变更解析（格式: field|旧值|新值，多条换行分隔）
declare -A FIELD_LABELS=(
  [title]="标题" [status]="状态" [type]="类型" [priority]="优先级"
  [estimate]="工时" [version]="版本" [assignee]="负责人" [branch]="分支"
)
META_DETAIL=""   # Block Kit mrkdwn 格式: "• 状态: 待开发 → 开发中"
META_PLAIN=""    # 纯文本 fallback
if [[ -n "$META_FIELDS" ]]; then
  while IFS= read -r diff_line; do
    [[ -z "$diff_line" ]] && continue
    field=$(echo "$diff_line" | cut -d'|' -f1)
    old_val=$(echo "$diff_line" | cut -d'|' -f2)
    new_val=$(echo "$diff_line" | cut -d'|' -f3)
    label="${FIELD_LABELS[$field]:-$field}"
    META_DETAIL="${META_DETAIL}• *${label}*:  ${old_val}  →  ${new_val}\n"
    META_PLAIN="${META_PLAIN}• ${label}: ${old_val} → ${new_val}
"
  done <<< "$META_FIELDS"
fi

# GitHub commit URL
COMMIT_URL="https://github.com/${REPO}/commit/${COMMIT_SHORT}"

# ── 构造 Block Kit blocks（Bot Token 专用）──

build_blocks() {
  local blocks="[]"

  # 标题
  if [[ -n "$CHANGED_FILES" ]]; then
    blocks=$(echo "$blocks" | jq --arg t "📝  *${TITLE}*  文档更新" \
      '. + [{"type":"header","text":{"type":"plain_text","text":"📝 '"$TITLE"' 文档更新","emoji":true}}]')
  else
    blocks=$(echo "$blocks" | jq --arg t "🔄  *${TITLE}*  任务信息更新" \
      '. + [{"type":"header","text":{"type":"plain_text","text":"🔄 '"$TITLE"' 任务信息更新","emoji":true}}]')
  fi

  # 来源上下文
  blocks=$(echo "$blocks" | jq \
    --arg repo "$REPO" --arg branch "$BRANCH" --arg sha "$COMMIT_SHORT" --arg url "$COMMIT_URL" \
    '. + [{"type":"context","elements":[
      {"type":"mrkdwn","text":("*" + $repo + "*  ·  `" + $branch + "`  ·  <" + $url + "|" + $sha + ">")}
    ]}]')

  # 分隔线
  blocks=$(echo "$blocks" | jq '. + [{"type":"divider"}]')

  # 文档变更区域
  if [[ -n "$CHANGED_FILES" ]]; then
    # 变更文件标签
    local file_tags=""
    IFS=', ' read -ra FILES_ARR <<< "$CHANGED_FILES"
    for f in "${FILES_ARR[@]}"; do
      [[ -n "$f" ]] && file_tags="${file_tags}\`${f}\`  "
    done

    blocks=$(echo "$blocks" | jq --arg files "$file_tags" \
      '. + [{"type":"section","text":{"type":"mrkdwn","text":("*变更文件*\n" + $files)}}]')

    # 改动摘要
    if [[ -n "$SUMMARY" ]]; then
      blocks=$(echo "$blocks" | jq --arg s "$SUMMARY" \
        '. + [{"type":"section","text":{"type":"mrkdwn","text":("*改动摘要*\n" + $s)}}]')
    fi
  fi

  # 元数据变更区域
  if [[ -n "$META_DETAIL" ]]; then
    blocks=$(echo "$blocks" | jq --arg m "$META_DETAIL" \
      '. + [{"type":"section","text":{"type":"mrkdwn","text":("*元数据变更*\n" + $m)}}]')
  fi

  # Notion 链接 + mention
  local actions_parts=""
  if [[ -n "$NOTION_URL" ]]; then
    actions_parts="<${NOTION_URL}|📎 Notion 页面>"
  fi
  if [[ -n "$MENTION" ]]; then
    [[ -n "$actions_parts" ]] && actions_parts="${actions_parts}    "
    actions_parts="${actions_parts}👤 ${MENTION}"
  fi
  if [[ -n "$actions_parts" ]]; then
    blocks=$(echo "$blocks" | jq '. + [{"type":"divider"}]')
    blocks=$(echo "$blocks" | jq --arg a "$actions_parts" \
      '. + [{"type":"context","elements":[{"type":"mrkdwn","text":$a}]}]')
  fi

  echo "$blocks"
}

# 纯文本 fallback（用于 Webhook 和 text 字段）
build_fallback_text() {
  local text="${REPO} | ${BRANCH} | ${COMMIT_SHORT}"
  if [[ -n "$CHANGED_FILES" ]]; then
    text="${text}
📝 ${TITLE} 文档更新
变更文件：${CHANGED_FILES}"
    [[ -n "$SUMMARY" ]] && text="${text}
改动摘要：${SUMMARY}"
  else
    text="${text}
🔄 ${TITLE} 任务信息更新"
  fi
  if [[ -n "$META_PLAIN" ]]; then
    text="${text}
元数据变更：
${META_PLAIN}"
  fi
  [[ -n "$NOTION_URL" ]] && text="${text}
🔗 ${NOTION_URL}"
  [[ -n "$MENTION" ]] && text="${text}
${MENTION}"
  echo "$text"
}

FALLBACK_TEXT=$(build_fallback_text)

# ── 发送方式：Bot Token 优先，Webhook 兜底 ──

if [[ -n "${SLACK_BOT_TOKEN:-}" ]]; then
  BLOCKS=$(build_blocks)

  PAYLOAD=$(jq -n \
    --arg ch "$SLACK_CHANNEL" \
    --arg text "$FALLBACK_TEXT" \
    --argjson blocks "$BLOCKS" \
    '{channel: $ch, text: $text, blocks: $blocks}')

  HTTP_CODE=$(curl -sS -o /tmp/slack_resp.txt -w "%{http_code}" -X POST \
    "https://slack.com/api/chat.postMessage" \
    -H "Authorization: Bearer $SLACK_BOT_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" || echo "000")

  if [[ "$HTTP_CODE" == "200" ]]; then
    OK=$(jq -r '.ok' /tmp/slack_resp.txt 2>/dev/null || echo "false")
    if [[ "$OK" == "true" ]]; then
      echo "  ✓ Slack notified (bot)"
      exit 0
    else
      ERR=$(jq -r '.error // "unknown"' /tmp/slack_resp.txt 2>/dev/null || echo "unknown")
      echo "  ⚠ Slack bot API error: $ERR" >&2
    fi
  else
    RESP_BODY=$(cat /tmp/slack_resp.txt 2>/dev/null || echo "(no body)")
    echo "  ⚠ Slack bot HTTP $HTTP_CODE: $RESP_BODY" >&2
  fi
  exit 1
fi

# Webhook 方式（兼容旧配置，仅纯文本）
if [[ -n "${SLACK_WEBHOOK_URL:-}" ]]; then
  PAYLOAD=$(jq -n --arg text "$FALLBACK_TEXT" '{text: $text}')

  HTTP_CODE=$(curl -sS -o /tmp/slack_resp.txt -w "%{http_code}" -X POST \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    "$SLACK_WEBHOOK_URL" || echo "000")

  if [[ "$HTTP_CODE" == "200" ]]; then
    echo "  ✓ Slack notified (webhook)"
  else
    RESP_BODY=$(cat /tmp/slack_resp.txt 2>/dev/null || echo "(no body)")
    echo "  ⚠ Slack webhook failed (HTTP $HTTP_CODE): $RESP_BODY" >&2
  fi
fi
