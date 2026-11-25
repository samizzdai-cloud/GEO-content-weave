"""One durable local execution lane. Claimed calls are never replayed on boot."""
import json
import threading
from geo_workbench import store

_wake = threading.Event()
_stop = threading.Event()
_thread = None


def notify():
    _wake.set()


def enqueue(run_id, kind, payload, key):
    with store.LOCK, store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        wid = store.enqueue_in_db(db, run_id, kind, payload, key)
    notify()
    return wid


def claim():
    with store.LOCK, store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        # Also guards a second consumer within the same installation.
        if db.execute("SELECT 1 FROM work_items WHERE status='running'").fetchone():
            return None
        rows = db.execute("SELECT w.*,w.rowid AS sequence,r.product AS input_json FROM work_items w JOIN runs r ON r.id=w.run_id WHERE w.status='queued'").fetchall()
        if not rows:
            return None
        from geo_workbench.batches import brand_key
        def order(item):
            doc = json.loads(item['input_json'])
            product = doc.get('product') or {}
            return (item['created'], brand_key(doc.get('brand') or {}, item['run_id']),
                    str(product.get('name') or product.get('id') or '').casefold(), item['sequence'])
        row = min(rows, key=order)
        db.execute("UPDATE work_items SET status='running',updated=? WHERE id=? AND status='queued'", (store.now(), row['id']))
        return dict(row)


def process_one(workflow_provider=None, content_provider=None):
    row = claim()
    if not row:
        return False
    from geo_workbench import workflow, content, images
    rid = row['run_id']
    try:
        payload = json.loads(row['payload'])
        if row['kind'] == 'workflow':
            kwargs = {'provider': workflow_provider} if workflow_provider else {}
            workflow.execute(rid, payload['mode'], payload['payload'], **kwargs)
        elif row['kind'] == 'content':
            feedback = payload.get('feedback')
            if feedback:
                saved = [store.read_artifact(rid, a['id']) for a in store.artifacts(rid) if a['kind'] == 'content_review_feedback']
                if not any(x.get('attempt_id') == payload['attempt_id'] for x in saved):
                    content.save(rid, 'content_review_feedback', payload['attempt_id'], feedback)
            kwargs = {'provider_factory': content_provider} if content_provider else {}
            content.run_trial(rid, payload['attempt_id'], review=payload.get('review'), **kwargs)
        elif row['kind']=='image':
            images.process(payload['job_id'])
        else:
            raise ValueError('unknown work kind')
        run = store.get(rid)
        failed = run['status'] in ('blocked_input', 'failed') or (run['status'] == 'needs_review' and bool(run['error']))
        status, error = ('failed', run['error']) if failed else ('done', None)
        if row['kind']=='image':
            outcome=images.job(payload['job_id'])
            status,error=('failed',outcome['error']) if outcome['status']=='failed' else ('done',None)
    except Exception as exc:
        status, error = 'failed', f'执行失败：{type(exc).__name__}'
        if row['kind']!='image':store.set_status(rid, 'needs_review', error)
    with store.connect() as db:
        db.execute('UPDATE work_items SET status=?,error=?,updated=? WHERE id=?', (status, error, store.now(), row['id']))
    return True


def recover():
    """Call only with exclusive process ownership, before starting the worker."""
    with store.connect() as db:
        running = db.execute("SELECT * FROM work_items WHERE status='running'").fetchall()
        db.execute("UPDATE work_items SET status='interrupted',error='external_result_unknown',updated=? WHERE status='running'", (store.now(),))
        db.execute("UPDATE content_attempts SET status='needs_review',error='进程中断，未自动重放',updated=? WHERE status IN ('starting','generating') AND run_id NOT IN (SELECT run_id FROM work_items WHERE status='queued')", (store.now(),))
        stale = db.execute("SELECT id FROM runs WHERE status IN ('created','validating','researching','resuming','generating') AND id NOT IN (SELECT run_id FROM work_items WHERE status='queued')").fetchall()
    for row in stale:
        store.set_status(row['id'], 'needs_review', '上次运行中断；外部调用结果未知，未自动重放')
    # Index/file integrity is independent of business/checkpoint transactions.
    with store.connect() as db:
        metas = db.execute('SELECT id,run_id,path FROM artifacts').fetchall()
        waits = db.execute("SELECT id,status FROM runs WHERE status IN ('awaiting_input_resolution','awaiting_plan_approval')").fetchall()
    bad = set()
    indexed = set()
    for meta in metas:
        indexed.add(meta['path'])
        try:
            store.read_artifact(meta['run_id'], meta['id'])
        except (ValueError, KeyError, OSError, json.JSONDecodeError):
            bad.add(meta['run_id'])
            store.event(meta['run_id'], 'recovery_integrity', {'artifact_id': meta['id'], 'result': 'missing_or_corrupt'})
    from geo_workbench import workflow
    for row in waits:
        try:
            if not workflow.check_gate(row['id'], 'input_gate' if row['status'] == 'awaiting_input_resolution' else 'plan_gate'):
                bad.add(row['id'])
        except Exception:
            bad.add(row['id'])
    for path in (store.ROOT / 'runs').glob('*/*/*'):
        relative = path.relative_to(store.ROOT).as_posix()
        if path.is_file() and relative not in {p.replace('\\', '/') for p in indexed}:
            rid = path.relative_to(store.ROOT / 'runs').parts[0]
            if store.VALID.fullmatch(rid):
                store.event(rid, 'recovery_orphan', {'path': relative, 'action': 'retained_not_referenced'})
    for rid in bad:
        store.set_status(rid, 'needs_review', '恢复检查发现文件/摘要或确认 checkpoint 不一致；请人工核查')
        with store.connect() as db:
            db.execute("UPDATE work_items SET status='blocked',error='recovery_integrity',updated=? WHERE run_id=? AND status='queued'", (store.now(), rid))
    return {'interrupted': len(running), 'integrity_blocked': sorted(bad)}


