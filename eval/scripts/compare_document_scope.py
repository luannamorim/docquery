"""Does the answer come from the document the question named?

A compound question about one contract came back citing the term from a
*different* contract: "qual o prazo do contrato da CRK e o valor" answered the
prazo from db1_2023.pdf and the valor from crk_2025.pdf. Two independent causes,
so this measures them as a 2x2 rather than as one fix:

  - decomposition — the cross-encoder scores every candidate against the *whole*
    question, and no clause states both a term and a price, so every candidate
    looks mediocre. Splitting only helps if each part is reranked against itself.
  - document affinity — the cross-encoder sees `payload["text"]` alone, which for
    most chunks of a contract says nothing about which contract it is. The file
    name reaches BM25 (`document_terms`) and dies at the rerank, so a clause
    titled "DO PRAZO" in the wrong contract wins on merit it does not have.

The metric is source precision on the contexts actually sent to the model, not a
RAGAS score. RAGAS grades the answer; the question here is which document the
answer was allowed to be built from, and that is upstream of faithfulness — an
answer faithful to the wrong contract scores well and is still wrong.

Usage:
    python eval/scripts/compare_document_scope.py

Requires Qdrant running with the corpus ingested, and OPENAI_API_KEY set (the
decomposition arms call the model; the arms with it off do not).
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from openai import OpenAI
from qdrant_client import QdrantClient

from docquery.config import Settings, get_settings
from docquery.generate.decompose import decompose
from docquery.retrieve.affinity import documents_in, named_sources
from docquery.retrieve.hybrid import retrieve
from docquery.retrieve.reranker import rerank

#: The four arms. Retrieval and rerank are driven directly rather than through
#: query_pipeline so no arm spends a generation call it does not need — the
#: contexts are the measurement.
ARMS = {
    "baseline": {"decompose": False, "affinity": False},
    "decompose": {"decompose": True, "affinity": False},
    "affinity": {"decompose": False, "affinity": True},
    "both": {"decompose": True, "affinity": True},
}


def _contexts(query: str, arm: dict, qdrant, settings: Settings, client) -> list[dict]:
    """The contexts one arm would send to the model, in the order it sends them."""
    tuned = settings.model_copy(
        update={
            "query_decompose_enabled": arm["decompose"],
            "query_document_affinity_enabled": arm["affinity"],
        }
    )
    out: list[dict] = []
    for part in decompose(query, tuned, client):
        found = retrieve(part, qdrant, tuned)
        named = (
            named_sources(part, documents_in(found))
            if tuned.query_document_affinity_enabled
            else set()
        )
        out.extend(rerank(part, found, tuned, prefer_sources=named))
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Document scope: 2x2 ablation")
    parser.add_argument("--dataset", default="eval/dataset_document_scope.json")
    parser.add_argument("--output", default="eval/results/document_scope")
    args = parser.parse_args()

    settings = get_settings()
    items = json.loads(Path(args.dataset).read_text())
    qdrant = QdrantClient(
        host=settings.qdrant_host,
        port=settings.qdrant_port,
        api_key=(
            settings.qdrant_api_key.get_secret_value()
            if settings.qdrant_api_key
            else None
        ),
        https=False,
    )
    client = OpenAI(
        api_key=settings.openai_api_key.get_secret_value() or None,
        timeout=settings.openai_timeout_seconds,
        max_retries=settings.openai_max_retries,
    )

    rows = []
    for i, item in enumerate(items, 1):
        expected = item["expected_source_contains"]
        competing = item["competing_source_contains"]
        print(f"  [{i}/{len(items)}] {item['query'][:55]}...")

        row = {"query": item["query"], "expected_source_contains": expected, "arms": {}}
        for name, arm in ARMS.items():
            contexts = _contexts(item["query"], arm, qdrant, settings, client)
            sources = [c["source"] for c in contexts]
            row["arms"][name] = {
                "contexts": len(sources),
                # Share of what the model reads that comes from the right
                # document. The headline number: the reported bug is a context
                # set that was half right.
                "precision": (
                    sum(expected in s for s in sources) / len(sources)
                    if sources and expected
                    else None
                ),
                # Did the wrong contract get into the prompt at all? Precision
                # can look healthy while one intruding passage supplies the one
                # fact the user asked for.
                "competing_present": bool(competing)
                and any(competing in s for s in sources),
                "top_source": sources[0] if sources else "",
            }
        rows.append(row)

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    def _mean(name: str, key: str) -> float | None:
        vals = [r["arms"][name][key] for r in rows if r["arms"][name][key] is not None]
        return sum(vals) / len(vals) if vals else None

    summary = {
        "timestamp": datetime.now(UTC).isoformat(),
        "dataset": args.dataset,
        "arms": {
            name: {
                "mean_precision": _mean(name, "precision"),
                "queries_with_competing_document": sum(
                    r["arms"][name]["competing_present"] for r in rows
                ),
            }
            for name in ARMS
        },
        "rows": rows,
    }
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False)
    )

    print("\n=== Source precision on the named document ===")
    print(f"{'arm':<12}{'precision':>11}{'intruding docs':>16}")
    print("-" * 39)
    for name in ARMS:
        mean = summary["arms"][name]["mean_precision"]
        intruding = summary["arms"][name]["queries_with_competing_document"]
        shown = f"{mean:.2%}" if mean is not None else "n/a"
        print(f"{name:<12}{shown:>11}{intruding:>16}")

    base = summary["arms"]["baseline"]["mean_precision"]
    both = summary["arms"]["both"]["mean_precision"]
    # Each arm costs something — decomposition an LLM call per question, affinity
    # a reordering that can demote a passage the cross-encoder liked. Neither is
    # worth keeping on a number that did not move.
    if base is not None and both is not None and both <= base:
        print("\nWARNING: neither switch improved precision. Do not ship them on.")
    print(f"\nResults in {out_dir}/")


if __name__ == "__main__":
    main()
