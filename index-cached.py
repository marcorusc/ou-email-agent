"""One-time GPU indexing of cached mail; never invokes the analysis model."""
import json
from pathlib import Path

from ou_email_agent.config import load_config
from ou_email_agent.embeddings import index_embeddings
from ou_email_agent.models import request
from ou_email_agent.service import exclusive
from ou_email_agent.store import Store, now


def main():
    config = load_config()
    # Overrides apply only to this process; normal search/monitor settings stay unchanged.
    config['embedding_num_gpu'] = 99
    config['model_keep_alive'] = '30s'
    progress = Path(config['data_dir']) / 'indexing-progress.json'
    totals = dict(completed=0, failed=0, requests=0, chunks_saved=0, splits=0)

    def report(**fields):
        result = {'at': now(), 'device': 'GPU', **totals, **fields}
        temporary = progress.with_suffix('.tmp')
        temporary.write_text(json.dumps(result, indent=2))
        temporary.replace(progress)
        print(json.dumps(result), flush=True)

    with exclusive(config['data_dir'], 'catchup.lock'):
        db = Store(config['data_dir'])
        model = config['embedding_model']
        try:
            queue = [r[0] for r in db.db.execute('''SELECT m.id FROM messages m
                LEFT JOIN embedding_jobs j ON j.message=m.id AND j.model=?
                WHERE j.status IS NULL OR j.status='pending' ORDER BY m.id DESC''', (model,))]
            report(state='running', remaining_messages=len(queue))
            while queue:
                mid = queue[0]
                result = index_embeddings(config, db, message_id=mid, request_budget=32)
                for key in totals:
                    totals[key] += result[key]
                state = db.db.execute('SELECT status FROM embedding_jobs WHERE message=? AND model=?',
                                      (mid, model)).fetchone()
                if result['failed']:
                    raise RuntimeError('Embedding job failed; inspect status before retrying')
                if state and state[0] == 'complete':
                    queue.pop(0)
                elif result['requests'] == 0:
                    raise RuntimeError('No progress; stopping')
                report(state='running', remaining_messages=len(queue))
            report(state='complete', remaining_messages=0, index_status=db.status())
        except BaseException as error:
            report(state='stopped', error_type=type(error).__name__)
            raise
        finally:
            try:
                # A final empty embedding request releases this model immediately.
                request(config, '/api/embed', {'model': model, 'input': '', 'keep_alive': 0,
                                              'options': {'num_gpu': 99}}, timeout=30)
            finally:
                db.close()


if __name__ == '__main__':
    main()
