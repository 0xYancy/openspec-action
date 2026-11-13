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


class FailureReasonTest(unittest.TestCase):
    def test_failed_archive_reason_reaches_the_notification_card(self):
        import subprocess
        caps = ['rwa-framework-v1', 'tbpros-product', 'tbpros-subscription']
        output = '\n'.join(f'{cap}: [WARNING] Purpose section is still a placeholder rather than a Purpose anyone wrote' for cap in caps)
        args = ('python3', '/home/runner/work/_actions/o/action/main/.github/actions/archive/archive.py')
        with patch.object(a.subprocess, 'run', return_value=subprocess.CompletedProcess(args, 1, output, '')):
            with self.assertRaises(RuntimeError) as caught:
                a.run(Path('.'), *args)
        message = str(caught.exception)
        self.assertIn('3 个规范的 Purpose', message)
        for cap in caps:
            self.assertIn(cap, message)
        self.assertNotIn('/home/runner', message)
        with patch.dict(os.environ, RUN_URL='https://github.com/o/s/actions/runs/123'):
            payload = a.card('o/s', '⚠️ 自动化处理受阻', message, pr()['html_url'], pr())
        self.assertIn(message, payload['blocks'][2]['text']['text'])
        self.assertIn('运行详情', payload['blocks'][-1]['elements'][0]['text'])

    def test_error_details_are_bounded_redacted_and_stable_across_temp_paths(self):
        args = ('python3', '/actions/archive/archive.py')
        output = ('\x1b[31mError: /tmp/obs-archive-abc/work/spec.md rejected; '
                  'credential=local-secret-value token=unknown-secret '
                  'https://user:password@github.com/o/s and Bearer another-secret\x1b[0m')
        with patch.dict(os.environ, GH_TOKEN='local-secret-value'):
            reason = a.failure_reason(args, output)
        for secret in ['local-secret-value', 'unknown-secret', 'user:password', 'another-secret', '\x1b', 'obs-archive-abc']:
            self.assertNotIn(secret, reason)
        self.assertIn('spec.md rejected', reason)
        with patch.dict(os.environ, GH_TOKEN='local-secret-value'):
            self.assertEqual(reason, a.failure_reason(args, output.replace('obs-archive-abc', 'obs-archive-xyz')))
        self.assertLessEqual(len(a.failure_reason(args, 'Error: ' + 'x' * 4000)), 550)

    def test_multiple_token_json_values_are_redacted(self):
        with patch.dict(os.environ, OPENSPEC_ISSUE_TOKENS='{"org":"org-secret-value"}'):
            self.assertNotIn('org-secret-value', a.failure_reason(('git', 'push'), 'fatal: org-secret-value rejected'))

    def test_mixed_validation_errors_keep_non_purpose_reason(self):
        text = ('cap-a: [WARNING] Purpose section is still a placeholder\n'
                'cap-b: [ERROR] Requirement must include a scenario')
        reason = a.failure_reason(('python3', '/actions/archive/archive.py'), text)
        self.assertIn('Requirement must include a scenario', reason)
        self.assertIn('cap-a', reason)

    def test_empty_command_output_still_explains_failure(self):
        self.assertIn('命令未返回错误详情', a.failure_reason(('git', 'push'), ''))


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
        self.assertEqual([e for e in self.events if isinstance(e, str) and e != 'validated'], ['merged', 'followup'])
        self.assertIn('managed', self.marks)

    def test_complete_success_sends_one_final_card_and_silently_syncs_issue(self):
        self.pilot.notify=a.Pilot.notify.__get__(self.pilot)
        self.pilot.followup=a.Pilot.followup.__get__(self.pilot)
        self.pilot.validate=lambda p,k:None
        self.pilot.pages=lambda path:[]
        with patch.object(a,'run',return_value='before') as run, \
             patch.object(a,'slack') as slack, \
             patch.object(a,'summarize',return_value=['更新确认金额']):
            self.pilot.process(pr(), 'observe')
        slack.assert_called_once()
        payload=slack.call_args.args[0]
        self.assertIn('自动化 PR 已合入',payload['text'])
        self.assertIn('更新确认金额',payload['text'])
        self.assertIn('followup:merged', self.marks)
        self.assertFalse(any('--notify' in call.args for call in run.call_args_list))

    def test_failed_validation_never_marks_ready_or_merges(self):
        self.pilot.validate=lambda p,k:(_ for _ in ()).throw(RuntimeError('invalid'))
        with patch.object(a,'run') as run, self.assertRaises(RuntimeError):
            self.pilot.process(pr(draft=True),'observe')
        run.assert_not_called()
        self.assertFalse(any(isinstance(e,tuple) and e[0].endswith('/merge') for e in self.events))

    def test_base_changed_after_validation_silently_retries(self):
        self.pilot.validate=lambda p,k:None
        self.pilot.validated_base='old'
        self.pilot.process(pr(),'observe')
        self.assertNotIn('merged', self.events)
        self.assertFalse(any(isinstance(e,tuple) and e[0].endswith('/merge') for e in self.events))

    def test_existing_notice_is_not_resent(self):
        self.pilot.notify=a.Pilot.notify.__get__(self.pilot)
        self.marks={'opened'}
        with patch.object(a,'slack') as slack:
            self.pilot.notify(pr(),'opened','x','y')
        slack.assert_not_called()

    def test_followup_does_not_resend_completed_issues_or_requirement_notifications(self):
        self.pilot.followup=a.Pilot.followup.__get__(self.pilot)
        self.marks={'issues:merged'}
        with patch.object(a,'run') as run:
            self.pilot.followup(pr(merge_commit_sha='merged'))
        run.assert_not_called()
        self.assertIn('followup:merged',self.marks)

    def test_followup_syncs_issues_without_a_second_slack_notification(self):
        self.pilot.followup=a.Pilot.followup.__get__(self.pilot)
        with patch.object(a,'run',return_value='before') as run, patch.object(a,'slack') as slack:
            self.pilot.followup(pr(merge_commit_sha='merged'))
        calls=[c for c in run.call_args_list if c.args[1] == 'python3']
        self.assertEqual(len(calls),1)
        self.assertNotIn('--notify', calls[0].args)
        slack.assert_not_called()
        self.assertIn('issues:merged',self.marks)
        self.assertIn('followup:merged',self.marks)

    def test_issue_failure_remains_retryable_without_marking_success(self):
        self.pilot.followup=a.Pilot.followup.__get__(self.pilot)
        def run(work,*args):
            if args[0]=='python3': raise RuntimeError('issue failure')
            return 'before'
        with patch.object(a,'run',side_effect=run), self.assertRaisesRegex(RuntimeError,'下轮自动重试'):
            self.pilot.followup(pr(merge_commit_sha='merged'))
        self.assertNotIn('issues:merged',self.marks)
        self.assertNotIn('followup:merged',self.marks)

    def test_normal_check_waits_are_silent_but_completed_failures_are_errors(self):
        original_api = self.pilot.api
        checks, statuses = [], []
        def api(path, *args):
            if '/check-runs?' in path: return {'total_count': len(checks), 'check_runs': checks}
            if path.endswith('/status'): return {'statuses': statuses}
            return original_api(path, *args)
        self.pilot.api=api
        self.pilot.validate=lambda p,k:None
        checks.append({'name':'Other check','status':'in_progress','conclusion':None})
        self.pilot.process(pr(), 'observe')
        self.assertNotIn('merged',self.events)
        checks.clear(); statuses.append({'context':'external', 'state':'pending'})
        self.pilot.process(pr(), 'observe')
        self.assertNotIn('merged',self.events)
        statuses.clear(); checks.append({'name':'Other check','status':'completed','conclusion':'failure'})
        with self.assertRaisesRegex(RuntimeError,'其他检查失败'):
            self.pilot.process(pr(), 'observe')
        checks.clear(); statuses.append({'context':'external', 'state':'failure'})
        with self.assertRaisesRegex(RuntimeError,'其他状态检查失败'):
            self.pilot.process(pr(), 'observe')

    def test_reference_sync_delay_is_silent_and_never_marks_failure(self):
        self.pilot.pages=lambda path:[pr()] if 'state=open' in path else []
        self.pilot.writer_running=lambda kind:False
        self.pilot.process=lambda *args:(_ for _ in ()).throw(a.RetryLater('等待引用同步'))
        self.assertEqual(self.pilot.main(),0)
        self.assertEqual(self.events, [])

    def test_same_failure_alert_is_sent_only_once_across_retries(self):
        self.pilot.notify=a.Pilot.notify.__get__(self.pilot)
        self.pilot.pages=lambda path:[pr()] if 'state=open' in path else []
        self.pilot.writer_running=lambda kind:False
        self.pilot.process=lambda *args:(_ for _ in ()).throw(RuntimeError('invalid'))
        original_api=self.pilot.api
        self.pilot.api=lambda path,*args:pr(merged=False) if path=='/pulls/1' else original_api(path,*args)
        with patch.object(a,'slack') as slack:
            self.assertEqual(self.pilot.main(),1)
            self.assertEqual(self.pilot.main(),1)
        slack.assert_called_once()

    def test_merge_notification_is_not_repeated_when_recovering_issue_sync(self):
        self.pilot.notify=a.Pilot.notify.__get__(self.pilot)
        self.pilot.pages=lambda path:[pr(merged_at='now', merge_commit_sha='merged')] if 'state=closed' in path else []
        self.marks={'managed','merged'}
        with patch.object(a,'slack') as slack:
            self.assertEqual(self.pilot.main(),0)
        slack.assert_not_called()
        self.assertIn('followup',self.events)

    def test_first_and_recovery_followup_failure_share_one_alert(self):
        self.pilot.notify=a.Pilot.notify.__get__(self.pilot)
        self.pilot.writer_running=lambda kind:False
        self.marks={'managed','merged'}
        self.pilot.pages=lambda path:[pr()] if 'state=open' in path else []
        self.pilot.process=lambda *args:(_ for _ in ()).throw(RuntimeError('issue failed'))
        with patch.object(a,'slack') as slack:
            self.assertEqual(self.pilot.main(),1)
            self.pilot.pages=lambda path:[pr(merged_at='now',merge_commit_sha='merged')] if 'state=closed' in path else []
            self.pilot.followup=lambda *args:(_ for _ in ()).throw(RuntimeError('issue failed'))
            self.assertEqual(self.pilot.main(),1)
        slack.assert_called_once()

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



