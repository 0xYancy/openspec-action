#!/bin/bash
# llm-summarize.sh - 将 commit diff 总结为简明改动摘要（用于 Slack 通知）
# 用法: ./llm-summarize.sh <change-name> <before-sha> <after-sha>
# 输出: 摘要文本（每行一条 bullet），stdout
# 凭据: OPENROUTER_API_KEY (env), OPENROUTER_MODEL (env)

set -euo pipefail

CHANGE_NAME=$1
BEFORE_SHA=$2
AFTER_SHA=$3

: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY is required}"
OPENROUTER_MODEL="${OPENROUTER_MODEL:-stepfun/step-3.5-flash:free}"

# 收集该 change 目录下所有 .md 文件的 diff
DIFF=$(git diff "$BEFORE_SHA" "$AFTER_SHA" -- \
  "openspec/changes/$CHANGE_NAME/*.md" \
  "openspec/changes/archive/$CHANGE_NAME/*.md" 2>/dev/null || true)

if [[ -z "$DIFF" ]]; then
  echo "No diff for $CHANGE_NAME" >&2
  exit 0
fi

# 截断过长 diff，避免免费模型上下文超限
DIFF_TRUNCATED=$(echo "$DIFF" | head -c 30000)

PROMPT=$(cat <<'PROMPT_EOF'
你将收到一段 git diff，是某个软件变更（change）目录下文档的 commit diff。请总结本次提交"改了什么、加了什么、删了什么"。

规则：
- 总结的是本次提交对文档做了哪些修改，不是文档整体内容
- 每条用一句话概括，使用中文，以 "• " 开头
- 条目数量按实际改动多少决定，3-8 条之间
- 聚焦实质改动（新增/修改/删除的内容点），忽略纯排版/空白
- 如果是新建文档（diff 全为新增），改为提炼文档核心内容的要点
- 仅输出 bullet 列表本身，不要前后说明、不要包裹代码块

git diff:
PROMPT_EOF
)

FULL_INPUT="${PROMPT}
${DIFF_TRUNCATED}"

PAYLOAD=$(jq -n \
  --arg model "$OPENROUTER_MODEL" \
  --arg content "$FULL_INPUT" \
  '{model: $model, messages: [{role: "user", content: $content}]}')

attempt=0
max_attempts=3
while (( attempt < max_attempts )); do
  attempt=$((attempt + 1))
  RESP=$(curl -sS -X POST "https://openrouter.ai/api/v1/chat/completions" \
    -H "Authorization: Bearer $OPENROUTER_API_KEY" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" || echo '{}')

  CONTENT=$(echo "$RESP" | jq -r '.choices[0].message.content // empty')
  if [[ -n "$CONTENT" ]]; then
    echo "$CONTENT"
    exit 0
  fi

  ERR=$(echo "$RESP" | jq -r '.error.message // .error // "no content returned"')
  echo "  ⚠ OpenRouter summarize attempt $attempt failed: $ERR" >&2
  sleep 2
done

echo "  ✗ OpenRouter summarize failed after $max_attempts attempts" >&2
exit 1
