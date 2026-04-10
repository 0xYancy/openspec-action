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
  [Sora]="U020XGKHKF1"
  [Janpo]="U021CG7KR27"
  [Bonnie]="U02298KLM97"
  [chencheng]="U021CF4NA02"
  [AresCui]="U047E07BT9N"
  [hqwangningbo]="U03Q0J9FJGH"
  [Wonder]="U073BQX4X4G"
  [Kai]="U0A5JSTVDNF"
)

# 解析 mention：assignee 在表中 → @user，否则只 @Kai 并文字标注
MENTION=""
if [[ -n "$ASSIGNEE" ]]; then
  if [[ -n "${SLACK_IDS[$ASSIGNEE]:-}" ]]; then
    MENTION="<@${SLACK_IDS[$ASSIGNEE]}> <@U0A5JSTVDNF>"
  else
    MENTION="<@U0A5JSTVDNF> (assignee: $ASSIGNEE)"
  fi
fi

# 元数据变更字段翻译
declare -A FIELD_LABELS=(
  [title]="标题" [status]="状态" [type]="类型" [priority]="优先级"
  [estimate]="工时" [version]="版本" [assignee]="负责人" [branch]="分支"
)
META_TAGS=""
if [[ -n "$META_FIELDS" ]]; then
  for f in $META_FIELDS; do
    label="${FIELD_LABELS[$f]:-$f}"
    META_TAGS="${META_TAGS}\`${label}\`  "
  done
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
  if [[ -n "$META_TAGS" ]]; then
    blocks=$(echo "$blocks" | jq --arg m "$META_TAGS" \
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
  if [[ -n "$META_TAGS" ]]; then
    text="${text}
元数据变更：$(echo "$META_TAGS" | sed 's/`//g')"
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
