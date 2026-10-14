# Choosing local models

The assistant you chat with (ChatGPT, Claude, another MCP host), the local classifier, and the embedding model have distinct roles. Changing the local classifier does not change which assistant can call MCP.

The default classifier is Qwen3 14B. A smaller model around 3–4B parameters typically needs less memory, and can process simple messages faster on the same hardware, though actual speed and energy depend on quantization, context, offloading and workload. Fitting in VRAM alone does not ensure a cool laptop.

Ollama lists the default `qwen3:4b` artifact at about 2.5 GB and `qwen3:14b` at about 9.3 GB. Runtime needs additional memory. Qwen3's original dense family includes 1.7B and 4B rather than exactly 3B. A model's name or parameter count alone cannot establish its suitability for Japanese/English email.

Sources: [Qwen3 4B](https://ollama.com/library/qwen3:4b), [Qwen3 14B](https://ollama.com/library/qwen3:14b), [Qwen model family](https://qwenlm.github.io/blog/qwen3/).

## Expected tradeoffs to evaluate

- Simple keyword-like classification may hold up well; subtle requests, broadcast versus personal obligations, negation, quoted old deadlines and multilingual context are harder.
- Smaller models may make more source-evidence or date errors. Validation rejects unsupported importance evidence and prevents unsupported agenda details from being forwarded, but cannot catch every mistake or a missed important message.
- Evidence rejection increases failed classifications; the monitor deliberately does not retry them continuously.
- Evaluate false negatives (missed actionable mail), false positives (unwanted alerts), event/date correctness, evidence validity, failures and latency on a manually labelled sample. There is no project-specific accuracy claim comparing 4B with 14B yet.

To try the smaller classifier:

```powershell
ollama pull qwen3:4b
```

Set `model` to `qwen3:4b` in your private `config.json`, keeping `embedding_model` unchanged. Keep background analysis disabled while testing. Stop the monitor before manual `analyze` commands. Switching the setting affects future classification only; it does not silently overwrite accepted existing analyses.

EmbeddingGemma is a separate 300M-parameter embedding model. It controls semantic indexing/search, not importance decisions. Changing Qwen3 14B to a smaller classifier does **not** speed up an embedding-only index build. For retrieval-only use, skip installing the classifier entirely; the cloud/local MCP host can reason over retrieved source text itself. Tool responses are visible to the selected host.

Source: [EmbeddingGemma](https://ollama.com/library/embeddinggemma).
