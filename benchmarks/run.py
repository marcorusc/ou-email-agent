"""Run the actual classifier on a fixed synthetic dataset without opening a mailbox."""
from __future__ import annotations
import argparse
from collections import defaultdict
import hashlib
import json
import math
import platform
from pathlib import Path
import random
import statistics
import subprocess
import sys
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from ou_email_agent import models
from ou_email_agent.config import load_config


def divide(a,b): return a/b if b else None

def metrics(rows):
    total=len(rows)
    valid=[r for r in rows if r['error'] is None]
    tp=sum(r['expected_important'] and r['prediction']['important'] for r in valid)
    fp=sum(not r['expected_important'] and r['prediction']['important'] for r in valid)
    positives=sum(r['expected_important'] for r in rows)
    negatives=total-positives
    correct=sum(r['prediction']['important']==r['expected_important'] for r in valid)
    precision=divide(tp,tp+fp); recall=divide(tp,positives)
    return {'messages':total,'accepted':len(valid),'validation_or_request_failures':total-len(valid),
        'strict_accuracy':divide(correct,total),'accepted_accuracy':divide(correct,len(valid)),
        'alert_precision':precision,'alert_recall':recall,
        'alert_f1':divide(2*tp,2*tp+fp+(positives-tp)),
        'missed_important':positives-tp,'important_messages':positives,
        'false_alerts':fp,'nonimportant_messages':negatives,'false_alert_rate':divide(fp,negatives),
        'category_agreement':divide(sum(r['prediction']['category'] in r['acceptable_categories'] for r in valid),total),
        'agenda_evidence_rejected_messages':sum(r['prediction'].get('rejected_agenda_candidates',0)>0 for r in valid),
        'raw_importance_accuracy':divide(sum(r.get('raw_important') is not None and r['raw_important']==r['expected_important'] for r in rows),total)}


def cluster_interval(rows, repetitions=2000):
    # Resample scenarios rather than treating paired translations as independent.
    groups=defaultdict(list)
    for row in rows: groups[row['scenario']].append(row)
    values=list(groups.values()); rng=random.Random(20261014); estimates=[]
    for _ in range(repetitions):
        sample=[r for group in rng.choices(values,k=len(values)) for r in group]
        estimates.append(metrics(sample)['strict_accuracy'])
    estimates.sort()
    return [estimates[int(.025*repetitions)],estimates[int(.975*repetitions)]]


