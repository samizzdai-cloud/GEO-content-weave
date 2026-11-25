import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from langchain_core.messages import AIMessage, ToolMessage
from geo_workbench import intake_dialog, store, workflow
from test_stage_a import LUMA, FakeProvider


class IntakeDialogTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=store.ROOT;store.ROOT=Path(self.tmp.name);store.init()
        self.rid=store.create(copy.deepcopy(LUMA),'Emphasize wrinkle-free and commuting')
        store.set_status(self.rid,'awaiting_input_resolution',issues=[{'user_quote':'wrinkle-free','reason':'not supplied'}])
    def tearDown(self):store.ROOT=self.old;self.tmp.cleanup()
    def test_preview_preserves_false_zero_and_checks_old_values(self):
        product=copy.deepcopy(LUMA);product['product']['facts']['flag']=False;product['product']['facts']['number']=0
        operations=[{'operation':'replace','pointer':'/product/facts/number','old_value':0,'new_value':1,'user_quote':'number is 1','reason':'correction'}]
        result,diff=intake_dialog.preview(product,'keep commuting',operations,'number is 1','dialog')
        self.assertEqual(result['product']['facts']['number'],1);self.assertIs(result['product']['facts']['flag'],False)
        self.assertEqual(product['product']['facts']['number'],0);self.assertEqual(diff[0]['old_value'],0)
        warning_op=[{'operation':'add','pointer':'/product/facts/wrinkle_free','new_value':True,'user_quote':'confirmed wrinkle-free','reason':'supplied'}]
        _,warning=intake_dialog.preview(product,'commuting',warning_op,'confirmed wrinkle-free','dialog')
        self.assertTrue(warning[0]['warnings'])
        operations[0]['old_value']=False
        with self.assertRaises(ValueError):intake_dialog.preview(product,'keep commuting',operations,'number is 1','dialog')
        with self.assertRaises(ValueError):intake_dialog.preview(product,'keep commuting',operations,'number is 1','original')
    def test_dialog_candidate_requires_confirmation_and_idempotently_versions(self):
        observed=[]
        class Model:
            def bind_tools(self,tools,**kw):return self
            def invoke(self,messages):
                observed.append(copy.deepcopy(messages))
                if len(observed)==1:
                    return AIMessage(content='',tool_calls=[{'name':'preview_candidate','args':{'effective_query':'Focus on commuting','operations':[{'operation':'add','pointer':'/product/facts/wrinkle_free','new_value':True,'user_quote':'It is wrinkle-free','reason':'user confirmed'}]},'id':'call-1','type':'tool_call'}])
                return AIMessage(content='请确认商品资料修改。')
        with patch('geo_workbench.intake_dialog.workflow.check_gate',return_value=True):
            view=intake_dialog.respond(self.rid,'It is wrinkle-free',model_factory=Model)
            self.assertTrue(any(isinstance(m,ToolMessage) and m.tool_call_id=='call-1' for m in observed[1]))
            candidate=view['candidate'];self.assertIsNotNone(candidate)
            self.assertNotIn('wrinkle_free',store.get(self.rid)['product']['product']['facts'])
            self.assertTrue(intake_dialog.confirm(self.rid,candidate['id'],candidate['base_digest'],'same'))
            self.assertFalse(intake_dialog.confirm(self.rid,candidate['id'],candidate['base_digest'],'same'))
        self.assertTrue(store.get(self.rid)['product']['product']['facts']['wrinkle_free'])
        self.assertEqual(len(intake_dialog.state(self.rid)['versions']),2)
    def test_original_choice_keeps_product_and_stale_candidate_is_rejected(self):
        class Model:
            def __init__(self):self.n=0
            def bind_tools(self,tools,**kw):return self
            def invoke(self,messages):
                self.n+=1
                if self.n==1:return AIMessage(content='',tool_calls=[{'name':'preview_candidate','args':{'effective_query':'Focus on commuting','operations':[]},'id':'c','type':'tool_call'}])
                return AIMessage(content='请确认。')
        original=store.get(self.rid)['product']
        with patch('geo_workbench.intake_dialog.workflow.check_gate',return_value=True):
            c=intake_dialog.respond(self.rid,'按原商品资料继续',mode='original',model_factory=Model)['candidate']
            with self.assertRaises(ValueError):intake_dialog.confirm(self.rid,c['id'],'stale','wrong')
            self.assertTrue(intake_dialog.confirm(self.rid,c['id'],c['base_digest'],'correct'))
        self.assertEqual(store.get(self.rid)['product'],original);self.assertEqual(len(intake_dialog.state(self.rid)['versions']),1)
    def test_confirmed_version_reaches_existing_research_graph(self):
        rid=store.create(copy.deepcopy(LUMA),'write wrinkle-free for commuting')
        workflow.execute(rid,provider=FakeProvider)
        self.assertEqual(store.get(rid)['status'],'awaiting_input_resolution')
        class Model:
            def __init__(self):self.calls=0
            def bind_tools(self,tools,**kwargs):return self
            def invoke(self,messages):
                self.calls+=1
                if self.calls==1:return AIMessage(content='',tool_calls=[{'name':'preview_candidate','args':{'effective_query':'Focus on commuting','operations':[{'operation':'add','pointer':'/product/facts/new_feature','new_value':'confirmed','user_quote':'I confirm the feature','reason':'user supplied'}]},'id':'c','type':'tool_call'}])
                return AIMessage(content='请确认。')
        c=intake_dialog.respond(rid,'I confirm the feature',model_factory=Model)['candidate']
        self.assertTrue(intake_dialog.confirm(rid,c['id'],c['base_digest'],'accept',actor='test_runner'))
        self.assertEqual([e for e in store.events(rid) if e['type']=='input_candidate_confirmed'][-1]['detail']['actor'],'test_runner')
        with store.connect() as db:
            decision=json.loads(db.execute("SELECT payload FROM decisions WHERE run_id=? AND idempotency_key='accept'",(rid,)).fetchone()['payload'])
        self.assertEqual(decision['actor'],'test_runner')
        workflow.execute(rid,'resume',{'action':'retry'},provider=FakeProvider)
        self.assertEqual(store.get(rid)['status'],'awaiting_plan_approval')
        self.assertEqual(store.latest(rid,'task_contract')['product']['facts']['new_feature'],'confirmed')
        self.assertEqual(store.latest(rid,'plan')['input_snapshot']['revision'],2)
