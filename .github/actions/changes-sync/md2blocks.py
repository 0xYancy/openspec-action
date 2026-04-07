#!/usr/bin/env python3
"""
将 markdown 内容（从 stdin 读取）转换为 Notion blocks JSON。
用法: echo "$markdown" | python3 md2blocks.py

支持的 block 类型:
  - heading_1 / heading_2 / heading_3
  - bulleted_list_item / numbered_list_item
  - code (围栏代码块 ``` ... ```)
  - paragraph (默认)

支持的 inline 注解:
  - **bold**
  - `code`
  - [text](url)

限制:
  - 最多 50 个 block (Notion 单次写入便利上限)
  - 每段 rich_text content 最多 2000 字符 (Notion 硬限制)
"""
import sys
import json
import re

MAX_BLOCKS = 50
MAX_TEXT_LEN = 2000

# 行内 markdown 解析：bold / code / link，按出现顺序非贪婪匹配
INLINE_RE = re.compile(
    r'\*\*(.+?)\*\*'             # group 1: **bold**
    r'|`([^`]+)`'                # group 2: `code`
    r'|\[([^\]]+)\]\(([^)]+)\)'  # group 3: [text], group 4: (url)
)


def _make_text(content, *, bold=False, code=False, link=None):
    """构造一个 Notion rich_text 元素."""
    text_obj = {'content': content[:MAX_TEXT_LEN]}
    if link:
        text_obj['link'] = {'url': link}
    item = {'type': 'text', 'text': text_obj}
    annotations = {}
    if bold:
        annotations['bold'] = True
    if code:
        annotations['code'] = True
    if annotations:
        item['annotations'] = annotations
    return item


def parse_inline(text):
    """将一行文本解析成 Notion rich_text 数组."""
    if not text:
        return [_make_text('')]

    segments = []
    pos = 0
    for m in INLINE_RE.finditer(text):
        # 匹配前的普通文本
        if m.start() > pos:
            segments.append(_make_text(text[pos:m.start()]))

        if m.group(1) is not None:           # **bold**
            segments.append(_make_text(m.group(1), bold=True))
        elif m.group(2) is not None:         # `code`
            segments.append(_make_text(m.group(2), code=True))
        elif m.group(3) is not None:         # [text](url)
            segments.append(_make_text(m.group(3), link=m.group(4)))

        pos = m.end()

    # 末尾剩余的普通文本
    if pos < len(text):
        segments.append(_make_text(text[pos:]))

    return segments or [_make_text('')]


def make_block(block_type, *, rich_text=None, language=None):
    """生成一个 Notion block 对象."""
    body = {}
    if rich_text is not None:
        body['rich_text'] = rich_text
    if language is not None:
        body['language'] = language
    return {'object': 'block', 'type': block_type, block_type: body}


def main():
    content = sys.stdin.read()
    blocks = []

    # 围栏代码块状态机
    in_code_fence = False
    code_lang = 'plain text'
    code_buffer = []

    def flush_code_block():
        if not code_buffer:
            return
        joined = '\n'.join(code_buffer)[:MAX_TEXT_LEN]
        blocks.append(make_block(
            'code',
            rich_text=[_make_text(joined)],
            language=code_lang,
        ))

    for raw_line in content.splitlines():
        if len(blocks) >= MAX_BLOCKS:
            break
        line = raw_line.rstrip()

        # 围栏代码块开始/结束
        fence_match = re.match(r'^```(\w*)\s*$', line)
        if fence_match:
            if in_code_fence:
                flush_code_block()
                code_buffer = []
                in_code_fence = False
            else:
                in_code_fence = True
                code_lang = fence_match.group(1) or 'plain text'
            continue

        if in_code_fence:
            code_buffer.append(raw_line)
            continue

        # 跳过空行
        if not line:
            continue

        # heading
        if line.startswith('### '):
            blocks.append(make_block('heading_3', rich_text=parse_inline(line[4:].strip())))
        elif line.startswith('## '):
            blocks.append(make_block('heading_2', rich_text=parse_inline(line[3:].strip())))
        elif line.startswith('# '):
            blocks.append(make_block('heading_1', rich_text=parse_inline(line[2:].strip())))
        # 有序列表 1. 2. 3.
        elif re.match(r'^\d+\.\s+', line):
            text = re.sub(r'^\d+\.\s+', '', line)
            blocks.append(make_block('numbered_list_item', rich_text=parse_inline(text)))
        # 无序列表 - 或 *
        elif line.startswith(('- ', '* ')):
            blocks.append(make_block('bulleted_list_item', rich_text=parse_inline(line[2:].strip())))
        # 表格行（Notion 不支持原生 markdown 表格，降级为段落）
        elif line.startswith('| ') or line.startswith('|--'):
            blocks.append(make_block('paragraph', rich_text=parse_inline(line.strip())))
        else:
            blocks.append(make_block('paragraph', rich_text=parse_inline(line)))

    # 文档结尾仍在代码块中（异常情况，仍尝试输出）
    if in_code_fence and code_buffer and len(blocks) < MAX_BLOCKS:
        flush_code_block()

    print(json.dumps(blocks, ensure_ascii=False))


if __name__ == '__main__':
    main()