def gpu_sample():
    try:
        completed=subprocess.run(['nvidia-smi','--query-gpu=name,memory.total,memory.used,temperature.gpu,utilization.gpu',
                                  '--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=10)
        parts=[p.strip() for p in completed.stdout.splitlines()[0].split(',')]
        return {'name':parts[0],'total_mib':int(parts[1]),'used_mib':int(parts[2]),
                'temperature_c':int(parts[3]),'utilization_percent':int(parts[4])}
    except (OSError,ValueError,IndexError,subprocess.SubprocessError): return None


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--models',nargs='+',default=['qwen3:1.7b','qwen3:4b','qwen3:14b'])
    p.add_argument('--output',type=Path,default=ROOT/'benchmarks/results/local')
    p.add_argument('--seed',type=int,default=42)
    p.add_argument('--pause',type=float,default=1.0)
    args=p.parse_args(); args.output.mkdir(parents=True,exist_ok=True)
    if any(args.output.iterdir()):
        raise RuntimeError('Output directory is not empty; use a fresh directory to avoid mixing runs')
    dataset_path=ROOT/'benchmarks/synthetic-v1.json'; dataset=json.loads(dataset_path.read_text(encoding='utf-8'))
    dataset_hash=hashlib.sha256(dataset_path.read_bytes()).hexdigest()
    base=load_config(ROOT/'config.example.json')
    base.update(username=dataset['recipient'],timezone=dataset['timezone'],model_keep_alive='60s')
    actual_request=models.request
    installed=actual_request(base,'/api/tags')['models']
    version=actual_request(base,'/api/version')
    cases=list(dataset['cases']); random.Random(args.seed).shuffle(cases)
    manifest={'dataset_sha256':dataset_hash,'seed':args.seed,'python':platform.python_version(),
        'platform':platform.system(),'ollama':version,'hardware':gpu_sample(),
        'context_tokens':base['context_tokens'],'num_predict':1800,'think':False,'temperature':0,
        'num_gpu':99,'num_thread':4,'keep_alive':'60s','pause_between_cases_seconds':args.pause,
        'pause_after_every_8_cases_seconds':10,'models':args.models,
        'classifier_sha256':hashlib.sha256((ROOT/'ou_email_agent/models.py').read_bytes()).hexdigest(),
        'cases':len(cases),'limitations':dataset['scope']+' '+dataset['label_origin']}
    (args.output/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    for name in args.models:
        config={**base,'model':name}; slug=name.replace(':','-'); rows=[]; telemetry=[]
        info=next((m for m in installed if m['name'] in [name,name+':latest']),None)
        if info is None: raise RuntimeError('Model not installed: '+name)
        destination=args.output/(slug+'.json')
        capture={}
        def instrumented(c,path,payload=None,timeout=180):
            if path=='/api/chat':
                payload={**payload,'options':{**payload['options'],'seed':args.seed,'num_gpu':99,'num_thread':4}}
                capture['payload_options']=payload['options']
            result=actual_request(c,path,payload,timeout)
            if path=='/api/chat': capture['response']=result
            return result
        summary={'model':name,'model_info':info,'dataset_sha256':dataset_hash,'complete':False,'rows':rows}
        def save():
            temporary=destination.with_suffix('.tmp'); temporary.write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); temporary.replace(destination)
        try:
            # Unload this model first; measure one separate cold warm-up, excluded from scores.
            actual_request(config,'/api/generate',{'model':name,'keep_alive':0,'stream':False})
            start=time.perf_counter()
            with patch.object(models,'request',instrumented):
                try:
                    models.analyze(config,{'sender':'test@example.org','recipients':dataset['recipient'],
                        'subject':'Warm-up','date':'2026-10-14','body':'Automated receipt. No action or reply is required.'})
                except (ValueError,KeyError,TypeError): pass
            summary['cold_warmup_seconds']=time.perf_counter()-start
            loaded=actual_request(config,'/api/ps')['models']
            active=next((m for m in loaded if m['name'] in [name,name+':latest']),{})
            summary['ollama_loaded_model']={k:active.get(k) for k in ['name','size','size_vram','context_length']}
            for index,case in enumerate(cases,1):
                capture.clear(); start=time.perf_counter(); prediction=None; error=None
                try:
                    with patch.object(models,'request',instrumented): prediction=models.analyze(config,case['message'])
                except Exception as exc:
                    # Inputs are synthetic; validation messages are useful reproducible evidence.
                    error={'type':type(exc).__name__,'message':str(exc)[:500]}
                wall=time.perf_counter()-start; response=capture.get('response',{})
                raw=response.get('message',{}).get('content','')
                try: parsed=json.loads(raw); raw_important=parsed.get('important') if isinstance(parsed.get('important'),bool) else None
                except (ValueError,AttributeError): raw_important=None
                row={'id':case['id'],'scenario':case['scenario'],'language':case['language'],
                    'expected_important':case['important'],'acceptable_categories':case['acceptable_categories'],
                    'prediction':prediction,'error':error,'raw_important':raw_important,'raw_output':raw,
                    'wall_seconds':wall,'ollama_timing':{k:response.get(k) for k in ['total_duration','load_duration','prompt_eval_count','prompt_eval_duration','eval_count','eval_duration','done_reason']}}
                rows.append(row)
                sample=gpu_sample()
                if sample: telemetry.append(sample)
                save()
                print(json.dumps({'model':name,'case':index,'total':len(cases),'id':case['id'],
                    'accepted':error is None,'correct':error is None and prediction['important']==case['important'],
                    'seconds':round(wall,2)}),flush=True)
                if sample and sample['temperature_c']>=85: raise RuntimeError('GPU reached 85 C; stopping after saving progress')
                if index<len(cases): time.sleep(10 if index%8==0 else args.pause)
            summary.update(complete=True,metrics=metrics(rows),
                by_language={lang:metrics([r for r in rows if r['language']==lang]) for lang in ['en','ja']},
                strict_accuracy_cluster_bootstrap_95=cluster_interval(rows),
                latency={'median_seconds':statistics.median(r['wall_seconds'] for r in rows),
                         'p95_seconds':sorted(r['wall_seconds'] for r in rows)[math.ceil(.95*len(rows))-1]},
                post_request_gpu_samples=telemetry)
            save(); print(json.dumps({'model':name,'complete':True,'metrics':summary['metrics']}),flush=True)
        finally:
            actual_request(config,'/api/generate',{'model':name,'keep_alive':0,'stream':False})
        time.sleep(10)

if __name__=='__main__': main()
