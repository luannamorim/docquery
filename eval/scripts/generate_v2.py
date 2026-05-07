"""Generate eval/dataset_v2.json — ~100 stratified Q&A pairs from docs/sample/.

Reads each markdown doc in chunks, prompts gpt-4o-mini to generate questions
per type, then writes dataset_v2.json for manual review before committing.

Usage:
    python eval/scripts/generate_v2.py [--docs docs/sample] [--out eval/dataset_v2.json]

Question types generated:
    factual      (~40) — single-document, single-hop, explicit answer in text
    multi_hop    (~25) — requires combining info from ≥2 passages or docs
    comparative  (~20) — "what is the difference between X and Y"
    unanswerable (~15) — question plausible but outside corpus; expected "I don't know"

Seeds (system prompts) are documented in-file so results are reproducible.
After running, REVIEW the output manually before committing — LLMs hallucinate
ground truths.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "src"))

from openai import OpenAI

from docquery.config import get_settings

# ---------------------------------------------------------------------------
# Prompt templates (seeds) — modify these to change generation behaviour
# ---------------------------------------------------------------------------

FACTUAL_SEED = """\
You are building an evaluation dataset for a RAG system over technical documentation.

Given the text below (from {source}), generate {n} factual questions \
with ground-truth answers.
Rules:
- Each question must be answerable using ONLY the provided text — no external knowledge
- Ground truth must be a complete, self-contained answer (2-5 sentences)
- Vary specificity: include detail-level questions (exact config values, \
API fields, etc.)
- Do NOT generate yes/no questions

Return a JSON array of objects: \
[{{"question": "...", "ground_truth": "...", "source_doc": "{source}"}}]
Return ONLY the JSON, no markdown fences.

Text:
{text}
"""

MULTI_HOP_SEED = """\
You are building an evaluation dataset for a RAG system over technical documentation.

Given the texts below (from multiple docs), generate {n} multi-hop questions \
that require synthesising information from at least two of the provided passages.
Rules:
- Question cannot be answered from a single passage alone
- Ground truth must cite which concepts come from which source
- Prefer questions about how components interact \
(e.g. how does chunking affect retrieval quality?)

Return a JSON array: \
[{{"question": "...", "ground_truth": "...", "source_doc": "multiple"}}]
Return ONLY the JSON, no markdown fences.

Texts:
{text}
"""

COMPARATIVE_SEED = """\
You are building an evaluation dataset for a RAG system over technical documentation.

Given the texts below, generate {n} comparative questions \
(differences, trade-offs, when-to-use).
Rules:
- Each question must have a clear "A vs B" or "X vs Y" structure
- Ground truth must explain the difference, not just list features
- Base questions on entities actually present in the texts

Return a JSON array: \
[{{"question": "...", "ground_truth": "...", "source_doc": "multiple"}}]
Return ONLY the JSON, no markdown fences.

Texts:
{text}
"""

UNANSWERABLE_SEED = """\
You are building an evaluation dataset for a RAG system over technical documentation.

The corpus covers: {topics}.

Generate {n} questions that CANNOT be answered from this corpus — the topic is plausible
and related, but the specific information is not in the docs.
Examples: pricing, deployment to specific clouds, integration with \
external tools not mentioned.

Rules:
- Questions must sound reasonable for this domain
- Ground truth is always: \
"This information is not covered in the available documentation."
- Do NOT ask about concepts completely unrelated to the domain

Return a JSON array: [{{"question": "...", "ground_truth": \
"This information is not covered in the available documentation.", \
"source_doc": "N/A"}}]
Return ONLY the JSON, no markdown fences.
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_doc(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _call_llm(client: OpenAI, model: str, prompt: str) -> list[dict]:
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.7,
        max_tokens=4096,
    )
    raw = response.choices[0].message.content or "[]"
    raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    try:
        items = json.loads(raw)
        if not isinstance(items, list):
            items = []
    except json.JSONDecodeError as e:
        print(f"  WARNING: JSON parse error — {e}. Skipping batch.", file=sys.stderr)
        items = []
    return items


