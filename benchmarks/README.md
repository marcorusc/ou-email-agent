# Classification pilot benchmark

This benchmark exercises the **unchanged production `models.analyze` function**, including structured output, evidence validation, and agenda fallback. It does not open an IMAP connection or read the local email cache.

The fixed `synthetic-v1.json` set contains 24 scenarios, each in English and Japanese (48 messages; 24 important, 24 not important). Labels and rationales were authored by the assistant before evaluation. They are **not independent human annotations**. The translations are correlated, and many messages explicitly state whether action is needed. Treat this as a reproducible diagnostic pilot, not a representative estimate of real-mail accuracy or a competitive model leaderboard.

Scenarios include direct and indirect requests, mandatory broadcasts, deadline changes, cancellations, room changes, long quoted context, optional seminar announcements, completed requests, action assigned to someone else, and prompt-injection text. There is no evaluation of attachment reading, factual summaries, exact event dates, or calendar-write correctness.

The reference policy treats explicitly withdrawn non-meeting tasks with no remaining action as negative, but cancellations of the recipient's scheduled meetings as positive. Availability questions requiring a reply are positive even without a confirmed meeting. These policy choices are debatable: a user may want a withdrawal alert to stop work already underway. Scores measure agreement with the declared labels, and policy disagreements need human review rather than being treated as universally wrong model behavior.

## Run

Download the models with Ollama first, then from the source checkout:

```powershell
.venv\Scripts\python.exe benchmarks/run.py --models qwen3:1.7b qwen3:4b qwen3:14b --output benchmarks/results/my-run
```

The runner uses the generic example config, the synthetic recipient, temperature 0, seed 42, thinking disabled, an 8,192-token context and the production 1,800-token output limit. It requests full GPU offload, one model at a time, with four CPU threads. It retains the model between requests, inserts a one-second pause and a ten-second pause after every eight emails, and unloads the model after each model run. At a sampled GPU temperature of 85 C it stops after saving progress. Samples are taken after requests and do not establish peak temperature or energy use.

One cold warm-up request per model is measured separately and excluded from classification scores and warm latency. Each case is evaluated once in the same deterministic shuffled order. Accepted classifications are not retried. This provides no estimate of run-to-run variability. GPU latency on this laptop cannot be extrapolated directly to smaller GPUs or CPU-only systems.

Outputs include model digests and quantization, dataset and classifier hashes, Ollama version, model VRAM allocation, per-message responses/errors/timings, and aggregate scores. Use a fresh, empty output directory for every run. An incomplete run is saved for inspection, not silently rerun or merged. The runner refuses to overwrite earlier results or mix their metadata with a new run.

Generate tables and per-case CSV with `python benchmarks/report.py benchmarks/results/my-run`. Add `--plot` if matplotlib is installed to generate PNG and SVG figures. Plotting is optional and is not a runtime dependency of the email agent.

## Metrics

- **Strict accuracy:** correct important/not-important decisions that pass production validation, divided by all messages. Every rejected/error output counts as incorrect, including errors on negative messages.
- **Raw importance accuracy:** correct model importance booleans before production validation, divided by all messages. It shows whether a model understood the task but failed to provide usable evidence.
- **Accepted accuracy:** accuracy among accepted outputs only. Always read it with the failure count to avoid selection bias.
- **Importance precision/recall:** importance decisions after validation. Rejected positive messages count as missed important mail; rejected negatives do not produce false important decisions. The raw runner's `alert_*` fields refer to these importance decisions, before the separate notification-category policy.
- **Desktop notification metrics:** the report also applies the default category filter (`direct_request`, `deadline`, `meeting_change`), with no priority-sender overrides. Historical-mail suppression is ignored so this simulates newly received messages. These can differ from importance metrics.
- **Category agreement:** membership in the predeclared acceptable category set, with failures counted as incorrect. Request/deadline overlap is allowed where justified by the annotation.
- **Evidence failures:** rejected classifications and accepted classifications whose agenda evidence required fallback are reported separately. Verbatim evidence alone does not prove the proposed event is correct.
- **Latency:** median and nearest-rank p95 wall time for all scored requests, excluding explicit pauses and cold warm-up.
- **Memory:** Ollama's loaded-model VRAM allocation at the configured context, not a guarantee that the same capacity suffices under other drivers, contexts or concurrent applications.

Exploratory 95% accuracy intervals resample the 24 scenario clusters (keeping each English/Japanese pair together). They reflect this small synthetic set, not uncertainty over real mail or independently sampled users. Run the benchmark on colleagues' hardware and independently label representative local mail before deciding to automate alerts.

The set deliberately has a 50% important-message prevalence. Precision on a mailbox with mostly routine mail can differ substantially even if sensitivity and false-positive rate were unchanged. Do not treat this pilot's precision as your expected proportion of useful daily alerts.
