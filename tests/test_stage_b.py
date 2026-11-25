import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from geo_workbench import content, store, workflow
from geo_workbench.app import app, TOKEN
from test_stage_a import FakeProvider, LUMA, ORBIT


class ProviderA(FakeProvider):
    @staticmethod
    def audience(run_id,contract):
        return {'schema_version':'2.0','status':'complete','disclosure':'simulated','personas':[],
                'question_map':[{'question_id':'q1','question':'What matters?'}]}
    @staticmethod
    def evidence(run_id,contract): return {'sources':[],'evidence_items':[],'limitations':['no external evidence']}
    @staticmethod
    def plan(run_id,c,a,e):
        outline={'h1':'title','meta_title':'meta','meta_description':'description','faq_ideas':['a','b','c']}
        return {'schema_version':'2.0','blog_outline':copy.deepcopy(outline),'product_outline':copy.deepcopy(outline),'question_map':[],
                'competitor_analysis':[],'evidence_gaps':[],
                'editorial_strategy':{'topic':c['content_requests']['blog']['topic'],'reader_goal':'choose','narrative_throughline':'decision',
                    'selected_questions':[{'question_id':'q1','reason':'relevant'}],'blog_pdp_roles':'guide and product','product_link_rationale':'next step'},
                'writing_brief':{'product_fact_pointers':[],'evidence_refs':[],'unknowns':[],'prohibited_claims':[],'page_requirements':{}}}

def fake_ir(product_name):
    def page(kind):
        return {'page_type':kind,'language':'en','h1':product_name,'meta':{'title':'A title','description':'A description'},
                'sections':[{'id':'s1','heading':'Decide','blocks':[{'id':'b1','text':'Consider the intended use.'},{'id':'b2','text':'Compare the available choices.'}]}],
                'faqs':[{'id':f'f{i}','question':f'Question {i}?','answer':'Consider your needs.'} for i in range(3)]}
    return {'pages':[page('blog'),page('product_page')]}

class ProviderB:
    def __init__(self,run_id,attempt_id): self.calls=[]
    def call(self,node,skill,task,context):
        self.calls.append(node)
        if node in ('write','rewrite'):
            task_data=context if node=='write' else context['approved_context']
            ir=fake_ir(task_data['task']['product']['name'])
            value=task_data['task']['product']['facts']['type']
            for page in ir['pages']:
                page['sections'][0]['blocks'][0].update(text='Product type: '+value+'.',evidence_refs=['/product/facts/type'])
            return ir
        if node in ('fact_check','fact_recheck','fact_regression'):
            return {'units':[{'unit_id':u['id'],'claims':[{'text':u['text'],'kind':'product','source_ref':'/product/facts/type','status':'supported','notes':'fixture'}] if '/product/facts/type' in u.get('evidence_refs',[]) else []} for u in context['units']]}
        if node in ('edit','edit_retry'):
            editable=[u for u in context['editable_units'] if u['id'].endswith(':b1') or u['id'].endswith(':b2')]
            return {'operations':[{'unit_id':u['id'],'before':u['text'],'after':u['text']+' Please.','reason':'clarity'} for u in editable[:2]]}
        raise AssertionError(node)

class CompleteProvider(ProviderB):
    """A bounded fixture with enough distinct grounded product facts to deliver."""
    def call(self,node,skill,task,context):
        if node=='write':
            ir=fake_ir(context['task']['product']['name'])
            for page in ir['pages']:
                page['sections'][0]['blocks']=[{'id':f'b{i+1}','text':f'{key}: {context["task"]["product"]["facts"][key]}.',
                    'evidence_refs':['/product/facts/'+key]} for i,key in enumerate(['type','material','fit','care','colors'])]
            return ir
        if node in ('fact_check','fact_regression'):
            return {'units':[{'unit_id':u['id'],'claims':[{'text':u['text'],'kind':'product','source_refs':u['evidence_refs'],'status':'supported'}] if u.get('evidence_refs') else []} for u in context['units']]}
        return super().call(node,skill,task,context)

