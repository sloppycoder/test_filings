uv run fa_sync.py --type filing --source gs://edgar2026/fa/filings -o filings all_filings.txt
uv run fa_sync.py --type index --source gs://edgar2026/fa/cache/content -o cache_content  all_filings.txt
