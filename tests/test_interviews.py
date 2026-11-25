import copy
import json
import tempfile
import threading
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from geo_workbench import research, store, workflow

ROOT=Path(__file__).resolve().parents[1]
PRODUCT=json.loads((ROOT/'test-data/luma-normal.json').read_text(encoding='utf-8'))

class Interviews(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=store.ROOT;store.ROOT=Path(self.tmp.name);store.init()
        self.rid=store.create(copy.deepcopy(PRODUCT),'normal')
        self.contract,_=workflow.contract(PRODUCT,'normal')
    def tearDown(self):
        store.ROOT=self.old;self.tmp.cleanup()

    def test_two_roles_two_rounds_and_isolated_histories(self):
        calls=[];lock=threading.Lock()
        def fake(run,node,skill,context,thinking='disabled',messages=None):
            with lock: calls.append((node,copy.deepcopy(context),copy.deepcopy(messages)))
            if node=='audience_seed': return {'personas':[{'scenario':'desk'},{'scenario':'travel'}]}
            if node=='interviewer': return {'question':context['persona']['id']+(' q2' if messages else ' q1')}
            if node=='persona_response': return {'answer':context['persona']['id']+' answer'}
            if node=='audience_synthesis': return {'question_map':[{'question':'What matters?'}],'persona_details':[{'persona_id':p['id'],'decision_path':[{'stage_label':'evaluate'}]} for p in context['personas']]}
            raise AssertionError(node)
        with patch.object(research,'model_json',side_effect=fake): result=research.audience(self.rid,self.contract)
        self.assertEqual(result['status'],'complete')
        self.assertEqual(len(result['interviews']),2)
        for session in result['interviews']:
            self.assertEqual([(m['round'],m['role']) for m in session['messages']],[(1,'interviewer'),(1,'persona'),(2,'interviewer'),(2,'persona')])
            self.assertTrue(all(session['persona_id'] in m['content'] for m in session['messages']))
        for node,context,messages in calls:
            if node in ('interviewer','persona_response') and messages:
                own=context['persona']['id'];other='p2' if own=='p1' else 'p1'
                self.assertNotIn(other,json.dumps(messages))
        interviewer_second=[m for n,c,m in calls if n=='interviewer' and m]
        self.assertEqual(len(interviewer_second),2)
        self.assertTrue(all(len(m)==2 and m[-1]['role']=='user' for m in interviewer_second))
        interviewer_first=[(c,m) for n,c,m in calls if n=='interviewer' and not m]
        self.assertTrue(all('facts' not in c['contract'] for c,m in interviewer_first))
        self.assertTrue(all('/product/facts/material' in m[-1]['content'] for m in interviewer_second))
        self.assertEqual(len([a for a in store.artifacts(self.rid) if a['kind']=='interview_progress']),10)

    def test_interview_failure_persists_and_blocks_plan(self):
        def fake(run,node,skill,context,thinking='disabled',messages=None):
            if node=='audience_seed': return {'personas':[{'scenario':'desk'}]}
            if node=='interviewer': return {'question':'question'}
            if node=='persona_response': raise TimeoutError()
            raise AssertionError('must not synthesize incomplete interviews')
        with patch.object(research,'model_json',side_effect=fake): aud=research.audience(self.rid,self.contract)
        self.assertEqual(aud['status'],'incomplete')
        self.assertEqual(len(aud['interviews'][0]['messages']),1)
        self.assertEqual(store.latest(self.rid,'interview_progress')['status'],'incomplete')
        class Provider:
            @staticmethod
            def audit_input(*args): return {'issues':[]}
            @staticmethod
            def audience(*args): return aud
            @staticmethod
            def evidence(*args): return {'sources':[],'limitations':[]}
            @staticmethod
            def plan(*args): raise AssertionError('must not plan')
        workflow.execute(self.rid,provider=Provider)
        self.assertEqual(store.get(self.rid)['status'],'needs_review')
        self.assertIsNone(store.latest(self.rid,'plan'))

    def test_synthesis_failure_keeps_completed_interviews(self):
        def fake(run,node,skill,context,thinking='disabled',messages=None):
            if node=='audience_seed': return {'personas':[{'scenario':'desk'}]}
            if node=='interviewer': return {'question':'question'}
            if node=='persona_response': return {'answer':'answer'}
            if node=='audience_synthesis': return {'persona_details':[],'question_map':[]}
            raise AssertionError(node)
        with patch.object(research,'model_json',side_effect=fake): result=research.audience(self.rid,self.contract)
        self.assertEqual(result['status'],'incomplete')
        self.assertEqual(result['interviews'][0]['status'],'complete')
        self.assertEqual(len(result['interviews'][0]['messages']),4)
        self.assertIsNotNone(store.latest(self.rid,'persona_seed'))

    def test_report_is_readable_and_old_format_marked(self):
        workflow.write_report(self.rid,{}, {'personas':[]}, {'sources':[]}, {'question_map':['question'],'competitor_analysis':[]})
        report=store.latest(self.rid,'research_report')
        self.assertIn('历史版本',report)
        self.assertIn('购买问题地图',report)
        self.assertNotIn('```json',report)

    def test_report_keeps_interview_text_without_source_snapshot(self):
        audience={'schema_version':'2.0','interviews':[{'messages':[{'role':'persona','content':'my actual answer'}]}]}
        workflow.write_report(self.rid,{},audience,{'sources':[{'source_id':'s1','content':'raw webpage body'}]}, {'question_map':[]})
        report=store.latest(self.rid,'research_report')
        self.assertIn('my actual answer',report)
        self.assertNotIn('raw webpage body',report)

    def test_comparison_references_do_not_count_as_semantic_proof(self):
        plan={'competitor_analysis':[
            {'source_ids':['s1'],'product_fact_refs':['/product/facts/material']},
            {'source_ids':['s2'],'product_fact_refs':['/product/facts/imaginary']},
            {'source_ids':['s1'],'product_fact_refs':[]}]}
        workflow.mark_comparisons(plan,{'sources':[{'source_id':'s1','status':'ok'},{'source_id':'s2','status':'failed'}]},self.contract)
        rows=plan['competitor_analysis']
        self.assertEqual(rows[0]['evidence_status'],'needs_human_review')
        self.assertEqual(rows[1]['evidence_status'],'insufficient_evidence')
        self.assertEqual(rows[1]['reference_check']['invalid_sources'],['s2'])
        self.assertEqual(rows[1]['reference_check']['invalid_product_pointers'],['/product/facts/imaginary'])
        self.assertEqual(rows[2]['evidence_status'],'insufficient_evidence')

    def test_invalid_json_is_retried_once_and_usage_is_recorded(self):
        replies=[SimpleNamespace(content='{invalid',usage_metadata={'input_tokens':7},response_metadata={}),
                 SimpleNamespace(content='{"answer":"ok"}',usage_metadata={'input_tokens':9},response_metadata={})]
        model=SimpleNamespace(invoke=lambda *args,**kwargs:replies.pop(0))
        with patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test','DEEPSEEK_BASE_URL':'https://api.deepseek.com'}), patch.object(research,'ChatDeepSeek',return_value=model):
            self.assertEqual(research.model_json(self.rid,'test','audience',{}),{'answer':'ok'})
        events=[e['detail'] for e in store.events(self.rid) if e['type']=='model']
        self.assertEqual([e['status'] for e in events],['invalid_json','ok'])
        self.assertEqual([e['usage']['input_tokens'] for e in events],[7,9])

    def test_json_code_fence_is_unwrapped_without_inventing_content(self):
        model=SimpleNamespace(invoke=lambda *args,**kwargs:SimpleNamespace(content='```json\n{"answer":"ok"}\n```',usage_metadata={},response_metadata={}))
        with patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test','DEEPSEEK_BASE_URL':'https://api.deepseek.com'}), patch.object(research,'ChatDeepSeek',return_value=model):
            self.assertEqual(research.model_json(self.rid,'test','audience',{}),{'answer':'ok'})
        events=[e['detail'] for e in store.events(self.rid) if e['type']=='model']
        self.assertEqual(events[0]['normalization'],'markdown_fence_removed')

if __name__=='__main__': unittest.main()