def generate_factual(
    client: OpenAI, model: str, docs: dict[str, str], n_per_doc: int
) -> list[dict]:
    results = []
    for source, text in docs.items():
        print(f"  factual: {source} ({n_per_doc} questions)")
        prompt = FACTUAL_SEED.format(source=source, n=n_per_doc, text=text[:6000])
        items = _call_llm(client, model, prompt)
        for item in items:
            item.setdefault("type", "factual")
            item.setdefault("source_doc", source)
        results.extend(items[:n_per_doc])
    return results


def generate_multi_hop(
    client: OpenAI, model: str, docs: dict[str, str], n: int
) -> list[dict]:
    print(f"  multi_hop: {n} questions across all docs")
    combined = "\n\n---\n\n".join(
        f"[{src}]\n{txt[:2000]}" for src, txt in list(docs.items())[:4]
    )
    prompt = MULTI_HOP_SEED.format(n=n, text=combined)
    items = _call_llm(client, model, prompt)
    for item in items:
        item.setdefault("type", "multi_hop")
    return items[:n]


def generate_comparative(
    client: OpenAI, model: str, docs: dict[str, str], n: int
) -> list[dict]:
    print(f"  comparative: {n} questions")
    combined = "\n\n---\n\n".join(
        f"[{src}]\n{txt[:2000]}" for src, txt in list(docs.items())[:4]
    )
    prompt = COMPARATIVE_SEED.format(n=n, text=combined)
    items = _call_llm(client, model, prompt)
    for item in items:
        item.setdefault("type", "comparative")
    return items[:n]


def generate_unanswerable(
    client: OpenAI, model: str, topics: str, n: int
) -> list[dict]:
    print(f"  unanswerable: {n} questions")
    prompt = UNANSWERABLE_SEED.format(topics=topics, n=n)
    items = _call_llm(client, model, prompt)
    for item in items:
        item.setdefault("type", "unanswerable")
    return items[:n]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate eval/dataset_v2.json")
    parser.add_argument("--docs", type=Path, default=Path("docs/sample"))
    parser.add_argument("--out", type=Path, default=Path("eval/dataset_v2.json"))
    args = parser.parse_args()

    settings = get_settings()
    client = OpenAI(api_key=settings.openai_api_key.get_secret_value() or None)
    model = settings.llm_model

    doc_files = sorted(args.docs.glob("*.md"))
    if not doc_files:
        sys.exit(f"No .md files found in {args.docs}")

    docs = {f.name: _read_doc(f) for f in doc_files}
    topics = ", ".join(docs.keys())

    print(f"Generating dataset_v2 from {len(docs)} docs using {model}...")

    # Target: ~40 factual (6-7/doc), ~25 multi-hop, ~20 comparative, ~15 unanswerable
    n_docs = len(docs)
    factual_per_doc = max(1, 40 // n_docs)

    all_items: list[dict] = []
    all_items.extend(generate_factual(client, model, docs, factual_per_doc))
    all_items.extend(generate_multi_hop(client, model, docs, 25))
    all_items.extend(generate_comparative(client, model, docs, 20))
    all_items.extend(generate_unanswerable(client, model, topics, 15))

    # Enforce schema: keep only expected keys, add type if missing
    clean = []
    for item in all_items:
        if not item.get("question") or not item.get("ground_truth"):
            continue
        clean.append(
            {
                "question": item["question"].strip(),
                "ground_truth": item["ground_truth"].strip(),
                "source_doc": item.get("source_doc", "multiple"),
                "type": item.get("type", "factual"),
            }
        )

    args.out.write_text(json.dumps(clean, indent=2, ensure_ascii=False))
    print(f"\nGenerated {len(clean)} items → {args.out}")
    print("IMPORTANT: Review manually before committing — verify ground truths.")


if __name__ == "__main__":
    main()
