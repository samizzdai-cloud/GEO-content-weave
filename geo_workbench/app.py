"""Local single-user product research, content and image workbench API."""
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, Field
from geo_workbench import composition, store, workflow, research, content, intake_dialog, batches, queue, ownership, images

app=FastAPI(title='GEO Research Workbench',docs_url=None,redoc_url=None,openapi_url=None)
TOKEN=secrets.token_urlsafe(32)
HTML=(Path(__file__).parent/'static'/'index.html').read_text(encoding='utf-8')

@app.on_event('startup')
def startup():
    store.init()
    images.init()
    ownership.acquire()
    try:
        queue.recover()
        images.recover()
        content.reconcile_completion()
        if os.environ.get('GEO_WORKER_DISABLED')!='1': queue.start()
    except Exception:
        ownership.release()
        raise

@app.on_event('shutdown')
def shutdown():
    if queue.stop(): ownership.release()

@app.middleware('http')
async def local_only(request:Request,call_next):
    host=request.headers.get('host','').split(':')[0].lower()
    if host not in ('127.0.0.1','localhost','testserver'):
        return HTMLResponse('Invalid host',status_code=403)
    origin=request.headers.get('origin')
    if origin:
        p=urlsplit(origin)
        if p.scheme!='http' or p.netloc!=request.headers.get('host'): return HTMLResponse('Invalid origin',status_code=403)
    if request.method not in ('GET','HEAD') and not secrets.compare_digest(request.headers.get('x-geo-token',''),TOKEN):
        return HTMLResponse('Invalid local token',status_code=403)
    response=await call_next(request)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Cache-Control']='no-store'
    response.headers['Content-Security-Policy']="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'"
    if ('/content-attempts/' in request.url.path and '/preview/' in request.url.path) or request.url.path.endswith('/image-preview'):
        response.headers['Content-Security-Policy']="sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:; frame-ancestors 'self'; base-uri 'none'; form-action 'none'"
    return response

class Create(BaseModel):
    product:dict
    user_query:str=Field(min_length=1,max_length=10000)

class ImportFile(BaseModel):
    filename:str=Field(min_length=1,max_length=200)
    text:str=Field(max_length=5000000)

class BatchPreview(BaseModel):
    files:list[ImportFile]=Field(min_length=1,max_length=20)
    defaults:dict=Field(default_factory=dict)
    user_query:str=Field(min_length=1,max_length=10000)

class BatchCreate(BatchPreview):
    idempotency_key:str=Field(min_length=1,max_length=100)
    selected:list[int]|None=None

class WorkRetry(BaseModel):
    work_id:str
    idempotency_key:str=Field(min_length=1,max_length=100)

@app.post('/api/batches/preview')
def preview_batch(data:BatchPreview):
    try:
        result=batches.preflight([f.model_dump() for f in data.files],data.defaults,data.user_query)
        return {k:v for k,v in result.items() if k!='sources'}
    except (ValueError,TypeError) as exc:raise HTTPException(400,str(exc))

@app.post('/api/batches')
def create_batch(data:BatchCreate):
    try:
        bid,fresh=batches.create([f.model_dump() for f in data.files],data.defaults,data.user_query,data.idempotency_key,data.selected)
        queue.notify()
        return {'id':bid,'duplicate':not fresh}
    except (ValueError,TypeError) as exc:raise HTTPException(409,str(exc))

@app.get('/api/batches')
def list_batches():
    with store.connect() as db:
        rows=db.execute('SELECT id,created FROM batches ORDER BY created DESC LIMIT 100').fetchall()
    return [dict(row) for row in rows]

@app.get('/api/batches/{batch_id}')
def get_batch(batch_id:str):
    try:return batches.get(batch_id)
    except (ValueError,KeyError):raise HTTPException(404,'批次不存在')

@app.get('/api/batches/{batch_id}/sources/{source_id}')
def get_batch_source(batch_id:str,source_id:str):
    try:return batches.source(batch_id,source_id)
    except (ValueError,KeyError):raise HTTPException(404,'来源不存在或摘要不匹配')

@app.post('/api/runs/{run_id}/work-retry')
def retry_work(run_id:str,data:WorkRetry):
    try:return {'work_id':queue.retry(run_id,data.work_id,data.idempotency_key)}
    except (ValueError,KeyError) as exc:raise HTTPException(409,str(exc))

