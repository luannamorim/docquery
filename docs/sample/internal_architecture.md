---
clearance: 5
---

# Internal Architecture Notes

This document contains internal engineering notes on docquery's architecture that are not
intended for public distribution. Access requires clearance level 5.

## Production Deployment Topology

The production deployment runs three Qdrant nodes in a distributed cluster behind a load
balancer. Each node holds a full replica of the collection to ensure query availability
during rolling restarts. Write operations (ingest) are coordinated through the primary node
and replicated asynchronously to followers within 200ms.

## Internal Cost Targets

The engineering team targets a mean cost of under $0.002 per query for the gpt-4o-mini
configuration with the default reranker settings. Costs above $0.005 per query trigger an
alert and indicate that the context window sent to the LLM is larger than expected, usually
caused by a regression in the reranker threshold configuration.

## Embedding Model Upgrade Path

The internal roadmap plans to evaluate replacing all-MiniLM-L6-v2 with a domain-adapted
model fine-tuned on technical documentation corpora. Preliminary experiments suggest a 12%
improvement in context recall on the internal gold-set, at the cost of a 40% increase in
embedding inference time per batch.

## Known Limitations

1. The BM25 sparse vector does not account for multi-word phrases (bigrams). Queries
   containing compound technical terms (e.g. "reciprocal rank fusion") may under-rank
   passages where the phrase appears as a unit.
2. The context expansion window can introduce off-topic neighbor chunks when documents
   have tightly packed, highly diverse sections (e.g. API reference pages with many
   independent endpoint descriptions).
3. The SHA256-based chunk ID is stable across re-ingests but does not detect partial
   content updates within a document section — the entire source file must change for
   updated chunks to be re-indexed.