def projection(run_id):
    r = store.get(run_id)
    with store.connect() as db:
        row = db.execute('SELECT * FROM work_items WHERE run_id=? ORDER BY created DESC,rowid DESC LIMIT 1', (run_id,)).fetchone()
    legacy_complete=r['status']=='needs_review' and r['error']=='试生成已完成，需人工审核；检查项可能包含未通过或未知。'
    if r['status']=='completed':
        state,action='completed','已完成，可下载和继续编辑'
    elif legacy_complete:
        state,action='content_ready_for_review','生成完成，等待人工审核'
    elif row and row['status'] in ('queued', 'running', 'interrupted', 'blocked', 'failed'):
        state = row['status']
        action = {'queued': '等待顺序执行', 'running': '执行中', 'interrupted': '执行中断，待 review；外部结果未知', 'blocked': '数据不一致；待 review', 'failed': '执行失败，待 review；检查错误与现有产物'}[state]
    else:
        state, action = {
            'awaiting_input_resolution': ('waiting_input', '处理输入问题'),
            'awaiting_plan_approval': ('waiting_plan', '审阅并确认规划'),
            'plan_approved': ('plan_approved', '可明确启动内容试生成'),
            'needs_review': ('content_ready_for_review' if store.content_attempts(run_id) and not r['error'] else 'needs_review', '审阅试稿与核查结果' if not r['error'] else '检查错误和现有产物'),
            'blocked_input': ('invalid', '修正输入并新建任务'),
            'cancelled': ('cancelled', '已取消'),
        }.get(r['status'], (r['status'], '查看任务详情'))
    return {'state': state, 'next_action': action, 'status': r['status'], 'error': None if legacy_complete else r['error'], 'work_id': row['id'] if row else None}


def retry(run_id, work_id, key):
    with store.LOCK, store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('SELECT * FROM work_items WHERE id=? AND run_id=?', (work_id, store.valid_run(run_id))).fetchone()
        if not row:
            raise KeyError('任务请求不存在')
        unique = 'retry:' + run_id + ':' + key
        prior = db.execute('SELECT payload,kind,id FROM work_items WHERE unique_key=?', (unique,)).fetchone()
        if prior:
            if prior['payload'] != row['payload'] or prior['kind'] != row['kind']:
                raise ValueError('幂等键已用于其他请求')
            return prior['id']
        latest = db.execute('SELECT id FROM work_items WHERE run_id=? ORDER BY created DESC,rowid DESC LIMIT 1', (run_id,)).fetchone()
        if latest['id'] != work_id or row['status'] not in ('interrupted','failed'):
            raise ValueError('只可重试当前失败或中断请求，请刷新')
        # A start can safely run afresh as a new research version; resume needs its original gate.
        payload = json.loads(row['payload'])
        run=store.get(run_id)
        if row['status']=='failed' and (row['kind']!='workflow' or payload['mode']!='start' or run['plan_rev'] or not run['error'] or 'OpenAIConnectionError' not in run['error']):
            raise ValueError('该失败需检查现有产物；内容请使用新一轮试生成')
        if row['kind'] == 'workflow' and payload['mode'] == 'resume':
            from geo_workbench import workflow
            gate = 'input_gate' if payload['payload'].get('action') == 'retry' else 'plan_gate'
            if not workflow.check_gate(run_id, gate):
                raise ValueError('原确认点已移动，不能重放该恢复请求；请核查 checkpoint')
        wid = store.enqueue_in_db(db, run_id, row['kind'], payload, unique)
        db.execute("UPDATE runs SET status=?,error=NULL,updated=? WHERE id=?", ('resuming' if row['kind'] == 'workflow' else 'generating', store.now(), run_id))
    notify()
    return wid


def start():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    def loop():
        while not _stop.is_set():
            _wake.clear()
            try:
                if process_one():
                    continue
            except Exception:
                # Keep durable work available; do not crash the sole worker silently.
                pass
            _wake.wait(1)
    _thread = threading.Thread(target=loop, name='geo-durable-worker', daemon=True)
    _thread.start()


def stop():
    _stop.set()
    notify()
    if _thread:
        _thread.join(timeout=1)
    return not _thread or not _thread.is_alive()
