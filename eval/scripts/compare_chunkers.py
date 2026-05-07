"""Compare RAGAS metrics across chunking strategies.

Runs ingest + eval for each of: markdown, recursive, semantic.
Saves results to eval/results/chunker_comparison/<strategy>.json.

Usage:
    uv run python eval/scripts/compare_chunkers.py \
        [--docs docs/sample] [--dataset eval/dataset_v2.json]

Requirements:
    - Qdrant running (docker compose up -d qdrant)
    - OPENAI_API_KEY set
    - langchain-experimental installed for semantic strategy (uv sync --extra chunking)
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from datetime import UTC

from _eval_utils import sample_stratified

from docquery.config import get_settings
from docquery.ingest.pipeline import ingest_path

STRATEGIES = ["markdown", "recursive", "semantic"]


def run_strategy(
    strategy: str, docs_path: Path, dataset_path: Path, output_dir: Path
) -> dict:
    os.environ["CHUNKER_STRATEGY"] = strategy
    get_settings.cache_clear()

    settings = get_settings()
    print(f"\n[{strategy}] Re-ingesting {docs_path} ...")
    result = ingest_path(docs_path, settings=settings)
    print(
        f"[{strategy}] Ingested {result['chunks']} chunks"
        f" (deleted {result['deleted']} orphans)"
    )

    print(f"[{strategy}] Running RAGAS eval on {dataset_path} ...")
    from datetime import datetime

    from langchain_openai import ChatOpenAI, OpenAIEmbeddings
    from ragas import EvaluationDataset, SingleTurnSample, evaluate
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.llms import LangchainLLMWrapper
    from ragas.metrics import (
        answer_relevancy,
        context_precision,
        context_recall,
        faithfulness,
    )

    from docquery.generate.rag import query_pipeline

    items = json.loads(dataset_path.read_text())
    sampled = sample_stratified(items, n=30)

    samples = []
    cost_rows = []
    for i, item in enumerate(sampled, 1):
        print(f"  [{i}/{len(sampled)}] {item['question'][:55]}...")
        try:
            r = query_pipeline(item["question"], settings=settings)
            samples.append(
                SingleTurnSample(
                    user_input=item["question"],
                    response=r["answer"],
                    retrieved_contexts=[s["text"] for s in r["sources"]],
                    reference=item["ground_truth"],
                )
            )
            cost_rows.append(
                {
                    "tokens_in": r.get("tokens_in", 0),
                    "tokens_out": r.get("tokens_out", 0),
                    "cost_usd": r.get("cost_usd", 0.0),
                }
            )
        except Exception as e:
            print(f"    WARNING: skipping — {e}")

    api_key = settings.openai_api_key.get_secret_value()
    ragas_llm = LangchainLLMWrapper(
        ChatOpenAI(model=settings.llm_model, api_key=api_key, max_tokens=2048)
    )
    ragas_embeddings = LangchainEmbeddingsWrapper(OpenAIEmbeddings(api_key=api_key))
    dataset = EvaluationDataset(samples=samples)
    eval_result = evaluate(
        dataset=dataset,
        metrics=[faithfulness, answer_relevancy, context_precision, context_recall],
        llm=ragas_llm,
        embeddings=ragas_embeddings,
    )

    scores = eval_result._repr_dict
    total_cost = sum(r["cost_usd"] for r in cost_rows)

    payload = {
        "strategy": strategy,
        "timestamp": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "n_samples": len(samples),
        "chunks_ingested": result["chunks"],
        "scores": scores,
        "cost": {
            "total_cost_usd": round(total_cost, 6),
            "mean_cost_per_query_usd": round(
                total_cost / len(cost_rows) if cost_rows else 0, 6
            ),
        },
    }

    out_path = output_dir / f"{strategy}.json"
    out_path.write_text(json.dumps(payload, indent=2))
    print(f"[{strategy}] Saved → {out_path}")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare chunking strategies via RAGAS"
    )
    parser.add_argument("--docs", type=Path, default=Path("docs/sample"))
    parser.add_argument("--dataset", type=Path, default=Path("eval/dataset_v2.json"))
    parser.add_argument(
        "--strategies", nargs="+", default=STRATEGIES, choices=STRATEGIES
    )
    args = parser.parse_args()

    output_dir = Path("eval/results/chunker_comparison")
    output_dir.mkdir(parents=True, exist_ok=True)

    results = {}
    for strategy in args.strategies:
        results[strategy] = run_strategy(strategy, args.docs, args.dataset, output_dir)

    print("\n=== Chunker Comparison Summary ===")
    metrics = [
        "faithfulness",
        "answer_relevancy",
        "context_precision",
        "context_recall",
    ]
    header = (
        f"{'Strategy':<12}"
        + "".join(f"  {m[:10]:<12}" for m in metrics)
        + "  cost/query"
    )
    print(header)
    print("-" * len(header))
    for strategy, payload in results.items():
        scores = payload["scores"]
        cost = payload["cost"]["mean_cost_per_query_usd"]
        row = (
            f"{strategy:<12}"
            + "".join(f"  {scores.get(m, 0):<12.4f}" for m in metrics)
            + f"  ${cost:.5f}"
        )
        print(row)


if __name__ == "__main__":
    main()
