import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('autopilot', ROOT / '.github/actions/store-autopilot/autopilot.py')
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


def pr(kind='observe', **extra):
    return dict(number=1, title='obs: 新增确认', html_url='https://github.com/o/s/pull/1', user={'login': a.BOT},
        head={'sha': 'head', 'ref': 'obs/change' if kind == 'observe' else 'wiki/sync', 'repo': {'full_name': 'o/s'}},
        base={'ref': 'main'}, body='<!-- openspec-obs: {"change":"a","prs":["o/d#1"]} -->' if kind == 'observe' else '<!-- openspec-wiki: {"run":"x"} -->', **extra)


class GuardTest(unittest.TestCase):
    def test_bot_same_repo_default_branch_and_marker_are_all_required(self):
        self.assertEqual(a.eligible(pr(), 'o/s', 'main'), 'observe')
        self.assertEqual(a.eligible(pr('wiki'), 'o/s', 'main'), 'wiki')
        for field, value in [('user', {'login': 'human'}), ('body', ''), ('base', {'ref':'develop'}),
                             ('head', {'ref':'obs/a', 'repo':{'full_name':'fork/s'}})]:
            p = pr(); p[field] = value
            self.assertFalse(a.eligible(p, 'o/s', 'main'))

    def test_observe_cannot_directly_change_main_spec_or_framework(self):
        self.assertTrue(a.allowed_files('observe', ['openspec/changes/a/.openspec.yaml', 'openspec/observations/a.json']))
        for p in ['openspec/specs/a/spec.md', '.github/workflows/run.yml', '.openspec-store/store.yaml', '../x', 'openspec/changes/../../x', 'wiki/structure.yaml']:
            self.assertFalse(a.allowed_files('observe', [p]))
        self.assertFalse(a.allowed_files('observe', []))

    def test_wiki_only_published_artifacts(self):
        self.assertTrue(a.allowed_files('wiki', ['wiki/published/a.md', 'wiki/published/a.meta.json']))
        for p in ['wiki/structure.yaml', 'wiki/drafts/a.md', 'wiki/published/a.py', 'openspec/changes/a/proposal.md']:
            self.assertFalse(a.allowed_files('wiki', [p]))

    def test_card_has_hierarchy_links_and_escapes_untrusted_mentions(self):
        payload = a.card('o/s', '✅ 自动化 PR 已合入', '<!channel> 已完成', 'https://github.com/o/s/pull/1', pr())
        self.assertEqual(payload['channel'], 'C0ARQG0J3LM')
        self.assertEqual([b['type'] for b in payload['blocks']], ['header', 'context', 'section', 'divider', 'context'])
        self.assertIn('&lt;!channel&gt;', payload['blocks'][2]['text']['text'])
        self.assertIn('查看 PR', payload['blocks'][-1]['elements'][0]['text'])


class FlowTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, GH_TOKEN='test'); self.env.start()
        self.tmp = tempfile.TemporaryDirectory(); self.pilot = a.Pilot('o/s','main',Path(self.tmp.name))
        self.events=[]; self.marks=set()
        self.pilot.notices=lambda p:set(self.marks)
        self.pilot.mark=lambda p,k,t:self.marks.add(k)
        self.pilot.notify=lambda p,k,t,s:self.events.append(k)
        def api(path, method='GET', body=None):
            self.events.append((path, method, body))
            if '/merge' in path: return {'merged':True}
            if path.startswith('/git/ref'): return {'object':{'sha':'base'}}
            return pr(merged=True, merge_commit_sha='merged')
        self.pilot.api=api
        self.pilot.followup=lambda p:self.events.append('followup')
        self.pilot.prepare_archive=lambda p:p
        self.pilot.validated_base='base'

    def tearDown(self):
        self.tmp.cleanup(); self.env.stop()

    def test_success_validates_before_merging_and_pins_head_sha(self):
        self.pilot.validate=lambda p,k:self.events.append('validated')
        with patch.object(a, 'run', return_value='') as run:
            self.pilot.process(pr(draft=True),'observe')
        merge=[e for e in self.events if isinstance(e,tuple) and e[0].endswith('/merge')][0]
        self.assertLess(self.events.index('validated'), self.events.index(merge))
        self.assertEqual(merge[2],{'sha':'head','merge_method':'squash'})
        self.assertTrue(any(c.args[1:4] == ('gh','pr','ready') for c in run.call_args_list))
        self.assertIn('merged',self.events); self.assertIn('followup',self.events)

    def test_failed_validation_never_marks_ready_or_merges(self):
        self.pilot.validate=lambda p,k:(_ for _ in ()).throw(RuntimeError('invalid'))
        with patch.object(a,'run') as run, self.assertRaises(RuntimeError):
            self.pilot.process(pr(draft=True),'observe')
        run.assert_not_called()
        self.assertFalse(any(isinstance(e,tuple) and e[0].endswith('/merge') for e in self.events))

    def test_base_changed_after_validation_stops_merge(self):
        self.pilot.validate=lambda p,k:None
        self.pilot.validated_base='old'
        with self.assertRaisesRegex(RuntimeError,'主分支'):
            self.pilot.process(pr(),'observe')
        self.assertFalse(any(isinstance(e,tuple) and e[0].endswith('/merge') for e in self.events))

    def test_existing_notice_is_not_resent(self):
        self.pilot.notify=a.Pilot.notify.__get__(self.pilot)
        self.marks={'opened'}
        with patch.object(a,'slack') as slack:
            self.pilot.notify(pr(),'opened','x','y')
        slack.assert_not_called()

    def test_followup_retries_only_failed_stage(self):
        self.pilot.followup=a.Pilot.followup.__get__(self.pilot)
        p=pr(merge_commit_sha='merged')
        self.marks={'issues:merged'}
        with patch.object(a,'run',return_value='before') as run:
            self.pilot.followup(p)
        calls=[c for c in run.call_args_list if c.args[1] == 'python3']
        self.assertEqual(len(calls),1)
        self.assertIn('--notify',calls[0].args)
        self.assertIn('followup:merged',self.marks)

    def test_issue_failure_does_not_block_notification_and_remains_retryable(self):
        self.pilot.followup=a.Pilot.followup.__get__(self.pilot)
        def run(work,*args):
            if args[0]=='python3' and '--notify' not in args: raise RuntimeError('issue failure')
            return 'before'
        with patch.object(a,'run',side_effect=run), self.assertRaisesRegex(RuntimeError,'下轮自动重试'):
            self.pilot.followup(pr(merge_commit_sha='merged'))
        self.assertIn('notify:merged',self.marks)
        self.assertNotIn('followup:merged',self.marks)

    def test_bot_comment_required_for_dedup(self):
        self.pilot.notices=a.Pilot.notices.__get__(self.pilot)
        self.pilot.pages=lambda p:[{'user':{'login':'human'},'body':'<!-- openspec-autopilot:opened -->'},
                                   {'user':{'login':a.BOT},'body':'<!-- openspec-autopilot:merged -->'}]
        self.assertEqual(self.pilot.notices(pr()),{'merged'})
    def test_running_generator_defers_merge_until_generation_finishes(self):
        self.pilot.api=lambda *args: {'total_count':1,'workflow_runs':[{'name':'Store · 实现观察'}]}
        self.assertTrue(self.pilot.writer_running('observe'))
        self.assertFalse(self.pilot.writer_running('wiki'))
        self.pilot.pages=lambda path: [pr()] if 'state=open' in path else []
        self.pilot.process=lambda *args: self.fail('producer is still writing')
        self.assertEqual(self.pilot.main(),0)