class HeadSyncTest(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict(os.environ, GH_TOKEN='test');self.env.start()
        self.pilot=a.Pilot('o/s','main',Path('.'))

    def tearDown(self):
        self.env.stop()

    def test_stale_pr_api_is_retried_until_pushed_sha_matches(self):
        responses=iter([pr(), pr(), pr()])
        heads=iter(['old','old','new'])
        def api(*args):
            result=next(responses);result['head']['sha']=next(heads);return result
        self.pilot.api=api
        with patch.object(a.time,'sleep') as sleep:
            result=self.pilot.wait_for_head(pr(),'new')
        self.assertEqual(result['head']['sha'],'new')
        self.assertEqual([call.args[0] for call in sleep.call_args_list],[1,2])

    def test_stale_api_or_concurrently_changed_head_is_bounded_and_deferred(self):
        self.pilot.api=lambda *args:pr()
        with patch.object(a.time,'sleep') as sleep, self.assertRaises(a.RetryLater):
            self.pilot.wait_for_head(pr(),'expected')
        self.assertEqual(sleep.call_count,len(a.SYNC_DELAYS)-1)

    def test_stale_pull_ref_is_retried_before_validation(self):
        heads=iter(['old','head'])
        def run(work,*args):
            return next(heads) if args[:2]==('git','rev-parse') else ''
        with patch.object(a,'run',side_effect=run) as command, patch.object(a.time,'sleep') as sleep:
            self.assertEqual(self.pilot.fetch_pr_head(pr()),'refs/openspec-autopilot/pr-1')
        self.assertEqual(sum(c.args[1:3]==('git','fetch') for c in command.call_args_list),2)
        sleep.assert_called_once_with(1)

    def test_ref_never_matches_and_no_validation_tree_is_created(self):
        with patch.object(a,'run',return_value='stale') as run, patch.object(a.time,'sleep'), self.assertRaises(a.RetryLater):
            self.pilot.validate(pr(),'archive')
        self.assertFalse(any('worktree' in call.args for call in run.call_args_list))

    def test_file_guard_uses_git_snapshot_without_reading_stale_pr_files_api(self):
        self.pilot.fetch_pr_head=lambda pr:'ref'
        self.pilot.pages=lambda path:self.fail('PR files API must not determine validation scope')
        def run(work,*args):
            return 'base' if args[:2]==('git','rev-parse') else '.github/workflows/injected.yml'
        with patch.object(a,'run',side_effect=run), self.assertRaisesRegex(RuntimeError,'范围外'):
            self.pilot.validate(pr(),'archive')

    def test_renamed_archive_files_still_reject_leftover_active_change(self):
        self.pilot.fetch_pr_head=lambda pr:'ref'
        def run(work,*args):
            if args[:2]==('git','rev-parse'): return 'base'
            if args[:2]==('git','diff'): return 'openspec/changes/archive/2026-11-08-a/proposal.md'
            if args[:3]==('git','worktree','add'):
                (Path(args[-2])/'openspec/changes/a').mkdir(parents=True)
            return ''
        with patch.object(a,'run',side_effect=run), self.assertRaisesRegex(RuntimeError,'仍包含活动 Change'):
            self.pilot.validate(pr(),'archive')

    def test_store_check_installs_tools_outside_the_untrusted_pr_directory(self):
        import yaml
        data=yaml.safe_load((ROOT/'.github/actions/store-check/action.yml').read_text())
        steps={s['name']:s for s in data['runs']['steps']}
        self.assertNotIn('working-directory',steps['Install OpenSpec CLI'])
        for name in ['Validate specs and changes','Check status, metadata and protected files']:
            self.assertEqual(steps[name]['working-directory'],'${{ inputs.store-path }}')
        self.assertNotIn('npm install',steps['Validate specs and changes']['run'])


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

    def test_quiet_managed_pr_recovers_without_an_opened_notification(self):
        self.closed = [pr(merged_at='now', merge_commit_sha='merge')]
        self.comments = [{'user': {'login': a.BOT}, 'body': '<!-- openspec-autopilot:managed -->'}]
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

    def exercise(self, existing, lag=False, new_capability=False):
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
            delta_text = self.DELTA.replace('## MODIFIED Requirements', '## ADDED Requirements') if new_capability else self.DELTA
            if new_capability:
                cap.unlink()
                (work/'README.md').write_text('Store integration fixture\n')
            change=work/'openspec/changes/example';change.mkdir(parents=True)
            meta=change/'.openspec.yaml';meta.write_text('schema: store\nstatus: 已批准\ntitle: Confirmation\nID: 20261105-abcd\nassignee: Janpo\nversion: 1.0.0\nrepositories: [o/d]\n')
            (change/'proposal.md').write_text('## Why\n\nDisplay confirmation amount.\n\n## What Changes\n\nShow the amount.\n\n## Capabilities\n\n### Modified Capabilities\n- `cap`: Confirmation amount.\n\n## Impact\n\nUsers.\n')
            delta=change/'specs/cap/spec.md';delta.parent.mkdir(parents=True);delta.write_text(delta_text)
            if not existing:
                # 新 Change 只在观察分支上出现。
                shutil.rmtree(change)
            git(work,'add','-A');git(work,'commit','-m','base');git(work,'push','origin','main')
            git(work,'checkout','-b','obs/change')
            if not existing:
                change.mkdir(parents=True);meta.write_text('schema: store\nstatus: 已实现\ntitle: Confirmation\nID: 20261105-abcd\nassignee: Janpo\nversion: 1.0.0\nrepositories: [o/d]\n')
                (change/'proposal.md').write_text('## Why\n\nDisplay confirmation amount.\n\n## What Changes\n\nShow amount.\n\n## Capabilities\n\n### Modified Capabilities\n- `cap`: Amount.\n\n## Impact\n\nUsers.\n')
                delta.parent.mkdir(parents=True);delta.write_text(delta_text)
            else:
                meta.write_text(meta.read_text().replace('已批准','已实现'))
            meta.write_text(meta.read_text()+'implementations:\n  - pr: o/d#1\n    url: https://github.com/o/d/pull/1\n')
            git(work,'add','-A');git(work,'commit','-m','observe');git(work,'push','origin','obs/change')
            source_sha=git(work,'rev-parse','HEAD')
            p=pr();p['head']['sha']=source_sha;p['body']='<!-- openspec-obs: {"change":"example","prs":["o/d#1"]} -->'
            notifications=[]
            pilot=a.Pilot('o/s','main',work);pilot.notify=lambda *args:notifications.append(args)
            reads=[]
            def api(path,method='GET',body=None):
                if method=='PATCH': p['body']=body['body']
                else: reads.append(path)
                p['head']['sha']=source_sha if lag and len(reads)<3 else git(work,'rev-parse','origin/obs/change')
                return p
            pilot.api=api
            with patch.object(a.time,'sleep'):
                result=pilot.prepare_archive(p)
            self.assertNotEqual(result['head']['sha'],source_sha)
            git(remote,'update-ref','refs/pull/1/head',result['head']['sha'])
            pilot.validate(result,'archive')
            git(work,'fetch','origin','obs/change');git(work,'checkout','--detach','origin/obs/change')
            archived=list((work/'openspec/changes/archive').glob('*-example'))
            self.assertEqual(len(archived),1)
            data=a.load_yaml(archived[0]/'.openspec.yaml')
            self.assertEqual(data['status'],'已归档');self.assertEqual(data['assignee'],'Janpo')
            self.assertEqual(data['implementations'][0]['pr'],'o/d#1')
            self.assertFalse(change.exists());self.assertIn('display the amount',cap.read_text())
            self.assertNotIn('TBD - created by archiving', cap.read_text())
            self.assertTrue((cap.parent/'changelog.md').is_file())
            self.assertTrue(a.parse_marker(result['body'])['archived'])
            # 重跑保持相同 SHA，不重复归档或重复 changelog。
            again=pilot.prepare_archive(result)
            self.assertEqual(again['head']['sha'],result['head']['sha'])
            self.assertEqual(notifications, [])

    def test_new_capability_list_fills_purpose_before_strict_validation(self): self.exercise(False, new_capability=True)

    def test_stale_pr_api_after_archive_validates_the_new_commit(self): self.exercise(False,lag=True)

    def test_new_backfill_is_archived_in_same_pr(self): self.exercise(False)
    def test_manually_created_change_is_archived_in_same_pr(self): self.exercise(True)


if __name__ == '__main__': unittest.main()
