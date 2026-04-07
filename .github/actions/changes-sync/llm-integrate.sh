#!/bin/bash
# llm-integrate.sh - 将一个 change 的 proposal/design/tasks/tests 文档整合为精简 Notion 正文
# 用法: ./llm-integrate.sh <change-dir>
# 输出: 整合后的 markdown，stdout
# 凭据: OPENROUTER_API_KEY (env), OPENROUTER_MODEL (env)

set -euo pipefail

CHANGE_DIR=$1
: "${OPENROUTER_API_KEY:?OPENROUTER_API_KEY is required}"
OPENROUTER_MODEL="${OPENROUTER_MODEL:-stepfun/step-3.5-flash:free}"

# 拼接四个文档为一个输入
RAW=""
for f in proposal.md design.md tasks.md tests.md; do
  if [[ -f "$CHANGE_DIR/$f" ]]; then
    RAW="${RAW}

===== $f =====
$(cat "$CHANGE_DIR/$f")"
  fi
done

if [[ -z "$RAW" ]]; then
  echo "No documents found in $CHANGE_DIR" >&2
  exit 1
fi

PROMPT=$(cat <<'PROMPT_EOF'
你将收到一个软件变更的 proposal、design、tasks、tests 四类原始文档。请整合为一份适合 Notion 页面正文的精简 markdown。

整合原则：
- **精简**：去掉模板占位符、空 section、重复信息、冗余格式标记
- **合并**：多个文档的内容按逻辑合并，不要按原文件一一对应分 section。例如 proposal 里的背景 + design 里的方案可以合成一段连贯描述
- **提炼**：长段落提取要点用 bullet list 呈现；保留关键数据和结论，去掉推导过程
- **结构清晰**：建议 2-4 个 section,例如:
  - **背景与目标** - 为什么做、要解决什么问题（1-3 句）
  - **方案概要** - 怎么做、关键设计决策（bullet list）
  - **任务拆分** - 具体子任务（如有）
  - **验证要点** - 测试/验收标准（如有）
- **保持准确**：精简不等于丢信息。关键技术细节、数值、约束条件必须保留
- **篇幅控制**：整合后正文控制在原始内容的 30-50% 以内
- 仅输出整合后的 markdown，不要解释、不要前后缀，不要包裹代码块

原始文档：
PROMPT_EOF
)

FULL_INPUT="${PROMPT}
${RAW}"

# 构造 JSON payload
PAYLOAD=$(jq -n \
  --arg model "$OPENROUTER_MODEL" \
  --arg content "$FULL_INPUT" \
  '{model: $model, messages: [{role: "user", content: $content}]}')

# 调用 OpenRouter，最多重试 2 次
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
  echo "  ⚠ OpenRouter integrate attempt $attempt failed: $ERR" >&2
  sleep 2
done

echo "  ✗ OpenRouter integrate failed after $max_attempts attempts" >&2
exit 1
