#!/usr/bin/env python3
"""实现观察的补登起草：为一个未登记的行为变化，让 DeepSeek 写出要并入主 spec 的 delta。由 write.py 按单元顺序调用。

分两步调用：先从现有能力中选出受影响的能力（可以是新能力），再基于这些能力的完整主 spec 与实现 diff 起草 delta；
校验不通过时带上原因重写一次。delta 能否合并、合并后能否通过严格校验由 write.py 负责。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from llm import call  # noqa: E402

CAPABILITY = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
DELTA_SECTION = re.compile(r"^## (ADDED|MODIFIED|REMOVED|RENAMED) Requirements\s*$", re.M)
REQUIREMENT = re.compile(r"^### Requirement:\s*(.+?)\s*$", re.M)
SCENARIO = re.compile(r"^#### Scenario:\s*(.+?)\s*$", re.M)
PATCH_CHARS = 20000


def load_capabilities(store: Path) -> dict[str, str]:
    root = store / "openspec" / "specs"
    return {p.parent.name: p.read_text(encoding="utf-8") for p in sorted(root.glob("*/spec.md"))} if root.is_dir() else {}


def capability_view(name: str, text: str) -> dict:
    purpose = re.search(r"^## Purpose\s*\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return {"name": name, "purpose": (purpose.group(1).strip() if purpose else "")[:300], "requirements": REQUIREMENT.findall(text)}


def unit_view(unit: dict) -> dict:
    return {k: unit.get(k) for k in ("pr", "title", "body", "files")} | {"patch": (unit.get("patch") or "")[:PATCH_CHARS]}


def ask(system_file: str, payload: dict, key: str, model: str, history: list[dict] | None = None) -> tuple[dict, list[dict]]:
    """返回 (结论, 本次对话)。传入 history 时在其后继续对话（用于带上校验原因重写）。"""
    messages = history or [
        {"role": "system", "content": (HERE / system_file).read_text(encoding="utf-8")},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, indent=2)},
    ]
    content = call(messages, key, model)
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("模型输出不是 JSON 对象")
    return result, messages + [{"role": "assistant", "content": content}]


def requirement_blocks(text: str) -> dict[str, str]:
    """需求标题 → 该需求的完整文本（到下一个需求或二级标题为止）。"""
    blocks, matches = {}, list(REQUIREMENT.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        block = text[match.start():end]
        heading = re.search(r"^## ", block, re.M)
        blocks[match.group(1)] = block[:heading.start()] if heading else block
    return blocks


def delta_sections(markdown: str) -> dict[str, str]:
    """delta 中各操作段落的正文：{"MODIFIED": ..., "ADDED": ...}。"""
    parts = DELTA_SECTION.split(markdown)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts) - 1, 2)}


def check_draft(draft: dict, specs: dict[str, str], new: set[str]) -> str:
    """specs 为选中的现有能力的主 spec 全文。返回不合法的原因；合法时返回空字符串。"""
    existing = set(specs)
    deltas = draft.get("deltas")
    if not isinstance(deltas, list) or not deltas:
        return "模型没有给出任何 delta"
    for delta in deltas:
        name = str(delta.get("capability") or "")
        markdown = str(delta.get("markdown") or "")
        if name not in existing | new or not CAPABILITY.match(name):
            return f"delta 指向了未选定的能力 `{name}`"
        sections = set(DELTA_SECTION.findall(markdown))
        if not sections or sections - {"ADDED", "MODIFIED"}:
            return f"`{name}` 的 delta 只能包含 ADDED / MODIFIED Requirements"
        if name in new and ("MODIFIED" in sections or len(str(delta.get("purpose") or "")) < 50):
            return f"新能力 `{name}` 只能新增需求，且 Purpose 不少于 50 个字符"
        if name in existing:
            current = requirement_blocks(specs[name])
            modified = requirement_blocks(delta_sections(markdown).get("MODIFIED", ""))
            unknown = [t for t in modified if t not in current]
            if unknown:
                return f"`{name}` 的 MODIFIED 需求在主 spec 中不存在：{'、'.join(unknown)}"
            # MODIFIED 整块替换原需求，漏写的原有场景会被删掉（openspec archive 也会因此拒绝合并）
            for title, block in modified.items():
                dropped = [sc for sc in SCENARIO.findall(current[title]) if sc not in SCENARIO.findall(block)]
                if dropped:
                    return f"`{name}` 的 MODIFIED 需求「{title}」漏掉了原有场景：{'、'.join(dropped)}；请原样保留它们，或只修改其内容"
    return ""


def widen(draft: dict, existing_text: dict[str, str], specs: dict[str, str], new: set[str]) -> list[str]:
    """delta 指向未选定的能力时一并纳入：现有能力补上其主 spec 参与校验，不存在的按新能力处理。返回新纳入的现有能力。"""
    added = []
    for delta in draft.get("deltas") or []:
        name = str(delta.get("capability") or "") if isinstance(delta, dict) else ""
        if not CAPABILITY.match(name) or name in specs or name in new:
            continue
        if name in existing_text:
            specs[name] = existing_text[name]
            added.append(name)
        else:
            new.add(name)
    return added


def draft_one(item: dict, unit: dict, existing_text: dict[str, str], key: str, model: str) -> dict:
    selected, _ = ask("draft_select.md", {
        "unit": unit_view(unit),
        "behavior": item.get("behavior"),
        "capabilities": [capability_view(n, t) for n, t in existing_text.items()],
    }, key, model)
    selected = selected.get("capabilities") or []
    picked = [c for c in selected if isinstance(c, dict) and CAPABILITY.match(str(c.get("name") or ""))][:3]
    if not picked:
        return {"error": "模型没有选出需要更新的能力"}
    existing = {c["name"] for c in picked if c["name"] in existing_text}
    new = {c["name"] for c in picked if c["name"] not in existing_text}
    specs = {n: existing_text[n] for n in sorted(existing)}
    draft, history = ask("draft_write.md", {
        "unit": unit_view(unit),
        "behavior": item.get("behavior"),
        "specs": specs,
        "new_capabilities": sorted(new),
    }, key, model)
    added = widen(draft, existing_text, specs, new)
    reason = check_draft(draft, specs, new)
    if reason:
        # 带上校验原因让模型重写一次；新纳入的现有能力之前没给过主 spec，这次一并提供
        extra = f"\n\n以下是你改动到的其他现有能力的完整主 spec：\n{json.dumps({n: specs[n] for n in added}, ensure_ascii=False, indent=2)}" if added else ""
        retry = history + [{"role": "user", "content": f"上次输出不合法：{reason}。请修正后重新输出完整的 JSON 对象。{extra}"}]
        draft, _ = ask("draft_write.md", {}, key, model, retry)
        widen(draft, existing_text, specs, new)
        reason = check_draft(draft, specs, new)
        if reason:
            return {"error": f"重写后仍不合法：{reason}"}
    return {
        "title": str(draft.get("title") or "")[:60],
        "summary": str(draft.get("summary") or ""),
        "deltas": [{"capability": d["capability"], "purpose": str(d.get("purpose") or "") if d["capability"] in new else "",
                    "markdown": str(d["markdown"]).strip() + "\n"} for d in draft["deltas"]],
    }
