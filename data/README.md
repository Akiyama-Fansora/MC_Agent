# Local Runtime Data

This directory is intentionally almost empty in Git.

At runtime MC_Agent may create:

- `mcagent.sqlite`
- `vector_index.npz`
- `crawler_exports/`
- `agent_memory.jsonl`
- `crawl_ledger.jsonl`
- crawler reports and local research archives

Those files can contain large harvested datasets, local paths, credentials-adjacent logs, or generated indexes, so they are ignored by Git. Recreate them locally with:

```powershell
python ingest.py
```

