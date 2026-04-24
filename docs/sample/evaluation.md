# Evaluation

docquery is evaluated end-to-end with [RAGAS](https://docs.ragas.io/), an open-source framework for benchmarking Retrieval-Augmented Generation systems. Evaluation runs against a curated dataset of question/ground-truth pairs and produces four reproducible metrics that separate retrieval quality from generation quality.

## Running the Eval

```bash
# Install eval-only dependencies (ragas, langchain-openai)
uv sync --extra eval

# Run against the live API (requires docker compose up + ingested docs)
make eval
```

Results are saved to `eval/results/<timestamp>.json` with per-row scores and the aggregate summary.

## RAGAS Metrics

Four metrics are used: **faithfulness** (measures whether the answer is grounded in the retrieved contexts), **answer relevancy** (measures how relevant the answer is to the question), **context precision** (measures whether retrieved contexts are ranked with the most relevant first), and **context recall** (measures whether all relevant information was retrieved).

### Faithfulness

Faithfulness asks: *does the generated answer make any claim that is not supported by the retrieved context?* RAGAS breaks the answer into atomic statements and checks each one against the retrieved chunks using an LLM judge. A score of 1.0 means every claim in the answer is traceable to the context; lower scores indicate hallucination.

High faithfulness is the primary guarantee a RAG system offers over a raw LLM: the model is kept honest by grounding.

### Answer Relevancy

Answer relevancy asks: *does the answer actually address the question that was asked?* RAGAS generates synthetic questions from the answer and measures cosine similarity with the original query. An answer that is technically faithful but off-topic (e.g. answering a different but related question) scores low here.

### Context Precision

Context precision asks: *of the chunks the retriever returned, are the relevant ones ranked first?* This is a retrieval-side metric — it is indifferent to what the LLM does with the context. Low precision means the reranker is letting noise into the top-k, diluting the LLM's signal.

### Context Recall

Context recall asks: *can the ground-truth answer be reconstructed from the retrieved chunks?* RAGAS decomposes the reference answer into statements and checks how many can be attributed to retrieved context. This is the recall side of retrieval: if relevant information exists in the corpus but was not retrieved, this metric drops.

Context recall is typically the hardest metric to improve — it reflects both corpus coverage (is the answer in your docs at all?) and retrieval quality (does the retriever find it?).

## Metrics Summary

| Metric            | What it measures                                 | Weak score suggests               |
| ----------------- | ------------------------------------------------ | --------------------------------- |
| Faithfulness      | Answer grounded in retrieved context             | LLM is hallucinating              |
| Answer Relevancy  | Answer addresses the question                    | Off-topic generation              |
| Context Precision | Retrieved contexts ranked by relevance           | Reranker admitting noise          |
| Context Recall    | All relevant information is retrieved            | Chunks missing from top-k         |

## Interpreting Results

A healthy docquery baseline separates concerns cleanly: faithfulness and answer relevancy should be near 0.9 (generation is well-grounded), context precision should be 0.85+ (the reranker works), and context recall is usually the bottleneck — around 0.6-0.7 is common even for well-tuned RAG systems.

When iterating, check the per-row scores in the output JSON. A global recall of 0.65 often turns out to be two or three questions scoring 0 because the reference answer references information that is not in the corpus at all. Fixing the dataset (or adding the missing documentation) is higher-leverage than tuning `retrieval_top_k`.

## Dataset

The eval dataset lives at `eval/dataset.json` as a list of `{question, ground_truth}` pairs. Each run produces `SingleTurnSample` records — the user input, the system's generated response, the retrieved contexts, and the reference answer — which RAGAS scores against the four metrics above.

## LLM Judge Variance

Three of the four RAGAS metrics (faithfulness, answer relevancy, context recall) use an LLM as a judge. Run-to-run variance of ±0.02-0.05 on aggregate scores is expected. For a rigorous baseline, run the eval 3-5 times and report the mean and standard deviation rather than a single number.
