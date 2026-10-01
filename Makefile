.PHONY: setup data measure app test eval eval-holdout benchmark clean

setup:
	pip install -r requirements.txt && pip install -e .

data:
	python -m bi_copilot.cli generate

data-full:
	python -m bi_copilot.cli generate --full

measure:
	python -m bi_copilot.cli measure

app:
	streamlit run app/streamlit_app.py

test:
	pytest -q

benchmark:
	python scripts/build_benchmark.py

eval:
	python -m bi_copilot.cli eval

eval-holdout:
	python -m bi_copilot.cli eval --set holdout

clean:
	rm -f data/*.duckdb && find . -name __pycache__ -type d -exec rm -rf {} +
