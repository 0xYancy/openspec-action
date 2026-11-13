#!/usr/bin/env python3
"""受限 Store PR 自动合入。执行可信 action 中的检查，不执行 PR 中的脚本。"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

ACTIONS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ACTIONS / 'shared'))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pr_summary import summarize, fallback_points
from store_lib import gh_request, parse_marker, render_marker, path_allowed, load_yaml
from strict_spec import check as strict_check

BOT = 'github-actions[bot]'
CHANNEL = 'C0ARQG0J3LM'  # task-manager-notify，与需求通知共用。
SYNC_DELAYS = (0, 1, 2, 4, 8)


class RetryLater(RuntimeError):
    """GitHub 引用尚未同步或提交并发变化，保持静默，下轮重新发现。"""



def failure_reason(args: tuple[str, ...], output: str) -> str:
    """通知保留可操作的失败原因，过滤凭据、临时路径和终端控制字符。"""
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', output)
    for name, value in os.environ.items():
        if not value or not re.search(r'TOKEN|SECRET|PASSWORD|API_KEY|PRIVATE_KEY', name, re.I):
            continue
        secrets = [value]
        # 多组织凭据以 JSON 传入，子值也不能出现在通知中。
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                secrets += [v for v in parsed.values() if isinstance(v, str)]
        except ValueError:
            pass
        for secret in sorted(secrets, key=len, reverse=True):
            if len(secret) >= 4:
                text = text.replace(secret, '[已隐藏]')
    text = re.sub(r'https?://[^/\s@]+@', 'https://[已隐藏]@', text)
    text = re.sub(r'(?i)(Bearer\s+|(?:token|key|secret|password)=)[^\s&]+', r'\1[已隐藏]', text)
    text = re.sub(r'/\S*(?:obs-archive|obs-validate)-[^/\s]+(?:/work)?', '<临时目录>', text)
    text = re.sub(r'/home/runner/work/_actions/[^\s:]+', '<action>', text)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    archive = len(args) > 1 and Path(args[1]).name == 'archive.py'
    issues = [line for line in lines if re.match(r'^[a-z0-9][a-z0-9-]*: \[(?:ERROR|WARNING)\] ', line)]
    placeholders = [line.split(':', 1)[0] for line in issues if 'Purpose section is still a placeholder' in line]
    if archive and issues and len(placeholders) == len(issues):
        capabilities = list(dict.fromkeys(placeholders))
        return ('归档校验未通过：' + str(len(capabilities)) + ' 个规范的 Purpose 仍为占位内容，需补充有效用途说明：'
                + '、'.join(capabilities))[:550]
    label = '归档失败' if archive else f'{Path(args[0]).name} 执行失败'
    if issues:
        details = '；'.join(line[:160] for line in issues[:3])
        if len(issues) > 3:
            details += f'；另有 {len(issues) - 3} 项，详见运行日志'
    else:
        important = [line for line in lines if re.search(r'error|fatal|exception|失败|不能归档|未通过|拒绝', line, re.I)
                     and not line.startswith(('Traceback', 'File '))]
        details = '；'.join((important or lines)[-3:])
    return label + ('：' + details[:480] if details else '，命令未返回错误详情，请查看运行日志')


def run(work: Path, *args: str) -> str:
    p = subprocess.run(args, cwd=work, capture_output=True, text=True)
    if p.returncode:
        output = p.stdout + '\n' + p.stderr
        print(output[-6000:])
        raise RuntimeError(failure_reason(args, output))
    return p.stdout.strip()


def escape(value: str) -> str:
    return value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def card(repo: str, title: str, summary: str, url: str, pr: dict | None = None, points: list[str] | None = None) -> dict:
    subject = re.sub(r'^(obs:|wiki:)\s*', '', (pr or {}).get('title', '')).strip()
    detail = (f'*{escape(subject[:120])}*\n' if subject else '') + escape(summary[:600])
    bullets = '\n'.join('• ' + escape(p) for p in points or [])
    if bullets:
        detail += '\n\n*改动摘要*\n' + bullets
    links = f'<{url}|查看 PR>' if pr else f'<{url}|查看运行>'
    run_url = os.environ.get('RUN_URL', '')
    if pr and run_url:
        links += f'  ·  <{run_url}|运行详情>'
    return {'channel': CHANNEL, 'text': f'{title} | {repo}\n{subject}\n{summary}\n' + ('改动摘要\n' + '\n'.join('• ' + p for p in points) + '\n' if points else '') + url,
            'unfurl_links': False, 'unfurl_media': False,
            'blocks': [
                {'type': 'header', 'text': {'type': 'plain_text', 'text': title}},
                {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': escape(repo) + (f' · PR #{pr["number"]}' if pr else '')}]},
                {'type': 'section', 'text': {'type': 'mrkdwn', 'text': detail}},
                {'type': 'divider'},
                {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': links}]},
            ]}


def slack(payload: dict) -> None:
    token = os.environ.get('SLACK_BOT_TOKEN', '')
    if not token:
        raise RuntimeError('缺少 SLACK_BOT_TOKEN，自动化通知未发送')
    request = urllib.request.Request('https://slack.com/api/chat.postMessage',
        data=json.dumps(payload).encode(), headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json; charset=utf-8'})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
    except Exception:
        raise RuntimeError('Slack 通知请求失败，请检查运行日志和 Bot 权限') from None
    if not result.get('ok'):
        raise RuntimeError('Slack 通知失败，请检查频道和 Bot 权限')


def eligible(pr: dict, repo: str, base: str) -> str:
    if pr.get('user', {}).get('login') != BOT or pr.get('head', {}).get('repo', {}).get('full_name') != repo or pr.get('base', {}).get('ref') != base:
        return ''
    branch, body = pr['head']['ref'], pr.get('body') or ''
    if branch.startswith('archive/'):
        return 'archive'
    if branch.startswith('obs/') and parse_marker(body):
        return 'observe'
    if branch == 'wiki/sync' and re.search(r'<!--\s*openspec-wiki:\s*\{', body):
        return 'wiki'
    return ''


def allowed_files(kind: str, paths: list[str]) -> bool:
    if not paths:
        return False
    if kind == 'wiki':
        return all(re.fullmatch(r'wiki/published/[^/]+\.(?:md|meta\.json)', p) and path_allowed(p) for p in paths)
    if kind == 'archive':
        return all(p.startswith(('openspec/changes/', 'openspec/specs/', 'openspec/observations/')) and path_allowed(p) for p in paths)
    # 观察只保留 Change 与证据；验收归档 PR 由已触发的独立验收流程生成。
    return all(p.startswith(('openspec/changes/', 'openspec/observations/')) and path_allowed(p) for p in paths)


class Pilot:
    def __init__(self, repo: str, base: str, work: Path):
        self.repo, self.base, self.work = repo, base, work
        self.token = os.environ['GH_TOKEN']
        self.prefix = f'/repos/{repo}'
        self.summary_cache: dict[tuple, list[str]] = {}

    def api(self, path: str, method: str = 'GET', body=None):
        return gh_request(self.prefix + path, self.token, method, body)

    def pages(self, path: str) -> list[dict]:
        rows = []
        for page in range(1, 21):
            batch = self.api(f'{path}{"&" if "?" in path else "?"}per_page=100&page={page}') or []
            rows += batch
            if len(batch) < 100:
                return rows
        raise RuntimeError('分页超过上限，拒绝在不完整数据上自动合入')

    def notices(self, pr: dict) -> set[str]:
        return {m for c in self.pages(f'/issues/{pr["number"]}/comments')
                if c.get('user', {}).get('login') == BOT
                for m in re.findall(r'<!-- openspec-autopilot:([^>]+) -->', c.get('body') or '')}

    def mark(self, pr: dict, key: str, text: str) -> None:
        self.api(f'/issues/{pr["number"]}/comments', 'POST', {'body': f'{text}\n\n<!-- openspec-autopilot:{key} -->'})

    def notify(self, pr: dict, key: str, title: str, summary: str) -> None:
        if key in self.notices(pr):
            return
        cache_key = (pr['number'], pr['head']['sha'])
        if cache_key not in self.summary_cache:
            try:
                files = self.pages(f'/pulls/{pr["number"]}/files')
                self.summary_cache[cache_key] = summarize(pr, files)
            except Exception:
                self.summary_cache[cache_key] = fallback_points(pr, [])
        slack(card(self.repo, title, summary, pr['html_url'], pr, self.summary_cache[cache_key]))
        self.mark(pr, key, f'{title}：{summary}')

    def wait_for_head(self, pr: dict, expected: str) -> dict:
        """推送后的 API head 必须追上本地提交，不能用仍指向旧树的 PR 继续校验。"""
        for delay in SYNC_DELAYS:
            if delay:
                time.sleep(delay)
            current = self.api(f'/pulls/{pr["number"]}')
            actual = current['head']['sha']
            print(f'PR #{pr["number"]}: 等待 PR 同步 expected={expected} api_head={actual}')
            if actual == expected:
                return current
        raise RetryLater(f'PR #{pr["number"]} 的 head 尚未同步到 {expected}，下轮重新校验')

    def fetch_pr_head(self, pr: dict) -> str:
        """API 与 refs/pull 都匹配同一不可变 SHA 后才允许创建校验工作树。"""
        ref = f'refs/openspec-autopilot/pr-{pr["number"]}'
        expected = pr['head']['sha']
        for delay in SYNC_DELAYS:
            if delay:
                time.sleep(delay)
            run(self.work, 'git', 'fetch', '--quiet', 'origin',
                f'+refs/heads/{self.base}:refs/remotes/origin/{self.base}', f'+refs/pull/{pr["number"]}/head:{ref}')
            actual = run(self.work, 'git', 'rev-parse', ref)
            print(f'PR #{pr["number"]}: 拉取校验引用 expected={expected} pull_ref={actual}')
            if actual == expected:
                return ref
        raise RetryLater(f'PR #{pr["number"]} 的引用仍未匹配 {expected}，下轮重新校验')

    def prepare_archive(self, pr: dict) -> dict:
        """在同一观察 PR 完成归档；新补登和匹配的手写 Change 均保留归档记录。"""
        marker = parse_marker(pr.get('body') or '') or {}
        name = str(marker.get('change') or '')
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]*', name):
            raise RuntimeError('观察 PR 缺少完整 Change，保留草稿等待重试或修正')
        run(self.work, 'git', 'fetch', '--quiet', 'origin', f'+refs/heads/{self.base}:refs/remotes/origin/{self.base}',
            f'+refs/heads/{pr["head"]["ref"]}:refs/remotes/origin/{pr["head"]["ref"]}')
        branch_head = run(self.work, 'git', 'rev-parse', f'origin/{pr["head"]["ref"]}')
        if branch_head != pr['head']['sha']:
            pr = self.wait_for_head(pr, branch_head)
            marker = parse_marker(pr.get('body') or '') or {}
            if marker.get('change') != name:
                raise RetryLater('观察 PR 的 Change 标记已变化，下轮重新发现')
        with tempfile.TemporaryDirectory(prefix='obs-archive-') as tmp:
            work = Path(tmp) / 'work'
            run(self.work, 'git', 'worktree', 'add', '--detach', str(work), pr['head']['sha'])
            try:
                paths = run(work, 'git', 'diff', '--name-only', f'origin/{self.base}...HEAD').splitlines()
                if not all(path_allowed(p) and p.startswith(('openspec/changes/', 'openspec/specs/', 'openspec/observations/')) for p in paths):
                    raise RuntimeError('观察 PR 含受保护路径，拒绝自动归档')
                archived = list((work / 'openspec/changes/archive').glob('*-' + name))
                active = work / 'openspec/changes' / name
                if archived and not active.exists():
                    if any(load_yaml(d / '.openspec.yaml').get('status') != '已归档' for d in archived):
                        raise RuntimeError('观察 PR 的归档状态不一致')
                    return pr
                if not active.is_dir():
                    raise RuntimeError('观察 PR 中未找到待归档的 Change')
                if any(p.startswith('openspec/specs/') for p in paths):
                    raise RuntimeError('未归档的观察 PR 含主 spec 直改，拒绝合入')
                for path in paths:
                    if path.startswith('openspec/observations/') and (work / path).is_file():
                        if json.loads((work / path).read_text()).get('validation_error'):
                            raise RuntimeError('补登草稿尚未通过归档预演，保留草稿等待修正')
                run(work, 'git', 'config', 'user.name', BOT)
                run(work, 'git', 'config', 'user.email', '41898282+github-actions[bot]@users.noreply.github.com')
                run(work, 'git', 'config', 'commit.gpgsign', 'false')
                run(work, 'git', 'merge', '--no-edit', f'origin/{self.base}')
                # 只执行可信 action 的归档器；先验 Change，再验实际合并结果。
                run(work, 'openspec', 'validate', name, '--type', 'change', '--strict')
                body_path = Path(tmp) / 'archive.md'
                run(work, 'python3', str(ACTIONS / 'archive/archive.py'), '--store', str(work), '--store-repo', self.repo,
                    '--change', name, '--body', str(body_path), '--pr', str(pr['number']), '--observed')
                paths = run(work, 'git', 'diff', '--name-only', 'HEAD').splitlines()
                paths += run(work, 'git', 'ls-files', '--others', '--exclude-standard').splitlines()
                if not all(path_allowed(p) and p.startswith(('openspec/changes/', 'openspec/specs/')) for p in paths):
                    raise RuntimeError('归档器产生了范围外改动，未推送')
                run(work, 'git', 'add', '-A', '--', 'openspec/changes', 'openspec/specs')
                run(work, 'git', 'commit', '-m', 'obs: 自动归档 ' + name)
                expected_head = run(work, 'git', 'rev-parse', 'HEAD')
                run(work, 'git', 'push', 'origin', f'HEAD:refs/heads/{pr["head"]["ref"]}')
                marker['archived'] = True
                body = re.sub(r'<!--\s*openspec-obs:.*?-->', '', pr.get('body') or '', flags=re.S).rstrip()
                body += '\n\n---\n\n' + body_path.read_text() + '\n' + render_marker(marker)
                self.api(f'/pulls/{pr["number"]}', 'PATCH', {'body': body})
            finally:
                subprocess.run(['git', 'worktree', 'remove', '--force', str(work)], cwd=self.work, capture_output=True)
        return self.wait_for_head(pr, expected_head)

    def validate(self, pr: dict, kind: str) -> None:
        sha = pr['head']['sha']
        self.fetch_pr_head(pr)
        self.validated_base = run(self.work, 'git', 'rev-parse', f'origin/{self.base}')
        # 文件范围从实际 Git 快照计算，避免 PR files API 在推送后返回旧差异。
        paths = run(self.work, 'git', 'diff', '--name-only', '--no-renames', f'{self.validated_base}...{sha}').splitlines()
        print(f'PR #{pr["number"]}: 校验 head={sha} base={self.validated_base} files={len(paths)}')
        if not allowed_files(kind, paths):
            raise RuntimeError('PR 含本流程范围外的路径，已停止合入')
        if kind == 'wiki':
            ancestor = run(self.work, 'git', 'merge-base', sha, self.validated_base)
            stale = run(self.work, 'git', 'diff', '--name-only', ancestor, self.validated_base, '--',
                        'openspec/specs', 'openspec/changes/archive', 'wiki')
            if stale:
                raise RuntimeError('Wiki 来源在生成后已有变化，等待下一轮整理重写后再合入')
        with tempfile.TemporaryDirectory(prefix='store-check-') as tmp:
            work = Path(tmp) / 'work'
            run(self.work, 'git', 'worktree', 'add', '--detach', str(work), sha)
            try:
                run(work, 'git', '-c', 'user.name=github-actions[bot]', '-c', 'user.email=41898282+github-actions[bot]@users.noreply.github.com', 'merge', '--no-edit', f'origin/{self.base}')
                # 不执行合入树中的文件，检查器来自本 action 的可信版本。
                run(work, 'openspec', 'validate', '--all')
                for path in paths:
                    match = re.fullmatch(r'openspec/specs/([^/]+)/spec.md', path)
                    if match and (work / path).is_file():
                        ok, failures, _ = strict_check(match[1], str(work))
                        if not ok:
                            print('\n'.join(failures))
                            raise RuntimeError('归档后的主 spec 严格校验失败')
                    if path.startswith('openspec/observations/') and (work / path).is_file():
                        evidence = json.loads((work / path).read_text())
                        if evidence.get('validation_error'):
                            raise RuntimeError('补登草稿尚未通过归档预演，已保留 PR 等待修正')
                run(work, 'python3', str(ACTIONS / 'store-check/check.py'), '--store', '.', '--base-ref', f'origin/{self.base}', '--author', BOT)
                # Wiki 的结构、出处校验由 wiki-sync 在提交前执行；自动合入额外限制为发布产物。
                # 即使只有元数据变更，也检查所有被触及的活动 Change。
                changes = {p.split('/')[2] for p in paths if p.startswith('openspec/changes/') and len(p.split('/')) > 3 and p.split('/')[2] != 'archive'}
                if kind == 'archive':
                    # Git 将整个 Change 识别为重命名时，仍从归档目录反查活动目录。
                    changes |= {re.sub(r'^\d{4}-\d{2}-\d{2}-', '', p.split('/')[3]) for p in paths
                                if p.startswith('openspec/changes/archive/') and len(p.split('/')) > 4}
                for name in sorted(changes):
                    if kind == 'archive' and (work / 'openspec/changes' / name).is_dir():
                        raise RuntimeError('归档 PR 仍包含活动 Change，拒绝合入')
                    if (work / 'openspec/changes' / name).is_dir():
                        run(work, 'openspec', 'validate', name, '--type', 'change', '--strict')
            finally:
                subprocess.run(['git', 'worktree', 'remove', '--force', str(work)], cwd=self.work, capture_output=True)

    def status(self, pr: dict, state: str) -> None:
        self.api(f'/statuses/{pr["head"]["sha"]}', 'POST', {'state': state, 'context': 'Store 检查',
            'description': '自动合入主动执行 Store 校验', 'target_url': os.environ.get('RUN_URL', pr['html_url'])})

    def followup(self, pr: dict) -> None:
        """GITHUB_TOKEN 合入不触发 push workflow；静默同步 Issue，Slack 只发最终合入卡片。"""
        sha = pr['merge_commit_sha']
        key = 'followup:' + sha
        notices = self.notices(pr)
        if key in notices:
            return
        stage = 'issues:' + sha
        if stage not in notices:
            run(self.work, 'git', 'fetch', '--quiet', 'origin', self.base)
            before = run(self.work, 'git', 'rev-parse', sha + '^1')
            try:
                run(self.work, 'python3', str(self.work / 'openspec/scripts/issue-delivery.py'), '--store', str(self.work),
                    '--store-repo', self.repo, '--before', before, '--sha', sha)
            except Exception:
                raise RuntimeError('合入成功，但需求 Issue 同步失败；下轮自动重试') from None
            self.mark(pr, stage, '合入后的需求 Issue 已同步。')
        self.mark(pr, key, '合入后的需求投递已完成；Slack 统一使用最终合入卡片。')

    def process(self, pr: dict, kind: str) -> None:
        # 接管记录用于中断后恢复，与 Slack 通知独立；兼容此前 opened 通知留下的记录。
        if not {'managed', 'opened'} & self.notices(pr):
            self.mark(pr, 'managed', '自动合入已接管；正常中间步骤静默，合入或失败时通知。')
        # 迁移旧的直接改主 spec 补登 PR，始终经完整 Change 合入。
        marker = parse_marker(pr.get('body') or '')
        if kind == 'observe' and marker and marker.get('kind') == 'patch':
            sys.path.insert(0, str(ACTIONS / 'observe'))
            from backfill import convert_pr
            convert_pr(self.work, self.repo, pr['number'], 'change')
            pr = self.api(f'/pulls/{pr["number"]}')
        if kind == 'observe':
            pr = self.prepare_archive(pr)
            kind = 'archive'
        self.validate(pr, kind)
        base_now = self.api('/git/ref/heads/' + self.base)['object']['sha']
        if base_now != self.validated_base:
            print(f'PR #{pr["number"]}: 主分支在校验后已变化，下一轮重新试合并')
            return
        self.status(pr, 'success')
        # GITHUB_TOKEN 产生的 PR workflow 可能等待批准；同名 Store 检查已由本任务主动执行，不依赖那次运行。
        checks = self.api(f'/commits/{pr["head"]["sha"]}/check-runs?filter=latest&per_page=100')
        if checks.get('total_count', 0) > 100:
            raise RuntimeError('检查数量超出上限，拒绝在不完整结果上合入')
        other_checks = [c for c in checks.get('check_runs', []) if c.get('name') != 'Store 检查']
        if any(c.get('status') == 'completed' and c.get('conclusion') not in ('success', 'neutral', 'skipped') for c in other_checks):
            raise RuntimeError('PR 的其他检查失败，保留 PR 下一轮重试')
        status = self.api(f'/commits/{pr["head"]["sha"]}/status')
        other_statuses = [c for c in status.get('statuses', []) if c.get('context') != 'Store 检查']
        if any(c.get('state') not in ('success', 'pending') for c in other_statuses):
            raise RuntimeError('PR 的其他状态检查失败，保留 PR 下一轮重试')
        if any(c.get('status') != 'completed' for c in other_checks) or any(c.get('state') == 'pending' for c in other_statuses):
            print(f'PR #{pr["number"]}: 等待其他检查完成，下一轮重试')
            return
        if pr.get('draft'):
            # gh 使用 GraphQL 将草稿转为就绪；无需其他人的批准操作。
            run(self.work, 'gh', 'pr', 'ready', str(pr['number']), '--repo', self.repo)
        result = self.api(f'/pulls/{pr["number"]}/merge', 'PUT', {'sha': pr['head']['sha'], 'merge_method': 'squash'})
        if not result.get('merged'):
            raise RuntimeError('GitHub 尚未允许合入，保留 PR 下轮重试')
        pr = self.api(f'/pulls/{pr["number"]}')
        self.notify(pr, 'merged', '✅ 自动化 PR 已合入', {'observe':'Change 已合入，状态为「已实现」。', 'wiki':'Wiki 已合入发布，知识中心将在下一次索引时更新。', 'archive':'需求已归档，主 spec 已同步，继续更新关联 Issue。'}[kind])
        self.followup(pr)

    def writer_running(self, kind: str) -> bool:
        names = {'observe': {'Store · 实现观察', 'Store · 观察 PR 归档', 'Store · 补登形式切换'},
                 'wiki': {'Store · 产品 wiki'}, 'archive': {'Store · 验收归档'}}[kind]
        runs = self.api('/actions/runs?status=in_progress&per_page=100')
        # 不在生产者仍可能追加提交时归档／合入；workflow_run 或下一轮恢复会继续处理。
        return runs.get('total_count', 0) > 100 or any(r.get('name') in names for r in runs.get('workflow_runs', []))

    def needs_followup(self, pr: dict) -> bool:
        if not pr.get('merged_at') or not eligible(pr, self.repo, self.base):
            return False
        notices = self.notices(pr)
        return bool({'managed', 'opened'} & notices) and 'followup:' + pr.get('merge_commit_sha', '') not in notices

    def pending(self) -> bool:
        """只读检查，不安装工具、不拉取仓库，也不发送空检查通知。"""
        if any(eligible(pr, self.repo, self.base)
               for pr in self.pages('/pulls?state=open&sort=created&direction=asc')):
            return True
        return any(self.needs_followup(pr)
                   for pr in self.pages('/pulls?state=closed&sort=updated&direction=desc'))

    def main(self) -> int:
        failures = 0
        processed = set()
        for pr in self.pages('/pulls?state=open&sort=created&direction=asc'):
            kind = eligible(pr, self.repo, self.base)
            if not kind:
                continue
            if self.writer_running(kind):
                print(f'PR #{pr["number"]}: 生成任务仍在运行，完成后继续自动处理')
                continue
            processed.add(pr['number'])
            try:
                self.process(pr, kind)
            except RetryLater as pending:
                print(str(pending))
            except Exception as error:
                failures += 1
                print(f'PR #{pr["number"]}: {error}')
                current = self.api(f'/pulls/{pr["number"]}')
                if current.get('merged'):
                    self.notify(current, f'followup-failed:{current["merge_commit_sha"]}', '⚠️ 合入后同步待重试', str(error))
                else:
                    self.status(current, 'failure')
                    digest = hashlib.sha256(str(error).encode()).hexdigest()[:12]
                    self.notify(current, f'failed:{current["head"]["sha"]}:{digest}', '⚠️ 自动化处理受阻', str(error))
        # 恢复「已合入但发通知／投递时中断」的运行。手动 PR 和历史无记录的 PR 不处理。
        for pr in self.pages('/pulls?state=closed&sort=updated&direction=desc'):
            if pr['number'] in processed or not self.needs_followup(pr):
                continue
            try:
                pr = self.api(f'/pulls/{pr["number"]}')
                self.notify(pr, 'merged', '✅ 自动化 PR 已合入', '已完成自动合入，继续静默同步需求 Issue。')
                self.followup(pr)
            except Exception as error:
                failures += 1
                self.notify(pr, f'followup-failed:{pr["merge_commit_sha"]}', '⚠️ 合入后同步待重试', str(error))
        if failures and os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write('handled-errors=true\n')
        return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', default=os.environ.get('GITHUB_REPOSITORY'))
    parser.add_argument('--base', default='main')
    parser.add_argument('--store', default='.')
    parser.add_argument('--alert', default='')
    parser.add_argument('--check', action='store_true', help='只读检查是否有待处理 PR 或合入后同步')
    args = parser.parse_args()
    if args.alert:
        slack(card(args.repo, '⚠️ Store 自动化运行异常', args.alert, os.environ['RUN_URL']))
        return 0
    pilot = Pilot(args.repo, args.base, Path(args.store).resolve())
    if args.check:
        pending = pilot.pending()
        if os.environ.get('GITHUB_OUTPUT'):
            with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
                output.write('pending=' + str(pending).lower() + '\n')
        message = '发现待处理自动化 PR 或合入后同步，将继续自动处理。' if pending else '暂无待处理自动化 PR 或合入后同步，已跳过自动合入。'
        print(message)
        if os.environ.get('GITHUB_STEP_SUMMARY'):
            with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
                summary.write(message + '\n')
        return 0
    return pilot.main()

if __name__ == '__main__':
    sys.exit(main())
