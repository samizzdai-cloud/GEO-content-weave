"""Stage A graph: intake, bounded research and explicit human gates."""
import json
import sqlite3
import threading
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from typing import TypedDict
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from geo_workbench import composition, research, store

class State(TypedDict, total=False):
    run_id: str
    has_issues: bool
    decision: str

def write_report(rid,c,aud,ev,plan):
    def readable(value,level=0):
        if value is None: return '未提供\n'
        if isinstance(value,dict):
            return ''.join(('  '*level+'- **'+str(k).replace('_',' ')+'**：'+(readable(v,level+1) if not isinstance(v,(dict,list)) else '\n'+readable(v,level+1))) for k,v in value.items()) or '未提供\n'
        if isinstance(value,list):
            return ''.join(('  '*level+'- '+(readable(x,level+1) if not isinstance(x,(dict,list)) else '\n'+readable(x,level+1))) for x in value) or '  '*level+'- 暂无\n'
        return str(value).replace('\n',' ')+'\n'
    report='# 研究与规划报告\n\n> 模拟画像不代表真实用户；竞品结论和外部来源支持关系须人工核查。\n\n'
    if plan.get('input_snapshot'): report+='## 商品资料版本\n\n'+readable(plan['input_snapshot'])+'\n'
    if plan.get('intent_snapshot'): report+='## 任务意图版本\n\n'+readable(plan['intent_snapshot'])+'\n'
    if aud.get('schema_version')!='2.0': report+='> 历史版本：不具备每个画像两轮访谈；请勿视为新版研究。\n\n'
    for title,value in [('购买问题地图',aud.get('question_map') or plan.get('question_map')),('模拟画像与两轮访谈',aud),('竞品与内容建议',plan.get('competitor_analysis')),('研究来源',ev),('内容策略与主线',plan.get('editorial_strategy')),('Blog 规划',plan.get('blog_outline')),('商品详情页规划',plan.get('product_outline')),('规划结构检查',plan.get('planning_warnings')),('内部写作 Brief',plan.get('writing_brief')),('Brief 引用检查',plan.get('brief_warnings')),('证据不足',plan.get('evidence_gaps')),('任务契约',c)]:
        if title=='研究来源': value={**ev,'sources':[{k:v for k,v in s.items() if k!='content'} for s in ev.get('sources',[])]}
        report+=f'## {title}\n\n'+readable(value)+'\n'
    store.put(rid,'research_report',report,markdown=True)

def citation_warnings(plan,evidence):
    """Mark missing or failed source identifiers without claiming semantic verification."""
    known={str(s.get('source_id')):s.get('status') for s in evidence.get('sources',[])}
    warnings=[]
    def walk(value,path='$'):
        if isinstance(value,dict):
            for k,v in value.items():
                if k in ('source_ids','sources') and isinstance(v,list):
                    for source in v:
                        if isinstance(source,str) and (source not in known or known[source]!='ok'):
                            warnings.append({'location':path+'.'+k,'source_id':source,'reason':'来源不存在或读取失败'})
                else: walk(v,path+'.'+k)
        elif isinstance(value,list):
            for i,v in enumerate(value): walk(v,f'{path}[{i}]')
    walk(plan)
    return warnings

def referenceable_product_paths(contract):
    """Enumerate input-backed paths without modifying a previously approved contract."""
    paths={f.get('pointer') for f in contract.get('facts',[]) if isinstance(f,dict)}
    def walk(value,path):
        paths.add(path)
        if isinstance(value,dict):
            for key,item in value.items(): walk(item,path+'/'+str(key).replace('~','~0').replace('/','~1'))
        elif isinstance(value,list):
            for index,item in enumerate(value): walk(item,path+'/'+str(index))
    product=contract.get('product') or {}
    for key in ('id','name','category','facts','target_scenarios'):
        if key in product: walk(product[key],'/product/'+key)
    if contract.get('brand') is not None: walk(contract['brand'],'/brand')
    return paths

