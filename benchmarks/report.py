"""Create a public report and plot from completed synthetic benchmark outputs."""
import argparse
import csv
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from benchmarks.run import metrics


def pct(value): return 'n/a' if value is None else f'{value*100:.1f}%'


def notification_metrics(rows):
    accepted=[r for r in rows if r['error'] is None]
    flagged=[r for r in accepted if r['prediction']['important'] and r['prediction']['category'] in {'direct_request','deadline','meeting_change'}]
    tp=sum(r['expected_important'] for r in flagged); fp=len(flagged)-tp
    positives=sum(r['expected_important'] for r in rows)
    return {'precision':tp/len(flagged) if flagged else None,'recall':tp/positives if positives else None,
            'true_positive':tp,'false_positive':fp,'missed_important':positives-tp}


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('directory',type=Path)
    parser.add_argument('--plot',action='store_true'); args=parser.parse_args()
    manifest=json.loads((args.directory/'manifest.json').read_text())
    runs=[]
    for name in manifest['models']:
        run=json.loads((args.directory/(name.replace(':','-')+'.json')).read_text(encoding='utf-8'))
        if not run['complete']: raise SystemExit('A model run is incomplete; no final comparison generated')
        assert run['dataset_sha256']==manifest['dataset_sha256']
        runs.append(run)
    comparison=[]
    for run in runs:
        m=metrics(run['rows']); alerts=notification_metrics(run['rows'])
        comparison.append({'model':run['model'],**m,'notification_policy':alerts,
            'median_seconds':run['latency']['median_seconds'],'p95_seconds':run['latency']['p95_seconds'],
            'loaded_vram_gib':run['ollama_loaded_model']['size_vram']/1024**3,
            'cold_warmup_seconds':run['cold_warmup_seconds'],
            'strict_accuracy_cluster_bootstrap_95':run['strict_accuracy_cluster_bootstrap_95']})
    (args.directory/'comparison.json').write_text(json.dumps(comparison,indent=2)+'\n')
    lines=['# Synthetic classification pilot: Qwen3 model sizes','',
        '48 synthetic messages: 24 scenarios in English and Japanese, with 24 important and 24 nonimportant messages. '
        'Assistant-authored labels; not independently human-labelled. This is a diagnostic pilot, not a measured accuracy claim for real university mail.','',
        f"Hardware: {manifest['hardware']['name']}, {manifest['hardware']['total_mib']} MiB VRAM. "
        f"Ollama {manifest['ollama']['version']}; {manifest['context_tokens']}-token context; thinking off; temperature 0; seed {manifest['seed']}. "
        'All models are Q4_K_M and were run sequentially with full GPU offload.','',
        '| Model | Validated accuracy | Raw importance accuracy | Failed outputs | Important recall | False important decisions | Median / p95 warm latency | Loaded model VRAM |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for r in comparison:
        lines.append(f"| {r['model']} | {pct(r['strict_accuracy'])} | {pct(r['raw_importance_accuracy'])} | "
                     f"{r['validation_or_request_failures']}/48 | {pct(r['alert_recall'])} | {r['false_alerts']}/24 | "
                     f"{r['median_seconds']:.2f} / {r['p95_seconds']:.2f} s | {r['loaded_vram_gib']:.2f} GiB |")
    lines+=['','Validated accuracy counts every failed/rejected output as incorrect. Importance recall counts rejected important messages as misses. '
            'False important decisions above are before the desktop notification category filter.','',
            '| Model | English validated accuracy | Japanese validated accuracy | Importance precision | Desktop notification recall | Desktop false alerts | Cold warm-up |',
            '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for run,r in zip(runs,comparison):
        a=r['notification_policy']
        lines.append(f"| {r['model']} | {pct(run['by_language']['en']['strict_accuracy'])} | {pct(run['by_language']['ja']['strict_accuracy'])} | "
                     f"{pct(r['alert_precision'])} | {pct(a['recall'])} | {a['false_positive']}/24 | {r['cold_warmup_seconds']:.2f} s |")
    lines+=['','Desktop metrics assume new mail and the three default alert categories, with no priority sender overrides. '
            'This benchmark does not deliver notifications or modify the mailbox.','',
            '## Error details','']
    for run in runs:
        failures=[r['id'] for r in run['rows'] if r['error'] is not None]
        wrong=[r['id'] for r in run['rows'] if r['error'] is None and r['prediction']['important']!=r['expected_important']]
        interval=run['strict_accuracy_cluster_bootstrap_95']
        lines += [f"### {run['model']}",'',
                  'Rejected/error cases: '+(', '.join(failures) or 'none')+'.','',
                  'Accepted but incorrect importance decisions: '+(', '.join(wrong) or 'none')+'.','',
                  f"Exploratory 95% scenario-cluster bootstrap interval for validated accuracy: {pct(interval[0])}–{pct(interval[1])}.",'']
    lines+=['## Interpretation limits','',
        '- Only 24 scenario designs; English/Japanese versions are paired. The exploratory interval does not describe real-mail performance.',
        '- Each case was run once. These results do not quantify model variability across seeds or repeated runs.',
        '- Many negative examples explicitly say no action is needed. Real messages can be substantially more ambiguous.',
        '- Evidence validation measures exact source matching, not factual correctness of dates, summaries or event proposals. Calendar extraction was not scored.',
        '- Latencies are warm GPU measurements on this RTX 5080 laptop. Smaller GPUs and CPU-only machines were not timed. Model VRAM allocation is not a universal minimum hardware requirement.',
        '- Models use the current unchanged production prompt. A separately evaluated simpler prompt might improve smaller-model performance; these results do not prove a model is intrinsically incapable.',
        '- Inspect per-case JSON outputs and run the same suite on your own hardware. Use independent human labels on representative private mail before trusting automated alerts.','',
        'See [methodology](../../README.md), [dataset](../../synthetic-v1.json), and [runner](../../run.py).']
    if args.plot: lines.insert(6,'![Accuracy, latency and VRAM comparison](comparison.png)\n')
    (args.directory/'README.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    with (args.directory/'cases.csv').open('w',newline='',encoding='utf-8') as f:
        fields=['model','id','language','expected_important','raw_important','accepted','predicted_important','error_type','wall_seconds']
        writer=csv.DictWriter(f,fieldnames=fields); writer.writeheader()
        for run in runs:
            for r in run['rows']:
                writer.writerow({'model':run['model'],'id':r['id'],'language':r['language'],
                    'expected_important':r['expected_important'],'raw_important':r['raw_important'],
                    'accepted':r['error'] is None,'predicted_important':r['prediction']['important'] if r['prediction'] else '',
                    'error_type':r['error']['type'] if r['error'] else '', 'wall_seconds':round(r['wall_seconds'],4)})
    if args.plot:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        fig,axes=plt.subplots(1,3,figsize=(12,4.2)); x=list(range(len(comparison)))
        labels=[r['model'].split(':')[1] for r in comparison]
        axes[0].bar([i-.18 for i in x],[r['raw_importance_accuracy']*100 for r in comparison],width=.35,label='Raw decision',color='#94a3b8')
        axes[0].bar([i+.18 for i in x],[r['strict_accuracy']*100 for r in comparison],width=.35,label='Validated',color='#2563eb')
        axes[0].set_ylim(0,105); axes[0].set_ylabel('Correct messages (%)'); axes[0].legend(fontsize=8)
        axes[0].set_title('Classification accuracy')
        for ax,field,title,ylabel,color in [(axes[1],'median_seconds','Warm median latency','Seconds per email','#0f766e'),
                                            (axes[2],'loaded_vram_gib','Loaded model VRAM','GiB','#7c3aed')]:
            bars=ax.bar(x,[r[field] for r in comparison],color=color,width=.6)
            ax.bar_label(bars,fmt='%.2f',padding=3); ax.set_title(title); ax.set_ylabel(ylabel); ax.margins(y=.2)
        for ax in axes:
            ax.set_xticks(x,labels); ax.set_xlabel('Qwen3 model tag'); ax.spines[['top','right']].set_visible(False)
        fig.suptitle('48 synthetic EN/JA emails • unchanged prompt • RTX 5080 Laptop GPU',fontsize=12)
        fig.text(.5,.01,'Pilot only: assistant-authored labels; 24 paired scenarios; no real-mail or CPU-speed claim.',ha='center',fontsize=9)
        fig.tight_layout(rect=(0,.05,1,.94)); fig.savefig(args.directory/'comparison.png',dpi=180); fig.savefig(args.directory/'comparison.svg'); plt.close(fig)
    print(json.dumps(comparison,indent=2))

if __name__=='__main__': main()
