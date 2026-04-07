#!/usr/bin/env python3
"""
将 markdown 内容（从 stdin 读取）转换为 Notion blocks JSON。
用法: echo "$markdown" | python3 md2blocks.py
"""
import sys
import json

content = sys.stdin.read()
blocks = []

for line in content.splitlines():
    line = line.rstrip()
    if not line:
        continue
    if len(blocks) >= 50:
        break
    if line.startswith('## '):
        t = line[3:].strip()[:2000]
        blocks.append({'object': 'block', 'type': 'heading_2',
                        'heading_2': {'rich_text': [{'text': {'content': t}}]}})
    elif line.startswith('# '):
        t = line[2:].strip()[:2000]
        blocks.append({'object': 'block', 'type': 'heading_1',
                        'heading_1': {'rich_text': [{'text': {'content': t}}]}})
    elif line.startswith(('- ', '* ')):
        t = line[2:].strip()[:2000]
        blocks.append({'object': 'block', 'type': 'bulleted_list_item',
                        'bulleted_list_item': {'rich_text': [{'text': {'content': t}}]}})
    elif line.startswith('| ') or line.startswith('|--'):
        # 表格行转为段落（Notion 不支持 markdown 表格 block）
        t = line.strip()[:2000]
        blocks.append({'object': 'block', 'type': 'paragraph',
                        'paragraph': {'rich_text': [{'text': {'content': t}}]}})
    else:
        t = line[:2000]
        blocks.append({'object': 'block', 'type': 'paragraph',
                        'paragraph': {'rich_text': [{'text': {'content': t}}]}})

print(json.dumps(blocks))
