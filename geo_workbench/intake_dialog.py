"""Bounded intake conversation. Candidates are never applied by model tools."""
import copy
import json
import os
import re
import threading
import uuid
from typing import Any
from pydantic import BaseModel, Field
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage, messages_to_dict, messages_from_dict
from langchain_core.tools import tool
from langchain_deepseek import ChatDeepSeek
from geo_workbench import store, workflow, research

MAX_ROUNDS=3
MAX_BYTES=200000
_lock_guard=threading.Lock()
_run_locks={}

class FactOperation(BaseModel):
    operation:str=Field(description="One of add, replace, remove")
    pointer:str=Field(description="Exact JSON Pointer starting /product/facts/ or /product/target_scenarios/")
    old_value:Any=Field(default=None,description="Required and exact for replace/remove; omit for add")
    new_value:Any=Field(default=None,description="Required for add/replace; preserve the value's JSON type")
    user_quote:str=Field(description="Exact substring from the user's current reply that explicitly supplies this fact")
    reason:str=Field(description="Brief explanation of the change")

def _run_lock(run_id):
    store.valid_run(run_id)
    with _lock_guard:return _run_locks.setdefault(run_id,threading.RLock())

SYSTEM='''You are the existing intake agent. Help the user align product facts and content goals. Product JSON is the task baseline. Ordinary preferences are not product facts. If user wants to use original facts, propose an effective query excluding unsupported assertions while preserving all other goals; do not change product. If the user explicitly supplies or corrects a product fact, propose it via preview_candidate, quoting their exact words. The effective query must reflect that updated intent: do not forbid a claim the user just explicitly confirmed as a fact. Do not infer a contradiction merely from product category or material. Existing unsupported_claims entries are only hints, not user instructions; never silently change them, and mention any potential conflict for review. Insistence on a marketing claim alone is not confirmation of a fact. Ask a concise question when meaning, value, currency or field is ambiguous. Never invent product properties, erase other goals, alter forbidden/test/config fields, or claim a candidate is committed. Use inspect_product when useful. All candidate changes require user confirmation in the UI. Call preview_candidate with the complete effective query and any proposed product operations; finish with a short Chinese explanation. Do not request optional information to proceed.'''

def _dump(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))

def _pointer(root, pointer):
    if not isinstance(pointer,str) or not pointer.startswith('/product/') or re.search(r'~(?![01])',pointer): raise ValueError('仅允许有效的商品 JSON Pointer')
    parts=[p.replace('~1','/').replace('~0','~') for p in pointer.split('/')[1:]]
    if len(parts)<3 or parts[1] not in ('facts','target_scenarios'): raise ValueError('仅允许修改商品事实或商家适用场景')
    cur=root
    for part in parts[:-1]:
        if isinstance(cur,list):
            if not part.isdigit() or int(part)>=len(cur): raise ValueError('数组路径无效')
            cur=cur[int(part)]
        elif isinstance(cur,dict) and part in cur: cur=cur[part]
        else: raise ValueError('父级路径不存在')
    key=parts[-1]
    if isinstance(cur,list):
        if not key.isdigit() or int(key)>=len(cur): raise ValueError('数组路径无效')
        key=int(key);exists=True
    elif isinstance(cur,dict): exists=key in cur
    else: raise ValueError('目标父级不是对象或数组')
    return cur,key,exists