class PendingTest(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, GH_TOKEN='test'); self.env.start()
        self.pilot = a.Pilot('o/s', 'main', Path('.'))
        self.open, self.closed, self.comments = [], [], []
        self.pilot.pages = lambda path: (self.open if 'state=open' in path else
                                        self.closed if 'state=closed' in path else self.comments)

    def tearDown(self):
        self.env.stop()

    def test_no_pr_or_only_human_pr_is_not_pending(self):
        self.assertFalse(self.pilot.pending())
        p = pr(); p['user'] = {'login': 'human'}
        self.open = [p]
        self.assertFalse(self.pilot.pending())

    def test_automation_pr_is_pending_without_mutations_or_tools(self):
        self.open = [pr()]
        with patch.object(a, 'run') as run, patch.object(a, 'slack') as slack:
            self.assertTrue(self.pilot.pending())
        run.assert_not_called(); slack.assert_not_called()

    def test_closed_unmerged_and_untracked_historical_pr_are_not_pending(self):
        self.closed = [pr(merged_at=None), pr(merged_at='now', merge_commit_sha='merge')]
        self.assertFalse(self.pilot.pending())

    def test_merged_pr_with_unfinished_followup_remains_pending(self):
        self.closed = [pr(merged_at='now', merge_commit_sha='merge')]
        self.comments = [{'user': {'login': a.BOT}, 'body': '<!-- openspec-autopilot:opened -->'}]
        self.assertTrue(self.pilot.pending())
        self.comments.append({'user': {'login': a.BOT}, 'body': '<!-- openspec-autopilot:followup:merge -->'})
        self.assertFalse(self.pilot.pending())

    def test_human_cannot_create_or_complete_recovery_markers(self):
        self.closed = [pr(merged_at='now', merge_commit_sha='merge')]
        self.comments = [{'user': {'login': 'human'}, 'body': '<!-- openspec-autopilot:opened -->'}]
        self.assertFalse(self.pilot.pending())
        self.comments.append({'user': {'login': a.BOT}, 'body': '<!-- openspec-autopilot:opened -->'})
        self.comments.append({'user': {'login': 'human'}, 'body': '<!-- openspec-autopilot:followup:merge -->'})
        self.assertTrue(self.pilot.pending())

    def test_api_failure_is_not_treated_as_an_empty_queue(self):
        self.pilot.pages = lambda path: (_ for _ in ()).throw(RuntimeError('API unavailable'))
        with self.assertRaisesRegex(RuntimeError, 'API unavailable'):
            self.pilot.pending()

    def test_check_cli_writes_skip_output_and_does_not_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            output, summary = Path(tmp)/'output', Path(tmp)/'summary'
            with patch.dict(os.environ, GITHUB_OUTPUT=str(output), GITHUB_STEP_SUMMARY=str(summary)), \
                 patch('sys.argv', ['autopilot.py', '--repo', 'o/s', '--check']), \
                 patch.object(a.Pilot, 'pending', return_value=False), patch.object(a.Pilot, 'main') as main:
                self.assertEqual(a.main(), 0)
            self.assertEqual(output.read_text(), 'pending=false\n')
            self.assertIn('已跳过', summary.read_text())
            main.assert_not_called()

    def test_check_cli_can_import_without_site_packages(self):
        import subprocess
        result = subprocess.run(['python3', '-S', str(ROOT/'.github/actions/store-autopilot/autopilot.py'), '--help'],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--check', result.stdout)

    def test_action_gate_skips_tools_and_merge_but_keeps_failure_alerts(self):
        import yaml
        data = yaml.safe_load((ROOT/'.github/actions/store-autopilot/action.yml').read_text())
        steps = {s['name']: s for s in data['runs']['steps']}
        expected = "inputs.check-only != 'true' && steps.pending.outputs.pending == 'true'"
        for name in ['Install validation tools', 'Validate and merge automation PRs']:
            self.assertEqual(steps[name]['if'], expected)
        self.assertIn('--check', steps['Check pending automation PRs']['run'])
        self.assertNotIn('pending.outputs', steps['Notify source workflow failure']['if'])
        self.assertIn('failure()', steps['Notify autopilot failure']['if'])


class ArchiveIntegrationTest(unittest.TestCase):
    SPEC = '''# Example Specification

## Purpose
This capability describes the complete business rules for the confirmation experience presented to end users.

## Requirements

### Requirement: Confirmation
The system SHALL show a confirmation before submission.

#### Scenario: Submit
- **WHEN** the user submits
- **THEN** the system shows a confirmation
'''
    DELTA = '''## MODIFIED Requirements

### Requirement: Confirmation
The system SHALL show a confirmation before submission and display the amount.

#### Scenario: Submit
- **WHEN** the user submits
- **THEN** the system shows a confirmation with the amount
'''

    def exercise(self, existing):
        import shutil
        import subprocess
        if not shutil.which('openspec'): self.skipTest('requires OpenSpec CLI')
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, GH_TOKEN='test'):
            root=Path(tmp); remote=root/'remote'; work=root/'work'
            def git(cwd,*args):
                return subprocess.check_output(['git','-c','commit.gpgsign=false',*args],cwd=cwd,stderr=subprocess.PIPE,text=True).strip()
            git(root,'init','--bare','--initial-branch=main',str(remote))
            git(root,'clone',str(remote),str(work))
            git(work,'config','user.name',a.BOT); git(work,'config','user.email','test@example.com'); git(work,'config','commit.gpgsign','false')
            cap=work/'openspec/specs/cap/spec.md';cap.parent.mkdir(parents=True);cap.write_text(self.SPEC)
            change=work/'openspec/changes/example';change.mkdir(parents=True)
            meta=change/'.openspec.yaml';meta.write_text('schema: store\nstatus: 已批准\ntitle: Confirmation\nID: 20261105-abcd\nassignee: Janpo\nversion: 1.0.0\nrepositories: [o/d]\n')
            (change/'proposal.md').write_text('## Why\n\nDisplay confirmation amount.\n\n## What Changes\n\nShow the amount.\n\n## Capabilities\n\n### Modified Capabilities\n- `cap`: Confirmation amount.\n\n## Impact\n\nUsers.\n')
            delta=change/'specs/cap/spec.md';delta.parent.mkdir(parents=True);delta.write_text(self.DELTA)
            if not existing:
                # 新 Change 只在观察分支上出现。
                shutil.rmtree(change)
            git(work,'add','-A');git(work,'commit','-m','base');git(work,'push','origin','main')
            git(work,'checkout','-b','obs/change')
            if not existing:
                change.mkdir(parents=True);meta.write_text('schema: store\nstatus: 已实现\ntitle: Confirmation\nID: 20261105-abcd\nassignee: Janpo\nversion: 1.0.0\nrepositories: [o/d]\n')
                (change/'proposal.md').write_text('## Why\n\nDisplay confirmation amount.\n\n## What Changes\n\nShow amount.\n\n## Capabilities\n\n### Modified Capabilities\n- `cap`: Amount.\n\n## Impact\n\nUsers.\n')
                delta.parent.mkdir(parents=True);delta.write_text(self.DELTA)
            else:
                meta.write_text(meta.read_text().replace('已批准','已实现'))
            meta.write_text(meta.read_text()+'implementations:\n  - pr: o/d#1\n    url: https://github.com/o/d/pull/1\n')
            git(work,'add','-A');git(work,'commit','-m','observe');git(work,'push','origin','obs/change')
            source_sha=git(work,'rev-parse','HEAD')
            p=pr();p['head']['sha']=source_sha;p['body']='<!-- openspec-obs: {"change":"example","prs":["o/d#1"]} -->'
            pilot=a.Pilot('o/s','main',work);pilot.notify=lambda *args:None
            def api(path,method='GET',body=None):
                if method=='PATCH': p['body']=body['body']
                p['head']['sha']=git(work,'rev-parse','origin/obs/change')
                return p
            pilot.api=api
            result=pilot.prepare_archive(p)
            git(work,'fetch','origin','obs/change');git(work,'checkout','--detach','origin/obs/change')
            archived=list((work/'openspec/changes/archive').glob('*-example'))
            self.assertEqual(len(archived),1)
            data=a.load_yaml(archived[0]/'.openspec.yaml')
            self.assertEqual(data['status'],'已归档');self.assertEqual(data['assignee'],'Janpo')
            self.assertEqual(data['implementations'][0]['pr'],'o/d#1')
            self.assertFalse(change.exists());self.assertIn('display the amount',cap.read_text())
            self.assertTrue((cap.parent/'changelog.md').is_file())
            self.assertTrue(a.parse_marker(result['body'])['archived'])
            # 重跑保持相同 SHA，不重复归档或重复 changelog。
            again=pilot.prepare_archive(result)
            self.assertEqual(again['head']['sha'],result['head']['sha'])

    def test_new_backfill_is_archived_in_same_pr(self): self.exercise(False)
    def test_manually_created_change_is_archived_in_same_pr(self): self.exercise(True)


if __name__ == '__main__': unittest.main()
