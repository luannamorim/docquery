.PHONY: serve serve-prod ingest eval test lint format

serve:
	uv run fastapi dev

serve-prod:
	uv run fastapi run

ingest:
	uv run python -m docquery.ingest.pipeline $(filter-out $@,$(MAKECMDGOALS))

eval:
	uv run python eval/run_eval.py

test:
	uv run pytest

lint:
	uv run ruff check src/ tests/

format:
	uv run ruff format src/ tests/

%:
	@:
