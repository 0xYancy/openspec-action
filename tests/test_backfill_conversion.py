from __future__ import annotations
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.github/actions/observe'))
sys.path.insert(0, str(ROOT / '.github/actions/shared'))
import backfill
import write
from store_lib import parse_marker

SPEC = '''# rewards Specification

## Purpose
定义用户领取奖励的业务规则，明确余额不足等异常情况。

## Requirements

### Requirement: 领取奖励
系统 SHALL 允许用户领取奖励。

#### Scenario: 正常领取
- **当** 用户点击领取
- **则** 奖励到账
'''
DELTA = '''## MODIFIED Requirements

### Requirement: 领取奖励
系统 SHALL 允许用户领取奖励，领取前显示确认。

#### Scenario: 正常领取
- **当** 用户点击领取
- **则** 确认后奖励到账

#### Scenario: 无余额
- **当** 用户余额不足
- **则** 提示余额不足
'''


class ConversionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'store'
        self.root.mkdir()
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.com')
        self.git('config', 'commit.gpgsign', 'false')
        path = self.root / 'openspec/specs/rewards/spec.md'
        path.parent.mkdir(parents=True)
        path.write_text(SPEC)
        self.commit()
        self.base = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-qb', 'obs/test')
        self.now = dt.datetime(2026, 11, 3, tzinfo=dt.timezone.utc)
        self.item = dict(pr='o/app#12', url='https://github.com/o/app/pull/12', title='奖励确认', behavior='领取前增加确认', suggestion='补充规范')
        self.plan = dict(title='奖励确认', summary='用户领取前确认。', backfill_mode='spec', mode_reason='现有规则补正', deltas=[dict(capability='rewards', purpose='', markdown=DELTA)])

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.check_output(['git', '-c', 'commit.gpgsign=false', *args], cwd=self.root, text=True, stderr=subprocess.PIPE).strip()

    def commit(self):
        self.git('add', '-A')
        self.git('commit', '-qm', 'fixture')

    def spec_branch(self):
        _, error = write.merge_draft(self.root, self.item, self.plan, self.now)
        self.assertEqual(error, '')
        backfill.save_record(self.root, [self.item], self.plan, 'spec')
        self.commit()
        return {'change': None, 'kind': 'patch', 'prs': [self.item['pr']]}

    def test_round_trip_preserves_manual_scenarios_and_identity(self):
        marker = self.spec_branch()
        path = self.root / 'openspec/specs/rewards/spec.md'
        path.write_text(path.read_text().replace('提示余额不足', '显示余额不足并保留输入'))
        self.commit()
        changed, state = backfill.convert_tree(self.root, self.base, marker, 'change', 123, self.now)
        self.assertEqual(state, 'converted')
        self.assertEqual(path.read_text(), SPEC)
        directory = self.root / 'openspec/changes' / changed['change']
        self.assertIn('显示余额不足并保留输入', (directory / 'specs/rewards/spec.md').read_text())
        before_meta = (directory / '.openspec.yaml').read_text()
        self.commit()
        direct, state = backfill.convert_tree(self.root, self.base, changed, 'spec', 123, self.now)
        self.assertEqual(state, 'converted')
        self.assertIsNone(direct['change'])
        self.assertFalse(directory.exists())
        self.assertIn('显示余额不足并保留输入', path.read_text())
        self.commit()
        again, _ = backfill.convert_tree(self.root, self.base, direct, 'change', 123, self.now)
        self.assertEqual(changed['change'], again['change'])
        self.assertEqual(before_meta, (directory / '.openspec.yaml').read_text())

    def test_preflight_uses_real_archive_and_does_not_mutate_source(self):
        before = self.git('status', '--porcelain')
        self.assertEqual(backfill.preflight(self.root, 'obs-test', [self.item], self.plan, self.now), '')
        self.assertEqual(self.git('status', '--porcelain'), before)
        self.assertEqual((self.root / 'openspec/specs/rewards/spec.md').read_text(), SPEC)

    def test_new_capability_with_short_real_purpose_is_archivable(self):
        plan = dict(self.plan, deltas=[dict(capability='new-rewards', purpose='负责新奖励入口。', markdown=DELTA.replace('MODIFIED', 'ADDED'))])
        self.assertEqual(backfill.preflight(self.root, 'obs-new', [self.item], plan, self.now), '')
        meta = backfill.materialize(self.root, 'obs-new', [self.item], plan, self.now)
        self.assertEqual(meta['status'], '已实现')
        self.assertIn('负责新奖励入口。', (self.root / 'openspec/changes/obs-new/proposal.md').read_text())

    def test_invalid_delta_reports_original_scenario_without_mutating(self):
        plan = dict(self.plan, deltas=[dict(capability='rewards', purpose='', markdown=DELTA.replace('#### Scenario: 正常领取', '#### Scenario: 被改名'))])
        error = backfill.preflight(self.root, 'obs-test', [self.item], plan, self.now)
        self.assertIn('漏掉了原有场景', error)
        self.assertEqual((self.root / 'openspec/specs/rewards/spec.md').read_text(), SPEC)

    def test_duplicate_conversion_does_not_generate_more_changes(self):
        marker = self.spec_branch()
        changed, _ = backfill.convert_tree(self.root, self.base, marker, 'change', 123, self.now)
        self.commit()
        # 模拟 git push 成功但 PR 元数据更新失败，仍拿到旧 marker 时可以恢复。
        recovered, state = backfill.convert_tree(self.root, self.base, marker, 'change', 123, self.now)
        self.assertEqual(state, 'already')
        self.assertEqual(changed, recovered)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_deleting_requirement_is_not_silently_converted(self):
        path = self.root / 'openspec/specs/rewards/spec.md'
        path.write_text(SPEC.replace('### Requirement: 领取奖励', '### Requirement: 改名领取'))
        self.commit()
        with self.assertRaisesRegex(ValueError, '删除或重命名'):
            backfill.draft_from_spec_diff(self.root, self.base)

    def test_label_conversion_pushes_same_branch_without_force(self):
        marker = self.spec_branch()
        bare = Path(self.tmp.name) / 'remote.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(bare)], check=True)
        self.git('remote', 'add', 'origin', str(bare))
        self.git('push', '-q', 'origin', 'main', 'obs/test')
        self.git('checkout', '-q', 'main')
        fake = Path(self.tmp.name) / 'bin';fake.mkdir()
        view = Path(self.tmp.name) / 'view.json'
        view.write_text(json.dumps(dict(state='OPEN', isCrossRepository=False, headRefName='obs/test', baseRefName='main', isDraft=False,
            body='原说明\n' + write.render_marker(marker), labels=[dict(name='obs'), dict(name='obs:patch')])))
        executable = fake / 'gh'
        executable.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
p=Path(os.environ['TEST_PR_VIEW']);d=json.loads(p.read_text());args=sys.argv[1:]
if args[:2]==['pr','view']:print(json.dumps(d))
elif args[:2]==['pr','edit']:
 if '--body-file' in args:d['body']=sys.stdin.read()
 if '--title' in args:d['title']=args[args.index('--title')+1]
 p.write_text(json.dumps(d))
''')
        executable.chmod(0o755)
        before=self.git('rev-parse','origin/obs/test')
        with patch.dict(os.environ, {'PATH': str(fake)+os.pathsep+os.environ['PATH'], 'TEST_PR_VIEW': str(view),
             'GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'commit.gpgsign','GIT_CONFIG_VALUE_0':'false'}):
            result=backfill.convert_pr(self.root,'o/store',123,'change')
        after=self.git('rev-parse','origin/obs/test')
        self.assertNotEqual(before,after)
        self.assertEqual(self.git('merge-base','--is-ancestor',before,after),'')
        self.assertEqual(parse_marker(json.loads(view.read_text())['body'])['change'],'obs-backfill-123')
        self.assertIn('同一 PR',result)


if __name__ == '__main__':
    unittest.main()