class StageB(unittest.TestCase):
    def complete_fixture(self):
        product=copy.deepcopy(LUMA)
        for kind in ('blog','product_page'):product['content_requests'][kind]['target_words']=[1,1000]
        rid,r=self.approved(product);attempt,_=store.reserve_content(rid,'completed-output',r['plan_rev'],r['plan_hash'])
        content.run_trial(rid,attempt['id'],CompleteProvider)
        return rid,attempt

    def test_completed_text_delivery_has_optional_review_and_still_accepts_edits(self):
        rid,attempt=self.complete_fixture()
        self.assertEqual(store.get(rid)['status'],'completed')
        self.assertNotIn(content.DRAFT_NOTICE,store.latest(rid,'content_pages')['data']['pages']['blog'])
        summary=content.review_summary(content.attempt_artifacts(rid,attempt['id']))
        self.assertTrue(summary['task_complete']);self.assertFalse(summary['pending_checks'])
        self.assertFalse(summary['human_review_required']);self.assertFalse(summary['human_accepted'])
        r=store.get(rid);self.assertTrue(content.approved_snapshot(rid,r['plan_rev'],r['plan_hash']))
        store.reserve_content(rid,'optional-regeneration',r['plan_rev'],r['plan_hash'])
        self.assertEqual(store.get(rid)['status'],'generating')

    def test_jsonld_and_markdown_downloads_match_same_attempt_and_never_call_models(self):
        rid,attempt=self.complete_fixture();base=f'/api/runs/{rid}/content-attempts/{attempt["id"]}'
        before=len(store.events(rid))
        with TestClient(app) as client:
            pages=client.get(base).json()['artifacts']['content_pages']['data']['pages']
            jsonld=client.get(base+'/download/product.jsonld');markdown=client.get(base+'/download/blog.md')
            self.assertEqual(jsonld.status_code,200);self.assertEqual(markdown.status_code,200)
            self.assertEqual(jsonld.json(),content.product_jsonld(pages['product_page']))
            self.assertIn('application/ld+json',jsonld.headers['content-type'])
            self.assertIn('product.jsonld',jsonld.headers['content-disposition'])
            self.assertIn('text/markdown',markdown.headers['content-type'])
            self.assertIn('# '+LUMA['product']['name'],markdown.text)
            self.assertIn('material: 100% linen.',markdown.text)
            self.assertIn('](product.html)',markdown.text)
            self.assertNotIn('evidence_refs',markdown.text);self.assertNotIn('/product/facts',markdown.text)
            self.assertEqual(client.get(base+'/download/arbitrary.txt').status_code,404)
            other=store.create(ORBIT,'other')
            self.assertEqual(client.get(f'/api/runs/{other}/content-attempts/{attempt["id"]}/download/product.jsonld').status_code,404)
            current=store.latest(rid,'content_ir')
            content.save(rid,'content_ir',attempt['id'],current['data'])
            self.assertEqual(client.get(base+'/download/blog.md').status_code,409)
        self.assertFalse(any(e['type']=='content_model' for e in store.events(rid)[before:]))

    def test_informational_checks_do_not_block_legacy_completion_or_rewrite_artifacts(self):
        rid,attempt=self.complete_fixture()
        evaluation=store.latest(rid,'content_evaluation')['data']
        evaluation['checks'].append({'check':'source_text_overlap','status':'unknown','total_matches':3})
        content.save(rid,'content_evaluation',attempt['id'],evaluation)
        pages=store.latest(rid,'content_pages')['data']
        pages['pages']['blog']=pages['pages']['blog'].replace('<main>',content.DRAFT_NOTICE+'<main>',1)
        content.save(rid,'content_pages',attempt['id'],pages)
        store.set_content_status(rid,attempt['id'],'needs_review','试生成已完成，需人工审核；检查项可能包含未通过或未知。')
        content.reconcile_completion()
        self.assertEqual(store.get(rid)['status'],'completed')
        self.assertIn(content.DRAFT_NOTICE,store.latest(rid,'content_pages')['data']['pages']['blog'])
        found=content.attempt_artifacts(rid,attempt['id'])
        self.assertNotIn(content.DRAFT_NOTICE,found['content_pages']['data']['pages']['blog'])
        self.assertFalse(content.review_summary(found)['pending_checks'])
        self.assertFalse(any(e['type']=='decision' and e['detail'].get('kind')=='content_approval' for e in store.events(rid)))

    def test_attempt_preview_links_resolve_inside_same_attempt_and_remain_sandboxed(self):
        from urllib.parse import urljoin
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'preview-routes',r['plan_rev'],r['plan_hash'])
        content.run_trial(rid,attempt['id'],ProviderB)
        base=f'/api/runs/{rid}/content-attempts/{attempt["id"]}/preview/'
        other,_=self.approved(ORBIT)
        with TestClient(app) as client:
            blog=client.get(base+'blog.html')
            self.assertEqual(blog.status_code,200);self.assertIn('href="product.html"',blog.text)
            product=client.get(urljoin(base+'blog.html','product.html'))
            self.assertEqual(product.status_code,200);self.assertIn(LUMA['product']['name'],product.text)
            self.assertIn('sandbox;',product.headers['content-security-policy'])
            self.assertIn("frame-ancestors 'self'",product.headers['content-security-policy'])
            self.assertNotIn('allow-scripts',product.headers['content-security-policy'])
            self.assertEqual(client.get(base+'other.html').status_code,404)
            self.assertEqual(client.get(f'/api/runs/{other}/content-attempts/{attempt["id"]}/preview/product.html').status_code,404)
            summary=client.get(f'/api/runs/{rid}/content-attempts/{attempt["id"]}').json()['review_summary']
            self.assertTrue(summary['processing_complete']);self.assertTrue(summary['fact_review_complete'])
            self.assertEqual(summary['fact_issue_count'],0);self.assertFalse(summary['human_accepted'])
            self.assertTrue(summary['failed_checks'])  # Deliberately short fixture remains a quality failure.
        self.assertIsNone(store.get(rid)['error'])

    def test_completed_legacy_message_is_projected_as_pending_human_not_execution_failure(self):
        from geo_workbench import queue
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'legacy-completion',r['plan_rev'],r['plan_hash'])
        content.run_trial(rid,attempt['id'],ProviderB)
        store.set_status(rid,'needs_review','试生成已完成，需人工审核；检查项可能包含未通过或未知。')
        view=queue.projection(rid)
        self.assertEqual(view['state'],'content_ready_for_review');self.assertIsNone(view['error'])

    def test_v2_review_of_legacy_draft_rechecks_before_editor_without_writer(self):
        class V2(ProviderB):
            checker_contract_version='2.0'
            def __init__(self,*args):super().__init__(*args);self.nodes=[]
            def call(self,node,skill,task,ctx):
                self.nodes.append(node);result=super().call(node,skill,task,ctx)
                if node in ('fact_check','fact_regression'):
                    for row in result['units']:
                        for claim in row['claims']:claim.pop('text')
                    result.update(checker_contract_version='2.0',content_revision=ctx['content_revision'])
                return result
        rid,r=self.approved();parent,_=store.reserve_content(rid,'legacy-parent',r['plan_rev'],r['plan_hash']);content.run_trial(rid,parent['id'],ProviderB)
        found={}
        for meta in store.artifacts(rid):
            if meta['kind'] in ('content_ir','claim_ledger'):
                value=store.read_artifact(rid,meta['id'])
                if value.get('attempt_id')==parent['id']:found[meta['kind']]=(meta,value)
        draft=found['content_ir'];ledger=found['claim_ledger']
        attempt,_=store.reserve_content(rid,'v2-review',r['plan_rev'],r['plan_hash']);provider=V2(rid,attempt['id'])
        content.run_trial(rid,attempt['id'],lambda *_:provider,review={'parent_attempt':parent['id'],'revision':draft[0]['revision'],
            'content_id':draft[0]['id'],'ledger_id':ledger[0]['id'],'feedback':['Review with program-owned targets.']})
        self.assertEqual(provider.nodes[0],'fact_check');self.assertNotIn('write',provider.nodes)
        self.assertEqual(store.latest(rid,'claim_ledger')['data']['checker_contract_version'],'2.0')
        self.assertTrue(any(e['type']=='checker_contract_upgrade' for e in store.events(rid)))

    def test_v2_checker_binds_program_text_and_rejects_quotes_wrong_revision_and_ids(self):
        ir=fake_ir('Name')
        result={'checker_contract_version':'2.0','content_revision':9,'units':[{'unit_id':u['id'],'claims':[]} for u in content.units(ir)]}
        target=next(r for r in result['units'] if r['unit_id']=='blog:b1')
        target['claims']=[{'kind':'guidance','scope':'none','factual_premise':False,'status':'uncertain','source_refs':[],'notes':'ordinary advice'}]
        bound=content.bind_checker_results(result,ir,9)
        claim=next(r for r in bound['units'] if r['unit_id']=='blog:b1')['claims'][0]
        self.assertEqual(claim['text'],'Consider the intended use.')
        self.assertEqual(claim['program_target']['content_revision'],9)
        self.assertNotIn('text',target['claims'][0])
        ledger=content.validate_ledger(bound,ir,{'product_facts':[],'external_evidence':[]})
        self.assertFalse(ledger['claims'][0]['actionable'])
        with self.assertRaises(ValueError):content.bind_checker_results(result,ir,10)
        target['claims'][0]['text']='Invented quotation'
        with self.assertRaises(ValueError):content.bind_checker_results(result,ir,9)
        del target['claims'][0]['text']
        result['units'][0]['unit_id']='other-task:unknown'
        with self.assertRaises(ValueError):content.bind_checker_results(result,ir,9)

    def test_v2_checker_complete_coverage_and_multiple_findings_share_program_original(self):
        ir=fake_ir('Name');rows=[{'unit_id':u['id'],'claims':[]} for u in content.units(ir)]
        target=next(r for r in rows if r['unit_id']=='blog:b1')
        target['claims']=[{'kind':'product','status':'unsupported','source_refs':[],'notes':'first aspect'},
                          {'kind':'category','status':'uncertain','source_refs':[],'notes':'second aspect'}]
        result={'checker_contract_version':'2.0','content_revision':1,'units':rows}
        bound=content.bind_checker_results(result,ir,1)
        claims=next(r for r in bound['units'] if r['unit_id']=='blog:b1')['claims']
        self.assertEqual(claims[0]['text'],claims[1]['text'])
        self.assertNotEqual(claims[0]['notes'],claims[1]['notes'])
        result['units']=rows[:-1]
        with self.assertRaises(ValueError):content.bind_checker_results(result,ir,1)
        result['units']=[*rows[:-1],rows[0]]
        with self.assertRaises(ValueError):content.bind_checker_results(result,ir,1)

    def test_v2_full_block_missing_phrase_does_not_exempt_other_product_facts(self):
        ir=fake_ir('Name');ir['pages'][0]['sections'][0]['blocks'][0]['text']='This shirt is wrinkle-free; drying time is not provided.'
        result={'checker_contract_version':'2.0','content_revision':1,'units':[{'unit_id':u['id'],'claims':[]} for u in content.units(ir)]}
        next(row for row in result['units'] if row['unit_id']=='blog:b1')['claims']=[{'kind':'product','status':'supported','source_refs':['/product/facts']}]
        bound=content.bind_checker_results(result,ir,1)
        ledger=content.validate_ledger(bound,ir,{'product_facts':[{'pointer':'/product/facts','value':{'material':'linen'}}],'external_evidence':[]})
        self.assertEqual(ledger['claims'][0]['status'],'uncertain')

    def test_v2_pipeline_uses_no_model_quotes_and_still_blocks_unsupported_claims(self):
        class V2(ProviderB):
            checker_contract_version='2.0'
            def call(self,node,skill,task,ctx):
                result=super().call(node,skill,task,ctx)
                if node in ('fact_check','fact_regression'):
                    self.task=task
                    for row in result['units']:
                        for claim in row['claims']:
                            claim.pop('text');claim['status']='unsupported'
                    result.update(checker_contract_version='2.0',content_revision=ctx['content_revision'])
                return result
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'v2-runtime',r['plan_rev'],r['plan_hash']);provider=V2(rid,attempt['id'])
        content.run_trial(rid,attempt['id'],lambda *_:provider)
        self.assertEqual(store.get(rid)['status'],'needs_review')
        ledger=store.latest(rid,'claim_ledger')['data']
        self.assertEqual(ledger['original_text_source'],'program')
        self.assertTrue(all(c.get('original_text_source')=='program' for c in ledger['claims']))
        self.assertEqual(ledger['checker_contract_version'],'2.0')
        self.assertIn('Do NOT output text',provider.task)
        self.assertFalse(any(m['kind']=='checker_alignment' for m in store.artifacts(rid)))

    def test_v2_bad_contract_gets_one_bounded_repair_not_a_quote_repair(self):
        class V2(ProviderB):
            checker_contract_version='2.0'
            def __init__(self,*args):super().__init__(*args);self.repairs=0
            def call(self,node,skill,task,ctx):
                result=super().call(node,skill,task,ctx)
                if node in ('fact_check','fact_regression'):
                    for row in result['units']:
                        for claim in row['claims']:claim.pop('text')
                    result.update(checker_contract_version='2.0',content_revision=ctx['content_revision'])
                    if node=='fact_check' and 'checker_output_to_repair' not in ctx:result['content_revision']=-1
                    if 'checker_output_to_repair' in ctx:self.repairs+=1
                return result
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'v2-schema-repair',r['plan_rev'],r['plan_hash']);provider=V2(rid,attempt['id'])
        content.run_trial(rid,attempt['id'],lambda *_:provider)
        self.assertEqual(provider.repairs,1)
        self.assertEqual(store.latest(rid,'claim_ledger')['data']['checker_contract_version'],'2.0')
        self.assertEqual(store.latest(rid,'content_checker_error')['data']['checker_contract_version'],'2.0')

    def test_classification_fields_are_repaired_by_checker_before_editor(self):
        class V2(ProviderB):
            checker_contract_version='2.0'
            def __init__(self,*args):super().__init__(*args);self.repairs=0;self.original=None
            def call(self,node,skill,task,ctx):
                result=super().call(node,skill,task,ctx)
                if node in ('fact_check','fact_regression'):
                    for row in result['units']:
                        for claim in row['claims']:claim.pop('text')
                    result.update(checker_contract_version='2.0',content_revision=ctx['content_revision'])
                    if node=='fact_check':
                        row=next(r for r in result['units'] if r['unit_id']=='blog:b2')
                        if 'checker_output_to_repair' not in ctx:
                            self.original=copy.deepcopy(ctx['units'])
                            row['claims']=[{'kind':'background','scope':'category','status':'supported','source_refs':[],'notes':'low risk ordinary advice'}]
                        else:
                            self.repairs+=1
                            assert ctx['units']==self.original
                            assert any(e['field']=='quantified' for e in ctx['checker_output_errors'])
                            assert self.calls==['write','fact_check','fact_check']
                            row['claims']=[{'kind':'guidance','scope':'none','factual_premise':False,'status':'supported','source_refs':[],'notes':'ordinary decision advice'}]
                return result
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'classification-repair',r['plan_rev'],r['plan_hash']);provider=V2(rid,attempt['id'])
        content.run_trial(rid,attempt['id'],lambda *_:provider)
        self.assertEqual(provider.repairs,1)
        self.assertIn('edit',provider.calls)
        self.assertEqual(store.latest(rid,'claim_ledger')['data']['checker_classification_version'],content.CHECKER_CLASSIFICATION_VERSION)
        self.assertEqual(store.latest(rid,'content_checker_error')['data']['error_kind'],'classification')

    def test_classification_failure_is_bounded_and_never_sent_to_editor(self):
        class V2(ProviderB):
            checker_contract_version='2.0'
            def call(self,node,skill,task,ctx):
                result=super().call(node,skill,task,ctx)
                if node in ('fact_check','fact_regression'):
                    for row in result['units']:
                        for claim in row['claims']:claim.pop('text')
                    result.update(checker_contract_version='2.0',content_revision=ctx['content_revision'])
                    result['units'][0]['claims']=[{'kind':'category','status':'supported','source_refs':[]}]
                return result
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'classification-stop',r['plan_rev'],r['plan_hash']);provider=V2(rid,attempt['id'])
        content.run_trial(rid,attempt['id'],lambda *_:provider)
        self.assertEqual(provider.calls,['write','fact_check','fact_check'])
        self.assertIn('不代表正文事实错误',store.get(rid)['error'])
        self.assertEqual(store.latest(rid,'content_checker_error')['data']['repair'],1)
        self.assertFalse(content.review_summary(content.attempt_artifacts(rid,attempt['id']))['fact_review_complete'])

    def test_classification_examples_preserve_real_performance_failures(self):
        def check(claim):content.validate_checker_classification({'units':[{'unit_id':'blog:b1','claims':[claim]}]})
        background={'kind':'background','scope':'category','status':'supported','source_refs':[],
                    'evidence_basis':'common_background','risk':'low','product_specific':False,'quantified':False}
        check(background)
        for field in ('evidence_basis','risk','product_specific','quantified'):
            broken=copy.deepcopy(background);broken.pop(field)
            with self.assertRaises(content.CheckerClassificationError):check(broken)
        with self.assertRaises(content.CheckerClassificationError):check({**background,'quantified':True})
        check({'kind':'guidance','scope':'none','factual_premise':False,'source_refs':[],'status':'supported'})
        with self.assertRaises(content.CheckerClassificationError):check({'kind':'guidance','scope':'category','factual_premise':True,'status':'supported'})
        absence={'kind':'input_absence','scope':'product','source_refs':[],'absent_pointers':['/product/facts/ironing'],'status':'unsupported'}
        with self.assertRaises(content.CheckerClassificationError):check(absence)
        check({**absence,'assertion_mode':'attribute_assertion','asserted_fact':'This shirt can safely be ironed.',
               'implicit_premise':'Permission is asserted despite missing product care support.'})
        ir=fake_ir('Shirt');sentence='This shirt dries overnight and can safely be ironed.'
        ir['pages'][0]['sections'][0]['blocks'][0]['text']=sentence
        rows=[{'unit_id':u['id'],'claims':[]} for u in content.units(ir)]
        rows[0]['claims']=[]
        next(r for r in rows if r['unit_id']=='blog:b1')['claims']=[{'text':sentence,'kind':'product','status':'unsupported','source_refs':[]}]
        result={'units':rows};content.validate_checker_classification(result)
        ledger=content.validate_ledger(result,ir,{'product_facts':[],'external_evidence':[]})
        self.assertTrue(content.actionable_claim(ledger['claims'][0]))

    def test_classification_repair_preserves_downstream_budget(self):
        class V2(ProviderB):
            checker_contract_version='2.0'
            def call(self,node,skill,task,ctx):
                result=super().call(node,skill,task,ctx)
                if node=='fact_check':
                    for row in result['units']:
                        for claim in row['claims']:claim.pop('text')
                    result.update(checker_contract_version='2.0',content_revision=ctx['content_revision'])
                    result['units'][0]['claims']=[{'kind':'background','status':'supported'}]
                    self.calls.extend(['previous-call']*(content.LIMITS['calls']-2-len(self.calls)))
                return result
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'classification-budget',r['plan_rev'],r['plan_hash']);provider=V2(rid,attempt['id'])
        content.run_trial(rid,attempt['id'],lambda *_:provider)
        self.assertEqual(provider.calls.count('fact_check'),1)
        self.assertNotIn('edit',provider.calls)
        self.assertIn('预算内修正',store.get(rid)['error'])

    def test_absence_paths_and_source_scope_are_checker_errors_not_article_edits(self):
        ctx={'task':{'product':{'facts':{'care':'cold wash','colors':['White'],'weight':0,'certified':False}}},
             'product_facts':[{'pointer':'/product/facts/care','value':'cold wash'}],
             'external_evidence':[{'evidence_id':'e1'}]}
        def check(claim):content.validate_checker_classification({'units':[{'unit_id':'blog:b1','claims':[claim]}]},ctx)
        absence={'kind':'input_absence','scope':'product','status':'supported','source_refs':[]}
        for path in ['/product/facts/care','/product/facts/colors/images','/product/facts/weight','/product/facts/certified']:
            with self.subTest(path=path),self.assertRaises(content.CheckerClassificationError) as caught:
                check({**absence,'absent_pointers':[path]})
            self.assertEqual(caught.exception.issues[0]['field'],'absent_pointers')
            self.assertEqual(caught.exception.issues[0]['unit_id'],'blog:b1')
        check({**absence,'absent_pointers':['/product/facts/drying_time']})
        category={'kind':'category','scope':'category','status':'supported','source_refs':['/product/facts/care']}
        with self.assertRaises(content.CheckerClassificationError):check(category)
        check({**category,'scope':'product'})
        check({**category,'source_refs':['e1']})
        with self.assertRaises(content.CheckerClassificationError):check({**category,'source_refs':['e-unknown']})
        check({'kind':'product','scope':'product','status':'unsupported','source_refs':[]})

    def test_editor_rejections_report_exact_field_and_current_base(self):
        ir=fake_ir('Shirt');text=ir['pages'][0]['sections'][0]['blocks'][0]['text']
        op={'unit_id':'blog:b1','before':'stale candidate text','after':'A useful replacement.','reason':'clarity'}
        revised,accepted,rejected=content.partition_edits(ir,[op],7)
        self.assertEqual(revised,ir);self.assertFalse(accepted)
        self.assertTrue(rejected[0]['reason'].startswith('before:'))
        self.assertEqual(rejected[0]['expected_before'],text);self.assertEqual(rejected[0]['base_revision'],7)
        op.update(before=text,before_refs=['wrong'],after_refs=[])
        _,_,rejected=content.partition_edits(ir,[op],7)
        self.assertTrue(rejected[0]['reason'].startswith('before_refs:'))
        self.assertEqual(rejected[0]['expected_before_refs'],[])

    def test_regression_failure_preserves_actual_reason_and_draft(self):
        class Failure(ProviderB):
            def call(self,node,skill,task,ctx):
                if node=='fact_regression':raise RuntimeError('observed token budget exhausted before regression')
                return super().call(node,skill,task,ctx)
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'regression-failure-reason',r['plan_rev'],r['plan_hash'])
        content.run_trial(rid,attempt['id'],Failure)
        self.assertIn('observed token budget',store.get(rid)['error'])
        self.assertNotIn('exc',store.get(rid)['error'])
        evaluation=store.latest(rid,'content_evaluation')['data']
        self.assertIn('observed token budget',next(c['reason'] for c in evaluation['checks'] if c['check']=='fact_review'))
        self.assertTrue(store.latest(rid,'content_pages')['data']['draft_only'])

    def test_checker_literal_alignment_preserves_verdicts_and_unique_locations(self):
        ir=fake_ir('Name');ir['pages'][0]['sections'][0]['blocks'][0]['text']='It can provide insulation when it has a double-wall design.'
        rows=[{'unit_id':u['id'],'claims':[]} for u in content.units(ir)]
        next(r for r in rows if r['unit_id']=='blog:b2')['claims']=[{'text':'it can provide insulation when it has a double-wall design','kind':'category','status':'unsupported','source_refs':[]}]
        aligned,changes,errors=content.align_checker_quotes({'units':rows},ir)
        self.assertFalse(errors);self.assertEqual(changes[0]['resolved_unit_id'],'blog:b1')
        claim=next(r for r in aligned['units'] if r['unit_id']=='blog:b1')['claims'][0]
        self.assertEqual(claim['status'],'unsupported');self.assertTrue(claim['text'].startswith('It'))
        next(r for r in rows if r['unit_id']=='blog:b2')['claims'][0]['text']='This product insulates well.'
        _,_,errors=content.align_checker_quotes({'units':rows},ir)
        self.assertEqual(len(errors),1)

    def test_checker_alignment_does_not_guess_between_multiple_matching_units(self):
        ir=fake_ir('Name');rows=[{'unit_id':u['id'],'claims':[]} for u in content.units(ir)]
        next(r for r in rows if r['unit_id']=='blog:meta_title')['claims']=[{'text':'Consider the intended use.','kind':'guidance','status':'supported','source_refs':[]}]
        _,_,errors=content.align_checker_quotes({'units':rows},ir)
        self.assertEqual(len(errors),1)

    def test_absence_disclosure_does_not_depend_on_fixed_english_phrasing(self):
        ir=fake_ir('Name');ctx={'product_facts':[], 'external_evidence':[], 'task':{'product':{'facts':{'lid':'screw-top lid'}}}}
        for sentence,path in [('The lid comes without seal detail.','/product/facts/seal'),('Nothing in the listing addresses refills.','/product/facts/refills'),('No double-wall construction appears in the specifications.','/product/facts/double_wall'),('The listed specifications do not cover insulation.','/product/facts/insulation')]:
            ir['pages'][0]['sections'][0]['blocks'][0]['text']=sentence
            rows=[{'unit_id':u['id'],'claims':[]} for u in content.units(ir)]
            claim={'text':sentence,'kind':'input_absence','scope':'product','status':'supported','source_refs':[],'absent_pointers':[path]}
            next(r for r in rows if r['unit_id']=='blog:b1')['claims']=[claim]
            self.assertEqual(content.validate_ledger({'units':rows},ir,ctx)['claims'][0]['status'],'supported')
            claim['absent_pointers']=['/product/facts/lid']
            self.assertEqual(content.validate_ledger({'units':rows},ir,ctx)['claims'][0]['status'],'uncertain')

    def test_misquoted_checker_output_gets_one_bounded_repair_without_editing_article(self):
        class Repairing(ProviderB):
            def __init__(self,*args):super().__init__(*args);self.repairs=0
            def call(self,node,skill,task,ctx):
                result=super().call(node,skill,task,ctx)
                if node=='fact_check':
                    if 'checker_output_to_repair' not in ctx:
                        next(r for r in result['units'] if r['claims'])['claims'][0]['text']='A paraphrase not in the article.'
                    else:self.repairs+=1
                return result
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'checker-repair',r['plan_rev'],r['plan_hash'])
        provider=Repairing(rid,attempt['id']);content.run_trial(rid,attempt['id'],lambda *_:provider)
        self.assertEqual(provider.repairs,1)
        self.assertIsNotNone(store.latest(rid,'content_checker_error'))
        self.assertIsNotNone(store.latest(rid,'claim_ledger'))
        self.assertNotIn('A paraphrase not in the article.',json.dumps(store.latest(rid,'content_ir')))

    def test_repeated_checker_misquote_stops_as_checker_failure_not_content_claim(self):
        class Broken(ProviderB):
            def call(self,node,skill,task,ctx):
                result=super().call(node,skill,task,ctx)
                if node=='fact_check':next(r for r in result['units'] if r['claims'])['claims'][0]['text']='Not actually in this draft.'
                return result
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'bad-checker',r['plan_rev'],r['plan_hash'])
        content.run_trial(rid,attempt['id'],Broken)
        self.assertIn('核查输出定位失败',store.get(rid)['error'])
        self.assertEqual(store.get(rid)['status'],'needs_review')
        self.assertFalse(any(m['kind']=='claim_ledger' for m in store.artifacts(rid)))

    def test_reference_only_correction_is_version_checked_and_still_regresses_claims(self):
        ir=fake_ir('Name');block=ir['pages'][0]['sections'][0]['blocks'][0];block['evidence_refs']=['e8']
        op={'unit_id':'blog:b1','before':block['text'],'after':block['text'],'reason':'Remove unrelated care citation',
            'before_refs':['e8'],'after_refs':[]}
        revised,ops,rejected=content.partition_edits(ir,[op],1)
        self.assertFalse(rejected);self.assertEqual(revised['pages'][0]['sections'][0]['blocks'][0]['evidence_refs'],[])
        self.assertEqual(ops[0]['before_refs'],['e8']);self.assertEqual(block['evidence_refs'],['e8'])
        _,_,rejected=content.partition_edits(ir,[{**op,'before_refs':['stale']}],1)
        self.assertEqual(len(rejected),1)
        ctx={'product_facts':[], 'external_evidence':[], 'unknowns':[]}
        bundle=content.check_bundle(revised,ctx,2);self.assertFalse(bundle['reference_errors'])
        rows=[{'unit_id':u['id'],'claims':[]} for u in content.units(revised)]
        next(row for row in rows if row['unit_id']=='blog:b1')['claims']=[{'text':block['text'],'kind':'product','status':'unsupported','source_refs':[]}]
        self.assertTrue(content.validate_ledger({'units':rows},revised,ctx)['claims'][0]['actionable'])
        ops[0]['new_revision']=2
        report=content.make_report(revised,{}, {'claims':[]},ops,[])
        self.assertIn('实际编辑对照仅 0 组',report)

    def test_editor_new_reference_must_resolve_in_current_snapshot(self):
        ir=fake_ir('Name');block=ir['pages'][0]['sections'][0]['blocks'][0]
        op={'unit_id':'blog:b1','before':block['text'],'after':block['text'],'reason':'citation change','before_refs':[],'after_refs':['outside-run']}
        revised,_,rejected=content.partition_edits(ir,[op],1);self.assertFalse(rejected)
        bundle=content.check_bundle(revised,{'product_facts':[],'external_evidence':[],'unknowns':[]},2)
        self.assertEqual(bundle['reference_errors'][0]['reference'],'outside-run')

    def test_common_background_is_explicit_and_never_bypasses_product_or_citation_risk(self):
        ir=fake_ir('Name');sentence='Drying speed varies by climate and garment, so build in a margin.'
        ir['pages'][0]['sections'][0]['blocks'][0]['text']=sentence
        ctx={'product_facts':[], 'external_evidence':[{'evidence_id':'e8','excerpt':'Machine wash cold.'}]}
        claim={'text':sentence,'kind':'background','scope':'category','status':'supported','source_refs':[],
               'evidence_basis':'common_background','risk':'low','product_specific':False,'quantified':False}
        def check():
            rows=[{'unit_id':u['id'],'claims':[copy.deepcopy(claim)] if u['id']=='blog:b1' else []} for u in content.units(ir)]
            return content.validate_ledger({'units':rows},ir,ctx)['claims'][0]
        self.assertEqual(check()['verification_basis'],'common_background_model_judgment')
        self.assertFalse(check()['actionable'])
        for key,value in [('product_specific',True),('quantified',True),('risk','high'),('source_refs',['e8'])]:
            old=claim[key];claim[key]=value
            self.assertEqual(check()['status'],'uncertain');self.assertTrue(check()['actionable']);claim[key]=old
        claim['status']='unsupported';self.assertTrue(check()['actionable'])

    def test_missing_disclosure_checks_absent_fields_and_preserves_zero_false(self):
        ir=fake_ir('Name');sentence='Dimensions and weight are not listed, so check pocket fit against your bag.'
        ir['pages'][0]['sections'][0]['blocks'][0]['text']=sentence
        ctx={'product_facts':[], 'external_evidence':[], 'task':{'product':{'facts':{}}}}
        claim={'text':sentence,'kind':'input_absence','scope':'product','status':'supported','source_refs':[],
               'absent_pointers':['/product/facts/dimensions','/product/facts/weight']}
        def check():
            rows=[{'unit_id':u['id'],'claims':[claim] if u['id']=='blog:b1' else []} for u in content.units(ir)]
            return content.validate_ledger({'units':rows},ir,ctx)['claims'][0]
        self.assertEqual(check()['verification_basis'],'input_absence_checked')
        self.assertEqual(check()['status'],'supported')
        for value in [0,False,42]:
            ctx['task']['product']['facts']['weight']=value
            self.assertEqual(check()['status'],'uncertain')
        ctx['task']['product']['facts'].clear();claim['absent_pointers']=['/invalid/weight']
        self.assertEqual(check()['status'],'uncertain')

    def test_legacy_evidence_view_separates_extraction_from_semantic_review(self):
        rid,r=self.approved();snapshot=content.approved_snapshot(rid,r['plan_rev'],r['plan_hash'])
        snapshot['evidence']={'sources':[{'source_id':'s1','status':'ok','content':'A bottle should be easy to clean.','url':'https://example.com'}],
            'evidence_items':[{'source_id':'s1','evidence_id':'e1','excerpt':'A bottle should be easy to clean.','status':'exact_excerpt_unverified'}]}
        view=content.build_context(snapshot)['external_evidence'][0]
        self.assertEqual(view['excerpt_match'],'exact');self.assertEqual(view['semantic_support'],'to_be_assessed')
        self.assertEqual(view['status'],'excerpt_located')
        self.assertEqual(snapshot['evidence']['evidence_items'][0]['status'],'exact_excerpt_unverified')

    def test_editor_role_exhaustion_does_not_enter_automatic_retry(self):
        class Exhausted(ProviderB):
            def __init__(self,*args):super().__init__(*args);self.role_calls={'edit':0};self.nodes=[]
            def call(self,node,skill,task,ctx):
                self.nodes.append(node)
                result=super().call(node,skill,task,ctx)
                if node=='edit':self.role_calls['edit']=content.LIMITS['editor_calls']
                if node=='fact_regression':
                    claim=next(row['claims'][0] for row in result['units'] if row['claims'])
                    claim['status']='unsupported'
                return result
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'exhausted-role',r['plan_rev'],r['plan_hash'])
        provider=Exhausted(rid,attempt['id']);content.run_trial(rid,attempt['id'],lambda *_:provider)
        self.assertNotIn('edit_retry',provider.nodes)
        self.assertEqual(store.latest(rid,'claim_ledger')['data']['phase'],'after_edit')
        self.assertEqual(store.get(rid)['status'],'needs_review')

    def test_editor_selected_previous_candidate_is_valid_base_only_when_current_matches(self):
        product=copy.deepcopy(LUMA)
        for kind in ('blog','product_page'):product['content_requests'][kind]['target_words']=[1,1000]
        rid,r=self.approved(product);ctx=content.build_context(content.approved_snapshot(rid,r['plan_rev'],r['plan_hash']))
        attempt,_=store.reserve_content(rid,'base-revision',r['plan_rev'],r['plan_hash']);ir=fake_ir('Name');observed=[]
        class Model:
            def __init__(self,**kwargs):pass
            def bind_tools(self,*args,**kwargs):return self
            def invoke(self,messages,**kwargs):
                observed.append(copy.deepcopy(messages))
                args={'operations':[{'unit_id':'blog:b1','before':'Consider the intended use.' if len(observed)==1 else 'Consider the intended setting.',
                    'after':'Consider the intended setting.' if len(observed)==1 else 'Consider your setting.','reason':'clarity'}],'submit_if_valid':True}
                if len(observed)>1:
                    args['base_draft_ref']=json.loads(next(m.content for m in reversed(messages) if isinstance(m,ToolMessage)))['draft_ref']
                return AIMessage(content='',tool_calls=[{'name':'word_count','args':args,'id':str(len(observed)),'type':'tool_call'}],usage_metadata={'input_tokens':10,'output_tokens':5,'total_tokens':15})
        with patch('geo_workbench.content.ChatDeepSeek',Model),patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test'}):
            provider=content.ModelProvider(rid,attempt['id'])
            first=provider.call('edit','editorial',content.EDITOR_TASK,{},tool_context={'ir':ir,'revision':1,'context':ctx})
            revised,_=content.edit_blocks(ir,first['operations'],1)
            second=provider.call('edit_retry','editorial',content.EDITOR_TASK,{},tool_context={'ir':revised,'revision':2,'context':ctx})
        self.assertEqual(second['operations'][0]['before'],'Consider the intended setting.')
        self.assertEqual(second['operations'][0]['after'],'Consider your setting.')
        self.assertEqual(observed[0],observed[1][:len(observed[0])])

    def test_editor_uses_four_initial_and_three_retry_turns_with_regression_reserved(self):
        product=copy.deepcopy(LUMA)
        for kind in ('blog','product_page'):product['content_requests'][kind]['target_words']=[1,1000]
        rid,r=self.approved(product);ctx=content.build_context(content.approved_snapshot(rid,r['plan_rev'],r['plan_hash']))
        attempt,_=store.reserve_content(rid,'seven-editor-turns',r['plan_rev'],r['plan_hash']);ir=fake_ir('Name');observed=[]
        class Model:
            def __init__(self,**kwargs):pass
            def bind_tools(self,*args,**kwargs):return self
            def invoke(self,messages,**kwargs):
                observed.append(copy.deepcopy(messages));turn=len(observed);usage={'input_tokens':10,'output_tokens':5,'total_tokens':15}
                if turn==8:return AIMessage(content='{"units":[]}',usage_metadata=usage)
                if turn not in (4,7):return AIMessage(content='{}',usage_metadata=usage)
                before='Consider the intended use.' if turn==4 else 'Consider the intended setting.'
                after='Consider the intended setting.' if turn==4 else 'Consider your setting.'
                return AIMessage(content='',tool_calls=[{'name':'word_count','args':{'operations':[{'unit_id':'blog:b1','before':before,'after':after,'reason':'clarity'}],'submit_if_valid':True},'id':str(turn),'type':'tool_call'}],usage_metadata=usage)
        with patch('geo_workbench.content.ChatDeepSeek',Model),patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test'}):
            provider=content.ModelProvider(rid,attempt['id'])
            first=provider.call('edit','editorial',content.EDITOR_TASK,{},tool_context={'ir':ir,'revision':1,'context':ctx})
            self.assertEqual(provider.role_calls['edit'],4)
            revised,_=content.edit_blocks(ir,first['operations'],1)
            provider.call('edit_retry','editorial',content.EDITOR_TASK,{},tool_context={'ir':revised,'revision':2,'context':ctx})
            self.assertEqual(provider.role_calls['edit'],7)
            self.assertEqual(provider.call('fact_regression','factcheck','test',{}),{'units':[]})
        restored=content.ModelProvider(rid,attempt['id'])
        self.assertEqual(restored.role_calls['edit'],7)
        self.assertEqual(restored.calls,8)

    def test_changed_current_content_rejects_old_base_even_with_reused_messages(self):
        product=copy.deepcopy(LUMA)
        for kind in ('blog','product_page'):product['content_requests'][kind]['target_words']=[1,1000]
        rid,r=self.approved(product);ctx=content.build_context(content.approved_snapshot(rid,r['plan_rev'],r['plan_hash']))
        attempt,_=store.reserve_content(rid,'stale-base',r['plan_rev'],r['plan_hash']);ir=fake_ir('Name');turns=[]
        class Model:
            def __init__(self,**kwargs):pass
            def bind_tools(self,*args,**kwargs):return self
            def invoke(self,messages,**kwargs):
                turns.append(1);args={'operations':[{'unit_id':'blog:b1','before':'Consider the intended use.' if len(turns)==1 else 'Consider the intended setting.',
                    'after':'Consider the intended setting.' if len(turns)==1 else 'Consider your setting.','reason':'clarity'}],'submit_if_valid':True}
                if len(turns)==2:args['base_draft_ref']=json.loads(next(m.content for m in reversed(messages) if isinstance(m,ToolMessage)))['draft_ref']
                return AIMessage(content='',tool_calls=[{'name':'word_count','args':args,'id':str(len(turns)),'type':'tool_call'}],usage_metadata={'input_tokens':10,'output_tokens':5,'total_tokens':15})
        with patch('geo_workbench.content.ChatDeepSeek',Model),patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test'}):
            provider=content.ModelProvider(rid,attempt['id'])
            first=provider.call('edit','editorial',content.EDITOR_TASK,{},tool_context={'ir':ir,'revision':1,'context':ctx})
            revised,_=content.edit_blocks(ir,first['operations'],1)
            revised['pages'][0]['sections'][0]['blocks'][1]['text']='Changed by a different confirmed revision.'
            provider.call('edit_retry','editorial',content.EDITOR_TASK,{},tool_context={'ir':revised,'revision':2,'context':ctx})
        errors=[e['detail'] for e in store.events(rid) if e['type']=='content_tool' and e['detail'].get('error')]
        self.assertEqual(len(errors),1);self.assertIn('stale',errors[0]['detail'])

    def setUp(self):
        self.worker=patch.dict('os.environ',{'GEO_WORKER_DISABLED':'1'});self.worker.start();self.addCleanup(self.worker.stop)
        self.tmp=tempfile.TemporaryDirectory();self.old=store.ROOT;store.ROOT=Path(self.tmp.name);store.init()
    def tearDown(self): store.ROOT=self.old;self.tmp.cleanup()
    def approved(self,product=LUMA):
        rid=store.create(copy.deepcopy(product),'normal');store.put(rid,'original_input',{'product':product,'user_query':'normal'})
        workflow.execute(rid,provider=ProviderA);r=store.get(rid)
        store.decisions(rid,'approve','plan_decision',{'action':'approve'},'awaiting_plan_approval',r['plan_rev'],r['plan_hash'])
        workflow.execute(rid,'resume',{'action':'approve'},provider=ProviderA)
        return rid,store.get(rid)
    def test_no_change_is_ignored_but_stale_or_conflicting_edits_are_rejected(self):
        ir=fake_ir('Name');before='Consider the intended use.'
        no_change={'unit_id':'blog:b1','before':before,'after':before,'reason':'keep'}
        stale={'unit_id':'blog:b2','before':'wrong','after':'new','reason':'edit'}
        revised,operations,rejected=content.partition_edits(ir,[no_change,stale],1)
        self.assertEqual(revised,ir);self.assertEqual(operations,[]);self.assertEqual(len(rejected),1);self.assertEqual(rejected[0]['unit_id'],'blog:b2')
        _,_,rejected=content.partition_edits(ir,[no_change,no_change],1)
        self.assertEqual(len(rejected),1)
    def test_input_interpretation_uses_product_source_not_category_source(self):
        ir=fake_ir('Name');text='Care instructions describe washing and drying, not drying time.'
        ir['pages'][0]['sections'][0]['blocks'][0]['text']=text
        ctx={'product_facts':[{'pointer':'/product/facts/care','value':'Gentle cold wash; air dry.'}],'external_evidence':[]}
        rows=[{'unit_id':u['id'],'claims':[]} for u in content.units(ir)]
        rows[[x['unit_id'] for x in rows].index('blog:b1')]['claims']=[{'text':text,'kind':'input_interpretation','source_refs':['/product/facts/care'],'status':'supported'}]
        claim=content.validate_ledger({'units':rows},ir,ctx)['claims'][0]
        self.assertEqual(claim['status'],'supported');self.assertEqual(claim['kind'],'product');self.assertEqual(claim['kind_original'],'input_interpretation')
        rows[[x['unit_id'] for x in rows].index('blog:b1')]['claims'][0]['kind']='category'
        self.assertEqual(content.validate_ledger({'units':rows},ir,ctx)['claims'][0]['status'],'uncertain')
        rows[[x['unit_id'] for x in rows].index('blog:b1')]['claims'][0]['scope']='product'
        product_scope=content.validate_ledger({'units':rows},ir,ctx)['claims'][0]
        self.assertEqual(product_scope['status'],'supported');self.assertEqual(product_scope['kind_original'],'category')
    def test_only_explicit_nonfactual_guidance_is_nonblocking(self):
        ir=fake_ir('Name');ctx={'product_facts':[],'external_evidence':[]}
        rows=[{'unit_id':u['id'],'claims':[]} for u in content.units(ir)]
        target=next(x for x in rows if x['unit_id']=='blog:b1')
        claim={'text':'Consider the intended use.','kind':'guidance','scope':'none','factual_premise':False,'source_refs':[],'status':'uncertain','notes':'ordinary advice'}
        target['claims']=[claim]
        def check():return content.validate_ledger({'units':rows},ir,ctx)['claims'][0]
        self.assertFalse(check()['actionable'])
        claim['status']='unsupported';self.assertTrue(check()['actionable'])
        claim['status']='uncertain';claim['factual_premise']=True;self.assertTrue(check()['actionable'])
        claim['factual_premise']=False;claim['source_refs']=['/invalid'];self.assertTrue(check()['actionable'])
        ctx['product_facts']=[{'pointer':'/product/facts/fit','value':'relaxed'}]
        claim['source_refs']=['/product/facts/fit'];self.assertFalse(check()['actionable'])
        claim['source_refs']=[];claim['text']='not present';self.assertTrue(check()['actionable'])
    def test_review_feedback_is_version_bound_and_runs_editor_then_regression(self):
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'first',r['plan_rev'],r['plan_hash'])
        content.run_trial(rid,attempt['id'],provider_factory=ProviderB)
        ledger=store.latest(rid,'claim_ledger')['data'];revision=ledger['content_revision']
        data={'parent_attempt':attempt['id'],'content_revision':revision,'message':'Keep conditional advice natural','idempotency_key':'review-1'}
        def execute(run_id,attempt_id,review):content.run_trial(run_id,attempt_id,provider_factory=ProviderB,review=review)
        with patch('geo_workbench.content.schedule_review',side_effect=execute),TestClient(app) as client:
            h={'x-geo-token':TOKEN};res=client.post(f'/api/runs/{rid}/content-review',headers=h,json=data)
            self.assertEqual(res.status_code,200,res.text);aid=res.json()['attempt_id']
            self.assertFalse(res.json()['duplicate']);self.assertEqual(client.post(f'/api/runs/{rid}/content-review',headers=h,json=data).json()['duplicate'],True)
            conflict={**data,'message':'different'};self.assertEqual(client.post(f'/api/runs/{rid}/content-review',headers=h,json=conflict).status_code,409)
            stale={**data,'idempotency_key':'review-2'};self.assertEqual(client.post(f'/api/runs/{rid}/content-review',headers=h,json=stale).status_code,409)
        events=[x['detail']['node'] for x in store.events(rid) if x['type']=='content_node' and x['detail'].get('attempt_id')==aid and x['detail'].get('status')=='started']
        self.assertEqual(events[:2],['edit','fact_regression']);self.assertNotIn('write',events)
        saved=[store.read_artifact(rid,x['id']) for x in store.artifacts(rid) if x['kind']=='content_review_feedback'][-1]
        self.assertEqual(saved['data']['history'],[data['message']]);self.assertEqual(store.get(rid)['product'],LUMA)
    def test_frozen_approval_and_cross_task_dependency(self):
        rid,r=self.approved();other,_=self.approved(ORBIT)
        snap=content.approved_snapshot(rid,r['plan_rev'],r['plan_hash']);self.assertEqual(snap['references']['plan']['digest'],r['plan_hash'])
        with self.assertRaises(KeyError): store.read_artifact(other,snap['references']['audience']['id'])
        with self.assertRaises(ValueError): content.approved_snapshot(rid,r['plan_rev'], 'bad')
        p=store.ROOT/'runs'/rid/'evidence';next(p.glob('*.json')).write_text('{}',encoding='utf-8')
        with self.assertRaises(ValueError): content.approved_snapshot(rid,r['plan_rev'],r['plan_hash'])
    def test_legacy_writing_view_omits_ambiguous_budgets_without_mutation(self):
        original={'blog_outline':{'sections':[{'h2':'Keep this heading','purpose':'Keep the message','word_budget':40}]},'product_outline':{'faq_ideas':[{'question':'Still here?','word_budget':20}]}}
        frozen=copy.deepcopy(original);snapshot={'plan':original,'references':{'plan':{'id':'source','digest':'hash'}}}
        view,adaptation=content.writing_plan_view(snapshot)
        self.assertEqual(original,frozen);self.assertEqual(view['blog_outline']['sections'][0]['h2'],'Keep this heading')
        self.assertNotIn('word_budget',view['blog_outline']['sections'][0]);self.assertNotIn('word_budget',view['product_outline']['faq_ideas'][0])
        self.assertEqual(len(adaptation['ignored_ambiguous_budgets']),2);self.assertEqual(adaptation['source_plan']['digest'],'hash')
        original['composition_contract_version']='1.0';view,adaptation=content.writing_plan_view(snapshot)
        self.assertEqual(view['blog_outline']['sections'][0]['word_budget'],40);self.assertFalse(adaptation['ignored_ambiguous_budgets'])
    def test_new_infeasible_plan_cannot_be_approved_or_downgraded(self):
        from test_topic_planning import plan_result
        class BadPlan(ProviderA):
            @staticmethod
            def plan(run_id,c,a,e):
                plan=plan_result();plan['schema_version']='2.0';plan['composition_contract_version']='1.0'
                for s in plan['product_outline']['sections']: s['visible_word_range']=[180,200]
                return plan
        rid=store.create(copy.deepcopy(LUMA),'normal');workflow.execute(rid,provider=BadPlan);r=store.get(rid)
        self.assertEqual(r['status'],'awaiting_plan_approval')
        with patch('geo_workbench.workflow.schedule'), TestClient(app) as client:
            h={'x-geo-token':TOKEN};payload={'action':'approve','revision':r['plan_rev'],'digest':r['plan_hash'],'idempotency_key':'approve-invalid'}
            response=client.post(f'/api/runs/{rid}/decision',headers=h,json=payload)
            self.assertEqual(response.status_code,409);self.assertIn('篇幅规划',response.json()['detail'])
            edited=store.latest(rid,'plan');edited.pop('composition_contract_version')
            response=client.post(f'/api/runs/{rid}/plan',headers=h,json={'revision':r['plan_rev'],'digest':r['plan_hash'],'plan':edited})
            self.assertEqual(response.status_code,200);self.assertEqual(store.latest(rid,'plan')['composition_contract_version'],'1.0')
    def test_start_is_idempotent_and_trial_cannot_pass(self):
        rid,r=self.approved()
        with patch('geo_workbench.content.schedule') as schedule, TestClient(app) as client:
            data={'revision':r['plan_rev'],'digest':r['plan_hash'],'idempotency_key':'same'}
            h={'x-geo-token':TOKEN}
            a=client.post(f'/api/runs/{rid}/content-start',headers=h,json=data);b=client.post(f'/api/runs/{rid}/content-start',headers=h,json=data)
            self.assertEqual(a.status_code,200);self.assertTrue(b.json()['duplicate']);self.assertEqual(schedule.call_count,1)
        attempt=a.json()['attempt_id'];content.run_trial(rid,attempt,ProviderB)
        self.assertEqual(store.get(rid)['status'],'needs_review')
        result=store.latest(rid,'content_evaluation')['data'];self.assertTrue(result['trial_only'])
        self.assertTrue(any(x['check']=='human_review' and x['status']=='unknown' for x in result['checks']))
        trace=store.latest(rid,'edit_trace')['data'];self.assertGreaterEqual(len(trace['operations']),2)
        self.assertNotEqual(trace['before_revision'],trace['after_revision'])
        with closing(sqlite3.connect(store.ROOT/'checkpoints.sqlite',check_same_thread=False)) as db:
            graph=content.build_trial_graph(rid,attempt,{},ProviderB(rid,attempt),{},SqliteSaver(db))
            snapshot=graph.get_state({'configurable':{'thread_id':'content:'+rid+':'+attempt}})
            self.assertEqual(snapshot.values['attempt_id'],attempt);self.assertFalse(snapshot.next)
            self.assertNotIn('Consider the intended use.',json.dumps(snapshot.values))
    def test_unsupported_injected_after_edit_stops_render(self):
        class Bad(ProviderB):
            def call(self,node,skill,task,ctx):
                if node in ('edit','edit_retry'):
                    item=ctx['editable_units'][0]
                    return {'operations':[{'unit_id':item['id'],'before':item['text'],'after':item['text']+' This shirt is wrinkle-free.','reason':'injected test error'}]}
                if node=='fact_regression':
                    rows=[{'unit_id':u['id'],'claims':[]} for u in ctx['units']]
                    index=next(i for i,u in enumerate(ctx['units']) if 'This shirt is wrinkle-free.' in u['text'])
                    rows[index]['claims']=[{'text':'This shirt is wrinkle-free.','kind':'product','source_ref':None,'status':'unsupported','notes':'injected'}]
                    return {'units':rows}
                return super().call(node,skill,task,ctx)
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'one',r['plan_rev'],r['plan_hash'])
        content.run_trial(rid,attempt['id'],Bad)
        self.assertEqual(store.get(rid)['status'],'needs_review');self.assertTrue(store.latest(rid,'content_pages')['data']['draft_only'])
        self.assertEqual(store.latest(rid,'claim_ledger')['data']['claims'][0]['status'],'unsupported')
    def test_editor_retry_never_bypasses_final_fact_gate(self):
        class Unresolved(ProviderB):
            def call(self,node,skill,task,ctx):
                if node in ('fact_check','fact_regression'):
                    result=super().call(node,skill,task,ctx)
                    claim=next(row['claims'][0] for row in result['units'] if row['claims'])
                    claim['status']='unsupported';claim['notes']='fixture remains unresolved'
                    return result
                return super().call(node,skill,task,ctx)
        product=copy.deepcopy(LUMA)
        for kind in ('blog','product_page'): product['content_requests'][kind]['target_words']=[1,1000]
        rid,r=self.approved(product);attempt,_=store.reserve_content(rid,'unresolved',r['plan_rev'],r['plan_hash']);content.run_trial(rid,attempt['id'],Unresolved)
        outputs=[]
        for meta in store.artifacts(rid):
            if meta['kind']=='content_model_output':
                v=store.read_artifact(rid,meta['id'])
                if v.get('attempt_id')==attempt['id']: outputs.append(v['data']['node'])
        self.assertEqual(outputs,['write','fact_check','edit','fact_regression','edit_retry','fact_regression'])
        self.assertEqual(store.get(rid)['status'],'needs_review')
        self.assertTrue(store.latest(rid,'content_pages')['data']['draft_only'])
        self.assertEqual(store.latest(rid,'claim_ledger')['data']['phase'],'after_edit_retry')
        self.assertEqual(len(store.latest(rid,'edit_trace')['data']['operations']),4)
        self.assertIn('unsupported',store.latest(rid,'content_report')['data']['markdown'])
    def test_bad_writer_reference_stops_before_edit(self):
        class BadRef(ProviderB):
            def call(self,node,skill,task,ctx):
                result=super().call(node,skill,task,ctx)
                if node=='write': result['pages'][0]['sections'][0]['blocks'][0]['evidence_refs']=['/wrong/item']
                return result
        product=copy.deepcopy(LUMA)
        for kind in ('blog','product_page'): product['content_requests'][kind]['target_words']=[1,1000]
        rid,r=self.approved(product);attempt,_=store.reserve_content(rid,'bad-ref',r['plan_rev'],r['plan_hash']);content.run_trial(rid,attempt['id'],BadRef)
        self.assertEqual(store.get(rid)['status'],'needs_review');self.assertIsNone(store.latest(rid,'edit_trace'))
        self.assertTrue(store.latest(rid,'content_pages')['data']['draft_only'])
    def test_editor_receives_actual_word_count_findings(self):
        class Capture(ProviderB):
            edit_context=None
            def call(self,node,skill,task,ctx):
                if node=='edit': Capture.edit_context=copy.deepcopy(ctx)
                return super().call(node,skill,task,ctx)
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'word-context',r['plan_rev'],r['plan_hash']);content.run_trial(rid,attempt['id'],Capture)
        findings=Capture.edit_context['page_word_goals']
        self.assertEqual({x['page'] for x in findings},{'blog','product_page'})
        self.assertTrue(all(x['words_to_add']>0 for x in findings))
        outputs=[store.read_artifact(rid,m['id'])['data']['node'] for m in store.artifacts(rid) if m['kind']=='content_model_output']
        self.assertNotIn('length_rewrite',outputs)
        checks=store.latest(rid,'content_evaluation')['data']['checks']
        self.assertTrue(any(x['check']=='word_count' and x['status']=='fail' for x in checks))
    def test_editor_receives_targets_even_when_both_pages_are_within_range(self):
        class Capture(ProviderB):
            goals=None
            def call(self,node,skill,task,ctx):
                if node=='edit': Capture.goals=copy.deepcopy(ctx['page_word_goals'])
                return super().call(node,skill,task,ctx)
        product=copy.deepcopy(LUMA)
        for kind in ('blog','product_page'): product['content_requests'][kind]['target_words']=[1,1000]
        rid,r=self.approved(product);attempt,_=store.reserve_content(rid,'word-goals',r['plan_rev'],r['plan_hash']);content.run_trial(rid,attempt['id'],Capture)
        self.assertEqual(len(Capture.goals),2);self.assertTrue(all(x['status']=='within' and x['headroom']>0 for x in Capture.goals))
    def test_one_invalid_editor_proposal_does_not_discard_valid_edits(self):
        class Mixed(ProviderB):
            def call(self,node,skill,task,ctx):
                result=super().call(node,skill,task,ctx)
                if node=='edit':
                    u=ctx['editable_units'][2]
                    result['operations'].append({'unit_id':u['id'],'before':u['text'],'after':u['text'],'reason':''})
                return result
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'mixed-edits',r['plan_rev'],r['plan_hash']);content.run_trial(rid,attempt['id'],Mixed)
        trace=store.latest(rid,'edit_trace')['data']
        self.assertGreaterEqual(len(trace['operations']),2);self.assertEqual(len(trace['rejected_operations']),1)
        self.assertIn(store.latest(rid,'claim_ledger')['data']['phase'],('after_edit','after_edit_retry'))
        checks=store.latest(rid,'content_evaluation')['data']['checks']
        self.assertEqual(next(c for c in checks if c['check']=='editor_rejected_operations')['status'],'unknown')
        self.assertIn('未采纳的编辑提案',store.latest(rid,'content_report')['data']['markdown'])
    def test_editor_keeps_both_pages_available_for_quality_edits(self):
        class Capture(ProviderB):
            ids=[]
            def call(self,node,skill,task,ctx):
                if node=='edit': Capture.ids=[u['id'] for u in ctx['editable_units']]
                return super().call(node,skill,task,ctx)
        product=copy.deepcopy(LUMA);product['content_requests']['blog']['target_words']=[1,1000];product['content_requests']['product_page']['target_words']=[1,10]
        rid,r=self.approved(product);attempt,_=store.reserve_content(rid,'focus',r['plan_rev'],r['plan_hash']);content.run_trial(rid,attempt['id'],Capture)
        self.assertTrue(any(x.startswith('blog:') for x in Capture.ids));self.assertTrue(any(x.startswith('product_page:') for x in Capture.ids))
    def test_ledger_requires_complete_coverage_and_real_source(self):
        ir=fake_ir('Name');context={'product_facts':[],'external_evidence':[]}
        with self.assertRaises(ValueError): content.validate_ledger({'units':[]},ir,context)
        rows=[{'unit_id':u['id'],'claims':[]} for u in content.units(ir)]
        rows[0]['claims']=[{'text':'Name','kind':'product','source_ref':'/invented','status':'supported'}]
        self.assertEqual(content.validate_ledger({'units':rows},ir,context)['claims'][0]['status'],'uncertain')
        rows[0]['claims']=[{'text':'A paraphrase absent from the unit','kind':'product','source_ref':'/product/name','status':'supported'}]
        checked=content.validate_ledger({'units':rows},ir,{'product_facts':[{'pointer':'/product/name','value':'Name'}],'external_evidence':[]})['claims'][0]
        self.assertEqual(checked['status'],'uncertain');self.assertEqual(checked['text'],'Name');self.assertEqual(checked['location_granularity'],'unit')
        rows[0]['claims']=[{'text':'Name','kind':'product','source_ref':'f1','status':'supported'}]
        checked=content.validate_ledger({'units':rows},ir,{'product_facts':[{'id':'f1','pointer':'/product/name','value':'Name'}],'external_evidence':[]})['claims'][0]
        self.assertEqual(checked['status'],'supported');self.assertEqual(checked['source_ref'],'/product/name');self.assertEqual(checked['source_ref_original'],'f1')
        rows[0]['claims'][0]['status']='support'
        checked=content.validate_ledger({'units':rows},ir,{'product_facts':[{'id':'f1','pointer':'/product/name','value':'Name'}],'external_evidence':[]})['claims'][0]
        self.assertEqual(checked['status'],'supported');self.assertEqual(checked['status_original'],'support')
        rows[0]['claims'][0]['status']='unrecognized'
        self.assertEqual(content.validate_ledger({'units':rows},ir,{'product_facts':[{'id':'f1','pointer':'/product/name','value':'Name'}],'external_evidence':[]})['claims'][0]['status'],'uncertain')
    def test_missing_fields_and_test_expectations_do_not_enter_writer(self):
        product=copy.deepcopy(LUMA);del product['product']['facts']['price'];del product['product']['facts']['care']
        rid,r=self.approved(product);ctx=content.build_context(content.approved_snapshot(rid,r['plan_rev'],r['plan_hash']))
        self.assertIn('/product/facts/price',ctx['unknowns']);self.assertIn('/product/facts/care',ctx['unknowns'])
        self.assertNotIn('price',ctx['task']['product']['facts']);self.assertNotIn('care',ctx['task']['product']['facts'])
        self.assertNotIn('test_scenarios',json.dumps(ctx));self.assertNotIn('unsupported_claims',json.dumps(ctx))
    def test_contradictory_price_is_corrected_by_editor_then_rechecked(self):
        class CorrectPrice(ProviderB):
            seen=[]
            def call(self,node,skill,task,ctx):
                if node=='write':
                    ir=fake_ir(ctx['task']['product']['name'])
                    for page in ir['pages']:
                        page['sections'][0]['blocks'][0].update(text='Price: $99 USD.',evidence_refs=['/product/facts/price/value','/product/facts/price/currency'])
                    return ir
                if node in ('fact_check','fact_regression'):
                    return {'units':[{'unit_id':u['id'],'claims':[{'text':u['text'],'kind':'product','source_refs':['/product/facts/price/value','/product/facts/price/currency'],'status':'supported'}] if u['text'].startswith('Price:') else []} for u in ctx['units']]}
                if node=='edit':
                    CorrectPrice.seen=ctx['review_findings']
                    return {'operations':[{'unit_id':u['id'],'before':u['text'],'after':'Price: $59 USD.','reason':'Correct to the supplied product price.'} for u in ctx['editable_units'] if u['text']=='Price: $99 USD.']}
                return super().call(node,skill,task,ctx)
        product=copy.deepcopy(LUMA)
        for kind in ('blog','product_page'): product['content_requests'][kind]['target_words']=[1,1000]
        rid,r=self.approved(product);attempt,_=store.reserve_content(rid,'correct-price',r['plan_rev'],r['plan_hash']);content.run_trial(rid,attempt['id'],CorrectPrice)
        self.assertTrue(any(c['status']=='contradicted' for c in CorrectPrice.seen))
        self.assertEqual(len(store.latest(rid,'edit_trace')['data']['operations']),2)
        ledger=store.latest(rid,'claim_ledger')['data'];self.assertEqual(ledger['phase'],'after_edit')
        self.assertTrue(all(c['status']=='supported' for c in ledger['claims']))
        self.assertNotIn('$99 USD',json.dumps(store.latest(rid,'content_pages')['data']['pages']))
    def test_missing_optional_price_and_care_can_complete_with_available_facts(self):
        product=copy.deepcopy(LUMA);del product['product']['facts']['price'];del product['product']['facts']['care']
        for kind in ('blog','product_page'): product['content_requests'][kind]['target_words']=[1,1000]
        rid,r=self.approved(product);attempt,_=store.reserve_content(rid,'missing-default',r['plan_rev'],r['plan_hash']);content.run_trial(rid,attempt['id'],ProviderB)
        pages=store.latest(rid,'content_pages')['data']
        self.assertTrue(store.latest(rid,'content_evaluation')['data']['render_complete'])
        self.assertTrue(pages['draft_only'])  # This small fixture still lacks the required five grounded claims.
        self.assertEqual(store.latest(rid,'claim_ledger')['data']['phase'],'after_edit')
        self.assertNotIn('offers',pages['pages']['product_page'])
        self.assertEqual(store.get(rid)['status'],'needs_review')
    def test_approved_scenarios_are_resolved_once_and_refs_are_bound(self):
        rid,r=self.approved();snap=content.approved_snapshot(rid,r['plan_rev'],r['plan_hash']);ctx=content.build_context(snap)
        pointer='/product/target_scenarios/1';self.assertIn(pointer,workflow.referenceable_product_paths(snap['task_contract']))
        ir=fake_ir('Name')
        for p in ir['pages']: p['sections'][0]['blocks'][0]['evidence_refs']=[pointer,pointer]
        bundle=content.check_bundle(ir,ctx,1)
        self.assertEqual(bundle['reference_pool'][pointer]['value'],'travel')
        self.assertEqual(bundle['reference_pool'][pointer]['semantic_type'],'merchant_scenario')
        self.assertEqual(bundle['unit_references']['blog:b1'],[pointer])
        self.assertEqual(bundle['snapshot']['references']['task_contract']['digest'],snap['references']['task_contract']['digest'])
        ir['pages'][1]['sections'][0]['blocks'][0]['evidence_refs']=['/product/target_scenarios/999']
        self.assertEqual(len(content.check_bundle(ir,ctx,1)['reference_errors']),1)
    def test_fact_id_aliases_resolve_to_current_snapshot_pointers(self):
        rid,r=self.approved();ctx=content.build_context(content.approved_snapshot(rid,r['plan_rev'],r['plan_hash']))
        fact=next(f for f in ctx['product_facts'] if f.get('id') and f['pointer']=='/product/facts/material')
        ir=fake_ir('Name');ir['pages'][0]['sections'][0]['blocks'][0]['evidence_refs']=[fact['id'],fact['pointer']]
        bundle=content.check_bundle(ir,ctx,1)
        self.assertEqual(bundle['unit_references']['blog:b1'],[fact['pointer']])
        self.assertEqual(bundle['original_unit_references']['blog:b1'],[fact['id'],fact['pointer']])
        self.assertEqual(bundle['reference_pool'][fact['pointer']]['value'],fact['value']);self.assertFalse(bundle['reference_errors'])
        ctx['product_facts'].append({'id':fact['id'],'pointer':'/product/facts/other','value':'other'})
        bundle=content.check_bundle(ir,ctx,1)
        self.assertEqual(len(bundle['reference_errors']),1);self.assertEqual(bundle['reference_errors'][0]['reference'],fact['id'])
    def test_empty_values_are_unknown_but_false_and_zero_remain_facts(self):
        product=copy.deepcopy(LUMA);product['product']['facts'].update(empty=None,empty_list=[],explicit_false=False,explicit_zero=0)
        rid,r=self.approved(product);ctx=content.build_context(content.approved_snapshot(rid,r['plan_rev'],r['plan_hash']))
        by_path={f['pointer']:f for f in ctx['product_facts']}
        self.assertEqual(by_path['/product/facts/empty']['status'],'unknown')
        self.assertEqual(by_path['/product/facts/empty_list']['status'],'unknown')
        self.assertEqual(by_path['/product/facts/explicit_false']['status'],'provided')
        self.assertEqual(by_path['/product/facts/explicit_zero']['status'],'provided')
        ir=fake_ir('Name');rows=[{'unit_id':u['id'],'claims':[]} for u in content.units(ir)]
        rows[0]['claims']=[{'text':'Name','kind':'product','source_ref':'/product/facts/empty','status':'supported'}]
        self.assertEqual(content.validate_ledger({'units':rows},ir,ctx)['claims'][0]['status'],'uncertain')
    def test_zero_claims_stops_before_edit_and_report_is_readable(self):
        class Empty(ProviderB):
            def call(self,node,skill,task,ctx):
                if node=='fact_check': return {'units':[{'unit_id':u['id'],'claims':[]} for u in ctx['units']]}
                return super().call(node,skill,task,ctx)
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'empty',r['plan_rev'],r['plan_hash']);content.run_trial(rid,attempt['id'],Empty)
        self.assertIsNone(store.latest(rid,'edit_trace'));self.assertEqual(store.get(rid)['status'],'needs_review')
        report=store.latest(rid,'content_report')['data']['markdown']
        self.assertNotIn('```json',report);self.assertLess(report.index('## 结果概览'),report.index('## 未解决问题'))
    def test_multiple_refs_and_deterministic_price_mismatch(self):
        ir=fake_ir('Name');ir['pages'][0]['sections'][0]['blocks'][0]['text']='100% linen, priced at $99.'
        ctx={'product_facts':[{'pointer':'/product/facts/material','value':'100% linen'},{'pointer':'/product/facts/price/value','value':59},{'pointer':'/product/facts/price/currency','value':'USD'}],'external_evidence':[]}
        rows=[{'unit_id':u['id'],'claims':[]} for u in content.units(ir)]
        next(x for x in rows if x['unit_id']=='blog:b1')['claims']=[{'text':'100% linen, priced at $99.','kind':'product','source_refs':['/product/facts/material','/product/facts/price/value','/product/facts/price/currency'],'status':'supported'}]
        result=content.validate_ledger({'units':rows},ir,ctx)['claims'][0]
        self.assertEqual(result['status'],'contradicted');self.assertEqual(len(result['resolved_sources']),3)
        ir['pages'][0]['sections'][0]['blocks'][0]['text']='It does not cost $99.'
        rows=[{'unit_id':u['id'],'claims':[]} for u in content.units(ir)]
        next(x for x in rows if x['unit_id']=='blog:b1')['claims']=[{'text':'It does not cost $99.','kind':'product','source_refs':['/product/facts/price/value','/product/facts/price/currency'],'status':'supported'}]
        result=content.validate_ledger({'units':rows},ir,ctx)['claims'][0]
        self.assertEqual(result['status'],'uncertain');self.assertEqual(result['deterministic_check'],'usd_price_needs_context')
    def test_category_claim_accepts_mixed_sources_but_product_requires_product_source(self):
        ir=fake_ir('Name');ctx={'product_facts':[{'pointer':'/product/target_scenarios/0','value':'travel'}],
                                'external_evidence':[{'evidence_id':'e1','excerpt':'A travel excerpt'}]}
        rows=[{'unit_id':u['id'],'claims':[]} for u in content.units(ir)]
        claim={'text':'Consider the intended use.','kind':'category','source_refs':['/product/target_scenarios/0','e1'],'status':'supported'}
        next(x for x in rows if x['unit_id']=='blog:b1')['claims']=[claim]
        result=content.validate_ledger({'units':rows},ir,ctx)['claims'][0]
        self.assertEqual(result['status'],'supported');self.assertEqual({x['type'] for x in result['resolved_sources']},{'product','category'})
        claim.update(kind='product',source_refs=['e1'])
        self.assertEqual(content.validate_ledger({'units':rows},ir,ctx)['claims'][0]['status'],'uncertain')
    def test_recreated_provider_does_not_reset_attempt_budget(self):
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'budget',r['plan_rev'],r['plan_hash'])
        for _ in range(content.LIMITS['calls']):
            store.event(rid,'content_model',{'attempt_id':attempt['id'],'status':'ok','usage':{'input_tokens':5,'output_tokens':3}})
        provider=content.ModelProvider(rid,attempt['id'])
        self.assertEqual(provider.calls,content.LIMITS['calls']);self.assertEqual(provider.tokens,8*content.LIMITS['calls'])
        with self.assertRaisesRegex(RuntimeError,'预算已耗尽'): provider.call('check','factcheck','test',{})
    def test_writer_and_editor_budgets_are_independent_and_restored(self):
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'separate-budget',r['plan_rev'],r['plan_hash'])
        for node in ['write']*4+['edit']*2:
            store.event(rid,'content_model',{'attempt_id':attempt['id'],'node':node,'status':'ok','usage':{'input_tokens':1,'output_tokens':1}})
        provider=content.ModelProvider(rid,attempt['id'])
        self.assertEqual(provider.role_calls,{'write':4,'edit':2})
        with self.assertRaisesRegex(RuntimeError,'预算不足'):provider.agent_call('write','writing','test',{})
        self.assertEqual(content.LIMITS['editor_calls']-provider.role_calls['edit'],5)
    def test_editor_can_refine_checked_candidate_without_losing_original_version(self):
        product=copy.deepcopy(LUMA)
        for kind in ('blog','product_page'):product['content_requests'][kind]['target_words']=[1,1000]
        rid,r=self.approved(product);ctx=content.build_context(content.approved_snapshot(rid,r['plan_rev'],r['plan_hash']));attempt,_=store.reserve_content(rid,'incremental',r['plan_rev'],r['plan_hash']);ir=fake_ir('Name');calls=[]
        class Model:
            def __init__(self,**kw):pass
            def bind_tools(self,tools,**kw):return self
            def invoke(self,messages,**kw):
                calls.append(1);usage={'input_tokens':10,'output_tokens':5,'total_tokens':15}
                if len(calls)==1:args={'operations':[{'unit_id':'blog:b1','before':'Consider the intended use.','after':'First draft.','reason':'clarity'}]}
                else:
                    ref=json.loads(next(m.content for m in reversed(messages) if isinstance(m,ToolMessage)))['draft_ref']
                    args={'base_draft_ref':ref,'operations':[{'unit_id':'blog:b1','before':'First draft.','after':'Final draft.','reason':'refine'}],'submit_if_valid':True}
                return AIMessage(content='',tool_calls=[{'name':'word_count','args':args,'id':str(len(calls)),'type':'tool_call'}],usage_metadata=usage)
        with patch('geo_workbench.content.ChatDeepSeek',Model),patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test','DEEPSEEK_BASE_URL':'https://api.deepseek.com'}):
            result=content.ModelProvider(rid,attempt['id']).call('edit','editorial','test',{},tool_context={'ir':ir,'revision':1,'context':ctx})
        self.assertEqual(result['operations'][0]['before'],'Consider the intended use.');self.assertEqual(result['operations'][0]['after'],'Final draft.')
    def test_writer_tool_loop_preserves_message_prefix_and_selects_checked_draft(self):
        product=copy.deepcopy(LUMA)
        for kind in ('blog','product_page'): product['content_requests'][kind]['target_words']=[1,1000]
        rid,r=self.approved(product);snap=content.approved_snapshot(rid,r['plan_rev'],r['plan_hash']);ctx=content.build_context(snap)
        attempt,_=store.reserve_content(rid,'writer-tool',r['plan_rev'],r['plan_hash']);draft=fake_ir('Name');observed=[]
        class Model:
            def __init__(self,**kwargs): pass
            def bind_tools(self,tools,**kwargs): return self
            def invoke(self,messages,**kwargs):
                observed.append(copy.deepcopy(messages))
                usage={'input_tokens':10,'output_tokens':5,'total_tokens':15}
                if len(observed)==1:
                    return AIMessage(content='',tool_calls=[{'name':'word_count','args':{'draft':draft},'id':'call-1','type':'tool_call'}],usage_metadata=usage)
                ref=json.loads(next(m.content for m in reversed(messages) if isinstance(m,ToolMessage)))['draft_ref']
                return AIMessage(content=json.dumps({'selected_draft_id':ref}),usage_metadata=usage)
        with patch('geo_workbench.content.ChatDeepSeek',Model),patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test','DEEPSEEK_BASE_URL':'https://api.deepseek.com'}):
            provider=content.ModelProvider(rid,attempt['id']);result=provider.call('write','writing',content.WRITER_TASK,ctx)
        self.assertEqual(len(result['pages']),2);self.assertEqual(provider.calls,2)
        self.assertEqual(observed[0],observed[1][:len(observed[0])])
        self.assertTrue(any(e['type']=='content_tool' and e['detail']['tool']=='word_count' for e in store.events(rid)))
    def test_editor_can_conditionally_submit_exact_counted_operations(self):
        product=copy.deepcopy(LUMA)
        for kind in ('blog','product_page'): product['content_requests'][kind]['target_words']=[1,1000]
        rid,r=self.approved(product);ctx=content.build_context(content.approved_snapshot(rid,r['plan_rev'],r['plan_hash']));attempt,_=store.reserve_content(rid,'editor-tool',r['plan_rev'],r['plan_hash'])
        ir=fake_ir('Name');ops=[{'unit_id':'blog:b1','before':'Consider the intended use.','after':'Consider the intended setting.','reason':'clarity'}]
        class Model:
            def __init__(self,**kwargs): pass
            def bind_tools(self,tools,**kwargs): return self
            def invoke(self,messages,**kwargs):
                return AIMessage(content='',tool_calls=[{'name':'word_count','args':{'operations':ops,'submit_if_valid':True},'id':'call-1','type':'tool_call'}],usage_metadata={'input_tokens':10,'output_tokens':5,'total_tokens':15})
        with patch('geo_workbench.content.ChatDeepSeek',Model),patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test','DEEPSEEK_BASE_URL':'https://api.deepseek.com'}):
            provider=content.ModelProvider(rid,attempt['id']);result=provider.call('edit','editorial',content.EDITOR_TASK,{},tool_context={'ir':ir,'revision':1,'context':ctx})
        self.assertEqual(result['operations'],ops);self.assertEqual(provider.calls,1)
        self.assertTrue(any(e['type']=='content_tool' and e['detail']['accepted'] for e in store.events(rid)))
    def test_role_specific_word_count_schemas_and_editor_out_of_range(self):
        product=copy.deepcopy(LUMA)
        for kind in ('blog','product_page'): product['content_requests'][kind]['target_words']=[500,1000]
        rid,r=self.approved(product);ctx=content.build_context(content.approved_snapshot(rid,r['plan_rev'],r['plan_hash']));ir=fake_ir('Name');schemas=[]
        ops=[{'unit_id':'blog:b1','before':'Consider the intended use.','after':'Consider the intended setting.','reason':'clarity'}]
        class Model:
            def __init__(self,**kwargs): pass
            def bind_tools(self,tools,**kwargs):
                schemas.append((tools[0].name,tools[0].args_schema.model_json_schema()));return self
            def invoke(self,messages,**kwargs):
                args={'draft':ir} if 'draft' in schemas[-1][1]['properties'] else {'operations':ops,'submit_if_valid':True}
                return AIMessage(content='',tool_calls=[{'name':'word_count','args':args,'id':'call','type':'tool_call'}],usage_metadata={'input_tokens':10,'output_tokens':5,'total_tokens':15})
        with patch('geo_workbench.content.ChatDeepSeek',Model),patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test','DEEPSEEK_BASE_URL':'https://api.deepseek.com'}):
            a,_=store.reserve_content(rid,'role-writer',r['plan_rev'],r['plan_hash'])
            content.ModelProvider(rid,a['id']).call('write','writing',content.WRITER_TASK,ctx)
            result=content.ModelProvider(rid,a['id']).call('edit','editorial',content.EDITOR_TASK,{},tool_context={'ir':ir,'revision':1,'context':ctx})
        self.assertEqual([x[0] for x in schemas],['word_count','word_count'])
        self.assertIn('draft',schemas[0][1]['required']);self.assertNotIn('operations',schemas[0][1]['properties'])
        self.assertIn('operations',schemas[1][1]['required']);self.assertNotIn('draft',schemas[1][1]['properties'])
        self.assertEqual(result['operations'],ops)
        selection=[e['detail'] for e in store.events(rid) if e['type']=='content_tool_selection' and e['detail']['node']=='edit'][-1]
        self.assertFalse(selection['all_goals_met'])
        revised,_=content.edit_blocks(ir,ops,1)
        self.assertTrue(any(p['status']!='within' for p in content.page_length_status(revised,ctx)))
    def test_invalid_tool_arguments_get_matching_tool_message(self):
        product=copy.deepcopy(LUMA)
        for kind in ('blog','product_page'): product['content_requests'][kind]['target_words']=[1,1000]
        rid,r=self.approved(product);ctx=content.build_context(content.approved_snapshot(rid,r['plan_rev'],r['plan_hash']));attempt,_=store.reserve_content(rid,'invalid-tool',r['plan_rev'],r['plan_hash']);draft=fake_ir('Name');observed=[]
        class Model:
            def __init__(self,**kwargs): pass
            def bind_tools(self,tools,**kwargs): return self
            def invoke(self,messages,**kwargs):
                observed.append(copy.deepcopy(messages));usage={'input_tokens':10,'output_tokens':5,'total_tokens':15}
                if len(observed)==1:
                    return AIMessage(content='',invalid_tool_calls=[{'name':'word_count','args':'{bad','id':'bad-1','error':'invalid JSON','type':'invalid_tool_call'}],usage_metadata=usage)
                return AIMessage(content='',tool_calls=[{'name':'word_count','args':{'draft':draft,'submit_if_valid':True},'id':'good-1','type':'tool_call'}],usage_metadata=usage)
        with patch('geo_workbench.content.ChatDeepSeek',Model),patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test','DEEPSEEK_BASE_URL':'https://api.deepseek.com'}):
            result=content.ModelProvider(rid,attempt['id']).call('write','writing',content.WRITER_TASK,ctx)
        self.assertEqual(len(result['pages']),2)
        self.assertTrue(any(isinstance(m,ToolMessage) and m.tool_call_id=='bad-1' for m in observed[1]))
    def test_writer_budget_fallback_keeps_last_resolved_draft_marked_unconfirmed(self):
        product=copy.deepcopy(LUMA)
        for kind in ('blog','product_page'): product['content_requests'][kind]['target_words']=[1,1000]
        rid,r=self.approved(product);ctx=content.build_context(content.approved_snapshot(rid,r['plan_rev'],r['plan_hash']));attempt,_=store.reserve_content(rid,'tool-fallback',r['plan_rev'],r['plan_hash']);draft=fake_ir('Name');calls=[]
        class Model:
            def __init__(self,**kwargs): pass
            def bind_tools(self,tools,**kwargs): return self
            def invoke(self,messages,**kwargs):
                calls.append(1);usage={'input_tokens':10,'output_tokens':5,'total_tokens':15}
                if len(calls)<3:return AIMessage(content='',tool_calls=[{'name':'word_count','args':{'draft':draft},'id':f'call-{len(calls)}','type':'tool_call'}],usage_metadata=usage)
                return AIMessage(content='',invalid_tool_calls=[{'name':'word_count','args':'{bad','id':'bad-1','error':'invalid JSON','type':'invalid_tool_call'}],usage_metadata=usage)
        with patch('geo_workbench.content.ChatDeepSeek',Model),patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test','DEEPSEEK_BASE_URL':'https://api.deepseek.com'}):
            provider=content.ModelProvider(rid,attempt['id']);result=provider.call('write','writing',content.WRITER_TASK,ctx)
        self.assertEqual(len(result['pages']),2);self.assertEqual(provider.calls,content.LIMITS['writer_calls'])
        check=content.writer_selection_check(rid,attempt['id']);self.assertEqual(check['status'],'unknown');self.assertIn('未经 Writer 最终确认',check['reason'])
        self.assertEqual(store.latest(rid,'content_tool_error')['data']['call_id'],'bad-1')
    def test_editor_retry_reuses_complete_message_prefix_when_available(self):
        product=copy.deepcopy(LUMA)
        for kind in ('blog','product_page'): product['content_requests'][kind]['target_words']=[1,1000]
        rid,r=self.approved(product);ctx=content.build_context(content.approved_snapshot(rid,r['plan_rev'],r['plan_hash']));attempt,_=store.reserve_content(rid,'cache-prefix',r['plan_rev'],r['plan_hash']);ir=fake_ir('Name');observed=[]
        first=[{'unit_id':'blog:b1','before':'Consider the intended use.','after':'Consider the intended setting.','reason':'clarity'}]
        second=[{'unit_id':'blog:b1','before':'Consider the intended setting.','after':'Consider your setting.','reason':'clarity'}]
        class Model:
            def __init__(self,**kwargs): pass
            def bind_tools(self,tools,**kwargs): return self
            def invoke(self,messages,**kwargs):
                observed.append(copy.deepcopy(messages));usage={'input_tokens':10,'output_tokens':5,'total_tokens':15}
                if len(observed)==1:return AIMessage(content='',tool_calls=[{'name':'word_count','args':{'operations':first},'id':'first','type':'tool_call'}],usage_metadata=usage)
                if len(observed)==2:
                    ref=json.loads(next(m.content for m in reversed(messages) if isinstance(m,ToolMessage)))['draft_ref']
                    return AIMessage(content=json.dumps({'selected_draft_id':ref}),usage_metadata=usage)
                return AIMessage(content='',tool_calls=[{'name':'word_count','args':{'operations':second,'submit_if_valid':True},'id':'second','type':'tool_call'}],usage_metadata=usage)
        with patch('geo_workbench.content.ChatDeepSeek',Model),patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test','DEEPSEEK_BASE_URL':'https://api.deepseek.com'}):
            provider=content.ModelProvider(rid,attempt['id']);result=provider.call('edit','editorial',content.EDITOR_TASK,{'round':1},tool_context={'ir':ir,'revision':1,'context':ctx})
            revised,_=content.edit_blocks(ir,result['operations'],1)
            provider.call('edit_retry','editorial',content.EDITOR_TASK,{'round':2},tool_context={'ir':revised,'revision':2,'context':ctx})
        self.assertEqual(observed[1],observed[2][:len(observed[1])])
        events=[e['detail'] for e in store.events(rid) if e['type']=='content_model']
        self.assertTrue(events[-1]['history_reused'])
    def test_conditional_editor_submit_keeps_tool_messages_for_retry(self):
        product=copy.deepcopy(LUMA)
        for kind in ('blog','product_page'): product['content_requests'][kind]['target_words']=[1,1000]
        rid,r=self.approved(product);ctx=content.build_context(content.approved_snapshot(rid,r['plan_rev'],r['plan_hash']));attempt,_=store.reserve_content(rid,'conditional-prefix',r['plan_rev'],r['plan_hash']);ir=fake_ir('Name');observed=[]
        first=[{'unit_id':'blog:b1','before':'Consider the intended use.','after':'Consider the intended setting.','reason':'clarity'}]
        second=[{'unit_id':'blog:b1','before':'Consider the intended setting.','after':'Consider your setting.','reason':'clarity'}]
        class Model:
            def __init__(self,**kwargs): pass
            def bind_tools(self,tools,**kwargs): return self
            def invoke(self,messages,**kwargs):
                observed.append(copy.deepcopy(messages));ops=first if len(observed)==1 else second
                return AIMessage(content='',tool_calls=[{'name':'word_count','args':{'operations':ops,'submit_if_valid':True},'id':f'call-{len(observed)}','type':'tool_call'}],usage_metadata={'input_tokens':10,'output_tokens':5,'total_tokens':15})
        with patch('geo_workbench.content.ChatDeepSeek',Model),patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test','DEEPSEEK_BASE_URL':'https://api.deepseek.com'}):
            provider=content.ModelProvider(rid,attempt['id']);result=provider.call('edit','editorial',content.EDITOR_TASK,{'round':1},tool_context={'ir':ir,'revision':1,'context':ctx})
            revised,_=content.edit_blocks(ir,result['operations'],1)
            provider.call('edit_retry','editorial',content.EDITOR_TASK,{'round':2},tool_context={'ir':revised,'revision':2,'context':ctx})
        self.assertEqual(observed[0],observed[1][:len(observed[0])])
        self.assertTrue(any(isinstance(m,ToolMessage) and m.tool_call_id=='call-1' for m in observed[1]))
    def test_invalid_model_json_is_preserved_and_never_accepted(self):
        class Model:
            def __init__(self,**kwargs): pass
            def invoke(self,*args,**kwargs):
                class Response:
                    content='{invalid';usage_metadata={'input_tokens':12,'output_tokens':3};response_metadata={}
                return Response()
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'invalid-json',r['plan_rev'],r['plan_hash'])
        with patch('geo_workbench.content.ChatDeepSeek',Model), patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test','DEEPSEEK_BASE_URL':'https://api.deepseek.com'}):
            with self.assertRaises(RuntimeError): content.ModelProvider(rid,attempt['id']).call('plain_probe','writing','task',{})
        invalid=store.latest(rid,'content_invalid_output')
        self.assertEqual(invalid['raw'],'{invalid');self.assertEqual(invalid['node'],'plain_probe')
        self.assertEqual(content.ModelProvider(rid,attempt['id']).calls,1)
    def test_fact_json_retry_preserves_prefix_and_counts_both_calls(self):
        observed=[]
        class Model:
            def __init__(self,**kwargs):pass
            def invoke(self,messages,**kwargs):
                observed.append(copy.deepcopy(messages))
                value='{invalid' if len(observed)==1 else '{"units":[]}'
                return AIMessage(content=value,usage_metadata={'input_tokens':12,'output_tokens':3,'total_tokens':15})
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'retry-json',r['plan_rev'],r['plan_hash'])
        with patch('geo_workbench.content.ChatDeepSeek',Model),patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test','DEEPSEEK_BASE_URL':'https://api.deepseek.com'}):
            provider=content.ModelProvider(rid,attempt['id']);result=provider.call('fact_regression','factcheck','return units',{})
        self.assertEqual(result,{'units':[]});self.assertEqual(provider.calls,2);self.assertEqual(provider.tokens,30)
        self.assertEqual(observed[0],observed[1][:2]);self.assertEqual(observed[1][2].content,'{invalid');self.assertIn('invalid JSON',observed[1][3].content)
        events=[x for x in store.events(rid) if x['type']=='content_model'];self.assertEqual([x['detail']['status'] for x in events],['failed','ok'])
        self.assertEqual(len([x for x in store.events(rid) if x['type']=='content_json_retry']),1)
    def test_fact_json_retry_respects_budget_and_never_accepts_invalid(self):
        class Model:
            def __init__(self,**kwargs):pass
            def invoke(self,*args,**kwargs):return AIMessage(content='{invalid',usage_metadata={'input_tokens':1,'output_tokens':1,'total_tokens':2})
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'retry-limited',r['plan_rev'],r['plan_hash'])
        for _ in range(content.LIMITS['calls']-1):store.event(rid,'content_model',{'attempt_id':attempt['id'],'node':'edit','usage':{'input_tokens':1,'output_tokens':1}})
        with patch('geo_workbench.content.ChatDeepSeek',Model),patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test','DEEPSEEK_BASE_URL':'https://api.deepseek.com'}):
            provider=content.ModelProvider(rid,attempt['id'])
            with self.assertRaises(RuntimeError):provider.call('fact_regression','factcheck','test',{})
        self.assertEqual(provider.calls,content.LIMITS['calls'])
    def test_missing_closing_brace_is_repaired_with_raw_trace(self):
        raw='{"units":[{"unit_id":"x","claims":[{"text":"quoted ] character"]}]}'
        fixed,positions=content.repair_missing_object_closures(raw)
        self.assertEqual(json.loads(fixed)['units'][0]['claims'][0]['text'],'quoted ] character')
        self.assertEqual(len(positions),1);self.assertEqual(len(fixed),len(raw)+1)
        with self.assertRaises(ValueError): content.repair_missing_object_closures('{"units":[1,2]}')
        class Model:
            def __init__(self,**kwargs): pass
            def invoke(self,*args,**kwargs):
                class Response:
                    content=raw;usage_metadata={'input_tokens':12,'output_tokens':3};response_metadata={}
                return Response()
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'repair-json',r['plan_rev'],r['plan_hash'])
        with patch('geo_workbench.content.ChatDeepSeek',Model), patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test','DEEPSEEK_BASE_URL':'https://api.deepseek.com'}):
            result=content.ModelProvider(rid,attempt['id']).call('check','factcheck','task',{})
        self.assertEqual(result['units'][0]['unit_id'],'x');self.assertEqual(store.latest(rid,'content_invalid_output')['raw'],raw)
        event=[e for e in store.events(rid) if e['type']=='content_model'][-1]
        self.assertEqual(event['detail']['json_repair']['kind'],'missing_object_closure')
    def test_report_distinguishes_original_and_claim_and_counts_supported(self):
        ir=fake_ir('Name');claim={'unit_id':'blog:b1','text':'intended use','kind':'product','status':'uncertain','resolved_sources':[{'ref':'/product/facts/type','type':'product','value':'shirt'}]}
        report=content.make_report(ir,{}, {'claims':[claim]},[],[])
        self.assertIn('**正文原文：** Consider the intended use.',report)
        self.assertIn('**产品主张（原文片段）：** intended use',report)
        self.assertIn('有商品资料来源且受支持的不同主张仅 0 条',report)
        self.assertLess(report.index('## 结果概览'),report.index('## 未解决问题'))
    def test_broad_fact_reference_does_not_automatically_reject_missing_disclosure(self):
        ir=fake_ir('Name');block=ir['pages'][0]['sections'][0]['blocks'][0]
        ctx={'product_facts':[{'pointer':'/product/facts','value':{'material':'linen'}}],'external_evidence':[]}
        def check(sentence):
            block['text']=sentence;rows=[{'unit_id':u['id'],'claims':[]} for u in content.units(ir)]
            next(x for x in rows if x['unit_id']=='blog:b1')['claims']=[{'text':sentence,'kind':'product','source_ref':'/product/facts','status':'supported'}]
            return content.validate_ledger({'units':rows},ir,ctx)['claims'][0]['status']
        self.assertEqual(check('Wrinkle performance is not provided.'),'supported')
        self.assertEqual(check('The shirt is wrinkle-free.'),'uncertain')
    def test_product_jsonld_contains_only_visible_supplied_attributes(self):
        ir=fake_ir('Name');ir['pages'][1]['sections'][0]['blocks'][0]['text']='LUMA: 100% linen in White.'
        ctx={'task':{'product':{'name':'Name','facts':{'material':'100% linen','colors':['White','Blue'],'price':{'value':59,'currency':'USD'}}},'brand':{'name':'LUMA'}}}
        pages=content.render(ir,ctx);parser=content.PageInspector();parser.feed(pages['product_page']);schema=json.loads(parser.scripts[0])
        self.assertEqual(schema['material'],'100% linen');self.assertEqual(schema['color'],'White');self.assertNotIn('offers',schema)
        self.assertEqual(next(c for c in content.inspect_pages(pages,ctx) if c['check']=='jsonld_mapping_visible')['status'],'pass')
    def test_unapproved_and_unsupported_input_cannot_start(self):
        rid=store.create(copy.deepcopy(LUMA),'write wrinkle-free');store.put(rid,'original_input',{'product':LUMA,'user_query':'write wrinkle-free'})
        workflow.execute(rid,provider=ProviderA);r=store.get(rid)
        self.assertEqual(r['status'],'awaiting_input_resolution')
        with self.assertRaises(ValueError): store.reserve_content(rid,'early',r['plan_rev'],r['plan_hash'])
        self.assertIsNone(store.latest(rid,'content_ir'))
        resolution={'action':'retry','user_query':'use confirmed facts'}
        store.decisions(rid,'resolve','input_resolution',resolution,'awaiting_input_resolution')
        workflow.execute(rid,'resume',resolution,provider=ProviderA);r=store.get(rid)
        store.decisions(rid,'approve','plan_decision',{'action':'approve'},'awaiting_plan_approval',r['plan_rev'],r['plan_hash'])
        workflow.execute(rid,'resume',{'action':'approve'},provider=ProviderA);r=store.get(rid)
        attempt,_=store.reserve_content(rid,'after-resolution',r['plan_rev'],r['plan_hash']);content.run_trial(rid,attempt['id'],ProviderB)
        self.assertNotIn('wrinkle-free',json.dumps(store.latest(rid,'content_pages')['data']).lower())
    def test_restart_marks_running_trial_needs_review_without_replay(self):
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'one',r['plan_rev'],r['plan_hash'])
        with patch('geo_workbench.content.schedule') as schedule, TestClient(app): pass
        self.assertEqual(store.get(rid)['status'],'needs_review')
        self.assertEqual(store.content_attempt(rid,attempt['id'])['status'],'needs_review')
        self.assertEqual(schedule.call_count,0)
    def test_invalid_checker_stops_without_final_pages(self):
        class Broken(ProviderB):
            def call(self,node,skill,task,ctx):
                if node=='fact_check': return {'units':[]}
                return super().call(node,skill,task,ctx)
        rid,r=self.approved();attempt,_=store.reserve_content(rid,'one',r['plan_rev'],r['plan_hash'])
        content.run_trial(rid,attempt['id'],Broken)
        self.assertEqual(store.get(rid)['status'],'needs_review');self.assertTrue(store.latest(rid,'content_pages')['data']['draft_only'])
        self.assertIsNotNone(store.latest(rid,'content_ir'))
    def test_trial_supports_both_categories_with_same_implementation(self):
        for product in (LUMA,ORBIT):
            rid,r=self.approved(product);attempt,_=store.reserve_content(rid,'one',r['plan_rev'],r['plan_hash'])
            content.run_trial(rid,attempt['id'],ProviderB)
            pages=store.latest(rid,'content_pages')['data']['pages']
            self.assertIn(product['product']['name'],pages['product_page'])
            self.assertEqual(store.get(rid)['status'],'needs_review')
    def test_attempt_history_can_be_retrieved_without_cross_task_leak(self):
        rid,r=self.approved();a,_=store.reserve_content(rid,'one',r['plan_rev'],r['plan_hash']);content.run_trial(rid,a['id'],ProviderB)
        b,_=store.reserve_content(rid,'two',r['plan_rev'],r['plan_hash']);content.run_trial(rid,b['id'],ProviderB)
        other,_=self.approved(ORBIT)
        with TestClient(app) as client:
            first=client.get(f'/api/runs/{rid}/content-attempts/{a["id"]}')
            self.assertEqual(first.status_code,200);self.assertEqual(first.json()['artifacts']['content_pages']['attempt_id'],a['id'])
            self.assertEqual(client.get(f'/api/runs/{other}/content-attempts/{a["id"]}').status_code,404)
    def test_model_text_does_not_escape_jsonld_script(self):
        ir=fake_ir('</script><script>alert(1)</script>')
        rendered=content.render(ir,{'task':{'product':{'name':'</script><script>alert(1)</script>'}}})
        self.assertNotIn('</script><script>alert(1)',rendered['product_page'])
        checks=content.inspect_pages(rendered,{'task':{'product':{'name':'</script><script>alert(1)</script>'}}})
        self.assertTrue(all(x['status']=='pass' for x in checks if x['check']!='jsonld_platform_eligibility'))
        self.assertTrue(all(x['status']=='unknown' for x in checks if x['check']=='jsonld_platform_eligibility'))
    def test_verbatim_overlap_is_reported_for_manual_review(self):
        ir=fake_ir('Name');ir['pages'][0]['sections'][0]['blocks'][0]['text']='One two three four five six seven eight nine.'
        ctx={'task':{'product':{'name':'Name'},'content_requests':{'blog':{'target_words':[1,1000]},'product_page':{'target_words':[1,1000]}}},
             'external_evidence':[{'evidence_id':'e1','excerpt':'One two three four five six seven eight nine.'}]}
        checks=content.structure_checks(ir,ctx,content.render(ir,ctx))
        overlap=next(c for c in checks if c['check']=='source_text_overlap')
        self.assertEqual(overlap['status'],'unknown');self.assertEqual(overlap['matches'][0]['evidence_id'],'e1')
    def test_edit_rejects_stale_or_duplicate_text(self):
        ir=fake_ir('Name');op={'unit_id':'blog:b1','before':'wrong','after':'other','reason':'clarity'}
        with self.assertRaises(ValueError): content.edit_blocks(ir,[op],1)
        op['before']='Consider the intended use.'
        with self.assertRaises(ValueError): content.edit_blocks(ir,[op,op],1)
        self.assertEqual(ir['pages'][0]['sections'][0]['blocks'][0]['text'],'Consider the intended use.')
    def test_faq_units_can_be_edited_with_exact_original_text(self):
        ir=fake_ir('Name');op={'unit_id':'product_page:f0:answer','before':'Consider your needs.','after':'Consider your priorities.','reason':'clarity'}
        edited,operations=content.edit_blocks(ir,[op],1)
        self.assertEqual(edited['pages'][1]['faqs'][0]['answer'],'Consider your priorities.')
        self.assertEqual(ir['pages'][1]['faqs'][0]['answer'],'Consider your needs.')
        self.assertEqual(len(operations),1)
        op.update(unit_id='product_page:f0:question',before='Question 0?',after='Different question?')
        edited,operations=content.edit_blocks(ir,[op],1)
        self.assertEqual(edited['pages'][1]['faqs'][0]['question'],'Different question?')
        op['before']='stale question?'
        with self.assertRaises(ValueError): content.edit_blocks(ir,[op],1)
    def test_duplicate_model_ids_are_safely_scoped(self):
        ir=fake_ir('Name');ir['pages'][0]['sections'].append({'id':'s2','heading':'More','blocks':[{'id':'b1','text':'Other text.'}]})
        checked=content.validate_ir(ir);first=checked['pages'][0]['sections'][0]['blocks'][0];other=checked['pages'][0]['sections'][1]['blocks'][0]
        self.assertNotEqual(first['id'],other['id']);self.assertEqual(other['model_original_id'],'b1');self.assertEqual(other['text'],'Other text.')
    def test_render_escapes_model_text_and_preserves_local_link(self):
        ir=fake_ir('Name');ir['pages'][0]['sections'][0]['blocks'][0]['text']='<img src=x onerror=alert(1)>'
        result=content.render(ir,{'task':{'product':{'name':'Name'}}})
        self.assertIn('&lt;img',result['blog']);self.assertNotIn('<img',result['blog']);self.assertIn('href="product.html"',result['blog']);self.assertIn('UNREVIEWED TRIAL DRAFT',result['blog'])

if __name__=='__main__': unittest.main()
