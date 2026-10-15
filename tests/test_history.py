from pathlib import Path
import tempfile
import unittest

from ou_email_agent.config import load_config
from ou_email_agent.mailbox import parse_message, sync
from ou_email_agent.store import Store
from tests.test_agent import FakeIMAP, raw


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Store(self.temp.name)
        self.config=load_config(Path(__file__).resolve().parents[1]/'config.example.json')
        self.config.update(data_dir=self.temp.name, initial_days=0, batch_size=2)
        self.db.state('initial_cutoff', '2026-10-12T00:00:00+00:00')

    def tearDown(self):
        self.db.close(); self.temp.cleanup()

    def seed_recent_cache(self):
        self.db.insert(parse_message(raw('recent')), 'INBOX', '1', 10, False)
        with self.db.db:
            self.db.db.execute("INSERT INTO checkpoints VALUES ('INBOX','1',10,1)")
            self.db.db.execute("INSERT INTO chunks VALUES (1,0,'kept','embeddinggemma','[1,0]')")
            self.db.db.execute("INSERT INTO embedding_jobs VALUES (1,'embeddinggemma','complete',NULL,NULL,0,'2026-10-14')")

    def test_full_initial_sync_uses_all_not_date_filter(self):
        fake=FakeIMAP({1:raw('old')})
        sync(self.config,self.db,'secret',lambda *_:fake)
        self.assertIn(('SEARCH',None,'ALL'),fake.calls)

    def test_malformed_legacy_filename_does_not_hide_email_body(self):
        message = (b'From: test@example.org\r\nSubject: Old report\r\n'
            b'MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n'
            b'--x\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nSearchable report body.\r\n'
            b'--x\r\nContent-Type: application/pdf\r\n'
            b"Content-Disposition: attachment; filename*=iso-2022-jp''%1B%24\r\n\r\n"
            b'fake attachment\r\n--x--\r\n')
        parsed = parse_message(message)
        self.assertIn('Searchable report body.', parsed['body'])
        self.assertEqual(parsed['attachments'], ['[undecodable attachment filename]'])

    def test_oversize_stop_clears_after_successful_retry_with_higher_limit(self):
        fake=FakeIMAP({1:raw('old')})
        self.config['max_message_bytes']=511
        with self.assertRaises(RuntimeError): sync(self.config,self.db,'secret',lambda *_:fake)
        self.assertTrue(self.db.state('oversize_message'))
        self.config['max_message_bytes']=1024
        sync(self.config,self.db,'secret',lambda *_:fake)
        self.assertEqual(self.db.state('oversize_message'),'')

    def test_upgrade_preserves_vectors_and_fresh_sync_while_backfilling(self):
        self.seed_recent_cache()
        fake=FakeIMAP({1:raw('old1'),2:raw('old2'),3:raw('old3'),10:raw('recent'),20:raw('new')})
        result=sync(self.config,self.db,'secret',lambda *_:fake)
        self.assertEqual(result['history_messages'],2)
        self.assertEqual(self.db.db.execute('SELECT last_uid FROM checkpoints').fetchone()[0],20)
        progress=self.db.status()['history_import'][0]
        self.assertEqual((progress['remaining'],progress['complete']),(1,0))
        fake.messages[21]=raw('newer')
        sync(self.config,self.db,'secret',lambda *_:fake)
        self.assertEqual(self.db.db.execute('SELECT last_uid FROM checkpoints').fetchone()[0],21)
        self.assertEqual(self.db.status()['history_import'][0]['complete'],1)
        self.assertEqual(self.db.db.execute('SELECT vector_json FROM chunks WHERE message=1').fetchone()[0],'[1,0]')
        self.assertFalse(any(c[0]=='FETCH' and c[1]=='10' for c in fake.calls))
        old=self.db.db.execute("SELECT historical FROM messages WHERE message_id='<old1@example.org>'").fetchone()
        new=self.db.db.execute("SELECT historical FROM messages WHERE message_id='<new@example.org>'").fetchone()
        self.assertEqual((old[0],new[0]),(1,0))
        self.assertEqual(sync(self.config,self.db,'secret',lambda *_:fake)['new_messages'],0)

    def test_failure_resumes_at_failed_uid_without_refetching_success(self):
        self.seed_recent_cache()
        fake=FakeIMAP({1:raw('old1'),2:raw('old2'),10:raw('recent')},fail=2)
        with self.assertRaises(RuntimeError): sync(self.config,self.db,'secret',lambda *_:fake)
        self.assertEqual(self.db.status()['history_import'][0]['last_uid'],1)
        self.assertEqual(self.db.db.execute('SELECT last_uid FROM checkpoints').fetchone()[0],10)
        fake.fail=None
        sync(self.config,self.db,'secret',lambda *_:fake)
        self.assertEqual(sum(c[:3]==('FETCH','1','(BODY.PEEK[])') for c in fake.calls),1)
        self.assertEqual(self.db.status()['history_import'][0]['complete'],1)

    def test_uidvalidity_change_resets_history_cursor_and_deduplicates(self):
        self.seed_recent_cache()
        fake=FakeIMAP({1:raw('old1'),10:raw('recent')})
        sync(self.config,self.db,'secret',lambda *_:fake)
        before=self.db.status()['messages']
        fake=FakeIMAP({5:raw('old1'),7:raw('recent')},validity='2')
        sync(self.config,self.db,'secret',lambda *_:fake)
        progress=self.db.status()['history_import'][0]
        self.assertEqual((progress['validity'],progress['complete']),('2',1))
        self.assertEqual(self.db.status()['messages'],before)

    def test_deleted_gap_and_empty_folder_complete_without_range_reversal(self):
        self.seed_recent_cache()
        fake=FakeIMAP({10:raw('recent')})
        sync(self.config,self.db,'secret',lambda *_:fake)
        self.assertEqual(self.db.status()['history_import'][0]['complete'],1)
        self.assertFalse(any(c[0]=='FETCH' for c in fake.calls))
        with tempfile.TemporaryDirectory() as path:
            empty=Store(path)
            try:
                sync({**self.config,'data_dir':path},empty,'secret',lambda *_:FakeIMAP({}))
                self.assertEqual(empty.status()['history_import'][0]['complete'],1)
            finally: empty.close()
