"""Stage D: one image agent per page session, durable requests and joint releases."""
from __future__ import annotations
import base64, copy, hashlib, html, io, json, os, re, time, uuid, zipfile
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from PIL import Image, ImageDraw, ImageFont, ImageFilter
from geo_workbench import store, content, research

MODEL='doubao-seedream-5-5-261028'
PROMPT_SCHEMA='page-image-v2'
LAYER_KEYS={
    'blog':('visual_concept','product_presence','setting','composition','visual_style','constraints'),
    'product_page':('product_identity','variant_arrangement','view_and_framing','background','lighting_and_finish','constraints'),
}

def shared_visual_style(brand,style):
    """Same source-bound direction for both pages; no extra planning/model call."""
    return {'brand':copy.deepcopy(brand),'product_style':copy.deepcopy(style),
            'fallback':'Natural editorial photography; restrained neutral palette, soft directional light, realistic material texture and consistent color rendering. Use only where supplied brand/product direction leaves a gap; product direction wins conflicts.'}
MAX_IMAGE_BYTES=30*1024*1024

def candidate_direction(key):
    return {
        'with_user':'Show the actual product user or beneficiary interacting naturally with the product in the selected article-supported scenario. Infer the user from current facts, not a fixed human/gender template: e.g. a dog for a dog product, not automatically its human buyer. Keep the product prominent. Do not fabricate demographic, species or suitability claims.',
        'without_user':'Show only the product and meaningful environment. No actual user/beneficiary, people, animals, body parts, hands or silhouettes. Express a recognizable usage scene through staging, not token props.',
        'legacy':'Follow the current page purpose and explicit user requirements.',
    }[key]

def blog_scene_focus(snapshot):
    """Highlight current explicit creative context, never infer facts or SKU scope."""
    product=snapshot.get('product',{})
    sources=[]
    for value,pointer in ((product.get('target_scenarios'),'/product/target_scenarios'),
                          (snapshot.get('target_scenarios'),'/target_scenarios')):
        if value:sources.append({'value':copy.deepcopy(value),'pointer':pointer})
    return {'topic':copy.deepcopy(snapshot.get('content_requests',{}).get('blog',{}).get('topic')),
            'topic_pointer':'/content_requests/blog/topic',
            'target_audience':copy.deepcopy(snapshot.get('target_audience')),
            'audience_pointer':'/target_audience','target_scenarios':sources,
            'interpretation':'Creative-use context, not evidence of performance. Prefer product-scoped direction if conflicting. Choose one article-supported scenario for reader relevance, natural product prominence and visual impact. If absent, identify a scenario internally from the actual article; do not fabricate source fields.'}

def visual_colors(product):
    """Deduplicate supplied colorways, retaining scope; never expand sizes or SKUs."""
    options={}
    def add(facts,prefix,sku_id=None):
        for field in ('colors','color'):
            value=facts.get(field);values=value if isinstance(value,list) else [value]
            for index,name in enumerate(values):
                if not isinstance(name,str) or not name.strip():continue
                label=' '.join(name.split());key=label.casefold()
                option=options.setdefault(key,{'name':label,'sources':[]})
                option['sources'].append({'pointer':prefix+'/'+field+(('/'+str(index)) if isinstance(value,list) else ''),
                                          'scope':'sku' if sku_id else 'product','sku_id':sku_id})
    add(product.get('facts',{}),'/product/facts')
    for index,sku in enumerate(product.get('skus',[])):
        if isinstance(sku,dict):add(sku.get('facts',{}),'/product/skus/'+str(index)+'/facts',sku.get('id'))
    return list(options.values())