def mark_comparisons(plan,evidence,contract):
    """Check references, while explicitly leaving semantic support for human review."""
    items=plan.get('competitor_analysis')
    if not isinstance(items,list): return
    sources={str(s.get('source_id')):s.get('status') for s in evidence.get('sources',[])}
    facts=referenceable_product_paths(contract)
    for item in items:
        if not isinstance(item,dict): continue
        refs=item.get('source_ids');pointers=item.get('product_fact_refs')
        refs=refs if isinstance(refs,list) else []
        pointers=pointers if isinstance(pointers,list) else []
        bad_sources=[x for x in refs if not isinstance(x,str) or sources.get(x)!='ok']
        bad_facts=[x for x in pointers if not isinstance(x,str) or x not in facts]
        item['reference_check']={'invalid_sources':bad_sources,'invalid_product_pointers':bad_facts}
        item['evidence_status']='needs_human_review' if refs and pointers and not bad_sources and not bad_facts else 'insufficient_evidence'
        item['evidence_notice']='仅检查引用存在，未验证证据是否支持比较；证据不足时不得写成优势。'

def brief_warnings(plan,contract,audience,evidence):
    brief=plan.get('writing_brief') or {}; strategy=plan.get('editorial_strategy') or {}
    facts=referenceable_product_paths(contract)
    evidence_ids={x.get('evidence_id') for x in evidence.get('evidence_items',[]) if isinstance(x,dict)}
    questions={x.get('question_id') for x in audience.get('question_map',[]) if isinstance(x,dict)}
    warnings=[]
    for pointer in brief.get('product_fact_pointers',[]):
        if not isinstance(pointer,str) or pointer not in facts: warnings.append({'kind':'product_fact_pointer','reference':str(pointer),'reason':'商品事实路径不存在'})
    for ref in brief.get('evidence_refs',[]):
        if not isinstance(ref,str) or ref not in evidence_ids: warnings.append({'kind':'evidence_ref','reference':str(ref),'reason':'证据 ID 不存在或无法验证'})
    for q in strategy.get('selected_questions',[]):
        ref=q.get('question_id') if isinstance(q,dict) else None
        if not isinstance(ref,str) or ref not in questions: warnings.append({'kind':'question_ref','reference':str(ref),'reason':'问题 ID 不存在或无法验证'})
    return warnings

def planning_shape_warnings(plan,contract=None):
    """Identify planning omissions, without asserting actual page or SEO validity."""
    warnings=[]
    for page in ('blog_outline','product_outline'):
        obj=plan.get(page)
        if not isinstance(obj,dict):
            warnings.append({'page':page,'field':'outline','reason':'未识别为结构化大纲，请人工核查'})
            continue
        meta=obj.get('meta') if isinstance(obj.get('meta'),dict) else {}
        for field,value in [('h1',obj.get('h1')),('meta_title',obj.get('meta_title') or meta.get('title')),('meta_description',obj.get('meta_description') or meta.get('description'))]:
            if not isinstance(value,str) or not value.strip(): warnings.append({'page':page,'field':field,'reason':'规划中未识别到该字段，请核查'})
        faqs=next((obj.get(k) for k in ('faq_ideas','faqs','faq') if isinstance(obj.get(k),list)),None)
        if faqs is None or len(faqs)<3: warnings.append({'page':page,'field':'faq','reason':'未识别到至少三组 FAQ 规划，请核查'})
    return warnings+composition.plan_warnings(plan,contract)

