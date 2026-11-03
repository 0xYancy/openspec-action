"""补登 Change 的确定性生成、归档预演及 PR 形式转换。"""
from __future__ import annotations
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / 'shared'))
from draft import CAPABILITY, check_draft, delta_sections, requirement_blocks
from store_lib import dump_yaml, load_yaml, parse_marker, render_marker, path_allowed


def record_path(store: Path, refs: list[str]) -> Path:
    key = hashlib.sha256('\n'.join(sorted(refs)).encode()).hexdigest()[:16]
    return store / 'openspec/observations' / (key + '.json')


def save_record(store: Path, items: list[dict], draft: dict, mode: str, **extra) -> None:
    path = record_path(store, [i['pr'] for i in items])
    path.parent.mkdir(parents=True, exist_ok=True)
    # 不保存 token、环境变量或实现仓完整 diff。
    selected = [{k: i.get(k) for k in ('pr', 'url', 'title', 'behavior', 'suggestion', 'merged_at')} for i in items]
    path.write_text(json.dumps({'version': 1, 'items': selected, 'draft': draft, 'mode': mode, **extra}, ensure_ascii=False, indent=2) + '\n')


def materialize(store: Path, name: str, items: list[dict], draft: dict, now: dt.datetime,
                meta: dict | None = None, proposal: str = '') -> dict:
    from write import new_change_id, ref_repo
    if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', name):
        raise ValueError('补登 Change 名非法')
    meta = dict(meta or {})
    meta.update(schema='store', status='已实现', tier=meta.get('tier') or 'light')
    meta.setdefault('created', now.date().isoformat())
    meta.setdefault('title', '补登：' + str(draft.get('title') or items[0]['title'])[:70])
    meta.setdefault('ID', new_change_id(store, now, set()))
    for key in ('priority', 'assignee', 'version', 'deadline'):
        meta.setdefault(key, '')
    meta.setdefault('type', 'feature')
    meta['repositories'] = sorted({ref_repo(i['pr']) for i in items})
    meta['implementations'] = [dict(pr=i['pr'], url=i['url'], merged_at=i.get('merged_at') or '',
        basis=str(i.get('behavior') or i.get('title') or '')[:200], hint=False, confidence='高') for i in items]
    for key in ('progress', 'estimate', 'actual_days'):
        meta.pop(key, None)
    directory = store / 'openspec/changes' / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / '.openspec.yaml').write_text(dump_yaml(meta))
    if not proposal:
        features, modified = [], []
        for delta in draft.get('deltas') or []:
            line = f"- **{delta['capability']}**（标识 `{delta['capability']}`）：{delta.get('purpose') or draft.get('summary') or items[0].get('behavior') or '对齐已合入实现的业务规则'}"
            (features if delta.get('purpose') else modified).append(line)
        proposal = ('## 摘要\n\n' + str(draft.get('summary') or items[0].get('behavior') or '') +
            '\n\n## 需求背景\n\n实现已经合入，现补充可追溯的业务规则与验收场景。\n\n## 涉及规范\n\n### 新增特性\n\n' +
            '\n'.join(features) + '\n\n### 改动特性\n\n' + '\n'.join(modified) + '\n\n## 实现来源\n\n' +
            '\n'.join(f"- [{i['pr']}]({i['url']})" for i in items) + '\n')
    (directory / 'proposal.md').write_text(proposal)
    for delta in draft.get('deltas') or []:
        if not CAPABILITY.fullmatch(str(delta.get('capability') or '')):
            raise ValueError('非法能力名')
        path = directory / 'specs' / delta['capability'] / 'spec.md'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(delta['markdown'])
    return meta


def preflight(store: Path, name: str, items: list[dict], draft: dict, now: dt.datetime) -> str:
    """复用真实 OpenSpec 归档与主 spec 严格校验，不在原分支留下试合并产物。"""
    from write import merge_draft
    from draft import load_capabilities
    specs = load_capabilities(store)
    error = check_draft(draft, specs, {d['capability'] for d in draft.get('deltas') or [] if d['capability'] not in specs})
    if error:
        return error
    with tempfile.TemporaryDirectory(prefix='obs-preflight-') as tmp:
        scratch = Path(tmp)
        shutil.copytree(store / 'openspec', scratch / 'openspec')
        materialize(scratch, name, items, draft, now)
        checked = subprocess.run(['openspec', 'validate', name, '--type', 'change', '--strict'], cwd=scratch, capture_output=True, text=True)
        if checked.returncode:
            return '补登 Change 校验失败：' + (checked.stdout + checked.stderr)[-1000:]
        _, error = merge_draft(scratch, items[0], draft, now)
        return error


