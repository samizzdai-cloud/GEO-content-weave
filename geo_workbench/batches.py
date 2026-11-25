"""Deterministic, source-bound imports; a product, never a SKU, is a run."""
import copy
import hashlib
import json
import math
import unicodedata
import uuid
from geo_workbench import store, workflow

VERSION = '2.0'
MAX_ITEMS = 20
CONFIG = ('content_requests', 'target_audience', 'suggested_paths', 'style')


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def pointer(key):
    return str(key).replace('~', '~0').replace('/', '~1')


def brand_key(brand, source_id):
    name = brand.get('name') if isinstance(brand, dict) else None
    return 'name:' + ' '.join(unicodedata.normalize('NFKC', name).split()).casefold() if isinstance(name, str) and name.strip() else 'source:' + source_id


def _validate(value, path=''):
    if value is None:
        raise ValueError(f'{path or "/"}: null 的删除/继承含义未定义，请省略或提供明确值')
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f'{path}: 数值必须有限')
    if isinstance(value, dict):
        if 'price' in value:
            price = value['price']
            if price is None:
                raise ValueError(f'{path}/price: null 的删除/继承含义未定义')
            if not isinstance(price, dict) or set(price) != {'value', 'currency'} or isinstance(price['value'], bool) or not isinstance(price['value'], (int, float)) or price['value'] < 0 or not isinstance(price['currency'], str) or len(price['currency']) != 3 or not price['currency'].isalpha():
                raise ValueError(f'{path}/price: 价格须整体提供非负 value 和三字母 currency')
        for key, item in value.items():
            _validate(item, path + '/' + pointer(key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _validate(item, path + '/' + str(index))


def _merge(target, values, mapping, source, base, layer, effective=''):
    """Map containers as well as leaves; composite price is replaced atomically."""
    for key, value in values.items():
        dest = effective + '/' + pointer(key)
        origin = base + '/' + pointer(key)
        old = mapping.get(dest)
        if isinstance(value, dict) and value and key != 'price' and isinstance(target.get(key, {}), dict):
            target.setdefault(key, {})
            _merge(target[key], value, mapping, source, origin, layer, dest)
        else:
            for path in list(mapping):
                if path.startswith(dest + '/'):
                    del mapping[path]
            target[key] = copy.deepcopy(value)
            def children(item, ep, sp):
                if isinstance(item, dict):
                    iterator = item.items()
                elif isinstance(item, list):
                    iterator = enumerate(item)
                else:
                    return
                for k, v in iterator:
                    mapping[ep + '/' + pointer(k)] = {'source_id': source['id'], 'source_pointer': sp + '/' + pointer(k), 'layer': layer, 'source_hash': source['digest']}
                    children(v, ep + '/' + pointer(k), sp + '/' + pointer(k))
            children(value, dest, origin)
        mapping[dest] = {'source_id': source['id'], 'source_pointer': origin, 'layer': layer, 'source_hash': source['digest'], 'overrides': ([old] if old else [])}


def preflight(files, defaults=None, user_query=''):
    defaults = defaults or {}
    if set(defaults) - set(CONFIG):
        raise ValueError('批次 defaults 只允许 content_requests/target_audience/suggested_paths/style，不允许商品事实')
    _validate(defaults)
    if not 1 <= len(files) <= 20 or sum(len(f['text'].encode('utf-8')) for f in files) > 5_000_000:
        raise ValueError('每批最多 20 文件、原文总计 5 MB')
    sources, items = [], []
    default_source = {'id': 'batch_defaults', 'digest': store.input_digest(defaults)}
    for index, file in enumerate(files):
        source = {'id': f'source_{index + 1}', 'filename': file['filename'], 'text': file['text'], 'digest': hashlib.sha256(file['text'].encode('utf-8')).hexdigest()}
        sources.append(source)
        try:
            def unique(pairs):
                obj = {}
                for key, value in pairs:
                    if key in obj:
                        raise ValueError(f'重复 JSON 键 {key}')
                    obj[key] = value
                return obj
            doc = json.loads(file['text'].lstrip('\ufeff'), object_pairs_hook=unique, parse_constant=lambda _: (_ for _ in ()).throw(ValueError('非有限 JSON 数值')))
            if not isinstance(doc, dict):
                raise ValueError('文件顶层须为对象')
            if ('product' in doc) == ('products' in doc):
                raise ValueError('必须且只能提供 product 或 products')
            multi = 'products' in doc
            if multi and (doc.get('schema_version') != VERSION or not isinstance(doc['products'], list) or not doc['products']):
                raise ValueError('多商品须提供 schema_version="2.0" 和非空 products 数组')
            brand = doc.get('brand', {})
            if not isinstance(brand, dict):
                raise ValueError('brand 须为对象')
            _validate(brand, '/brand')
            for key in CONFIG:
                if key in doc:
                    _validate(doc[key], '/' + key)
            products = list(enumerate(doc['products'])) if multi else [(None, doc['product'])]
        except (ValueError, TypeError) as exc:
            items.append({'source_id': source['id'], 'source_pointer': '', 'error': str(exc), 'valid': False})
            continue
        for position, product in products:
            path = f'/products/{position}' if multi else '/product'
            item = {'source_id': source['id'], 'source_pointer': path, 'valid': False}
            try:
                if not isinstance(product, dict):
                    raise ValueError('product 须为对象')
                _validate(product, path)
                effective, mapping = {}, {}
                _merge(effective, defaults, mapping, default_source, '', 'batch_defaults')
                _merge(effective, {k: doc[k] for k in CONFIG if k in doc}, mapping, source, '', 'file_defaults')
                _merge(effective, {k: brand[k] for k in CONFIG if k in brand}, mapping, source, '/brand', 'brand')
                _merge(effective, {'brand': brand}, mapping, source, '', 'brand')
                inherited = brand.get('product_defaults', {})
                if not isinstance(inherited, dict) or set(inherited) - {'facts'}:
                    raise ValueError('brand.product_defaults 只允许 facts')
                effective['product'] = {}
                _merge(effective['product'], inherited, mapping, source, '/brand/product_defaults', 'brand', '/product')
                # The wrapper below is synthetic; original pointer must remain /product or /products/N.
                p = {k: v for k, v in product.items() if k not in CONFIG}
                _merge(effective.setdefault('product', {}), p, mapping, source, path, 'product', '/product')
                _merge(effective, {k: product[k] for k in CONFIG if k in product}, mapping, source, path, 'product')
                variants = []
                skus = p.get('skus', [])
                if not isinstance(skus, list):
                    raise ValueError('skus 须为显式数组')
                for sku_index, sku in enumerate(skus):
                    if not isinstance(sku, dict) or not isinstance(sku.get('id'), str) or not sku['id'].strip():
                        raise ValueError('每个 SKU 须为带 id 的对象')
                    if not isinstance(sku.get('facts', {}), dict):
                        raise ValueError('sku.facts 须为对象')
                    variant, refs = copy.deepcopy(effective), copy.deepcopy(mapping)
                    variant['product'].pop('skus', None)
                    refs = {k: v for k, v in refs.items() if not k.startswith('/product/skus')}
                    _merge(variant['product'], sku, refs, source, path + f'/skus/{sku_index}', 'sku', '/product')
                    variants.append({'source_pointer': path + f'/skus/{sku_index}', 'effective': variant, 'provenance': refs})
                _, errors = workflow.contract(effective, user_query)
                if errors:
                    raise ValueError('；'.join(errors))
                # Preserve legacy optional metadata and constraints without inferring facts.
                for key in ('dataset_id', 'unsupported_claims', 'test_scenarios'):
                    if key in doc:
                        _merge(effective, {key: doc[key]}, mapping, source, '', 'file_defaults')
                def attach_values(refs, view):
                    for dest, ref in refs.items():
                        value = view
                        for part in dest.split('/')[1:]:
                            key = part.replace('~1', '/').replace('~0', '~')
                            value = value[int(key)] if isinstance(value, list) else value[key]
                        ref['effective_value'] = copy.deepcopy(value)
                attach_values(mapping, effective)
                for variant in variants:
                    attach_values(variant['provenance'], variant['effective'])
                effective['_import'] = {'source_id': source['id'], 'filename': source['filename'], 'source_hash': source['digest'],
                                        'source_pointer': path, 'schema_version': VERSION, 'provenance': copy.deepcopy(mapping)}
                item.update(valid=True, effective=effective, effective_digest=store.input_digest(effective), provenance=mapping, variants=variants,
                            brand_key=brand_key(brand, source['id']), brand_name=brand.get('name') or '未命名品牌', name=product['name'], product_id=product.get('id'))
            except (ValueError, TypeError, KeyError) as exc:
                item['error'] = str(exc)
            items.append(item)
    if len(items) > MAX_ITEMS:
        raise ValueError(f'每批最多 {MAX_ITEMS} 个商品/错误项，请拆分文件')
    return {'schema_version': VERSION, 'sources': sources, 'items': items, 'defaults': defaults, 'user_query': user_query,
            'valid_count': sum(i['valid'] for i in items), 'invalid_count': sum(not i['valid'] for i in items)}


def create(files, defaults, query, key, selected=None):
    request = {'files': files, 'defaults': defaults, 'query': query, 'selected': selected}
    digest = store.input_digest(request)
    # Replay from the committed snapshot; no reinterpretation of old requests.
    with store.LOCK, store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute('SELECT id,request_digest FROM batches WHERE idempotency_key=?', (key,)).fetchone()
        if prior:
            if prior['request_digest'] != digest:
                raise ValueError('幂等键已用于不同批次输入')
            return prior['id'], False
        result = preflight(files, defaults, query)
        chosen = set(selected if selected is not None else [i for i, item in enumerate(result['items']) if item['valid']])
        if not chosen or any(not isinstance(i, int) or i < 0 or i >= len(result['items']) or not result['items'][i]['valid'] for i in chosen):
            raise ValueError('请选择至少一个有效商品，不能启动错误项')
        bid = uuid.uuid4().hex
        db.execute('INSERT INTO batches VALUES(?,?,?,?,?,?,?)', (bid, key, digest, dump(result), query, store.now(), VERSION))
        for index, item in enumerate(result['items']):
            rid = None
            if index in chosen:
                rid = store.create_in_db(db, item['effective'], query)
                store.enqueue_in_db(db, rid, 'workflow', {'mode': 'start', 'payload': None}, f'start:{rid}')
            db.execute('INSERT INTO batch_items VALUES(?,?,?,?,?)', (bid, index, rid, dump(item), 'selected' if index in chosen else 'not_started'))
        return bid, True


def get(batch_id):
    with store.connect() as db:
        row = db.execute('SELECT * FROM batches WHERE id=?', (store.valid_run(batch_id),)).fetchone()
        if not row:
            raise KeyError('批次不存在')
        items = db.execute('SELECT * FROM batch_items WHERE batch_id=? ORDER BY position', (batch_id,)).fetchall()
    from geo_workbench.queue import projection
    result = {'id': row['id'], 'created': row['created'], 'user_query': row['query'], 'counts': {}, 'items': []}
    for row_item in items:
        item = json.loads(row_item['detail'])
        view = projection(row_item['run_id']) if row_item['run_id'] else {'state': 'invalid' if not item['valid'] else 'not_started', 'next_action': item.get('error', '未选择启动')}
        result['items'].append({**item, 'position': row_item['position'], 'run_id': row_item['run_id'], **view})
        result['counts'][view['state']] = result['counts'].get(view['state'], 0) + 1
    return result


def source(batch_id, source_id):
    with store.connect() as db:
        row = db.execute('SELECT snapshot FROM batches WHERE id=?', (store.valid_run(batch_id),)).fetchone()
    if not row:
        raise KeyError('批次不存在')
    for item in json.loads(row['snapshot'])['sources']:
        if item['id'] == source_id:
            if hashlib.sha256(item['text'].encode('utf-8')).hexdigest() != item['digest']:
                raise ValueError('原始来源摘要不匹配')
            return item
    raise KeyError('来源不存在')