def contract(product,query):
    errors=[]
    if not isinstance(product,dict): return None,['商品 JSON 必须是对象']
    p=product.get('product'); req=product.get('content_requests')
    if not isinstance(p,dict) or not p.get('name') or not p.get('category'): errors.append('缺少 product.name 或 product.category')
    if not isinstance(req,dict): errors.append('缺少 content_requests');req={}
    for name in ('blog','product_page'):
        page=req.get(name)
        if not isinstance(page,dict) or not page.get('type'): errors.append(f'缺少 content_requests.{name}.type');continue
        w=page.get('target_words')
        if not isinstance(w,list) or len(w)!=2 or any(not isinstance(x,int) or isinstance(x,bool) or x<=0 for x in w) or w[0]>w[1]: errors.append(f'content_requests.{name}.target_words 必须为有效区间')
        if name=='blog' and (not isinstance(page.get('topic'),str) or not page['topic'].strip()): errors.append('缺少 content_requests.blog.topic')
    if not isinstance(query,str) or not query.strip(): errors.append('user_query 不能为空')
    if errors: return None,errors
    if not isinstance(p.get('facts',{}),dict): return None,['product.facts 必须是对象']
    missing=[f'/product/facts/{x}' for x in ('price','care') if x not in p.get('facts',{})]
    facts=[]
    def flatten(value,path):
        if isinstance(value,dict):
            for key,item in value.items(): flatten(item,path+'/'+str(key).replace('~','~0').replace('/','~1'))
        else:
            facts.append({'id':f'f{len(facts)+1}','pointer':path,'value':value,'origin':'input_json','status':'provided'})
    flatten(p.get('facts',{}),'/product/facts')
    contract_value = {'schema_version':'1.0','product':p,'brand':product.get('brand'), 'target_audience':product.get('target_audience'),
        'content_requests':req,'suggested_paths':product.get('suggested_paths'), 'user_query':query,'missing_fields':missing,'facts':facts,
        'requirements':{'faq_min_per_page':3,'blog_links_to_product':True},'disclosure':'Facts are user-provided, not independently verified.'}
    if isinstance(product.get('_import'),dict): contract_value['input_provenance']=product['_import']
    if 'style' in product: contract_value['style']=product['style']
    return contract_value, []

