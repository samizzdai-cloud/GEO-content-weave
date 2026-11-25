import copy
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from geo_workbench import batches, store, queue, workflow, ownership
from geo_workbench.app import app, TOKEN
from test_stage_a import FakeProvider, LUMA, ORBIT


def file(doc, name='input.json'):
    return {'filename': name, 'text': json.dumps(doc, ensure_ascii=False)}


def multi():
    doc = copy.deepcopy(LUMA)
    doc['schema_version'] = '2.0'
    doc['products'] = [doc.pop('product')]
    doc['products'].append(copy.deepcopy(doc['products'][0]))
    doc['products'][1]['name'] = 'Second shirt'
    return doc


class StageC(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = store.ROOT
        store.ROOT = Path(self.tmp.name)
        store.init()
        self.env = patch.dict(os.environ, {'GEO_WORKER_DISABLED': '1'})
        self.env.start()

    def tearDown(self):
        queue.stop()
        ownership.release()
        self.env.stop()
        store.ROOT = self.old
        self.tmp.cleanup()

    def test_legacy_inputs_preserved_and_invalid_file_does_not_block(self):
        result = batches.preflight([file(LUMA), {'filename': 'bad.json', 'text': '{'}, file(ORBIT)], {}, 'normal')
        self.assertEqual((result['valid_count'], result['invalid_count']), (2, 1))
        effective = result['items'][0]['effective']
        for key in ('brand', 'product', 'content_requests', 'target_audience', 'suggested_paths', 'unsupported_claims'):
            self.assertEqual(effective[key], LUMA[key])
        self.assertEqual(result['items'][2]['name'], ORBIT['product']['name'])

    def test_product_unit_sku_isolation_price_and_provenance(self):
        doc = multi()
        doc['brand']['product_defaults'] = {'facts': {'shared': True, 'price': {'value': 10, 'currency': 'EUR'}}}
        doc['products'][0]['skus'] = [{'id': 'white', 'facts': {'price': {'value': 0, 'currency': 'USD'}, 'available': False}}]
        result = batches.preflight([file(doc)], {}, 'normal')
        self.assertEqual(result['valid_count'], 2)
        item = result['items'][0]
        self.assertEqual(item['effective']['product']['facts']['price']['value'], 59)
        sku = item['variants'][0]
        self.assertEqual(sku['effective']['product']['facts']['price']['value'], 0)
        self.assertIs(sku['effective']['product']['facts']['available'], False)
        self.assertEqual(item['provenance']['/product/facts/shared']['source_pointer'], '/brand/product_defaults/facts/shared')
        self.assertEqual(sku['provenance']['/product/facts/price/value']['source_pointer'], '/products/0/skus/0/facts/price/value')
        self.assertEqual(item['provenance']['/product/facts/material']['source_pointer'], '/products/0/facts/material')
        # Every provenance source pointer resolves to the original file/defaults, never the normalized wrapper.
        for view, refs in [(item['effective'], item['provenance']), (sku['effective'], sku['provenance'])]:
            for dest, ref in refs.items():
                root = {} if ref['source_id'] == 'batch_defaults' else doc
                for key in ref['source_pointer'].split('/')[1:]:
                    key = key.replace('~1', '/').replace('~0', '~')
                    root = root[int(key)] if isinstance(root, list) else root[key]
                self.assertIsNotNone(root)

    def test_merge_arrays_null_ambiguous_price_and_partial_errors(self):
        doc = multi()
        doc['products'][0]['facts']['price'] = {'value': 20}
        result = batches.preflight([file(doc)], {}, 'normal')
        self.assertFalse(result['items'][0]['valid'])
        self.assertTrue(result['items'][1]['valid'])
        doc['products'][0]['facts']['price'] = None
        self.assertIn('null', batches.preflight([file(doc)], {}, 'normal')['items'][0]['error'])
        doc = multi()
        doc['brand']['style'] = {'tone': 'quiet', 'colors': ['green']}
        doc['products'][0]['style'] = {'tone': '', 'colors': []}
        item = batches.preflight([file(doc)], {}, 'normal')['items'][0]
        self.assertEqual(item['effective']['style'], {'tone': '', 'colors': []})
        with self.assertRaises(ValueError):
            batches.preflight([file(doc)], {'facts': {'material': 'silk'}}, 'normal')
        doc['product'] = doc['products'][0]
        self.assertEqual(batches.preflight([file(doc)], {}, 'normal')['valid_count'], 0)

    def test_same_brand_group_different_snapshots_and_repeated_ids(self):
        other = copy.deepcopy(LUMA)
        other['brand']['name'] = '  luma  '
        other['brand']['style'] = {'tone': 'loud'}
        result = batches.preflight([file(LUMA), file(other)], {}, 'normal')
        a, b = result['items']
        self.assertEqual(a['brand_key'], b['brand_key'])
        self.assertNotIn('style', a['effective'])
        self.assertEqual(b['effective']['style']['tone'], 'loud')
        bid, _ = batches.create([file(LUMA), file(other)], {}, 'normal', 'same-brand')
        ids = [i['run_id'] for i in batches.get(bid)['items']]
        self.assertEqual(len(set(ids)), 2)
        self.assertNotEqual(store.input_digest(store.get(ids[0])['product']), store.input_digest(store.get(ids[1])['product']))

    def test_transactional_create_idempotency_and_rollback(self):
        files = [file(multi())]
        bid, fresh = batches.create(files, {}, 'normal', 'one')
        self.assertTrue(fresh)
        self.assertEqual(batches.create(files, {}, 'normal', 'one'), (bid, False))
        with self.assertRaises(ValueError):
            batches.create(files, {}, 'changed', 'one')
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM work_items').fetchone()[0], 2)
        with patch.object(store, 'enqueue_in_db', side_effect=RuntimeError('crash before commit')):
            with self.assertRaises(RuntimeError):
                batches.create(files, {}, 'normal', 'crash')
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM runs').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT count(*) FROM batches').fetchone()[0], 1)

    def test_waiting_first_product_releases_lane_and_approval_is_independent(self):
        bid, _ = batches.create([file(LUMA), file(ORBIT)], {}, 'write wrinkle-free', 'wait')
        # Same batch request, different per-product audit results; gates remain independent.
        ids = [i['run_id'] for i in batches.get(bid)['items']]
        class SelectiveProvider(FakeProvider):
            @staticmethod
            def audit_input(run_id, product, query):
                return FakeProvider.audit_input(run_id, product, query) if product['product']['category'].startswith('apparel') else {'issues': []}
        self.assertTrue(queue.process_one(workflow_provider=SelectiveProvider))
        self.assertEqual(store.get(ids[0])['status'], 'awaiting_input_resolution')
        self.assertTrue(queue.process_one(workflow_provider=SelectiveProvider))
        self.assertEqual(store.get(ids[1])['status'], 'awaiting_plan_approval')
        self.assertFalse(queue.process_one(workflow_provider=FakeProvider))
        r = store.get(ids[1])
        store.decisions(ids[1], 'approve', 'plan_decision', {'action': 'approve'}, 'awaiting_plan_approval', r['plan_rev'], r['plan_hash'], actor='test_runner', dispatch=True)
        queue.process_one(workflow_provider=FakeProvider)
        self.assertEqual(store.get(ids[1])['status'], 'plan_approved')
        self.assertEqual(store.get(ids[0])['status'], 'awaiting_input_resolution')
        self.assertIsNone(store.latest(ids[1], 'content_ir'))
        self.assertEqual(batches.get(bid)['counts'], {'waiting_input': 1, 'plan_approved': 1})

    def test_equal_timestamps_order_by_brand_product_then_insertion(self):
        later = copy.deepcopy(LUMA)
        later['product']['name'] = 'Z shirt'
        earlier = copy.deepcopy(LUMA)
        earlier['product']['name'] = 'A shirt'
        with patch.object(store, 'now', return_value='2026-11-24T00:00:00+00:00'):
            bid, _ = batches.create([file(ORBIT), file(later), file(earlier), file(earlier)], {}, 'normal', 'ordering')
        ids = [i['run_id'] for i in batches.get(bid)['items']]
        actual = []
        for _ in ids:
            row = queue.claim();actual.append(row['run_id'])
            with store.connect() as db:
                db.execute("UPDATE work_items SET status='done' WHERE id=?", (row['id'],))
        self.assertEqual(actual, [ids[2], ids[3], ids[1], ids[0]])

    def test_failed_product_waits_for_review_other_product_continues(self):
        class FailingProvider(FakeProvider):
            @staticmethod
            def audience(run_id, contract):
                if contract['product']['category'].startswith('apparel'):
                    raise RuntimeError('fixture failure')
                return FakeProvider.audience(run_id, contract)
        bid, _ = batches.create([file(LUMA), file(ORBIT)], {}, 'normal', 'failure')
        while queue.process_one(workflow_provider=FailingProvider):
            pass
        items = batches.get(bid)['items']
        self.assertEqual(store.get(items[0]['run_id'])['status'], 'needs_review')
        self.assertEqual(items[1]['state'], 'waiting_plan')
        self.assertIsNone(store.latest(items[0]['run_id'], 'plan'))

    def test_connection_failure_can_be_explicitly_retried_without_new_import(self):
        class OpenAIConnectionError(RuntimeError):
            pass
        class Offline(FakeProvider):
            @staticmethod
            def audit_input(run_id, product, query):
                raise OpenAIConnectionError('fixture only')
        bid, _ = batches.create([file(LUMA)], {}, 'normal', 'offline')
        rid = batches.get(bid)['items'][0]['run_id']
        queue.process_one(workflow_provider=Offline)
        view=queue.projection(rid)
        self.assertEqual(view['state'],'failed')
        wid=queue.retry(rid,view['work_id'],'explicit-retry')
        self.assertEqual(queue.retry(rid,view['work_id'],'explicit-retry'),wid)
        queue.process_one(workflow_provider=FakeProvider)
        self.assertEqual(store.get(rid)['status'],'awaiting_plan_approval')
        self.assertEqual(batches.get(bid)['items'][0]['run_id'],rid)
        with self.assertRaises(ValueError):
            queue.retry(rid,view['work_id'],'stale-retry')

    def test_same_id_historical_json_never_fills_current_input_or_approval(self):
        old = copy.deepcopy(LUMA)
        old['brand']['style'] = {'tone': 'historical only'}
        old_run = store.create(old, 'old task')
        store.set_status(old_run, 'plan_approved')
        fresh = copy.deepcopy(LUMA)
        del fresh['product']['facts']['price']
        bid, _ = batches.create([file(fresh)], {}, 'normal', 'fresh-upload')
        item = batches.get(bid)['items'][0]
        self.assertNotEqual(item['run_id'], old_run)
        self.assertNotIn('price', store.get(item['run_id'])['product']['product']['facts'])
        self.assertNotIn('style', item['effective'])
        queue.process_one(workflow_provider=FakeProvider)
        self.assertEqual(store.get(item['run_id'])['status'], 'awaiting_plan_approval')
        with store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM decisions WHERE run_id=?', (item['run_id'],)).fetchone()[0], 0)

    def test_batch_content_queue_keeps_sources_reports_and_pages_separate(self):
        from test_stage_b import ProviderA, ProviderB
        bid, _ = batches.create([file(multi(), 'multi-fixture.json')], {}, 'normal', 'content-lane')
        ids = [i['run_id'] for i in batches.get(bid)['items']]
        while queue.process_one(workflow_provider=ProviderA):
            pass
        for index, rid in enumerate(ids):
            r = store.get(rid)
            store.decisions(rid, 'approve', 'plan_decision', {'action': 'approve'}, 'awaiting_plan_approval', r['plan_rev'], r['plan_hash'], actor='test_runner', dispatch=True)
        while queue.process_one(workflow_provider=ProviderA):
            pass
        with TestClient(app) as client:
            for rid in ids:
                r = store.get(rid)
                response = client.post(f'/api/runs/{rid}/content-start', headers={'x-geo-token': TOKEN}, json={'revision': r['plan_rev'], 'digest': r['plan_hash'], 'idempotency_key': 'write'})
                self.assertEqual(response.status_code, 200, response.text)
            while queue.process_one(content_provider=ProviderB):
                pass
            for index, rid in enumerate(ids):
                report = store.latest(rid, 'content_report')['data']['markdown']
                self.assertIn(f'/products/{index}/facts/type', report)
                self.assertIn('multi-fixture.json', report)
                pages = store.latest(rid, 'content_pages')['data']['pages']
                name = store.get(rid)['product']['product']['name']
                self.assertIn(name, pages['blog'])
                other = store.get(ids[1-index])['product']['product']['name']
                self.assertNotIn(other, pages['blog'])
                listed = next(item for item in client.get('/api/runs').json() if item['id']==rid)
                self.assertEqual(listed['source_pointer'], f'/products/{index}')

    def test_restart_queue_waiting_and_unknown_not_replayed(self):
        bid, _ = batches.create([file(multi())], {}, 'normal', 'restart')
        first = queue.claim()
        self.assertIsNotNone(first)
        self.assertIsNone(queue.claim())
        result = queue.recover()
        self.assertEqual(result['interrupted'], 1)
        items = batches.get(bid)['items']
        self.assertEqual(items[0]['state'], 'interrupted')
        self.assertEqual(items[1]['state'], 'queued')
        queue.process_one(workflow_provider=FakeProvider)
        rid = items[1]['run_id']
        queue.recover()
        self.assertEqual(store.get(rid)['status'], 'awaiting_plan_approval')
        with patch.object(workflow, 'execute') as execute:
            self.assertFalse(queue.process_one())
            execute.assert_not_called()
        wid = queue.retry(items[0]['run_id'], first['id'], 'retry')
        self.assertEqual(queue.retry(items[0]['run_id'], first['id'], 'retry'), wid)
        with self.assertRaises(ValueError):
            queue.retry(items[0]['run_id'], first['id'], 'another')
        queue.process_one(workflow_provider=FakeProvider)
        self.assertEqual(store.get(items[0]['run_id'])['status'], 'awaiting_plan_approval')

    def test_recovery_integrity_blocks_only_affected_item_and_keeps_orphans(self):
        bid, _ = batches.create([file(multi())], {}, 'normal', 'integrity')
        ids = [i['run_id'] for i in batches.get(bid)['items']]
        meta = store.put(ids[0], 'test', {'x': 1})
        with store.connect() as db:
            path = db.execute('SELECT path FROM artifacts WHERE id=?', (meta['id'],)).fetchone()['path']
        (store.ROOT / path).write_text('corrupt')
        orphan = store.ROOT / 'runs' / ids[1] / 'test' / 'orphan.json'
        orphan.parent.mkdir(parents=True); orphan.write_text('{}')
        result = queue.recover()
        self.assertEqual(result['integrity_blocked'], [ids[0]])
        self.assertTrue(orphan.exists())
        queue.process_one(workflow_provider=FakeProvider)
        self.assertIsNone(store.latest(ids[0], 'plan'))
        self.assertEqual(store.get(ids[1])['status'], 'awaiting_plan_approval')
        self.assertTrue(any(e['type'] == 'recovery_orphan' for e in store.events(ids[1])))

    def test_http_preview_selection_and_stale_approval(self):
        headers = {'x-geo-token': TOKEN}
        request = {'files': [file(multi()), {'filename': 'bad.json', 'text': '{'}], 'user_query': 'normal'}
        with TestClient(app) as client:
            preview = client.post('/api/batches/preview', headers=headers, json=request)
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.json()['valid_count'], 2)
            start = client.post('/api/batches', headers=headers, json={**request, 'idempotency_key': 'http', 'selected': [1]})
            self.assertEqual(start.status_code, 200)
            bid = start.json()['id']
            result = client.get('/api/batches/' + bid).json()
            self.assertEqual(result['counts'], {'not_started': 1, 'queued': 1, 'invalid': 1})
            rid = result['items'][1]['run_id']
            queue.process_one(workflow_provider=FakeProvider)
            r = store.get(rid)
            data = {'action': 'approve', 'revision': r['plan_rev'], 'digest': 'stale', 'idempotency_key': 'wrong'}
            self.assertEqual(client.post(f'/api/runs/{rid}/decision', headers=headers, json=data).status_code, 409)
            data['digest'] = r['plan_hash']
            self.assertEqual(client.post(f'/api/runs/{rid}/decision', headers=headers, json=data).status_code, 200)
            self.assertTrue(client.post(f'/api/runs/{rid}/decision', headers=headers, json=data).json()['duplicate'])
            queue.process_one(workflow_provider=FakeProvider)
            self.assertEqual(store.get(rid)['status'], 'plan_approved')
            self.assertEqual(client.get(f'/api/batches/{bid}/sources/source_1').json()['text'], request['files'][0]['text'])
            self.assertEqual(client.get('/workbench.js').status_code, 200)

    def test_real_process_restart_keeps_queued_items(self):
        bid, _ = batches.create([file(LUMA)], {}, 'normal', 'subprocess')
        script = "from geo_workbench import store,queue;store.init();print(queue.recover());print(queue.projection('" + batches.get(bid)['items'][0]['run_id'] + "')['state'])"
        result = subprocess.run([os.sys.executable, '-c', script], env={**os.environ, 'GEO_DATA_DIR': str(store.ROOT)}, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('queued', result.stdout)


if __name__ == '__main__':
    unittest.main()
