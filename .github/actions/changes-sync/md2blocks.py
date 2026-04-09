#!/usr/bin/env python3
"""
将 markdown 内容（从 stdin 读取）转换为 Notion blocks JSON。
用法: echo "$markdown" | python3 md2blocks.py

支持的 block 类型:
  - heading_1 / heading_2 / heading_3
  - bulleted_list_item / numbered_list_item (含基于缩进的嵌套)
  - table (markdown 表格 → Notion table block)
  - code (围栏代码块 ``` ... ```)
  - paragraph (默认)

特殊语法:
  - %toggle% Title  → 生成可折叠的 heading_1，后续内容作为 children 直到下一个 %toggle%

支持的 inline 注解:
  - **bold**
  - `code`
  - [text](url)

限制:
  - 顶层最多 MAX_BLOCKS 个 block
  - 每个 toggle 内最多 MAX_TOGGLE_CHILDREN 个子 block
  - 每段 rich_text content 最多 2000 字符 (Notion 硬限制)
"""
import sys
import json
import re

MAX_BLOCKS = 200
MAX_TOGGLE_CHILDREN = 100
MAX_TEXT_LEN = 2000

TOGGLE_RE = re.compile(r'^%toggle%\s+(.+)$')

# 行内 markdown 解析：bold / code / link，按出现顺序非贪婪匹配
INLINE_RE = re.compile(
    r'\*\*(.+?)\*\*'             # group 1: **bold**
    r'|`([^`]+)`'                # group 2: `code`
    r'|\[([^\]]+)\]\(([^)]+)\)'  # group 3: [text], group 4: (url)
)


def _make_text(content, *, bold=False, code=False, link=None):
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
        if m.start() > pos:
            segments.append(_make_text(text[pos:m.start()]))

        if m.group(1) is not None:
            segments.append(_make_text(m.group(1), bold=True))
        elif m.group(2) is not None:
            segments.append(_make_text(m.group(2), code=True))
        elif m.group(3) is not None:
            segments.append(_make_text(m.group(3), link=m.group(4)))

        pos = m.end()

    if pos < len(text):
        segments.append(_make_text(text[pos:]))

    return segments or [_make_text('')]


def make_block(block_type, *, rich_text=None, language=None):
    body = {}
    if rich_text is not None:
        body['rich_text'] = rich_text
    if language is not None:
        body['language'] = language
    return {'object': 'block', 'type': block_type, block_type: body}


def leading_spaces(s):
    return len(s) - len(s.lstrip(' '))


TABLE_SEP_RE = re.compile(r'^\|[\s|:-]+$')


def _parse_table_row(line):
    """解析一行 | col1 | col2 | 为 cell rich_text 列表."""
    # 去掉首尾 |，按 | 切分
    inner = line.strip('|')
    return [parse_inline(cell.strip()) for cell in inner.split('|')]


def _make_table_block(rows):
    """将收集到的表格行转换为 Notion table block."""
    if not rows:
        return None
    # 计算列数（取最大值以容错）
    col_count = max(len(r) for r in rows)
    # 补齐列数不足的行
    for r in rows:
        while len(r) < col_count:
            r.append([_make_text('')])
    table_rows = []
    for r in rows:
        table_rows.append({
            'object': 'block',
            'type': 'table_row',
            'table_row': {'cells': r},
        })
    return {
        'object': 'block',
        'type': 'table',
        'table': {
            'table_width': col_count,
            'has_column_header': True,
            'has_row_header': False,
            'children': table_rows,
        },
    }