def build(checkpointer,provider=research):
    def intake(state):
        rid=state['run_id']; run=store.get(rid); store.set_status(rid,'validating')
        c,errors=contract(run['product'],run['query'])
        if errors:
            store.set_status(rid,'blocked_input',issues=[{'type':'invalid','reason':x} for x in errors]);return {'has_issues':True}
        store.put(rid,'task_contract',c)
        store.put(rid,'fact_set',{'schema_version':'1.0','facts':c['facts'],'missing_fields':c['missing_fields']})
        try:
            result=provider.audit_input(rid,run['product'],run['query'])
            issues=result.get('issues')
            if not isinstance(issues,list): raise ValueError('输入审核缺少 issues 列表')
            if any(not isinstance(item,dict) or not isinstance(item.get('user_quote'),str) or not item['user_quote'].strip() or item['user_quote'].casefold() not in run['query'].casefold() for item in issues):
                raise ValueError('输入审核的问题不能定位到用户原文')
        except Exception:
            store.set_status(rid,'needs_review','输入语义审核失败，未继续研究');raise
        store.set_status(rid,'awaiting_input_resolution' if issues else 'researching',issues=issues)
        return {'has_issues':bool(issues)}
    def input_route(state):
        # Invalid JSON/contract is a terminal state, not an approval.
        return 'input_gate' if store.get(state['run_id'])['status']=='awaiting_input_resolution' else (END if state['has_issues'] else 'research')
    def input_gate(state):
        result=interrupt({'kind':'input_resolution','issues':store.get(state['run_id'])['issues']})
        if result.get('action')=='cancel': store.set_status(state['run_id'],'cancelled')
        return {'decision':result.get('action','cancel')}
    def input_after(state): return 'intake' if state['decision']=='retry' else END
    def research_node(state):
        rid=state['run_id']; c=store.latest(rid,'task_contract')
        with ThreadPoolExecutor(max_workers=2) as ex:
            fa=ex.submit(provider.audience,rid,c); fe=ex.submit(provider.evidence,rid,c)
            aud=fa.result();ev=fe.result()
        audience_meta=store.put(rid,'audience',aud);evidence_meta=store.put(rid,'evidence',ev)
        if aud.get('status')=='incomplete':
            store.set_status(rid,'needs_review','画像访谈或问题地图综合未完成；已保存现有对话，未生成规划。')
            return {}
        notes=store.latest(rid,'review_feedback')
        if notes: c={**c,'review_feedback':notes['feedback']}
        result=provider.plan(rid,c,aud,ev)
        if not isinstance(result,dict) or not all(k in result for k in ('blog_outline','product_outline','question_map','competitor_analysis','evidence_gaps')):
            raise ValueError('规划结果缺少必要模块')
        mark_comparisons(result,ev,c)
        result['source_warnings']=citation_warnings(result,ev)
        if result.get('schema_version')=='2.0':
            result['brief_warnings']=brief_warnings(result,c,aud,ev)
            result['planning_warnings']=planning_shape_warnings(result,c)
        result['review_notice']='来源引用仅检查是否读取成功，未核实其是否支持每条主张；请人工核查。'
        contract_meta=next((a for a in reversed(store.artifacts(rid)) if a['kind']=='task_contract'),None)
        result['research_dependencies']={'task_contract':contract_meta,'audience':audience_meta,'evidence':evidence_meta}
        with store.connect() as db:
            version=db.execute('SELECT revision,digest FROM input_versions WHERE run_id=? ORDER BY revision DESC LIMIT 1',(rid,)).fetchone()
            intent=db.execute('SELECT revision,digest FROM intent_versions WHERE run_id=? ORDER BY revision DESC LIMIT 1',(rid,)).fetchone()
        if version and version['digest']==store.input_digest(store.get(rid)['product']): result['input_snapshot']=dict(version)
        if intent and intent['digest']==store.intent_digest(store.get(rid)['query']): result['intent_snapshot']=dict(intent)
        store.put(rid,'plan',result)
        write_report(rid,c,aud,ev,result)
        store.set_status(rid,'awaiting_plan_approval')
        return {}
    def plan_gate(state):
        run=store.get(state['run_id'])
        result=interrupt({'kind':'plan_approval','revision':run['plan_rev'],'hash':run['plan_hash']})
        action=result.get('action')
        if action=='approve': store.set_status(state['run_id'],'plan_approved')
        elif action=='cancel': store.set_status(state['run_id'],'cancelled')
        elif action=='redo':
            store.put(state['run_id'],'review_feedback',{'feedback':result.get('feedback','')})
            store.set_status(state['run_id'],'researching')
        return {'decision':action}
    def plan_after(state): return 'research' if state['decision']=='redo' else END
    g=StateGraph(State)
    for n,f in [('intake',intake),('input_gate',input_gate),('research',research_node),('plan_gate',plan_gate)]: g.add_node(n,f)
    g.add_edge(START,'intake');g.add_conditional_edges('intake',input_route);g.add_conditional_edges('input_gate',input_after)
    g.add_conditional_edges('research',lambda state: 'plan_gate' if store.get(state['run_id'])['status']=='awaiting_plan_approval' else END);g.add_conditional_edges('plan_gate',plan_after)
    return g.compile(checkpointer=checkpointer)

def execute(run_id, mode='start', payload=None, provider=research):
    try:
        with closing(sqlite3.connect(store.ROOT/'checkpoints.sqlite',check_same_thread=False)) as conn:
            graph=build(SqliteSaver(conn),provider)
            config={'configurable':{'thread_id':run_id}}
            graph.invoke({'run_id':run_id} if mode=='start' else Command(resume=payload),config)
    except Exception as e:
        # Avoid logging input, credential, webpage or provider exception text.
        store.set_status(run_id,'needs_review',f'流程失败：{type(e).__name__}，请查看状态并重试')
        store.event(run_id,'error',{'error_type':type(e).__name__})

def check_gate(run_id,expected):
    """Prevent decisions racing a not-yet-durable interrupt checkpoint."""
    with closing(sqlite3.connect(store.ROOT/'checkpoints.sqlite',check_same_thread=False)) as conn:
        graph=build(SqliteSaver(conn))
        state=graph.get_state({'configurable':{'thread_id':run_id}})
        return state.next == (expected,)

def schedule(run_id,mode='start',payload=None,provider=research):
    from geo_workbench import queue
    # HTTP decisions already enqueue atomically. This function only wakes the lane.
    queue.notify()
