"""Detached, bounded indexing of cached mail, shared by MCP, schedules and manual runs."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
import time
import uuid

from .config import load_config
from .embeddings import _index
from .models import request
from .service import exclusive
from .store import Store, now


def job_status(config, db):
    job = json.loads(db.state('indexing_job') or '{}')
    try:
        with exclusive(config['data_dir'], 'catchup.lock'):
            pass
        locked = False
    except RuntimeError:
        locked = True
    if job.get('status') == 'queued' and not locked:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(job['updated_at'])).total_seconds()
        if age > 120:
            job = {**job, 'status': 'interrupted', 'error': 'Worker did not start; request indexing again'}
    elif job.get('status') == 'running' and not locked:
        job = {**job, 'status': 'interrupted', 'error': 'Worker stopped; saved chunks will resume'}
    return {**job, 'active': locked or job.get('status') == 'queued'}


def start_indexing(config):
    """No email credentials, shell commands, model names or paths supplied by the caller."""
    db = Store(config['data_dir'])
    try:
        with exclusive(config['data_dir'], 'index-launch.lock'):
            status = job_status(config, db)
            if status['active']:
                return {**status, 'already_running': True}
            job = {'id': uuid.uuid4().hex, 'status': 'queued', 'updated_at': now(),
                   'device': 'GPU' if config.get('scheduled_embedding_num_gpu', 99) else 'CPU',
                   'scope': 'Cached unfinished messages only; no mailbox fetch or classification'}
            db.state('indexing_job', json.dumps(job))
            env = {**os.environ, 'OU_EMAIL_CONFIG': config['_config_path'], 'OU_EMAIL_DATA': config['data_dir']}
            options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {'start_new_session': True}
            try:
                subprocess.Popen([sys.executable, '-m', 'ou_email_agent.indexing', job['id']],
                                 cwd=str(Path(__file__).resolve().parent.parent), env=env,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, **options)
            except Exception as exc:
                db.state('indexing_job', json.dumps({**job, 'status': 'failed', 'error': type(exc).__name__}))
                raise
            return {**job, 'active': True, 'last_sync': db.state('last_sync'),
                    'next_step': 'Use email_status to check progress and errors'}
    finally:
        db.close()


def run_job(config, job_id):
    db = Store(config['data_dir'])
    job = None
    loaded = False
    cfg = {**config, 'embedding_num_gpu': config.get('scheduled_embedding_num_gpu', 99),
           'model_keep_alive': '30s'}
    try:
        with exclusive(config['data_dir'], 'catchup.lock'):
            job = json.loads(db.state('indexing_job') or '{}')
            if job.get('id') != job_id or job.get('status') != 'queued':
                return
            def save(**fields):
                job.update(fields, updated_at=now())
                db.state('indexing_job', json.dumps(job))
            save(status='running', completed=0, failed=0, requests=0)
            try:
                with exclusive(config['data_dir'], 'embedding.lock'):
                    queue = [r[0] for r in db.db.execute('''SELECT m.id FROM messages m
                        LEFT JOIN embedding_jobs j ON j.message=m.id AND j.model=?
                        WHERE j.status IS NULL OR j.status='pending' ORDER BY m.id''', (cfg['embedding_model'],))]
                    save(total=len(queue), remaining=len(queue))
                    if queue:
                        # Fail before changing any email job if Ollama/model is unavailable at sign-in.
                        tags = request(cfg, '/api/tags', timeout=10)
                        if not any(m['name'] in {cfg['embedding_model'], cfg['embedding_model']+':latest'} for m in tags['models']):
                            raise RuntimeError('Embedding model unavailable')
                    deadline = time.monotonic() + config.get('indexing_max_seconds', 1200)
                    while queue and time.monotonic() < deadline:
                        loaded = True
                        result = _index(cfg, db, limit=1, request_budget=4, message_id=queue[0],
                                        retry_failed=False, new_only=False)
                        for key in ('completed', 'failed', 'requests'):
                            job[key] += result[key]
                        if result['completed']:
                            queue.pop(0)
                        save(remaining=len(queue))
                        if result['failed']:
                            save(status='failed', error='Embedding failed; inspect embedding_jobs in email_status')
                            return
                        if not result['requests'] and not result['completed']:
                            raise RuntimeError('No indexing progress')
                        if queue:
                            time.sleep(1)
                    save(status='partial' if queue else 'complete')
            except Exception as exc:
                save(status='failed', error=type(exc).__name__)
            finally:
                if loaded:
                    try:
                        request(cfg, '/api/embed', {'model': cfg['embedding_model'], 'input': '',
                                'keep_alive': 0, 'options': {'num_gpu': cfg['embedding_num_gpu']}}, timeout=30)
                    except Exception:
                        save(unload_warning='Immediate unload failed; 30-second idle expiry remains')
    finally:
        db.close()


def latest_slot(current, times):
    """Most recent local wall-clock slot; coalesce missed days into one catch-up."""
    candidates = []
    for days in (0, 1):
        for value in times:
            hour, minute = map(int, value.split(':'))
            candidate = (current - timedelta(days=days)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= current:
                candidates.append(candidate)
    return max(candidates).isoformat()


def schedule_tick(config, db, current=None):
    times = config.get('indexing_times', [])
    if not times:
        return
    current = current or datetime.now().astimezone()
    slot = latest_slot(current, times)
    record = json.loads(db.state('indexing_schedule') or '{}')
    if record.get('slot') != slot:
        record = {'slot': slot, 'attempts': 0}
    if record.get('done'):
        return
    status = job_status(config, db)
    if status['active']:
        return
    if record.get('job_id') == status.get('id') and status.get('status') in {'complete', 'partial'}:
        record['done'] = True
        db.state('indexing_schedule', json.dumps(record))
        return
    if record['attempts'] >= 3:
        return
    if record.get('attempt_at') and (current - datetime.fromisoformat(record['attempt_at'])).total_seconds() < 900:
        return
    result = start_indexing(config)
    record.update(attempts=record['attempts']+1, attempt_at=current.isoformat(), job_id=result['id'])
    db.state('indexing_schedule', json.dumps(record))


if __name__ == '__main__':
    run_job(load_config(), sys.argv[1])
