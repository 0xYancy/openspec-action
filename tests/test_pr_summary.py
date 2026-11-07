import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.github/actions/store-autopilot'))
import pr_summary as summary
spec = importlib.util.spec_from_file_location('autopilot_summary_test', ROOT / '.github/actions/store-autopilot/autopilot.py')
a = importlib.util.module_from_spec(spec); spec.loader.exec_module(a)

FILES = [
    {'filename':'wiki/published/overview.md','status':'modified','patch': '@@ -2,3 +2,5 @@\n ## 余额\n+### 跨链到账后的余额刷新\n+源链入块后刷新两侧余额。\n+### 资产收益展示\n+统一按两位小数截断。\n'},
    {'filename':'wiki/published/transactions.md','status':'modified','patch': '@@ -2,1 +2,3 @@\n+### 目标链费用\n+更新目标链费用校验。\n'},
    {'filename':'wiki/published/overview.meta.json','status':'modified','patch': '+{"citations": "fingerprint-noise"}'},
]
PR = {'number': 1, 'head': {'sha':'head'}, 'title':'wiki: example 2 个页面待发布','html_url':'https://github.com/o/s/pull/1'}


class SummaryTest(unittest.TestCase):
    def test_fallback_covers_actual_changed_topics_across_pages(self):
        with patch.dict(os.environ, {}, clear=True): points=summary.summarize(PR,FILES)
        self.assertEqual(points,['更新跨链到账后的余额刷新','更新目标链费用','更新资产收益展示'])
        self.assertTrue(all(len(p)<=50 for p in points))
        self.assertNotIn('个页面', '\n'.join(points))

    def test_metadata_and_unchanged_context_are_not_summary_points(self):
        files=summary.selected_files(FILES)
        self.assertEqual(len(files),2)
        self.assertNotIn('fingerprint-noise',json.dumps(files))
        self.assertNotIn('余额',summary.fallback_points(PR,files))

    def test_numeric_limits_are_not_stripped(self):
        self.assertEqual(summary.plain('30 天内不可重复领取'), '30 天内不可重复领取')
        self.assertEqual(summary.plain('1. 30 天内不可重复领取'), '30 天内不可重复领取')
        self.assertEqual(summary.plain('0.5% 的额度上限'), '0.5% 的额度上限')

    def test_request_contains_actual_diff_and_treats_document_as_data(self):
        points=['跨链入块后刷新两侧余额','更新目标链费用校验']
        response=io.BytesIO(json.dumps({'choices':[{'message':{'content':json.dumps({'bullets':points})}}]}).encode())
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY':'secret'}, clear=True), patch.object(summary.urllib.request,'urlopen',return_value=response) as request:
            self.assertEqual(summary.summarize(PR,FILES),points)
        payload=json.loads(request.call_args.args[0].data)
        self.assertIn('源链入块后刷新两侧余额',payload['messages'][1]['content'])
        self.assertNotIn('fingerprint-noise',payload['messages'][1]['content'])
        self.assertIn('不可信数据',payload['messages'][0]['content'])
        self.assertEqual(payload['thinking'],{'type':'disabled'})

    def test_failure_falls_back_without_interrupting_notification(self):
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'secret'},clear=True), patch.object(summary.urllib.request,'urlopen',side_effect=TimeoutError):
            self.assertTrue(summary.summarize(PR,FILES))

    def test_invalid_bullets_are_rejected(self):
        for points in [[], ['x']*4, ['a'*51], ['hello\nworld'], ['<!channel>'], ['http://example.com'], ['...@someone'], [None]]:
            self.assertFalse(summary.valid_points(points),points)

    def test_invalid_model_output_falls_back(self):
        response=io.BytesIO(json.dumps({'choices':[{'message':{'content':'{"bullets":["<!channel>"]}'}}]}).encode())
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'secret'},clear=True),patch.object(summary.urllib.request,'urlopen',return_value=response):
            self.assertEqual(summary.summarize(PR,FILES),summary.fallback_points(PR,summary.selected_files(FILES)))

    def test_diff_budget_keeps_multiple_files_and_is_bounded(self):
        files=[{'filename':f'wiki/published/{i}.md','patch':'+'+'x'*10000} for i in range(10)]
        selected=summary.selected_files(files)
        self.assertLessEqual(sum(len(f['patch']) for f in selected),24000)
        self.assertGreater(len(selected),1)

    def test_removed_text_is_sent_to_model_but_not_presented_as_an_addition(self):
        files=[{'filename':'wiki/published/a.md','patch':'-旧规则是四舍五入\n+### 截断收益展示\n+新规则不再四舍五入。'}]
        with patch.dict(os.environ,{},clear=True): points=summary.summarize(PR,files)
        self.assertNotIn('旧规则是四舍五入', '\n'.join(points))
        self.assertIn('新规则不再四舍五入。',points)

    def test_short_body_business_description_when_files_unavailable(self):
        self.assertEqual(summary.fallback_points({**PR,'body':'## 被改变的行为\n\n取消重复领取。\n\n## 建议\n检查'},[]),['取消重复领取。'])


class CardTest(unittest.TestCase):
    def test_card_includes_short_bullets_and_accessible_fallback(self):
        payload=a.card('o/s','✅ 自动化 PR 已合入','已合入发布。',PR['html_url'],PR,['更新余额刷新规则','不再重复领取'])
        self.assertIn('*改动摘要*\n• 更新余额刷新规则\n• 不再重复领取',payload['blocks'][2]['text']['text'])
        self.assertIn('• 不再重复领取',payload['text'])
        self.assertIn('查看 PR',payload['blocks'][-1]['elements'][0]['text'])

    def test_summary_escaped_in_slack_blocks(self):
        payload=a.card('o/s','x','完成',PR['html_url'],PR,['<!channel> & data'])
        self.assertIn('&lt;!channel&gt; &amp; data',payload['blocks'][2]['text']['text'])

    def test_final_merge_summary_is_sent_once_without_an_intermediate_card(self):
        with patch.dict(os.environ, GH_TOKEN='test'):
            pilot=a.Pilot('o/s','main',Path('.'))
        notices=set()
        pilot.notices=lambda p:set(notices)
        pilot.mark=lambda p,key,text:notices.add(key)
        pilot.pages=lambda path:FILES
        with patch.object(a,'summarize',return_value=['更新余额刷新规则']) as summarize,patch.object(a,'slack') as slack:
            pilot.notify(PR,'merged','合入','已合入发布')
            pilot.notify(PR,'merged','合入','已合入发布')
        summarize.assert_called_once();slack.assert_called_once()
        self.assertIn('• 更新余额刷新规则',slack.call_args.args[0]['text'])

    def test_file_read_failure_still_sends_notification(self):
        with patch.dict(os.environ, GH_TOKEN='test'):
            pilot=a.Pilot('o/s','main',Path('.'))
        pilot.notices=lambda p:set();pilot.mark=lambda *args:None
        pilot.pages=lambda path: (_ for _ in ()).throw(RuntimeError('unavailable'))
        with patch.object(a,'slack') as slack: pilot.notify(PR,'merged','合入','已合入发布')
        slack.assert_called_once()

    def test_previously_sent_notification_does_not_regenerate_summary(self):
        with patch.dict(os.environ, GH_TOKEN='test'):
            pilot=a.Pilot('o/s','main',Path('.'))
        pilot.notices=lambda p:{'merged'}
        with patch.object(a,'summarize') as summarize,patch.object(a,'slack') as slack:
            pilot.notify(PR,'merged','合入','已合入发布')
        summarize.assert_not_called();slack.assert_not_called()


if __name__ == '__main__':unittest.main()
