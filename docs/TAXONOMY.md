# Content organization (taxonomy)

The corpus is organized along **two axes** so queries can be scoped precisely.

## Where the corpus lives (ingest root)

Ingestion reads the folder set by `INGEST_ROOT` **recursively** — every
supported file in it and all its subfolders is indexed in a single pass. The
same applies to a remote folder URI (`sharepoint://…`, `gdrive://…`), which is
the root for everything fetched under it.

- **Demo / examples:** `INGEST_ROOT=docs` (default). Example files live under
  `docs/contracts`, `docs/policies`, `docs/manuals`.
- **Real (confidential) corpus:** put it in `data/`, which is **gitignored**,
  and set `INGEST_ROOT=data`. Never commit company contracts or policies.

```dotenv
INGEST_ROOT=data
```

## 1. Sector / subject → by FOLDER (automatic)

Every folder a document sits in, relative to the ingested root, becomes a
filterable facet on its chunks. There is **nothing to configure**: the structure
the corpus already has *is* the taxonomy, and a folder created in SharePoint is
a usable filter on the next ingest.

```
data/                            (INGEST_ROOT)
  rh/ferias.md                   → folders=["rh"]
  rh/beneficios/plano.pdf        → folders=["rh", "beneficios"]
  financeiro/2024/notas.xlsx     → folders=["financeiro", "2024"]
  aviso.md                       → folders=[]        (file at the root)
```

A SharePoint library works the same way, with the sector folders at its root:

```
sharepoint://contoso.sharepoint.com/sites/Corp/Documentos    (the ingested URI)
  RH/Ferias/politica.docx        → folders=["rh", "ferias"]
  Financeiro/notas.xlsx          → folders=["financeiro"]
```

Names are lowercased and Unicode-normalized; spaces and accents are kept, so
you filter by the folder name as it is displayed (`"recursos humanos"`).

Like `clearance_level`, folders are derived **server-side** from the path —
never read from frontmatter, because they gate retrieval scope.

**Ingest from the same root every time.** Folders are relative to whatever was
ingested, so pulling `data/rh/` after `data/` re-indexes those documents with
`folders=[]`. Ingest the corpus root, not a subfolder of it.

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
fields (`clearance`, `folders`) are **ignored** in frontmatter — they are
derived server-side at ingest time.

## Querying with scope

A folder name matches at **any depth**, so you can ask for a whole sector or
one subject inside it without knowing where it sits in the tree:

```jsonc
{"query": "prazo de férias", "folders": ["rh"]}              // the RH sector, nested folders included
{"query": "...", "folders": ["beneficios"]}                  // one subject, wherever it lives
{"query": "...", "folders": ["rh", "financeiro"]}            // either sector
{"query": "...", "folders": ["rh"], "tags": ["2024"]}        // RH, tagged 2024
{"query": "...", "source": "data/rh/ferias.md"}              // a single document
{"query": "..."}                                             // global (every folder)
```

Each citation in the response includes the source's `folders` for traceability.

Two folders with the same name at different depths share one facet — filtering
`["rh"]` matches `data/rh/` and `data/financeiro/rh/` alike. Google Drive allows
sibling folders with identical names; those merge into the same facet too.

## Summary

- **Folder** = *which sector / subject it belongs to* — derived automatically.
- **Frontmatter** = *whom / what it is about* (Acme, Globex, HR…).

Drop each file into the right folder; nothing else to set up.
