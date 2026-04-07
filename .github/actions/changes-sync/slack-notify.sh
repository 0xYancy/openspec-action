#!/bin/bash
# slack-notify.sh - 向 Slack webhook 发送一条 change 文档更新通知
# 用法: ./slack-notify.sh <repo> <branch> <commit-short> <change-title> <changed-files-csv> <assignee> <notion-page-url> <summary-text>
# 凭据: SLACK_WEBHOOK_URL (env)

set -euo pipefail

REPO=$1
BRANCH=$2
COMMIT_SHORT=$3
TITLE=$4
CHANGED_FILES=$5
ASSIGNEE=$6
NOTION_URL=$7
SUMMARY=$8

: "${SLACK_WEBHOOK_URL:?SLACK_WEBHOOK_URL is required}"

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

# 构造文本（与原 SKILL.md Step 5 格式一致）
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

PAYLOAD=$(jq -n --arg text "$TEXT" '{text: $text}')

HTTP_CODE=$(curl -sS -o /tmp/slack_resp.txt -w "%{http_code}" -X POST \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" \
  "$SLACK_WEBHOOK_URL" || echo "000")

if [[ "$HTTP_CODE" == "200" ]]; then
  echo "  ✓ Slack notified"
else
  RESP_BODY=$(cat /tmp/slack_resp.txt 2>/dev/null || echo "(no body)")
  echo "  ⚠ Slack notify failed (HTTP $HTTP_CODE): $RESP_BODY" >&2
  # 不阻断 workflow
fi
