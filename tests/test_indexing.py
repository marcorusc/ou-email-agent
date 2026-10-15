import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ou_email_agent.config import load_config
from ou_email_agent.indexing import job_status, latest_slot, run_job, schedule_tick, start_indexing
from ou_email_agent.service import exclusive
from ou_email_agent.store import Store, now


class IndexingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = load_config(Path(__file__).resolve().parents[1] / 'config.example.json')
        self.config.update(data_dir=self.temp.name, indexing_times=['09:00', '16:00'])
        self.db = Store(self.temp.name)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_slots_before_morning_between_slots_and_after_wake(self):
        tz = timezone(timedelta(hours=9))
        for hour, expected in [(8, '2026-10-14T16:00'), (9, '2026-10-15T09:00'),
                               (15, '2026-10-15T09:00'), (18, '2026-10-15T16:00')]:
            self.assertTrue(latest_slot(datetime(2026, 10, 15, hour, tzinfo=tz),
                                        self.config['indexing_times']).startswith(expected))

    def test_duplicate_launch_and_interrupted_process_recovery(self):
        with patch('ou_email_agent.indexing.subprocess.Popen') as spawn:
            first = start_indexing(self.config)
            self.assertTrue(start_indexing(self.config)['already_running'])
            self.assertEqual(spawn.call_count, 1)
            self.assertIn(self.temp.name, spawn.call_args.kwargs['env']['OU_EMAIL_DATA'])
            self.db.state('indexing_job', json.dumps({**first, 'status': 'running'}))
            self.assertEqual(job_status(self.config, self.db)['status'], 'interrupted')
            next_job = start_indexing(self.config)
            self.assertNotEqual(first['id'], next_job['id'])

    def test_stale_queued_job_can_be_restarted(self):
        self.db.state('indexing_job', json.dumps({'id':'old', 'status':'queued',
            'updated_at':(datetime.now(timezone.utc)-timedelta(minutes=3)).isoformat()}))
        with patch('ou_email_agent.indexing.subprocess.Popen') as spawn:
            self.assertNotEqual(start_indexing(self.config)['id'], 'old')
            spawn.assert_called_once()

    def test_worker_holds_single_run_lock_and_resumes_only_unfinished(self):
        for uid in (1, 2):
            self.db.insert({'message_id':str(uid), 'references':[], 'subject':'Test',
                'sender':'test@example.org', 'recipients':'you@example.org',
                'date':now(), 'body':'Please review.', 'attachments':[]}, 'INBOX', '1', uid, True)
        with self.db.db:
            self.db.db.execute('INSERT INTO embedding_jobs VALUES (?,?,?,?,?,?,?)',
                               (1, self.config['embedding_model'], 'complete', None, None, 0, now()))
        with patch('ou_email_agent.indexing.subprocess.Popen'):
            job = start_indexing(self.config)
        def embed(*args):
            with self.assertRaises(RuntimeError):
                with exclusive(self.temp.name, 'catchup.lock'): pass
            with self.assertRaises(RuntimeError):
                with exclusive(self.temp.name, 'embedding.lock'): pass
            self.assertTrue(start_indexing(self.config)['already_running'])
            return [[1.0, 0.0]]
        with patch('ou_email_agent.embeddings.embed', side_effect=embed) as model, \
             patch('ou_email_agent.indexing.request', return_value={'models':[{'name':'embeddinggemma:latest'}]}):
            run_job(self.config, job['id'])
            self.assertEqual(model.call_count, 1)
        status = job_status(self.config, self.db)
        self.assertEqual((status['status'], status['completed'], status['remaining']), ('complete', 1, 0))
        self.assertFalse(status['active'])

    def test_empty_queue_never_loads_model_and_wrong_worker_cannot_take_job(self):
        with patch('ou_email_agent.indexing.subprocess.Popen'):
            job = start_indexing(self.config)
        with patch('ou_email_agent.indexing.request') as model:
            run_job(self.config, 'wrong-id')
            self.assertEqual(job_status(self.config, self.db)['status'], 'queued')
            run_job(self.config, job['id'])
            model.assert_not_called()
        self.assertEqual(job_status(self.config, self.db)['status'], 'complete')

    def test_schedule_retries_are_spaced_and_success_is_not_repeated(self):
        current = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
        with patch('ou_email_agent.indexing.subprocess.Popen') as spawn:
            schedule_tick(self.config, self.db, current)
            job = json.loads(self.db.state('indexing_job'))
            self.db.state('indexing_job', json.dumps({**job, 'status':'failed'}))
            schedule_tick(self.config, self.db, current+timedelta(minutes=5))
            self.assertEqual(spawn.call_count, 1)
            schedule_tick(self.config, self.db, current+timedelta(minutes=16))
            self.assertEqual(spawn.call_count, 2)
            job = json.loads(self.db.state('indexing_job'))
            self.db.state('indexing_job', json.dumps({**job, 'status':'complete'}))
            schedule_tick(self.config, self.db, current+timedelta(minutes=17))
            schedule_tick(self.config, self.db, current+timedelta(minutes=40))
            self.assertEqual(spawn.call_count, 2)
            self.assertTrue(json.loads(self.db.state('indexing_schedule'))['done'])

    def test_schedule_stops_after_three_failures(self):
        current = datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
        with patch('ou_email_agent.indexing.subprocess.Popen') as spawn:
            for i in range(5):
                schedule_tick(self.config, self.db, current+timedelta(minutes=16*i))
                job = json.loads(self.db.state('indexing_job'))
                self.db.state('indexing_job', json.dumps({**job, 'status':'failed'}))
            self.assertEqual(spawn.call_count, 3)

    def test_unavailable_ollama_does_not_mark_email_failed(self):
        self.db.insert({'message_id':'1', 'references':[], 'subject':'Test',
            'sender':'test@example.org', 'recipients':'you@example.org',
            'date':now(), 'body':'Please review.', 'attachments':[]}, 'INBOX', '1', 1, True)
        with patch('ou_email_agent.indexing.subprocess.Popen'):
            job = start_indexing(self.config)
        with patch('ou_email_agent.indexing.request', side_effect=ConnectionError):
            run_job(self.config, job['id'])
        self.assertEqual(job_status(self.config, self.db)['status'], 'failed')
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM embedding_jobs').fetchone()[0], 0)

    def test_time_budget_preserves_unfinished_queue(self):
        self.db.insert({'message_id':'1', 'references':[], 'subject':'Test',
            'sender':'test@example.org', 'recipients':'you@example.org',
            'date':now(), 'body':'Please review.', 'attachments':[]}, 'INBOX', '1', 1, True)
        with patch('ou_email_agent.indexing.subprocess.Popen'):
            job = start_indexing(self.config)
        with patch('ou_email_agent.indexing.request', return_value={'models':[{'name':'embeddinggemma:latest'}]}), \
             patch('ou_email_agent.indexing.time.monotonic', side_effect=[0, 1201]), \
             patch('ou_email_agent.embeddings.embed') as model:
            run_job(self.config, job['id'])
            model.assert_not_called()
        status = job_status(self.config, self.db)
        self.assertEqual((status['status'], status['remaining']), ('partial', 1))
