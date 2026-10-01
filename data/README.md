The DuckDB warehouse is generated, not committed (it exceeds GitHub's 100 MB file limit).

    python -m bi_copilot.cli generate          # demo, 12,000 customers, ~30 s
    python -m bi_copilot.cli generate --full   # 60,000 customers, ~4.2M rows, ~2 min

The Streamlit app builds the demo warehouse automatically on first start.
