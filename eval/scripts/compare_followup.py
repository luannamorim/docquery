"""Does resolving a follow-up actually retrieve the right document?

The claim behind conversation history is narrow and testable: a follow-up like
"e a multa por atraso?" carries no anchor, so retrieving on it directly finds
whatever shares the word "multa", while resolving it against the opening
question first finds the document the user meant.

This measures exactly that and nothing else — retrieval hit rate on the
follow-up, with and without the rewrite. It is deliberately not a RAGAS run:
the metric that matters here is whether the expected source came back at all,
and generation quality is already covered by run_eval.py.

Usage:
    python eval/scripts/compare_followup.py [--dataset eval/dataset_multiturn.json]

Requires Qdrant running with docs/ ingested, and OPENAI_API_KEY set.
"""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from openai import OpenAI
from qdrant_client import QdrantClient

from docquery.config import get_settings
from docquery.generate.contextualize import contextualize
from docquery.retrieve.hybrid import retrieve


def _sources(query: str, qdrant, settings) -> list[str]:
    points = retrieve(query, qdrant, settings)
    return [(p.payload or {}).get("source", "") for p in points]


def main() -> None:
    parser = argparse.ArgumentParser(description="Follow-up rewrite: on vs off")
    parser.add_argument("--dataset", default="eval/dataset_multiturn.json")
    parser.add_argument("--output", default="eval/results/followup")
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
    openai_client = OpenAI(
        api_key=settings.openai_api_key.get_secret_value() or None,
        timeout=settings.openai_timeout_seconds,
        max_retries=settings.openai_max_retries,
    )

    rows = []
    for i, item in enumerate(items, 1):
        expected = item["expected_source_contains"]
        print(f"  [{i}/{len(items)}] {item['follow_up'][:55]}...")

        raw_hit = any(
            expected in s for s in _sources(item["follow_up"], qdrant, settings)
        )
        resolved = contextualize(
            item["follow_up"], [item["opening"]], settings, openai_client
        )
        rewritten_hit = any(expected in s for s in _sources(resolved, qdrant, settings))

        rows.append(
            {
                "opening": item["opening"],
                "follow_up": item["follow_up"],
                "resolved": resolved,
                "expected_source_contains": expected,
                "hit_without_rewrite": raw_hit,
                "hit_with_rewrite": rewritten_hit,
            }
        )

    without = sum(r["hit_without_rewrite"] for r in rows)
    with_ = sum(r["hit_with_rewrite"] for r in rows)
    total = len(rows)

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "dataset": args.dataset,
                "total": total,
                "hit_rate_without_rewrite": without / total if total else 0.0,
                "hit_rate_with_rewrite": with_ / total if total else 0.0,
                "rows": rows,
            },
            indent=2,
            ensure_ascii=False,
        )
    )

    print("\n=== Follow-up retrieval ===")
    print(f"{'':<22}{'hit rate':>10}")
    print("-" * 32)
    print(f"{'without rewrite':<22}{without / total if total else 0:>10.2%}")
    print(f"{'with rewrite':<22}{with_ / total if total else 0:>10.2%}")
    # A regression here is the signal to stop: the rewrite is only worth its
    # LLM call while it finds documents the raw follow-up does not.
    if with_ < without:
        print("\nWARNING: the rewrite retrieved *fewer* expected sources.")
    print(f"\nResults in {out_dir}/")


if __name__ == "__main__":
    main()
