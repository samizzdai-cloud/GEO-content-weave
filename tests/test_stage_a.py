import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from geo_workbench import store, workflow
from geo_workbench.app import app, TOKEN

ROOT=Path(__file__).resolve().parents[1]
LUMA=json.loads((ROOT/'test-data/luma-normal.json').read_text(encoding='utf-8'))
ORBIT=json.loads((ROOT/'test-data/orbit-normal.json').read_text(encoding='utf-8'))

class FakeProvider:
    @staticmethod
    def audit_input(run_id,product,query):
        return {'issues':[{'type':'unsupported','user_quote':'wrinkle-free','request':'wrinkle-free','reason':'not supplied'}] if 'wrinkle-free' in query else []}
    @staticmethod
    def audience(run_id,contract): return {'disclosure':'simulated','personas':[{'scenario':contract['product']['category']}]}
    @staticmethod
    def evidence(run_id,contract): return {'sources':[],'limitations':['mock only']}
    @staticmethod
    def plan(run_id,c,a,e): return {'blog_outline':[c['product']['category']],'product_outline':[],'question_map':[],'competitor_analysis':[],'evidence_gaps':['mock']}

class StageA(unittest.TestCase):
    def setUp(self):
        self.worker=patch.dict('os.environ',{'GEO_WORKER_DISABLED':'1'});self.worker.start();self.addCleanup(self.worker.stop)
        self.tmp=tempfile.TemporaryDirectory();self.old=store.ROOT;store.ROOT=Path(self.tmp.name);store.init()
    def tearDown(self):
        store.ROOT=self.old;self.tmp.cleanup()
    def create(self,product=LUMA,query='normal'):
        rid=store.create(copy.deepcopy(product),query);store.put(rid,'original_input',{'product':product,'user_query':query});return rid
    def start(self,rid): workflow.execute(rid,provider=FakeProvider)
    def resume(self,rid,payload): workflow.execute(rid,'resume',payload,provider=FakeProvider)
    def test_normal_resume_and_stale_idempotency(self):
        rid=self.create();self.start(rid);r=store.get(rid)
        self.assertEqual(r['status'],'awaiting_plan_approval')
        self.assertTrue(store.decisions(rid,'one','plan_decision',{'action':'approve'},'awaiting_plan_approval',r['plan_rev'],r['plan_hash']))
        self.assertFalse(store.decisions(rid,'one','plan_decision',{'action':'approve'},'awaiting_plan_approval',r['plan_rev'],r['plan_hash']))
        self.resume(rid,{'action':'approve'});self.assertEqual(store.get(rid)['status'],'plan_approved')
        with self.assertRaises(ValueError): store.decisions(rid,'two','plan_decision',{'action':'approve'},'awaiting_plan_approval',r['plan_rev'],r['plan_hash'])
    def test_test_runner_approval_identity_is_preserved(self):
        rid=self.create();self.start(rid);r=store.get(rid)
        store.decisions(rid,'test','plan_decision',{'action':'approve'},'awaiting_plan_approval',r['plan_rev'],r['plan_hash'],actor='test_runner')
        with store.connect() as db:
            decision=json.loads(db.execute("SELECT payload FROM decisions WHERE run_id=? AND idempotency_key='test'",(rid,)).fetchone()['payload'])
        self.assertEqual(decision['_approval']['actor'],'test_runner')
        self.assertEqual([e for e in store.events(rid) if e['type']=='decision'][-1]['detail']['actor'],'test_runner')
    def test_unsupported_gate_resolution(self):
        rid=self.create(query='write wrinkle-free');self.start(rid)
        self.assertEqual(store.get(rid)['status'],'awaiting_input_resolution')
        self.assertIsNone(store.latest(rid,'audience'))
        data={'action':'retry','user_query':'use confirmed facts'}
        store.decisions(rid,'i','input_resolution',data,'awaiting_input_resolution');self.resume(rid,data)
        self.assertEqual(store.get(rid)['status'],'awaiting_plan_approval')
    def test_missing_and_cross_category(self):
        missing=copy.deepcopy(LUMA);del missing['product']['facts']['price'];del missing['product']['facts']['care']
        rid=self.create(missing);self.start(rid)
        self.assertEqual(len(store.latest(rid,'task_contract')['missing_fields']),2)
        self.assertTrue(any(x['pointer']=='/product/facts/material' and x['value']=='100% linen' for x in store.latest(rid,'fact_set')['facts']))
        orb=self.create(ORBIT);self.start(orb)
        self.assertIn('drinkware',store.latest(orb,'plan')['blog_outline'][0])
        self.assertNotEqual(store.latest(rid,'plan'),store.latest(orb,'plan'))
        with self.assertRaises(KeyError): store.read_artifact(orb,store.artifacts(rid)[0]['id'])
    def test_invalid_input_never_runs_research(self):
        p=copy.deepcopy(LUMA);p['content_requests']['blog']['target_words']=[800,600]
        rid=self.create(p);self.start(rid)
        self.assertEqual(store.get(rid)['status'],'blocked_input');self.assertIsNone(store.latest(rid,'audience'))
    def test_browser_security_and_plan_revision(self):
        rid=self.create();self.start(rid);r=store.get(rid)
        with TestClient(app) as client:
            self.assertEqual(client.post('/api/runs',json={'product':LUMA,'user_query':'x'}).status_code,403)
            self.assertEqual(client.post('/api/runs',headers={'x-geo-token':TOKEN,'origin':'https://evil.example'},json={'product':LUMA,'user_query':'x'}).status_code,403)
            plan=store.latest(rid,'plan');plan['evidence_gaps']=['edited']
            resp=client.post(f'/api/runs/{rid}/plan',headers={'x-geo-token':TOKEN},json={'revision':r['plan_rev'],'digest':r['plan_hash'],'plan':plan})
            self.assertEqual(resp.status_code,200)
            stale=client.post(f'/api/runs/{rid}/decision',headers={'x-geo-token':TOKEN},json={'revision':r['plan_rev'],'digest':r['plan_hash'],'action':'approve','idempotency_key':'stale'})
            self.assertEqual(stale.status_code,409)
    def test_new_plan_edit_keeps_dependency_and_topic(self):
        class NewProvider(FakeProvider):
            @staticmethod
            def plan(run_id,c,a,e):
                return {'schema_version':'2.0','blog_outline':{},'product_outline':{},'question_map':[],'competitor_analysis':[],'evidence_gaps':[],
                    'editorial_strategy':{'topic':c['content_requests']['blog']['topic'],'reader_goal':'choose','narrative_throughline':'need to decision','selected_questions':[{'question_id':'q1','reason':'relevant'}],'blog_pdp_roles':'guide and product','product_link_rationale':'next step'},
                    'writing_brief':{'product_fact_pointers':[],'evidence_refs':[],'unknowns':[],'prohibited_claims':[],'page_requirements':{}}}
        rid=self.create();workflow.execute(rid,provider=NewProvider);before=store.get(rid)
        plan=store.latest(rid,'plan');deps=copy.deepcopy(plan['research_dependencies'])
        plan['research_dependencies']={'audience':'fake'};plan['editorial_strategy']['topic']='fabricated';plan['schema_version']='1.0'
        with TestClient(app) as client:
            response=client.post(f'/api/runs/{rid}/plan',headers={'x-geo-token':TOKEN},json={'revision':before['plan_rev'],'digest':before['plan_hash'],'plan':plan})
        self.assertEqual(response.status_code,200)
        saved=store.latest(rid,'plan');self.assertEqual(saved['research_dependencies'],deps)
        self.assertEqual(saved['schema_version'],'2.0')
        self.assertEqual(saved['editorial_strategy']['topic'],LUMA['content_requests']['blog']['topic'])
        self.assertTrue(any(w['kind']=='question_ref' for w in saved['brief_warnings']))
    def test_bad_url_and_dedupe(self):
        from geo_workbench.research import public_url, Tools
        self.assertFalse(public_url('http://127.0.0.1/x'));self.assertFalse(public_url('http://localhost/x'));self.assertFalse(public_url('https://user:pass@example.com'))
        self.assertTrue(public_url('https://example.com'))
        rid=self.create()
        with patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test','DEEPSEEK_BASE_URL':'https://api.deepseek.com'}):
            tools=Tools(rid)
            with patch.object(tools,'post',return_value={'title':'Example','content':'body'}) as call:
                self.assertEqual(tools.fetch('https://example.com'),tools.fetch('https://example.com'))
                self.assertEqual(call.call_count,1)
                self.assertEqual(tools.fetches,1)
    def test_invalid_model_issue_never_researches(self):
        class Bad(FakeProvider):
            @staticmethod
            def audit_input(run_id,product,query): return {'issues':[{'user_quote':'not in query','reason':'bad'}]}
        rid=self.create()
        workflow.execute(rid,provider=Bad)
        self.assertEqual(store.get(rid)['status'],'needs_review')
        self.assertIsNone(store.latest(rid,'audience'))
    def test_redo_creates_new_plan_and_reapproval(self):
        rid=self.create();self.start(rid);old=store.get(rid)
        action={'action':'redo','feedback':'expand portability'}
        store.decisions(rid,'redo-key','plan_decision',action,'awaiting_plan_approval',old['plan_rev'],old['plan_hash'])
        self.resume(rid,action)
        updated=store.get(rid)
        self.assertEqual(updated['status'],'awaiting_plan_approval')
        self.assertGreater(updated['plan_rev'],old['plan_rev'])
        self.assertEqual(store.latest(rid,'review_feedback')['feedback'],'expand portability')
        with self.assertRaises(ValueError):
            store.decisions(rid,'stale-key','plan_decision',{'action':'approve'},'awaiting_plan_approval',old['plan_rev'],old['plan_hash'])
    def test_failed_sources_are_flagged(self):
        warnings=workflow.citation_warnings({'section':{'source_ids':['s1','s2','missing']}},{'sources':[{'source_id':'s1','status':'ok'},{'source_id':'s2','status':'failed'}]})
        self.assertEqual({x['source_id'] for x in warnings},{'s2','missing'})
    def test_http_input_revision_and_duplicate_resolution(self):
        rid=self.create(query='write wrinkle-free');self.start(rid)
        updated=copy.deepcopy(LUMA);updated['product']['facts']['colors']=['White']
        data={'action':'retry','user_query':'use confirmed facts','product':updated,'idempotency_key':'fixed-key'}
        def run_sync(run_id,mode='start',payload=None): workflow.execute(run_id,mode,payload,provider=FakeProvider)
        with patch('geo_workbench.app.workflow.schedule',side_effect=run_sync), TestClient(app) as client:
            h={'x-geo-token':TOKEN}
            first=client.post(f'/api/runs/{rid}/resolve',headers=h,json=data)
            self.assertEqual(first.status_code,200)
            duplicate=client.post(f'/api/runs/{rid}/resolve',headers=h,json=data)
            self.assertEqual(duplicate.status_code,200);self.assertTrue(duplicate.json()['duplicate'])
        self.assertEqual(store.get(rid)['status'],'awaiting_plan_approval')
        self.assertEqual(store.latest(rid,'task_contract')['product']['facts']['colors'],['White'])
        self.assertEqual(store.latest(rid,'original_input')['product']['product']['facts']['colors'],['White','Light Blue'])

if __name__=='__main__': unittest.main()
