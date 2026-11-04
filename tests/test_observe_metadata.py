"""来源提交元数据、已发布版本与人工覆盖保留的回归测试。"""
from __future__ import annotations

import datetime as dt
import json
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
import prepare
import write


def source_commit(sha: str, name: str | None, login: str | None, subject: str) -> dict:
    return {
        'sha': sha,
        'commit': {'message': subject + '\n\nbody', 'author': {'name': name}},
        'author': {'login': login} if login else None,
    }


class SourceMetadataTest(unittest.TestCase):
    def test_author_preference_and_missing_author(self) -> None:
        named = prepare.commit_source_metadata(source_commit('a' * 40, 'Ningbo', 'ningbo-login', 'feature: x'))
        self.assertEqual(named['assignee'], 'Ningbo')
        login_only = prepare.commit_source_metadata(source_commit('b' * 40, '', 'damian-login', 'fix: y'))
        self.assertEqual(login_only['assignee'], 'damian-login')
        missing = prepare.commit_source_metadata(source_commit('c' * 40, '', '', 'feat: z'))
        self.assertEqual(missing['assignee'], '')

    def test_conventional_type_mapping_is_case_insensitive(self) -> None:
        cases = {
            'fix: x': 'bugfix', 'Fix(ui)!: x': 'bugfix', 'refactor: x': 'refactor',
            'REFACTOR(api): x': 'refactor', 'feat: x': 'feature', 'feat(ui)!: x': 'feature',
            'docs: x': 'feature', 'no prefix': 'feature', '': 'feature',
        }
        for subject, expected in cases.items():
            self.assertEqual(prepare.change_type_from_subject(subject), expected)

    def test_pr_commits_keep_api_order_and_last_commit_wins(self) -> None:
        first = source_commit('1' * 40, 'First', 'first', 'feat: first')
        last = source_commit('2' * 40, 'Last', 'last', 'fix: last')
        with patch.object(prepare, 'gh_request', return_value=[first, last]):
            commits = prepare.fetch_pull_commits('o/app', 12, 'token')
        self.assertEqual([c['sha'][0] for c in commits], ['1', '2'])
        self.assertEqual(prepare.commit_source_metadata(commits[-1])['assignee'], 'Last')
        self.assertEqual(prepare.commit_source_metadata(commits[-1])['change_type'], 'bugfix')

    def test_direct_push_batch_uses_last_commit(self) -> None:
        batch = [
            source_commit('1' * 40, 'First', 'first', 'feat: first'),
            source_commit('2' * 40, 'Damian', 'damian', 'refactor: second'),
        ]
        source = prepare.commit_source_metadata(batch[-1])
        self.assertEqual(source['assignee'], 'Damian')
        self.assertEqual(source['change_type'], 'refactor')

    def test_validate_uses_authoritative_source_fields(self) -> None:
        data = {
            'changes': [{'change': 'wallet'}],
            'merged': [{
                'pr': 'o/app#7', 'url': 'u7', 'title': 'PR title', 'merged_at': '2026-11-04T00:00:00Z',
                'assignee': 'Ningbo', 'change_type': 'bugfix', 'source_sha': 'a' * 40,
                'source_title': 'fix: wallet',
            }],
            'rejected': [],
        }
        result, _ = write.validate({
            'links': [{'change': 'wallet', 'prs': [{
                'pr': 'o/app#7', 'basis': 'b', 'hint': False, 'confidence': '高',
                'assignee': '模型猜错', 'change_type': 'feature',
            }], 'diffs': []}],
            'unregistered': [], 'no_draft': [],
        }, data)
        item = result['links'][0]['prs'][0]
        self.assertEqual(item['assignee'], 'Ningbo')
        self.assertEqual(item['change_type'], 'bugfix')
        self.assertEqual(item['source_sha'], 'a' * 40)
        self.assertEqual(item['source_title'], 'fix: wallet')


class MaterializeMetadataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Path(self.tmp.name)
        (self.store / 'openspec/changes').mkdir(parents=True)
        (self.store / 'openspec/releases/archive').mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def release(self, directory: str, version: str, status: str) -> None:
        path = self.store / 'openspec/releases/archive' / directory / 'meta.yaml'
        path.parent.mkdir(parents=True)
        path.write_text(f'version: "{version}"\nstatus: {status}\n')

    def item(self, number: int, assignee: str, change_type: str, repo: str = 'o/app') -> dict:
        return {
            'pr': f'{repo}#{number}', 'url': f'https://example/{number}', 'title': f'T{number}',
            'behavior': f'B{number}', 'merged_at': f'2026-11-0{number}T00:00:00Z',
            'assignee': assignee, 'change_type': change_type,
            'source_sha': str(number) * 40, 'source_title': f'{change_type}: T{number}',
        }

    def test_latest_released_semver_numeric_ordering_and_status_filter(self) -> None:
        self.release('v0.9.0', 'v0.9.0', 'released')
        self.release('0.10.0', '0.10.0', 'released')
        self.release('0.11.0', '0.11.0', 'planning')
        self.release('broken', 'latest', 'released')
        self.assertEqual(backfill.latest_released_version(self.store), '0.10.0')

    def test_no_released_version_is_empty(self) -> None:
        self.release('0.11.0', '0.11.0', 'in-progress')
        self.assertEqual(backfill.latest_released_version(self.store), '')

    def test_materialize_uses_last_unit_and_keeps_trace(self) -> None:
        self.release('0.10.0', '0.10.0', 'released')
        items = [self.item(1, 'A', 'feature'), self.item(2, 'B', 'bugfix'), self.item(3, 'C', 'refactor')]
        draft = {'title': '聚合补登', 'summary': 's', 'deltas': []}
        meta = backfill.materialize(
            self.store, 'obs-many', items, draft,
            dt.datetime(2026, 11, 4, tzinfo=dt.timezone.utc),
        )
        self.assertEqual(meta['assignee'], 'C')
        self.assertEqual(meta['type'], 'refactor')
        self.assertEqual(meta['version'], '0.10.0')
        self.assertEqual(meta['priority'], '')
        self.assertNotIn('deadline', meta)
        self.assertEqual([i['assignee'] for i in meta['implementations']], ['A', 'B', 'C'])
        self.assertEqual(meta['implementations'][-1]['source_sha'], '3' * 40)

    def test_manual_metadata_wins_over_sources_and_latest_version(self) -> None:
        self.release('0.10.0', '0.10.0', 'released')
        existing = {
            'schema': 'store', 'status': '已实现', 'ID': '20261104-abcd', 'title': '人工标题',
            'assignee': 'Manual Owner', 'type': 'bugfix', 'version': '0.9.0', 'priority': 'P1',
        }
        meta = backfill.materialize(
            self.store, 'obs-manual', [self.item(1, 'Commit Author', 'refactor')],
            {'title': 'source', 'deltas': []}, dt.datetime(2026, 11, 4, tzinfo=dt.timezone.utc), existing,
        )
        self.assertEqual(meta['assignee'], 'Manual Owner')
        self.assertEqual(meta['type'], 'bugfix')
        self.assertEqual(meta['version'], '0.9.0')
        self.assertEqual(meta['priority'], 'P1')
        self.assertNotIn('deadline', meta)

    def test_observation_record_keeps_source_fields(self) -> None:
        item = self.item(1, 'Ningbo', 'bugfix')
        backfill.save_record(self.store, [item], {'title': 'T'}, 'change')
        record = next((self.store / 'openspec/observations').glob('*.json'))
        saved = json.loads(record.read_text())['items'][0]
        for key in ('assignee', 'change_type', 'source_sha', 'source_title'):
            self.assertEqual(saved[key], item[key])

    def test_legacy_sync_slack_ids_use_exact_normalized_match(self) -> None:
        if subprocess.run(['bash', '-c', 'declare -A x=()'], capture_output=True).returncode:
            self.skipTest('本机 bash 不支持关联数组；actions runner 的 bash 5 会执行此回归')
        source = (ROOT / '.github/actions/changes-sync/slack-notify.sh').read_text()
        helpers = source[source.index('declare -A SLACK_IDS='):source.index('\n# 元数据变更')]
        script = helpers + '\nprintf "%s|" "$(resolve_slack_id " Ningbo ")" "$(resolve_slack_id "gemma")" "$(resolve_slack_id "Ningbo Team")" "$(resolve_slack_id "Unknown")" "$(resolve_slack_id "Ningbo \\"Team")"\n'
        result = subprocess.run(['bash', '-c', script], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), 'U03Q0J9FJGH|U073JSDQWSV||||')

    def test_legacy_sync_metadata_loader_filters_historical_deadline(self) -> None:
        change = self.store / 'legacy-change'
        change.mkdir()
        (change / '.openspec.yaml').write_text('title: Legacy\nassignee: Ningbo\ndeadline: 2026-12-31\n')
        result = subprocess.run(
            [sys.executable, str(ROOT / '.github/actions/changes-sync/load-metadata.py'), str(change)],
            capture_output=True, text=True, check=True,
        )
        data = json.loads(result.stdout)
        self.assertEqual(data['assignee'], 'Ningbo')
        self.assertNotIn('deadline', data)


if __name__ == '__main__':
    unittest.main()
