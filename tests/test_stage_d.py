import io,json,zipfile
import unittest
from unittest.mock import patch
from PIL import Image,ImageDraw
from fastapi.testclient import TestClient
import test_stage_b as b
from geo_workbench import images,store,content,queue,ownership
from geo_workbench.app import app,TOKEN

class FakeImages:
    def __init__(self,reviews=None):self.prepares=[];self.generations=[];self.reviews=[];self.verdicts=reviews or ['pass']
    def prepare(self,ctx,history,reference=None):
        self.prepares.append({'context':ctx.copy(),'history':list(history),'reference':bool(reference)})
        return {'layers':{k:'Illustrate '+ctx['product']['name']+'; restrained editorial image, honor current requirements' for k in images.LAYER_KEYS[ctx['page_role']]},'caption_en':'An AI visual illustration.','alt':'Product illustration'}, {'model':'test-fixture','usage':{}}
    def generate(self,request):
        self.generations.append(request.copy());size=tuple(map(int,request['size'].split('x')))
        im=Image.new('RGBA',size,(0,0,0,0) if request['background']=='transparent' else (235,238,240,255))
        ImageDraw.Draw(im).rectangle((size[0]//3,50,size[0]*2//3,size[1]-50),fill=(80,120,180,255))
        out=io.BytesIO();im.save(out,format='PNG');return out.getvalue(),{'model':'test-fixture','usage':{'generated_images':1}}
    def review(self,ctx,history,raw):
        self.reviews.append(raw);n=min(len(self.reviews)-1,len(self.verdicts)-1)
        return {'status':self.verdicts[n],'issues':[],'instruction':'Preserve product, improve framing'}, {'model':'test-fixture','usage':{}}

class StageD(unittest.TestCase):
    setUp=b.StageB.setUp
    tearDown=b.StageB.tearDown
    approved=b.StageB.approved
    complete_fixture=b.StageB.complete_fixture

    def test_blog_scene_focus_preserves_current_source_scope_without_invention(self):
        source={'product':{'target_scenarios':['commute'],'skus':[{'target_scenarios':['sport']}]},
                'content_requests':{'blog':{'topic':'A buying guide'}},'target_audience':'shoppers'}
        result=images.blog_scene_focus(source)
        self.assertEqual(result['target_scenarios'],[{'value':['commute'],'pointer':'/product/target_scenarios'}])
        self.assertEqual(result['topic'],'A buying guide');self.assertEqual(result['target_audience'],'shoppers')
        result['target_scenarios'][0]['value'].append('travel')
        self.assertEqual(source['product']['target_scenarios'],['commute'])
        empty=images.blog_scene_focus({'product':{'facts':{}}})
        self.assertEqual(empty['target_scenarios'],[]);self.assertIsNone(empty['topic'])

    def test_two_candidates_have_independent_budgets_references_and_page_choice(self):
        rid,_=self.complete_fixture();binding,_=images.snapshot(rid,'blog')
        store.put(rid,'image_preferences',{'binding':binding,'requirements':'No hotel scenes.'})
        first=self.submit(rid,key='with',candidate_key='with_user',message='Use the actual beneficiary.')
        p=FakeImages(['revise']);images.process(first['id'],p)
        a=images.view(rid,'blog')['candidates'][0]['assets'][-1]
        images.select(rid,'blog',a['id'])
        second=self.submit(rid,key='without',candidate_key='without_user')
        q=FakeImages(['revise']);images.process(second['id'],q)
        d=images.view(rid,'blog');other=d['candidates'][1]['assets'][-1]
        self.assertEqual(d['session']['selected_asset'],a['id'])
        self.assertEqual([c['session']['auto_calls'] for c in d['candidates']],[3,3])
        self.assertEqual(len(p.generations)+len(q.generations),6)
        self.assertFalse(q.prepares[0]['history']);self.assertFalse(q.prepares[0]['reference'])
        self.assertIn('beneficiary',p.prepares[0]['context']['candidate_direction'])
        self.assertEqual(q.prepares[0]['context']['user_visual_requirements'],'No hotel scenes.')
        self.assertTrue(all(g['watermark'] is False for g in p.generations+q.generations))
        for variant in ('with_user','without_user'):
            with self.assertRaises(ValueError):self.submit(rid,key=variant+'extra',candidate_key=variant,retry=True)
        with self.assertRaises(ValueError):self.submit(rid,key='cross-variant',mode='user_edit',candidate_key='with_user',reference=other['id'],message='Change light')
        edit=self.submit(rid,key='edit-other',mode='user_edit',candidate_key='without_user',reference=other['id'],message='Soften light')
        edited=FakeImages();images.process(edit['id'],edited)
        self.assertEqual(len(edited.generations),1);self.assertFalse(edited.reviews)
        self.assertEqual(len(edited.prepares[0]['history']),3)
        self.assertEqual(edited.generations[0]['image'],'data:image/png;base64,'+images.asset(rid,other['id'])['raw_png'])
        self.assertEqual(images.view(rid,'blog')['session']['selected_asset'],a['id'])
        release=images.confirm(rid,'blog',other['id'],binding['attempt_id'],binding['text_revision'],'test_runner')
        self.assertEqual(images.view(rid,'blog')['release']['id'],release)
        with TestClient(app) as client:
            r=client.get(f'/api/runs/{rid}/page-images/blog/delivery.html')
            self.assertEqual(r.status_code,200);self.assertIn('attachment',r.headers['content-disposition'])
            self.assertIn(images.asset(rid,other['id'])['display_png'],r.text)
            self.assertEqual(client.get(f'/api/runs/{rid}/image-delivery.zip').status_code,409)
        pdp=self.submit(rid,role='product_page',key='pdp');images.process(pdp['id'],FakeImages())
        pd=images.view(rid,'product_page');images.confirm(rid,'product_page',pd['session']['selected_asset'],binding['attempt_id'],binding['text_revision'],'test_runner')
        with zipfile.ZipFile(io.BytesIO(images.export_zip(rid))) as archive:
            versions=json.loads(archive.read('generation-record.json'))[0]['versions']
            self.assertEqual(len(versions),7)
            self.assertEqual({v['candidate_key'] for v in versions},{'with_user','without_user'})
            self.assertTrue(all(v['file'] in archive.namelist() for v in versions))

    def test_single_candidate_schema_upgrade_preserves_choice_and_budget(self):
        rid,_=self.complete_fixture();j=self.submit(rid);images.process(j['id'],FakeImages())
        before=images.view(rid,'blog');selected=before['session']['selected_asset']
        with store.connect() as db:
            db.execute('DROP TABLE image_page_choices')
            db.execute('ALTER TABLE image_sessions DROP COLUMN candidate_key')
        images.init();images.init()
        after=images.view(rid,'blog')
        self.assertEqual(after['session']['selected_asset'],selected)
        self.assertEqual(after['session']['candidate_key'],'legacy')
        self.assertEqual(after['session']['auto_calls'],1)
        self.assertEqual(after['assets'][0]['id'],before['assets'][0]['id'])

    def test_candidate_http_contract_and_badge_bottom_right(self):
        rid,_=self.complete_fixture();binding,_=images.snapshot(rid,'blog')
        with TestClient(app) as client:
            payload={'attempt_id':binding['attempt_id'],'revision':binding['text_revision'],'idempotency_key':'http-with','candidate_key':'with_user'}
            r=client.post(f'/api/runs/{rid}/page-images/blog',json=payload,headers={'X-Geo-Token':TOKEN})
            self.assertEqual(r.status_code,200)
            j=images.job(r.json()['job_id']);self.assertEqual(images.session(j['session_id'])['candidate_key'],'with_user')
            self.assertTrue(client.post(f'/api/runs/{rid}/page-images/blog',json=payload,headers={'X-Geo-Token':TOKEN}).json()['duplicate'])
        im=Image.new('RGB',(1280,720),'white');out=io.BytesIO();im.save(out,format='PNG')
        marked=Image.open(io.BytesIO(images.badge(out.getvalue()))).convert('RGB')
        self.assertEqual(marked.crop((0,600,300,720)).getextrema(),((255,255),(255,255),(255,255)))
        self.assertLess(marked.crop((900,600,1280,720)).getextrema()[0][0],255)

    def test_page_specific_prompt_contract_and_shared_style(self):
        rid,_=self.complete_fixture()
        contexts=[images.snapshot(rid,r)[1] for r in ('blog','product_page')]
        self.assertEqual(contexts[0]['shared_visual_style'],contexts[1]['shared_visual_style'])
        for ctx in contexts:
            plan,_=FakeImages().prepare(ctx,[])
            prompt=images.prompt_from(plan,ctx['page_role'])
            self.assertIn('Everyday Linen Shirt',prompt)
            other='product_page' if ctx['page_role']=='blog' else 'blog'
            with self.assertRaises(ValueError):images.prompt_from(plan,other)
            plan['layers']['marketing_analysis']='Do not render this'
            with self.assertRaises(ValueError):images.prompt_from(plan,ctx['page_role'])

    def test_agent_uses_low_thinking_and_role_specific_instruction(self):
        with patch.dict('os.environ',{'DEEPSEEK_API_KEY':'test','DEEPSEEK_BASE_URL':'https://api.deepseek.com'}),patch('geo_workbench.images.httpx.Client') as factory:
            response=factory.return_value.__enter__.return_value.post.return_value
            response.status_code=200
            response.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':'{}'}}],'model':'test','usage':{}}
            for role in ('blog','product_page'):
                _,usage=images.ImageAgent().prepare({'page_role':role,'mode':'auto_initial','user_instruction':''},[])
                request=factory.return_value.__enter__.return_value.post.call_args.kwargs['json']
                self.assertEqual(request['thinking'],{'type':'enabled'})
                self.assertEqual(request['reasoning_effort'],'low');self.assertEqual(usage['reasoning_effort'],'low')
                instruction=json.loads(request['messages'][-1]['content'][0]['text'])['task']
                self.assertTrue(all(k in instruction for k in images.LAYER_KEYS[role]))

    def test_visual_color_options_deduplicate_size_variants_without_global_fact_promotion(self):
        product={'facts':{'colors':['White']},'skus':[{'id':'blue-s','facts':{'color':'Light Blue','size':'S'}},
                   {'id':'blue-m','facts':{'color':'light blue','size':'M'}},{'id':'white-l','facts':{'color':'White','size':'L'}}]}
        options=images.visual_colors(product)
        self.assertEqual([o['name'] for o in options],['White','Light Blue'])
        self.assertTrue(all(s['scope']=='sku' for s in options[1]['sources']))
        self.assertEqual(product['facts'],{'colors':['White']})
        self.assertEqual(images.visual_colors({'facts':{}}),[])

    def test_english_metadata_fallback_does_not_change_prompt_or_invoke_review(self):
        plan={'layers':{'subject':'A layered shirt composition'},'caption_en':'浅蓝衬衫在前，白衬衫在后','alt':'两种颜色'}
        result=images.english_metadata(plan,{'product':{'name':'Everyday Linen Shirt'},'page_role':'product_page'})
        self.assertEqual(result['layers'],plan['layers']);self.assertIn('AI visual illustration',result['caption_en'])
        self.assertEqual(result['metadata_fallback']['original']['caption_en'],plan['caption_en'])
        self.assertEqual(plan['caption_en'],'浅蓝衬衫在前，白衬衫在后')

    def submit(self,rid,role='blog',key='initial',**kw):
        binding,_=images.snapshot(rid,role)
        return images.submit(rid,role,binding['attempt_id'],binding['text_revision'],key,**kw)[0]

    def test_automatic_agent_receives_current_product_and_honors_three_call_limit(self):
        rid,_=self.complete_fixture();j=self.submit(rid);p=FakeImages(['revise'])
        images.process(j['id'],p);d=images.view(rid,'blog')
        self.assertEqual(len(p.generations),3);self.assertEqual(len(p.reviews),3)
        self.assertEqual(d['session']['auto_calls'],3);self.assertEqual(len(d['assets']),3)
        self.assertFalse(p.prepares[0]['reference']);self.assertTrue(p.prepares[1]['reference'])
        self.assertEqual(p.prepares[0]['context']['product']['name'],'Everyday Linen Shirt')
        self.assertEqual(d['assets'][-1]['status'],'needs_review')
        with self.assertRaises(ValueError):self.submit(rid,key='extra-auto',retry=True)
        self.assertEqual(store.get(rid)['status'],'completed')

    def test_user_edit_retains_reference_and_history_but_never_reviews(self):
        rid,_=self.complete_fixture();j=self.submit(rid,role='product_page');p=FakeImages();images.process(j['id'],p)
        d=images.view(rid,'product_page');ref=d['assets'][-1]['id']
        j=self.submit(rid,role='product_page',key='edit',mode='user_edit',message='Keep the product, soften the light.',reference=ref)
        edited=FakeImages();images.process(j['id'],edited);d=images.view(rid,'product_page');a=d['assets'][-1]
        self.assertEqual(len(edited.generations),1);self.assertFalse(edited.reviews)
        self.assertTrue(edited.prepares[0]['reference']);self.assertTrue(edited.prepares[0]['history'])
        self.assertEqual(a['visual_review']['status'],'not_run_user_edit');self.assertEqual(a['parent_asset'],ref)
        self.assertEqual(a['user_quote'],'Keep the product, soften the light.')
        self.assertTrue(a['file_checks']['alpha_present'])
        raw=images.base64.b64decode(images.asset(rid,a['id'])['raw_png'])
        labeled=images.base64.b64decode(images.asset(rid,a['id'])['display_png'])
        self.assertNotEqual(raw,labeled)

    def test_duplicate_request_and_cross_page_reference_are_rejected(self):
        rid,_=self.complete_fixture();j=self.submit(rid);duplicate=self.submit(rid)
        self.assertEqual(j['id'],duplicate['id']);images.process(j['id'],FakeImages());a=images.view(rid,'blog')['assets'][0]
        with self.assertRaises(ValueError):self.submit(rid,key='initial',message='different')
        with self.assertRaises(ValueError):self.submit(rid,role='product_page',key='cross',mode='user_edit',message='change',reference=a['id'])
        other,_=self.complete_fixture()
        with self.assertRaises(KeyError):images.asset(other,a['id'])

    def test_joint_release_and_zip_use_selected_assets_and_stale_text_is_denied(self):
        rid,attempt=self.complete_fixture()
        for role in ('blog','product_page'):
            j=self.submit(rid,role,key=role);images.process(j['id'],FakeImages());d=images.view(rid,role)
            aid=d['session']['selected_asset'];binding=d['binding']
            first=images.confirm(rid,role,aid,binding['attempt_id'],binding['text_revision'],'test_runner')
            self.assertEqual(first,images.confirm(rid,role,aid,binding['attempt_id'],binding['text_revision'],'test_runner'))
        with zipfile.ZipFile(io.BytesIO(images.export_zip(rid))) as z:
            self.assertIn('images/blog.png',z.namelist());self.assertIn('images/product_page.png',z.namelist())
            self.assertIn(b'images/blog.png',z.read('blog.html'));self.assertNotIn(b'data:image',z.read('blog.html'))
            self.assertIn(b'product.html',z.read('blog.html'))
            manifest=json.loads(z.read('generation-record.json'))
            self.assertTrue(manifest[0]['versions'])
            self.assertIn(manifest[0]['versions'][0]['file'],z.namelist())
        r=store.get(rid);store.reserve_content(rid,'new-text',r['plan_rev'],r['plan_hash'])
        with self.assertRaises(ValueError):images.page_html(rid,'product_page',aid)
        with self.assertRaises(ValueError):images.export_zip(rid)

    def test_unknown_visual_result_and_generation_failure_never_fake_pass(self):
        rid,_=self.complete_fixture();j=self.submit(rid);p=FakeImages(['unknown']);images.process(j['id'],p)
        self.assertEqual(len(p.generations),1);self.assertEqual(images.view(rid,'blog')['assets'][0]['visual_review']['status'],'unknown')
        class Broken(FakeImages):
            def generate(self,request):raise RuntimeError('provider unavailable')
        j=self.submit(rid,'product_page',key='broken');images.process(j['id'],Broken())
        self.assertEqual(images.job(j['id'])['status'],'failed');self.assertEqual(store.get(rid)['status'],'completed')

    def test_interrupted_job_is_not_replayed_and_alpha_is_real(self):
        rid,_=self.complete_fixture();j=self.submit(rid)
        with store.connect() as db:db.execute("UPDATE image_jobs SET status='running' WHERE id=?",(j['id'],))
        images.recover();p=FakeImages();images.process(j['id'],p)
        self.assertEqual(images.job(j['id'])['status'],'interrupted');self.assertFalse(p.generations)
        raw,_=FakeImages().generate({'size':'1024x1024','background':'opaque'})
        self.assertFalse(images.png_checks(raw,{'size':'1024x1024','background':'transparent'})['pass'])

    def test_visual_service_failure_retains_paid_image_without_automatic_retry(self):
        class Unreadable(FakeImages):
            def review(self,*args):raise RuntimeError('vision unavailable')
        rid,_=self.complete_fixture();j=self.submit(rid);p=Unreadable();images.process(j['id'],p)
        d=images.view(rid,'blog');self.assertEqual(len(p.generations),1)
        self.assertEqual(d['assets'][0]['visual_review']['status'],'unknown')
        self.assertEqual(d['assets'][0]['status'],'needs_review')
        self.assertTrue(images.artifacts_for(rid,d['session']['id'],'image_generated'))
        self.assertIsNone(d['release'])

    def test_seedream_rejection_reports_cause_without_credentials_or_account_id(self):
        with patch.dict('os.environ',{'SEEDREAM_API_KEY':'test_secret'},clear=True),patch('geo_workbench.images.httpx.Client') as factory:
            response=factory.return_value.__enter__.return_value.post.return_value
            response.status_code=429
            response.json.return_value={'error':{'code':'SetLimitExceeded','message':'account [1234567] reached limit test_secret'}}
            with self.assertRaises(RuntimeError) as caught:images.ImageAgent().generate({'model':images.MODEL})
        self.assertIn('SetLimitExceeded',str(caught.exception));self.assertNotIn('1234567',str(caught.exception));self.assertNotIn('test_secret',str(caught.exception))

    def test_http_assets_preview_csp_and_no_user_auto_confirmation(self):
        rid,_=self.complete_fixture();j=self.submit(rid);images.process(j['id'],FakeImages());d=images.view(rid,'blog');aid=d['assets'][0]['id']
        self.assertIsNone(d['release'])
        with TestClient(app) as client:
            self.assertEqual(client.get('/images.js').status_code,200)
            self.assertEqual(client.get(f'/api/runs/{rid}/image-assets/{aid}.png').headers['content-type'],'image/png')
            r=client.get(f'/api/runs/{rid}/page-images/blog/image-preview',params={'asset_id':aid})
            self.assertEqual(r.status_code,200);self.assertIn('sandbox',r.headers['content-security-policy'])
            self.assertIn('data:image/png;base64',r.text)
            self.assertEqual(client.get(f'/api/runs/{rid}/image-delivery.zip').status_code,409)
            self.assertEqual(client.post(f'/api/runs/{rid}/page-images/blog/select',json={'asset_id':aid}).status_code,403)
