.PHONY: serve serve-prod ingest eval eval-v2 generate-dataset compare-chunkers ablation-reranker security-suite test lint format

serve:
	uv run fastapi dev

serve-prod:
	uv run fastapi run

ingest:
	uv run python -m docquery.ingest.pipeline $(filter-out $@,$(MAKECMDGOALS))

eval:
	uv run python eval/run_eval.py

eval-v2:
	uv run python eval/run_eval.py --dataset eval/dataset_v2.json

generate-dataset:
	uv run python eval/scripts/generate_v2.py

compare-chunkers:
	uv run python eval/scripts/compare_chunkers.py

ablation-reranker:
	uv run python eval/scripts/ablation_reranker.py

security-suite:
	uv run python eval/security/injection_suite.py

test:
	uv run pytest

lint:
	uv run ruff check src/ tests/

format:
	uv run ruff format src/ tests/

%:
	@:
