from pathlib import Path
import io
import json
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from ou_email_agent.config import load_config
from ou_email_agent.embeddings import index_embeddings, text_chunks
from ou_email_agent.models import EmbeddingError, embed, search
from ou_email_agent.store import Store


class EmbeddingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.config=load_config(Path(__file__).resolve().parents[1] / 'config.example.json')
        self.config['data_dir']=self.temp.name
        self.db=Store(self.temp.name)

    def tearDown(self):
        self.db.close(); self.temp.cleanup()

    def insert(self,text,uid=1):
        mail={'message_id':str(uid),'references':[],'subject':'','sender':'test@example.org',
              'recipients':'recipient@example.org','date':'2026-10-14T00:00:00+00:00',
              'body':text,'attachments':[]}
        self.db.insert(mail,'INBOX','1',uid,True)
        return self.db.db.execute('SELECT MAX(id) FROM messages').fetchone()[0]

    def test_multilingual_chunk_boundaries_preserve_all_text(self):
        text=('Japanese 日本語・実験報告書 🧬 résumé\n'*200)
        parts=list(text_chunks(text,1800,0))
        self.assertEqual(''.join(parts),text)
        self.assertTrue(all(0<len(x.encode())<=1800 for x in parts))
        overlap=list(text_chunks(text,1800,120))
        self.assertTrue(all(0<len(x.encode())<=1800 for x in overlap))

    def test_context_error_is_classified_without_leaking_response(self):
        error=urllib.error.HTTPError('http://localhost',400,'bad',{},
            io.BytesIO(b'{"error":"input length exceeds the context length", "private":"SECRET EMAIL"}'))
        with patch('ou_email_agent.models.request',side_effect=error):
            with self.assertRaises(EmbeddingError) as caught: embed(self.config,'private input')
        self.assertEqual(caught.exception.code,'input_too_long')
        self.assertEqual(caught.exception.http_status,400)
        self.assertNotIn('SECRET',str(caught.exception))

    def test_other_400_error_is_not_misidentified_as_length(self):
        error=urllib.error.HTTPError('http://localhost',400,'bad',{},io.BytesIO(b'{"error":"invalid model parameter"}'))
        with patch('ou_email_agent.models.request',side_effect=error):
            with self.assertRaises(EmbeddingError) as caught: embed(self.config,'text')
        self.assertEqual(caught.exception.code,'http_error')

    def test_cpu_two_threads_no_truncation_and_unload(self):
        with patch('ou_email_agent.models.request',return_value={'embeddings':[[1,0]]}) as call:
            self.assertEqual(embed(self.config,'text'),[[1,0]])
        payload=call.call_args.args[2]
        self.assertEqual(payload['options'],{'num_gpu':0,'num_thread':2})
        self.assertFalse(payload['truncate'])
        self.assertEqual(payload['keep_alive'],0)

    def test_invalid_vectors_rejected(self):
        for value in [[],[[]],[[float('nan')]],[[1],[2]]]:
            with self.subTest(value=value),patch('ou_email_agent.models.request',return_value={'embeddings':value}):
                with self.assertRaises(EmbeddingError): embed(self.config,'text')

    def test_budget_resume_and_reopen_do_not_repeat_successful_chunks(self):
        mid=self.insert('z'*5000)
        accepted=[]
        def fake(config,text): accepted.append(text); return [[1,0]]
        with patch('ou_email_agent.embeddings.embed',side_effect=fake):
            first=index_embeddings(self.config,self.db,request_budget=1,message_id=mid)
            self.assertEqual(first['requests'],1)
            self.assertEqual(first['completed'],0)
            self.db.close(); self.db=Store(self.temp.name)
            self.assertEqual(self.db.db.execute('SELECT status FROM embedding_jobs').fetchone()[0],'pending')
            second=index_embeddings(self.config,self.db,request_budget=8,message_id=mid)
            self.assertEqual(second['completed'],1)
        self.assertEqual(len(accepted),len(list(text_chunks('\n'+'z'*5000))))
        self.assertEqual(self.db.db.execute('SELECT COUNT(*) FROM chunks WHERE vector_json IS NULL').fetchone()[0],0)

    def test_adaptive_split_preserves_all_characters_and_completes(self):
        text='abcdefghijklmnop'
        mid=self.insert(text)
        accepted=[]
        def fake(config,text):
            if len(text)>6: raise EmbeddingError('input_too_long',400)
            accepted.append(text); return [[1,0]]
        with patch('ou_email_agent.embeddings.embed',side_effect=fake):
            result=index_embeddings(self.config,self.db,request_budget=32,message_id=mid)
        self.assertEqual(result['completed'],1)
        self.assertGreater(result['splits'],0)
        self.assertEqual(sorted(''.join(accepted)),sorted('\n'+text))

    def test_context_splitting_obeys_request_budget(self):
        mid=self.insert('abcde'*20)
        with patch('ou_email_agent.embeddings.embed',side_effect=EmbeddingError('input_too_long',400)) as call:
            result=index_embeddings(self.config,self.db,request_budget=2,message_id=mid)
        self.assertEqual(call.call_count,2)
        self.assertEqual(result['requests'],2)
        self.assertEqual(result['completed'],0)
        self.assertEqual(result['pending_jobs'],1)

    def test_failed_job_does_not_block_next_and_requires_explicit_retry(self):
        self.insert('healthy',1); self.insert('reject',2)
        def fake(config,text):
            if 'reject' in text: raise EmbeddingError('server_error',503)
            return [[1,0]]
        with patch('ou_email_agent.embeddings.embed',side_effect=fake):
            result=index_embeddings(self.config,self.db,limit=2)
        self.assertEqual(result['completed'],1)
        self.assertEqual(result['failed'],1)
        error=self.db.db.execute("SELECT error_code,http_status FROM embedding_jobs WHERE status='failed'").fetchone()
        self.assertEqual(tuple(error),('server_error',503))
        with patch('ou_email_agent.embeddings.embed',return_value=[[1,0]]) as call:
            self.assertEqual(index_embeddings(self.config,self.db,limit=2)['requests'],0)
            call.assert_not_called()
            self.assertEqual(index_embeddings(self.config,self.db,retry_failed=True)['completed'],1)

    def test_partial_vectors_remain_searchable_with_coverage_warning(self):
        mid=self.insert('protein '*500)
        with patch('ou_email_agent.embeddings.embed',return_value=[[1,0]]):
            index_embeddings(self.config,self.db,request_budget=1)
        with patch('ou_email_agent.models.embed',return_value=[[1,0]]):
            result=search(self.config,self.db,'protein')
        self.assertEqual(result['results'][0]['id'],mid)
        self.assertIn('incomplete',result['warning'])
        self.assertEqual(result['semantic_coverage']['complete_messages'],0)

    def test_legacy_completed_vectors_are_preserved(self):
        mid=self.insert('old indexed message')
        with self.db.db:
            self.db.db.execute('INSERT INTO chunks VALUES (?,?,?,?,?)',(mid,0,'old indexed message',self.config['embedding_model'],'[1,0]'))
        self.db.close(); self.db=Store(self.temp.name)
        with patch('ou_email_agent.embeddings.embed') as call:
            result=index_embeddings(self.config,self.db)
            call.assert_not_called()
        self.assertEqual(result['requests'],0)
        self.assertEqual(self.db.db.execute('SELECT status FROM embedding_jobs').fetchone()[0],'complete')


if __name__=='__main__': unittest.main()
