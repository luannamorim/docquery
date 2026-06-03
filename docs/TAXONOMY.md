# Organização do conteúdo (taxonomia)

O corpus é organizado em **dois eixos**, para que as perguntas possam ser
escopadas com precisão.

## 1. Tipo do documento → pela PASTA

A pasta define o `doc_type`, classificado **server-side** na ingestão (autores
não se auto-rotulam). O mapeamento pasta → tipo fica em `settings.type_policy`.

```
docs/
  contracts/   → doc_type=contract
  policies/    → doc_type=policy
  manuals/     → doc_type=manual   (adicione conforme a necessidade)
```

Configure uma vez no `.env` (valor é JSON — lista de pares `[prefixo, tipo]`,
o primeiro prefixo que casa vence):

```dotenv
type_policy=[["docs/contracts","contract"],["docs/policies","policy"],["docs/manuals","manual"]]
```

## 2. De quem / sobre o quê → no FRONTMATTER de cada arquivo

Metadados descritivos (não são fronteira de acesso) vão no cabeçalho YAML:

```markdown
---
title: Contrato de Fornecimento Acme 2024
entity: Acme
tags: [fornecimento, 2024]
---
```

Campos descritivos suportados: `title`, `entity`, `tags`. Campos que controlam
acesso/escopo (`clearance`, `doc_type`) são **ignorados** no frontmatter — eles
são definidos por política no servidor.

## Como consultar com escopo

```jsonc
{"query": "prazo de pagamento", "doc_types": ["contract"]}        // só contratos
{"query": "...", "doc_types": ["contract"], "tags": ["Acme"]}     // contratos da Acme
{"query": "...", "source": "docs/contracts/acme_fornecimento_2024.md"} // um documento
{"query": "..."}                                                  // global (todos os tipos)
```

`doc_type` aparece em cada citação da resposta, para rastreabilidade.

## Resumo

- **Pasta** = *que tipo é* (contrato, política, manual).
- **Frontmatter** = *de quem / sobre o quê* (Acme, Globex, RH…).

Defina o `type_policy` uma vez e depois é só colocar cada arquivo na pasta certa.
