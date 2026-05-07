"""RAGAS evaluation runner for docquery.

Usage:
    python eval/run_eval.py [--dataset eval/dataset.json] [--output eval/results/]

Requires:
    - Qdrant running and docs ingested (make ingest docs/sample/)
    - OPENAI_API_KEY set in environment or .env
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add src/ to path so docquery is importable when run directly
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from ragas import EvaluationDataset, SingleTurnSample, evaluate
from ragas.embeddings import LangchainEmbeddingsWrapper
from ragas.llms import LangchainLLMWrapper
from ragas.metrics import answer_relevancy, context_precision, context_recall, faithfulness  # noqa: E402

from docquery.config import get_settings
from docquery.generate.rag import query_pipeline

METRICS = [faithfulness, answer_relevancy, context_precision, context_recall]


def build_samples(
    dataset_path: Path, settings
) -> tuple[list[SingleTurnSample], list[dict]]:
    items = json.loads(dataset_path.read_text())
    samples = []
    cost_rows = []
    for i, item in enumerate(items, 1):
        question = item["question"]
        print(f"  [{i}/{len(items)}] {question[:60]}...")
        try:
            result = query_pipeline(question, settings=settings)
            samples.append(
                SingleTurnSample(
                    user_input=question,
                    response=result["answer"],
                    retrieved_contexts=[s["text"] for s in result["sources"]],
                    reference=item["ground_truth"],
                )
            )
            cost_rows.append(
                {
                    "question": question,
                    "tokens_in": result.get("tokens_in", 0),
                    "tokens_out": result.get("tokens_out", 0),
                    "cost_usd": result.get("cost_usd", 0.0),
                }
            )
        except Exception as e:
            print(f"    WARNING: skipping — {e}")
    return samples, cost_rows


def run(dataset_path: Path, output_dir: Path) -> None:
    settings = get_settings()
    print(f"Running eval on {dataset_path} ...")
    print("Step 1/3: Querying pipeline for each question")
    samples, cost_rows = build_samples(dataset_path, settings)

    if not samples:
        print("ERROR: No samples built — is Qdrant running and docs ingested?")
        sys.exit(1)

    print(f"\nStep 2/3: Running RAGAS on {len(samples)} samples")
    api_key = settings.openai_api_key.get_secret_value()
    # LangchainLLMWrapper/EmbeddingsWrapper needed: RAGAS metrics call embed_query internally,
    # which is a LangChain interface not implemented by ragas.embeddings.OpenAIEmbeddings.
    # max_tokens=2048 avoids truncation on faithfulness verdicts (RAGAS produces long JSON).
    ragas_llm = LangchainLLMWrapper(
        ChatOpenAI(model=settings.llm_model, api_key=api_key, max_tokens=2048)
    )
    ragas_embeddings = LangchainEmbeddingsWrapper(OpenAIEmbeddings(api_key=api_key))
    dataset = EvaluationDataset(samples=samples)
    result = evaluate(dataset=dataset, metrics=METRICS, llm=ragas_llm, embeddings=ragas_embeddings)

    # Print summary table
    print("\n=== RAGAS Results ===")
    scores = result._repr_dict
    for metric, score in scores.items():
        print(f"  {metric:<25} {score:.4f}")

    # Save results
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"{timestamp}.json"

    df = result.to_pandas()
    rows = json.loads(df.to_json(orient="records"))

    total_cost = sum(r["cost_usd"] for r in cost_rows)
    mean_cost = total_cost / len(cost_rows) if cost_rows else 0.0

    payload = {
        "timestamp": timestamp,
        "model": settings.llm_model,
        "embedding_model": settings.embedding_model,
        "reranker_model": settings.reranker_model,
        "n_samples": len(samples),
        "scores": scores,
        "cost": {
            "total_cost_usd": round(total_cost, 6),
            "mean_cost_per_query_usd": round(mean_cost, 6),
            "per_query": cost_rows,
        },
        "rows": rows,
    }
    output_path.write_text(json.dumps(payload, indent=2))

    print(f"\nStep 3/3: Results saved to {output_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run RAGAS evaluation")
    parser.add_argument(
        "--dataset",
        type=Path,
        default=Path("eval/dataset.json"),
        help="Path to evaluation dataset JSON",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("eval/results"),
        help="Directory to save results",
    )
    args = parser.parse_args()

    if not args.dataset.exists():
        parser.error(f"Dataset not found: {args.dataset}")

    run(args.dataset, args.output)


if __name__ == "__main__":
    main()