class Resolve(BaseModel):
    action:str
    user_query:str=''
    product:dict|None=None
    idempotency_key:str=Field(min_length=1,max_length=100)

class IntakeMessage(BaseModel):
    message:str=Field(min_length=1,max_length=10000)
    mode:str='dialog'

class IntakeConfirm(BaseModel):
    candidate_id:str
    base_digest:str
    idempotency_key:str=Field(min_length=1,max_length=100)

class Decision(BaseModel):
    action:str
    revision:int
    digest:str
    idempotency_key:str=Field(min_length=1,max_length=100)
    feedback:str=''

class PlanEdit(BaseModel):
    revision:int
    digest:str
    plan:dict

class ContentStart(BaseModel):
    revision:int
    digest:str
    idempotency_key:str=Field(min_length=1,max_length=100)

class ContentReview(BaseModel):
    parent_attempt:str
    content_revision:int
    message:str=Field(min_length=1,max_length=10000)
    idempotency_key:str=Field(min_length=1,max_length=100)

class ImageRequest(BaseModel):
    attempt_id:str
    revision:int
    idempotency_key:str=Field(min_length=1,max_length=100)
    mode:str='auto_initial'
    message:str=Field(default='',max_length=5000)
    reference:str|None=None
    transparent:bool|None=None
    retry:bool=False
    candidate_key:str|None=None

class ImageSelection(BaseModel):
    asset_id:str

class ImageConfirmation(ImageSelection):
    attempt_id:str
    revision:int

@app.get('/api/runs/{run_id}/page-images/{page_role}')
def image_state(run_id:str,page_role:str):
    try:return images.view(run_id,page_role)
    except (ValueError,KeyError,FileNotFoundError) as e:raise HTTPException(409,str(e))

@app.post('/api/runs/{run_id}/page-images/{page_role}')
def image_generate(run_id:str,page_role:str,data:ImageRequest):
    try:
        j,fresh=images.submit(run_id,page_role,**{'attempt_id':data.attempt_id,'revision':data.revision,'key':data.idempotency_key,
            'mode':data.mode,'message':data.message,'reference':data.reference,'transparent':data.transparent,'retry':data.retry,'candidate_key':data.candidate_key})
        return {'accepted':True,'job_id':j['id'],'duplicate':not fresh}
    except (ValueError,KeyError,FileNotFoundError) as e:raise HTTPException(409,str(e))

@app.post('/api/runs/{run_id}/page-images/{page_role}/select')
def image_select(run_id:str,page_role:str,data:ImageSelection):
    try:images.select(run_id,page_role,data.asset_id);return {'selected':True}
    except (ValueError,KeyError,FileNotFoundError) as e:raise HTTPException(409,str(e))

@app.post('/api/runs/{run_id}/page-images/{page_role}/confirm')
def image_confirm(run_id:str,page_role:str,data:ImageConfirmation):
    try:return {'release_id':images.confirm(run_id,page_role,data.asset_id,data.attempt_id,data.revision)}
    except (ValueError,KeyError,FileNotFoundError) as e:raise HTTPException(409,str(e))

@app.get('/api/runs/{run_id}/image-assets/{asset_id}.png')
def image_binary(run_id:str,asset_id:str,download:bool=False):
    try:
        a=images.asset(run_id,asset_id)
        headers={'Content-Disposition':f'attachment; filename="{a["page_role"]}.png"'} if download else {}
        return Response(images.base64.b64decode(a['display_png']),media_type='image/png',headers=headers)
    except (ValueError,KeyError,FileNotFoundError):raise HTTPException(404,'图片不存在或完整性检查失败')

@app.get('/api/runs/{run_id}/page-images/{page_role}/image-preview',response_class=HTMLResponse)
def image_preview(run_id:str,page_role:str,asset_id:str):
    try:return images.page_html(run_id,page_role,asset_id)
    except (ValueError,KeyError,FileNotFoundError) as e:raise HTTPException(409,str(e))