def draft_from_spec_diff(store: Path, base: str) -> dict:
    """从当前主 spec 的实际差异还原合法 delta，保留人工审阅修改。"""
    paths = subprocess.check_output(['git', 'diff', '--name-only', base, 'HEAD', '--', 'openspec/specs'], cwd=store, text=True).splitlines()
    deltas = []
    for path in paths:
        match = re.fullmatch(r'openspec/specs/([a-z0-9-]+)/spec.md', path)
        if not match:
            continue
        current = store / path
        if not current.is_file():
            raise ValueError('转换暂不支持删除整个能力，请保留原 PR 人工处理')
        after = current.read_text()
        before = subprocess.run(['git', 'show', f'{base}:{path}'], cwd=store, capture_output=True, text=True)
        old = requirement_blocks(before.stdout) if before.returncode == 0 else {}
        new = requirement_blocks(after)
        if set(old) - set(new):
            raise ValueError('转换发现需求删除或重命名，需人工确认，不自动丢失旧需求')
        added = [text for title, text in new.items() if title not in old]
        modified = [text for title, text in new.items() if title in old and text.strip() != old[title].strip()]
        parts = []
        if added:
            parts.append('## ADDED Requirements\n\n' + '\n'.join(added))
        if modified:
            parts.append('## MODIFIED Requirements\n\n' + '\n'.join(modified))
        if not parts:
            continue
        purpose = re.search(r'^## Purpose\s*\n(.*?)(?=^## |\Z)', after, re.M | re.S)
        deltas.append({'capability': match[1], 'purpose': purpose[1].strip() if before.returncode and purpose else '', 'markdown': '\n\n'.join(parts).rstrip() + '\n'})
    if not deltas:
        raise ValueError('没有可转换的主 spec 行为差异，可能已被主分支覆盖')
    return {'title': '实现观察补登', 'summary': '按本 PR 当前审阅内容补登已合入实现的业务规则。', 'deltas': deltas,
            'backfill_mode': 'change', 'mode_reason': '产品通过标签选择保留独立 Change'}


def convert_tree(store: Path, base: str, marker: dict, mode: str, pr: int, now: dt.datetime) -> tuple[dict, str]:
    from write import merge_draft
    refs = list(dict.fromkeys(marker.get('prs') or []))
    if not refs:
        raise ValueError('观察 PR 缺少实现来源')
    records = []
    for path in sorted((store / 'openspec/observations').glob('*.json')):
        data = json.loads(path.read_text())
        if {i.get('pr') for i in data.get('items') or []} & set(refs):
            records.append((path, data))
    known = {i['pr']: i for _, r in records for i in r.get('items') or []}
    items = [known.get(ref) or {'pr': ref, 'url': 'https://github.com/' + ref.replace('#', '/pull/').replace('@', '/compare/'),
             'title': ref, 'behavior': '本 PR 记录的已合入业务变化', 'suggestion': ''} for ref in refs]
    name = marker.get('change') or next((r.get('change_name') for _, r in records if r.get('change_name')), f'obs-backfill-{pr}')
    current_mode = 'change' if (store / 'openspec/changes' / name).is_dir() else 'spec'
    if current_mode == mode:
        return {'change': name if mode == 'change' else None, 'kind': 'backfill' if mode == 'change' else 'patch', 'prs': refs}, 'already'
    saved_meta = next((r.get('change_meta') for _, r in records if r.get('change_meta')), None)
    proposal = next((r.get('proposal') for _, r in records if r.get('proposal')), '')
    if mode == 'spec':
        directory = store / 'openspec/changes' / name
        if not directory.is_dir():
            raise ValueError('活动补登 Change 不存在，可能已经归档')
        meta = load_yaml(directory / '.openspec.yaml')
        proposal = (directory / 'proposal.md').read_text()
        deltas = []
        for path in sorted((directory / 'specs').glob('*/spec.md')):
            purpose = re.search(rf'\*\*(.+?)\*\*（标识 `{re.escape(path.parent.name)}`）[：:]\s*(.+)', proposal)
            deltas.append({'capability': path.parent.name, 'markdown': path.read_text(), 'purpose': purpose[2].strip() if purpose else ''})
        plan = {'title': meta.get('title'), 'summary': '按产品已审阅的补登需求直接更新主 spec。', 'deltas': deltas}
        error = preflight(store, name + '-preview', items, plan, now)
        if error:
            raise ValueError(error)
        _, error = merge_draft(store, items[0], plan, now)
        if error:
            raise ValueError(error)
        shutil.rmtree(directory)
        saved_meta = meta
    else:
        plan = draft_from_spec_diff(store, base)
        # 只恢复当前 PR 对主规范的改动；已合并的 base 与无关内容不回退。
        paths = subprocess.check_output(['git', 'diff', '--name-only', base, 'HEAD', '--', 'openspec/specs'], cwd=store, text=True).splitlines()
        for path in paths:
            old = subprocess.run(['git', 'show', f'{base}:{path}'], cwd=store, capture_output=True)
            target = store / path
            if old.returncode:
                if target.is_file():
                    target.unlink()
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(old.stdout)
        error = preflight(store, name, items, plan, now)
        if error:
            raise ValueError(error)
        saved_meta = materialize(store, name, items, plan, now, saved_meta, proposal)
    for path, _ in records:
        path.unlink()
    save_record(store, items, plan, mode, change_name=name, change_meta=saved_meta, proposal=proposal)
    return {'change': name if mode == 'change' else None, 'kind': 'backfill' if mode == 'change' else 'patch', 'prs': refs}, 'converted'


