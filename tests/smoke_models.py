from pathlib import Path
"""Exercise installed local models with synthetic English/Japanese mail only."""
import json
import tempfile

from ou_email_agent.config import load_config
from ou_email_agent.mailbox import parse_message
from ou_email_agent.models import analyze, embed, request, search
from ou_email_agent.store import Store


def main():
    config=load_config(Path(__file__).resolve().parents[1] / 'config.example.json')
    examples=[
        ('Direct request', 'Alex, please send me the final protein purification report by Friday, October 16, 2026.', True),
        ('Meeting change', 'Alex, our lab meeting has been moved from October 15 at 10:00 JST to October 16 at 14:00 JST. Please update your calendar.', True),
        ('Routine announcement', 'Monthly institute newsletter: this issue contains general research highlights. No response is required.', False),
        ('Japanese deadline', 'Alexさん、実験報告書を2026年10月16日（金）までに提出してください。よろしくお願いします。', True),
    ]
    with tempfile.TemporaryDirectory() as directory:
        db=Store(directory)
        results=[]
        for index,(subject,body,expected) in enumerate(examples,1):
            raw=(f'From: lab@example.org\r\nTo: {config["username"]}\r\nSubject: {subject}\r\n'
                 f'Date: Wed, 14 Oct 2026 09:00:00 +0900\r\nMessage-ID: <synthetic-{index}@example.org>\r\n'
                 f'Content-Type: text/plain; charset=utf-8\r\n\r\n{body}').encode()
            mail=parse_message(raw)
            db.insert(mail,'INBOX','test',index,True)
            output=analyze(config,mail)
            assert output['important']==expected,(subject,output)
            db.save_analysis(index,output)
            vector=embed(config,subject+'\n'+body)[0]
            with db.db:
                db.db.execute('INSERT INTO chunks VALUES (?,?,?,?,?)',
                              (index,0,body,config['embedding_model'],json.dumps(vector)))
            results.append({'case':subject,'category':output['category'],'important':output['important'],
                            'agenda_candidates':len(output['agenda_items'])})
            print(json.dumps(results[-1],ensure_ascii=True),flush=True)
        found=search(config,db,'rescheduled laboratory meeting')
        assert found['results'] and found['results'][0]['id']==2,found
        print('Hybrid semantic search ranked the meeting change first.',flush=True)
        loaded=request(config,'/api/ps')
        print(json.dumps({'loaded_models':[{k:m.get(k) for k in ['name','size','size_vram','context_length']}
                                           for m in loaded.get('models',[])]}),flush=True)
        db.close()


if __name__=='__main__': main()
