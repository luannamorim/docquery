# Content organization (taxonomy)

The corpus is organized along **two axes** so queries can be scoped precisely.

## 1. Document type → by FOLDER

The folder determines `doc_type`, classified **server-side** at ingest time
(authors do not self-label). The folder → type mapping lives in
`settings.type_policy`.

```
docs/
  contracts/   → doc_type=contract
  policies/    → doc_type=policy
  manuals/     → doc_type=manual   (add more as needed)
```

Configure it once in `.env` (the value is JSON — a list of `[prefix, type]`
pairs; the first matching prefix wins):

```dotenv
type_policy=[["docs/contracts","contract"],["docs/policies","policy"],["docs/manuals","manual"]]
```

You can nest by company/owner inside a type folder; the type still resolves
from the prefix:

```
docs/contracts/acme/supply_2024.md   → doc_type=contract
```

## 2. Whom / what it is about → in each file's FRONTMATTER

Descriptive metadata (not an access boundary) goes in the YAML header:

```markdown
---
title: Acme Supply Agreement 2024
entity: Acme
tags: [supply, 2024]
---
```

Supported descriptive fields: `title`, `entity`, `tags`. Access/scope-gating
fields (`clearance`, `doc_type`) are **ignored** in frontmatter — they are set
by server-side policy.

## Querying with scope

```jsonc
{"query": "payment terms", "doc_types": ["contract"]}                 // contracts only
{"query": "...", "doc_types": ["contract"], "tags": ["supply"]}       // contracts tagged supply
{"query": "...", "source": "docs/contracts/acme_supply_2024.md"}      // a single document
{"query": "..."}                                                      // global (all types)
```

Each citation in the response includes the source's `doc_type` for
traceability.

## Summary

- **Folder** = *what kind it is* (contract, policy, manual).
- **Frontmatter** = *whom / what it is about* (Acme, Globex, HR…).

Set `type_policy` once, then just drop each file into the right folder.