def convert_pr(store: Path, repo: str, number: int, mode: str) -> str:
    def run(*args, cwd=store):
        return subprocess.check_output(args, cwd=cwd, text=True, stderr=subprocess.PIPE).strip()
    view = json.loads(run('gh', 'pr', 'view', str(number), '--repo', repo, '--json', 'state,isCrossRepository,headRefName,baseRefName,isDraft,body,labels'))
    labels = {x['name'] for x in view['labels']}
    marker = parse_marker(view['body'] or '')
    if view['state'] != 'OPEN' or view['isCrossRepository'] or 'obs' not in labels or not marker or not view['headRefName'].startswith('obs/'):
        raise ValueError('仅支持本仓库打开的观察补登 PR')
    if 'obs:archive' in labels or (marker.get('change') and marker.get('kind') != 'backfill' and marker.get('kind') != 'patch'):
        raise ValueError('不能转换已归档或普通需求关联 PR')
    branch, base_branch = view['headRefName'], view['baseRefName']
    run('git', 'fetch', 'origin', branch, base_branch)
    initial_paths = run('git', 'diff', '--name-only', f'origin/{base_branch}...origin/{branch}').splitlines()
    if any(not path_allowed(path) for path in initial_paths):
        raise ValueError('观察 PR 含受保护路径，拒绝自动转换')
    temporary = tempfile.mkdtemp(prefix='obs-convert-')
    work = Path(temporary) / 'work'
    try:
        run('git', 'worktree', 'add', '--detach', str(work), f'origin/{branch}')
        run('git', 'config', 'user.name', 'github-actions[bot]', cwd=work)
        run('git', 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com', cwd=work)
        run('git', 'merge', '--no-edit', f'origin/{base_branch}', cwd=work)
        updated, result = convert_tree(work, f'origin/{base_branch}', marker, mode, number, dt.datetime.now(dt.timezone.utc))
        if result != 'already':
            run('git', 'add', '-A', '--', 'openspec/changes', 'openspec/specs', 'openspec/observations', cwd=work)
            run('git', 'commit', '-m', f'obs: convert backfill to {mode}', cwd=work)
            run('git', 'push', 'origin', f'HEAD:refs/heads/{branch}', cwd=work)
    finally:
        subprocess.run(['git', 'worktree', 'remove', '--force', str(work)], cwd=store, capture_output=True)
        shutil.rmtree(temporary, ignore_errors=True)
    if result == 'already' and marker == updated:
        for label in ('obs:as-change', 'obs:as-spec'):
            if label in labels:
                run('gh', 'pr', 'edit', str(number), '--repo', repo, '--remove-label', label)
        return '当前已是所选形式，未重复转换或重置审阅状态'
    body = ('## 补登形式\n\n当前为' + ('完整 Change' if mode == 'change' else '直接更新主 spec') +
            '；通过 `obs:as-change` / `obs:as-spec` 可切换。合并前仍需审阅、Ready for review 并通过 Store 检查。\n\n' +
            '## 实现来源\n\n' + '\n'.join('- ' + ref for ref in updated['prs']) + '\n\n' + render_marker(updated))
    previous_body = re.sub(r"<!--\s*openspec-obs:.*?-->", "", view['body'] or '', flags=re.S).strip()
    if previous_body:
        body = body.replace(render_marker(updated), '') + "\n\n<details><summary>转换前的审阅说明</summary>\n\n" + previous_body[:45000] + "\n\n</details>\n\n" + render_marker(updated)
    title = f"obs: {'补登 Change' if mode == 'change' else '补登汇总'}（PR #{number}，{len(updated['prs'])} 个实现）"
    subprocess.run(['gh', 'pr', 'edit', str(number), '--repo', repo, '--title', title, '--body-file', '-'], input=body, text=True, check=True, cwd=store)
    if not view['isDraft']:
        run('gh', 'pr', 'ready', str(number), '--repo', repo, '--undo')
    # 仅成功后更新标签，保留失败请求供用户重跑 Actions。
    for label in ('obs:patch', 'obs:change', 'obs:as-change', 'obs:as-spec'):
        if label in labels:
            run('gh', 'pr', 'edit', str(number), '--repo', repo, '--remove-label', label)
    run('gh', 'pr', 'edit', str(number), '--repo', repo, '--add-label', 'obs:change' if mode == 'change' else 'obs:patch')
    return '已在同一 PR 追加转换提交；请审阅后重新标记 Ready for review'


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--store', type=Path, default=Path('.'))
    parser.add_argument('--repo', required=True)
    parser.add_argument('--pr', type=int, required=True)
    parser.add_argument('--mode', choices=['change', 'spec'], required=True)
    args = parser.parse_args()
    try:
        print(convert_pr(args.store, args.repo, args.pr, args.mode))
    except (ValueError, subprocess.CalledProcessError) as error:
        print('补登形式转换失败，未自动合并：' + (str(error) if isinstance(error, ValueError) else (error.stderr or '')[-1000:]))
        raise SystemExit(1)