def parse_lines(content):
    """第一阶段：把每行解析为 (indent, block, is_listitem) 元组列表."""
    parsed = []

    in_code_fence = False
    code_lang = 'plain text'
    code_buffer = []
    table_buffer = []  # 收集连续的表格行

    def flush_code_block():
        if not code_buffer:
            return
        joined = '\n'.join(code_buffer)[:MAX_TEXT_LEN]
        parsed.append((0, make_block(
            'code',
            rich_text=[_make_text(joined)],
            language=code_lang,
        ), False))

    def flush_table():
        if not table_buffer:
            return
        block = _make_table_block(table_buffer)
        if block:
            parsed.append((0, block, False))

    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        stripped = line.lstrip(' ')

        # 围栏代码块开始/结束（允许前面有空格）
        fence_match = re.match(r'^```(\w*)\s*$', stripped)
        if fence_match:
            flush_table()
            table_buffer = []
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

        # 表格行检测：以 | 开头
        if stripped.startswith('|'):
            # 跳过分隔行 |---|---|
            if TABLE_SEP_RE.match(stripped):
                continue
            table_buffer.append(_parse_table_row(stripped))
            continue

        # 非表格行，先 flush 已收集的表格
        flush_table()
        table_buffer = []

        if not stripped:
            continue

        indent = leading_spaces(line)

        if stripped.startswith('### '):
            parsed.append((0, make_block(
                'heading_3', rich_text=parse_inline(stripped[4:].strip())), False))
        elif stripped.startswith('## '):
            parsed.append((0, make_block(
                'heading_2', rich_text=parse_inline(stripped[3:].strip())), False))
        elif stripped.startswith('# '):
            parsed.append((0, make_block(
                'heading_1', rich_text=parse_inline(stripped[2:].strip())), False))
        elif re.match(r'^\d+\.\s+', stripped):
            text = re.sub(r'^\d+\.\s+', '', stripped)
            parsed.append((indent, make_block(
                'numbered_list_item', rich_text=parse_inline(text)), True))
        elif stripped.startswith(('- ', '* ')):
            parsed.append((indent, make_block(
                'bulleted_list_item', rich_text=parse_inline(stripped[2:].strip())), True))
        else:
            parsed.append((0, make_block(
                'paragraph', rich_text=parse_inline(stripped)), False))

    if in_code_fence and code_buffer:
        flush_code_block()
    flush_table()

    return parsed


def build_tree(parsed):
    """第二阶段：基于缩进把 list item 嵌套为 Notion children 树."""
    blocks = []
    stack = []  # list of (indent, block_ref)

    for indent, block, is_listitem in parsed:
        if not is_listitem:
            # 非列表项重置嵌套栈
            stack = []
            blocks.append(block)
            continue

        # 弹出栈中所有缩进 >= 当前缩进的项（兄弟或祖先节点）
        while stack and stack[-1][0] >= indent:
            stack.pop()

        if stack:
            # 当前 item 是栈顶 item 的子节点
            _, parent = stack[-1]
            parent_body = parent[parent['type']]
            parent_body.setdefault('children', []).append(block)
        else:
            blocks.append(block)

        stack.append((indent, block))

    return blocks[:MAX_BLOCKS]


def split_toggle_sections(content):
    """按 %toggle% 标记切分内容为 (title|None, body) 列表."""
    sections = []
    current_title = None
    current_lines = []

    for line in content.splitlines():
        m = TOGGLE_RE.match(line.strip())
        if m:
            if current_lines or current_title is not None:
                sections.append((current_title, '\n'.join(current_lines)))
            current_title = m.group(1)
            current_lines = []
        else:
            current_lines.append(line)

    if current_lines or current_title is not None:
        sections.append((current_title, '\n'.join(current_lines)))

    return sections


def main():
    content = sys.stdin.read()
    sections = split_toggle_sections(content)

    all_blocks = []
    for title, body in sections:
        parsed = parse_lines(body)
        children = build_tree(parsed)
        if title is None:
            # toggle 标记之前的普通内容
            all_blocks.extend(children)
        else:
            # toggle block，文档内容作为 children
            toggle_block = {
                'object': 'block',
                'type': 'toggle',
                'toggle': {
                    'rich_text': parse_inline(title),
                    'children': children[:MAX_TOGGLE_CHILDREN],
                },
            }
            all_blocks.append(toggle_block)

    print(json.dumps(all_blocks[:MAX_BLOCKS], ensure_ascii=False))


if __name__ == '__main__':
    main()
