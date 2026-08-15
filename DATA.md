# data/ — corpus real (NÃO commitado)

Esta pasta é ignorada pelo git (`/data/` no `.gitignore`). Coloque aqui os
documentos reais/confidenciais a serem ingeridos. **Nunca** versione contratos
ou políticas da empresa.

Cada pasta vira um filtro de busca automaticamente — nada a configurar:

```
data/
  rh/ferias.md                → folders=["rh"]
  rh/beneficios/plano.pdf     → folders=["rh", "beneficios"]
  financeiro/2024/notas.xlsx  → folders=["financeiro", "2024"]
```

No `.env`:
```dotenv
INGEST_ROOT=data
```

Ingestão (recursiva — pega todas as subpastas de uma vez). Ingira sempre a
partir da mesma raiz: as pastas são relativas a ela.
```bash
curl -s -X POST localhost:8000/ingest -H 'content-type: application/json' -d '{"path":"data"}'
```

Consulta com escopo por setor:
```bash
curl -s -X POST localhost:8000/query -H 'content-type: application/json' \
  -d '{"query":"prazo de ferias","folders":["rh"]}'
```
