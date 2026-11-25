import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from geo_workbench import composition, research, store, workflow

ROOT=Path(__file__).resolve().parents[1]

def fixture(name):
    return json.loads((ROOT/'test-data'/name).read_text(encoding='utf-8'))

def plan_result():
    def outline(kind):
        section=[220,310] if kind=='blog' else [50,80];faq=[45,60] if kind=='blog' else [35,55]
        return {'h1':'Guide','meta_title':'Title','meta_description':'Description',
                'sections':[{'h2':'Section '+str(i),'purpose':'Explain','expression_strategy':'Direct explanation','visible_word_range':section[:]} for i in range(2)],
                'faq_ideas':[{'question':'Question '+str(i)+'?','answer_direction':'Answer the decision','expression_strategy':'Direct answer','visible_word_range':faq[:]} for i in range(3)]}
    return {'blog_outline':outline('blog'),'product_outline':outline('product'),'question_map':[], 'competitor_analysis':[],'evidence_gaps':[],
        'editorial_strategy':{'topic':'untrusted','reader_goal':'choose','narrative_throughline':'scenario to decision','selected_questions':[{'question_id':'q1','reason':'decision impact'}],'blog_pdp_roles':'guide and product','product_link_rationale':'after choices'},
        'writing_brief':{'product_fact_pointers':[],'evidence_refs':[],'unknowns':[],'prohibited_claims':[],'page_requirements':{}}}

