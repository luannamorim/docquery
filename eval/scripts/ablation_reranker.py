"""Ablation study: RAGAS metrics and cost with vs without cross-encoder reranking.

Runs eval twice on the same dataset:
  - with_reranker:    default settings (reranker active, top-k from config)
  - without_reranker: reranker disabled by setting reranker_score_threshold
                      to +inf so all candidates are filtered out, then
                      bypassing by passing the raw retrieved points directly.

To avoid re-implementing the pipeline, "without_reranker" uses
reranker_top_k = retrieval_top_k (no filtering) and
reranker_score_threshold = +1e9 means all pass but are unordered.
A cleaner approach: the reranker is skipped by setting an env override
RERANKER_TOP_K=0 so rerank() returns the raw points unchanged.

Usage:
    python eval/scripts/ablation_reranker.py [--dataset eval/dataset_v2.json]

Output:
    eval/results/ablation/with_reranker.json
    eval/results/ablation/without_reranker.json
    eval/results/ablation/summary.json
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))


def _run_eval(label: str, dataset_path: Path, reranker_enabled: bool, n_samples: int = 30) -> dict:
    """Run RAGAS eval with or without reranker."""
    from docquery.config import get_settings as _gs

    _gs.cache_clear()

    if not reranker_enabled:
        # Set threshold so high that nothing is filtered, and top_k matches retrieval
        # Effectively: skip cross-encoder ordering
        os.environ["RERANKER_SCORE_THRESHOLD"] = "1000.0"
        os.environ["RERANKER_TOP_K"] = "20"
    else:
        os.environ.pop("RERANKER_SCORE_THRESHOLD", None)
        os.environ.pop("RERANKER_TOP_K", None)

    _gs.cache_clear()
    settings = _gs()

    from datetime import datetime, timezone

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
    # Stratified sample
    from collections import defaultdict
    buckets: dict[str, list] = defaultdict(list)
    for item in items:
        buckets[item.get("type", "factual")].append(item)
    sampled = []
    per_type = max(1, n_samples // len(buckets))
    for bucket in buckets.values():
        sampled.extend(bucket[:per_type])
    sampled = sampled[:n_samples]

    print(f"\n[{label}] Running {len(sampled)} queries (reranker={'ON' if reranker_enabled else 'OFF'})...")

    ragas_samples = []
    cost_rows = []
    for i, item in enumerate(sampled, 1):
        print(f"  [{i}/{len(sampled)}] {item['question'][:55]}...")
        try:
            r = query_pipeline(item["question"], settings=settings)
            ragas_samples.append(
                SingleTurnSample(
                    user_input=item["question"],
                    response=r["answer"],
                    retrieved_contexts=[s["text"] for s in r["sources"]],
                    reference=item["ground_truth"],
                )
            )
            cost_rows.append({
                "tokens_in": r.get("tokens_in", 0),
                "tokens_out": r.get("tokens_out", 0),
                "cost_usd": r.get("cost_usd", 0.0),
            })
        except Exception as e:
            print(f"    WARNING: skipping — {e}")

    api_key = settings.openai_api_key.get_secret_value()
    ragas_llm = LangchainLLMWrapper(ChatOpenAI(model=settings.llm_model, api_key=api_key, max_tokens=2048))
    ragas_embeddings = LangchainEmbeddingsWrapper(OpenAIEmbeddings(api_key=api_key))
    dataset = EvaluationDataset(samples=ragas_samples)
    result = evaluate(dataset=dataset, metrics=[faithfulness, answer_relevancy, context_precision, context_recall], llm=ragas_llm, embeddings=ragas_embeddings)

    scores = result._repr_dict
    total_cost = sum(r["cost_usd"] for r in cost_rows)
    mean_cost = total_cost / len(cost_rows) if cost_rows else 0.0

    return {
        "label": label,
        "reranker_enabled": reranker_enabled,
        "timestamp": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "n_samples": len(ragas_samples),
        "scores": scores,
        "cost": {
            "total_cost_usd": round(total_cost, 6),
            "mean_cost_per_query_usd": round(mean_cost, 6),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Ablation study: reranker on vs off")
    parser.add_argument("--dataset", type=Path, default=Path("eval/dataset_v2.json"))
    parser.add_argument("--samples", type=int, default=30)
    args = parser.parse_args()

    if not args.dataset.exists():
        sys.exit(f"Dataset not found: {args.dataset}")

    out_dir = Path("eval/results/ablation")
    out_dir.mkdir(parents=True, exist_ok=True)

    with_result = _run_eval("with_reranker", args.dataset, reranker_enabled=True, n_samples=args.samples)
    without_result = _run_eval("without_reranker", args.dataset, reranker_enabled=False, n_samples=args.samples)

    (out_dir / "with_reranker.json").write_text(json.dumps(with_result, indent=2))
    (out_dir / "without_reranker.json").write_text(json.dumps(without_result, indent=2))

    metrics = ["faithfulness", "answer_relevancy", "context_precision", "context_recall"]
    summary = {
        "with_reranker": {m: with_result["scores"].get(m, 0) for m in metrics},
        "without_reranker": {m: without_result["scores"].get(m, 0) for m in metrics},
        "delta": {
            m: round(with_result["scores"].get(m, 0) - without_result["scores"].get(m, 0), 4)
            for m in metrics
        },
        "cost_delta_per_query_usd": round(
            with_result["cost"]["mean_cost_per_query_usd"]
            - without_result["cost"]["mean_cost_per_query_usd"],
            6,
        ),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    print("\n=== Ablation Summary ===")
    header = f"{'Metric':<25}  {'With reranker':>14}  {'Without reranker':>16}  {'Delta':>8}"
    print(header)
    print("-" * len(header))
    for m in metrics:
        w = with_result["scores"].get(m, 0)
        wo = without_result["scores"].get(m, 0)
        print(f"{m:<25}  {w:>14.4f}  {wo:>16.4f}  {w-wo:>+8.4f}")
    print(f"\n{'cost/query (USD)':<25}  {with_result['cost']['mean_cost_per_query_usd']:>14.5f}  {without_result['cost']['mean_cost_per_query_usd']:>16.5f}  {summary['cost_delta_per_query_usd']:>+8.5f}")
    print(f"\nResults in {out_dir}/")


if __name__ == "__main__":
    main()
