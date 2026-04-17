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

# OPENROUTER_MODEL 支持逗号分隔的多个模型，按顺序作为 fallback
# 默认列表覆盖几个常见的 free 模型，单个被限流时自动切换下一个
DEFAULT_MODELS="nvidia/nemotron-3-super-120b-a12b:free,minimax/minimax-m2.5:free,z-ai/glm-4.5-air:free,google/gemma-4-31b-it:free"
MODELS_RAW="${OPENROUTER_MODEL:-$DEFAULT_MODELS}"
IFS=',' read -ra MODELS <<< "$MODELS_RAW"

# 收集该 change 目录下 4 个文档的 diff（显式文件名,避免 git pathspec glob 兼容性问题）
# 同时兼容两种仓库布局：标准 openspec/changes/ 与根布局 changes/
# git diff 会自动忽略不存在的路径，所以全部列出即可
PATHS=()
for base in \
  "openspec/changes/$CHANGE_NAME" \
  "openspec/changes/archive/$CHANGE_NAME" \
  "changes/$CHANGE_NAME" \
  "changes/archive/$CHANGE_NAME"; do
  for f in proposal.md design.md tasks.md tests.md; do
    PATHS+=("$base/$f")
  done
done

# 判断 BEFORE_SHA 是否为有效 commit（新分支首次 push 时为 null SHA 0000...）
IS_VALID_BEFORE=true
if [[ "$BEFORE_SHA" =~ ^0+$ ]] || ! git cat-file -e "$BEFORE_SHA" 2>/dev/null; then
  IS_VALID_BEFORE=false
fi

DIFF=""
if [[ "$IS_VALID_BEFORE" == "true" ]]; then
  DIFF=$(git diff "$BEFORE_SHA" "$AFTER_SHA" -- "${PATHS[@]}" 2>&1 || true)
fi

# 如果 diff 为空（新分支或路径不匹配），直接读取当前文件内容作为"全新增"
if [[ -z "$DIFF" ]]; then
  for p in "${PATHS[@]}"; do
    FILE_CONTENT=$(git show "$AFTER_SHA:$p" 2>/dev/null || true)
    if [[ -n "$FILE_CONTENT" ]]; then
      DIFF="${DIFF}
--- /dev/null
+++ b/${p}
$(echo "$FILE_CONTENT" | sed 's/^/+/')"
    fi
  done
fi

if [[ -z "$DIFF" ]]; then
  echo "  ⚠ summarize: no content found for $CHANGE_NAME at $AFTER_SHA (paths checked: ${#PATHS[@]} files)" >&2
  exit 0
fi

# 截断过长 diff，避免免费模型上下文超限
DIFF_TRUNCATED=$(echo "$DIFF" | head -c 30000)

PROMPT=$(cat <<'PROMPT_EOF'
你将收到一段 git diff，是某个软件变更（change）目录下文档的 commit diff。用最简洁的方式总结改了什么。

规则：
- 每条 10-20 字，使用中文，以 "• " 开头
- 最多 3 条，只写最关键的改动
- 只说"做了什么"，不解释细节
- 如果是新建文档，一句话概括文档的核心主题
- 仅输出 bullet 列表，不要前后说明

好的示例：
• 新增移动端响应式设计方案
• 拆分 faucet 为独立服务
• 补充集成测试用例

git diff:
PROMPT_EOF
)

FULL_INPUT="${PROMPT}
${DIFF_TRUNCATED}"

# 调用 OpenRouter：按模型列表顺序 fallback，多轮重试
max_rounds=2
round=0
while (( round < max_rounds )); do
  round=$((round + 1))
  for model in "${MODELS[@]}"; do
    model=$(echo "$model" | xargs)  # trim 前后空白
    [[ -z "$model" ]] && continue

    PAYLOAD=$(jq -n \
      --arg model "$model" \
      --arg content "$FULL_INPUT" \
      '{
        model: $model,
        messages: [{role: "user", content: $content}],
        max_tokens: 256,
        temperature: 0.3
      }')

    RESP=$(curl -sS -X POST "https://openrouter.ai/api/v1/chat/completions" \
      -H "Authorization: Bearer $OPENROUTER_API_KEY" \
      -H "Content-Type: application/json" \
      -H "HTTP-Referer: https://github.com/0xYancy/openspec-action" \
      -H "X-Title: openspec-action changes-sync" \
      -d "$PAYLOAD" || echo '{}')

    CONTENT=$(echo "$RESP" | jq -r '.choices[0].message.content // empty')
    if [[ -n "$CONTENT" ]]; then
      echo "  ✓ OpenRouter summarize succeeded with $model (round $round)" >&2
      echo "$CONTENT"
      exit 0
    fi

    ERR=$(echo "$RESP" | jq -r '.error.message // .error // "no content returned"')
    ERR_DETAIL=$(echo "$RESP" | jq -c '.error // empty' 2>/dev/null || echo "")
    echo "  ⚠ OpenRouter summarize failed with $model (round $round): $ERR" >&2
    [[ -n "$ERR_DETAIL" ]] && echo "    detail: $ERR_DETAIL" >&2
  done

  if (( round < max_rounds )); then
    echo "  ⏳ all models failed this round, sleeping 5s before next round..." >&2
    sleep 5
  fi
done

echo "  ✗ OpenRouter summarize failed: exhausted ${#MODELS[@]} model(s) × $max_rounds round(s)" >&2
exit 1
