"""Bounded, resumable embedding work. No large-model classification inference."""
from __future__ import annotations

import json

from .models import EmbeddingError, embed
from .store import now


def text_chunks(text: str, max_bytes=1800, overlap_bytes=120):
    """Conservative UTF-8 byte windows; split only at codepoint boundaries.

    Byte counts are not a tokenizer. The indexer further subdivides any chunk
    the actual model rejects, retaining all text instead of truncating it.
    """
    if max_bytes < 4 or not 0 <= overlap_bytes < max_bytes:
        raise ValueError('Invalid chunk byte/overlap limits')
    start = 0
    while start < len(text):
        end, size = start, 0
        while end < len(text):
            width = len(text[end].encode('utf-8'))
            if size + width > max_bytes:
                break
            size += width; end += 1
        yield text[start:end]
        if end == len(text):
            return
        next_start, overlap = end, 0
        while next_start > start + 1:
            width = len(text[next_start-1].encode('utf-8'))
            if overlap + width > overlap_bytes:
                break
            next_start -= 1; overlap += width
        start = next_start


def index_embeddings(config, store, *, limit=1, request_budget=None, message_id=None,
                     retry_failed=False, new_only=False):
    from .service import exclusive
    with exclusive(config['data_dir'],'embedding.lock'):
        return _index(config,store,limit=limit,request_budget=request_budget,message_id=message_id,
                      retry_failed=retry_failed,new_only=new_only)


def _index(config, store, *, limit, request_budget, message_id, retry_failed, new_only):
    model = config['embedding_model']
    budget = max(1,min(int(request_budget or config.get('embedding_request_budget',8)),32))
    result = {'completed':0,'failed':0,'requests':0,'chunks_saved':0,'splits':0,'budget':budget}
    filters = ["(j.status IS NULL OR j.status='pending'" + (" OR j.status='failed'" if retry_failed else '') + ')']
    args = [model]
    if message_id is not None:
        filters.append('m.id=?'); args.append(message_id)
    if new_only:
        filters.append('m.historical=0')
    rows = store.db.execute(f'''SELECT m.id,m.subject,m.body,j.status FROM messages m
        LEFT JOIN embedding_jobs j ON j.message=m.id AND j.model=?
        WHERE {' AND '.join(filters)} ORDER BY (j.status='pending') DESC,m.historical,m.id DESC LIMIT ?''',
        args+[max(1,min(limit,100))]).fetchall()
    for row in rows:
        if result['requests'] >= budget:
            break
        mid = row['id']
        with store.db:
            if row['status'] is None:
                texts = list(text_chunks(row['subject']+'\n'+row['body'],config.get('embedding_max_bytes',1800)))
                store.db.execute('INSERT INTO embedding_jobs VALUES (?,?,?,?,?,?,?)',
                                 (mid,model,'pending',None,None,0,now()))
                store.db.executemany('INSERT INTO chunks VALUES (?,?,?,?,NULL)',
                                     [(mid,i,text,model) for i,text in enumerate(texts)])
            elif row['status']=='failed':
                store.db.execute("UPDATE embedding_jobs SET status='pending',error_code=NULL,http_status=NULL WHERE message=? AND model=?",
                                 (mid,model))
        while result['requests'] < budget:
            part = store.db.execute('''SELECT part,text FROM chunks WHERE message=? AND model=?
                AND vector_json IS NULL ORDER BY part LIMIT 1''',(mid,model)).fetchone()
            if part is None:
                break
            result['requests'] += 1
            try:
                vector = embed(config,part['text'])[0]
                with store.db:
                    store.db.execute('UPDATE chunks SET vector_json=? WHERE message=? AND model=? AND part=?',
                                     (json.dumps(vector),mid,model,part['part']))
                result['chunks_saved'] += 1
            except EmbeddingError as exc:
                if exc.code=='input_too_long' and len(part['text'])>1:
                    pivot = len(part['text'])//2
                    with store.db:
                        extra = store.db.execute('SELECT COALESCE(MAX(part),-1)+1 FROM chunks WHERE message=? AND model=?',
                                                (mid,model)).fetchone()[0]
                        store.db.execute('UPDATE chunks SET text=? WHERE message=? AND model=? AND part=?',
                                         (part['text'][:pivot],mid,model,part['part']))
                        store.db.execute('INSERT INTO chunks VALUES (?,?,?,?,NULL)',
                                         (mid,extra,part['text'][pivot:],model))
                    result['splits'] += 1
                    continue
                with store.db:
                    store.db.execute('''UPDATE embedding_jobs SET status='failed',error_code=?,http_status=?,
                        attempts=attempts+1,updated_at=? WHERE message=? AND model=?''',
                        (exc.code,exc.http_status,now(),mid,model))
                result['failed'] += 1
                break
        pending = store.db.execute('SELECT COUNT(*) FROM chunks WHERE message=? AND model=? AND vector_json IS NULL',
                                   (mid,model)).fetchone()[0]
        if pending==0:
            with store.db:
                store.db.execute("UPDATE embedding_jobs SET status='complete',error_code=NULL,http_status=NULL,updated_at=? WHERE message=? AND model=?",
                                 (now(),mid,model))
            result['completed'] += 1
    failures = store.db.execute("SELECT COUNT(*) FROM embedding_jobs WHERE model=? AND status='failed'",(model,)).fetchone()[0]
    store.state('embedding_error',f'{failures} failed embedding job(s); inspect embedding_jobs status' if failures else '')
    store.state('last_embedding_run',now())
    result['pending_jobs'] = store.db.execute("SELECT COUNT(*) FROM embedding_jobs WHERE model=? AND status='pending'",(model,)).fetchone()[0]
    return result