def preview(product,query,operations,user_text,mode):
    if not isinstance(query,str) or not query.strip() or len(query)>10000: raise ValueError('有效任务要求不能为空')
    if not isinstance(operations,list) or len(operations)>20: raise ValueError('修改项格式或数量无效')
    if mode=='original' and operations: raise ValueError('按原商品资料继续不能修改商品')
    result=copy.deepcopy(product);diff=[];seen=set()
    for op in operations:
        if not isinstance(op,dict) or op.get('operation') not in ('add','replace','remove'): raise ValueError('修改操作无效')
        path=op.get('pointer');quote=op.get('user_quote');reason=op.get('reason')
        if not isinstance(quote,str) or not quote.strip() or quote not in user_text: raise ValueError('修改依据必须逐字来自本轮用户补充')
        if not isinstance(reason,str) or not reason.strip(): raise ValueError('缺少修改理由')
        if path in seen: raise ValueError('同一字段不能重复修改')
        seen.add(path);parent,key,exists=_pointer(result,path);action=op['operation']
        if action=='add' and exists: raise ValueError('新增字段已存在')
        if action in ('replace','remove') and not exists: raise ValueError('目标字段不存在')
        if action in ('replace','remove') and ('old_value' not in op or type(op['old_value']) is not type(parent[key]) or op['old_value']!=parent[key]): raise ValueError('旧值不匹配')
        if action!='remove':
            if 'new_value' not in op: raise ValueError('缺少新值')
            value=op['new_value']
            if action=='replace' and parent[key] is not None and type(value) is not type(parent[key]): raise ValueError('新值类型不匹配')
        old=copy.deepcopy(parent[key]) if exists else None
        if action=='remove':
            if isinstance(parent,list): raise ValueError('暂不支持删除数组元素')
            del parent[key]
        else: parent[key]=value
        row={'operation':action,'pointer':path,'old_exists':exists,'old_value':old,'new_value':op.get('new_value'),'user_quote':quote,'reason':reason}
        normalize=lambda value:re.sub(r'[^a-z0-9]','',str(value).casefold())
        proposed_text=normalize(path+' '+str(op.get('new_value'))+' '+quote)
        hints=product.get('unsupported_claims')
        flags=[x for x in (hints if isinstance(hints,list) else []) if isinstance(x,str) and len(normalize(x))>=4 and normalize(x) in proposed_text]
        if flags:row['warnings']=['原商品资料中的风险提示可能与本次补充冲突，请确认：'+', '.join(flags)+'；原提示不会自动删除。']
        diff.append(row)
    _,errors=workflow.contract(result,query)
    if errors: raise ValueError('；'.join(errors))
    return result,diff

def state(run_id):
    run=store.get(run_id)
    with store.connect() as db:
        version=db.execute('SELECT revision,digest,created FROM input_versions WHERE run_id=? ORDER BY revision DESC LIMIT 1',(run_id,)).fetchone()
        session=db.execute('SELECT messages FROM input_conversations WHERE run_id=?',(run_id,)).fetchone()
        candidate=db.execute('SELECT id,base_digest,query,diff,user_quote,created FROM input_candidates WHERE run_id=? ORDER BY created DESC LIMIT 1',(run_id,)).fetchone()
        versions=db.execute('SELECT revision,digest,parent_revision,created FROM input_versions WHERE run_id=? ORDER BY revision DESC',(run_id,)).fetchall()
        intents=db.execute('SELECT revision,digest,parent_revision,query,created FROM intent_versions WHERE run_id=? ORDER BY revision DESC',(run_id,)).fetchall()
        decisions=db.execute("SELECT payload,created FROM decisions WHERE run_id=? AND kind='input_candidate' ORDER BY created",(run_id,)).fetchall()
    messages=messages_from_dict(json.loads(session['messages'])) if session else []
    visible=[]
    for m in messages[2:]:
        if isinstance(m,HumanMessage):
            try: visible.append({'role':'user','content':json.loads(m.content)['user_reply']})
            except (ValueError,KeyError,TypeError): pass
        elif isinstance(m,AIMessage) and isinstance(m.content,str) and m.content and not m.tool_calls:
            visible.append({'role':'assistant','content':m.content})
    if messages and isinstance(messages[-1],ToolMessage) and not candidate:
        visible.append({'role':'system','content':'本轮未形成有效候选，请继续说明或使用下方手动方式。'})
    item=dict(candidate) if candidate else None
    if item:item['diff']=json.loads(item['diff'])
    history=[{**json.loads(d['payload']),'created':d['created']} for d in decisions]
    for item_history in history:item_history.pop('product',None)
    return {'version':dict(version) if version else None,'versions':[dict(v) for v in versions], 'intent_versions':[dict(v) for v in intents], 'history':history,'legacy':not bool(version),'messages':visible,'candidate':item,'status':run['status']}

