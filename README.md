# repo for test filing used in various stages of the project

## Sync files from GCS

Run the sync script with `uv run`:

```bash
uv run fa_sync.py --source gs://bucket/path -o /local/destination all_filings.txt
```

Dry run without copying:

```bash
uv run fa_sync.py --source gs://bucket/path -o /local/destination --dry-run all_filings.txt
```

Force refresh even when local checksums match:

```bash
uv run fa_sync.py --source gs://bucket/path -o /local/destination --refresh all_filings.txt
```

Sync index files instead of filing files:

```bash
uv run fa_sync.py --type index --source gs://bucket/path -o /local/destination all_filings.txt
```