class TopicPlanning(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=store.ROOT;store.ROOT=Path(self.tmp.name);store.init()
    def tearDown(self): store.ROOT=self.old;self.tmp.cleanup()

    def test_distinct_topics_use_same_contract_and_no_fixed_template(self):
        observed=[]
        def fake(run,node,skill,context,thinking='disabled',messages=None):
            observed.append(copy.deepcopy(context));return plan_result()
        with patch.object(research,'model_json',side_effect=fake):
            for name in ('luma-normal.json','orbit-normal.json'):
                product=fixture(name);contract,issues=workflow.contract(product,'normal');self.assertFalse(issues)
                rid=store.create(product,'normal')
                result=research.plan(rid,contract,{'question_map':[]},{'sources':[],'limitations':[]})
                self.assertEqual(result['schema_version'],'2.0')
                self.assertEqual(result['editorial_strategy']['topic'],product['content_requests']['blog']['topic'])
        self.assertNotEqual(observed[0]['contract']['product']['category'],observed[1]['contract']['product']['category'])
        self.assertNotEqual(observed[0]['contract']['content_requests']['blog']['topic'],observed[1]['contract']['content_requests']['blog']['topic'])
        self.assertIn('not market search frequency',observed[0]['instruction'])

    def test_composition_feasibility_counts_fixed_and_unit_scopes(self):
        contract,_=workflow.contract(fixture('luma-normal.json'),'normal');plan=plan_result();plan['composition_contract_version']=composition.CONTRACT_VERSION
        self.assertEqual(composition.plan_warnings(plan,contract),[])
        plan['product_outline']['sections'][0]['visible_word_range']=[1,80]
        self.assertEqual(composition.plan_warnings(plan,contract),[])
        for section in plan['product_outline']['sections']: section['visible_word_range']=[160,170]
        warnings=composition.plan_warnings(plan,contract)
        self.assertTrue(any(x['page']=='product_outline' and x['field']=='composition_budget' for x in warnings))
        plan['product_outline']['sections'][0]['visible_word_range']=[True,170]
        self.assertTrue(any('正整数' in x['reason'] for x in composition.plan_warnings(plan,contract)))
        plan.pop('composition_contract_version')
        self.assertEqual(composition.plan_warnings(plan,contract),[])

    def test_execution_diagnostics_uses_unique_exact_matches_only(self):
        plan=plan_result();page={'page_type':'blog','sections':[{'id':'s1','heading':'Section 0','blocks':[{'text':'one two'}]},
                 {'id':'s2','heading':'Unplanned heading','blocks':[{'text':'three'}]}],
                 'faqs':[{'id':'f1','question':'Question 0?','answer':'an answer'}]}
        result=composition.execution_diagnostics(plan,{'pages':[page]})['pages']['blog']
        self.assertEqual(len(result['matched']),2);self.assertEqual(len(result['unmatched']),1)
        self.assertEqual(result['matched'][0]['actual'],4)
        plan['blog_outline']['sections'].append(copy.deepcopy(plan['blog_outline']['sections'][0]))
        result=composition.execution_diagnostics(plan,{'pages':[page]})['pages']['blog']
        self.assertFalse(any(x['id']=='s1' for x in result['matched']))

    def test_planner_revises_infeasible_plan_once_and_preserves_candidate(self):
        product=fixture('luma-normal.json');contract,_=workflow.contract(product,'normal');rid=store.create(product,'normal')
        bad=plan_result()
        for section in bad['product_outline']['sections']: section['visible_word_range']=[160,170]
        with patch.object(research,'model_json',side_effect=[bad,plan_result()]) as model:
            result=research.plan(rid,contract,{'question_map':[]},{'sources':[],'limitations':[]})
        self.assertEqual(model.call_count,2);self.assertEqual(result['composition_warnings'],[])
        self.assertTrue(store.latest(rid,'plan_candidate')['warnings'])
        self.assertEqual(model.call_args_list[1].args[1],'plan_composition_revision')
    def test_planner_stops_retrying_when_revision_remains_infeasible(self):
        product=fixture('luma-normal.json');contract,_=workflow.contract(product,'normal');rid=store.create(product,'normal')
        bad=plan_result()
        for s in bad['product_outline']['sections']: s['visible_word_range']=[180,200]
        with patch.object(research,'model_json',side_effect=[copy.deepcopy(bad),copy.deepcopy(bad)]) as model:
            result=research.plan(rid,contract,{'question_map':[]},{'sources':[],'limitations':[]})
        self.assertEqual(model.call_count,2);self.assertTrue(result['composition_warnings'])
        self.assertTrue(workflow.planning_shape_warnings(result,contract))

    def test_missing_topic_blocked_and_legacy_still_readable(self):
        product=fixture('luma-normal.json');del product['content_requests']['blog']['topic']
        _,issues=workflow.contract(product,'normal');self.assertTrue(any('blog.topic' in x for x in issues))
        rid=store.create(product,'normal');workflow.write_report(rid,{}, {}, {'sources':[]}, {'question_map':[]})
        self.assertIn('历史版本',store.latest(rid,'research_report'))

    def test_candidate_diversity_and_possible_duplicates(self):
        rows=[{'url':'https://one.example/a?utm_source=x'},{'url':'https://one.example/a?utm_source=y'},
              {'url':'https://one.example/b'},{'url':'https://two.example/c'},{'url':'http://127.0.0.1/private'}]
        chosen=research.diversified_candidates(rows)
        self.assertEqual([x['url'] for x in chosen],['https://one.example/a?utm_source=x','https://two.example/c','https://one.example/b'])
        sources=[{'source_id':'s1','url':'https://one.example/a','status':'ok','content':'one two three four five six seven'},
                 {'source_id':'s2','url':'https://two.example/c','status':'ok','content':'one two three four five six seven'},
                 {'source_id':'s3','url':'https://one.example/b','status':'ok','content':'unrelated translation different language'}]
        research.annotate_sources(sources)
        self.assertEqual(sources[1]['possible_duplicate_of'],'s1')
        self.assertIsNone(sources[2]['possible_duplicate_of'])

    def test_plan_references_are_checked_without_semantic_claim(self):
        contract,_=workflow.contract(fixture('luma-normal.json'),'normal');plan=plan_result()
        plan['writing_brief']['product_fact_pointers']=['/product/facts/material','/product/facts/imaginary']
        plan['writing_brief']['evidence_refs']=['e1','e9']
        plan['editorial_strategy']['selected_questions']=[{'question_id':'q1','reason':'x'},{'question_id':'missing','reason':'y'}]
        warnings=workflow.brief_warnings(plan,contract,{'question_map':[{'question_id':'q1'}]},{'evidence_items':[{'evidence_id':'e1'}]})
        self.assertEqual({w['reference'] for w in warnings},{'/product/facts/imaginary','e9','missing'})
        with self.assertRaises(ValueError): research.validate_plan_structure({'editorial_strategy':{},'writing_brief':{}})

    def test_planning_shape_warnings_surface_missing_meta_and_faq(self):
        data={'blog_outline':{'h1':'Guide','meta_description':'Description','faq_ideas':[{},{}]},
              'product_outline':{'h1':'Item','meta':{'title':'Title','description':'Description'},'faq_ideas':[{},{},{}]}}
        warnings=workflow.planning_shape_warnings(data)
        self.assertEqual({(x['page'],x['field']) for x in warnings},{('blog_outline','meta_title'),('blog_outline','faq')})

if __name__=='__main__': unittest.main()