@app.get('/api/runs/{run_id}/image-delivery.zip')
def image_delivery(run_id:str):
    try:return Response(images.export_zip(run_id),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="image-delivery.zip"'})
    except (ValueError,KeyError,FileNotFoundError) as e:raise HTTPException(409,str(e))

@app.get('/api/runs/{run_id}/page-images/{page_role}/delivery.html')
def image_page_delivery(run_id:str,page_role:str):
    try:
        d=images.view(run_id,page_role)
        if not d['release']:raise ValueError('请先确认当前图文版本')
        return Response(d['release']['html'],media_type='text/html',headers={'Content-Disposition':f'attachment; filename="{page_role}.html"',
                        'Content-Security-Policy':"sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"})
    except (ValueError,KeyError,FileNotFoundError) as e:raise HTTPException(409,str(e))

@app.get('/',response_class=HTMLResponse)
def index(): return HTML.replace('__TOKEN__',TOKEN)

@app.get('/workbench.js')
def workbench_script():
    from fastapi.responses import Response
    return Response((Path(__file__).parent/'static'/'workbench.js').read_text(encoding='utf-8'),media_type='text/javascript')

@app.get('/images.js')
def image_script():
    return Response((Path(__file__).parent/'static'/'images.js').read_text(encoding='utf-8'),media_type='text/javascript')

@app.get('/api/runs')
def list_runs():
    with store.connect() as db:
        rows=db.execute('SELECT id,status,product,created,updated,error FROM runs ORDER BY created DESC LIMIT 100').fetchall()
        links={r['run_id']:dict(r) for r in db.execute('SELECT batch_id,run_id,detail,position FROM batch_items WHERE run_id IS NOT NULL')}
    out=[]
    for row in rows:
        r=dict(row);doc=json.loads(r.pop('product'));product=doc.get('product') or {};brand=doc.get('brand') or {}
        link=links.get(r['id']); imported=json.loads(link['detail']) if link else {}
        out.append({**r,'name':product.get('name','未命名商品'),'product_id':product.get('id'),'brand_name':brand.get('name') or '未命名品牌',
                    'brand_key':batches.brand_key(brand,r['id']),'batch_id':link['batch_id'] if link else None,
                    'source_id':imported.get('source_id'),'source_pointer':imported.get('source_pointer'),
                    'source_filename':doc.get('_import',{}).get('filename'),**queue.projection(r['id'])})
    return out

@app.post('/api/runs')
def create(data:Create):
    if len(json.dumps(data.product,ensure_ascii=False))>500000: raise HTTPException(413,'商品数据过大')
    with store.LOCK,store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rid=store.create_in_db(db,data.product,data.user_query)
        store.enqueue_in_db(db,rid,'workflow',{'mode':'start','payload':None},'start:'+rid)
    store.put(rid,'original_input',{'product':data.product,'user_query':data.user_query})
    workflow.schedule(rid)
    return {'id':rid}

@app.get('/api/runs/{run_id}')
def get_run(run_id:str):
    try:
        r=store.get(run_id)
        return {'id':r['id'],'status':r['status'],'user_query':r['query'],'product':r['product'],'issues':r['issues'],'error':r['error'],'plan_rev':r['plan_rev'],'plan_hash':r['plan_hash'],'artifacts':store.artifacts(run_id),'content_attempts':store.content_attempts(run_id),'events':store.events(run_id),'execution':queue.projection(run_id)}
    except (KeyError,ValueError): raise HTTPException(404,'任务不存在')

@app.get('/api/runs/{run_id}/artifacts/{artifact_id}')
def artifact(run_id:str,artifact_id:str):
    try: return store.read_artifact(run_id,artifact_id)
    except (KeyError,ValueError,FileNotFoundError): raise HTTPException(404,'产物不存在或完整性校验失败')

@app.get('/api/runs/{run_id}/content-attempts/{attempt_id}')
def get_content_attempt(run_id:str,attempt_id:str):
    try:
        attempt=store.content_attempt(run_id,attempt_id)
        found=content.attempt_artifacts(run_id,attempt_id)
        return {'attempt':attempt,'artifacts':found,'review_summary':content.review_summary(found)}
    except (KeyError,ValueError,FileNotFoundError): raise HTTPException(404,'试生成记录或产物不存在')

@app.get('/api/runs/{run_id}/content-attempts/{attempt_id}/preview/{page_name}',response_class=HTMLResponse)
def preview_content_page(run_id:str,attempt_id:str,page_name:str):
    key={'blog.html':'blog','product.html':'product_page'}.get(page_name)
    if not key:raise HTTPException(404,'页面不存在')
    try:
        found=content.attempt_artifacts(run_id,attempt_id)
        page=found.get('content_pages',{}).get('data',{}).get('pages',{}).get(key)
        if not isinstance(page,str):raise KeyError('page')
        return HTMLResponse(page)
    except (KeyError,ValueError,FileNotFoundError):raise HTTPException(404,'页面尚未生成或完整性校验失败')

@app.get('/api/runs/{run_id}/content-attempts/{attempt_id}/download/{filename}')
def download_content(run_id:str,attempt_id:str,filename:str):
    from fastapi.responses import Response
    if filename not in ('blog.md','product.jsonld'):raise HTTPException(404,'下载文件不存在')
    try:
        found=content.attempt_artifacts(run_id,attempt_id)
        pages=found.get('content_pages',{}).get('data',{})
        if not pages.get('pages'):raise KeyError('pages')
        if filename=='blog.md':
            ir=found.get('content_ir',{})
            if ir.get('_artifact_revision')!=pages.get('content_revision'):raise ValueError('正文与页面版本不一致')
            body=content.blog_markdown(ir['data']);mime='text/markdown'
        else:
            body=json.dumps(content.product_jsonld(pages['pages']['product_page']),ensure_ascii=False,indent=2)+'\n';mime='application/ld+json'
        return Response(body,media_type=mime,headers={'Content-Disposition':f'attachment; filename="{filename}"'})
    except (KeyError,FileNotFoundError):raise HTTPException(404,'对应正文或页面尚未生成')
    except ValueError:raise HTTPException(409,'产物完整性、格式或版本校验未通过')

@app.post('/api/runs/{run_id}/resolve')
def resolve(run_id:str,data:Resolve):
    if data.action not in ('retry','cancel') or (data.action=='retry' and not data.user_query.strip()): raise HTTPException(400,'操作或指令无效')
    try:
        payload={'action':data.action,'user_query':data.user_query,'product':data.product}
        with store.connect() as db:
            existing=db.execute('SELECT kind,payload FROM decisions WHERE run_id=? AND idempotency_key=?',(store.valid_run(run_id),data.idempotency_key)).fetchone()
        if not existing and not workflow.check_gate(run_id,'input_gate'): raise ValueError('确认点尚未持久化，请稍后刷新')
        fresh=store.decisions(run_id,data.idempotency_key,'input_resolution',payload,'awaiting_input_resolution',dispatch=True)
        if fresh: workflow.schedule(run_id,'resume',payload)
        return {'accepted':True,'duplicate':not fresh}
    except (ValueError,KeyError) as e: raise HTTPException(409,str(e))

@app.get('/api/runs/{run_id}/intake-dialog')
def get_intake_dialog(run_id:str):
    try:return intake_dialog.state(run_id)
    except (ValueError,KeyError):raise HTTPException(404,'任务不存在')

@app.get('/api/runs/{run_id}/input-versions/{revision}')
def get_input_version(run_id:str,revision:int):
    try:
        store.valid_run(run_id)
        with store.connect() as db: row=db.execute('SELECT revision,digest,parent_revision,product,created FROM input_versions WHERE run_id=? AND revision=?',(run_id,revision)).fetchone()
        if not row:raise KeyError('version')
        result=dict(row);result['product']=json.loads(result['product'])
        if store.input_digest(result['product'])!=result['digest']:raise ValueError('digest mismatch')
        return result
    except (ValueError,KeyError):raise HTTPException(404,'商品版本不存在或完整性校验失败')

@app.post('/api/runs/{run_id}/intake-dialog')
def intake_message(run_id:str,data:IntakeMessage):
    try:return intake_dialog.respond(run_id,data.message,data.mode)
    except (ValueError,KeyError,RuntimeError) as e:raise HTTPException(409,str(e))
    except Exception:raise HTTPException(503,'入口助手调用失败，已保留本轮输入，请重试或使用手动修改方式。')

@app.post('/api/runs/{run_id}/intake-dialog/confirm')
def intake_confirm(run_id:str,data:IntakeConfirm):
    try:
        fresh=intake_dialog.confirm(run_id,data.candidate_id,data.base_digest,data.idempotency_key,dispatch=True)
        if fresh:workflow.schedule(run_id,'resume',{'action':'retry'})
        return {'accepted':True,'duplicate':not fresh}
    except (ValueError,KeyError) as e:raise HTTPException(409,str(e))

@app.post('/api/runs/{run_id}/plan')
def edit_plan(run_id:str,data:PlanEdit):
    try:
        with store.LOCK:
            r=store.get(run_id)
            if r['status']!='awaiting_plan_approval' or r['plan_rev']!=data.revision or r['plan_hash']!=data.digest: raise ValueError('规划已变更，请刷新')
            if not all(k in data.plan for k in ('blog_outline','product_outline','question_map','competitor_analysis','evidence_gaps')): raise ValueError('规划缺少必要模块')
            previous=store.latest(run_id,'plan')
            if previous.get('composition_contract_version')==composition.CONTRACT_VERSION:
                data.plan['composition_contract_version']=composition.CONTRACT_VERSION
            else:
                data.plan.pop('composition_contract_version',None)
            if previous.get('schema_version')=='2.0':
                research.validate_plan_structure(data.plan)
                data.plan['schema_version']='2.0'
                data.plan['editorial_strategy']['topic']=store.latest(run_id,'task_contract')['content_requests']['blog']['topic']
            if 'research_dependencies' in previous:
                data.plan['research_dependencies']=previous['research_dependencies']
            else:
                data.plan.pop('research_dependencies',None)
            if 'input_snapshot' in previous:data.plan['input_snapshot']=previous['input_snapshot']
            else:data.plan.pop('input_snapshot',None)
            if 'intent_snapshot' in previous:data.plan['intent_snapshot']=previous['intent_snapshot']
            else:data.plan.pop('intent_snapshot',None)
            workflow.mark_comparisons(data.plan,store.latest(run_id,'evidence'),store.latest(run_id,'task_contract'))
            data.plan['source_warnings']=workflow.citation_warnings(data.plan,store.latest(run_id,'evidence'))
            if previous.get('schema_version')=='2.0':
                data.plan['brief_warnings']=workflow.brief_warnings(data.plan,store.latest(run_id,'task_contract'),store.latest(run_id,'audience'),store.latest(run_id,'evidence'))
                data.plan['planning_warnings']=workflow.planning_shape_warnings(data.plan,store.latest(run_id,'task_contract'))
                if data.plan.get('composition_contract_version')==composition.CONTRACT_VERSION:
                    data.plan['composition_warnings']=composition.plan_warnings(data.plan,store.latest(run_id,'task_contract'))
            meta=store.put(run_id,'plan',data.plan)
            workflow.write_report(run_id,store.latest(run_id,'task_contract'),store.latest(run_id,'audience'),store.latest(run_id,'evidence'),data.plan)
        return meta
    except (ValueError,KeyError) as e: raise HTTPException(409,str(e))

@app.post('/api/runs/{run_id}/decision')
def decide(run_id:str,data:Decision):
    if data.action not in ('approve','cancel','redo') or (data.action=='redo' and not data.feedback.strip()): raise HTTPException(400,'操作无效或缺少退回意见')
    try:
        payload={'action':data.action,'feedback':data.feedback}
        # Completed duplicate decisions remain idempotent even after the gate has closed.
        with store.connect() as db:
            existing=db.execute('SELECT kind,payload FROM decisions WHERE run_id=? AND idempotency_key=?',(store.valid_run(run_id),data.idempotency_key)).fetchone()
        if not existing and not workflow.check_gate(run_id,'plan_gate'): raise ValueError('确认点尚未持久化，请稍后刷新')
        if not existing and data.action=='approve':
            plan=store.latest(run_id,'plan')
            if plan.get('composition_contract_version')==composition.CONTRACT_VERSION and workflow.planning_shape_warnings(plan,store.latest(run_id,'task_contract')):
                raise ValueError('篇幅规划或必要页面结构有待处理；请修改规划或退回后再确认')
        fresh=store.decisions(run_id,data.idempotency_key,'plan_decision',payload,'awaiting_plan_approval',data.revision,data.digest,dispatch=True)
        if fresh: workflow.schedule(run_id,'resume',payload)
        return {'accepted':True,'duplicate':not fresh}
    except (ValueError,KeyError) as e: raise HTTPException(409,str(e))

@app.post('/api/runs/{run_id}/content-start')
def start_content(run_id:str,data:ContentStart):
    try:
        with store.LOCK:
            with store.connect() as db:
                existing=db.execute('SELECT id FROM content_attempts WHERE run_id=? AND idempotency_key=?',(store.valid_run(run_id),data.idempotency_key)).fetchone()
            if not existing: content.approved_snapshot(run_id,data.revision,data.digest)
            attempt,fresh=store.reserve_content(run_id,data.idempotency_key,data.revision,data.digest,queue_payload={})
        if fresh: content.schedule(run_id,attempt['id'])
        return {'accepted':True,'duplicate':not fresh,'attempt_id':attempt['id']}
    except (ValueError,KeyError,FileNotFoundError) as e: raise HTTPException(409,str(e))

@app.post('/api/runs/{run_id}/content-review')
def review_content(run_id:str,data:ContentReview):
    return _review_content(run_id,data,'local_user')

def _review_content(run_id,data,actor):
    try:
        if actor not in ('local_user','test_runner'): raise ValueError('invalid review actor')
        if not data.message.strip(): raise ValueError('请填写审核意见')
        with store.LOCK:
            attempts=store.content_attempts(run_id);run=store.get(run_id)
            existing=next((x for x in attempts if x['idempotency_key']==data.idempotency_key),None)
            if existing:
                feedback=[store.read_artifact(run_id,m['id']) for m in store.artifacts(run_id) if m['kind']=='content_review_feedback']
                saved=next((x['data'] for x in feedback if x.get('attempt_id')==existing['id']),None)
                if not saved or saved['parent_attempt']!=data.parent_attempt or saved['content_revision']!=data.content_revision or saved['message']!=data.message: raise ValueError('幂等键已用于其他操作')
                return {'accepted':True,'duplicate':True,'attempt_id':existing['id']}
            if run['status'] not in ('needs_review','completed') or not attempts or attempts[-1]['id']!=data.parent_attempt: raise ValueError('内容版本已变化，请刷新后审核最新试稿')
            parent=attempts[-1];content.approved_snapshot(run_id,parent['plan_revision'],parent['plan_digest'])
            found={}
            for meta in store.artifacts(run_id):
                if meta['kind'] in ('content_ir','claim_ledger','content_review_feedback'):
                    value=store.read_artifact(run_id,meta['id'])
                    if value.get('attempt_id')==data.parent_attempt:found[meta['kind']]=(meta,value['data'])
            if 'content_ir' not in found or 'claim_ledger' not in found: raise ValueError('当前草稿缺少事实核查记录，请重新试生成')
            draft,ledger=found['content_ir'],found['claim_ledger']
            if draft[0]['revision']!=data.content_revision or ledger[1].get('content_revision')!=data.content_revision or ledger[1].get('coverage_status')!='reviewed': raise ValueError('草稿或核查版本已变化，或核查覆盖不足')
            history=found.get('content_review_feedback',({},{}))[1].get('history',[])
            history=(history+[data.message])[-10:]
            review={'parent_attempt':data.parent_attempt,'revision':data.content_revision,'content_id':draft[0]['id'],'ledger_id':ledger[0]['id'],'feedback':history}
            feedback={'parent_attempt':data.parent_attempt,'content_revision':data.content_revision,'message':data.message,'history':history,'actor':actor}
            attempt,fresh=store.reserve_content(run_id,data.idempotency_key,parent['plan_revision'],parent['plan_digest'],queue_payload={'review':review,'feedback':feedback})
            content.save(run_id,'content_review_feedback',attempt['id'],feedback)
        if fresh:content.schedule_review(run_id,attempt['id'],review)
        return {'accepted':True,'duplicate':False,'attempt_id':attempt['id']}
    except (ValueError,KeyError,FileNotFoundError) as e:raise HTTPException(409,str(e))
