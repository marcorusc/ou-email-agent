from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from ou_email_agent.config import load_config
from ou_email_agent.mailbox import parse_message, sync
from ou_email_agent.models import analyze, process_background
from ou_email_agent.service import exclusive, notify
from ou_email_agent.store import Store


def raw(mid='one', text='Please submit the experiment report by Friday.', date='Tue, 13 Oct 2026 10:00:00 +0900', refs=''):
    return (f'From: Supervisor <supervisor@example.org>\r\nTo: recipient@example.org\r\n'
            f'Subject: Experiment deadline\r\nMessage-ID: <{mid}@example.org>\r\n'
            f'Date: {date}\r\nReferences: {refs}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{text}').encode()


def classification():
    return {'summary':'Report due Friday.','category':'deadline','important':True,
            'reason':'An explicit request.','evidence':'Please submit the experiment report by Friday.',
            'agenda_items':[{'kind':'deadline','title':'Submit experiment report','date_text':'Friday',
                             'evidence':'Please submit the experiment report by Friday.','needs_review':True}]}


class FakeIMAP:
    def __init__(self, messages, validity='1', fail=None):
        self.messages=messages; self.validity=validity; self.fail=fail; self.calls=[]
    def __enter__(self): return self
    def __exit__(self,*args): pass
    def select(self,folder,readonly=False):
        assert readonly is True
        self.calls.append(('select',folder,readonly))
        return 'OK',[str(len(self.messages)).encode()]
    def response(self,name): return name,[self.validity.encode()]
    def uid(self,command,*args):
        self.calls.append((command,)+args)
        if command=='SEARCH':
            # Simulate IMAP's range reversal edge case by always returning the highest UID too.
            start=int(args[2].split(':')[0]) if args[1]=='UID' else 0
            ids=[u for u in self.messages if u>=start or u==max(self.messages)]
            return 'OK',[b' '.join(str(u).encode() for u in ids)]
        uid=int(args[0])
        if args[1]=='(RFC822.SIZE INTERNALDATE)':
            return 'OK',[f'{uid} (RFC822.SIZE 512 INTERNALDATE "13-Oct-2026 10:00:00 +0900")'.encode()]
        assert args[1]=='(BODY.PEEK[])'
        if uid==self.fail:
            return 'NO',[b'failure']
        return 'OK',[(b'metadata',self.messages[uid]),b')']


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.db=Store(self.temp.name)
        self.config=load_config(Path(__file__).resolve().parents[1] / 'config.example.json')
        self.config['data_dir']=self.temp.name
        self.config['batch_size']=50
    def tearDown(self):
        self.db.close(); self.temp.cleanup()
    def insert(self,mail=None,historical=False):
        self.db.insert(parse_message(mail or raw()),'INBOX','1',1,historical)
        return self.db.db.execute('SELECT id FROM messages ORDER BY id DESC LIMIT 1').fetchone()[0]

    def test_sync_is_readonly_and_uid_range_does_not_duplicate(self):
        fake=FakeIMAP({10:raw()})
        factory=lambda *_:fake
        self.assertEqual(sync(self.config,self.db,'secret',factory)['new_messages'],1)
        self.assertEqual(sync(self.config,self.db,'secret',factory)['new_messages'],0)
        self.assertEqual(self.db.status()['messages'],1)
        self.assertEqual(sum(1 for x in fake.calls if x[0]=='FETCH' and x[2]=='(BODY.PEEK[])'),1)

    def test_metadata_filters_constrain_keyword_and_semantic_results(self):
        wanted=self.insert(raw('wanted',text='protein purification'))
        self.insert(raw('wrong-recipient',text='protein purification').replace(b'recipient@example.org',b'other@example.org'))
        self.insert(raw('wrong-sender',text='protein purification').replace(b'supervisor@example.org',b'other@example.org'))
        with self.db.db:
            for row in self.db.db.execute('SELECT id FROM messages').fetchall():
                self.db.db.execute('INSERT INTO chunks VALUES (?,?,?,?,?)',(row[0],0,'protein purification','test','[1,0]'))
        for query in ['protein','purific','semantically related topic']:
            with self.subTest(query=query):
                results=self.db.search(query,sender='supervisor@example.org',recipient='RECIPIENT@example.org',
                                       after='2026-10-13',before='2026-10-13',vector=[1,0],model='test')
                self.assertEqual([r['id'] for r in results],[wanted])
                self.assertIn('recipient@example.org',results[0]['recipients'])
        self.assertEqual(self.db.search('protein',recipient='absent@example.org',vector=[1,0],model='test'),[])

    def test_recipient_only_search_includes_cc_without_model_calls(self):
        from ou_email_agent.models import search
        wanted=self.insert(raw().replace(b'To: recipient@example.org',b'To: recipient@example.org\r\nCc: Collaborator <collab@example.org>'))
        with patch('ou_email_agent.models.embed') as embed:
            results=search(self.config,self.db,'',recipient='collab@example.org')
            embed.assert_not_called()
        self.assertEqual([r['id'] for r in results['results']],[wanted])

    def test_failure_checkpoint_and_resume(self):
        fake=FakeIMAP({1:raw('one'),2:raw('two'),3:raw('three')},fail=2)
        with self.assertRaises(RuntimeError): sync(self.config,self.db,'secret',lambda *_:fake)
        self.assertEqual(self.db.db.execute('SELECT last_uid FROM checkpoints').fetchone()[0],1)
        fake.fail=None
        self.assertEqual(sync(self.config,self.db,'secret',lambda *_:fake)['new_messages'],2)
        self.assertEqual(self.db.status()['messages'],3)

    def test_uidvalidity_change_reindexes_without_duplicate_message(self):
        fake=FakeIMAP({1:raw()})
        sync(self.config,self.db,'secret',lambda *_:fake)
        fake.validity='2';fake.messages={5:raw()}
        sync(self.config,self.db,'secret',lambda *_:fake)
        self.assertEqual(self.db.status()['messages'],1)
        self.assertEqual(self.db.db.execute('SELECT validity,last_uid FROM checkpoints').fetchone()[:],('2',5))

    def test_no_history_alert_and_no_repeat_alert(self):
        mid=self.insert(historical=True)
        self.db.save_analysis(mid,classification())
        alerts=[]
        self.assertEqual(notify(self.config,self.db,lambda **k:alerts.append(k)),0)
        with self.db.db: self.db.db.execute('UPDATE messages SET historical=0')
        self.assertEqual(notify(self.config,self.db,lambda **k:alerts.append(k)),1)
        self.assertEqual(notify(self.config,self.db,lambda **k:alerts.append(k)),0)
        self.assertEqual(len(alerts),1)

    def test_search_japanese_and_literal_punctuation(self):
        mid=self.insert(raw(text='実験の打ち合わせは金曜日。protein ABC-123 sample.'))
        self.assertEqual(self.db.search('打ち合わせ')[0]['id'],mid)
        self.assertEqual(self.db.search('ABC-123')[0]['id'],mid)
        self.assertEqual(self.db.search('protein',sender='other@example.org'),[])
        self.assertEqual(self.db.search('protein',after='2026-10-14'),[])
        self.db.search('" OR *')  # User punctuation cannot inject FTS syntax.

    def test_header_threading_not_subject_matching(self):
        first=self.insert()
        self.db.insert(parse_message(raw('two',refs='<one@example.org>')),'INBOX','1',2,True)
        self.db.insert(parse_message(raw('unrelated')),'INBOX','1',3,True)
        self.assertEqual(len(self.db.thread(first)),2)

    def test_independent_agenda_acknowledgements(self):
        mid=self.insert(); self.db.save_analysis(mid,classification())
        item=self.db.pending()[0]['id']
        self.db.acknowledge(item,'calendar','done','event-123')
        self.db.acknowledge(item,'calendar','done','event-123')
        self.assertEqual(self.db.pending()[0]['obsidian_status'],'pending')
        with self.assertRaises(ValueError): self.db.acknowledge(item,'calendar','done','different-event')
        self.db.acknowledge(item,'obsidian','done','Agenda.md')
        self.assertEqual(self.db.pending(),[])

    def test_hallucinated_agenda_becomes_source_review_without_blocking_alert(self):
        item=parse_message(raw())
        output=classification(); output['agenda_items'][0]['evidence']='Transfer money now'
        with patch('ou_email_agent.models.request',return_value={'message':{'content':json.dumps(output)}}):
            result=analyze(self.config,item)
        self.assertTrue(result['important'])
        self.assertEqual(result['rejected_agenda_candidates'],1)
        self.assertEqual(result['agenda_items'][0]['kind'],'review')
        self.assertEqual(result['agenda_items'][0]['evidence'],'')
        self.assertNotIn('Transfer money',json.dumps(result))

    def test_hallucinated_importance_evidence_still_rejected(self):
        output=classification(); output['evidence']='This never appeared in the source'
        with patch('ou_email_agent.models.request',return_value={'message':{'content':json.dumps(output)}}):
            with self.assertRaises(ValueError): analyze(self.config,parse_message(raw()))

    def test_valid_classification_requires_review(self):
        output=classification();output['agenda_items'][0]['needs_review']=False
        with patch('ou_email_agent.models.request',return_value={'message':{'content':json.dumps(output)}}):
            result=analyze(self.config,parse_message(raw()))
        self.assertTrue(result['agenda_items'][0]['needs_review'])

    def test_html_is_text_without_scripts(self):
        msg=b'Subject: Test\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>Hello</p><script>bad()</script><img src="https://example.org/tracker">'
        self.assertEqual(parse_message(msg)['body'].strip(),'Hello')

    def test_semantic_search_can_find_without_exact_keywords(self):
        mid=self.insert(raw(text='The purification protocol was approved.'))
        with self.db.db:
            self.db.db.execute('INSERT INTO chunks VALUES (?,?,?,?,?)',(mid,0,'text','test','[1,0]'))
        self.assertEqual(self.db.search('accepted method',vector=[1,0],model='test')[0]['id'],mid)

    def test_second_monitor_cannot_acquire_same_lock(self):
        with exclusive(self.temp.name):
            with self.assertRaises(RuntimeError):
                with exclusive(self.temp.name):
                    pass
        with exclusive(self.temp.name):
            pass

    def test_oversize_message_is_not_silently_checkpointed(self):
        fake=FakeIMAP({1:raw()})
        self.config['max_message_bytes']=10
        with self.assertRaises(RuntimeError): sync(self.config,self.db,'secret',lambda *_:fake)
        self.assertEqual(self.db.status()['messages'],0)
        self.assertIsNone(self.db.db.execute('SELECT last_uid FROM checkpoints').fetchone())
        self.assertIn('UID 1',self.db.state('oversize_message'))

    def test_sync_only_never_calls_model_even_with_backlog(self):
        self.insert(historical=False)
        self.config['background_analysis']=False
        with patch('ou_email_agent.models.request') as call:
            self.assertEqual(process_background(self.config,self.db),{'analyzed':0,'failed':0})
            call.assert_not_called()

    def test_background_never_analyzes_or_embeds_history(self):
        self.insert(historical=True)
        self.config['background_analysis']=True
        with patch('ou_email_agent.models.request') as call:
            self.assertEqual(process_background(self.config,self.db),{'analyzed':0,'failed':0})
            call.assert_not_called()

    def test_background_processes_only_one_new_message_without_embeddings(self):
        self.insert(historical=False)
        self.db.insert(parse_message(raw('second')),'INBOX','1',2,False)
        self.config['background_analysis']=True
        with patch('ou_email_agent.models.analyze',return_value=classification()) as classify, \
             patch('ou_email_agent.models.embed') as embedding:
            self.assertEqual(process_background(self.config,self.db),{'analyzed':1,'failed':0})
            self.assertEqual(classify.call_count,1)
            embedding.assert_not_called()
        self.assertEqual(self.db.status()['unanalyzed'],1)

    def test_background_failure_is_not_automatically_retried(self):
        self.insert(historical=False)
        self.config['background_analysis']=True
        with patch('ou_email_agent.models.analyze',side_effect=ValueError('Synthetic failure')) as classify:
            self.assertEqual(process_background(self.config,self.db)['failed'],1)
            self.assertEqual(process_background(self.config,self.db),{'analyzed':0,'failed':0})
            self.assertEqual(classify.call_count,1)

    def test_classification_releases_model_after_request(self):
        with patch('ou_email_agent.models.request',return_value={'message':{'content':json.dumps(classification())}}) as call:
            analyze(self.config,parse_message(raw()))
        self.assertEqual(call.call_args.args[2]['keep_alive'],0)


if __name__=='__main__': unittest.main()
