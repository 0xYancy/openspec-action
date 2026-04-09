#!/bin/bash
# slack-notify.sh - 向 Slack 发送一条 change 文档更新通知
# 优先使用 Bot Token (chat.postMessage)，fallback 到 Webhook
# 用法: ./slack-notify.sh <repo> <branch> <commit-short> <change-title> <changed-files-csv> <assignee> <notion-page-url> <summary-text>
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

# 构造文本
NOTION_LINE=""
[[ -n "$NOTION_URL" ]] && NOTION_LINE="
🔗 ${NOTION_URL}"

TEXT="${REPO} | ${BRANCH} | ${COMMIT_SHORT}
📝 ${TITLE} 文档更新
变更文件：${CHANGED_FILES}
提交：${COMMIT_SHORT}

改动摘要：
${SUMMARY}
${NOTION_LINE}
${MENTION}"

# ── 发送方式：Bot Token 优先，Webhook 兜底 ──

if [[ -n "${SLACK_BOT_TOKEN:-}" ]]; then
  # Bot Token 方式：chat.postMessage
  PAYLOAD=$(jq -n --arg ch "$SLACK_CHANNEL" --arg text "$TEXT" \
    '{channel: $ch, text: $text}')

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

# Webhook 方式（兼容旧配置）
if [[ -n "${SLACK_WEBHOOK_URL:-}" ]]; then
  PAYLOAD=$(jq -n --arg text "$TEXT" '{text: $text}')

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