def init():
    with store.connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS image_sessions(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,page_role TEXT NOT NULL,
          binding TEXT NOT NULL,config TEXT NOT NULL,auto_calls INTEGER NOT NULL DEFAULT 0,selected_asset TEXT,
          created TEXT NOT NULL,updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS image_jobs(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,run_id TEXT NOT NULL,
          mode TEXT NOT NULL,payload TEXT NOT NULL,status TEXT NOT NULL,error TEXT,created TEXT NOT NULL,updated TEXT NOT NULL,
          idempotency_key TEXT NOT NULL,UNIQUE(run_id,idempotency_key));
        CREATE TABLE IF NOT EXISTS image_page_choices(run_id TEXT NOT NULL,page_role TEXT NOT NULL,binding TEXT NOT NULL,
          asset_id TEXT NOT NULL,updated TEXT NOT NULL,PRIMARY KEY(run_id,page_role,binding));
        ''')
        if 'candidate_key' not in [r['name'] for r in db.execute('PRAGMA table_info(image_sessions)')]:
            db.execute("ALTER TABLE image_sessions ADD COLUMN candidate_key TEXT NOT NULL DEFAULT 'legacy'")
        # Preserve existing previews when upgrading from the single-candidate schema.
        for row in db.execute('SELECT * FROM image_sessions WHERE selected_asset IS NOT NULL ORDER BY created DESC').fetchall():
            db.execute('INSERT OR IGNORE INTO image_page_choices VALUES(?,?,?,?,?)',
                       (row['run_id'],row['page_role'],json.dumps(json.loads(row['binding']),sort_keys=True),row['selected_asset'],row['updated']))

def snapshot(run_id,page_role):
    if page_role not in ('blog','product_page'):raise ValueError('图片页面类型无效')
    run=store.get(run_id);attempts=store.content_attempts(run_id)
    if not attempts or attempts[-1]['status']!='completed':raise ValueError('请先完成当前文字生成与事实核验')
    attempt=attempts[-1];approved=content.approved_snapshot(run_id,attempt['plan_revision'],attempt['plan_digest'])
    artifacts=content.attempt_artifacts(run_id,attempt['id'])
    if not content.review_summary(artifacts)['task_complete']:raise ValueError('当前文字尚有阻断检查')
    ir=artifacts['content_ir'];page=next(p for p in ir['data']['pages'] if p['page_type']==page_role)
    binding={'attempt_id':attempt['id'],'text_revision':ir['_artifact_revision'],
             'product_digest':store.input_digest(run['product']),'plan_revision':attempt['plan_revision'],'plan_digest':attempt['plan_digest']}
    preferences=[store.read_artifact(run_id,a['id']) for a in store.artifacts(run_id) if a['kind']=='image_preferences']
    requirements=next((p.get('requirements','') for p in reversed(preferences) if p.get('binding')==binding),'')
    return binding,{'product':run['product'].get('product',{}),'brand':run['product'].get('brand',{}),
                    'style':run['product'].get('style'),
                    'shared_visual_style':shared_visual_style(run['product'].get('brand',{}),run['product'].get('style')),
                    'visual_color_options':visual_colors(run['product'].get('product',{})),
                    'blog_scene_focus':blog_scene_focus(run['product']) if page_role=='blog' else None,
                    'user_visual_requirements':requirements,
                    'editorial_strategy':approved['plan'].get('editorial_strategy',{}),'page':page,'page_role':page_role,
                    'purpose':'Blog hero image' if page_role=='blog' else 'Product detail hero image'}

def session(sid,run_id=None):
    with store.connect() as db:row=db.execute('SELECT * FROM image_sessions WHERE id=?',(sid,)).fetchone()
    if not row or run_id and row['run_id']!=run_id:raise KeyError('图片会话不存在')
    d=dict(row);d['binding']=json.loads(d['binding']);d['config']=json.loads(d['config']);return d

def job(jid,run_id=None):
    with store.connect() as db:row=db.execute('SELECT * FROM image_jobs WHERE id=?',(jid,)).fetchone()
    if not row or run_id and row['run_id']!=run_id:raise KeyError('图片请求不存在')
    d=dict(row);d['payload']=json.loads(d['payload']);return d

def asset(run_id,aid):
    if store.artifact_meta(run_id,aid)['kind']!='image_asset':raise ValueError('不是图片资产')
    d=store.read_artifact(run_id,aid);return {'id':aid,**d}

def artifacts_for(run_id,sid,kind):
    out=[]
    for meta in store.artifacts(run_id):
        if meta['kind']==kind:
            d=store.read_artifact(run_id,meta['id'])
            if d.get('session_id')==sid:out.append({'id':meta['id'],**d})
    return out

def submit(run_id,page_role,attempt_id,revision,key,mode='auto_initial',message='',reference=None,transparent=None,retry=False,actor='local_user',candidate_key=None):
    init();binding,ctx=snapshot(run_id,page_role)
    if (binding['attempt_id'],binding['text_revision'])!=(attempt_id,revision):raise ValueError('正文已变化，请刷新')
    if mode not in ('auto_initial','user_edit'):raise ValueError('图片请求类型无效')
    if candidate_key is None:
        candidate_key=session(asset(run_id,reference)['session_id'],run_id)['candidate_key'] if mode=='user_edit' and reference else 'legacy'
    if candidate_key not in (('legacy','with_user','without_user') if page_role=='blog' else ('legacy',)):
        raise ValueError('图片候选类型无效')
    if not key or len(key)>100 or len(message)>5000:raise ValueError('请求参数无效')
    config={'size':'1280x720' if page_role=='blog' else '1024x1024','format':'png',
            'background':'transparent' if (page_role=='product_page' and transparent is not False) else 'opaque',
            'generation_model':MODEL}
    if mode=='user_edit' and (not message.strip() or not reference):raise ValueError('请填写修改意见并选择参考图片')
    if mode=='user_edit' and transparent is None:
        ref_session=session(asset(run_id,reference)['session_id'],run_id)
        config['background']=ref_session['config']['background']
    if actor not in ('local_user','test_runner'):raise ValueError('图片请求身份无效')
    payload={'binding':binding,'config':config,'candidate_key':candidate_key,'message':message.strip(),'reference':reference,'retry':bool(retry),'actor':actor}
    encoded=json.dumps(payload,ensure_ascii=False,sort_keys=True)
    with store.LOCK,store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        prior=db.execute('SELECT * FROM image_jobs WHERE run_id=? AND idempotency_key=?',(run_id,key)).fetchone()
        if prior:
            if prior['payload']!=encoded or prior['mode']!=mode:raise ValueError('幂等键已用于其他图片请求')
            return dict(prior),False
        rows=db.execute('SELECT * FROM image_sessions WHERE run_id=? AND page_role=? ORDER BY created DESC',(run_id,page_role)).fetchall()
        current=next((r for r in rows if json.loads(r['binding'])==binding and r['candidate_key']==candidate_key),None)
        if current and json.loads(current['config'])!=config:raise ValueError('当前图片会话的比例/背景已固定；修改意见可调整画面，不能静默更换输出契约')
        if current:sid=current['id']
        else:
            if mode=='user_edit':raise ValueError('当前文字版本还没有可编辑的图片')
            sid=uuid.uuid4().hex;t=store.now()
            db.execute('INSERT INTO image_sessions(id,run_id,page_role,binding,config,created,updated,candidate_key) VALUES(?,?,?,?,?,?,?,?)',(sid,run_id,page_role,json.dumps(binding),json.dumps(config),t,t,candidate_key))
        if db.execute("SELECT 1 FROM image_jobs WHERE session_id=? AND status IN ('queued','running')",(sid,)).fetchone():raise ValueError('该页图片正在处理中，请等待')
        if mode=='user_edit':
            ref=asset(run_id,reference)
            if ref['session_id']!=sid:raise ValueError('参考图片不属于当前候选、页面和正文版本')
        else:
            previous=db.execute('SELECT * FROM image_jobs WHERE session_id=?',(sid,)).fetchall()
            calls=db.execute('SELECT auto_calls FROM image_sessions WHERE id=?',(sid,)).fetchone()[0]
            if calls>=3:raise ValueError('自动生成已达 3 次上限，请选择已有图并填写人工修改意见')
            if previous and not retry:raise ValueError('已有自动请求；失败/中断后须明确点击核查后重试')
            if any(r['status']=='done' for r in previous):raise ValueError('自动流程已结束，请查看图片或提交人工修改')
        jid=uuid.uuid4().hex;t=store.now()
        db.execute('INSERT INTO image_jobs VALUES(?,?,?,?,?,?,NULL,?,?,?)',(jid,sid,run_id,mode,encoded,'queued',t,t,key))
        store.enqueue_in_db(db,run_id,'image',{'job_id':jid},'image:'+jid)
    from geo_workbench import queue
    queue.notify();return job(jid),True

def png_checks(raw,config):
    if len(raw)>MAX_IMAGE_BYTES:raise ValueError('图片超过 30 MB')
    with Image.open(io.BytesIO(raw)) as im:
        if im.format!='PNG':raise ValueError('生图未返回约定 PNG')
        im.load();w,h=im.size;ew,eh=map(int,config['size'].split('x'))
        if w*h>20_000_000:raise ValueError('图片像素超过保护上限')
        ratio_ok=abs(w/h-ew/eh)<0.015
        alpha=im.getchannel('A').getextrema()[0]<255 if 'A' in im.getbands() else False
        return {'width':w,'height':h,'format':'PNG','ratio_pass':ratio_ok,'alpha_present':alpha,
                'pass':ratio_ok and (config['background']!='transparent' or alpha),
                'notice':'透明背景需真实 alpha，棋盘格图案不算透明'}

def badge(raw):
    with Image.open(io.BytesIO(raw)) as source:im=source.convert('RGBA')
    draw=ImageDraw.Draw(im);font=label_font(max(14,im.width//65))
    label='AI 视觉示意';bounds=draw.textbbox((0,0),label,font=font);pad=max(8,im.width//100)
    bw=bounds[2]-bounds[0]+pad*2;bh=bounds[3]-bounds[1]+pad*2
    x=im.width-bw-pad;y=im.height-bh-pad
    frost=im.crop((x,y,x+bw,y+bh)).filter(ImageFilter.GaussianBlur(5))
    overlay=Image.new('RGBA',frost.size,(240,244,248,175));frost=Image.alpha_composite(frost,overlay)
    im.paste(frost,(x,y));draw=ImageDraw.Draw(im);draw.text((x+pad,y+pad-bounds[1]),label,font=font,fill=(35,50,65,235))
    out=io.BytesIO();im.save(out,format='PNG');return out.getvalue()

def label_font(size):
    """Use a CJK-capable system font on Windows/macOS; never silently draw tofu."""
    configured=os.getenv('GEO_LABEL_FONT')
    candidates=[configured] if configured else [
        'C:/Windows/Fonts/msyh.ttc','C:/Windows/Fonts/simhei.ttf',
        '/System/Library/Fonts/PingFang.ttc','/System/Library/Fonts/STHeiti Light.ttc',
        '/System/Library/Fonts/Supplemental/Arial Unicode.ttf',
        '/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc']
    for value in candidates:
        if value and Path(value).is_file():
            try:return ImageFont.truetype(value,size)
            except OSError:continue
    raise ValueError('找不到中文标记字体，请将 GEO_LABEL_FONT 设置为本机中文 TTF/TTC 字体路径')

class ImageAgent:
    """One logical agent; Seedream is its generation tool, DeepSeek reads images."""
    def _chat(self,context,history,task,reference=None):
        rules=(Path(__file__).parent/'skills'/'images'/'SKILL.md').read_text(encoding='utf-8')
        stable={k:v for k,v in context.items() if k not in ('mode','user_instruction')}
        messages=[{'role':'system','content':rules},{'role':'user','content':json.dumps({'context':stable},ensure_ascii=False,sort_keys=True)}]
        for item in history[-12:]:messages.append({'role':'assistant','content':json.dumps(item,ensure_ascii=False,sort_keys=True)})
        user=[{'type':'text','text':json.dumps({'task':task,'mode':context['mode'],'user_instruction':context['user_instruction']},ensure_ascii=False)}]
        if reference:user.append({'type':'image_url','image_url':{'url':'data:image/png;base64,'+base64.b64encode(reference).decode()}})
        request={'model':os.getenv('DEEPSEEK_MODEL','deepseek-flash'),'thinking':{'type':'enabled'},'reasoning_effort':'low','max_tokens':6000,
                 'response_format':{'type':'json_object'},'messages':messages+[{'role':'user','content':user}]}
        if os.getenv('DEEPSEEK_BASE_URL','https://api.deepseek.com').rstrip('/')!='https://api.deepseek.com':raise ValueError('DeepSeek 地址配置不符合当前适配器')
        with httpx.Client(trust_env=True,timeout=90) as client:
            r=client.post('https://api.deepseek.com/chat/completions',headers={'Authorization':'Bearer '+os.environ['DEEPSEEK_API_KEY']},json=request)
        if r.status_code!=200:raise RuntimeError('图片 Agent HTTP '+str(r.status_code))
        d=r.json()
        if d['choices'][0].get('finish_reason')=='length':raise RuntimeError('图片 Agent 输出截断')
        return json.loads(d['choices'][0]['message']['content']),{'model':d.get('model'),'usage':d.get('usage',{}),
            'thinking':'enabled','reasoning_effort':'low',
            'skill_sha256':hashlib.sha256(rules.encode()).hexdigest(),'history_total':len(history),'history_sent':min(12,len(history)),
            'history_policy':'完整历史本地保留；模型接收最近 12 条和固定商品/正文上下文；供应商缓存命中仅按 usage 观察'}

    def prepare(self,ctx,history,reference=None):
        keys=LAYER_KEYS[ctx['page_role']]
        check=(' For automatic Blog preparation, prioritize blog_scene_focus alongside the actual title, full article and product facts. Internally select ONE supported target-use scenario with strong reader relevance, natural product prominence and visual impact; turn it into a concrete creative scene. A generic product display with symbolic props alone is insufficient. Internally check and revise title/full-article scenario/product fit before output in this same call. Return only the reconciled final picture fields, no scenario list, checklist, scores or rationale.' if ctx['page_role']=='blog' and ctx['mode']=='auto_initial' else '')
        result,usage=self._chat(ctx,history,'Return ONLY JSON with exact top-level keys layers, caption_en, alt. For this '+ctx['page_role']+' page, layers must have exactly these string fields: '+', '.join(keys)+'. Internally apply the editorial-cover guidance, candidate_direction and shared_visual_style. Identify the actual user/beneficiary from current product and article, never default to human or fixed gender. If use identity is genuinely unresolved for with_user, return only {"error":"使用者不明确，请补充使用对象"}. Output concrete renderable descriptions, not marketing analysis, reasoning, scores or alternatives. All layers and metadata must be English. The program composes these internal fields into Seedream prompt text; they are not Seedream API fields.'+check+' For user_edit faithfully translate the user instruction, preserve unrequested properties and reference identity; do NOT introduce a new creative concept or perform quality review.',reference)
        if isinstance(result,dict) and result.get('error'):raise ValueError('使用者不明确，请补充使用对象后重试')
        return result,usage

    def review(self,ctx,history,raw):
        return self._chat(ctx,history,'Read the actual attached image, compare it to the current product, page purpose, shared visual style and requested picture (latest history plan). Return JSON {status:"pass|revise|unknown",issues:[specific strings],instruction:"a concise change instruction, or empty"}. Review obvious subject/category/color/composition errors and clear departure from the requested visual concept, not imagined physical authenticity or predicted marketing effectiveness. Do not demand unsupported fine details or gratuitous retouching.',raw)

    def generate(self,request):
        key=next((v for k,v in os.environ.items() if k.casefold() in ('seedream_api_key','ark_api_key') and v),None)
        if not key:raise ValueError('未配置 Seedream API key')
        with httpx.Client(trust_env=True,timeout=180) as client:
            r=client.post('https://ark.cn-beijing.volces.com/api/v3/images/generations',headers={'Authorization':'Bearer '+key},json=request)
        if r.status_code!=200:
            try:
                err=r.json().get('error',{});detail=str(err.get('code',''))+': '+str(err.get('message',''))
            except (ValueError,AttributeError):detail='供应商未返回结构化错误'
            detail=re.sub(r'account\s*\[[^\]]+\]','account [redacted]',detail.replace(key,'[redacted]'),flags=re.I)[:300]
            raise RuntimeError('Seedream HTTP '+str(r.status_code)+'；'+detail)
        d=r.json();items=d.get('data',[])
        if len(items)!=1:raise RuntimeError('预期一张图片，实际返回数量异常')
        item=items[0]
        if item.get('b64_json'):raw=base64.b64decode(item['b64_json'],validate=True)
        elif item.get('url'):
            u=urlsplit(item['url'])
            if u.scheme!='https' or not u.hostname or not u.hostname.endswith(('.volces.com','.volcengine.com')):raise ValueError('图片下载地址不在已允许供应商域名')
            with httpx.Client(trust_env=True,timeout=60,follow_redirects=False) as client:
                with client.stream('GET',item['url']) as response:
                    response.raise_for_status();chunks=[];size=0
                    for chunk in response.iter_bytes():
                        size+=len(chunk)
                        if size>MAX_IMAGE_BYTES:raise ValueError('图片下载超过大小上限')
                        chunks.append(chunk)
                    raw=b''.join(chunks)
        else:raise RuntimeError('供应商未返回有效图片')
        return raw,{'model':d.get('model',MODEL),'usage':d.get('usage',{}),'request_id':d.get('id')}

def prompt_from(plan,page_role):
    layers=plan.get('layers',{})
    keys=LAYER_KEYS[page_role]
    if not isinstance(layers,dict) or set(layers)!=set(keys) or any(not isinstance(layers.get(k),str) or not layers[k].strip() or len(layers[k])>1200 for k in keys):raise ValueError('图片 Agent 未返回对应页面的完整分层 Prompt')
    prompt='\n'.join(k.replace('_',' ').title()+': '+layers[k].strip() for k in keys)
    if len(prompt.split())>600:raise ValueError('Prompt 超过 600 词，请缩短')
    if any(not isinstance(plan.get(k),str) or not plan[k].strip() for k in ('caption_en','alt')):raise ValueError('缺少英文图注或 Alt')
    return prompt

def english_metadata(plan,ctx):
    """Keep delivery metadata English without adding a visual-review round."""
    result=copy.deepcopy(plan);original={}
    name=ctx['product'].get('name') or 'the product'
    for field in ('caption_en','alt'):
        value=result.get(field,'')
        if isinstance(value,str) and re.search(r'[\u3400-\u9fff]',value):
            original[field]=value
            result[field]=('AI visual illustration of '+name+'.' if field=='caption_en' else
                           'Illustration of '+name+' for the '+('blog hero' if ctx['page_role']=='blog' else 'product detail')+' image.')
    if original:result['metadata_fallback']={'original':original,'method':'program English description; no visual/model recheck'}
    return result

def process(jid,provider=None):
    j=job(jid);s=session(j['session_id']);provider=provider or ImageAgent();rid=j['run_id'];started=time.monotonic()
    with store.connect() as db:
        if db.execute("UPDATE image_jobs SET status='running',updated=? WHERE id=? AND status='queued'",(store.now(),jid)).rowcount!=1:return
    try:
        binding,ctx=snapshot(rid,s['page_role'])
        if binding!=s['binding']:raise ValueError('文字/商品版本已变化，图片请求已过期')
        with store.connect() as db:
            first=db.execute("SELECT payload FROM image_jobs WHERE session_id=? AND mode='auto_initial' ORDER BY created LIMIT 1",(s['id'],)).fetchone()
        ctx.update(config=s['config'],mode=j['mode'],user_instruction=j['payload']['message'],
                   initial_requirements=json.loads(first['payload']).get('message','') if first else '',
                   candidate_key=s['candidate_key'],candidate_direction=candidate_direction(s['candidate_key']))
        history=[a['message'] for a in artifacts_for(rid,s['id'],'image_message')]
        ref_id=j['payload']['reference']
        if not ref_id and j['mode']=='auto_initial':
            previous=artifacts_for(rid,s['id'],'image_asset');ref_id=previous[-1]['id'] if previous else None
        reference=base64.b64decode(asset(rid,ref_id)['raw_png']) if ref_id else None
        count=1 if j['mode']=='user_edit' else 3-s['auto_calls']
        for round_no in range(count):
            if time.monotonic()-started>600:raise RuntimeError('图片流程时间预算耗尽')
            plan,usage=provider.prepare(ctx,history,reference);plan=english_metadata(plan,ctx);prompt=prompt_from(plan,s['page_role'])
            request={'model':s['config']['generation_model'],'prompt':prompt,'size':s['config']['size'],
                     'background':s['config']['background'],'output_format':'png','response_format':'b64_json','watermark':False}
            if reference:request['image']='data:image/png;base64,'+base64.b64encode(reference).decode()
            store.put(rid,'image_request',{'session_id':s['id'],'candidate_key':s['candidate_key'],'job_id':jid,'mode':j['mode'],'prompt_schema':PROMPT_SCHEMA,'shared_visual_style':ctx['shared_visual_style'],'blog_scene_focus':ctx.get('blog_scene_focus'),'candidate_direction':ctx['candidate_direction'],'initial_requirements':ctx['initial_requirements'],'user_visual_requirements':ctx['user_visual_requirements'],'request':request,'user_quote':j['payload']['message'],'agent':usage})
            with store.connect() as db:
                if j['mode']=='auto_initial' and db.execute('UPDATE image_sessions SET auto_calls=auto_calls+1 WHERE id=? AND auto_calls<3',(s['id'],)).rowcount!=1:raise ValueError('自动生图已达 3 次上限')
            store.event(rid,'image_generation_started',{'session_id':s['id'],'job_id':jid,'mode':j['mode'],'reference':ref_id})
            raw,gen_usage=provider.generate(request)
            if len(raw)>MAX_IMAGE_BYTES:raise ValueError('图片超过 30 MB')
            store.put(rid,'image_generated',{'session_id':s['id'],'job_id':jid,'raw_png':base64.b64encode(raw).decode(),'generator':gen_usage,'plan':plan})
            checks=png_checks(raw,s['config']);visual={'status':'not_run_user_edit','issues':[]};review_usage=None
            if j['mode']=='auto_initial':
                try:
                    visual,review_usage=provider.review(ctx,history+[{'role':'assistant','plan':plan}],raw)
                    if visual.get('status') not in ('pass','revise','unknown') or not isinstance(visual.get('issues'),list):raise ValueError('视觉核验输出无效')
                except Exception as exc:
                    visual={'status':'unknown','issues':['视觉检查未完成：'+type(exc).__name__],'instruction':''}
                    store.put(rid,'image_review_error',{'session_id':s['id'],'job_id':jid,'error_type':type(exc).__name__})
            value={'session_id':s['id'],'candidate_key':s['candidate_key'],'job_id':jid,'page_role':s['page_role'],'binding':binding,'mode':j['mode'],
                   'parent_asset':ref_id,'raw_png':base64.b64encode(raw).decode(),'display_png':base64.b64encode(badge(raw)).decode(),
                   'prompt_schema':PROMPT_SCHEMA,'shared_visual_style':ctx['shared_visual_style'],
                   'prompt':prompt,'layers':plan['layers'],'caption_en':plan['caption_en'],'alt':plan['alt'],'file_checks':checks,
                   'metadata_fallback':plan.get('metadata_fallback'),
                   'visual_review':visual,'generator':gen_usage,'review_agent':review_usage,'user_quote':j['payload']['message'],
                   'actor':j['payload'].get('actor','local_user'),'status':'ready' if checks['pass'] and visual['status'] in ('pass','not_run_user_edit') else 'needs_review'}
            meta=store.put(rid,'image_asset',value);ref_id=meta['id'];reference=raw
            message={'role':'assistant','plan':plan,'asset_id':ref_id,'file_checks':checks,'visual_review':visual,'user_instruction':ctx['user_instruction']}
            store.put(rid,'image_message',{'session_id':s['id'],'job_id':jid,'message':message});history.append(message)
            with store.connect() as db:db.execute('UPDATE image_sessions SET selected_asset=?,updated=? WHERE id=?',(ref_id,store.now(),s['id']))
            with store.connect() as db:
                db.execute('INSERT OR IGNORE INTO image_page_choices VALUES(?,?,?,?,?)',
                           (rid,s['page_role'],json.dumps(binding,sort_keys=True),ref_id,store.now()))
            if j['mode']=='user_edit' or visual['status']=='unknown' or checks['pass'] and visual['status']=='pass':break
            ctx['user_instruction']=visual.get('instruction') or 'Correct the reported image/file issues while preserving the product.'
        with store.connect() as db:db.execute("UPDATE image_jobs SET status='done',updated=? WHERE id=?",(store.now(),jid))
    except Exception as exc:
        error=str(exc)[:300] if isinstance(exc,(ValueError,RuntimeError)) else type(exc).__name__+'；外部结果可能未知，请核查后显式重试'
        with store.connect() as db:db.execute("UPDATE image_jobs SET status='failed',error=?,updated=? WHERE id=?",(error,store.now(),jid))
        store.event(rid,'image_error',{'job_id':jid,'error':error})

def recover():
    init()
    with store.connect() as db:db.execute("UPDATE image_jobs SET status='interrupted',error='external_result_unknown; 未自动重放',updated=? WHERE status='running'",(store.now(),))

def view(run_id,page_role):
    init();binding,ctx=snapshot(run_id,page_role)
    with store.connect() as db:rows=db.execute('SELECT id FROM image_sessions WHERE run_id=? AND page_role=? ORDER BY created DESC',(run_id,page_role)).fetchall()
    sessions={}
    for row in rows:
        s=session(row['id'])
        if s['binding']==binding:sessions.setdefault(s['candidate_key'],s)
    candidates=[]
    for key in (('with_user','without_user','legacy') if page_role=='blog' else ('legacy',)):
        s=sessions.get(key);assets=artifacts_for(run_id,s['id'],'image_asset') if s else []
        for a in assets:a.pop('raw_png');a.pop('display_png')
        with store.connect() as db:
            jobs=[dict(r) for r in db.execute('SELECT id,mode,status,error,created,payload FROM image_jobs WHERE session_id=? ORDER BY created',(s['id'],))] if s else []
        for item in jobs:item['message']=json.loads(item.pop('payload')).get('message','')
        if key!='legacy' or s:candidates.append({'candidate_key':key,'session':s,'assets':assets,'jobs':jobs})
    with store.connect() as db:
        choice=db.execute('SELECT asset_id FROM image_page_choices WHERE run_id=? AND page_role=? AND binding=?',
                          (run_id,page_role,json.dumps(binding,sort_keys=True))).fetchone()
    chosen=choice['asset_id'] if choice else None
    active=next((c for c in candidates if any(a['id']==chosen for a in c['assets'])),None)
    if not active:active=next((c for c in reversed(candidates) if c['session']),None)
    if not active:return {'binding':binding,'session':None,'assets':[],'jobs':[],'release':None,'candidates':candidates,'default_brief':ctx['user_visual_requirements']}
    s=copy.deepcopy(active['session']);s['selected_asset']=chosen or s['selected_asset']
    releases=artifacts_for(run_id,s['id'],'page_release');release=next((r for r in reversed(releases) if r['asset_id']==s['selected_asset'] and r['binding']==binding),None)
    return {'binding':binding,'session':s,'assets':active['assets'],'jobs':active['jobs'],'release':release,'candidates':candidates,'default_brief':ctx['user_visual_requirements']}

def select(run_id,page_role,aid):
    a=asset(run_id,aid);s=session(a['session_id'],run_id);binding,_=snapshot(run_id,page_role)
    if s['page_role']!=page_role or s['binding']!=binding or a['binding']!=binding:raise ValueError('图片/正文版本已过期或不属于当前页面')
    with store.connect() as db:
        if db.execute("SELECT 1 FROM image_jobs WHERE session_id=? AND status IN ('queued','running')",(s['id'],)).fetchone():raise ValueError('图片正在处理，请稍后选择')
        db.execute('UPDATE image_sessions SET selected_asset=?,updated=? WHERE id=?',(aid,store.now(),s['id']))
        db.execute('INSERT INTO image_page_choices VALUES(?,?,?,?,?) ON CONFLICT(run_id,page_role,binding) DO UPDATE SET asset_id=excluded.asset_id,updated=excluded.updated',
                   (run_id,page_role,json.dumps(binding,sort_keys=True),aid,store.now()))

def page_html(run_id,page_role,aid):
    a=asset(run_id,aid);s=session(a['session_id'],run_id);binding,_=snapshot(run_id,page_role)
    if s['page_role']!=page_role or a['binding']!=binding:raise ValueError('图文依赖已过期')
    text=content.attempt_artifacts(run_id,binding['attempt_id'])['content_pages']['data']['pages'][page_role]
    figure='<figure><img style="max-width:100%;height:auto" src="data:image/png;base64,'+a['display_png']+'" alt="'+html.escape(a['alt'],quote=True)+'"><figcaption>'+html.escape(a['caption_en'])+'</figcaption></figure>'
    end=text.find('</h1>')+5
    return text[:end]+figure+text[end:]

def confirm(run_id,page_role,aid,attempt_id,revision,actor='local_user'):
    binding,_=snapshot(run_id,page_role)
    if (binding['attempt_id'],binding['text_revision'])!=(attempt_id,revision):raise ValueError('正文已变化，请刷新')
    select(run_id,page_role,aid);a=asset(run_id,aid)
    if not a['file_checks']['pass']:raise ValueError('图片文件/比例/透明通道检查未通过')
    with store.LOCK:
        prior=artifacts_for(run_id,a['session_id'],'page_release')
        old=next((r for r in prior if r['asset_id']==aid and r['binding']==binding),None)
        if old:return old['id']
        rendered=page_html(run_id,page_role,aid)
        return store.put(run_id,'page_release',{'session_id':a['session_id'],'page_role':page_role,'binding':binding,
                         'asset_id':aid,'html':rendered,'actor':actor,'confirmed_at':store.now(),
                         'visual_review':a['visual_review'],'render_digest':hashlib.sha256(rendered.encode()).hexdigest()})['id']

def export_zip(run_id):
    files={};manifest=[]
    for role,filename in [('blog','blog.html'),('product_page','product.html')]:
        d=view(run_id,role);release=d['release']
        if not release:raise ValueError('请先在两个页面确认当前图文版本')
        a=asset(run_id,release['asset_id']);name='images/'+role+'.png'
        raw=base64.b64decode(a['display_png']);files[name]=raw
        files[filename]=release['html'].replace('data:image/png;base64,'+a['display_png'],name).encode('utf-8')
        versions=[asset(run_id,item['id']) for c in d['candidates'] for item in c['assets']];history=[]
        for version in versions:
            historical_name='history/'+role+'/'+version['id']+'.png'
            files[historical_name]=base64.b64decode(version['display_png'])
            history.append({'asset_id':version['id'],'parent_asset':version.get('parent_asset'),'file':historical_name,
                            'candidate_key':version.get('candidate_key','legacy'),
                            'mode':version['mode'],'prompt':version['prompt'],'layers':version['layers'],
                            'prompt_schema':version.get('prompt_schema','legacy-v1'),'shared_visual_style':version.get('shared_visual_style'),
                            'user_quote':version['user_quote'],'generator':version['generator'],
                            'visual_review':version['visual_review'],'file_checks':version['file_checks']})
        manifest.append({'page':role,'release':release['id'],'binding':release['binding'],'asset_id':a['id'],
                         'prompt_schema':a.get('prompt_schema','legacy-v1'),'shared_visual_style':a.get('shared_visual_style'),
                         'prompt':a['prompt'],'caption_en':a['caption_en'],'alt':a['alt'],'mode':a['mode'],
                         'visual_review':a['visual_review'],'generator':a['generator'],'user_quote':a['user_quote'],
                         'params':d['session']['config'],'versions':history})
    files['generation-record.json']=json.dumps(manifest,ensure_ascii=False,indent=2).encode('utf-8')
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as z:
        for name,value in files.items():z.writestr(name,value)
    return out.getvalue()
