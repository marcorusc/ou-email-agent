from __future__ import annotations

import argparse
import getpass
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys

from .config import load_config, credential_store, credential_service, password
from .store import Store


def main():
    parser=argparse.ArgumentParser(description='Local OU email monitoring and MCP search')
    parser.add_argument('--config')
    sub=parser.add_subparsers(dest='command',required=True)
    sub.add_parser('credentials',help='Enter password privately and save in Windows Credential Manager')
    doctor=sub.add_parser('doctor',help='Check TLS, optional login, models and local index')
    doctor.add_argument('--login',action='store_true')
    sub.add_parser('sync',help='Download one read-only batch')
    analyze=sub.add_parser('analyze',help='Classify and embed one batch')
    analyze.add_argument('--limit',type=int,default=1)
    embed_cmd=sub.add_parser('embed',help='Bounded CPU-only embedding repair; does not run the analysis model')
    embed_cmd.add_argument('--limit',type=int,default=1)
    embed_cmd.add_argument('--max-requests',type=int,default=8)
    embed_cmd.add_argument('--message',type=int)
    embed_cmd.add_argument('--retry-failed',action='store_true')
    sub.add_parser('retry-analysis',help='Reset failed model attempts for retry')
    sub.add_parser('monitor',help='Monitor mail and issue desktop alerts until stopped')
    search=sub.add_parser('search')
    search.add_argument('query'); search.add_argument('--sender',default='')
    search.add_argument('--recipient',default='')
    search.add_argument('--after',default=''); search.add_argument('--before',default='')
    sub.add_parser('status')
    sub.add_parser('mcp',help='Run stdio MCP server for ChatGPT/Codex')
    sub.add_parser('pull-models',help='Download the configured local models using Ollama')
    args=parser.parse_args()
    config=load_config(args.config)
    handlers = [logging.StreamHandler(sys.stderr)] if sys.stderr is not None else []
    if args.command == 'monitor':
        Path(config['data_dir']).mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(Path(config['data_dir'])/'monitor.log',
                                           maxBytes=1000000, backupCount=2, encoding='utf-8'))
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s',handlers=handlers)
    from . import mailbox,models,service
    if args.command=='credentials':
        db=Store(config['data_dir'])
        secret=None
        stage='opening Windows Credential Manager'
        try:
            db.state('setup_error','')
            db.state('setup_stage',stage)
            store=credential_store()
            stage='waiting for password in local window'
            db.state('setup_stage',stage)
            secret=getpass.getpass('OU email password (hidden, never sent to ChatGPT): ')
            if not secret:
                print('No password entered. Nothing was saved.',flush=True)
                raise ValueError('Password cannot be empty')
            stage='verifying mailbox login'
            db.state('setup_stage',stage)
            print('Checking the password with the OU mail server...',flush=True)
            # Verify before persisting; failed authentication never overwrites a working secret.
            result=mailbox.check_connection(config,secret)
            stage='saving verified password'
            db.state('setup_stage',stage)
            store.set_password(credential_service(config),config['username'],secret)
            if store.get_password(credential_service(config),config['username']) != secret:
                raise RuntimeError('Credential storage verification failed')
            db.state('setup_stage','password verified and saved')
        except Exception as exc:
            db.state('setup_error',stage+': '+type(exc).__name__)
            print('Setup stopped while '+stage+' ('+type(exc).__name__+').',flush=True)
            if isinstance(exc,mailbox.imaplib.IMAP4.error):
                print('The mail server rejected the login or mailbox command. You can retry with another password.',flush=True)
            elif isinstance(exc,(OSError,TimeoutError)):
                print('A connection or local storage operation failed; this does not prove the password is wrong.',flush=True)
            raise
        finally:
            secret=None
            db.close()
        print('Login verified. Password saved in Windows Credential Manager.')
        print(json.dumps(result,indent=2,ensure_ascii=False))
        return
    if args.command=='mcp':
        from .mcp_server import server
        server(config).run(transport='stdio')
        return
    if args.command=='monitor':
        service.monitor(config)
        return
    if args.command=='pull-models':
        import urllib.request
        for name in [config['model'],config['embedding_model']]:
            print('Downloading ' + name,flush=True)
            req=urllib.request.Request(config['ollama_url']+'/api/pull',
                data=json.dumps({'model':name,'stream':True}).encode(),headers={'Content-Type':'application/json'})
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            last=-1
            with opener.open(req,timeout=600) as response:
                for line in response:
                    item=json.loads(line)
                    if 'error' in item:
                        raise RuntimeError(item['error'])
                    percent=int(item.get('completed',0)*100/max(1,item.get('total',1)))
                    if 'total' not in item or percent//10!=last:
                        print(item['status'] + (f' {percent}%' if 'total' in item else ''),flush=True)
                        last=percent//10
        return
    db=Store(config['data_dir'])
    try:
        if args.command=='doctor':
            result={}
            result['imap']=mailbox.check_connection(config,password(config) if args.login else None)
            try:
                tags=models.request(config,'/api/tags',timeout=10)
                available={m['name'] for m in tags.get('models',[])}
                result['models']={m: m in available or m+':latest' in available
                                  for m in [config['model'],config['embedding_model']]}
                result['ollama_version']=models.request(config,'/api/version',timeout=10)
            except Exception as exc:
                result['ollama_error']=type(exc).__name__
            result['index']=db.status()
        elif args.command=='sync':
            with service.exclusive(config['data_dir']):
                result=mailbox.sync(config,db,password(config))
        elif args.command=='analyze':
            with service.exclusive(config['data_dir']):
                result=models.process(config,db,limit=max(1,min(args.limit,100)))
        elif args.command=='embed':
            from .embeddings import index_embeddings
            result=index_embeddings(config,db,limit=args.limit,request_budget=args.max_requests,
                                    message_id=args.message,retry_failed=args.retry_failed)
        elif args.command=='retry-analysis':
            with service.exclusive(config['data_dir']),db.db:
                db.db.execute('UPDATE messages SET analysis_attempts=0,analysis_error=NULL WHERE analysis_json IS NULL')
            result={'retry':'Failed analyses reset; run analyze or monitor.'}
        elif args.command=='search':
            result=models.search(config,db,args.query,sender=args.sender,recipient=args.recipient,
                                 after=args.after,before=args.before)
        else:
            result=db.status()
        print(json.dumps(result,indent=2,ensure_ascii=False))
    finally:
        db.close()


def entrypoint():
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:
        # Do not echo arbitrary server responses (which might contain account data).
        print(f'{type(exc).__name__}: operation failed. Check server/model configuration; run doctor.',file=sys.stderr)
        sys.exit(1)


if __name__=='__main__':
    entrypoint()
