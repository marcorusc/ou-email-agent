from __future__ import annotations

import json
import logging
import os
import threading
from contextlib import contextmanager
from email.utils import parseaddr
from pathlib import Path

from . import mailbox, models
from .config import password
from .store import Store, now

log = logging.getLogger(__name__)


@contextmanager
def exclusive(directory, filename='monitor.lock'):
    path = Path(directory)
    path.mkdir(parents=True, exist_ok=True)
    file = open(path / filename, 'a+b')
    try:
        file.seek(0, 2)
        if file.tell() == 0:
            file.write(b'0'); file.flush()
        file.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        file.close()
        raise RuntimeError('Another worker already holds '+filename+'.') from None
    try:
        yield
    finally:
        file.close()


def notify(config, store, notifier=None):
    if not config['notifications']:
        return 0
    if notifier is None:
        if os.name != 'nt':
            return 0
        from plyer import notification
        notifier = notification.notify
    count = 0
    rows = store.db.execute('''SELECT id,sender,subject,analysis_json FROM messages
        WHERE historical=0 AND notified=0 AND analysis_json IS NOT NULL ORDER BY id''').fetchall()
    for row in rows:
        analysis = json.loads(row['analysis_json'])
        priority = parseaddr(row['sender'])[1].lower() in {x.lower() for x in config['priority_senders']}
        important = priority or (analysis['important'] and analysis['category'] in config['alert_categories'])
        if important:
            # Short Windows notification; full evidence remains accessible via MCP.
            notifier(title='OU email: ' + row['subject'][:45],
                     message=(analysis['summary'][:180] + f"\nLocal email #{row['id']}")[:250],
                     app_name='OU Email Agent', timeout=10)
            count += 1
        with store.db:
            store.db.execute('UPDATE messages SET notified=1 WHERE id=?',(row['id'],))
    return count


def monitor(config):
    stop = threading.Event()
    secret = password(config)
    def sync_worker():
        store = Store(config['data_dir'])
        while not stop.is_set():
            try:
                result = mailbox.sync(config, store, secret)
                log.info('Sync: %s new messages', result['new_messages'])
            except Exception as exc:
                store.state('sync_error',type(exc).__name__ + ': check connection/login and run doctor')
                log.error('Sync failed: %s',type(exc).__name__)
            else:
                # Wait for a successful empty sync so startup backlog has been fetched first.
                if result['new_messages'] == 0:
                    try:
                        from .indexing import schedule_tick
                        schedule_tick(config, store)
                        store.state('indexing_schedule_error', '')
                    except Exception as exc:
                        store.state('indexing_schedule_error', type(exc).__name__)
                        log.error('Index scheduling failed: %s', type(exc).__name__)
            store.state('monitor_heartbeat',now())
            stop.wait(config['poll_seconds'])
        store.close()
    def analysis_worker():
        store = Store(config['data_dir'])
        while not stop.is_set():
            try:
                result = models.process_background(config,store)
                notify(config,store)
                store.state('worker_error','')
                if result['analyzed'] or result['failed']:
                    log.info('Analysis: %s', result)
            except Exception as exc:
                store.state('worker_error',type(exc).__name__)
                log.error('Analysis/notification failed: %s',type(exc).__name__)
            stop.wait(config['analysis_interval_seconds'])
        store.close()
    with exclusive(config['data_dir']):
        store = Store(config['data_dir'])
        mode = 'new-mail-paced' if config['background_analysis'] else 'sync-only (AI paused)'
        store.state('analysis_mode',mode)
        store.state('analysis_interval_seconds',config['analysis_interval_seconds'])
        store.state('historical_analysis','manual only')
        store.close()
        functions = [sync_worker]
        if config['background_analysis']:
            functions.append(analysis_worker)
        workers = [threading.Thread(target=f,daemon=True) for f in functions]
        for worker in workers:
            worker.start()
        log.info('Monitoring mode: %s. Ctrl+C stops. Calendar/Obsidian remain in ChatGPT.',mode)
        try:
            while not stop.wait(1):
                if any(not worker.is_alive() for worker in workers):
                    raise RuntimeError('Background worker stopped unexpectedly; restart monitor.')
        finally:
            stop.set()
            for worker in workers:
                worker.join(timeout=2)