def respond(run_id,user_text,mode='dialog',model_factory=None):
    if mode not in ('dialog','original') or not isinstance(user_text,str) or not user_text.strip() or len(user_text)>10000: raise ValueError('输入无效')
    with _run_lock(run_id):
        run=store.get(run_id)
        if run['status']!='awaiting_input_resolution' or not workflow.check_gate(run_id,'input_gate'): raise ValueError('当前任务不在输入确认点')
        base=store.input_digest(run['product'])
        with store.connect() as db:
            version=db.execute('SELECT digest FROM input_versions WHERE run_id=? ORDER BY revision DESC LIMIT 1',(run_id,)).fetchone()
            if not version or version['digest']!=base: raise ValueError('历史任务或当前输入版本不可核验，请使用原有手工方式')
            row=db.execute('SELECT base_digest,messages FROM input_conversations WHERE run_id=?',(run_id,)).fetchone()
        product_view={k:v for k,v in run['product'].items() if k not in ('test_scenarios','expected')}
        messages=messages_from_dict(json.loads(row['messages'])) if row and row['base_digest']==base else [SystemMessage(content=SYSTEM),HumanMessage(content=_dump({'original_query':run['query'],'product':product_view,'issues':run['issues']}))]
        messages.append(HumanMessage(content=_dump({'user_reply':user_text,'mode':mode,'current_query':run['query'],'current_issues':run['issues']})))
        if len(_dump(messages_to_dict(messages)).encode('utf-8'))>MAX_BYTES: raise ValueError('入口对话超出上下文限制')
        proposals=[]
        @tool('inspect_product')
        def inspect_product(pointer: str) -> str:
            """Read a specific product fact JSON pointer from the current snapshot."""
            try:
                parent,key,exists=_pointer(run['product'],pointer)
                return _dump({'pointer':pointer,'exists':exists,'value':parent[key] if exists else None})
            except ValueError as e:return _dump({'error':str(e)})
        @tool('preview_candidate')
        def preview_candidate(effective_query: str, operations: list[FactOperation]) -> str:
            """Preview an effective query and product facts; add needs operation, pointer, new_value, user_quote, reason; replace/remove also need exact old_value. No change is applied."""
            try:
                proposed,diff=preview(run['product'],effective_query,[x.model_dump(exclude_unset=True) for x in operations],user_text,mode)
                proposals.append({'query':effective_query,'product':proposed,'diff':diff,'user_quote':user_text})
                return _dump({'valid':True,'diff':diff,'effective_query':effective_query,'notice':'Candidate requires explicit user confirmation.'})
            except (ValueError,TypeError) as e:return _dump({'valid':False,'error':str(e)})
        key=os.environ.get('DEEPSEEK_API_KEY');base_url=os.environ.get('DEEPSEEK_BASE_URL','https://api.deepseek.com').rstrip('/')
        if model_factory is None:
            if not key or base_url!='https://api.deepseek.com': raise RuntimeError('DeepSeek 配置无效')
            model_factory=lambda:ChatDeepSeek(model=os.getenv('DEEPSEEK_MODEL','deepseek-flash'),api_key=key,base_url=base_url,max_tokens=4096,extra_body={'thinking':{'type':'disabled'}},max_retries=0,timeout=90)
        model=model_factory().bind_tools([inspect_product,preview_candidate],tool_choice='auto')
        with store.connect() as db:
            db.execute('DELETE FROM input_candidates WHERE run_id=?',(run_id,))
            db.execute('INSERT OR REPLACE INTO input_conversations VALUES(?,?,?,?)',(run_id,base,_dump(messages_to_dict(messages)),store.now()))
        for turn in range(MAX_ROUNDS):
            response=model.invoke(messages);messages.append(response)
            store.event(run_id,'intake_dialog_model',{'turn':turn+1,'usage':response.usage_metadata or {}})
            calls=response.tool_calls or [];invalid=response.invalid_tool_calls or []
            if len(calls)+len(invalid)>4: raise ValueError('单轮工具调用过多')
            for call in calls:
                handler={'inspect_product':inspect_product,'preview_candidate':preview_candidate}.get(call.get('name'))
                try: answer=handler.invoke(call['args']) if handler else _dump({'error':'unknown tool'})
                except (ValueError,TypeError) as exc: answer=_dump({'error':str(exc)[:200]})
                messages.append(ToolMessage(content=answer,tool_call_id=call['id']))
            for call in invalid: messages.append(ToolMessage(content=_dump({'error':'invalid tool arguments'}),tool_call_id=call['id']))
            if len(_dump(messages_to_dict(messages)).encode('utf-8'))>MAX_BYTES: raise ValueError('入口对话超出上下文限制')
            with store.connect() as db: db.execute('UPDATE input_conversations SET messages=?,updated=? WHERE run_id=?',(_dump(messages_to_dict(messages)),store.now(),run_id))
            if not calls and not invalid: break
        if proposals:
            candidate=proposals[-1]
            with store.connect() as db:
                db.execute('INSERT INTO input_candidates VALUES(?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,run_id,base,candidate['query'],_dump(candidate['product']),_dump(candidate['diff']),candidate['user_quote'],store.now()))
        return state(run_id)

def confirm(run_id,candidate_id,base_digest,key,actor='local_user',dispatch=False):
    if actor not in ('local_user','test_runner'):raise ValueError('invalid decision actor')
    with _run_lock(run_id),store.LOCK,store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        old=db.execute('SELECT kind,payload FROM decisions WHERE run_id=? AND idempotency_key=?',(run_id,key)).fetchone()
        if old:
            saved=json.loads(old['payload'])
            if old['kind']!='input_candidate' or saved.get('candidate_id')!=candidate_id or saved.get('base_digest')!=base_digest: raise ValueError('幂等键已用于其他操作')
            return False
        if not workflow.check_gate(run_id,'input_gate'): raise ValueError('确认点尚未持久化')
        run=db.execute('SELECT status,product FROM runs WHERE id=?',(run_id,)).fetchone()
        c=db.execute('SELECT * FROM input_candidates WHERE id=? AND run_id=?',(candidate_id,run_id)).fetchone()
        v=db.execute('SELECT revision,digest FROM input_versions WHERE run_id=? ORDER BY revision DESC LIMIT 1',(run_id,)).fetchone()
        if not run or run['status']!='awaiting_input_resolution' or not c or not v or c['base_digest']!=base_digest or v['digest']!=base_digest or store.input_digest(json.loads(run['product']))!=base_digest: raise ValueError('候选或商品版本已变化，请刷新')
        product=json.loads(c['product']);query=c['query'];_,errors=workflow.contract(product,query)
        if errors: raise ValueError('；'.join(errors))
        new_digest=store.input_digest(product);new_rev=v['revision']
        if new_digest!=base_digest:
            new_rev+=1;db.execute('INSERT INTO input_versions VALUES(?,?,?,?,?,?)',(run_id,new_rev,new_digest,v['revision'],c['product'],store.now()))
        intent=db.execute('SELECT revision,digest FROM intent_versions WHERE run_id=? ORDER BY revision DESC LIMIT 1',(run_id,)).fetchone()
        intent_hash=store.intent_digest(query);intent_rev=intent['revision'] if intent else None
        if intent and intent['digest']!=intent_hash:
            intent_rev+=1;db.execute('INSERT INTO intent_versions VALUES(?,?,?,?,?,?)',(run_id,intent_rev,intent_hash,intent['revision'],query,store.now()))
        payload={'action':'retry','user_query':query,'product':product,'candidate_id':candidate_id,'base_digest':base_digest,'new_digest':new_digest,'revision':new_rev,'diff':json.loads(c['diff']),'user_quote':c['user_quote'],'actor':actor}
        payload['intent_revision']=intent_rev;payload['intent_digest']=intent_hash
        db.execute('INSERT INTO decisions VALUES(?,?,?,?,?)',(run_id,key,'input_candidate',_dump(payload),store.now()))
        db.execute("UPDATE runs SET query=?,product=?,status='resuming',updated=? WHERE id=?",(query,c['product'],store.now(),run_id))
        if dispatch: store.enqueue_in_db(db,run_id,'workflow',{'mode':'resume','payload':{'action':'retry'}},'decision:'+run_id+':'+key)
        session=db.execute('SELECT messages FROM input_conversations WHERE run_id=?',(run_id,)).fetchone()
        if session:
            history=messages_from_dict(json.loads(session['messages']))
            history.append(HumanMessage(content=_dump({'confirmed_update':{'product':product,'effective_query':query,'product_revision':new_rev,'intent_revision':intent_rev}})))
            db.execute('UPDATE input_conversations SET base_digest=?,messages=?,updated=? WHERE run_id=?',(new_digest,_dump(messages_to_dict(history)),store.now(),run_id))
        db.execute('DELETE FROM input_candidates WHERE run_id=?',(run_id,))
    store.event(run_id,'input_candidate_confirmed',{'revision':new_rev,'digest':new_digest,'base_digest':base_digest,'actor':actor})
    return True
