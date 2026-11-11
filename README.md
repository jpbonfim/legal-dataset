# dataset-juridico

Código em Python para gerar um dataset de **legislação brasileira vigente** voltado a
*Continuous Pre-Training* (CPT) de LLMs jurídicos.

## Objetivo

Coletar, limpar e deduplicar textos normativos (Constituição, códigos, leis, decretos etc.)
e exportá-los em um formato pronto para treino.

## Pipeline (planejado)

1. **Descoberta** — lista de normas e metadados via LexML / Dados Abertos do Senado.
2. **Coleta** — download do texto compilado (ex.: Planalto), com cache local e rate limit.
3. **Limpeza** — remoção de dispositivos revogados (texto riscado), boilerplate e notas;
   normalização Unicode (NFC) preservando a estrutura de artigos, parágrafos e incisos.
4. **Filtragem e deduplicação** — filtros de qualidade, dedup exata (hash) e aproximada (MinHash).
5. **Exportação** — JSONL / Parquet, um documento por norma.

## Formato de saída

Um documento por linha, com o texto e metadados:

```json
{"id": "urn:lex:br:federal:lei:2002-01-10;10406", "text": "...", "tipo": "lei",
 "esfera": "federal", "data": "2002-01-10", "fonte": "planalto", "url": "...",
 "data_coleta": "2026-11-10"}
```

## Dados

Os dados gerados ficam em `data/` e **não são versionados** (ver `.gitignore`).

Textos de leis e atos oficiais não são protegidos por direito autoral
(Lei nº 9.610/1998, art. 8º, IV).
