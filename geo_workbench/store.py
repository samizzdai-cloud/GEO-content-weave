"""Run state, immutable artifacts and durable events for a local installation."""
from __future__ import annotations
import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(os.environ.get('GEO_DATA_DIR', Path(__file__).resolve().parents[1] / '.local-data')).resolve()
LOCK = threading.RLock()
VALID = re.compile(r'^[a-f0-9]{32}$')

def now():
    return datetime.now(timezone.utc).isoformat()

def init():
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / 'runs').mkdir(exist_ok=True)
    with connect() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,status TEXT NOT NULL,query TEXT NOT NULL,product TEXT NOT NULL,issues TEXT NOT NULL DEFAULT '[]',plan_rev INTEGER NOT NULL DEFAULT 0,plan_hash TEXT, created TEXT NOT NULL,updated TEXT NOT NULL,error TEXT);
        CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,kind TEXT NOT NULL,revision INTEGER NOT NULL,path TEXT NOT NULL,digest TEXT NOT NULL,created TEXT NOT NULL, UNIQUE(run_id,kind,revision));
        CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,run_id TEXT NOT NULL,time TEXT NOT NULL,type TEXT NOT NULL,detail TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS decisions(run_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(run_id,idempotency_key));
        CREATE TABLE IF NOT EXISTS content_attempts(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,idempotency_key TEXT NOT NULL,plan_revision INTEGER NOT NULL,plan_digest TEXT NOT NULL,status TEXT NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL,error TEXT, UNIQUE(run_id,idempotency_key));
        CREATE TABLE IF NOT EXISTS input_versions(run_id TEXT NOT NULL,revision INTEGER NOT NULL,digest TEXT NOT NULL,parent_revision INTEGER,product TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(run_id,revision));
        CREATE TABLE IF NOT EXISTS intent_versions(run_id TEXT NOT NULL,revision INTEGER NOT NULL,digest TEXT NOT NULL,parent_revision INTEGER,query TEXT NOT NULL,created TEXT NOT NULL,PRIMARY KEY(run_id,revision));
        CREATE TABLE IF NOT EXISTS input_conversations(run_id TEXT PRIMARY KEY,base_digest TEXT NOT NULL,messages TEXT NOT NULL,updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS input_candidates(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,base_digest TEXT NOT NULL,query TEXT NOT NULL,product TEXT NOT NULL,diff TEXT NOT NULL,user_quote TEXT NOT NULL,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,idempotency_key TEXT UNIQUE NOT NULL,request_digest TEXT NOT NULL,snapshot TEXT NOT NULL,query TEXT NOT NULL,created TEXT NOT NULL,schema_version TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS batch_items(batch_id TEXT NOT NULL,position INTEGER NOT NULL,run_id TEXT UNIQUE,detail TEXT NOT NULL,selection TEXT NOT NULL,PRIMARY KEY(batch_id,position));
        CREATE TABLE IF NOT EXISTS work_items(id TEXT PRIMARY KEY,run_id TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,unique_key TEXT UNIQUE NOT NULL,status TEXT NOT NULL,created TEXT NOT NULL,updated TEXT NOT NULL,error TEXT);
        CREATE INDEX IF NOT EXISTS work_pending ON work_items(status,created);
        ''')

class ClosingConnection(sqlite3.Connection):
    def __exit__(self, typ, value, traceback):
        try:
            return super().__exit__(typ, value, traceback)
        finally:
            self.close()

def connect():
    db = sqlite3.connect(ROOT / 'business.sqlite', timeout=20, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    return db

def valid_run(run_id):
    if not VALID.fullmatch(run_id):
        raise ValueError('invalid run id')
    return run_id

def event(run_id, kind, detail):
    with connect() as db:
        db.execute('INSERT INTO events(run_id,time,type,detail) VALUES(?,?,?,?)', (valid_run(run_id), now(), kind, json.dumps(detail, ensure_ascii=False)))

def create(product, query):
    with connect() as db:
        rid = create_in_db(db, product, query)
    event(rid,'run_created',{})
    return rid

def create_in_db(db, product, query):
    rid = uuid.uuid4().hex
    t = now()
    db.execute('INSERT INTO runs(id,status,query,product,created,updated) VALUES(?,?,?,?,?,?)',(rid,'created',query,json.dumps(product,ensure_ascii=False),t,t))
    db.execute('INSERT INTO input_versions VALUES(?,?,?,?,?,?)',(rid,1,input_digest(product),None,json.dumps(product,ensure_ascii=False),t))
    db.execute('INSERT INTO intent_versions VALUES(?,?,?,?,?,?)',(rid,1,intent_digest(query),None,query,t))
    return rid

def enqueue_in_db(db, run_id, kind, payload, key):
    value=json.dumps(payload,ensure_ascii=False,sort_keys=True)
    prior=db.execute('SELECT * FROM work_items WHERE unique_key=?',(key,)).fetchone()
    if prior:
        if prior['run_id']!=run_id or prior['kind']!=kind or prior['payload']!=value:raise ValueError('queue key already used')
        return prior['id']
    wid=uuid.uuid4().hex;t=now()
    db.execute('INSERT INTO work_items VALUES(?,?,?,?,?,?,?,?,?)',(wid,valid_run(run_id),kind,value,key,'queued',t,t,None))
    return wid

def input_digest(product):
    return hashlib.sha256(json.dumps(product,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')).hexdigest()

def intent_digest(query):return hashlib.sha256(query.encode('utf-8')).hexdigest()

def get(run_id):
    with connect() as db:
        row=db.execute('SELECT * FROM runs WHERE id=?',(valid_run(run_id),)).fetchone()
    if not row:
        raise KeyError('run not found')
    out=dict(row)
    out['product']=json.loads(out['product']);out['issues']=json.loads(out['issues'])
    return out

def set_status(run_id, status, error=None, issues=None):
    with connect() as db:
        if issues is None:
            db.execute('UPDATE runs SET status=?,updated=?,error=? WHERE id=?',(status,now(),error,valid_run(run_id)))
        else:
            db.execute('UPDATE runs SET status=?,updated=?,error=?,issues=? WHERE id=?',(status,now(),error,json.dumps(issues,ensure_ascii=False),valid_run(run_id)))
    event(run_id,'status',{'status':status,'error':error})

def put(run_id, kind, value, markdown=False):
    valid_run(run_id)
    if not re.fullmatch(r'[a-z_]+', kind): raise ValueError('invalid kind')
    data=(str(value) if markdown else json.dumps(value,ensure_ascii=False,indent=2)).encode('utf-8')
    digest=hashlib.sha256(data).hexdigest(); aid=uuid.uuid4().hex
    folder=ROOT/'runs'/run_id/kind
    folder.mkdir(parents=True,exist_ok=True)
    with LOCK, connect() as db:
        rev=db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM artifacts WHERE run_id=? AND kind=?',(run_id,kind)).fetchone()[0]
        dest=folder/f'{rev:04d}-{aid}.{"md" if markdown else "json"}'; tmp=folder/f'.{aid}.tmp'
        tmp.write_bytes(data);os.replace(tmp,dest)
        db.execute('INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)',(aid,run_id,kind,rev,str(dest.relative_to(ROOT)),digest,now()))
        if kind=='plan': db.execute('UPDATE runs SET plan_rev=?,plan_hash=?,updated=? WHERE id=?',(rev,digest,now(),run_id))
    event(run_id,'artifact',{'id':aid,'kind':kind,'revision':rev,'sha256':digest})
    return {'id':aid,'revision':rev,'sha256':digest}

def artifacts(run_id):
    with connect() as db:
        rows=db.execute('SELECT id,kind,revision,digest,created FROM artifacts WHERE run_id=? ORDER BY created',(valid_run(run_id),)).fetchall()
    return [dict(x) for x in rows]

def artifact_meta(run_id, aid):
    with connect() as db:
        row=db.execute('SELECT id,kind,revision,digest,created FROM artifacts WHERE run_id=? AND id=?',(valid_run(run_id),aid)).fetchone()
    if not row: raise KeyError('artifact not found')
    return dict(row)

def content_attempt(run_id, attempt_id):
    with connect() as db:
        row=db.execute('SELECT * FROM content_attempts WHERE run_id=? AND id=?',(valid_run(run_id),attempt_id)).fetchone()
    if not row: raise KeyError('content attempt not found')
    return dict(row)

def content_attempts(run_id):
    with connect() as db:
        rows=db.execute('SELECT * FROM content_attempts WHERE run_id=? ORDER BY created',(valid_run(run_id),)).fetchall()
    return [dict(row) for row in rows]

def reserve_content(run_id, key, revision, digest, queue_payload=None):
    """Claim one explicit start, transactionally and independently of graph state."""
    valid_run(run_id)
    with LOCK, connect() as db:
        db.execute('BEGIN IMMEDIATE')
        prior=db.execute('SELECT * FROM content_attempts WHERE run_id=? AND idempotency_key=?',(run_id,key)).fetchone()
        if prior:
            if prior['plan_revision']!=revision or prior['plan_digest']!=digest: raise ValueError('idempotency key already used')
            return dict(prior),False
        row=db.execute('SELECT status,plan_rev,plan_hash FROM runs WHERE id=?',(run_id,)).fetchone()
        prior_trial=db.execute('SELECT 1 FROM content_attempts WHERE run_id=? LIMIT 1',(run_id,)).fetchone()
        if not row or not (row['status']=='plan_approved' or (row['status'] in ('needs_review','completed') and prior_trial)): raise ValueError('需要已批准规划且没有其他内容任务运行')
        if row['plan_rev']!=revision or row['plan_hash']!=digest: raise ValueError('批准的规划版本已变化')
        aid=uuid.uuid4().hex;t=now()
        db.execute('INSERT INTO content_attempts VALUES(?,?,?,?,?,?,?,?,?)',(aid,run_id,key,revision,digest,'starting',t,t,None))
        db.execute("UPDATE runs SET status='generating',updated=?,error=NULL WHERE id=?",(t,run_id))
        if queue_payload is not None:
            enqueue_in_db(db,run_id,'content',{'attempt_id':aid,**queue_payload},'content:'+aid)
    event(run_id,'content_start',{'attempt_id':aid,'plan_revision':revision,'plan_digest':digest})
    return content_attempt(run_id,aid),True

def set_content_status(run_id, attempt_id, status, error=None):
    with connect() as db:
        db.execute('UPDATE content_attempts SET status=?,updated=?,error=? WHERE run_id=? AND id=?',(status,now(),error,valid_run(run_id),attempt_id))
    set_status(run_id,status,error)
    event(run_id,'content_status',{'attempt_id':attempt_id,'status':status,'error':error})

def read_artifact(run_id, aid):
    valid_run(run_id)
    with connect() as db:
        row=db.execute('SELECT * FROM artifacts WHERE run_id=? AND id=?',(run_id,aid)).fetchone()
    if not row: raise KeyError('artifact not found')
    path=(ROOT/row['path']).resolve()
    if not path.is_relative_to((ROOT/'runs'/run_id).resolve()): raise ValueError('invalid artifact path')
    data=path.read_bytes()
    if hashlib.sha256(data).hexdigest()!=row['digest']: raise ValueError('artifact integrity failure')
    return data.decode('utf-8') if path.suffix=='.md' else json.loads(data)

def latest(run_id, kind):
    with connect() as db:
        row=db.execute('SELECT id FROM artifacts WHERE run_id=? AND kind=? ORDER BY revision DESC LIMIT 1',(valid_run(run_id),kind)).fetchone()
    return read_artifact(run_id,row['id']) if row else None

def events(run_id):
    with connect() as db:
        rows=db.execute('SELECT id,time,type,detail FROM events WHERE run_id=? ORDER BY id',(valid_run(run_id),)).fetchall()
    return [{**dict(x),'detail':json.loads(x['detail'])} for x in rows]

def decisions(run_id,key,kind,payload,expected_status,revision=None,digest=None,actor='local_user',dispatch=False):
    """Atomically claim a user decision and reject stale versions."""
    valid_run(run_id)
    stored_payload=dict(payload)
    if actor not in ('local_user','test_runner'): raise ValueError('invalid decision actor')
    if kind=='plan_decision': stored_payload['_approval']={'revision':revision,'digest':digest,'actor':actor}
    value=json.dumps(stored_payload,ensure_ascii=False,sort_keys=True)
    with LOCK,connect() as db:
        db.execute('BEGIN IMMEDIATE')
        prior=db.execute('SELECT kind,payload FROM decisions WHERE run_id=? AND idempotency_key=?',(run_id,key)).fetchone()
        if prior:
            legacy=json.dumps(payload,ensure_ascii=False,sort_keys=True)
            if prior['kind']!=kind or prior['payload'] not in (value,legacy): raise ValueError('idempotency key already used')
            return False
        row=db.execute('SELECT status,plan_rev,plan_hash FROM runs WHERE id=?',(run_id,)).fetchone()
        if not row or row['status']!=expected_status: raise ValueError('state changed; reload')
        if revision is not None and (row['plan_rev']!=revision or row['plan_hash']!=digest): raise ValueError('plan version changed; reload')
        db.execute('INSERT INTO decisions VALUES(?,?,?,?,?)',(run_id,key,kind,value,now()))
        if kind=='input_resolution' and payload.get('action')=='retry':
            intent=db.execute('SELECT revision,digest FROM intent_versions WHERE run_id=? ORDER BY revision DESC LIMIT 1',(run_id,)).fetchone()
            intent_hash=intent_digest(payload['user_query'])
            if intent and intent['digest']!=intent_hash:db.execute('INSERT INTO intent_versions VALUES(?,?,?,?,?,?)',(run_id,intent['revision']+1,intent_hash,intent['revision'],payload['user_query'],now()))
            db.execute('UPDATE runs SET query=? WHERE id=?',(payload['user_query'],run_id))
            if payload.get('product') is not None:
                current=db.execute('SELECT revision,digest FROM input_versions WHERE run_id=? ORDER BY revision DESC LIMIT 1',(run_id,)).fetchone()
                digest_value=input_digest(payload['product'])
                if current and current['digest']!=digest_value:
                    db.execute('INSERT INTO input_versions VALUES(?,?,?,?,?,?)',(run_id,current['revision']+1,digest_value,current['revision'],json.dumps(payload['product'],ensure_ascii=False),now()))
                db.execute('UPDATE runs SET product=? WHERE id=?',(json.dumps(payload['product'],ensure_ascii=False),run_id))
            db.execute('DELETE FROM input_candidates WHERE run_id=?',(run_id,))
            session=db.execute('SELECT messages FROM input_conversations WHERE run_id=?',(run_id,)).fetchone()
            if session:
                from langchain_core.messages import HumanMessage, messages_from_dict, messages_to_dict
                messages=messages_from_dict(json.loads(session['messages']))
                current_product=payload.get('product') or json.loads(db.execute('SELECT product FROM runs WHERE id=?',(run_id,)).fetchone()['product'])
                messages.append(HumanMessage(content=json.dumps({'confirmed_manual_update':{'product':current_product,'effective_query':payload['user_query']}},ensure_ascii=False)))
                db.execute('UPDATE input_conversations SET base_digest=?,messages=?,updated=? WHERE run_id=?',(input_digest(current_product),json.dumps(messages_to_dict(messages),ensure_ascii=False),now(),run_id))
        db.execute('UPDATE runs SET status=?,updated=? WHERE id=?',('resuming',now(),run_id))
        if dispatch: enqueue_in_db(db,run_id,'workflow',{'mode':'resume','payload':payload},'decision:'+run_id+':'+key)
    event(run_id,'decision',{'kind':kind,'revision':revision,'digest':digest,'actor':actor})
    return True
