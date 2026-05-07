"""Shared utilities for eval scripts."""

from collections import defaultdict


def sample_stratified(items: list[dict], n: int) -> list[dict]:
    """Sample n items proportionally from each 'type' bucket."""
    buckets: dict[str, list] = defaultdict(list)
    for item in items:
        buckets[item.get("type", "factual")].append(item)

    per_type = max(1, n // len(buckets))
    result: list[dict] = []
    for bucket in buckets.values():
        result.extend(bucket[:per_type])
    return result[:n]
