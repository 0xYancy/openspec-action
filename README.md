# openspec-action

将 [OpenSpec](https://github.com/0xYancy/openspec-f) changes 元数据与文档同步到 Notion Task 数据库的 GitHub Composite Action。

承载逻辑（原本在本地 OpenClaw + Cloudflare Tunnel 链路上跑）：

- 元数据字段同步（创建 / 增量更新，包含 status / priority normalize、Estimate 保护、已完成不回退）
- 文档整合（proposal / design / tasks / tests → 精简正文，调用 OpenRouter 免费模型）
- Notion 正文写入（Markdown → Notion blocks）
- Slack 通知（commit diff 摘要 + assignee @mention）
- GitHub Actions step summary 输出同步结果

## 用法

在调用方仓库的 workflow 中：

```yaml
name: Openspec Sync
on:
  push:
    paths:
      - 'openspec/changes/**'

jobs:
  sync:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Build metadata array
        id: meta
        run: |
          # ... grep + sed 从 .openspec.yaml 提取 metadata，输出 metadata / changed-files
          # 见 openspec-f/.github/workflows/openspec-sync.yml 参考实现

      - uses: 0xYancy/openspec-action/.github/actions/changes-sync@main
        with:
          metadata: ${{ steps.meta.outputs.metadata }}
          changed-files: ${{ steps.meta.outputs.changed-files }}
          repo: ${{ github.repository }}
          branch: ${{ github.ref_name }}
          commit-sha: ${{ github.sha }}
          before-sha: ${{ github.event.before }}
        env:
          NOTION_API_KEY: ${{ secrets.NOTION_API_KEY }}
          OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
          SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
```

## Inputs

| input | 必需 | 说明 |
|-------|-----|------|
| `metadata` | ✅ | JSON 数组：本次 push 涉及的 change 元数据 |
| `changed-files` | ✅ | 空格分隔的文件路径列表（来自 git diff --name-only） |
| `repo` | ✅ | owner/name 仓库标识 |
| `branch` | ✅ | 分支名 |
| `commit-sha` | ✅ | 本次 commit SHA |
| `before-sha` | ❌ | 上一个 commit SHA，用于计算 Slack 摘要的 diff |

## 必需的 Secrets

调用方仓库需要在 GitHub repo settings 中配置以下 3 个 secret：

| 名称 | 说明 |
|-----|-----|
| `NOTION_API_KEY` | Notion integration token |
| `OPENROUTER_API_KEY` | OpenRouter API key（用 `:free` 模型零成本） |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook URL |

## 可选的 env 覆盖（不配的话用脚本默认值）

以下值在 `sync-task.sh` / `llm-*.sh` 内有默认硬编码（不是 secret，是公开 ID 与版本号），如需覆盖，在调用方 workflow 的 `env:` 段落里追加同名变量即可：

| 名称 | 默认值 |
|-----|-------|
| `NOTION_VERSION` | `2025-09-03` |
| `NOTION_TASK_DB_ID` | `318bc3b8-8a84-8005-bc7e-c2ed7b4f6a40` |
| `NOTION_TASK_DS_ID` | `318bc3b8-8a84-802f-af8e-000b8589126c` |
| `NOTION_VERSION_DS_ID` | `f9098417-055a-41f3-bdc1-1f777df6ea6c` |
| `OPENROUTER_MODEL` | `qwen/qwen3.6-plus:free` |

## 行为细则

- **新建 task**：Notion 中按 ID 找不到对应记录 → 创建并附整合后的正文
- **元数据更新**：找到对应记录 → 逐字段比对，仅写差异
- **文档更新**：本次 push 修改了 change 目录下任一 `.md` 文件 → 重新整合并覆写正文 + 发 Slack 通知
- **纯元数据 push**：不调 LLM、不发 Slack
- **Estimate 保护**：Notion 中已有非空 Estimate 不会被覆盖
- **状态保护**：Notion 中已为「完成」的 task 不会被回退到旧状态
- **失败隔离**：单条 change 同步失败不阻断其他 change；Slack 通知失败仅 warning 不阻断 workflow

## 脚本组成

```
.github/actions/changes-sync/
├── action.yml         composite action 描述
├── run.sh             主入口：编排所有 step
├── sync-task.sh       Notion 字段同步（创建/更新，从 OpenClaw 迁移而来，凭据改为 env）
├── md2blocks.py       Markdown → Notion blocks 转换器
├── llm-integrate.sh   调 OpenRouter 整合 4 个文档为精简正文
├── llm-summarize.sh   调 OpenRouter 总结 commit diff 为 Slack 摘要
└── slack-notify.sh    向 Slack webhook 推送通知
```
